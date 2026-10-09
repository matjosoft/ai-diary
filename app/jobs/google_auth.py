"""Mint a new Google Health (Fitbit) refresh token without the OAuth Playground.

Testing-mode refresh tokens die after ~7 days. This job automates the round-trip:
it builds the consent URL, collects the authorisation code, exchanges it, writes
the result into `.env`, verifies it, and backfills the days the sync missed while
the token was dead.

Usage:
    python -m app.jobs.google_auth                  # full flow (loopback or paste)
    python -m app.jobs.google_auth --check          # is the current token still alive?
    python -m app.jobs.google_auth --print-url      # just print the consent URL
    python -m app.jobs.google_auth --code "<url>"   # finish with a pasted redirect URL
    python -m app.jobs.google_auth --no-backfill    # skip the catch-up sync

Run `--check` from cron each morning so you get a Telegram nudge (with a ready
consent link) while there's still time, instead of finding out at 23:30:

    0 8 * * *  cd /path/to/ai-diary && .venv/bin/python -m app.jobs.google_auth --check >> sync.log 2>&1
"""

import argparse
import asyncio
import logging
import os
import sys
import urllib.parse
import webbrowser
from datetime import date, timedelta

from app.database import init_db
from app.jobs.health_sync import (
    backfill_missing_sleep,
    configure_cli_logging,
    missing_dates,
    sync_dates,
)
from app.services.google_health import GoogleHealthAuthError
from app.services.google_oauth import (
    TESTING_TOKEN_LIFETIME_DAYS,
    build_auth_url,
    capture_code,
    exchange_code,
    extract_code,
    reauth_message,
    save_refresh_token,
    scopes,
    token_status,
)
from app.services.notify import notify
from app.config import settings

logger = logging.getLogger("google_auth")

# How far back to look for days the sync missed while the token was expired.
BACKFILL_WINDOW_DAYS = 10


def _loopback_port(redirect: str) -> int | None:
    """Port to listen on if `redirect` is a loopback URI, else None."""
    parsed = urllib.parse.urlparse(redirect)
    if parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1"):
        return parsed.port or 80
    return None


def _maybe_open_browser(url: str) -> bool:
    """Open the consent URL locally when there's plausibly a browser to open it."""
    if not (os.environ.get("DISPLAY") or sys.platform in ("darwin", "win32")):
        return False
    try:
        return webbrowser.open(url)
    except Exception:  # noqa: BLE001 — a failed browser launch is never fatal
        return False


def _print_url(url: str, redirect: str) -> None:
    print("\nOpen this URL and approve access with your Google account:\n")
    print(url)
    port = _loopback_port(redirect)
    if port:
        print(
            f"\n(If you're opening it on another machine, tunnel the callback first:\n"
            f"   ssh -L {port}:localhost:{port} {os.environ.get('USER', 'pi')}@<this-host>\n"
            f" — or just copy the address bar afterwards and pass it to --code.)"
        )
    print()


async def _run_backfill(enabled: bool) -> None:
    """Sync days that have no health row yet — the ones lost to an expired token."""
    if not enabled:
        return
    dates = missing_dates(BACKFILL_WINDOW_DAYS)
    if dates:
        logger.info(
            "Backfilling %d missing day(s): %s", len(dates), ", ".join(d.isoformat() for d in dates)
        )
        await sync_dates(dates)
    else:
        logger.info("No missing health days in the last %d days.", BACKFILL_WINDOW_DAYS)
    # Separate pass: a night is stored a day late, so an outage also leaves rows
    # that exist but never got their sleep — invisible to missing_dates.
    patched = backfill_missing_sleep(BACKFILL_WINDOW_DAYS)
    if patched:
        logger.info("Filled in sleep for %d earlier day(s).", patched)


async def _finish(code: str, redirect: str, backfill: bool, quiet: bool = False) -> int:
    """Exchange, persist, verify, announce, backfill."""
    save_refresh_token(exchange_code(code, redirect=redirect))

    status = token_status()
    if not status.ok:
        logger.error("Saved the token but it failed verification: %s", status.detail)
        if not quiet:
            await notify(
                f"⚠️ <b>Hälsosynk</b>\nNy token sparades men gick inte att verifiera:\n{status.detail}"
            )
        return 1

    expires = date.today() + timedelta(days=TESTING_TOKEN_LIFETIME_DAYS)
    logger.info("New refresh token verified. Expected to last until ~%s.", expires)
    print(f"Saved and verified. Expected to last until ~{expires.isoformat()}.")
    if not quiet:
        await notify(
            "✅ <b>Hälsosynk: Google-token förnyad</b>\n"
            f"Giltig igen — förväntas hålla till omkring {expires.isoformat()}."
        )
    await _run_backfill(backfill)
    return 0


