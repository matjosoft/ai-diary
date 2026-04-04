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


class QueryIntent(BaseModel):
    time_scope: str  # "day" | "week" | "month" | "year" | "all"
    date_from: str | None = None
    date_to: str | None = None
    search_terms: list[str] = []
    search_fields: list[str] = []
    needs_full_text: bool = False
    question_type: str  # "summary" | "detail" | "trend" | "lookup"


class ChatRequest(BaseModel):
    question: str
    messages: list[dict] = []  # conversation history from Open WebUI


class ChatResponse(BaseModel):
    answer: str
    entries_used: int
