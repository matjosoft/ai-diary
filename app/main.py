from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.auth import require_token
from app.config import settings
from app.database import backfill_fts, init_db
from app.routers import audio_summaries, chat, entries, health, reports
from app.routers.entries import photo_router
from app.services.telegram import start_telegram_bot, stop_telegram_bot


@asynccontextmanager
async def lifespan(app: FastAPI):
    print(
        "[startup] models — "
        f"LLM_MODEL={settings.LLM_MODEL} "
        f"PHOTO_DESCRIPTION_MODEL={settings.PHOTO_DESCRIPTION_MODEL} "
        f"TTS_MODEL={settings.TTS_MODEL}",
        flush=True,
    )
    print(
        "[startup] API token auth "
        + ("enabled" if settings.API_TOKEN.strip() else "DISABLED (API_TOKEN unset)"),
        flush=True,
    )
    settings.audio_dir.mkdir(parents=True, exist_ok=True)
    settings.photos_dir.mkdir(parents=True, exist_ok=True)
    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    settings.audio_summaries_dir.mkdir(parents=True, exist_ok=True)
    init_db()
    backfill_fts()
    await start_telegram_bot()
    yield
    await stop_telegram_bot()


app = FastAPI(title="AI Diary", lifespan=lifespan)

# Every data router is gated on the shared API token (see app/auth.py). It is
# applied per router rather than app-wide so `GET /` stays an open health check.
_auth = [Depends(require_token)]

app.include_router(entries.router, dependencies=_auth)
app.include_router(photo_router, dependencies=_auth)
app.include_router(chat.router, dependencies=_auth)
app.include_router(reports.router, dependencies=_auth)
app.include_router(health.router, dependencies=_auth)
app.include_router(audio_summaries.router, dependencies=_auth)


@app.exception_handler(RequestValidationError)
async def log_validation_error(request: Request, exc: RequestValidationError):
    body = await request.body()
    print(
        f"[422] {request.method} {request.url.path} "
        f"body={body[:1000]!r} errors={exc.errors()}"
    )
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


@app.get("/")
async def root():
    return {"status": "ok", "service": "AI Diary"}
