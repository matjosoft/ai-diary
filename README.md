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
                    Transcribe (OpenRouter or
                     separate transcription server)
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
- **Transcription** -- via OpenRouter's audio API by default, or offloaded to any OpenAI-compatible transcription server (Speaches/faster-whisper-server, whisper.cpp, LocalAI, LM Studio, Groq, OpenAI) by setting `TRANSCRIPTION_BASE_URL` / `TRANSCRIPTION_API_KEY` in `.env`
- **LLM analysis** -- extracts summary, mood, events, people, topics, and planned actions
- **Photo capture** -- send images via Telegram; a vision model writes a Swedish description and attaches the photo to the day's entry
- **Health data** -- steps, distance, active energy, and flights climbed from Apple Health, plus resting heart rate, sleep, and total calories from a Google Fitbit device. Three input paths, all upserting the same date-keyed row that feeds chat and audio summaries: an iPhone Shortcut `POST /api/health` (direct, on the same network as the Pi); pasting the Shortcut's JSON as a text message into the Telegram bot chat (works from anywhere -- the message must come from you, not from a bot); or a nightly sync from either the **Google Health API (Fitbit)** (see [Google Health sync](#google-health-fitbit-sync)) or **Home Assistant** sensors over MCP (see [Home Assistant sync](#home-assistant-health-sync))
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

