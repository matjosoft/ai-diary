# AI Diary — Specification

## Overview

An AI-powered voice diary application running on a Raspberry Pi. The user records audio on an iPhone, sends it to a FastAPI server on the Pi, where it is transcribed, analyzed by an LLM, and stored with structured metadata. The system supports conversational queries over diary data and generates automatic summary reports.

The recording language is **Swedish**.

## Architecture

```
iPhone (Shortcut) --> FastAPI Server (Raspberry Pi)
                          |
                          v
                    Save audio file
                          |
                          v
                    Transcribe (kb-whisper-medium)
                          |
                          v
                    LLM Analysis (OpenRouter)
                          |
                          v
                    Store entry (SQLite + filesystem)
                          |
                          v
                    Query interface / Reports
```

## Core Components

### 1. Audio Receiving (exists)

- **Endpoint:** `POST /api/entries`
- Receives raw audio body (m4a from iPhone Shortcut)
- Saves to `audio/` directory with timestamp filename
- Already implemented in `receive_audio.py`

### 2. Transcription

- **Model:** `KBLab/kb-whisper-medium` (Swedish-optimized Whisper) — https://huggingface.co/KBLab/kb-whisper-medium
- Run locally on the Pi using the `transformers` library with the Whisper pipeline
- The transcription model name must be configurable via `.env` to allow swapping models
- Input: m4a audio file on disk
- Output: full text transcription (string)
- The transcription should happen as part of the entry processing pipeline (triggered after audio is saved)

### 3. LLM Analysis

- **API:** OpenRouter (OpenAI-compatible API) with configurable base URL
- **Model:** Configurable via `.env`
- API key and base URL configured via `.env`
- Use the `openai` Python SDK pointed at the OpenRouter URL

#### Analysis outputs (structured JSON from LLM):

| Field             | Type       | Description                                       |
|-------------------|------------|---------------------------------------------------|
| `summary`         | string     | 2-4 sentence summary of the entry                 |
| `mood`            | string     | Primary mood (e.g. "glad", "stressad", "lugn")    |
| `mood_score`      | int (1-10) | Numeric mood rating (1=very negative, 10=very positive) |
| `events`          | list[str]  | Key events mentioned                              |
| `people`          | list[str]  | People mentioned                                  |
| `planned_actions` | list[str]  | Future plans or intentions mentioned               |
| `topics`          | list[str]  | Main topics/themes                                |

The LLM prompt should instruct the model to respond in Swedish for summary text, but use consistent labels/keys in English.

### 4. Storage

#### SQLite Database (`diary.db`)

**Table: `entries`**

| Column           | Type     | Description                              |
|------------------|----------|------------------------------------------|
| `id`             | INTEGER  | Primary key, autoincrement               |
| `date`           | DATE     | Entry date (one entry per day; if multiple recordings on the same day, append transcription to existing entry) |
| `audio_files`    | TEXT     | JSON list of audio filenames for this date |
| `transcription`  | TEXT     | Full transcription text                  |
| `summary`        | TEXT     | LLM-generated summary                   |
| `mood`           | TEXT     | Mood label                               |
| `mood_score`     | INTEGER  | Mood score 1-10                          |
| `events`         | TEXT     | JSON list of events                      |
| `people`         | TEXT     | JSON list of people                      |
| `planned_actions`| TEXT     | JSON list of planned actions             |
| `topics`         | TEXT     | JSON list of topics                      |
| `created_at`     | DATETIME | Timestamp of first recording             |
| `updated_at`     | DATETIME | Timestamp of last update                 |

#### Filesystem

- Audio files: `audio/YYYYMMDD_HHMMSS.m4a`
- Monthly reports: `reports/YYYY-MM.md`
- Yearly reports: `reports/YYYY.md`

### 5. Processing Pipeline

When audio is received:

1. Save audio file to disk (existing)
2. Transcribe audio using Whisper model
3. Check if an entry for today already exists in the database
   - If yes: append new transcription to existing text, re-run LLM analysis on the combined text
   - If no: create new entry
4. Send transcription to LLM for structured analysis
5. Store/update the entry in SQLite
6. Return success response with summary to the caller

The endpoint must return `200 OK` to the iPhone immediately after saving the audio file. Transcription and LLM analysis run as a **background task** (e.g. FastAPI `BackgroundTasks`) so the iPhone Shortcut is not left waiting.

### 6. Query Interface

#### REST API Endpoints

- `GET /api/entries` — List entries (with optional date range filter `?from=YYYY-MM-DD&to=YYYY-MM-DD`)
- `GET /api/entries/{date}` — Get a specific day's entry
- `GET /api/entries/{date}/audio` — Stream/download audio file(s) for a date
- `GET /api/reports/monthly/{YYYY-MM}` — Get or generate monthly report
- `GET /api/reports/yearly/{YYYY}` — Get or generate yearly report

#### Chat Endpoint

- `POST /api/chat` — Send a natural language question, get an AI-generated answer based on diary data

**Chat flow:**
1. Receive user question
2. Determine relevant date range from the question (using LLM)
3. Fetch relevant entries from SQLite
4. Send entries + question to LLM
5. Return answer

Example queries:
- "Ge mig en sammanfattning av mina dagboksanteckningar för förra månaden"
- "Beskriv mitt humör i januari förra året"
- "Följde jag upp planerade händelser förra veckan?"

#### MCP Server (future consideration)

The chat functionality should be designed so it can later be exposed as an MCP server, allowing Claude or other AI assistants to query the diary directly. This is out of scope for initial implementation but the chat logic should be modular enough to support it.

### 7. Automatic Reports

#### Monthly Report (generated on the 1st of each month, or on demand)

Markdown file containing:
- Overall mood trend (average score, mood distribution)
- Key events of the month
- People mentioned
- Recurring topics
- Planned vs completed actions (cross-referencing plans from prior months)
- LLM-written narrative summary of the month

#### Yearly Report (generated on January 1st, or on demand)

Markdown file containing:
- Month-by-month mood graph (text-based)
- Yearly highlights
- Most mentioned people and topics
- Goals and follow-through analysis
- LLM-written narrative summary of the year

Reports are triggered by a cron job or generated on demand via API.

## Configuration (`.env`)

```env
# Transcription
WHISPER_MODEL=KBLab/kb-whisper-medium

# LLM / OpenRouter
OPENROUTER_API_KEY=sk-or-...
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
LLM_MODEL=anthropic/claude-sonnet-4

# Storage
DATABASE_PATH=./diary.db
AUDIO_DIR=./audio
REPORTS_DIR=./reports

# Server
HOST=0.0.0.0
PORT=8000
```

## Non-Functional Requirements

- Must run on Raspberry Pi (ARM64, limited RAM) — keep dependencies lean
- Transcription will be slow on Pi hardware; that is acceptable
- All data stays local on the Pi (privacy)
- LLM calls go to OpenRouter (external API) — only transcription text is sent, never raw audio
