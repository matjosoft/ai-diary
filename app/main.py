from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import settings
from app.database import init_db
from app.routers import chat, entries, reports


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.audio_dir.mkdir(parents=True, exist_ok=True)
    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    init_db()
    yield


app = FastAPI(title="AI Diary", lifespan=lifespan)

app.include_router(entries.router)
app.include_router(chat.router)
app.include_router(reports.router)


@app.get("/")
async def root():
    return {"status": "ok", "service": "AI Diary"}