All `/api/...` endpoints require the shared token when `API_TOKEN` is set — see
[API token](#api-token). `GET /` is always open.

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

### API token

The diary API is unauthenticated out of the box, which is fine only if nothing but you
can reach the port. To lock it down, generate a token and put it in `.env`:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

```bash
API_TOKEN=<the generated token>
```

Restart the server. Every `/api/...` endpoint now requires the token; `GET /` stays open
so you can still health-check the server. Leaving `API_TOKEN` empty disables the check
entirely, so nothing breaks until you set it. The startup log says which mode you are in:

```
[startup] API token auth enabled
```

Clients can present the token three ways:

| Form | Used by |
|------|---------|
| `X-API-Key: <token>` header | iPhone Shortcut, Open WebUI pipe, `curl` |
| `Authorization: Bearer <token>` header | generic HTTP clients |
| `?token=<token>` query parameter | links the *browser* opens, where no header can be set |

The third form exists because the photo and podcast-MP3 URLs are opened directly by the
browser. The server appends `?token=...` to those URLs itself when it hands them out, so
the `[▶ Lyssna]` link in Open WebUI keeps working with auth on.

```bash
curl -H "X-API-Key: $API_TOKEN" http://localhost:8000/api/entries
```

The Telegram bot is unaffected — it runs inside the server process and calls the services
directly rather than going over HTTP.

#### iPhone Shortcut

In each **Get Contents of URL** action (the audio upload and the health POST), tap the
arrow to expand it and add a header:

1. **Headers** → `+`
2. Key: `X-API-Key`
3. Text: your token

Method and Request Body stay exactly as they were. Note that the token is stored in
plaintext inside the Shortcut and sent in the clear over plain HTTP, so treat this as an
access gate on your own network, not as encryption.

### Transcription on a separate server

By default transcription goes through OpenRouter, like the rest of the LLM work. To
run it elsewhere -- a beefier machine on the LAN, a hosted Whisper API -- point these
at any OpenAI-compatible endpoint:

```bash
TRANSCRIPTION_BASE_URL=http://192.168.1.50:8000/v1
TRANSCRIPTION_API_KEY=              # many local servers need none
TRANSCRIPTION_MODEL=Systran/faster-whisper-large-v3
```

Nothing else changes: the Pi still stores the audio and runs the pipeline, it just
uploads the file to that server instead of to OpenRouter. Leave `TRANSCRIPTION_BASE_URL`
empty and the original OpenRouter path is used.

Two API shapes are supported, selected by `TRANSCRIPTION_API`:

| Value | Request | Use for |
|-------|---------|---------|
| `auto` (default) | `audio` when `TRANSCRIPTION_BASE_URL` is set, else `chat` | most setups |
| `audio` | multipart `POST {base}/audio/transcriptions` | Speaches/faster-whisper-server, whisper.cpp server, LocalAI, LM Studio, vLLM, Groq, OpenAI |
| `chat` | chat completions with an `input_audio` part | OpenRouter and other audio-capable chat models |

`TRANSCRIPTION_LANGUAGE` (default `sv`) is passed as the language hint in `audio` mode --
empty lets the service auto-detect. `TRANSCRIPTION_TIMEOUT` (default 600s) bounds the
wait; raise it for slow CPU-only Whisper boxes.

Quick check that a server is reachable and speaks the right dialect:

```bash
curl -sS http://192.168.1.50:8000/v1/audio/transcriptions \
  -F model=Systran/faster-whisper-large-v3 -F language=sv \
  -F file=@audio/2026-09-05.m4a
```

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
2. Create an **OAuth client ID** of type *Web application* and add `http://localhost:8765/` as an
   authorized redirect URI (this is what `app.jobs.google_auth` listens on — change it with
   `GOOGLE_HEALTH_REDIRECT_URI` / `--port` if the port is taken).
3. Put the client id and secret in `.env`:

```
GOOGLE_HEALTH_CLIENT_ID=...
GOOGLE_HEALTH_CLIENT_SECRET=...
# optional overrides: GOOGLE_HEALTH_SOURCE (default "fitbit"), HEALTH_SYNC_NOTIFY_CHAT_ID,
# GOOGLE_HEALTH_REDIRECT_URI, GOOGLE_HEALTH_SCOPES
```

   The scopes requested (overridable via `GOOGLE_HEALTH_SCOPES`) are
   `googlehealth.activity_and_fitness.readonly` for steps/distance/energy/floors,
   `googlehealth.health_metrics_and_measurements.readonly` for heart rate, and
   `googlehealth.sleep.readonly` for sleep. Note there is no `googlehealth.heart_rate` scope —
   Google rejects it with `invalid_scope`.

4. Mint the refresh token — the job writes `GOOGLE_HEALTH_REFRESH_TOKEN` into `.env` for you:

```bash
python -m app.jobs.google_auth
```

It prints a Google consent URL, catches the redirect on `localhost:8765`, exchanges the code,
verifies the new token, and backfills any days the sync missed. On a headless Pi, either forward
the port (`ssh -L 8765:localhost:8765 pi@your-pi`) or approve on any device and paste the address
bar back:

```bash
python -m app.jobs.google_auth --print-url            # authorize on your phone/laptop
python -m app.jobs.google_auth --code '<pasted URL>'  # finish (accepts the bare code too)
```

### Re-authorizing (the 7-day token expiry)

> Step-by-step walkthrough for the recurring renewal: **[GOOGLE-REAUTH.md](GOOGLE-REAUTH.md)**.

While the OAuth app stays in *Testing* status, Google expires the refresh token after ~7 days.
(Publishing the app to *Production* removes the expiry but, for Restricted scopes, would require
the CASA review.) Three things keep that from silently costing you a day of health data:

- **A daily check** — `python -m app.jobs.google_auth --check` tests the stored token and Telegrams
  you a ready-to-click consent link when it's dead or on its last day. Exit code `0` = valid,
  `1` = expiring soon, `2` = invalid. Schedule it in the morning so you have all day to act:

  ```cron
  0 8 * * *  cd /path/to/ai-diary && .venv/bin/python -m app.jobs.google_auth --check >> sync.log 2>&1
  ```

- **Re-auth from Telegram, from anywhere** — send `/healthauth` to the bot (or use the link in the
  alert), approve in the browser, and paste the resulting address back into the chat. The bot
  exchanges it, rewrites `.env`, and immediately fetches the days that were missed. Both the check
  and the nightly sync's failure alert include the same link, so re-authorizing is usually one tap
  and one paste.

- **Automatic backfill** — after any successful re-auth, days in the last 10 with no `health_data`
  row are fetched (Google keeps the history). A second pass patches sleep onto rows that do exist
  but never got it, since a night lands a day after the rest of its day and an outage strands it.
  Skip both with `--no-backfill`.

The re-auth job also stamps `GOOGLE_HEALTH_REFRESH_TOKEN_ISSUED` in `.env`, which is how `--check`
knows the token's age and can warn a day *before* it expires rather than after.

### Run it

```bash
python -m app.jobs.health_sync                     # sync today
python -m app.jobs.health_sync --date 2026-07-12   # a specific day
python -m app.jobs.health_sync --from 2026-07-01 --to 2026-07-12   # backfill a range
```

Each run OAuths to the Google Health API, upserts the day's `health_data` row, and sends a Telegram
confirmation with the numbers (or an alert on failure). Schedule it end-of-day with cron:

**Sleep runs a day behind.** A night counts toward the evening you went to bed, so `sleep_minutes`
on day D is the night D → D+1 — which hasn't happened when D's own run fires at 23:30. The plain
nightly run therefore also patches *yesterday's* sleep (the night that ended this morning) into
yesterday's row, touching only that column. Runs for an explicit past date get their sleep directly,
since by then the night is over. Attribution comes from wake time minus a day rather than bedtime,
so a bedtime past midnight still lands on the right day; daytime naps count toward the day they
happen on. The stored figure is minutes *asleep*, excluding awake time inside the sleep period.

**Resting heart rate is an estimate.** The API serves no computed resting-HR figure — only the raw
intraday `heart-rate` samples — so the sync takes a low percentile of the day's ~30 000 samples
(`GOOGLE_HEALTH_RESTING_HR_PERCENTILE`, default 20). That lands within about 1 bpm of the Fitbit app.
Taking the day's *minimum* instead, as the sync originally did, gives a near-constant floor several
bpm too low. Exact agreement isn't reachable: Fitbit's own number comes from a proprietary algorithm
that is smoothed across several days. If your synced values read consistently high or low against the
app, nudge the percentile down or up.

Sleep and resting heart rate are part of the chat context, so "hur mycket sov jag i juli?" and "vilken
natt sov jag sämst?" work in both Telegram and Open WebUI. Day-grain context spells the night out as
`Sömn natten 2026-07-30→2026-07-31` (the Swedish "natten till X" and "natten efter X" mean opposite
nights), and the retrieval fetches one day of lookback so the night ending on the first morning of the
range is available. Period summaries and Dagboksradion get an average per night rather than a total.

```cron
55 23 * * *  cd /path/to/ai-diary && python -m app.jobs.health_sync >> sync.log 2>&1
0  8 * * *   cd /path/to/ai-diary && python -m app.jobs.google_auth --check >> sync.log 2>&1
```

> The Google Health API v4 REST surface is new; all endpoint paths, data-type names, and response
> parsing live in [`app/services/google_health.py`](app/services/google_health.py) behind clearly
> marked constants (`METRICS`, `_DATA_POINTS_PATH`, `_extract_values`) so you can adjust them against
> the live response without touching the rest of the app.

## Home Assistant health sync

As an alternative to Google Health, the same nightly job can read health sensors from
**Home Assistant** through its MCP server. Set `HEALTH_SYNC_PROVIDER=homeassistant` in `.env`.

1. In Home Assistant, add the **Model Context Protocol Server** integration (it serves
   Streamable HTTP MCP at `http://homeassistant.local:8123/api/mcp`, using the Assist API).
2. Expose each health sensor to Assist (**Settings → Voice assistants → Expose**). The MCP
   `GetLiveContext` tool only lists exposed entities.
3. Create a long-lived access token (**Profile → Security**) and configure:

```env
HEALTH_SYNC_PROVIDER=homeassistant
HOMEASSISTANT_MCP_URL=http://homeassistant.local:8123/api/mcp
HOMEASSISTANT_ACCESS_TOKEN=<long-lived token>
HOMEASSISTANT_SENSOR_STEPS=sensor.mattias_steg          # -> steps
HOMEASSISTANT_SENSOR_DISTANCE=sensor.mattias_avstand    # -> distance_km
HOMEASSISTANT_SENSOR_RESTING_HR=sensor.mattias_vilopuls # -> resting_heart_rate
HOMEASSISTANT_SENSOR_SLEEP=sensor.mattias_tid_sovande   # hours -> sleep_minutes
```

`GetLiveContext` identifies entities by friendly name, not entity id, so a configured
`sensor.mattias_avstand` is matched against the slugified names ("Mattias Avstånd" →
`mattias_avstand`). A friendly name works as the configured value too. If a sensor is renamed,
or two entities end up with the same name, set the friendly name explicitly.

The sleep sensor reports hours (e.g. `8.1`) and is stored as minutes (486). It covers the night
that ended this morning, so the run writes it to **yesterday's** row, following the same
convention as the Fitbit sync. Home Assistant only has current values, so this source syncs today
only: `--date`/`--from`/`--to` are rejected, and a missed night can't be backfilled. The existing
cron line (`python -m app.jobs.health_sync` late in the evening) works unchanged; the
`google_auth --check` line isn't needed with this source.

## Open WebUI Integration

The file `openwebui_pipe.py` is a Pipe function for [Open WebUI](https://github.com/open-webui/open-webui).

**Install:**
1. Open WebUI → Admin Panel → Functions → Add Function
2. Paste the contents of `openwebui_pipe.py`
3. Configure the Valves:
   - `DIARY_API_URL` — server-to-server URL used by the Open WebUI backend to call `/api/chat` (default: `http://host.docker.internal:8000`).
   - `PUBLIC_DIARY_URL` — browser-reachable URL used to build audio-summary links the user clicks. Leave empty if Open WebUI runs natively on the same host as the browser; set to e.g. `http://my-pi.local:8000` when Open WebUI runs in Docker.
   - `API_TOKEN` — must match `API_TOKEN` in the server's `.env`; sent as the `X-API-Key` header. Leave empty if the server has no token configured. A mismatch shows up in the chat as a 401 message naming this valve.

The pipe appears as **Dagbokassistenten** in the Open WebUI model selector and routes all questions through `POST /api/chat`.

## Project Structure

```
app/
  main.py              # FastAPI app and lifespan
  auth.py              # Shared-secret API token check (header or ?token=)
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
    transcription.py   # Audio-to-text (OpenRouter chat+audio, or a separate OpenAI-compatible server)
    llm.py             # LLM calls (analysis + chat)
    photos.py          # Vision description and photo storage/retrieval
    search.py          # Smart hierarchical retrieval (query analysis + FTS + context building)
    summaries.py       # Generate and store condensed period summaries
    reports.py         # Report generation
    audio_summary.py   # Build TTS-friendly podcast scripts and render to audio
    tts.py             # OpenRouter /audio/speech wrapper (with chunking)
    health.py          # Health-data upsert + JSON-message parsing (shared ingest path)
    google_health.py   # Google Health API (Fitbit) client — OAuth + daily metric fetch
    homeassistant_health.py # Home Assistant health sensors over MCP (GetLiveContext)
    google_oauth.py    # Re-authorisation flow (consent URL, code exchange, .env write)
    notify.py          # One-shot Telegram sender (for standalone scripts/jobs)
  jobs/
    health_sync.py     # CLI: nightly health sync (Google Health/Fitbit or Home Assistant)
    google_auth.py     # CLI: mint/check the Google OAuth refresh token
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
