import asyncio
import json
import traceback
from datetime import date, datetime
from pathlib import Path

from app.database import get_connection
from app.services.llm import analyze_entry
from app.services.summaries import refresh_summaries_for_date
from app.services.transcription import transcribe


def _date_from_filename(audio_path: Path) -> str:
    """Extract date from filename like 20260404_155958.m4a, fall back to today."""
    stem = audio_path.stem  # e.g. "20260404_155958"
    try:
        dt = datetime.strptime(stem[:8], "%Y%m%d")
        return dt.date().isoformat()
    except (ValueError, IndexError):
        return date.today().isoformat()


def process_audio(audio_path: Path):
    """Background task: transcribe audio, analyze with LLM, store in DB."""
    try:
        _process_audio_sync(audio_path)
    except Exception:
        traceback.print_exc()


def _process_audio_sync(audio_path: Path):
    print(f"Transcribing {audio_path.name}...")
    transcription = transcribe(audio_path)
    print(f"Transcription complete ({len(transcription)} chars)")

    entry_date = _date_from_filename(audio_path)

    with get_connection() as conn:
        existing = conn.execute(
            "SELECT * FROM entries WHERE date = ?", (entry_date,)
        ).fetchone()

        if existing:
            # Append transcription and audio file to existing entry
            old_transcription = existing["transcription"]
            combined = f"{old_transcription}\n\n{transcription}"

            old_files = json.loads(existing["audio_files"])
            old_files.append(audio_path.name)

            # Run LLM analysis on combined text
            analysis = asyncio.run(analyze_entry(combined))

            conn.execute(
                """UPDATE entries SET
                    transcription = ?,
                    audio_files = ?,
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
                    combined,
                    json.dumps(old_files),
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
            print(f"Updated entry for {entry_date}")
            asyncio.run(refresh_summaries_for_date(entry_date))
        else:
            # Create new entry
            analysis = asyncio.run(analyze_entry(transcription))

            conn.execute(
                """INSERT INTO entries
                    (date, audio_files, transcription, summary, mood, mood_score,
                     events, people, planned_actions, topics, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    entry_date,
                    json.dumps([audio_path.name]),
                    transcription,
                    analysis.summary,
                    analysis.mood,
                    analysis.mood_score,
                    json.dumps(analysis.events),
                    json.dumps(analysis.people),
                    json.dumps(analysis.planned_actions),
                    json.dumps(analysis.topics),
                    datetime.now().isoformat(),
                    datetime.now().isoformat(),
                ),
            )
            print(f"Created entry for {entry_date}")
            asyncio.run(refresh_summaries_for_date(entry_date))
