from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Transcription
    WHISPER_MODEL: str = "KBLab/kb-whisper-medium"
    TRANSCRIPTION_MODEL: str = "google/gemini-3.1-flash-lite-preview"

    # LLM / OpenRouter
    OPENROUTER_API_KEY: str = ""
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"
    LLM_MODEL: str = "anthropic/claude-sonnet-4"
    PHOTO_DESCRIPTION_MODEL: str = "google/gemini-2.0-flash-exp"

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

    # Server
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

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


settings = Settings()
