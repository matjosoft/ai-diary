import json
from datetime import date

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from app.database import get_connection
from app.models import HealthDataRequest, HealthDataResponse
from app.services.health import save_health_data

router = APIRouter(prefix="/api/health", tags=["health"])


def _row_to_health(row) -> HealthDataResponse:
    return HealthDataResponse(
        id=row["id"],
        date=row["date"],
        steps=row["steps"],
        distance_km=row["distance_km"],
        active_energy_kcal=row["active_energy_kcal"],
        flights_climbed=row["flights_climbed"],
        source=row["source"],
        raw_data=json.loads(row["raw_data"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@router.post("")
async def receive_health(payload: HealthDataRequest):
    action = save_health_data(payload)
    return {
        "status": "ok",
        "date": payload.date.isoformat(),
        "action": action,
    }


@router.get("")
async def list_health(
    from_date: date | None = Query(None, alias="from"),
    to_date: date | None = Query(None, alias="to"),
):
    with get_connection() as conn:
        query = "SELECT * FROM health_data WHERE 1=1"
        params: list = []

        if from_date:
            query += " AND date >= ?"
            params.append(from_date.isoformat())
        if to_date:
            query += " AND date <= ?"
            params.append(to_date.isoformat())

        query += " ORDER BY date DESC"
        rows = conn.execute(query, params).fetchall()

    return [_row_to_health(row) for row in rows]


@router.get("/{entry_date}")
async def get_health(entry_date: date):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM health_data WHERE date = ?", (entry_date.isoformat(),)
        ).fetchone()

    if not row:
        return JSONResponse(status_code=404, content={"error": "Health data not found"})

    return _row_to_health(row)
