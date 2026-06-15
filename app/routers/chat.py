import base64
import mimetypes
from pathlib import Path

from fastapi import APIRouter

from app.config import settings
from app.models import ChatPhoto, ChatRequest, ChatResponse
from app.services.audio_summary import (
    detect_audio_summary_request,
    generate_audio_summary,
)
from app.services.edits import apply_edit, detect_edit, format_edit_confirmation, reanalyze_affected_entries
from app.services.llm import chat_query
from app.services.photos import photos_for_answer
from app.services.search import analyze_query, smart_retrieve

MAX_PHOTOS_PER_RESPONSE = 10


def _build_chat_photos(answer: str, inline: bool) -> list[ChatPhoto]:
    out: list[ChatPhoto] = []
    for p in photos_for_answer(answer, limit=MAX_PHOTOS_PER_RESPONSE):
        path = settings.photos_dir / p["filename"]
        data_url = None
        if inline and path.exists():
            mime, _ = mimetypes.guess_type(p["filename"])
            mime = mime or "image/jpeg"
            b64 = base64.b64encode(path.read_bytes()).decode("ascii")
            data_url = f"data:{mime};base64,{b64}"
        out.append(
            ChatPhoto(
                date=p["date"],
                filename=p["filename"],
                description=p.get("description") or "",
                caption=p.get("caption"),
                data_url=data_url,
                url=f"/api/photos/{p['filename']}",
            )
        )
    return out


router = APIRouter(prefix="/api/chat", tags=["chat"])


@router.post("")
async def chat(req: ChatRequest):
    # Step 0a: Did the user ask for a podcast/radio-style audio summary?
    audio_req = await detect_audio_summary_request(req.question, req.messages or None)
    if audio_req.is_audio_summary and audio_req.period_type and audio_req.period_key:
        result = await generate_audio_summary(audio_req.period_type, audio_req.period_key)
        if result.get("audio_path"):
            audio_path = result["audio_path"]
            answer = (
                f"Här kommer Dagboksradion för {result['label']} — "
                f"{result['entry_count']} dagboksanteckning(ar) sammanfattade."
            )
            return ChatResponse(
                answer=answer,
                entries_used=result["entry_count"],
                audio_url=f"/api/audio-summaries/file/{Path(audio_path).name}",
                audio_label=result["label"],
            )
        return ChatResponse(
            answer=f"Inga dagboksanteckningar hittades för {result.get('label', audio_req.period_key)}.",
            entries_used=0,
        )

    # Step 0b: Edit/correction command?
    edit_cmd = await detect_edit(req.question, req.messages or None)
    if edit_cmd.is_edit:
        count, dates = apply_edit(edit_cmd)
        if dates:
            await reanalyze_affected_entries(dates)
        answer = format_edit_confirmation(edit_cmd, count, dates)
        return ChatResponse(answer=answer, entries_used=count)

    # Step 1: Analyze the question to determine intent and scope
    intent = await analyze_query(req.question, req.messages or None)

    # Step 2: Retrieve the right level of context
    context = await smart_retrieve(intent)

    # Step 3: Generate answer with the retrieved context
    answer = await chat_query(req.question, context, req.messages or None, req.client_type)

    # Inline base64 photos for non-telegram clients (Telegram fetches separately).
    inline_photos = req.client_type != "telegram"
    photos = _build_chat_photos(answer, inline=inline_photos)

    return ChatResponse(
        answer=answer,
        entries_used=context.count("###"),
        photos=photos,
    )
