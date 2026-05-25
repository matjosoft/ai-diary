"""Detect and apply edit commands on diary entries via chat.

Flow:
1. `detect_edit` asks the LLM whether the user's question is an edit command
   and, if so, extracts mode (replace/append), target field, date range and
   the relevant text payload.
2. `apply_edit` dispatches to `_apply_replace` (SQL REPLACE over matching rows)
   or `_apply_append` (concatenate new text on a specific day, creating the
   entry if it doesn't yet exist). FTS stays in sync through the existing
   insert/update triggers on the `entries` table.
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
    """Apply the edit to entries matching the command.

    Returns (rows_changed, affected_dates).
    """
    if not cmd.is_edit:
        return 0, []
    if cmd.mode == "append":
        return _apply_append(cmd)
    return _apply_replace(cmd)


def _apply_replace(cmd: EditCommand) -> tuple[int, list[str]]:
    if not (cmd.old_text and cmd.new_text and cmd.target):
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


def _apply_append(cmd: EditCommand) -> tuple[int, list[str]]:
    """Append text to a single day's entry. Create the entry if missing."""
    if not cmd.append_text:
        return 0, []

    target_date = cmd.date_from or cmd.date_to or date.today().isoformat()
    now = datetime.now().isoformat()

    with get_connection() as conn:
        row = conn.execute(
            "SELECT transcription FROM entries WHERE date = ?", (target_date,)
        ).fetchone()
        if row:
            existing = row["transcription"] or ""
            combined = f"{existing}\n{cmd.append_text}" if existing else cmd.append_text
            conn.execute(
                "UPDATE entries SET transcription = ?, updated_at = ? WHERE date = ?",
                (combined, now, target_date),
            )
        else:
            conn.execute(
                """INSERT INTO entries
                    (date, audio_files, transcription, created_at, updated_at)
                VALUES (?, '[]', ?, ?, ?)""",
                (target_date, cmd.append_text, now, now),
            )

    return 1, [target_date]


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
                    meals = ?,
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
                    json.dumps(analysis.meals, ensure_ascii=False),
                    datetime.now().isoformat(),
                    entry_date,
                ),
            )

        await refresh_summaries_for_date(entry_date)


def format_edit_confirmation(cmd: EditCommand, count: int, dates: list[str]) -> str:
    if cmd.mode == "append":
        if count == 0:
            return "Kunde inte lägga till — inget datum eller text att lägga till."
        target_date = dates[0]
        return f"La till \"{cmd.append_text}\" i anteckningen för {target_date}."

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
