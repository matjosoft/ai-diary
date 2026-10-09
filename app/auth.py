"""Shared-secret API token check.

The diary holds everything you ever said into it, so the data endpoints are
gated on a single secret token (`API_TOKEN` in `.env`). An empty token leaves
auth **off** — that is the default, so an existing deployment keeps working
until the token is actually configured.

The token may arrive three ways, because not every client can set a header:

    X-API-Key: <token>              iPhone Shortcut, Open WebUI pipe, curl
    Authorization: Bearer <token>   generic HTTP clients
    ?token=<token>                  links the *browser* opens — the podcast
                                    MP3 and the photo URLs handed to chat
                                    clients, where no header can be attached

`token_query()` builds that last form for the URLs we hand out ourselves.
"""

import secrets
from urllib.parse import quote

from fastapi import Header, HTTPException, Query

from app.config import settings


def _matches(candidate: str, expected: str) -> bool:
    """Constant-time compare, so a wrong token leaks no timing signal."""
    return secrets.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))


async def require_token(
    x_api_key: str = Header(default="", alias="X-API-Key"),
    authorization: str = Header(default=""),
    token: str = Query(default="", description="API token, for browser-opened links"),
) -> None:
    expected = settings.API_TOKEN.strip()
    if not expected:
        return

    candidates = [x_api_key.strip(), token.strip()]
    if authorization.lower().startswith("bearer "):
        candidates.append(authorization[len("bearer ") :].strip())

    if any(c and _matches(c, expected) for c in candidates):
        return

    raise HTTPException(status_code=401, detail="Missing or invalid API token")


def token_query() -> str:
    """`?token=...` for URLs we emit, or "" when auth is disabled."""
    expected = settings.API_TOKEN.strip()
    return f"?token={quote(expected, safe='')}" if expected else ""
