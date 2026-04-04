import json
from pathlib import Path

from app.config import settings
from app.database import get_connection
from app.services.llm import generate_report


async def generate_monthly_report(year: int, month: int) -> str:
    report_path = settings.reports_dir / f"{year}-{month:02d}.md"

    # Return cached report if it exists
    if report_path.exists():
        return report_path.read_text()

    # Fetch entries for the month
    from_date = f"{year}-{month:02d}-01"
    if month == 12:
        to_date = f"{year + 1}-01-01"
    else:
        to_date = f"{year}-{month + 1:02d}-01"

    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM entries WHERE date >= ? AND date < ? ORDER BY date",
            (from_date, to_date),
        ).fetchall()

    if not rows:
        return "Inga dagboksanteckningar för denna period."

    entries = _rows_to_dicts(rows)
    report = await generate_report(entries, "monthly")

    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report)

    return report


async def generate_yearly_report(year: int) -> str:
    report_path = settings.reports_dir / f"{year}.md"

    if report_path.exists():
        return report_path.read_text()

    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM entries WHERE date >= ? AND date < ? ORDER BY date",
            (f"{year}-01-01", f"{year + 1}-01-01"),
        ).fetchall()

    if not rows:
        return "Inga dagboksanteckningar för denna period."

    entries = _rows_to_dicts(rows)
    report = await generate_report(entries, "yearly")

    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report)

    return report


def _rows_to_dicts(rows) -> list[dict]:
    entries = [dict(row) for row in rows]
    for entry in entries:
        for field in ("audio_files", "events", "people", "planned_actions", "topics"):
            entry[field] = json.loads(entry[field])
    return entries
