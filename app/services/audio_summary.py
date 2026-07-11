"""Generate podcast/radio-style audio summaries from diary entries.

Builds a Swedish spoken script for a chosen period (day, month, year,
year-to-date), then renders it to audio via OpenRouter TTS. Both the
script and the rendered audio are cached on disk under
`settings.audio_summaries_dir` so repeat requests are cheap.
"""

import json
from datetime import date as _date
from pathlib import Path

from app.config import settings
from app.database import get_connection
from app.models import AudioSummaryRequest
from app.services.llm import _get_client, _load_prompt
from app.services.summaries import _fetch_health_rows, _health_block, _rows_to_dicts
from app.services.tts import synthesize

SWEDISH_MONTHS = [
    "januari", "februari", "mars", "april", "maj", "juni",
    "juli", "augusti", "september", "oktober", "november", "december",
]

# Host styles -> prompt file. "default" is the original warm radio host.
AUDIO_SUMMARY_STYLES = {
    "default": "audio_summary.txt",
    "factual": "audio_summary_factual.txt",
    "roasting": "audio_summary_roasting.txt",
}
DEFAULT_STYLE = "default"


def _resolve_style(style: str | None) -> str:
    """Pick the host style: explicit request > .env default > built-in default."""
    if style:
        s = style.strip().lower()
        if s in AUDIO_SUMMARY_STYLES:
            return s
    configured = (settings.AUDIO_SUMMARY_STYLE or "").strip().lower()
    if configured in AUDIO_SUMMARY_STYLES:
        return configured
    return DEFAULT_STYLE


def _period_label(period_type: str, period_key: str) -> str:
    """Human-readable Swedish label for a period — read aloud by the host."""
    if period_type == "day":
        y, m, d = period_key.split("-")
        return f"den {int(d)} {SWEDISH_MONTHS[int(m) - 1]} {y}"
    if period_type == "month":
        y, m = period_key.split("-")
        return f"{SWEDISH_MONTHS[int(m) - 1]} {y}"
    if period_type == "year":
        return f"året {period_key}"
    if period_type == "ytd":
        today = _date.today()
        return (
            f"året {period_key} fram till den "
            f"{today.day} {SWEDISH_MONTHS[today.month - 1]}"
        )
    return period_key


def _date_range(period_type: str, period_key: str) -> tuple[str, str]:
    """Return half-open [from, to) ISO date range for the period."""
    if period_type == "day":
        d = _date.fromisoformat(period_key)
        nxt = _date.fromordinal(d.toordinal() + 1)
        return d.isoformat(), nxt.isoformat()
    if period_type == "month":
        y, m = period_key.split("-")
        y, m = int(y), int(m)
        date_from = f"{y}-{m:02d}-01"
        if m == 12:
            date_to = f"{y + 1}-01-01"
        else:
            date_to = f"{y}-{m + 1:02d}-01"
        return date_from, date_to
    if period_type == "year":
        y = int(period_key)
        return f"{y}-01-01", f"{y + 1}-01-01"
    if period_type == "ytd":
        y = int(period_key)
        today = _date.today()
        # Half-open up to (and including) today => to = today + 1
        return f"{y}-01-01", _date.fromordinal(today.toordinal() + 1).isoformat()
    raise ValueError(f"Unknown period_type: {period_type}")


def _format_entries_for_script(entries: list[dict]) -> str:
    """Compact, LLM-friendly rendering of diary entries."""
    blocks: list[str] = []
    for e in entries:
        meals = e.get("meals") or {}
        meals_str = ", ".join(f"{k}: {v}" for k, v in meals.items()) if meals else ""
        parts = [
            f"## {e['date']}",
            f"Sammanfattning: {e.get('summary') or 'Saknas'}",
            f"Humör: {e.get('mood') or '?'} ({e.get('mood_score') or '?'}/10)",
            f"Händelser: {', '.join(e.get('events') or []) or '-'}",
            f"Personer: {', '.join(e.get('people') or []) or '-'}",
            f"Ämnen: {', '.join(e.get('topics') or []) or '-'}",
            f"Planerade åtgärder: {', '.join(e.get('planned_actions') or []) or '-'}",
        ]
        if meals_str:
            parts.append(f"Måltider: {meals_str}")
        # Transcription is only included for short periods (day) — handled by caller.
        if e.get("__include_transcription") and e.get("transcription"):
            parts.append(f"Transkription: {e['transcription']}")
        blocks.append("\n".join(parts))
    return "\n\n".join(blocks)


