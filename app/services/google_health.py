"""Google Health API client — pulls a day's Fitbit metrics for the daily sync.

The Google Health API (https://health.googleapis.com, v4) is Google's official
successor to the Fitbit Web API. It uses Google OAuth 2.0; for this personal Pi
project we stay in OAuth *testing* mode against the owner's own account and hold a
refresh token obtained once from the OAuth 2.0 Playground.

Read pattern (users.dataTypes.dataPoints.list):
    GET {base}/v4/users/me/dataTypes/{dataType}/dataPoints
        ?filter={dataType}.interval.start_time >= "T0" AND ... < "T1"
        &pageSize=...&pageToken=...
Response: {"dataPoints": [ {DataPoint}, ... ], "nextPageToken": "..."}

DataPoint value fields are *type-specific* and returned as camelCase JSON, e.g.
steps -> "count", distance -> "millimeters", heart rate -> "beatsPerMinute".
Interval types (steps/distance/floors/energy) carry `interval.startTime/endTime`;
sample types (heart rate) carry `sampleTime.physicalTime`.

The exact field names for the less-common metrics (energy, floors, sleep,
resting HR) aren't fully pinned from the docs, so each metric's data-type name,
filter field, and value keys live in the `METRICS` table below and per-metric
API failures are non-fatal — a wrong guess for one metric just skips it rather
than failing the whole sync. Look for `# TODO: confirm against live v4 response`.
"""

import logging
import time
from datetime import date, datetime, time as time_cls, timedelta, timezone
from typing import Callable

import httpx

from app.config import settings
from app.models import HealthDataRequest

logger = logging.getLogger(__name__)

_PAGE_SIZE = 1000  # steps/distance come as many short intervals; page big.


class GoogleHealthError(Exception):
    """Base error for Google Health sync failures."""


class GoogleHealthAuthError(GoogleHealthError):
    """OAuth failed — typically an expired/revoked refresh token.

    Testing-mode refresh tokens expire ~7 days after issue, so this is the
    expected failure mode; the caller should prompt the user to re-run the
    OAuth 2.0 Playground.
    """


# --- OAuth ----------------------------------------------------------------

# Cached access token: (token, expires_at_epoch). Access tokens last ~1h.
_access_token: tuple[str, float] | None = None


def _get_access_token(client: httpx.Client) -> str:
    """Exchange the refresh token for an access token, cached until near expiry."""
    global _access_token
    if _access_token and _access_token[1] - time.time() > 60:
        return _access_token[0]

    if not (
        settings.GOOGLE_HEALTH_CLIENT_ID
        and settings.GOOGLE_HEALTH_CLIENT_SECRET
        and settings.GOOGLE_HEALTH_REFRESH_TOKEN
    ):
        raise GoogleHealthAuthError(
            "Google Health credentials missing — set GOOGLE_HEALTH_CLIENT_ID, "
            "GOOGLE_HEALTH_CLIENT_SECRET and GOOGLE_HEALTH_REFRESH_TOKEN in .env."
        )

    resp = client.post(
        settings.GOOGLE_HEALTH_TOKEN_URI,
        data={
            "client_id": settings.GOOGLE_HEALTH_CLIENT_ID,
            "client_secret": settings.GOOGLE_HEALTH_CLIENT_SECRET,
            "refresh_token": settings.GOOGLE_HEALTH_REFRESH_TOKEN,
            "grant_type": "refresh_token",
        },
    )
    if resp.status_code >= 400:
        detail = resp.text
        if resp.status_code in (400, 401) and "invalid_grant" in detail:
            raise GoogleHealthAuthError(
                "Refresh token rejected (invalid_grant) — it has likely expired "
                "(testing-mode tokens last ~7 days). Re-run the OAuth 2.0 Playground "
                "and update GOOGLE_HEALTH_REFRESH_TOKEN."
            )
        raise GoogleHealthAuthError(f"Token endpoint returned {resp.status_code}: {detail}")

    body = resp.json()
    token = body.get("access_token")
    if not token:
        raise GoogleHealthAuthError(f"No access_token in token response: {body}")

    _access_token = (token, time.time() + float(body.get("expires_in", 3600)))
    return token


# --- Metric mapping -------------------------------------------------------


def _sum(values: list[float]) -> float:
    return sum(values)


def _avg(values: list[float]) -> float:
    return sum(values) / len(values)


def _latest(values: list[float]) -> float:
    return values[-1]


def _min(values: list[float]) -> float:
    return min(values)


