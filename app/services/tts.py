"""Text-to-speech via OpenRouter's /audio/speech endpoint.

Docs: https://openrouter.ai/docs/guides/overview/multimodal/tts
Endpoint follows the OpenAI Audio Speech API spec — raw audio bytes back,
not JSON.
"""

import httpx

from app.config import settings

# OpenRouter caps individual TTS requests; chunk longer scripts and concat.
# 3500 chars stays well under provider limits and keeps each request snappy.
_MAX_CHARS_PER_REQUEST = 3500


def _chunk_text(text: str, max_chars: int = _MAX_CHARS_PER_REQUEST) -> list[str]:
    """Split on paragraph / sentence boundaries so chunks don't cut mid-word."""
    text = text.strip()
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_chars:
        window = remaining[:max_chars]
        # Prefer paragraph break, then sentence end, then space.
        split_at = window.rfind("\n\n")
        if split_at < max_chars // 2:
            split_at = max(window.rfind(". "), window.rfind("! "), window.rfind("? "))
            if split_at > 0:
                split_at += 1  # keep the punctuation in this chunk
        if split_at < max_chars // 2:
            split_at = window.rfind(" ")
        if split_at <= 0:
            split_at = max_chars
        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


async def synthesize(text: str) -> bytes:
    """Render `text` to audio bytes (MP3 or PCM per TTS_FORMAT).

    For MP3 the chunked bytes can be concatenated directly — each chunk is
    a self-contained MP3 frame stream. For PCM the same applies as long as
    sample rate matches across chunks (it does, single model).
    """
    chunks = _chunk_text(text)

    url = f"{settings.OPENROUTER_BASE_URL.rstrip('/')}/audio/speech"
    headers = {
        "Authorization": f"Bearer {settings.OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }

    audio = bytearray()
    async with httpx.AsyncClient(timeout=120) as client:
        for chunk in chunks:
            payload = {
                "model": settings.TTS_MODEL,
                "input": chunk,
                "voice": settings.TTS_VOICE,
                "response_format": settings.TTS_FORMAT,
            }
            if settings.TTS_SPEED and settings.TTS_SPEED != 1.0:
                payload["speed"] = settings.TTS_SPEED

            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            audio.extend(resp.content)

    return bytes(audio)


def media_type() -> str:
    fmt = settings.TTS_FORMAT.lower()
    return "audio/mpeg" if fmt == "mp3" else "audio/pcm"