async def run_check(quiet: bool) -> int:
    """Report whether the stored token still works; nudge via Telegram if not."""
    if settings.health_sync_provider != "google":
        logger.info("HEALTH_SYNC_PROVIDER is not google — skipping token check.")
        print("Skipped: HEALTH_SYNC_PROVIDER is not google.")
        return 0

    status = token_status()
    age = f"{status.age_days}d old" if status.age_days is not None else "age unknown"
    logger.info("Token status: %s (%s)", status.detail, age)

    if not status.ok:
        print(f"INVALID: {status.detail}")
        if not quiet:
            await notify(reauth_message(status.detail))
        return 2

    if status.expiring_soon:
        left = status.days_left if status.days_left is not None else 0
        print(f"VALID but expiring soon ({left}d left).")
        if not quiet:
            await notify(
                reauth_message(
                    f"Nuvarande token är {status.age_days} dagar gammal och slutar gälla om ca {left} dag(ar)."
                )
            )
        return 1

    print(f"VALID ({age}).")
    return 0


async def run_flow(args) -> int:
    """Interactive/loopback re-authorisation."""
    redirect = settings.GOOGLE_HEALTH_REDIRECT_URI.strip()
    port = _loopback_port(redirect)
    if args.port:
        port = args.port
        redirect = f"http://localhost:{port}/"

    if not scopes():
        logger.error("GOOGLE_HEALTH_SCOPES is empty — nothing to request.")
        return 2

    url, state = build_auth_url(redirect=redirect)

    if args.print_url:
        _print_url(url, redirect)
        print("Then finish with:\n  python -m app.jobs.google_auth --code '<pasted redirect URL>'\n")
        return 0

    if args.code:
        code = extract_code(args.code)
        if not code:
            logger.error("Could not find an authorisation code in --code input.")
            return 2
        return await _finish(code, redirect, args.backfill, args.quiet)

    _print_url(url, redirect)
    opened = not args.no_browser and _maybe_open_browser(url)
    if opened:
        print("Opened it in your browser.")

    if port and not args.paste:
        print(f"Waiting for the redirect on http://localhost:{port}/ ... (Ctrl-C to paste instead)")
        try:
            code = capture_code(port, state, timeout=args.timeout)
        except KeyboardInterrupt:
            code = None
        except GoogleHealthAuthError as exc:
            logger.warning("Loopback capture failed: %s", exc)
            code = None
        if code:
            return await _finish(code, redirect, args.backfill, args.quiet)
        print("\nFalling back to pasting the code.")

    pasted = input("Paste the full redirect URL (or just the code): ").strip()
    code = extract_code(pasted)
    if not code:
        logger.error("That didn't contain an authorisation code.")
        return 2
    return await _finish(code, redirect, args.backfill, args.quiet)


async def _main_async(args) -> int:
    if args.check:
        return await run_check(args.quiet)
    init_db()  # backfill writes health rows; make sure the schema exists
    try:
        return await run_flow(args)
    except GoogleHealthAuthError as exc:
        logger.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        print("\nAborted.")
        return 130


def main() -> int:
    parser = argparse.ArgumentParser(description="Re-authorise the Google Health (Fitbit) sync.")
    parser.add_argument("--check", action="store_true", help="Only report whether the stored token still works.")
    parser.add_argument("--print-url", action="store_true", help="Print the consent URL and exit.")
    parser.add_argument("--code", help="Finish the flow with a pasted redirect URL or code.")
    parser.add_argument("--paste", action="store_true", help="Skip the loopback listener; paste the code instead.")
    parser.add_argument("--port", type=int, help="Loopback port to catch the redirect on (overrides the redirect URI).")
    parser.add_argument("--timeout", type=float, default=300.0, help="Seconds to wait for the redirect (default 300).")
    parser.add_argument("--no-browser", action="store_true", help="Never try to open a browser.")
    parser.add_argument(
        "--no-backfill",
        dest="backfill",
        action="store_false",
        help="Don't sync health days missed while the token was expired.",
    )
    parser.add_argument("--quiet", action="store_true", help="Don't send Telegram notifications.")
    args = parser.parse_args()

    configure_cli_logging()
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    sys.exit(main())
