"""Generate and store condensed summaries for chat context.

These are shorter than the full markdown reports — optimized for fitting
into LLM context windows during chat queries.
"""

import json

from pathlib import Path

from app.config import settings
from app.database import get_connection
from app.services.llm import _get_client, PROJECT_ROOT

SUMMARY_PROMPT = """\
Du får dagboksanteckningar för en period. Skriv en koncis sammanfattning \
(max 200 ord) som fångar de viktigaste händelserna, humörtrender, \
personer och ämnen. Sammanfattningen ska vara tillräckligt informativ \
för att besvara övergripande frågor om perioden utan att behöva läsa \
enskilda dagboksanteckningar.

Returnera ett JSON-objekt:
{
  "summary": "sammanfattning här",
  "topics": ["ämne1", "ämne2"],
  "people": ["person1", "person2"]
}

Returnera BARA JSON, ingen annan text.
"""


def _rows_to_dicts(rows) -> list[dict]:
    entries = [dict(row) for row in rows]
    for entry in entries:
        for field in ("audio_files", "events", "people", "planned_actions", "topics"):
            entry[field] = json.loads(entry[field])
    return entries


async def generate_period_summary(period_type: str, period_key: str, force: bool = False) -> dict | None:
    """Generate a condensed summary for a month or year and store it.

    Args:
        period_type: 'monthly' or 'yearly'
        period_key: '2026-03' for monthly, '2026' for yearly
        force: regenerate even if summary exists
    """
    # Check for existing summary
    if not force:
        with get_connection() as conn:
            existing = conn.execute(
                "SELECT * FROM summaries WHERE period_type = ? AND period_key = ?",
                (period_type, period_key),
            ).fetchone()
            if existing:
                return dict(existing)

    # Determine date range
    if period_type == "monthly":
        year, month = period_key.split("-")
        year, month = int(year), int(month)
        date_from = f"{year}-{month:02d}-01"
        if month == 12:
            date_to = f"{year + 1}-01-01"
        else:
            date_to = f"{year}-{month + 1:02d}-01"
    else:  # yearly
        year = int(period_key)
        date_from = f"{year}-01-01"
        date_to = f"{year + 1}-01-01"

    # Fetch entries
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM entries WHERE date >= ? AND date < ? ORDER BY date",
            (date_from, date_to),
        ).fetchall()

    if not rows:
        return None

    entries = _rows_to_dicts(rows)

    # Build compact input for the LLM (daily summaries + metadata only)
    entries_text = "\n".join(
        f"- {e['date']}: {e.get('summary', 'Saknas')} "
        f"Humör: {e.get('mood', '?')} ({e.get('mood_score', '?')}/10) "
        f"Ämnen: {', '.join(e.get('topics', []))} "
        f"Personer: {', '.join(e.get('people', []))}"
        for e in entries
    )

    # Load person context
    person_info = ""
    person_file = PROJECT_ROOT / "person.md"
    if person_file.exists():
        person_info = f"\n\n## Om dagboksförfattaren\n\n{person_file.read_text()}"

    client = _get_client()
    response = await client.chat.completions.create(
        model=settings.LLM_MODEL,
        messages=[
            {"role": "system", "content": SUMMARY_PROMPT + person_info},
            {"role": "user", "content": f"Period: {period_key}\n\n{entries_text}"},
        ],
        temperature=0.3,
    )

    content = response.choices[0].message.content
    if content.startswith("```"):
        content = content.split("\n", 1)[1]
        content = content.rsplit("```", 1)[0]

    data = json.loads(content)

    # Compute mood average
    scores = [e["mood_score"] for e in entries if e.get("mood_score")]
    mood_avg = round(sum(scores) / len(scores), 1) if scores else None

    # Upsert into summaries table
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO summaries (period_type, period_key, summary, topics, people, mood_avg, entry_count, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(period_key) DO UPDATE SET "
            "summary=excluded.summary, topics=excluded.topics, people=excluded.people, "
            "mood_avg=excluded.mood_avg, entry_count=excluded.entry_count, updated_at=datetime('now')",
            (
                period_type,
                period_key,
                data["summary"],
                json.dumps(data.get("topics", []), ensure_ascii=False),
                json.dumps(data.get("people", []), ensure_ascii=False),
                mood_avg,
                len(entries),
            ),
        )

    return {
        "period_type": period_type,
        "period_key": period_key,
        "summary": data["summary"],
        "topics": data.get("topics", []),
        "people": data.get("people", []),
        "mood_avg": mood_avg,
        "entry_count": len(entries),
    }


async def refresh_summaries_for_date(entry_date: str):
    """Regenerate monthly and yearly summaries that include the given date."""
    month_key = entry_date[:7]  # '2026-03'
    year_key = entry_date[:4]   # '2026'

    await generate_period_summary("monthly", month_key, force=True)
    await generate_period_summary("yearly", year_key, force=True)
