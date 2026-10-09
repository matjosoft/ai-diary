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
sample types (heart rate) carry `sampleTime.physicalTime`. Sleep is the odd one
out: its whole payload is nested under a `sleep` envelope and the day's figure
comes from `sleep.summary.minutesAsleep` (see `_collect_sleep_values`).

Day windows are built in *local* time, not UTC — the diary's "day" is the civil
day the owner lives in, and at UTC+2 a UTC window would drop the first two hours
of each morning into the previous day.

The exact field names for the less-common metrics (energy, floors) aren't fully
pinned from the docs, so each metric's data-type name, filter field, and value
keys live in the `METRICS` table below and per-metric API failures are non-fatal
— a wrong guess for one metric just skips it rather than failing the whole sync.
Look for `# TODO: confirm against live v4 response`.

Resting heart rate has no data type of its own here; it is estimated from the
day's intraday `heart-rate` samples — see `_resting_hr`.
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
    expected failure mode; the caller should prompt the user to re-authorise
    with `python -m app.jobs.google_auth` (or /healthauth in Telegram).
    """


# --- OAuth ----------------------------------------------------------------

# Cached access token: (token, expires_at_epoch). Access tokens last ~1h.
_access_token: tuple[str, float] | None = None


def reset_token_cache() -> None:
    """Drop the cached access token — call after the refresh token changes."""
    global _access_token
    _access_token = None


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
                "(testing-mode tokens last ~7 days). Re-authorise with "
                "`python -m app.jobs.google_auth` or /healthauth in Telegram."
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


# Resting heart rate is an estimate, not a reading. The Google Health API serves
# no computed resting-HR data type (only `heart-rate`, whose daily rollup gives
# min/avg/max), so we derive it from the day's ~30k intraday samples.
#
# The obvious choice — the day's lowest sample — is wrong: it's a single-sample
# floor that the wrist device hits on almost any day, so it reported a near-constant
# 39 bpm against the Fitbit app's 44-47. A low percentile of the day's samples
# tracks the app far better. Fitted against the app's own figures:
#
#     day        app   p20   sleep-median   lowest-30min-mean   min
#     2026-07-29  45    45        44               42            39
#     2026-07-30  44    43        43               40            39
#     2026-07-31  44    45        47               41            39
#
# Expect ~1 bpm of disagreement: Fitbit's figure comes from a proprietary
# algorithm (sleeping + sedentary HR) that is also smoothed across several days,
# which is why the app barely moved on 07-31 even though that night's HR ran
# ~6 bpm high. Tune with GOOGLE_HEALTH_RESTING_HR_PERCENTILE if your device
# reads differently.


def _percentile(values: list[float], pct: float) -> float:
    """Linearly-interpolated percentile (same convention as numpy's default)."""
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * pct / 100.0
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def _resting_hr(values: list[float]) -> float:
    return _percentile(values, settings.GOOGLE_HEALTH_RESTING_HR_PERCENTILE)


class Metric:
    """One Google Health data type mapped onto a health_data field.

    data_type:   kebab-case identifier used in the URL path (e.g. "active-energy-burned").
    field:       target column on health_data / HealthDataRequest.
    mode:        "list"   — GET dataPoints with a time filter, aggregated here;
                 "rollup" — POST dataPoints:dailyRollup, the API pre-aggregates the
                 day (types like floors/total-calories don't support list);
                 "sleep"  — list, then attribute each session to a night
                 (see `_collect_sleep_values`).
    time_kind:   "interval" (steps/distance/energy/sleep) or "sample" (heart rate)
                 — determines the list filter time field. Ignored for rollup.
    value_keys:  camelCase JSON keys that may hold this metric's number in a listed
                 DataPoint (searched recursively). Ignored for rollup/sleep.
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
# TODO: exact energy value field still to confirm.

# Sleep sessions are listed by when they *end* — a session that ends on the
# morning of D+1 is the night of D, and the two-day window below is wide enough
# to also catch daytime naps on D. `_collect_sleep_values` does the attribution.
SLEEP_METRIC = Metric("sleep", "sleep_minutes", _sum, mode="sleep", to_int=True,
                      filter_field="sleep.interval.end_time")

METRICS: list[Metric] = [
    Metric("steps", "steps", _sum, value_keys={"count"}, to_int=True),
    # distance value is reported in millimetres; store km.
    Metric("distance", "distance_km", _sum, value_keys={"millimeters"},
           convert=lambda mm: mm / 1_000_000.0),
    Metric("active-energy-burned", "active_energy_kcal", _sum,
           value_keys={"kilocalories", "calories", "energy", "kcal", "activeKilocalories"}),
    # Resting HR is estimated from the day's intraday samples — see `_resting_hr`.
    Metric("heart-rate", "resting_heart_rate", _resting_hr,
           value_keys={"beatsPerMinute", "bpm"}, time_kind="sample", to_int=True),
    SLEEP_METRIC,
    # These don't support list — use the daily rollup endpoint.
    Metric("floors", "flights_climbed", _sum, mode="rollup", to_int=True),
    Metric("total-calories", "total_calories_kcal", _sum, mode="rollup"),
]

_DATA_POINTS_PATH = "{base}/v4/users/me/dataTypes/{data_type}/dataPoints"
_ROLLUP_PATH = "{base}/v4/users/me/dataTypes/{data_type}/dataPoints:dailyRollUp"


def _local_midnight(day: date) -> datetime:
    """Midnight at the start of `day` in the machine's local timezone."""
    return datetime.combine(day, time_cls.min).astimezone()


def _day_bounds_rfc3339(day: date, days: int = 1) -> tuple[str, str]:
    """RFC3339 UTC bounds for `days` local calendar days starting at `day`.

    Anchored on local midnights rather than a fixed 24h span so the window stays
    a whole civil day across a DST change.
    """
    start = _local_midnight(day)
    end = _local_midnight(day + timedelta(days=days))
    fmt = lambda dt: dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")  # noqa: E731
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
    seen = 0
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
        points = body.get("dataPoints", [])
        seen += len(points)
        for point in points:
            v = _find_value(point, metric.value_keys)
            if v is not None:
                values.append(v)
        pages += 1
        page_token = body.get("nextPageToken")
        if not page_token or pages >= 50:  # hard cap to avoid runaway paging
            break
    # Record both counts: a `points` > 0 with `values` 0 means the API returned
    # data we failed to read, which looks identical to "no data" otherwise.
    return values, {"points": seen, "values": len(values), "pages": pages}


# --- Sleep ---------------------------------------------------------------
#
# Sleep needs its own collector for two reasons:
#
#   1. Shape. A sleep DataPoint nests everything under a `sleep` envelope
#      (`point["sleep"]["interval"]`, not `point["interval"]`), and the useful
#      number is `summary.minutesAsleep` — time actually asleep, excluding the
#      awake stretches inside the sleep period.
#   2. Attribution. A night belongs to the evening you went to bed, so the diary
#      row for day D holds the night D -> D+1. Bedtime is often past midnight
#      (roughly half the nights here), so the start time is not a reliable day
#      marker; the wake date minus one day is. Naps are the exception — they
#      count toward the day they happen on.
#
# Because the sync runs in the evening, day D's own night hasn't happened yet;
# the run on the evening of D+1 fills it in (see app/jobs/health_sync.py).

_SLEEP_WINDOW_DAYS = 2  # day D's night ends on D+1; also catches naps on D.


def _utc_offset(value) -> timedelta | None:
    """Parse an API offset like "7200s" into a timedelta."""
    try:
        return timedelta(seconds=int(str(value).rstrip("s")))
    except (TypeError, ValueError):
        return None


def _civil_date(timestamp, offset) -> date | None:
    """Calendar date of an RFC3339 instant, in the offset the device recorded.

    Falls back to this machine's timezone when the point carries no offset.
    """
    if not timestamp:
        return None
    try:
        moment = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    delta = _utc_offset(offset)
    return (moment + delta).date() if delta is not None else moment.astimezone().date()


def _sleep_session_day(sleep: dict) -> date | None:
    """The day a sleep session counts toward: nap -> its own day, night -> bedtime day."""
    interval = sleep.get("interval", {})
    started = _civil_date(interval.get("startTime"), interval.get("startUtcOffset"))
    woke = _civil_date(interval.get("endTime"), interval.get("endUtcOffset"))
    if sleep.get("metadata", {}).get("nap"):
        return started or woke
    return woke - timedelta(days=1) if woke else started


def _sleep_minutes(sleep: dict) -> float | None:
    """Minutes actually asleep in a session (API reports it as a string)."""
    try:
        return float(sleep.get("summary", {}).get("minutesAsleep"))
    except (TypeError, ValueError):
        return None


def _collect_sleep_values(client, token, metric, day) -> tuple[list[float], dict]:
    """List sleep sessions ending in a two-day window and keep those belonging to `day`."""
    start, end = _day_bounds_rfc3339(day, days=_SLEEP_WINDOW_DAYS)
    url = _DATA_POINTS_PATH.format(
        base=settings.GOOGLE_HEALTH_API_BASE.rstrip("/"), data_type=metric.data_type
    )
    filter_expr = f'{metric.time_field} >= "{start}" AND {metric.time_field} < "{end}"'
    resp = client.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        params={"filter": filter_expr, "pageSize": _PAGE_SIZE},
    )
    skip = _check_response(metric, day, resp)
    if skip is not None:
        return [], skip

    points = resp.json().get("dataPoints", [])
    values: list[float] = []
    for point in points:
        sleep = point.get("sleep", {})
        if _sleep_session_day(sleep) != day:
            continue  # a neighbouring night or nap that the window swept up
        minutes = _sleep_minutes(sleep)
        if minutes is not None:
            values.append(minutes)
    return values, {"sessions": len(points), "matched": len(values)}


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
    elif metric.mode == "sleep":
        values, raw = _collect_sleep_values(client, token, metric, day)
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


def fetch_sleep_minutes(day: date) -> int | None:
    """Minutes asleep during the night that started on `day`, or None if unknown.

    Split out from `fetch_day` because the nightly sync runs before that night
    happens: the run on the evening of D+1 uses this to fill in day D's sleep
    without re-fetching (and rewriting) the rest of D's metrics.
    """
    with httpx.Client(timeout=30) as client:
        token = _get_access_token(client)
        value, raw = _fetch_metric(client, token, SLEEP_METRIC, day)
    logger.info("Google Health: sleep for %s -> %s (%s)", day, value, raw)
    return value
