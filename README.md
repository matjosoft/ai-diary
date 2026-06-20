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
- **Photo capture** -- send images via Telegram; a vision model writes a Swedish description and attaches the photo to the day's entry
- **Daily merging** -- multiple recordings on the same day are combined into a single entry
- **Full-text search** -- FTS5 index over transcriptions, summaries, topics, and people
- **Smart chat** -- ask natural language questions; a query-analysis step picks the coarsest grain of data needed (daily summaries, monthly summaries, or full transcriptions) before answering. Photos for referenced dates are returned alongside the answer
- **Period summaries** -- condensed monthly and yearly summaries stored in the database and used as chat context
- **Reports** -- generate monthly and yearly summaries with mood trends, recurring topics, and narrative overviews
- **Audio summaries (Dagboksradion)** -- a podcast/radio-style spoken summary for a day, month, full year, or year-to-date. The LLM writes a TTS-friendly script (in Swedish), OpenRouter renders it to MP3, and both the script and audio are cached. Triggerable from the chat in Open WebUI ("Ge mig en ljudsammanfattning för idag", "Gör en podcast av juni") or Telegram (natural language or `/summary <period>`)
- **Open WebUI integration** -- `openwebui_pipe.py` exposes the diary chat as a Pipe function ("Dagbokassistenten") inside Open WebUI, with inline photo rendering and audio summary links

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/entries` | Upload audio (raw m4a body) |
| `GET` | `/api/entries` | List entries (`?from=YYYY-MM-DD&to=YYYY-MM-DD`) |
| `GET` | `/api/entries/{date}` | Get a single day's entry |
| `GET` | `/api/entries/{date}/audio` | Download audio for a date |
| `GET` | `/api/photos/{filename}` | Download a stored photo |
| `POST` | `/api/chat` | Ask a question about your diary |
| `GET` | `/api/reports/monthly/{YYYY-MM}` | Monthly report |
| `GET` | `/api/reports/yearly/{YYYY}` | Yearly report |
| `GET` | `/api/audio-summaries/day/{YYYY-MM-DD}` | Podcast-style audio summary for a day |
| `GET` | `/api/audio-summaries/month/{YYYY-MM}` | Audio summary for a month |
| `GET` | `/api/audio-summaries/year/{YYYY}` | Audio summary for a full year |
| `GET` | `/api/audio-summaries/ytd/{YYYY}` | Audio summary for the year so far |
| `GET` | `/api/audio-summaries/file/{filename}` | Download a rendered audio file |

The audio-summary endpoints accept `?format=json|script|audio` (default `json`, returns metadata + script + a relative `audio_url`), `?style=default|factual|roasting` (host tone — defaults to `AUDIO_SUMMARY_STYLE` in `.env`), and `?force=true` to regenerate.

### Chat request format

```json
{
  "question": "Hur mådde jag i mars?",
  "messages": []
}
```

`messages` is an optional conversation history array (`[{"role": "user"|"assistant", "content": "..."}]`) used to maintain context across follow-up questions.

The response includes a `photos` array with any images attached to dates referenced in the answer. For non-Telegram clients each photo carries a base64 `data_url` for inline rendering; Telegram clients get a server-relative `url` instead and fetch each image separately.

If the question is detected as a podcast/radio-style audio-summary request (e.g. "Ge mig en ljudsammanfattning för juni"), the response instead carries `audio_url` and `audio_label`, and the chat handler skips the normal RAG flow. Telegram replies with the MP3 attached directly; Open WebUI appends a clickable `[▶ Lyssna]` link the browser opens with its native audio player.

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
3. Configure the Valves:
   - `DIARY_API_URL` — server-to-server URL used by the Open WebUI backend to call `/api/chat` (default: `http://host.docker.internal:8000`).
   - `PUBLIC_DIARY_URL` — browser-reachable URL used to build audio-summary links the user clicks. Leave empty if Open WebUI runs natively on the same host as the browser; set to e.g. `http://my-pi.local:8000` when Open WebUI runs in Docker.

The pipe appears as **Dagbokassistenten** in the Open WebUI model selector and routes all questions through `POST /api/chat`.

