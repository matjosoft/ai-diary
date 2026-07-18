"""Health-data ingestion shared by the REST router and the Telegram bot.

The iPhone Shortcut can deliver an Apple Health snapshot two ways: as a POST to
`/api/health`, or as a plain JSON text message to the Telegram bot. Both paths
funnel through `save_health_data` so the upsert logic lives in one place.
"""

import json
import logging
from datetime import date as date_cls
from datetime import datetime

from app.database import get_connection
from app.models import HealthDataRequest

logger = logging.getLogger(__name__)


def save_health_data(payload: HealthDataRequest) -> str:
    """Upsert a health row keyed by date. Returns 'inserted' or 'updated'."""
    with get_connection() as conn:
        existed = conn.execute(
            "SELECT 1 FROM health_data WHERE date = ?",
            (payload.date.isoformat(),),
        ).fetchone()

        conn.execute(
            "INSERT INTO health_data "
            "(date, steps, distance_km, active_energy_kcal, flights_climbed, "
            "resting_heart_rate, sleep_minutes, total_calories_kcal, source, raw_data, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(date) DO UPDATE SET "
            "steps=excluded.steps, "
            "distance_km=excluded.distance_km, "
            "active_energy_kcal=excluded.active_energy_kcal, "
            "flights_climbed=excluded.flights_climbed, "
            "resting_heart_rate=excluded.resting_heart_rate, "
            "sleep_minutes=excluded.sleep_minutes, "
            "total_calories_kcal=excluded.total_calories_kcal, "
            "source=excluded.source, "
            "raw_data=excluded.raw_data, "
            "updated_at=datetime('now')",
            (
                payload.date.isoformat(),
                payload.steps,
                payload.distance_km,
                payload.active_energy_kcal,
                payload.flights_climbed,
                payload.resting_heart_rate,
                payload.sleep_minutes,
                payload.total_calories_kcal,
                payload.source,
                json.dumps(payload.raw_data, ensure_ascii=False),
            ),
        )

    return "updated" if existed else "inserted"


# Incoming keys vary with how the Shortcut is built; accept common spellings.
_INT_ALIASES = {
    "steps": ("steps", "step_count", "stepcount"),
    "flights_climbed": ("flights_climbed", "flights", "floors", "flights_climbed_count", "floors_climbed"),
}
_FLOAT_ALIASES = {
    "distance_km": ("distance_km", "distance", "walking_running_distance", "walking_distance"),
    "active_energy_kcal": (
        "active_energy_kcal",
        "active_energy",
        "active_calories",
        "active_energy_burned",
        "activekcal",
    ),
}


def _norm_keys(data: dict) -> dict:
    """Lower-case keys and collapse spaces/hyphens so aliases match loosely."""
    return {str(k).lower().replace(" ", "_").replace("-", "_"): v for k, v in data.items()}


def _coerce_number(value, cast):
    if value is None or value == "":
        return None
    try:
        return cast(float(value))
    except (TypeError, ValueError):
        return None


def _parse_date(value) -> date_cls:
    if not value:
        return date_cls.today()
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    # Last resort: ISO datetime like 2026-07-11T10:00:00
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return date_cls.today()


def parse_health_message(text: str) -> HealthDataRequest | None:
    """Parse a JSON text message into a HealthDataRequest, or None if it isn't one.

    Returns None for anything that isn't a JSON object, so ordinary chat text
    falls through to the normal handler.
    """
    text = text.strip()
    if not (text.startswith("{") and text.endswith("}")):
        return None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or not data:
        return None

    norm = _norm_keys(data)

    fields: dict = {"raw_data": data}
    fields["date"] = _parse_date(norm.get("date"))
    for field, aliases in _INT_ALIASES.items():
        for alias in aliases:
            if alias in norm:
                fields[field] = _coerce_number(norm[alias], int)
                break
    for field, aliases in _FLOAT_ALIASES.items():
        for alias in aliases:
            if alias in norm:
                fields[field] = _coerce_number(norm[alias], float)
                break
    if norm.get("source"):
        fields["source"] = str(norm["source"])

    payload = HealthDataRequest(**fields)

    # Require at least one recognised metric so a random JSON blob isn't stored.
    if all(
        getattr(payload, f) is None
        for f in ("steps", "distance_km", "active_energy_kcal", "flights_climbed")
    ):
        return None

    return payload


def format_health_confirmation(payload: HealthDataRequest, action: str) -> str:
    """Swedish confirmation summarising what was stored."""
    verb = "Uppdaterade" if action == "updated" else "Sparade"
    parts = []
    if payload.steps is not None:
        parts.append(f"{payload.steps} steg")
    if payload.distance_km is not None:
        parts.append(f"{payload.distance_km:.1f} km")
    if payload.active_energy_kcal is not None:
        parts.append(f"{payload.active_energy_kcal:.0f} kcal")
    if payload.flights_climbed is not None:
        parts.append(f"{payload.flights_climbed} våningar")
    if payload.resting_heart_rate is not None:
        parts.append(f"vilopuls {payload.resting_heart_rate} bpm")
    if payload.sleep_minutes is not None:
        hours, minutes = divmod(payload.sleep_minutes, 60)
        parts.append(f"sömn {hours}h {minutes}m")
    if payload.total_calories_kcal is not None:
        parts.append(f"{payload.total_calories_kcal:.0f} kcal totalt")
    detail = ", ".join(parts) if parts else "inga mätvärden"
    return f"<b>Hälsodata {payload.date.isoformat()}</b>\n{verb}: {detail}"
