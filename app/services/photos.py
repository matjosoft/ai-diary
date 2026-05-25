import base64
import logging
import mimetypes
import re
from datetime import datetime
from pathlib import Path

from app.config import settings
from app.database import get_connection
from app.services.llm import _get_client

logger = logging.getLogger(__name__)

_ISO_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")

_DESCRIPTION_PROMPT = (
    "Beskriv vad som syns på bilden på svenska. Var konkret och saklig: "
    "personer, plats, föremål, aktivitet, stämning, text om det finns. "
    "2–4 meningar. Skriv ingen inledning."
)


def _encode_image(path: Path) -> str:
    mime, _ = mimetypes.guess_type(path.name)
    if not mime:
        mime = "image/jpeg"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


async def describe_image(path: Path, caption: str | None = None) -> str:
    """Generate a Swedish description for an image using the vision model."""
    client = _get_client()
    data_url = _encode_image(path)

    user_content: list[dict] = [
        {"type": "image_url", "image_url": {"url": data_url}},
    ]
    text = _DESCRIPTION_PROMPT
    if caption and caption.strip():
        text += f"\n\nBildtext från användaren: {caption.strip()}"
    user_content.append({"type": "text", "text": text})

    response = await client.chat.completions.create(
        model=settings.PHOTO_DESCRIPTION_MODEL,
        messages=[{"role": "user", "content": user_content}],
        temperature=0.3,
    )
    return (response.choices[0].message.content or "").strip()


def save_photo_bytes(data: bytes, suffix: str = "jpg") -> Path:
    """Persist incoming photo bytes to the photos dir with a timestamped name."""
    settings.photos_dir.mkdir(parents=True, exist_ok=True)
    name = f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.{suffix.lstrip('.')}"
    path = settings.photos_dir / name
    path.write_bytes(data)
    return path


def fetch_photos_for_dates(dates: list[str]) -> dict[str, list[dict]]:
    """Return photos grouped by date for the requested dates."""
    if not dates:
        return {}
    placeholders = ",".join("?" * len(dates))
    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT id, date, filename, description, caption, created_at "
            f"FROM photos WHERE date IN ({placeholders}) ORDER BY date, id",
            dates,
        ).fetchall()
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        d = dict(row)
        grouped.setdefault(d["date"], []).append(d)
    return grouped


def store_photo(
    entry_date: str, filename: str, description: str, caption: str | None
) -> int:
    """Insert a photo row and return its id."""
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO photos (date, filename, description, caption) VALUES (?, ?, ?, ?)",
            (entry_date, filename, description, caption),
        )
        return cur.lastrowid


def photos_for_answer(answer: str, limit: int = 10) -> list[dict]:
    """Find photos for any ISO dates mentioned in the answer.

    Returns a flat, ordered list of photo dicts (date, filename, description,
    caption), capped at `limit`.
    """
    dates = sorted(set(_ISO_DATE_RE.findall(answer or "")))
    if not dates:
        return []
    grouped = fetch_photos_for_dates(dates)
    out: list[dict] = []
    for d in dates:
        for p in grouped.get(d, []):
            if len(out) >= limit:
                return out
            out.append(p)
    return out


async def process_photo(
    path: Path, entry_date: str, caption: str | None = None
) -> tuple[int, str]:
    """Describe the image and store the row. Returns (photo_id, description)."""
    try:
        description = await describe_image(path, caption)
    except Exception:
        logger.exception("Image description failed for %s", path.name)
        description = ""
    photo_id = store_photo(entry_date, path.name, description, caption)
    return photo_id, description
