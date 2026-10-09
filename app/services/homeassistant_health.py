"""Home Assistant health client — reads today's health sensors over MCP.

Home Assistant's "Model Context Protocol Server" integration serves a
Streamable HTTP MCP endpoint at `/api/mcp`, authenticated with a long-lived
access token (Profile → Security → Long-lived access tokens). Its tools are the
Assist LLM API; the one that reads sensor values is `GetLiveContext`, which
returns the current state of every entity *exposed to Assist* (Settings →
Voice assistants → Expose). A sensor that isn't exposed simply isn't there.

Two quirks shape this module:

* `GetLiveContext` lists entities by friendly name, not entity_id. We match
  a configured `sensor.mattias_steg` by slugifying each listed name the way
  Home Assistant derives entity ids ("Mattias Steg" → `mattias_steg`,
  "Mattias Avstånd" → `mattias_avstand`). A plain friendly name also works as
  the configured value.
* It only knows *current* values — there is no history, so this source can
  only sync today (no backfill of missed days).

The MCP exchange is small enough to speak directly with httpx: `initialize`,
`notifications/initialized`, then `tools/call`. Responses may come back as
plain JSON or as a single-event SSE stream; both are handled.
"""

import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import date

import httpx
import yaml

from app.config import settings
from app.models import HealthDataRequest

logger = logging.getLogger(__name__)

_PROTOCOL_VERSION = "2025-03-26"
# Newer Home Assistant versions prefix tool names with their domain
# ("homeassistant__GetLiveContext"); older ones use the bare name.
_LIVE_CONTEXT_TOOL = "GetLiveContext"
_TIMEOUT = 30.0


class HomeAssistantError(Exception):
    """Base error for Home Assistant health sync failures."""


class HomeAssistantAuthError(HomeAssistantError):
    """The access token was rejected (missing, revoked, or wrong)."""


# --- MCP transport ----------------------------------------------------------


class _McpClient:
    """Minimal Streamable HTTP MCP client — just enough to call one tool."""

    def __init__(self, client: httpx.Client, url: str, token: str):
        self._client = client
        self._url = url
        self._token = token
        self._session_id: str | None = None
        self._next_id = 1

    def _headers(self) -> dict:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": _PROTOCOL_VERSION,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _post(self, payload: dict) -> httpx.Response:
        try:
            resp = self._client.post(self._url, json=payload, headers=self._headers())
        except httpx.HTTPError as exc:
            raise HomeAssistantError(f"Kunde inte nå Home Assistant ({self._url}): {exc}") from exc
        if resp.status_code in (401, 403):
            raise HomeAssistantAuthError(
                f"Home Assistant nekade åtkomst (HTTP {resp.status_code}) — "
                "kontrollera HOMEASSISTANT_ACCESS_TOKEN."
            )
        if resp.status_code == 404:
            raise HomeAssistantError(
                f"Ingen MCP-server på {self._url} — är integrationen "
                "'Model Context Protocol Server' installerad?"
            )
        if resp.status_code >= 400:
            raise HomeAssistantError(f"MCP HTTP {resp.status_code}: {resp.text[:300]}")
        if session := resp.headers.get("mcp-session-id"):
            self._session_id = session
        return resp

    def _request(self, method: str, params: dict) -> dict:
        request_id = self._next_id
        self._next_id += 1
        resp = self._post({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        message = _parse_rpc_response(resp, request_id)
        if "error" in message:
            err = message["error"]
            raise HomeAssistantError(f"MCP {method} misslyckades: {err.get('message', err)}")
        return message.get("result") or {}

    def _notify(self, method: str) -> None:
        self._post({"jsonrpc": "2.0", "method": method})

    def initialize(self) -> None:
        self._request(
            "initialize",
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "ai-diary", "version": "1.0"},
            },
        )
        self._notify("notifications/initialized")

    def tool_names(self) -> list[str]:
        return [tool.get("name", "") for tool in self._request("tools/list", {}).get("tools", [])]

    def call_tool(self, name: str, arguments: dict | None = None) -> str:
        """Call a tool and return its text content joined together."""
        result = self._request("tools/call", {"name": name, "arguments": arguments or {}})
        text = "\n".join(
            part.get("text", "")
            for part in result.get("content", [])
            if part.get("type") == "text"
        )
        if result.get("isError"):
            raise HomeAssistantError(f"MCP-verktyget {name} gav fel: {text[:300]}")
        return text

    def close(self) -> None:
        # Stateful servers hand out a session; release it. Best effort only.
        if self._session_id:
            try:
                self._client.delete(self._url, headers=self._headers())
            except httpx.HTTPError:
                pass


