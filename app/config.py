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

    # Storage
    DATABASE_PATH: str = "./diary.db"
    AUDIO_DIR: str = "./audio"
    REPORTS_DIR: str = "./reports"

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
    def reports_dir(self) -> Path:
        return Path(self.REPORTS_DIR)


settings = Settings()
