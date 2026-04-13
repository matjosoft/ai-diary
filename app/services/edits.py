"""Detect and apply edit commands on diary entries via chat.

Flow:
1. `detect_edit` asks the LLM whether the user's question is an edit command
   and, if so, extracts target field, date range and old/new text.
2. `apply_edit` runs a SQL REPLACE() over matching rows. FTS stays in sync
   through the existing update triggers on the `entries` table.
"""

import json
from datetime import date, datetime

from app.config import settings
from app.database import get_connection
from app.models import EditCommand
from app.services.llm import _get_client, _load_prompt, analyze_entry
from app.services.summaries import refresh_summaries_for_date


async def detect_edit(
    question: str,
    conversation_history: list[dict] | None = None,
) -> EditCommand:
    client = _get_client()
    prompt_template = _load_prompt("edit_detection.txt")
    system_prompt = prompt_template.replace("{today}", date.today().isoformat())

    messages = [{"role": "system", "content": system_prompt}]
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
        return EditCommand(**data)
    except (json.JSONDecodeError, Exception):
        return EditCommand(is_edit=False)


def apply_edit(cmd: EditCommand) -> tuple[int, list[str]]:
    """Apply the edit to entries (and daily summary) matching the date range.

    Returns (rows_changed, affected_dates).
    """
    if not (cmd.is_edit and cmd.old_text and cmd.new_text and cmd.target):
        return 0, []

    fields: list[str] = []
    if cmd.target in ("transcription", "both"):
        fields.append("transcription")
    if cmd.target in ("summary", "both"):
        fields.append("summary")
    if not fields:
        return 0, []

    where_parts = []
    params: list = []
    # Only rows that actually contain the old text in any targeted field
    contains_clause = " OR ".join(f"{f} LIKE ?" for f in fields)
    where_parts.append(f"({contains_clause})")
    params.extend([f"%{cmd.old_text}%"] * len(fields))

    if cmd.date_from:
        where_parts.append("date >= ?")
        params.append(cmd.date_from)
    if cmd.date_to:
        where_parts.append("date <= ?")
        params.append(cmd.date_to)

    where = " AND ".join(where_parts)
    set_clause = ", ".join(f"{f} = REPLACE({f}, ?, ?)" for f in fields)
    update_params = []
    for _ in fields:
        update_params.extend([cmd.old_text, cmd.new_text])

    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT date FROM entries WHERE {where} ORDER BY date",
            params,
        ).fetchall()
        affected_dates = [r[0] for r in rows]
        if not affected_dates:
            return 0, []

        conn.execute(
            f"UPDATE entries SET {set_clause}, updated_at = datetime('now') WHERE {where}",
            update_params + params,
        )

    return len(affected_dates), affected_dates


async def reanalyze_affected_entries(dates: list[str]):
    """Re-run LLM analysis on affected entries to refresh derived fields."""
    for entry_date in dates:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT transcription FROM entries WHERE date = ?", (entry_date,)
            ).fetchone()
        if not row:
            continue

        analysis = await analyze_entry(row["transcription"])

        with get_connection() as conn:
            conn.execute(
                """UPDATE entries SET
                    summary = ?,
                    mood = ?,
                    mood_score = ?,
                    events = ?,
                    people = ?,
                    planned_actions = ?,
                    topics = ?,
                    updated_at = ?
                WHERE date = ?""",
                (
                    analysis.summary,
                    analysis.mood,
                    analysis.mood_score,
                    json.dumps(analysis.events),
                    json.dumps(analysis.people),
                    json.dumps(analysis.planned_actions),
                    json.dumps(analysis.topics),
                    datetime.now().isoformat(),
                    entry_date,
                ),
            )

        await refresh_summaries_for_date(entry_date)


def format_edit_confirmation(cmd: EditCommand, count: int, dates: list[str]) -> str:
    if count == 0:
        return (
            f"Hittade inga anteckningar som innehåller \"{cmd.old_text}\" "
            f"i det valda intervallet. Ingen ändring gjord."
        )
    target_label = {
        "transcription": "transkribering",
        "summary": "sammanfattning",
        "both": "transkribering och sammanfattning",
    }.get(cmd.target or "", cmd.target or "")
    preview = ", ".join(dates[:5]) + ("…" if len(dates) > 5 else "")
    return (
        f"Ändrade \"{cmd.old_text}\" till \"{cmd.new_text}\" i {target_label} "
        f"för {count} anteckning(ar): {preview}."
    )