def _parse_rpc_response(resp: httpx.Response, request_id: int) -> dict:
    """The JSON-RPC reply matching `request_id`, from a JSON or SSE response body."""
    content_type = resp.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        messages = []
        for block in resp.text.split("\n\n"):
            data = "\n".join(
                line[5:].lstrip() for line in block.splitlines() if line.startswith("data:")
            )
            if data:
                try:
                    messages.append(json.loads(data))
                except ValueError:
                    continue
    else:
        try:
            body = resp.json()
        except ValueError as exc:
            raise HomeAssistantError(f"Oväntat svar från MCP-servern: {resp.text[:300]}") from exc
        messages = body if isinstance(body, list) else [body]

    for message in messages:
        if isinstance(message, dict) and message.get("id") == request_id:
            return message
    raise HomeAssistantError("MCP-servern svarade utan resultat för anropet.")


# --- Live context parsing ---------------------------------------------------


def _slugify(text: str) -> str:
    """Home Assistant-style slug: "Mattias Avstånd" → "mattias_avstand"."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", ascii_text.lower()).strip("_")


def parse_live_context(text: str) -> list[dict]:
    """Entities from a GetLiveContext reply, as dicts with names/domain/state/attributes.

    The tool's text is a JSON envelope `{"success": true, "result": "<text>"}`
    whose result is a header line followed by a YAML list of entities.
    """
    try:
        envelope = json.loads(text)
    except ValueError:
        envelope = text
    if isinstance(envelope, dict):
        if envelope.get("success") is False:
            raise HomeAssistantError(f"GetLiveContext misslyckades: {envelope.get('error', envelope)}")
        envelope = envelope.get("result", "")
    body = str(envelope)

    # Skip the prose header — the YAML list starts at the first "- " line.
    match = re.search(r"^- ", body, flags=re.MULTILINE)
    if not match:
        return []
    try:
        entities = yaml.safe_load(body[match.start():])
    except yaml.YAMLError as exc:
        raise HomeAssistantError(f"Kunde inte tolka GetLiveContext-svaret: {exc}") from exc
    return [e for e in entities or [] if isinstance(e, dict)]


def _entity_names(entity: dict) -> list[str]:
    names = entity.get("names") or entity.get("name") or ""
    if isinstance(names, list):
        return [str(n) for n in names]
    return [n.strip() for n in str(names).split(",") if n.strip()]


def find_entity(entities: list[dict], wanted: str) -> dict | None:
    """The entity matching a configured entity_id (or friendly name), if listed."""
    wanted = wanted.strip()
    domain, _, object_id = wanted.partition(".")
    if not object_id:  # a friendly name rather than an entity_id
        domain, object_id = "", _slugify(wanted)
    for entity in entities:
        if domain and entity.get("domain") not in (None, domain):
            continue
        if any(_slugify(name) == object_id for name in _entity_names(entity)):
            return entity
    return None


def _number(entity: dict | None) -> float | None:
    if entity is None:
        return None
    try:
        return float(str(entity.get("state")).replace(",", "."))
    except (TypeError, ValueError):
        return None  # "unknown", "unavailable", ...


def _unit(entity: dict | None) -> str:
    attributes = (entity or {}).get("attributes") or {}
    return str(attributes.get("unit_of_measurement") or "").strip().lower()


def _distance_km(entity: dict | None) -> float | None:
    value = _number(entity)
    if value is None:
        return None
    unit = _unit(entity)
    if unit == "m":
        return value / 1000
    if unit == "mi":
        return value * 1.609344
    return value


def _sleep_minutes(entity: dict | None) -> int | None:
    """Sleep in minutes. The sensor reports hours (8.1 → 486) unless its unit says otherwise."""
    value = _number(entity)
    if value is None:
        return None
    unit = _unit(entity)
    if unit in ("min", "mins", "minutes"):
        minutes = value
    elif unit in ("s", "sec", "seconds"):
        minutes = value / 60
    else:
        minutes = value * 60
    return round(minutes)


# --- Public API -------------------------------------------------------------


@dataclass
class HomeAssistantReading:
    """Today's health values, plus the most recent night's sleep.

    Sleep is kept apart because it belongs to a different row: the sensor
    reports the night that ended this morning, which the diary stores on the
    day that night *started* (yesterday).
    """

    payload: HealthDataRequest
    sleep_minutes: int | None


def _sensor_config() -> dict[str, str]:
    return {
        "steps": settings.HOMEASSISTANT_SENSOR_STEPS,
        "distance_km": settings.HOMEASSISTANT_SENSOR_DISTANCE,
        "resting_heart_rate": settings.HOMEASSISTANT_SENSOR_RESTING_HR,
        "sleep_minutes": settings.HOMEASSISTANT_SENSOR_SLEEP,
    }


def _resolve_tool(available: list[str], wanted: str) -> str:
    """The server's name for `wanted`, with or without a "<domain>__" prefix."""
    for name in available:
        if name == wanted or name.endswith(f"__{wanted}"):
            return name
    raise HomeAssistantError(
        f"MCP-servern har inget {wanted}-verktyg (har: {', '.join(available) or 'inga'}) — "
        "välj Assist som LLM-API i integrationen 'Model Context Protocol Server'."
    )


