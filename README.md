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
- **Health data** -- steps, distance, active energy, and flights climbed from Apple Health, plus resting heart rate, sleep, and total calories from a Google Fitbit device. Three input paths, all upserting the same date-keyed row that feeds chat and audio summaries: an iPhone Shortcut `POST /api/health` (direct, on the same network as the Pi); pasting the Shortcut's JSON as a text message into the Telegram bot chat (works from anywhere -- the message must come from you, not from a bot); or a nightly **Google Health API (Fitbit) sync** (see [Google Health sync](#google-health-fitbit-sync))
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
| `POST` | `/api/health` | Upsert a day's health data (JSON body) |
| `GET` | `/api/health` | List health data (`?from=YYYY-MM-DD&to=YYYY-MM-DD`) |
| `GET` | `/api/health/{date}` | Get a single day's health data |
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

## Google Health (Fitbit) sync

Pull the day's metrics from a **Google Fitbit** device automatically each night via the
[Google Health API](https://developers.google.com/health) — Google's official successor to the
Fitbit Web API (uses Google OAuth 2.0; the legacy Fitbit Web API is turned down in September 2026).
No phone in the loop.

### One-time OAuth setup (testing mode)

All Google Health API scopes are **Restricted**, which normally requires a privacy/security
(CASA) review. For a personal project you avoid that by staying in OAuth **testing** mode against
your own Google account:

1. In the [Google Cloud Console](https://console.cloud.google.com/), create a project, enable the
   **Google Health API**, and configure the OAuth consent screen as **External / Testing**, adding
   your own Google account as a **test user**.
2. Create an **OAuth client ID** of type *Web application* and add
   `https://developers.google.com/oauthplayground` as an authorized redirect URI.
3. Go to the [OAuth 2.0 Playground](https://developers.google.com/oauthplayground), click the gear
   icon → *Use your own OAuth credentials*, and paste your client ID/secret.
4. In the scope box, request the read-only Google Health scopes you need, e.g.:
   - `https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly` (steps, distance, energy, floors)
   - `https://www.googleapis.com/auth/googlehealth.heart_rate.readonly`
   - `https://www.googleapis.com/auth/googlehealth.sleep.readonly`
5. Authorize, then exchange the code for tokens and copy the **refresh token**.

Put the values in `.env`:

```
GOOGLE_HEALTH_CLIENT_ID=...
GOOGLE_HEALTH_CLIENT_SECRET=...
GOOGLE_HEALTH_REFRESH_TOKEN=...
# optional overrides: GOOGLE_HEALTH_SOURCE (default "fitbit"), HEALTH_SYNC_NOTIFY_CHAT_ID
```

> **Heads up — 7-day token expiry:** while the OAuth app stays in *Testing* status, Google expires
> the refresh token after ~7 days. When that happens the sync detects `invalid_grant` and sends you
> a Telegram alert; just repeat steps 3–5 to mint a fresh refresh token. (Publishing the app to
> *Production* removes the expiry but, for Restricted scopes, would require the CASA review.)

### Run it

```bash
python -m app.jobs.health_sync                     # sync today
python -m app.jobs.health_sync --date 2026-07-12   # a specific day
python -m app.jobs.health_sync --from 2026-07-01 --to 2026-07-12   # backfill a range
```

Each run OAuths to the Google Health API, upserts the day's `health_data` row, and sends a Telegram
confirmation with the numbers (or an alert on failure). Schedule it end-of-day with cron:

```cron
55 23 * * *  cd /path/to/ai-diary && python -m app.jobs.health_sync >> sync.log 2>&1
```

> The Google Health API v4 REST surface is new; all endpoint paths, data-type names, and response
> parsing live in [`app/services/google_health.py`](app/services/google_health.py) behind clearly
> marked constants (`METRICS`, `_DATA_POINTS_PATH`, `_extract_values`) so you can adjust them against
> the live response without touching the rest of the app.

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
    health.py          # Health-data upsert + JSON-message parsing (shared ingest path)
    google_health.py   # Google Health API (Fitbit) client — OAuth + daily metric fetch
    notify.py          # One-shot Telegram sender (for standalone scripts/jobs)
  jobs/
    health_sync.py     # CLI: nightly Google Health (Fitbit) sync
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
