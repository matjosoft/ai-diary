import base64
from pathlib import Path

from openai import OpenAI

from app.config import settings

_client = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            api_key=settings.OPENROUTER_API_KEY,
            base_url=settings.OPENROUTER_BASE_URL,
        )
    return _client


def transcribe(audio_path: Path) -> str:
    client = _get_client()

    audio_data = base64.b64encode(audio_path.read_bytes()).decode("utf-8")
    suffix = audio_path.suffix.lstrip(".")  # e.g. "m4a"

    response = client.chat.completions.create(
        model=settings.TRANSCRIPTION_MODEL,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Transkribera detta ljudklipp ordagrant på svenska. Returnera BARA transkriptionen, ingen annan text.",
                    },
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
