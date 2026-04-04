import json

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.database import get_connection
from app.models import ChatRequest, ChatResponse
from app.services.llm import chat_query

router = APIRouter(prefix="/api/chat", tags=["chat"])


@router.post("")
async def chat(req: ChatRequest):
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM entries ORDER BY date DESC LIMIT 100"
        ).fetchall()

    if not rows:
        return JSONResponse(
            status_code=404, content={"error": "No diary entries found"}
        )

    entries = [dict(row) for row in rows]
    for entry in entries:
        for field in ("audio_files", "events", "people", "planned_actions", "topics"):
            entry[field] = json.loads(entry[field])

    answer = await chat_query(req.question, entries)

    return ChatResponse(answer=answer, entries_used=len(entries))