class Metric:
    """One Google Health data type mapped onto a health_data field.

    data_type:   kebab-case identifier used in the URL path (e.g. "active-energy-burned").
    field:       target column on health_data / HealthDataRequest.
    mode:        "list" (GET dataPoints with a time filter, aggregated here) or
                 "rollup" (POST dataPoints:dailyRollup — the API pre-aggregates the
                 day; used for types like floors/total-calories that don't support list).
    time_kind:   "interval" (steps/distance/energy/sleep) or "sample" (heart rate)
                 — determines the list filter time field. Ignored for rollup.
    value_keys:  camelCase JSON keys that may hold this metric's number in a listed
                 DataPoint (searched recursively). Ignored for rollup/duration.
    duration:    True => value is the interval length in minutes (used for sleep).
    aggregate:   reduces the day's per-point values to one daily figure (list mode).
    convert:     post-aggregation unit fix (e.g. mm -> km), or None.
    to_int:      round the final value to an int (for INTEGER columns).
    """

    def __init__(
        self,
        data_type: str,
        field: str,
        aggregate: Callable[[list[float]], float],
        mode: str = "list",
        value_keys: set[str] | None = None,
        time_kind: str = "interval",
        duration: bool = False,
        convert: Callable[[float], float] | None = None,
        to_int: bool = False,
        filter_field: str | None = None,
    ):
        self.data_type = data_type
        self.field = field
        self.aggregate = aggregate
        self.mode = mode
        self.value_keys = value_keys or set()
        self.time_kind = time_kind
        self.duration = duration
        self.convert = convert
        self.to_int = to_int
        self.filter_field = filter_field

    @property
    def time_field(self) -> str:
        """Field path used in the list `filter` expression (snake_case)."""
        if self.filter_field:  # explicit override (e.g. sessions filter by end_time)
            return self.filter_field
        # Hyphens aren't valid identifiers in the filter grammar, so snake_case.
        prefix = self.data_type.replace("-", "_")
        if self.time_kind == "sample":
            return f"{prefix}.sample_time.physical_time"
        return f"{prefix}.interval.start_time"


# Data-type IDs and modes confirmed against the live v4 API (2026-07):
#   steps/distance/heart-rate  -> list works
#   floors/total-calories      -> list unsupported; dailyRollup only
#   active-energy-burned       -> renamed from "active-energy"
# TODO: sleep session-filter member and exact energy value field still to confirm.
METRICS: list[Metric] = [
    Metric("steps", "steps", _sum, value_keys={"count"}, to_int=True),
    # distance value is reported in millimetres; store km.
    Metric("distance", "distance_km", _sum, value_keys={"millimeters"},
           convert=lambda mm: mm / 1_000_000.0),
    Metric("active-energy-burned", "active_energy_kcal", _sum,
           value_keys={"kilocalories", "calories", "energy", "kcal", "activeKilocalories"}),
    # Resting HR ≈ the day's lowest heart-rate sample.
    Metric("heart-rate", "resting_heart_rate", _min,
           value_keys={"beatsPerMinute", "bpm"}, time_kind="sample", to_int=True),
    # Sleep is a Session type: filter by when the session *ends* (Fitbit attributes
    # a night's sleep to the wake day). Sum the session durations, in minutes.
    Metric("sleep", "sleep_minutes", _sum, duration=True, to_int=True,
           filter_field="sleep.interval.end_time"),
    # These don't support list — use the daily rollup endpoint.
    Metric("floors", "flights_climbed", _sum, mode="rollup", to_int=True),
    Metric("total-calories", "total_calories_kcal", _sum, mode="rollup"),
]

_DATA_POINTS_PATH = "{base}/v4/users/me/dataTypes/{data_type}/dataPoints"
_ROLLUP_PATH = "{base}/v4/users/me/dataTypes/{data_type}/dataPoints:dailyRollUp"


