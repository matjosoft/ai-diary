"""Daily Google Health (Fitbit) sync — run by cron at end of day.

Usage:
    python -m app.jobs.health_sync                       # sync today
    python -m app.jobs.health_sync --date 2026-07-12     # sync one day
    python -m app.jobs.health_sync --from 2026-07-01 --to 2026-07-12   # backfill

For each target date it pulls the day's metrics from the Google Health API,
upserts them into the health_data table, and sends a Telegram confirmation with
the numbers (or an alert on failure). Exits non-zero on error so cron surfaces it.

Suggested crontab (fetch each day at 23:55):
    55 23 * * *  cd /path/to/ai-diary && python -m app.jobs.health_sync >> sync.log 2>&1
"""

import argparse
import asyncio
import logging
import sys
from datetime import date, datetime, timedelta

from app.database import init_db
from app.services.google_health import (
    GoogleHealthAuthError,
    GoogleHealthError,
    fetch_day,
)
from app.services.health import format_health_confirmation, save_health_data
from app.services.notify import notify

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# httpx logs every request at INFO — with paginated heart-rate that's dozens of
# lines per run. Silence it; our own INFO/WARNING lines carry the useful info.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("health_sync")


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


async def sync_dates(dates: list[date]) -> int:
    """Sync each date; notify per day. Returns a process exit code."""
    exit_code = 0
    for day in dates:
        try:
            payload = fetch_day(day)
            action = save_health_data(payload)
            message = format_health_confirmation(payload, action)
            logger.info("Synced %s (%s)", day, action)
            await notify(message)
        except GoogleHealthAuthError as exc:
            logger.error("Auth error syncing %s: %s", day, exc)
            await notify(
                f"⚠️ <b>Hälsosynk misslyckades</b> ({day.isoformat()})\n"
                f"Autentisering: {exc}"
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync Google Health (Fitbit) data into health_data.")
    parser.add_argument("--date", help="Single date to sync (YYYY-MM-DD). Defaults to today.")
    parser.add_argument("--from", dest="from_date", help="Backfill start date (YYYY-MM-DD).")
    parser.add_argument("--to", dest="to_date", help="Backfill end date (YYYY-MM-DD).")
    args = parser.parse_args()

    init_db()  # ensure schema/migrations are applied when run standalone
    dates = _target_dates(args)
    logger.info("Syncing %d day(s): %s", len(dates), ", ".join(d.isoformat() for d in dates))
    return asyncio.run(sync_dates(dates))


if __name__ == "__main__":
    sys.exit(main())
