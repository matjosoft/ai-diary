"""HTTP endpoints for podcast-style audio diary summaries.

Each endpoint accepts ?format=script|audio|json (default json — returns
metadata + script text plus a relative URL to fetch the audio).
"""

from pathlib import Path

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from app.services.audio_summary import generate_audio_summary, generate_script
from app.services.tts import media_type

router = APIRouter(prefix="/api/audio-summaries", tags=["audio-summaries"])


async def _respond(
    period_type: str,
    period_key: str,
    format: str,
    force: bool,
    style: str | None = None,
):
    if format == "script":
        result = await generate_script(
            period_type, period_key, style=style, force=force
        )
        if result["script"] is None:
            return JSONResponse(
                status_code=404, content={"error": "No entries for this period"}
            )
        return PlainTextResponse(result["script"])

    result = await generate_audio_summary(
        period_type, period_key, style=style, force=force
    )
    if result.get("audio_path") is None:
        return JSONResponse(
            status_code=404, content={"error": "No entries for this period"}
        )

    if format == "audio":
        return FileResponse(
            result["audio_path"],
            media_type=media_type(),
            filename=Path(result["audio_path"]).name,
        )

    return {
        "period_type": result["period_type"],
        "period_key": result["period_key"],
        "style": result["style"],
        "label": result["label"],
        "entry_count": result["entry_count"],
        "script": result["script"],
        "audio_url": f"/api/audio-summaries/file/{Path(result['audio_path']).name}",
    }


_STYLE_PATTERN = "^(default|factual|roasting)$"


@router.get("/day/{entry_date}")
async def day_summary(
    entry_date: str,
    format: str = Query("json", pattern="^(json|script|audio)$"),
    style: str | None = Query(None, pattern=_STYLE_PATTERN),
    force: bool = Query(False),
):
    return await _respond("day", entry_date, format, force, style)


@router.get("/month/{year_month}")
async def month_summary(
    year_month: str,
    format: str = Query("json", pattern="^(json|script|audio)$"),
    style: str | None = Query(None, pattern=_STYLE_PATTERN),
    force: bool = Query(False),
):
    try:
        y, m = year_month.split("-")
        int(y); int(m)
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "Use format YYYY-MM"})
    return await _respond("month", year_month, format, force, style)


@router.get("/year/{year}")
async def year_summary(
    year: int,
    format: str = Query("json", pattern="^(json|script|audio)$"),
    style: str | None = Query(None, pattern=_STYLE_PATTERN),
    force: bool = Query(False),
):
    return await _respond("year", str(year), format, force, style)


@router.get("/ytd/{year}")
async def year_to_date_summary(
    year: int,
    format: str = Query("json", pattern="^(json|script|audio)$"),
    style: str | None = Query(None, pattern=_STYLE_PATTERN),
    force: bool = Query(False),
):
    return await _respond("ytd", str(year), format, force, style)


@router.get("/file/{filename}")
async def get_audio_file(filename: str):
    if "/" in filename or "\\" in filename or ".." in filename:
        return JSONResponse(status_code=400, content={"error": "Invalid filename"})
    from app.config import settings  # local import to avoid cycle on reload

    filepath = settings.audio_summaries_dir / filename
    if not filepath.exists():
        return JSONResponse(status_code=404, content={"error": "File not found"})
    return FileResponse(filepath, media_type=media_type(), filename=filename)
