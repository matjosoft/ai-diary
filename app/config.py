import logging
from pathlib import Path

from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    # Transcription
    WHISPER_MODEL: str = "KBLab/kb-whisper-medium"
    TRANSCRIPTION_MODEL: str = "google/gemini-3.1-flash-lite-preview"
    # Point transcription at a separate server/service (anything speaking the
    # OpenAI API — Speaches/faster-whisper-server, whisper.cpp, LocalAI,
    # LM Studio, Groq, OpenAI itself...). Empty => reuse the OpenRouter
    # credentials below, i.e. the original behaviour.
    TRANSCRIPTION_BASE_URL: str = ""
    TRANSCRIPTION_API_KEY: str = ""
    # Which API shape the transcription service speaks:
    #   "audio" — POST {base_url}/audio/transcriptions (OpenAI Whisper style)
    #   "chat"  — chat completions with an input_audio part (OpenRouter style)
    #   "auto"  — "audio" when TRANSCRIPTION_BASE_URL is set, else "chat"
    TRANSCRIPTION_API: str = "auto"
    # ISO-639-1 hint for the "audio" API; empty => let the service detect.
    TRANSCRIPTION_LANGUAGE: str = "sv"
    # Seconds to wait for the transcription service (local/CPU boxes are slow).
    TRANSCRIPTION_TIMEOUT: float = 600.0

    # LLM / OpenRouter
    OPENROUTER_API_KEY: str = ""
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"
    LLM_MODEL: str = "anthropic/claude-sonnet-4"
    PHOTO_DESCRIPTION_MODEL: str = "google/gemini-2.0-flash-exp"
    # Model for podcast/radio-style audio summary scripts.
    # Leave empty to fall back to LLM_MODEL (see podcast_model).
    PODCAST_MODEL: str = ""

    # TTS — used for audio summaries (podcast/radio style)
    TTS_MODEL: str = "openai/gpt-4o-mini-tts"
    TTS_VOICE: str = "alloy"
    TTS_FORMAT: str = "mp3"
    TTS_SPEED: float = 1.0

    # Audio summary host style — "default" | "factual" | "roasting".
    # Overridable per request; this is the fallback when none is given.
    AUDIO_SUMMARY_STYLE: str = "default"

    # Storage
    DATABASE_PATH: str = "./diary.db"
    AUDIO_DIR: str = "./audio"
    PHOTOS_DIR: str = "./photos"
    REPORTS_DIR: str = "./reports"
    AUDIO_SUMMARIES_DIR: str = "./audio/summaries"

    # Telegram
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_ALLOWED_USERS: str = ""  # comma-separated Telegram user IDs

    # Google Health API (Fitbit sync) — OAuth 2.0 credentials.
    # Mint/refresh the refresh token with `python -m app.jobs.google_auth`, which
    # writes GOOGLE_HEALTH_REFRESH_TOKEN(_ISSUED) back into .env. Testing-mode
    # tokens expire after ~7 days, so this is a recurring flow.
    GOOGLE_HEALTH_CLIENT_ID: str = ""
    GOOGLE_HEALTH_CLIENT_SECRET: str = ""
    GOOGLE_HEALTH_REFRESH_TOKEN: str = ""
    # Date the current refresh token was issued (YYYY-MM-DD), maintained by the
    # re-auth job so the check can warn a day before the ~7-day expiry.
    GOOGLE_HEALTH_REFRESH_TOKEN_ISSUED: str = ""
    GOOGLE_HEALTH_TOKEN_URI: str = "https://oauth2.googleapis.com/token"
    GOOGLE_HEALTH_AUTH_URI: str = "https://accounts.google.com/o/oauth2/v2/auth"
    # Must be registered as an authorised redirect URI on the OAuth client.
    # The loopback default lets the re-auth job catch the code automatically;
    # any registered URI works if you paste the code back instead.
    GOOGLE_HEALTH_REDIRECT_URI: str = "http://localhost:8765/"
    # Scope names verified against a granted token (tokeninfo); there is no
    # googlehealth.heart_rate scope — heart rate falls under health_metrics.
    #   activity_and_fitness        -> steps, distance, energy, floors, total calories
    #   health_metrics_and_measurements -> heart rate
    #   sleep                       -> sleep sessions
    GOOGLE_HEALTH_SCOPES: str = (
        "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly,"
        "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly,"
        "https://www.googleapis.com/auth/googlehealth.sleep.readonly"
    )
    GOOGLE_HEALTH_API_BASE: str = "https://health.googleapis.com"
    GOOGLE_HEALTH_SOURCE: str = "fitbit"  # stored in health_data.source
    # The API exposes no computed resting heart rate, only raw intraday samples,
    # so we estimate it as a low percentile of the day's samples. 20 was fitted
    # against the Fitbit app's own figures (see google_health.RESTING_HR_PERCENTILE).
    GOOGLE_HEALTH_RESTING_HR_PERCENTILE: float = 20.0
    # Chat id for daily sync notifications. Empty => first id in TELEGRAM_ALLOWED_USERS.
    HEALTH_SYNC_NOTIFY_CHAT_ID: str = ""

    # Where the nightly `python -m app.jobs.health_sync` reads from:
    #   "google"        — Google Health API (Fitbit), settings above
    #   "homeassistant" — Home Assistant sensors over its MCP server, below
    HEALTH_SYNC_PROVIDER: str = "google"

    # Home Assistant (MCP server integration). The token is a long-lived access
    # token from your HA profile; sensors must be exposed to Assist or the MCP
    # server won't list them. Only current values are available (no backfill).
    HOMEASSISTANT_MCP_URL: str = "http://homeassistant.local:8123/api/mcp"
    HOMEASSISTANT_ACCESS_TOKEN: str = ""
    HOMEASSISTANT_SOURCE: str = "homeassistant"  # stored in health_data.source
    # Entity ids (or friendly names) per health_data column; empty skips it.
    HOMEASSISTANT_SENSOR_STEPS: str = "sensor.mattias_steg"
    HOMEASSISTANT_SENSOR_DISTANCE: str = "sensor.mattias_avstand"  # km
    HOMEASSISTANT_SENSOR_RESTING_HR: str = "sensor.mattias_vilopuls"
    # Reported in hours (8.1) and stored as minutes; last night's value is
    # written to yesterday's row, matching how sleep is dated everywhere else.
    HOMEASSISTANT_SENSOR_SLEEP: str = "sensor.mattias_tid_sovande"

    # Security — shared secret required by every /api endpoint.
    # Empty (the default) disables the check, so an existing deployment keeps
    # working until a token is set. Generate one with:
    #   python -c "import secrets; print(secrets.token_urlsafe(32))"
    API_TOKEN: str = ""

    # Server
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # "ignore", not the pydantic default "forbid": an unrecognised key in
    # .env (a typo like TRANSCRIPTION_MODE for TRANSCRIPTION_MODEL) used to
    # raise here at import time, which killed every cron job before it could
    # even reach its own error handler — no sync, and no Telegram alert to
    # say so. Unknown keys are now reported by _warn_unknown_env_keys()
    # below instead, so a stray line degrades to "that setting didn't apply".
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    @property
    def transcription_base_url(self) -> str:
        """Base URL for transcription; falls back to OpenRouter."""
        return self.TRANSCRIPTION_BASE_URL.strip().rstrip("/") or self.OPENROUTER_BASE_URL

    @property
    def transcription_api_key(self) -> str:
        """API key for transcription; falls back to the OpenRouter key.

        Local servers usually need no key, but the OpenAI client insists on a
        non-empty one, so send a placeholder when nothing is configured.
        """
        if self.TRANSCRIPTION_API_KEY.strip():
            return self.TRANSCRIPTION_API_KEY.strip()
        if not self.TRANSCRIPTION_BASE_URL.strip():
            return self.OPENROUTER_API_KEY or "no-key"
        return "no-key"

    @property
    def transcription_api(self) -> str:
        """Resolved transcription API shape: "audio" or "chat"."""
        api = self.TRANSCRIPTION_API.strip().lower()
        if api in ("audio", "chat"):
            return api
        return "audio" if self.TRANSCRIPTION_BASE_URL.strip() else "chat"

    @property
    def health_sync_provider(self) -> str:
        """Resolved health sync source: "google" or "homeassistant"."""
        provider = self.HEALTH_SYNC_PROVIDER.strip().lower().replace("_", "").replace("-", "")
        return "homeassistant" if provider in ("homeassistant", "ha") else "google"

    @property
    def podcast_model(self) -> str:
        """Model used for podcast audio summaries; defaults to LLM_MODEL."""
        return self.PODCAST_MODEL.strip() or self.LLM_MODEL

    @property
    def database_path(self) -> Path:
        return Path(self.DATABASE_PATH)

    @property
    def audio_dir(self) -> Path:
        return Path(self.AUDIO_DIR)

    @property
    def photos_dir(self) -> Path:
        return Path(self.PHOTOS_DIR)

    @property
    def reports_dir(self) -> Path:
        return Path(self.REPORTS_DIR)

    @property
    def audio_summaries_dir(self) -> Path:
        return Path(self.AUDIO_SUMMARIES_DIR)


def _env_file_keys(env_path: Path) -> list[str]:
    """Setting names assigned in an env file, in order, upper-cased."""
    keys: list[str] = []
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        return keys
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name = line.split("=", 1)[0].strip()
        if name.startswith("export "):
            name = name[len("export ") :].strip()
        if name:
            keys.append(name.upper())
    return keys


def _warn_unknown_env_keys() -> list[str]:
    """Log any .env keys that don't match a setting. Returns them.

    Only the env *file* is checked — the surrounding process environment is full
    of unrelated variables and would drown the warning in noise.
    """
    env_path = Path(str(Settings.model_config.get("env_file", ".env")))
    known = {name.upper() for name in Settings.model_fields}
    unknown = [key for key in _env_file_keys(env_path) if key not in known]
    if unknown:
        logger.warning(
            "Ignoring %d unrecognised setting(s) in %s: %s — check for typos; "
            "these have no effect.",
            len(unknown),
            env_path,
            ", ".join(unknown),
        )
    return unknown


settings = Settings()
_warn_unknown_env_keys()
