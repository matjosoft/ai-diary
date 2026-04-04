from datetime import date, datetime
from pydantic import BaseModel


class AnalysisResult(BaseModel):
    summary: str
    mood: str
    mood_score: int
    events: list[str]
    people: list[str]
    planned_actions: list[str]
    topics: list[str]


class EntryResponse(BaseModel):
    id: int
    date: date
    audio_files: list[str]
    transcription: str
    summary: str | None = None
    mood: str | None = None
    mood_score: int | None = None
    events: list[str] = []
    people: list[str] = []
    planned_actions: list[str] = []
    topics: list[str] = []
    created_at: datetime
    updated_at: datetime


class ChatRequest(BaseModel):
    question: str


class ChatResponse(BaseModel):
    answer: str
    entries_used: int
