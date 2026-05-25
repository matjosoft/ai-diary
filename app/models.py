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
    meals: dict[str, str] = {}


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
    meals: dict[str, str] = {}
    created_at: datetime
    updated_at: datetime


class HealthDataRequest(BaseModel):
    date: date
    steps: int | None = None
    distance_km: float | None = None
    active_energy_kcal: float | None = None
    flights_climbed: int | None = None
    source: str = "iphone"
    raw_data: dict = {}


class HealthDataResponse(BaseModel):
    id: int
    date: date
    steps: int | None = None
    distance_km: float | None = None
    active_energy_kcal: float | None = None
    flights_climbed: int | None = None
    source: str
    raw_data: dict = {}
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


class EditCommand(BaseModel):
    is_edit: bool
    mode: str = "replace"  # "replace" | "append"
    target: str | None = None  # "transcription" | "summary" | "both"
    date_from: str | None = None
    date_to: str | None = None
    old_text: str | None = None
    new_text: str | None = None
    append_text: str | None = None


class ChatRequest(BaseModel):
    question: str
    messages: list[dict] = []  # conversation history from Open WebUI
    client_type: str = "web"  # "web", "telegram", etc.


class ChatPhoto(BaseModel):
    date: str
    filename: str
    description: str = ""
    caption: str | None = None
    data_url: str | None = None  # base64 image data URL for inline rendering
    url: str | None = None  # server-relative URL when data_url isn't included


class ChatResponse(BaseModel):
    answer: str
    entries_used: int
    photos: list[ChatPhoto] = []
