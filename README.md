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
                    Store entry (SQLite + FTS5 index)
                          |
                          v
                    Refresh period summaries (monthly/yearly)
```

Audio is saved and acknowledged immediately. Transcription, LLM analysis, and summary refresh run as background tasks so the iPhone Shortcut doesn't block.

## Features

- **Voice capture** -- receive m4a audio from an iPhone Shortcut
- **Transcription** -- via OpenRouter audio API (configurable model)
- **LLM analysis** -- extracts summary, mood, events, people, topics, and planned actions
- **Daily merging** -- multiple recordings on the same day are combined into a single entry
- **Full-text search** -- FTS5 index over transcriptions, summaries, topics, and people
- **Smart chat** -- ask natural language questions; a query-analysis step picks the coarsest grain of data needed (daily summaries, monthly summaries, or full transcriptions) before answering
- **Period summaries** -- condensed monthly and yearly summaries stored in the database and used as chat context
- **Reports** -- generate monthly and yearly summaries with mood trends, recurring topics, and narrative overviews
- **Open WebUI integration** -- `openwebui_pipe.py` exposes the diary chat as a Pipe function ("Dagbokassistenten") inside Open WebUI

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

### Chat request format

```json
{
  "question": "Hur mådde jag i mars?",
  "messages": []
}
```

`messages` is an optional conversation history array (`[{"role": "user"|"assistant", "content": "..."}]`) used to maintain context across follow-up questions.

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

## Open WebUI Integration

The file `openwebui_pipe.py` is a Pipe function for [Open WebUI](https://github.com/open-webui/open-webui).

**Install:**
1. Open WebUI → Admin Panel → Functions → Add Function
2. Paste the contents of `openwebui_pipe.py`
3. Configure the `DIARY_API_URL` Valve to point at your Raspberry Pi (default: `http://host.docker.internal:8000`)

The pipe appears as **Dagbokassistenten** in the Open WebUI model selector and routes all questions through `POST /api/chat`.

## Project Structure

```
app/
  main.py              # FastAPI app and lifespan
  config.py            # Settings via pydantic-settings
  database.py          # SQLite schema (entries, summaries, FTS5 index + triggers)
  models.py            # Pydantic models (including QueryIntent for smart search)
  routers/
    entries.py         # Audio upload and entry CRUD
    chat.py            # Natural language queries
    reports.py         # Monthly/yearly reports
  services/
    pipeline.py        # Audio processing pipeline (transcribe → analyse → summarise)
    transcription.py   # Audio-to-text
    llm.py             # LLM calls (analysis + chat)
    search.py          # Smart hierarchical retrieval (query analysis + FTS + context building)
    summaries.py       # Generate and store condensed period summaries
    reports.py         # Report generation
  prompts/             # LLM prompt templates
    chat_query.txt     # System prompt for chat answers
    query_analysis.txt # System prompt for query intent analysis
openwebui_pipe.py      # Open WebUI Pipe function
```

## How Smart Chat Works

1. The user's question is sent to `POST /api/chat`.
2. A lightweight LLM call (`query_analysis.txt` prompt) classifies the question into a `QueryIntent` — determining time scope, date range, search terms, and whether the answer needs full transcriptions or just summaries.
3. `smart_retrieve()` fetches the coarsest grain of data that can answer the question:
   - **trend** questions → monthly/yearly summaries + daily mood scores
   - **summary** questions → monthly summaries + daily entry summaries
   - **lookup** questions → FTS5 search → matching entry metadata
   - **detail** questions → FTS5 search → full transcriptions
4. The retrieved context is injected into the chat prompt and answered by the LLM.

## Privacy

All data (audio, database, reports) stays on your device. Only transcription text is sent to OpenRouter for LLM processing -- raw audio is never uploaded to the LLM.