def fetch_live_entities() -> list[dict]:
    """All entities Home Assistant exposes to Assist, with their current state."""
    url = settings.HOMEASSISTANT_MCP_URL.strip()
    token = settings.HOMEASSISTANT_ACCESS_TOKEN.strip()
    if not url:
        raise HomeAssistantError("HOMEASSISTANT_MCP_URL saknas i .env.")
    if not token:
        raise HomeAssistantAuthError("HOMEASSISTANT_ACCESS_TOKEN saknas i .env.")

    with httpx.Client(timeout=_TIMEOUT) as client:
        mcp = _McpClient(client, url, token)
        try:
            mcp.initialize()
            tool = _resolve_tool(mcp.tool_names(), _LIVE_CONTEXT_TOOL)
            # The domain filter is only understood by newer versions (the
            # prefixed ones); older servers get no arguments and list everything.
            arguments = {"domain": "sensor"} if tool != _LIVE_CONTEXT_TOOL else {}
            text = mcp.call_tool(tool, arguments)
        finally:
            mcp.close()

    entities = parse_live_context(text)
    if not entities:
        raise HomeAssistantError(
            "GetLiveContext returnerade inga entiteter — exponera sensorerna för Assist "
            "(Inställningar → Röstassistenter → Exponera)."
        )
    return entities


def fetch_today() -> HomeAssistantReading:
    """Read the configured health sensors and build today's payload."""
    entities = fetch_live_entities()

    found: dict[str, dict | None] = {}
    raw: dict[str, dict] = {}
    for field, sensor in _sensor_config().items():
        if not sensor.strip():
            found[field] = None
            continue
        entity = find_entity(entities, sensor)
        found[field] = entity
        if entity is None:
            logger.warning(
                "Sensor %s (%s) not found in Home Assistant's live context — is it "
                "exposed to Assist? Listed: %s",
                sensor,
                field,
                ", ".join(n for e in entities for n in _entity_names(e)),
            )
            continue
        raw[sensor] = {"state": entity.get("state"), "unit": _unit(entity) or None}

    if all(entity is None for entity in found.values()):
        raise HomeAssistantError(
            "Ingen av hälsosensorerna hittades i Home Assistant — exponera dem för Assist "
            "(Inställningar → Röstassistenter → Exponera)."
        )

    steps = _number(found["steps"])
    resting_hr = _number(found["resting_heart_rate"])
    payload = HealthDataRequest(
        date=date.today(),
        steps=round(steps) if steps is not None else None,
        distance_km=_distance_km(found["distance_km"]),
        resting_heart_rate=round(resting_hr) if resting_hr is not None else None,
        source=settings.HOMEASSISTANT_SOURCE,
        raw_data={"homeassistant": raw},
    )
    return HomeAssistantReading(payload=payload, sleep_minutes=_sleep_minutes(found["sleep_minutes"]))
