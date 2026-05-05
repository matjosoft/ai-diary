import json
from datetime import date, datetime
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Query, Request
from fastapi.responses import FileResponse, JSONResponse

from app.config import settings
from app.database import get_connection
from app.models import EntryResponse
from app.services.pipeline import process_audio

router = APIRouter(prefix="/api/entries", tags=["entries"])


def _row_to_entry(row) -> EntryResponse:
    return EntryResponse(
        id=row["id"],
        date=row["date"],
        audio_files=json.loads(row["audio_files"]),
        transcription=row["transcription"],
        summary=row["summary"],
        mood=row["mood"],
        mood_score=row["mood_score"],
        events=json.loads(row["events"]),
        people=json.loads(row["people"]),
        planned_actions=json.loads(row["planned_actions"]),
        topics=json.loads(row["topics"]),
        meals=json.loads(row["meals"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@router.post("")
async def receive_entry(request: Request, background_tasks: BackgroundTasks):
    body = await request.body()

    if not body:
        return JSONResponse(
            status_code=400, content={"status": "error", "message": "Empty body"}
        )

    audio_dir = settings.audio_dir
    audio_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = audio_dir / f"{timestamp}.m4a"
    filepath.write_bytes(body)

    print(f"Saved {filepath.name} ({len(body)} bytes)")

    background_tasks.add_task(process_audio, filepath)

    return {"status": "ok", "filename": filepath.name, "size": len(body)}


@router.get("")
async def list_entries(
    from_date: date | None = Query(None, alias="from"),
    to_date: date | None = Query(None, alias="to"),
):
    with get_connection() as conn:
        query = "SELECT * FROM entries WHERE 1=1"
        params: list = []

        if from_date:
            query += " AND date >= ?"
            params.append(from_date.isoformat())
        if to_date:
            query += " AND date <= ?"
            params.append(to_date.isoformat())

        query += " ORDER BY date DESC"
        rows = conn.execute(query, params).fetchall()

    return [_row_to_entry(row) for row in rows]


@router.get("/{entry_date}")
async def get_entry(entry_date: date):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM entries WHERE date = ?", (entry_date.isoformat(),)
        ).fetchone()

    if not row:
        return JSONResponse(status_code=404, content={"error": "Entry not found"})

    return _row_to_entry(row)


@router.get("/{entry_date}/audio")
async def get_audio(entry_date: date):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT audio_files FROM entries WHERE date = ?",
            (entry_date.isoformat(),),
        ).fetchone()

    if not row:
        return JSONResponse(status_code=404, content={"error": "Entry not found"})

    audio_files = json.loads(row["audio_files"])
    if not audio_files:
        return JSONResponse(status_code=404, content={"error": "No audio files"})

    # Return the first audio file; for multiple files, client can pick from the list
    filepath = settings.audio_dir / audio_files[0]
    if not filepath.exists():
        return JSONResponse(status_code=404, content={"error": "Audio file missing"})

    return FileResponse(filepath, media_type="audio/mp4", filename=audio_files[0])
