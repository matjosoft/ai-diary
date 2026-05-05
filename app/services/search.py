"""Smart hierarchical search for diary entries.

Two-step flow:
1. Analyze the user's question to determine intent, date range, and search terms.
2. Retrieve the minimal context needed: summaries for broad questions,
   full transcriptions only when detail is required.
"""

import json
from datetime import date

from app.database import get_connection
from app.models import QueryIntent
from app.services.llm import _get_client, _load_prompt
from app.config import settings


async def analyze_query(question: str, conversation_history: list[dict] | None = None) -> QueryIntent:
    """Use LLM to parse a natural-language question into a structured QueryIntent."""
    client = _get_client()
    prompt_template = _load_prompt("query_analysis.txt")
    system_prompt = prompt_template.replace("{today}", date.today().isoformat())

    messages = [{"role": "system", "content": system_prompt}]

    # Only include prior user questions for context (not the long assistant
    # answers, which confuse the analysis model into generating creative text)
    if conversation_history:
        prior_questions = [
            m["content"] for m in conversation_history[-6:]
            if m["role"] == "user"
        ]
        if prior_questions:
            context = "Tidigare frågor i konversationen:\n" + "\n".join(
                f"- {q}" for q in prior_questions[-3:]
            )
            messages.append({"role": "user", "content": context})
            messages.append({"role": "assistant", "content": "Förstått. Jag väntar på nästa fråga att analysera."})

    messages.append({"role": "user", "content": question})

    response = await client.chat.completions.create(
        model=settings.LLM_MODEL,
        messages=messages,
        temperature=0.1,
    )

    content = response.choices[0].message.content
    if content.startswith("```"):
        content = content.split("\n", 1)[1]
        content = content.rsplit("```", 1)[0]

    try:
        data = json.loads(content)
        return QueryIntent(**data)
    except (json.JSONDecodeError, Exception):
        # Fallback: broad search with the question as search term
        return QueryIntent(
            time_scope="all",
            search_terms=question.split()[:5],
            search_fields=["summary", "topics"],
            needs_full_text=False,
            question_type="summary",
        )


def _fts_search(search_terms: list[str], date_from: str | None, date_to: str | None) -> list[int]:
    """Run FTS5 search and return matching entry IDs."""
    if not search_terms:
        return []

    # Build FTS match expression: OR between terms for broad matching
    match_expr = " OR ".join(f'"{term}"' for term in search_terms)

    query = (
        "SELECT e.id FROM entries e "
        "JOIN entries_fts f ON e.id = f.rowid "
        "WHERE entries_fts MATCH ? "
    )
    params: list = [match_expr]

    if date_from:
        query += "AND e.date >= ? "
        params.append(date_from)
    if date_to:
        query += "AND e.date <= ? "
        params.append(date_to)

    query += "ORDER BY rank LIMIT 50"

    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
    return [row[0] for row in rows]


