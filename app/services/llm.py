import json
from pathlib import Path

from openai import AsyncOpenAI

from app.config import settings
from app.models import AnalysisResult

_client = None

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
PROJECT_ROOT = Path(__file__).parent.parent.parent


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


async def chat_query(question: str, entries: list[dict]) -> str:
    client = _get_client()
    system_prompt = _load_prompt("chat_query.txt")

    entries_text = "\n\n".join(
        f"## {e['date']}\n"
        f"Sammanfattning: {e.get('summary', 'Saknas')}\n"
        f"Humör: {e.get('mood', '?')} ({e.get('mood_score', '?')}/10)\n"
        f"Händelser: {', '.join(e.get('events', []))}\n"
        f"Personer: {', '.join(e.get('people', []))}\n"
        f"Ämnen: {', '.join(e.get('topics', []))}\n"
        f"Planerade åtgärder: {', '.join(e.get('planned_actions', []))}\n"
        f"Transkription: {e.get('transcription', '')}"
        for e in entries
    )

    response = await client.chat.completions.create(
        model=settings.LLM_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": f"Dagboksdata:\n\n{entries_text}\n\nFråga: {question}",
            },
        ],
        temperature=0.5,
    )

    return response.choices[0].message.content


async def generate_report(entries: list[dict], report_type: str) -> str:
    client = _get_client()
    prompt_file = f"{report_type}_report.txt"
    system_prompt = _load_prompt(prompt_file)

    entries_text = "\n\n".join(
        f"## {e['date']}\n"
        f"Sammanfattning: {e.get('summary', 'Saknas')}\n"
        f"Humör: {e.get('mood', '?')} ({e.get('mood_score', '?')}/10)\n"
        f"Händelser: {', '.join(e.get('events', []))}\n"
        f"Personer: {', '.join(e.get('people', []))}\n"
        f"Ämnen: {', '.join(e.get('topics', []))}\n"
        f"Planerade åtgärder: {', '.join(e.get('planned_actions', []))}"
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