## Project Structure

```
app/
  main.py              # FastAPI app and lifespan
  config.py            # Settings via pydantic-settings
  database.py          # SQLite schema (entries, summaries, FTS5 index + triggers)
  models.py            # Pydantic models (including QueryIntent for smart search)
  routers/
    entries.py         # Audio upload, entry CRUD, photo download
    chat.py            # Natural language queries
    reports.py         # Monthly/yearly reports
    audio_summaries.py # Podcast-style audio summaries (day/month/year/ytd)
  services/
    pipeline.py        # Audio processing pipeline (transcribe → analyse → summarise)
    transcription.py   # Audio-to-text
    llm.py             # LLM calls (analysis + chat)
    photos.py          # Vision description and photo storage/retrieval
    search.py          # Smart hierarchical retrieval (query analysis + FTS + context building)
    summaries.py       # Generate and store condensed period summaries
    reports.py         # Report generation
    audio_summary.py   # Build TTS-friendly podcast scripts and render to audio
    tts.py             # OpenRouter /audio/speech wrapper (with chunking)
  prompts/             # LLM prompt templates
    chat_query.txt          # System prompt for chat answers
    query_analysis.txt      # System prompt for query intent analysis
    audio_summary.txt       # "Dagboksradion" host persona + show structure (default style)
    audio_summary_factual.txt  # Neutral, fact-only host style
    audio_summary_roasting.txt # Sarcastic, joking roast host style
    audio_summary_detect.txt # Intent detector for audio-summary requests (period + style)
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

## How Audio Summaries Work

1. When the user asks for a "ljudsammanfattning" / "podd" / "radioshow" (or uses `/summary` in Telegram), an LLM intent detector (`audio_summary_detect.txt`) resolves the period — `day` / `month` / `year` / `ytd` — and a concrete `period_key`, handling Swedish relative phrases like "idag", "förra månaden", "året så här långt". It also detects an optional **host style** from phrases like "roasta mig" or "bara fakta".
2. `audio_summary.py` fetches the matching entries plus aggregated health data and feeds them to the LLM with the style's prompt — a "Dagboksradion" host persona with a 10-segment show structure (cold open → headlines → events → mood → people → topics → body & movement → meals → planned items → outro) and length targets per period (2–4 min for a day, up to ~18 min for a full year).
3. The resulting spoken-Swedish script (no markdown, written-out numbers/dates, pause cues) is rendered to MP3 via OpenRouter's `/audio/speech` endpoint (`tts.py`), chunked on paragraph boundaries when the script exceeds the per-request character cap.
4. Both the script (`.md`) and the audio file are cached under `audio/summaries/` (style-suffixed for non-default styles, e.g. `month-2026-06-roasting.mp3`). Pass `?force=true` to regenerate.

### Host styles

Audio summaries come in three tones, selectable per request or set as the default via `AUDIO_SUMMARY_STYLE` in `.env`. The resolution order is **per-request style → `AUDIO_SUMMARY_STYLE` → built-in `default`**.

- **`default`** — warm, personal "Sommar i P1"-style host with nicknames and reflection.
- **`factual`** — neutral, fact-only reading with no personal touch, like a news recap.
- **`roasting`** — sarcastic, joking, on-the-edge roast (still grounded only in real entries).

Set the style via the `?style=default|factual|roasting` query param on the HTTP endpoints, by asking in natural language in chat/Telegram ("roasta gårdagen", "ge mig en saklig sammanfattning av juni"), or change the default in `.env`.

Configure the TTS model and voice via `TTS_MODEL`, `TTS_VOICE`, `TTS_FORMAT`, and `TTS_SPEED` in `.env`. Discover speech-capable models via the OpenRouter Models page (filter on speech output) or the Models API with `output_modalities=speech`.

## Privacy

All data (audio, photos, database, reports) stays on your device. Transcription text is sent to OpenRouter for LLM analysis, and photos are sent once to the vision model so a Swedish description can be generated -- the description is then stored locally and used for all subsequent chat context. Raw audio is never uploaded to the LLM.