def _paths_for(period_type: str, period_key: str, style: str) -> tuple[Path, Path]:
    """Cached script (.md) and audio file paths for this period + style.

    The default style keeps its original (style-less) filenames so existing
    caches stay valid; other styles get a `-{style}` suffix.
    """
    settings.audio_summaries_dir.mkdir(parents=True, exist_ok=True)
    name = f"{period_type}-{period_key}"
    if style != DEFAULT_STYLE:
        name += f"-{style}"
    base = settings.audio_summaries_dir / name
    ext = settings.TTS_FORMAT.lower()
    return base.with_suffix(".md"), base.with_suffix(f".{ext}")


async def generate_script(
    period_type: str,
    period_key: str,
    style: str | None = None,
    force: bool = False,
) -> dict:
    """Build the spoken script for a period. Returns dict with script + meta.

    `style` selects the host persona ("default" | "factual" | "roasting");
    None falls back to the .env default. Cached to disk per style; pass
    force=True to regenerate.
    """
    style = _resolve_style(style)
    script_path, _ = _paths_for(period_type, period_key, style)

    date_from, date_to = _date_range(period_type, period_key)

    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM entries WHERE date >= ? AND date < ? ORDER BY date",
            (date_from, date_to),
        ).fetchall()

    if not rows:
        return {
            "period_type": period_type,
            "period_key": period_key,
            "style": style,
            "label": _period_label(period_type, period_key),
            "script": None,
            "entry_count": 0,
        }

    if not force and script_path.exists():
        return {
            "period_type": period_type,
            "period_key": period_key,
            "style": style,
            "label": _period_label(period_type, period_key),
            "script": script_path.read_text(),
            "entry_count": len(rows),
            "cached": True,
        }

    entries = _rows_to_dicts(rows)

    # For a single day we include the raw transcription so the host can
    # quote details verbatim; for longer periods this would blow the context.
    if period_type == "day":
        for e in entries:
            e["__include_transcription"] = True

    entries_text = _format_entries_for_script(entries)

    health_rows = _fetch_health_rows(date_from, date_to)
    total_days = (
        _date.fromisoformat(date_to) - _date.fromisoformat(date_from)
    ).days
    health_text = _health_block(health_rows, total_days=total_days)

    label = _period_label(period_type, period_key)
    user_msg_parts = [
        f"Period: {label}",
        f"Antal dagboksanteckningar: {len(entries)}",
        "",
        "## Dagboksanteckningar",
        entries_text,
    ]
    if health_text:
        user_msg_parts.extend(["", health_text])
    user_msg = "\n".join(user_msg_parts)

    system_prompt = _load_prompt(AUDIO_SUMMARY_STYLES[style])

    client = _get_client()
    response = await client.chat.completions.create(
        model=settings.podcast_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.6,
    )
    script = response.choices[0].message.content.strip()

    script_path.write_text(script)

    return {
        "period_type": period_type,
        "period_key": period_key,
        "style": style,
        "label": label,
        "script": script,
        "entry_count": len(entries),
        "cached": False,
    }


async def generate_audio_summary(
    period_type: str,
    period_key: str,
    style: str | None = None,
    force: bool = False,
) -> dict:
    """Produce both script and audio for the period. Returns paths + meta."""
    style = _resolve_style(style)
    script_path, audio_path = _paths_for(period_type, period_key, style)

    script_result = await generate_script(
        period_type, period_key, style=style, force=force
    )
    if script_result["script"] is None:
        return script_result | {"audio_path": None}

    if force or not audio_path.exists():
        audio_bytes = await synthesize(script_result["script"])
        audio_path.write_bytes(audio_bytes)

    return script_result | {
        "audio_path": str(audio_path),
        "script_path": str(script_path),
    }


async def detect_audio_summary_request(
    question: str,
    conversation_history: list[dict] | None = None,
) -> AudioSummaryRequest:
    """LLM-detect whether the user asked for a podcast/radio-style summary."""
    client = _get_client()
    prompt = _load_prompt("audio_summary_detect.txt").replace(
        "{today}", _date.today().isoformat()
    )

    messages = [{"role": "system", "content": prompt}]
    if conversation_history:
        prior = [m["content"] for m in conversation_history[-4:] if m["role"] == "user"]
        if prior:
            messages.append({
                "role": "user",
                "content": "Tidigare frågor:\n" + "\n".join(f"- {q}" for q in prior),
            })
            messages.append({"role": "assistant", "content": "Förstått."})
    messages.append({"role": "user", "content": question})

    response = await client.chat.completions.create(
        model=settings.LLM_MODEL,
        messages=messages,
        temperature=0.0,
    )

    content = response.choices[0].message.content
    if content.startswith("```"):
        content = content.split("\n", 1)[1]
        content = content.rsplit("```", 1)[0]

    try:
        data = json.loads(content)
        return AudioSummaryRequest(**data)
    except (json.JSONDecodeError, Exception):
        return AudioSummaryRequest(is_audio_summary=False)
