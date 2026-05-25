from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.config import settings
from app.database import backfill_fts, init_db
from app.routers import chat, entries, health, reports
from app.routers.entries import photo_router
from app.services.telegram import start_telegram_bot, stop_telegram_bot


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.audio_dir.mkdir(parents=True, exist_ok=True)
    settings.photos_dir.mkdir(parents=True, exist_ok=True)
    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    init_db()
    backfill_fts()
    await start_telegram_bot()
    yield
    await stop_telegram_bot()


app = FastAPI(title="AI Diary", lifespan=lifespan)

app.include_router(entries.router)
app.include_router(photo_router)
app.include_router(chat.router)
app.include_router(reports.router)
app.include_router(health.router)


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
