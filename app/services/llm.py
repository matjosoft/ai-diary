import json
from datetime import date
from pathlib import Path

from openai import AsyncOpenAI

from app.config import settings
from app.models import AnalysisResult

_client = None

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
PROJECT_ROOT = Path(__file__).parent.parent.parent

SWEDISH_WEEKDAYS = ["måndag", "tisdag", "onsdag", "torsdag", "fredag", "lördag", "söndag"]


def _today_swedish() -> str:
    today = date.today()
    return f"{SWEDISH_WEEKDAYS[today.weekday()]} {today.isoformat()}"


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=settings.OPENROUTER_API_KEY,
            base_url=settings.OPENROUTER_BASE_URL,
        )
    return _client


def _load_prompt(name: str) -> str:
    prompt = (PROMPTS_DIR / name).read_text()
    person_file = PROJECT_ROOT / "person.md"
    if person_file.exists():
        person_info = person_file.read_text()
        prompt = f"{prompt}\n\n## Om dagboksförfattaren\n\n{person_info}"
    return prompt


async def analyze_entry(transcription: str) -> AnalysisResult:
    client = _get_client()
    system_prompt = _load_prompt("analyze_entry.txt")

    response = await client.chat.completions.create(
        model=settings.LLM_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": transcription},
        ],
        temperature=0.3,
    )

    content = response.choices[0].message.content
    # Strip markdown code fences if present
    if content.startswith("```"):
        content = content.split("\n", 1)[1]
        content = content.rsplit("```", 1)[0]

    data = json.loads(content)
    return AnalysisResult(**data)


async def chat_query(
    question: str,
    context: str,
    conversation_history: list[dict] | None = None,
    client_type: str = "web",
) -> str:
    client = _get_client()
    system_prompt = _load_prompt("chat_query.txt")

    # Override formatting section for non-web clients
    format_file = PROMPTS_DIR / f"chat_format_{client_type}.txt"
    if format_file.exists():
        # Replace everything from "## Formatering" onward with the client-specific version
        marker = "## Formatering"
        if marker in system_prompt:
            system_prompt = system_prompt[: system_prompt.index(marker)]
        system_prompt += format_file.read_text()

    system_prompt = system_prompt.replace("{today}", _today_swedish())

    messages = [{"role": "system", "content": system_prompt}]

    # Include conversation history for follow-up questions
    if conversation_history:
        for msg in conversation_history[-6:]:
            messages.append({"role": msg["role"], "content": msg["content"]})

    messages.append({
        "role": "user",
        "content": f"Dagboksdata:\n\n{context}\n\nFråga: {question}",
    })

    response = await client.chat.completions.create(
        model=settings.LLM_MODEL,
        messages=messages,
        temperature=0.5,
    )

    return response.choices[0].message.content


async def generate_report(entries: list[dict], report_type: str) -> str:
    client = _get_client()
    prompt_file = f"{report_type}_report.txt"
    system_prompt = _load_prompt(prompt_file)

    def _meals_line(e: dict) -> str:
        meals = e.get("meals") or {}
        if not meals:
            return ""
        return "\nMåltider: " + ", ".join(f"{k}: {v}" for k, v in meals.items())

    entries_text = "\n\n".join(
        f"## {e['date']}\n"
        f"Sammanfattning: {e.get('summary', 'Saknas')}\n"
        f"Humör: {e.get('mood', '?')} ({e.get('mood_score', '?')}/10)\n"
        f"Händelser: {', '.join(e.get('events', []))}\n"
        f"Personer: {', '.join(e.get('people', []))}\n"
        f"Ämnen: {', '.join(e.get('topics', []))}\n"
        f"Planerade åtgärder: {', '.join(e.get('planned_actions', []))}"
        f"{_meals_line(e)}"
        for e in entries
    )

    response = await client.chat.completions.create(
        model=settings.LLM_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": entries_text},
        ],
        temperature=0.5,
    )

    return response.choices[0].message.content
