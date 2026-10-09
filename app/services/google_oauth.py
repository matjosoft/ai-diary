"""Google OAuth re-authorisation for the Health (Fitbit) sync.

While the OAuth app stays in *Testing* mode Google expires the refresh token after
~7 days, so minting a new one is a recurring chore. This module turns the manual
OAuth 2.0 Playground round-trip into: open a link, approve, done.

Two ways to complete the flow, both ending in `save_refresh_token()`:

* **Loopback** (`capture_code`) — a throwaway HTTP server on localhost catches
  Google's redirect, so nothing needs pasting. Requires the redirect URI to be a
  `http://localhost:<port>/` registered on the OAuth client.
* **Paste** (`extract_code`) — the browser lands on a page that can't load (or on
  the Playground), and the `code=` in the address bar is pasted back into the CLI
  or the Telegram bot. Works from any device, including off the Pi's network.

The new refresh token is written straight into `.env` and applied to the running
process, so the next sync uses it without a restart.
"""

import logging
import re
import secrets
import threading
import time
import urllib.parse
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx

from app.config import settings
from app.services.google_health import GoogleHealthAuthError, reset_token_cache

logger = logging.getLogger(__name__)

# .env lives next to the app package (repo root).
ENV_PATH = Path(__file__).resolve().parents[2] / ".env"

REFRESH_TOKEN_KEY = "GOOGLE_HEALTH_REFRESH_TOKEN"
ISSUED_KEY = "GOOGLE_HEALTH_REFRESH_TOKEN_ISSUED"

# Google expires testing-mode refresh tokens after this long. Used to warn a day
# ahead rather than discovering it when the nightly sync fails.
TESTING_TOKEN_LIFETIME_DAYS = 7
WARN_AT_AGE_DAYS = 6

# How long a started flow stays completable (Google auth codes are short-lived).
FLOW_TTL_SECONDS = 15 * 60


def scopes() -> list[str]:
    """Requested scopes, from settings (comma- or space-separated)."""
    return [s for s in re.split(r"[,\s]+", settings.GOOGLE_HEALTH_SCOPES) if s]


def redirect_uri() -> str:
    return settings.GOOGLE_HEALTH_REDIRECT_URI.strip()


def _require_client_credentials() -> tuple[str, str]:
    client_id = settings.GOOGLE_HEALTH_CLIENT_ID.strip()
    client_secret = settings.GOOGLE_HEALTH_CLIENT_SECRET.strip()
    if not (client_id and client_secret):
        raise GoogleHealthAuthError(
            "Set GOOGLE_HEALTH_CLIENT_ID and GOOGLE_HEALTH_CLIENT_SECRET in .env first."
        )
    return client_id, client_secret


# --- Step 1: the consent URL ----------------------------------------------


