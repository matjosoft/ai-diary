# AI Diary

An AI-powered voice diary running on a Raspberry Pi. Record audio on your iPhone, send it to the server, and get automatic transcription, mood analysis, and structured metadata -- all stored locally.

The recording language is **Swedish**.

## Architecture

```
iPhone (Shortcut) --> FastAPI Server (Raspberry Pi)
                          |
                          v
                    Save audio file
                          |
                          v
                    Transcribe (OpenRouter)
                          |
                          v
                    LLM Analysis (OpenRouter)
                          |
                          v
                    Store entry (SQLite + filesystem)
```

Audio is saved and acknowledged immediately. Transcription and LLM analysis run as background tasks so the iPhone Shortcut doesn't block.

## Features

- **Voice capture** -- receive m4a audio from an iPhone Shortcut
- **Transcription** -- via OpenRouter audio API (configurable model)
- **LLM analysis** -- extracts summary, mood, events, people, topics, and planned actions
- **Daily merging** -- multiple recordings on the same day are combined into a single entry
- **Chat** -- ask natural language questions about your diary (`POST /api/chat`)
- **Reports** -- generate monthly and yearly summaries with mood trends, recurring topics, and narrative overviews

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/entries` | Upload audio (raw m4a body) |
| `GET` | `/api/entries` | List entries (`?from=YYYY-MM-DD&to=YYYY-MM-DD`) |
| `GET` | `/api/entries/{date}` | Get a single day's entry |
| `GET` | `/api/entries/{date}/audio` | Download audio for a date |
| `POST` | `/api/chat` | Ask a question about your diary |
| `GET` | `/api/reports/monthly/{YYYY-MM}` | Monthly report |
| `GET` | `/api/reports/yearly/{YYYY}` | Yearly report |

## Setup

### Requirements

- Python 3.12+
- An [OpenRouter](https://openrouter.ai) API key

### Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Configure

```bash
cp .env.example .env
cp person.md.example person.md
```

Edit `.env` with your OpenRouter API key and preferred models. Edit `person.md` with your personal details (used by the LLM for context).

### Run

```bash
python -m app
```

The server starts on `http://0.0.0.0:8000` by default.

## Project Structure

```
app/
  main.py              # FastAPI app and lifespan
  config.py            # Settings via pydantic-settings
  database.py          # SQLite connection and schema
  models.py            # Pydantic models
  routers/
    entries.py         # Audio upload and entry CRUD
    chat.py            # Natural language queries
    reports.py         # Monthly/yearly reports
  services/
    pipeline.py        # Audio processing pipeline
    transcription.py   # Audio-to-text
    llm.py             # LLM calls (analysis + chat)
    reports.py         # Report generation
  prompts/             # LLM prompt templates
```

## Privacy

All data (audio, database, reports) stays on your device. Only transcription text is sent to OpenRouter for LLM processing -- raw audio is never uploaded to the LLM.
