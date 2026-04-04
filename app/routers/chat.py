from fastapi import APIRouter

from app.models import ChatRequest, ChatResponse
from app.services.llm import chat_query
from app.services.search import analyze_query, smart_retrieve

router = APIRouter(prefix="/api/chat", tags=["chat"])


@router.post("")
async def chat(req: ChatRequest):
    # Step 1: Analyze the question to determine intent and scope
    intent = await analyze_query(req.question, req.messages or None)

    # Step 2: Retrieve the right level of context
    context = await smart_retrieve(intent)

    # Step 3: Generate answer with the retrieved context
    answer = await chat_query(req.question, context, req.messages or None)

    return ChatResponse(answer=answer, entries_used=context.count("###"))
