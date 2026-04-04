# AI Diary — Implementation Plan

## Prerequisites

- Python 3.11+ (already available)
- FastAPI with uvicorn (already set up)
- SQLite (built-in)
- `.env` file with API keys and model config

## Phase 1: Foundation — Config, Database, and Project Structure

### 1.1 Project structure

```
ai-diary/
├── .env                    # Configuration (not committed)
├── .env.example            # Template for config
├── app/
│   ├── __init__.py
│   ├── main.py             # FastAPI app, mounts routers
│   ├── config.py           # Pydantic Settings, reads .env
│   ├── database.py         # SQLite setup, connection, schema
│   ├── models.py           # Pydantic models for entries, analysis results
│   ├── routers/
│   │   ├── __init__.py
│   │   ├── entries.py      # /api/entries endpoints
│   │   ├── chat.py         # /api/chat endpoint
│   │   └── reports.py      # /api/reports endpoints
│   ├── services/
│   │   ├── __init__.py
│   │   ├── transcription.py  # Whisper transcription
│   │   ├── llm.py            # OpenRouter LLM calls
│   │   ├── pipeline.py       # Orchestrates: transcribe → analyze → store
│   │   └── reports.py        # Report generation
│   └── prompts/
│       ├── analyze_entry.txt    # Prompt for entry analysis
│       ├── chat_query.txt       # Prompt for chat queries
│       ├── monthly_report.txt   # Prompt for monthly reports
│       └── yearly_report.txt    # Prompt for yearly reports
├── audio/                  # Audio files (existing)
├── reports/                # Generated markdown reports
├── diary.db                # SQLite database
├── requirements.txt
├── SPEC.md
└── PLAN.md
```

### 1.2 Tasks

1. Create `app/config.py` — Pydantic `Settings` class loading from `.env`
2. Create `.env.example` with all config keys
3. Create `app/database.py` — SQLite connection manager and schema creation (entries table per SPEC)
4. Create `app/models.py` — Pydantic models: `EntryCreate`, `EntryResponse`, `AnalysisResult`, `ChatRequest`, `ChatResponse`
5. Create `app/main.py` — FastAPI app with lifespan (init DB on startup), mount routers
6. Move existing `receive_audio.py` logic into `app/routers/entries.py`
7. Create `requirements.txt` with dependencies

**Dependencies to add:**
- `fastapi`, `uvicorn`
- `python-dotenv`, `pydantic-settings`
- `openai` (for OpenRouter calls)
- `transformers`, `torch`, `accelerate` (for Whisper)
- `soundfile` or `pydub` (for audio handling if needed)

## Phase 2: Transcription Service

### 2.1 Tasks

1. Create `app/services/transcription.py`
   - Load Whisper model on first use (lazy loading to save memory)
   - Function: `transcribe(audio_path: Path) -> str`
   - Use `transformers.pipeline("automatic-speech-recognition", model=config.WHISPER_MODEL)`
   - Handle m4a input (may need ffmpeg or pydub for conversion to wav)
2. Test transcription locally with a sample audio file
3. Consider memory: unload model after transcription if RAM is tight (configurable)

**Note:** ffmpeg must be installed on the Pi for audio format handling (`sudo apt install ffmpeg`).

## Phase 3: LLM Analysis Service

### 3.1 Tasks

1. Create `app/services/llm.py`
   - Initialize OpenAI client with `base_url` from config pointing at OpenRouter
   - Function: `analyze_entry(transcription: str) -> AnalysisResult`
   - Function: `chat_query(question: str, entries: list[Entry]) -> str`
   - Function: `generate_report(entries: list[Entry], report_type: str) -> str`
2. Create `app/prompts/analyze_entry.txt` — system prompt instructing the LLM to return structured JSON with: summary, mood, mood_score, events, people, planned_actions, topics
3. Parse LLM JSON response into `AnalysisResult` Pydantic model (with error handling for malformed responses)

## Phase 4: Processing Pipeline

### 4.1 Tasks

1. Create `app/services/pipeline.py`
   - Function: `process_audio(audio_path: Path) -> EntryResponse`
   - Steps: transcribe → check for existing entry today → analyze → store/update in DB → return result
2. Update `app/routers/entries.py` POST endpoint to save audio, return `200 OK` immediately, and trigger the pipeline via FastAPI `BackgroundTasks`
3. Handle the "multiple recordings per day" case: append transcription, re-analyze combined text

## Phase 5: Query Endpoints

### 5.1 Tasks

1. Implement `GET /api/entries` with date range filtering in `app/routers/entries.py`
2. Implement `GET /api/entries/{date}` — return full entry for a given date
3. Implement `GET /api/entries/{date}/audio` — serve audio file(s)
4. Create `app/routers/chat.py`
   - `POST /api/chat` — accepts `{ "question": "..." }`
   - Use LLM to parse date range from question, fetch relevant entries, generate answer
5. Create `app/prompts/chat_query.txt` — system prompt for the chat query LLM call

## Phase 6: Automatic Reports

### 6.1 Tasks

1. Create `app/services/reports.py`
   - Function: `generate_monthly_report(year: int, month: int) -> str`
   - Function: `generate_yearly_report(year: int) -> str`
   - Fetch entries for the period, send to LLM with report prompt, save markdown to `reports/`
2. Create `app/routers/reports.py`
   - `GET /api/reports/monthly/{YYYY-MM}` — return or generate monthly report
   - `GET /api/reports/yearly/{YYYY}` — return or generate yearly report
3. Create prompts: `app/prompts/monthly_report.txt`, `app/prompts/yearly_report.txt`
4. Add optional cron-triggered report generation (can be a simple script called by system cron)

## Implementation Order

Implement phases sequentially: **1 → 2 → 3 → 4 → 5 → 6**

Each phase should result in testable, working code before moving to the next. Test each phase via manual curl commands or a simple test script.

## Testing Approach

- Manual testing with curl/httpie for API endpoints
- Keep a sample audio file in the repo for transcription testing
- For LLM calls, verify JSON structure parsing works with a few real calls
- Test the full pipeline end-to-end: send audio → verify entry in DB with all fields populated