def _fetch_entries(
    entry_ids: list[int] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Fetch entries by IDs or date range."""
    with get_connection() as conn:
        if entry_ids:
            placeholders = ",".join("?" * len(entry_ids))
            rows = conn.execute(
                f"SELECT * FROM entries WHERE id IN ({placeholders}) ORDER BY date",
                entry_ids,
            ).fetchall()
        elif date_from or date_to:
            conditions = []
            params: list = []
            if date_from:
                conditions.append("date >= ?")
                params.append(date_from)
            if date_to:
                conditions.append("date <= ?")
                params.append(date_to)
            where = " AND ".join(conditions)
            rows = conn.execute(
                f"SELECT * FROM entries WHERE {where} ORDER BY date LIMIT ?",
                params + [limit],
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM entries ORDER BY date DESC LIMIT ?", (limit,)
            ).fetchall()

    entries = [dict(row) for row in rows]
    for entry in entries:
        for field in ("audio_files", "events", "people", "planned_actions", "topics"):
            entry[field] = json.loads(entry[field])
        entry["meals"] = json.loads(entry["meals"])
    return entries


def _fetch_summaries(period_type: str, date_from: str | None, date_to: str | None) -> list[dict]:
    """Fetch pre-computed summaries for a period type within a date range."""
    query = "SELECT * FROM summaries WHERE period_type = ?"
    params: list = [period_type]

    if date_from:
        query += " AND period_key >= ?"
        # Convert date to period key: '2026-03-01' -> '2026-03' for monthly
        if period_type == "monthly":
            params.append(date_from[:7])
        else:
            params.append(date_from[:4])
    if date_to:
        query += " AND period_key <= ?"
        if period_type == "monthly":
            params.append(date_to[:7])
        else:
            params.append(date_to[:4])

    query += " ORDER BY period_key"

    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()

    summaries = [dict(row) for row in rows]
    for s in summaries:
        for field in ("topics", "people"):
            s[field] = json.loads(s[field])
    return summaries


async def smart_retrieve(intent: QueryIntent) -> str:
    """Build LLM context using the coarsest grain that answers the question.

    Returns a formatted text block ready to inject into the chat prompt.
    """
    sections: list[str] = []

    if intent.question_type == "trend":
        # Broad pattern question -> prefer monthly/yearly summaries
        summaries = _fetch_summaries("monthly", intent.date_from, intent.date_to)
        if summaries:
            sections.append("## Månadssammanfattningar\n")
            for s in summaries:
                sections.append(
                    f"### {s['period_key']}\n"
                    f"Sammanfattning: {s['summary']}\n"
                    f"Snitthumör: {s.get('mood_avg', '?')}/10\n"
                    f"Antal anteckningar: {s.get('entry_count', '?')}\n"
                    f"Ämnen: {', '.join(s.get('topics', []))}\n"
                    f"Personer: {', '.join(s.get('people', []))}\n"
                )

        # Also include daily summaries (without transcriptions) for granularity
        entries = _fetch_entries(
            date_from=intent.date_from, date_to=intent.date_to
        )
        if entries:
            sections.append("## Dagliga sammanfattningar\n")
            for e in entries:
                sections.append(
                    f"**{e['date']}** — Humör: {e.get('mood', '?')} ({e.get('mood_score', '?')}/10)\n"
                    f"{e.get('summary', 'Ingen sammanfattning')}\n"
                )

    elif intent.question_type == "summary":
        # Period overview -> monthly summary + daily summaries
        summaries = _fetch_summaries("monthly", intent.date_from, intent.date_to)
        if summaries:
            sections.append("## Månadssammanfattningar\n")
            for s in summaries:
                sections.append(
                    f"### {s['period_key']}\n{s['summary']}\n"
                    f"Ämnen: {', '.join(s.get('topics', []))}\n"
                    f"Personer: {', '.join(s.get('people', []))}\n"
                )

        entries = _fetch_entries(
            date_from=intent.date_from, date_to=intent.date_to
        )
        if entries:
            sections.append("## Dagliga anteckningar\n")
            for e in entries:
                sections.append(_format_entry(e, include_transcription=False))

    elif intent.question_type == "lookup":
        # Find specific data -> metadata search + FTS
        fts_ids = _fts_search(intent.search_terms, intent.date_from, intent.date_to)
        if fts_ids:
            entries = _fetch_entries(entry_ids=fts_ids)
        else:
            entries = _fetch_entries(
                date_from=intent.date_from, date_to=intent.date_to
            )
        if entries:
            sections.append("## Matchande anteckningar\n")
            for e in entries:
                sections.append(_format_entry(e, include_transcription=False))

    elif intent.question_type == "detail":
        # Needs exact content -> FTS to narrow, then full transcription
        fts_ids = _fts_search(intent.search_terms, intent.date_from, intent.date_to)
        if fts_ids:
            entries = _fetch_entries(entry_ids=fts_ids)
        else:
            entries = _fetch_entries(
                date_from=intent.date_from, date_to=intent.date_to, limit=30
            )
        if entries:
            sections.append("## Dagboksanteckningar (fullständiga)\n")
            for e in entries:
                sections.append(_format_entry(e, include_transcription=True))

    if not sections:
        return "Inga dagboksanteckningar hittades för den angivna perioden."

    return "\n".join(sections)


def _format_entry(entry: dict, include_transcription: bool) -> str:
    """Format a single entry for LLM context."""
    parts = [
        f"### {entry['date']}",
        f"Sammanfattning: {entry.get('summary', 'Saknas')}",
        f"Humör: {entry.get('mood', '?')} ({entry.get('mood_score', '?')}/10)",
        f"Händelser: {', '.join(entry.get('events', []))}",
        f"Personer: {', '.join(entry.get('people', []))}",
        f"Ämnen: {', '.join(entry.get('topics', []))}",
        f"Planerade åtgärder: {', '.join(entry.get('planned_actions', []))}",
    ]
    meals = entry.get("meals") or {}
    if meals:
        meals_str = ", ".join(f"{k}: {v}" for k, v in meals.items())
        parts.append(f"Måltider: {meals_str}")
    if include_transcription:
        parts.append(f"Transkription: {entry.get('transcription', '')}")
    return "\n".join(parts) + "\n"