def _day_bounds_rfc3339(day: date) -> tuple[str, str]:
    """RFC3339 UTC [start, next-day-start) bounds for a calendar day."""
    start = datetime.combine(day, time_cls.min, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    fmt = lambda dt: dt.isoformat().replace("+00:00", "Z")  # noqa: E731
    return fmt(start), fmt(end)


def _find_value(obj, keys: set[str]) -> float | None:
    """Recursively find the first numeric value under any of `keys` (camelCase)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and v is not None and not isinstance(v, (dict, list)):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
        for v in obj.values():
            found = _find_value(v, keys)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_value(v, keys)
            if found is not None:
                return found
    return None


def _first_number(obj) -> float | None:
    """Recursively find the first numeric leaf value (any key). Used for rollups,
    whose per-day value lives under a data-type-specific field like `countSum`."""
    if isinstance(obj, dict):
        for v in obj.values():
            found = _first_number(v)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _first_number(v)
            if found is not None:
                return found
    elif obj is not None and not isinstance(obj, bool):
        try:
            return float(obj)
        except (TypeError, ValueError):
            return None
    return None


def _interval_minutes(point: dict) -> float | None:
    """Length of a DataPoint's interval in minutes, or None if unparseable."""
    interval = point.get("interval", {})
    start, end = interval.get("startTime"), interval.get("endTime")
    if not (start and end):
        return None
    try:
        t0 = datetime.fromisoformat(start.replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return max((t1 - t0).total_seconds() / 60.0, 0.0)


def _point_value(metric: Metric, point: dict) -> float | None:
    if metric.duration:
        return _interval_minutes(point)
    return _find_value(point, metric.value_keys)


def _civil(day: date, hours: int, minutes: int, seconds: int) -> dict:
    return {
        "date": {"year": day.year, "month": day.month, "day": day.day},
        "time": {"hours": hours, "minutes": minutes, "seconds": seconds},
    }


def _check_response(metric: Metric, day: date, resp: httpx.Response) -> dict | None:
    """Shared HTTP status handling. Returns a raw-summary dict to short-circuit
    (skip metric), or None to proceed. Escalates 401 to an auth error."""
    if resp.status_code == 401:
        raise GoogleHealthAuthError("Google Health API returned 401 — token invalid.")
    if resp.status_code == 404:
        logger.info("Google Health: no %s data (404) for %s", metric.data_type, day)
        return {"status": 404}
    if resp.status_code >= 400:
        logger.warning(
            "Google Health: skipping %s — %s: %s",
            metric.data_type, resp.status_code, resp.text[:300],
        )
        return {"status": resp.status_code, "error": resp.text[:500]}
    return None


def _collect_list_values(client, token, metric, day) -> tuple[list[float], dict]:
    start, end = _day_bounds_rfc3339(day)
    url = _DATA_POINTS_PATH.format(
        base=settings.GOOGLE_HEALTH_API_BASE.rstrip("/"), data_type=metric.data_type
    )
    filter_expr = f'{metric.time_field} >= "{start}" AND {metric.time_field} < "{end}"'
    values: list[float] = []
    pages = 0
    page_token: str | None = None
    while True:
        params = {"filter": filter_expr, "pageSize": _PAGE_SIZE}
        if page_token:
            params["pageToken"] = page_token
        resp = client.get(url, headers={"Authorization": f"Bearer {token}"}, params=params)
        skip = _check_response(metric, day, resp)
        if skip is not None:
            return [], skip
        body = resp.json()
        for point in body.get("dataPoints", []):
            v = _point_value(metric, point)
            if v is not None:
                values.append(v)
        pages += 1
        page_token = body.get("nextPageToken")
        if not page_token or pages >= 50:  # hard cap to avoid runaway paging
            break
    return values, {"points": len(values), "pages": pages}


def _collect_rollup_values(client, token, metric, day) -> tuple[list[float], dict]:
    """POST dataPoints:dailyRollup — the API returns one pre-aggregated point per day."""
    url = _ROLLUP_PATH.format(
        base=settings.GOOGLE_HEALTH_API_BASE.rstrip("/"), data_type=metric.data_type
    )
    body = {
        "range": {"start": _civil(day, 0, 0, 0), "end": _civil(day, 23, 59, 59)},
        "windowSizeDays": 1,
    }
    resp = client.post(url, headers={"Authorization": f"Bearer {token}"}, json=body)
    skip = _check_response(metric, day, resp)
    if skip is not None:
        return [], skip
    payload = resp.json()
    values: list[float] = []
    for point in payload.get("rollupDataPoints", payload.get("dataPoints", [])):
        # Ignore the civil-time envelope; the value lives under a type-specific key.
        rest = {k: v for k, v in point.items() if not str(k).startswith("civil")}
        n = _first_number(rest)
        if n is not None:
            values.append(n)
    return values, {"rollupPoints": len(values)}


def _fetch_metric(client: httpx.Client, token: str, metric: Metric, day: date) -> tuple[float | None, dict]:
    """Fetch and aggregate one metric for a day, via its access mode.

    Returns (value_or_None, raw_summary). Never raises for ordinary API errors —
    logs and returns (None, {...}) so a single bad metric doesn't fail the whole
    sync. Only a 401 (token invalid) is escalated to GoogleHealthAuthError.
    """
    if metric.mode == "rollup":
        values, raw = _collect_rollup_values(client, token, metric, day)
    else:
        values, raw = _collect_list_values(client, token, metric, day)

    if not values:
        return None, raw

    result = metric.aggregate(values)
    if metric.convert:
        result = metric.convert(result)
    result = int(round(result)) if metric.to_int else round(float(result), 3)
    return result, raw


def fetch_day(day: date) -> HealthDataRequest:
    """Fetch all mapped metrics for `day` and assemble a HealthDataRequest.

    Raises GoogleHealthAuthError on auth failure. Individual metrics that are
    absent (404 / empty) or fail with a non-auth API error are left as None
    rather than failing the whole sync.
    """
    fields: dict = {"date": day, "source": settings.GOOGLE_HEALTH_SOURCE}
    raw_by_type: dict = {}

    with httpx.Client(timeout=30) as client:
        token = _get_access_token(client)
        for metric in METRICS:
            value, raw = _fetch_metric(client, token, metric, day)
            raw_by_type[metric.data_type] = raw
            if value is not None:
                fields[metric.field] = value

    fields["raw_data"] = raw_by_type
    return HealthDataRequest(**fields)