def build_auth_url(state: str | None = None, redirect: str | None = None) -> tuple[str, str]:
    """Build the Google consent URL. Returns (url, state).

    `prompt=consent` + `access_type=offline` are what make Google hand back a
    *refresh* token; without them a re-authorisation of an already-approved app
    returns only an access token.
    """
    client_id, _ = _require_client_credentials()
    state = state or secrets.token_urlsafe(16)
    params = {
        "client_id": client_id,
        "redirect_uri": redirect or redirect_uri(),
        "response_type": "code",
        "scope": " ".join(scopes()),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{settings.GOOGLE_HEALTH_AUTH_URI}?{urllib.parse.urlencode(params)}", state


# --- Step 2a: catch the redirect on localhost ------------------------------


class _CallbackHandler(BaseHTTPRequestHandler):
    """Single-shot handler that stashes the query params on the server object."""

    def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler's naming
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        self.server.oauth_result = {k: v[0] for k, v in query.items()}  # type: ignore[attr-defined]
        ok = "code" in query
        body = (
            "<h2>Klart!</h2><p>Du kan stänga den här fliken och gå tillbaka till terminalen.</p>"
            if ok
            else f"<h2>Något gick fel</h2><pre>{self.server.oauth_result}</pre>"  # type: ignore[attr-defined]
        )
        payload = f"<html><body style='font-family:sans-serif'>{body}</body></html>".encode()
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # silence per-request stderr logging
        pass


def capture_code(port: int, state: str, timeout: float = 300.0) -> str:
    """Serve on localhost:port until Google redirects back, and return the code.

    Raises GoogleHealthAuthError on timeout, a state mismatch (CSRF guard), or an
    error response from Google.
    """
    server = HTTPServer(("127.0.0.1", port), _CallbackHandler)
    server.oauth_result = None  # type: ignore[attr-defined]
    server.timeout = 1.0
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = server.oauth_result  # type: ignore[attr-defined]
            if result:
                if result.get("error"):
                    raise GoogleHealthAuthError(f"Google returned an error: {result['error']}")
                if result.get("state") != state:
                    raise GoogleHealthAuthError("State mismatch on the OAuth callback — aborting.")
                code = result.get("code")
                if not code:
                    raise GoogleHealthAuthError(f"No code in callback: {result}")
                return code
            time.sleep(0.2)
        raise GoogleHealthAuthError(f"Timed out after {timeout:.0f}s waiting for the Google redirect.")
    finally:
        server.shutdown()
        server.server_close()


# --- Step 2b: or accept a pasted code / redirect URL -----------------------

_CODE_RE = re.compile(r"[?&#]code=([^&\s]+)")


def extract_code(text: str) -> str | None:
    """Pull an auth code out of a pasted redirect URL, a `code=...` fragment, or
    a bare code. Returns None if the text doesn't look like one."""
    text = (text or "").strip()
    if not text:
        return None
    match = _CODE_RE.search(text)
    if match:
        return urllib.parse.unquote(match.group(1))
    if text.lower().startswith("code="):
        return urllib.parse.unquote(text[5:].strip())
    # Bare code: Google's are "4/" + opaque url-safe chars, no whitespace.
    if text.startswith("4/") and not re.search(r"\s", text):
        return text
    return None


# --- Step 3: exchange the code for a refresh token -------------------------


def exchange_code(code: str, redirect: str | None = None) -> str:
    """Trade an authorisation code for a refresh token."""
    client_id, client_secret = _require_client_credentials()
    resp = httpx.post(
        settings.GOOGLE_HEALTH_TOKEN_URI,
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect or redirect_uri(),
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    if resp.status_code >= 400:
        detail = resp.text[:500]
        if "invalid_grant" in detail:
            raise GoogleHealthAuthError(
                "Google rejected the code (invalid_grant) — it expires within minutes "
                "and can only be used once. Start the flow again."
            )
        if "redirect_uri_mismatch" in detail:
            raise GoogleHealthAuthError(
                f"redirect_uri_mismatch — add {redirect or redirect_uri()} as an authorised "
                "redirect URI on the OAuth client in Google Cloud Console."
            )
        raise GoogleHealthAuthError(f"Token exchange failed ({resp.status_code}): {detail}")

    refresh_token = resp.json().get("refresh_token")
    if not refresh_token:
        raise GoogleHealthAuthError(
            "No refresh_token in Google's response. This happens when consent was "
            "skipped for an already-approved app — retry with a fresh consent link."
        )
    return refresh_token


# --- Step 4: persist it ----------------------------------------------------


def _set_env_line(lines: list[str], key: str, value: str) -> list[str]:
    """Replace the assignment for `key`, or append it if the file has none."""
    pattern = re.compile(rf"^\s*(export\s+)?{re.escape(key)}\s*=")
    out, replaced = [], False
    for line in lines:
        if pattern.match(line) and not replaced:
            out.append(f"{key}={value}")
            replaced = True
        elif pattern.match(line):
            continue  # drop duplicate assignments; the last value would have won
        else:
            out.append(line)
    if not replaced:
        out.append(f"{key}={value}")
    return out


def save_refresh_token(token: str, env_path: Path | None = None) -> Path:
    """Write the token into .env, stamp the issue date, and apply it in-process.

    The file is rewritten atomically so a crash mid-write can't leave a .env that
    the app (and every other credential in it) can no longer be read from.
    """
    path = env_path or ENV_PATH
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    lines = _set_env_line(lines, REFRESH_TOKEN_KEY, token)
    lines = _set_env_line(lines, ISSUED_KEY, date.today().isoformat())

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if path.exists():
        tmp.chmod(path.stat().st_mode & 0o777)
    tmp.replace(path)

    # Apply to the running process so the next call doesn't need a restart.
    settings.GOOGLE_HEALTH_REFRESH_TOKEN = token
    settings.GOOGLE_HEALTH_REFRESH_TOKEN_ISSUED = date.today().isoformat()
    reset_token_cache()
    logger.info("Wrote a new Google Health refresh token to %s", path)
    return path


# --- Status ----------------------------------------------------------------


@dataclass
class TokenStatus:
    ok: bool
    detail: str
    issued: date | None = None
    age_days: int | None = None
    days_left: int | None = None

    @property
    def expiring_soon(self) -> bool:
        return self.ok and self.age_days is not None and self.age_days >= WARN_AT_AGE_DAYS


def _issued_date() -> date | None:
    raw = settings.GOOGLE_HEALTH_REFRESH_TOKEN_ISSUED.strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def token_status() -> TokenStatus:
    """Check whether the stored refresh token still works.

    Does a real refresh-grant round-trip — the only way to know, and cheap.
    """
    issued = _issued_date()
    age = (date.today() - issued).days if issued else None
    days_left = max(TESTING_TOKEN_LIFETIME_DAYS - age, 0) if age is not None else None

    if not settings.GOOGLE_HEALTH_REFRESH_TOKEN.strip():
        return TokenStatus(False, "No refresh token configured.", issued, age, days_left)

    try:
        client_id, client_secret = _require_client_credentials()
    except GoogleHealthAuthError as exc:
        return TokenStatus(False, str(exc), issued, age, days_left)

    resp = httpx.post(
        settings.GOOGLE_HEALTH_TOKEN_URI,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": settings.GOOGLE_HEALTH_REFRESH_TOKEN.strip(),
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    if resp.status_code < 400:
        return TokenStatus(True, "Refresh token is valid.", issued, age, days_left)
    if "invalid_grant" in resp.text:
        return TokenStatus(False, "Refresh token expired or revoked (invalid_grant).", issued, age, days_left)
    return TokenStatus(False, f"Token endpoint returned {resp.status_code}: {resp.text[:200]}", issued, age, days_left)


# --- Telegram-facing copy ---------------------------------------------------


def reauth_message(reason: str, url: str | None = None) -> str:
    """Swedish Telegram alert with a ready-to-click consent link and paste-back steps.

    Shared by the nightly sync (on invalid_grant), the daily token check, and the
    /healthauth command so the instructions can't drift apart.
    """
    if url is None:
        try:
            url, _ = build_auth_url()
        except GoogleHealthAuthError as exc:
            return f"⚠️ <b>Hälsosynk: OAuth behöver förnyas</b>\n{reason}\n{exc}"
    return (
        f"🔑 <b>Hälsosynk: Google-token behöver förnyas</b>\n{reason}\n\n"
        f'1. Öppna <a href="{url}">den här länken</a> och godkänn åtkomsten.\n'
        "2. Kopiera hela adressen från webbläsarens adressfält efteråt "
        "(sidan kan visa ett fel — det gör inget).\n"
        "3. Klistra in adressen här i chatten, så förnyar jag token automatiskt.\n\n"
        "Skriv /healthauth om du vill starta om flödet."
    )


# --- Pending flow, for the chat-driven (Telegram) variant -------------------


@dataclass
class PendingFlow:
    state: str
    url: str
    redirect: str
    started: float

    @property
    def expired(self) -> bool:
        return time.time() - self.started > FLOW_TTL_SECONDS


_pending: PendingFlow | None = None


def start_pending_flow() -> PendingFlow:
    """Begin a paste-back flow (used by the Telegram bot) and return it."""
    global _pending
    redirect = redirect_uri()
    url, state = build_auth_url(redirect=redirect)
    _pending = PendingFlow(state=state, url=url, redirect=redirect, started=time.time())
    return _pending


def pending_flow() -> PendingFlow | None:
    """The active paste-back flow, or None if there isn't one or it timed out."""
    global _pending
    if _pending and _pending.expired:
        _pending = None
    return _pending


def complete_from_text(text: str) -> bool:
    """Finish a re-authorisation from a pasted redirect URL or code.

    Returns False when the text holds no code, so a chat handler can fall through
    to normal handling. Works with no pending flow too: the nightly sync's alert
    link is minted in the cron process, so the bot never saw that flow start —
    it falls back to the configured redirect URI.

    Raises GoogleHealthAuthError if a code was found but couldn't be exchanged.
    """
    global _pending
    code = extract_code(text)
    if not code:
        return False
    flow = pending_flow()
    try:
        save_refresh_token(exchange_code(code, redirect=flow.redirect if flow else redirect_uri()))
    finally:
        _pending = None
    return True
