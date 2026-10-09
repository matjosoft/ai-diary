"""Daily Google Health (Fitbit) sync — run by cron at end of day.

Usage:
    python -m app.jobs.health_sync                       # sync today
    python -m app.jobs.health_sync --date 2026-07-12     # sync one day
    python -m app.jobs.health_sync --from 2026-07-01 --to 2026-07-12   # backfill

For each target date it pulls the day's metrics from the Google Health API,
upserts them into the health_data table, and sends a Telegram confirmation with
the numbers (or an alert on failure). Exits non-zero on error so cron surfaces it.

Sleep is a day behind everything else: a night belongs to the evening you went
to bed, and the run at 23:30 happens before that night. So the default (no-args)
nightly run also patches *yesterday's* sleep into yesterday's row, which is the
night that ended this morning. Runs for an explicit past date get that date's
sleep straight from `fetch_day` — by then the night is over.

With HEALTH_SYNC_PROVIDER=homeassistant in .env the same job reads Home
Assistant's health sensors over its MCP server instead (see
app/services/homeassistant_health.py). Home Assistant only knows current
values, so that source syncs today only: the day's metrics go on today's row
and the sleep sensor — last night — on yesterday's.

Suggested crontab (fetch each day at 23:55):
    55 23 * * *  cd /path/to/ai-diary && python -m app.jobs.health_sync >> sync.log 2>&1
"""

import argparse
import asyncio
import logging
import sys
from datetime import date, datetime, timedelta

from app.config import settings
from app.database import get_connection, init_db
from app.services.google_health import (
    GoogleHealthAuthError,
    GoogleHealthError,
    fetch_day,
    fetch_sleep_minutes,
)
from app.services.google_oauth import reauth_message
from app.services.homeassistant_health import (
    HomeAssistantAuthError,
    HomeAssistantError,
    fetch_today,
)
from app.services.health import (
    format_health_confirmation,
    format_sleep,
    save_health_data,
    update_sleep_minutes,
)
from app.services.notify import notify

logger = logging.getLogger("health_sync")


