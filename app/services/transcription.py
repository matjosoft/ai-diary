import base64
import json
from pathlib import Path

from openai import OpenAI

from app.config import settings

PROMPT = (
    "Transkribera detta ljudklipp ordagrant på svenska. "
    "Returnera BARA transkriptionen, ingen annan text."
)

_client = None
_client_key: tuple[str, str, float] | None = None


def _get_client() -> OpenAI:
    """OpenAI client aimed at the transcription service.

    Cached, but rebuilt if the settings change (tests, reloaded config).
    """
    global _client, _client_key
    key = (
        settings.transcription_base_url,
        settings.transcription_api_key,
        settings.TRANSCRIPTION_TIMEOUT,
    )
    if _client is None or _client_key != key:
        _client = OpenAI(
            api_key=key[1],
            base_url=key[0],
            timeout=key[2],
        )
        _client_key = key
    return _client


def _transcribe_audio_api(client: OpenAI, audio_path: Path) -> str:
    """OpenAI-style POST {base_url}/audio/transcriptions (multipart upload).

    What Speaches/faster-whisper-server, whisper.cpp, LocalAI, LM Studio,
    Groq and OpenAI all speak.
    """
    kwargs = {}
    if settings.TRANSCRIPTION_LANGUAGE.strip():
        kwargs["language"] = settings.TRANSCRIPTION_LANGUAGE.strip()

    with audio_path.open("rb") as f:
        response = client.audio.transcriptions.create(
            model=settings.TRANSCRIPTION_MODEL,
            file=(audio_path.name, f, "application/octet-stream"),
            response_format="text",
            **kwargs,
        )

    return _extract_text(response)


def _extract_text(response) -> str:
    """Pull the transcript out of whatever the service sent back.

    response_format="text" normally yields a plain string, but servers that
    ignore the parameter answer with the JSON object instead — and the SDK
    then hands us its raw body as a string.
    """
    if not isinstance(response, str):
        return (getattr(response, "text", "") or "").strip()

    text = response.strip()
    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return text
        if isinstance(payload, dict) and isinstance(payload.get("text"), str):
            return payload["text"].strip()
    return text


def _transcribe_chat_api(client: OpenAI, audio_path: Path) -> str:
    """Chat completions with an input_audio part (OpenRouter audio models)."""
    audio_data = base64.b64encode(audio_path.read_bytes()).decode("utf-8")
    suffix = audio_path.suffix.lstrip(".")  # e.g. "m4a"

    response = client.chat.completions.create(
        model=settings.TRANSCRIPTION_MODEL,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {
                        "type": "input_audio",
                        "input_audio": {
                            "data": audio_data,
                            "format": suffix,
                        },
                    },
                ],
            }
        ],
    )

    return response.choices[0].message.content.strip()


def transcribe(audio_path: Path) -> str:
    client = _get_client()
    if settings.transcription_api == "audio":
        return _transcribe_audio_api(client, audio_path)
    return _transcribe_chat_api(client, audio_path)