def configure_cli_logging() -> None:
    """Set up console logging for standalone runs.

    Done in main() rather than at import time so importing this module from the
    server (the Telegram bot reuses `sync_dates`) doesn't reconfigure the root
    logger out from under uvicorn.
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs every request at INFO — with paginated heart-rate that's dozens
    # of lines per run. Silence it; our own INFO/WARNING lines carry the info.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _parse_date(text: str) -> date:
    return datetime.strptime(text, "%Y-%m-%d").date()


def _target_dates(args) -> list[date]:
    if args.from_date and args.to_date:
        start, end = _parse_date(args.from_date), _parse_date(args.to_date)
        if start > end:
            start, end = end, start
        return [start + timedelta(days=i) for i in range((end - start).days + 1)]
    if args.date:
        return [_parse_date(args.date)]
    return [date.today()]


def missing_dates(days_back: int, end: date | None = None) -> list[date]:
    """Days in the trailing `days_back` window with no health_data row yet.

    Used after a re-authorisation to recover the nights the sync skipped while
    the refresh token was expired — Google keeps the history, so they're still
    fetchable. Today is excluded; it gets its own scheduled run.
    """
    end = (end or date.today()) - timedelta(days=1)
    start = end - timedelta(days=days_back - 1)
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT date FROM health_data WHERE date BETWEEN ? AND ?",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    have = {row["date"] for row in rows}
    span = (end - start).days + 1
    return [d for i in range(span) if (d := start + timedelta(days=i)).isoformat() not in have]


def backfill_missing_sleep(days_back: int, end: date | None = None) -> int:
    """Patch sleep onto rows in the trailing window that don't have it. Returns the count.

    A night is written by the *next* evening's run, so it is lost whenever that
    run doesn't happen — usually because the refresh token had expired.
    `missing_dates` can't recover those days: the row exists, only its sleep
    column is empty. Retrying is cheap (one small request per day, no paging),
    so days that simply have no sleep recorded are no great loss.
    """
    end = end or date.today()
    start = end - timedelta(days=days_back - 1)
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT date FROM health_data "
            "WHERE date BETWEEN ? AND ? AND sleep_minutes IS NULL ORDER BY date",
            (start.isoformat(), end.isoformat()),
        ).fetchall()

    patched = 0
    for row in rows:
        day = _parse_date(row["date"])
        try:
            minutes = fetch_sleep_minutes(day)
        except GoogleHealthAuthError:
            logger.warning("Stopping sleep backfill — token invalid.")
            break
        except GoogleHealthError as exc:
            logger.warning("Could not fetch sleep for %s: %s", day, exc)
            continue
        if minutes is not None:
            update_sleep_minutes(day, minutes)
            logger.info("Backfilled %d sleep minutes for %s", minutes, day)
            patched += 1
    return patched


def sync_previous_night_sleep(day: date) -> str | None:
    """Patch the sleep of the night that started on `day` - 1 into its row.

    Returns a Swedish line to append to the day's confirmation, or None when
    there's nothing to report. Never raises for ordinary API trouble — the
    day's own sync has already succeeded by this point and shouldn't be undone
    by a missing night.
    """
    night = day - timedelta(days=1)
    try:
        minutes = fetch_sleep_minutes(night)
    except GoogleHealthAuthError:
        raise
    except Exception as exc:  # noqa: BLE001 — a missing night isn't worth failing over
        logger.warning("Could not fetch sleep for %s: %s", night, exc)
        return None
    if minutes is None:
        logger.info("No sleep data for the night of %s", night)
        return None
    update_sleep_minutes(night, minutes)
    logger.info("Stored %d sleep minutes for %s", minutes, night)
    return (
        f"Sömn natten till {day.isoformat()}: {format_sleep(minutes)} "
        f"(sparad på {night.isoformat()})"
    )


async def sync_dates(dates: list[date], with_previous_night: bool = False) -> int:
    """Sync each date; notify per day. Returns a process exit code.

    `with_previous_night` additionally stores the preceding night's sleep on the
    previous day's row — for the nightly run, whose own night is still ahead of it.
    """
    exit_code = 0
    for day in dates:
        try:
            payload = fetch_day(day)
            action = save_health_data(payload)
            message = format_health_confirmation(payload, action)
            if with_previous_night and (line := sync_previous_night_sleep(day)):
                message += f"\n{line}"
            logger.info("Synced %s (%s)", day, action)
            await notify(message)
        except GoogleHealthAuthError as exc:
            logger.error("Auth error syncing %s: %s", day, exc)
            # Send the consent link with the alert so re-authorising is one tap
            # away — the token is expired far more often than anything else here.
            await notify(
                reauth_message(f"Synken för {day.isoformat()} misslyckades: {exc}")
            )
            # Auth won't recover across dates in the same run — stop early.
            return 2
        except GoogleHealthError as exc:
            logger.error("API error syncing %s: %s", day, exc)
            await notify(f"⚠️ <b>Hälsosynk misslyckades</b> ({day.isoformat()})\n{exc}")
            exit_code = 1
        except Exception as exc:  # noqa: BLE001 — surface anything else too
            logger.exception("Unexpected error syncing %s", day)
            await notify(f"⚠️ <b>Hälsosynk fel</b> ({day.isoformat()})\n{exc}")
            exit_code = 1
    return exit_code


async def sync_homeassistant() -> int:
    """Sync today's Home Assistant sensors; notify. Returns a process exit code."""
    today = date.today()
    try:
        reading = fetch_today()
        action = save_health_data(reading.payload)
        message = format_health_confirmation(reading.payload, action)
        if reading.sleep_minutes is not None:
            night = today - timedelta(days=1)
            update_sleep_minutes(night, reading.sleep_minutes)
            logger.info("Stored %d sleep minutes for %s", reading.sleep_minutes, night)
            message += (
                f"\nSömn natten till {today.isoformat()}: {format_sleep(reading.sleep_minutes)} "
                f"(sparad på {night.isoformat()})"
            )
        logger.info("Synced %s from Home Assistant (%s)", today, action)
        await notify(message)
        return 0
    except HomeAssistantAuthError as exc:
        logger.error("Auth error syncing from Home Assistant: %s", exc)
        await notify(f"🔑 <b>Hälsosynk (Home Assistant) nekad</b>\n{exc}")
        return 2
    except HomeAssistantError as exc:
        logger.error("Home Assistant sync failed: %s", exc)
        await notify(f"⚠️ <b>Hälsosynk misslyckades</b> ({today.isoformat()})\n{exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 — surface anything else too
        logger.exception("Unexpected error syncing from Home Assistant")
        await notify(f"⚠️ <b>Hälsosynk fel</b> ({today.isoformat()})\n{exc}")
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync health data (Google Health/Fitbit or Home Assistant) into health_data."
    )
    parser.add_argument("--date", help="Single date to sync (YYYY-MM-DD). Defaults to today.")
    parser.add_argument("--from", dest="from_date", help="Backfill start date (YYYY-MM-DD).")
    parser.add_argument("--to", dest="to_date", help="Backfill end date (YYYY-MM-DD).")
    args = parser.parse_args()

    configure_cli_logging()
    init_db()  # ensure schema/migrations are applied when run standalone
    dates = _target_dates(args)

    if settings.health_sync_provider == "homeassistant":
        if dates != [date.today()]:
            logger.error(
                "Home Assistant only exposes current sensor values — it can sync today, "
                "not %s. Drop --date/--from/--to.",
                ", ".join(d.isoformat() for d in dates),
            )
            return 1
        logger.info("Syncing %s from Home Assistant", date.today().isoformat())
        return asyncio.run(sync_homeassistant())

    # Only the plain nightly run has a night still to collect; an explicit date
    # is far enough in the past that fetch_day already carries its sleep.
    nightly = not (args.date or args.from_date or args.to_date)
    logger.info("Syncing %d day(s): %s", len(dates), ", ".join(d.isoformat() for d in dates))
    return asyncio.run(sync_dates(dates, with_previous_night=nightly))


if __name__ == "__main__":
    sys.exit(main())
