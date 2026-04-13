import asyncio
import logging
import re
from datetime import datetime
from pathlib import Path

from telegram import Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.config import settings
from app.services.edits import apply_edit, detect_edit, format_edit_confirmation
from app.services.llm import chat_query
from app.services.pipeline import process_audio
from app.services.search import analyze_query, smart_retrieve

logger = logging.getLogger(__name__)

# Per-chat conversation history (chat_id -> list of message dicts)
_chat_history: dict[int, list[dict]] = {}
MAX_HISTORY = 6


def _md_to_html(text: str) -> str:
    """Convert common Markdown to Telegram-compatible HTML."""
    # Escape HTML entities first
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # Headers → bold
    text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)

    # Bold: **text** or __text__
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)

    # Italic: *text* or _text_ (but not inside words like file_name)
    text = re.sub(r"(?<!\w)\*([^*]+?)\*(?!\w)", r"<i>\1</i>", text)
    text = re.sub(r"(?<!\w)_([^_]+?)_(?!\w)", r"<i>\1</i>", text)

    # Inline code
    text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)

    # Bullet lists: - or * at start of line → bullet character
    text = re.sub(r"^[\-\*]\s+", "• ", text, flags=re.MULTILINE)

    return text


async def _reply(message, text: str):
    """Send a reply with HTML formatting, falling back to plain text."""
    html = _md_to_html(text)
    # Telegram has a 4096 char limit per message
    chunks = [html[i : i + 4096] for i in range(0, len(html), 4096)]
    for chunk in chunks:
        try:
            await message.reply_text(chunk, parse_mode="HTML")
        except BadRequest:
            # If HTML parsing fails, send the original plain text
            plain_chunk = text[: len(chunk)]
            await message.reply_text(plain_chunk)


def _is_allowed(user_id: int) -> bool:
    """Check if a Telegram user ID is in the allowed list."""
    allowed = settings.TELEGRAM_ALLOWED_USERS.strip()
    if not allowed:
        return True
    allowed_ids = {int(uid.strip()) for uid in allowed.split(",") if uid.strip()}
    return user_id in allowed_ids


def _get_history(chat_id: int) -> list[dict]:
    return _chat_history.get(chat_id, [])


def _append_history(chat_id: int, role: str, content: str):
    history = _chat_history.setdefault(chat_id, [])
    history.append({"role": role, "content": content})
    # Keep only last MAX_HISTORY messages
    if len(history) > MAX_HISTORY:
        _chat_history[chat_id] = history[-MAX_HISTORY:]


async def _start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update.effective_user.id):
        await update.message.reply_text("Du har inte behörighet att använda denna bot.")
        return
    await update.message.reply_text(
        "Hej! Jag är din dagboksassistent.\n\n"
        "Skicka ett röstmeddelande för att skapa en dagboksinlägg.\n"
        "Skriv en fråga för att chatta med din dagbok.\n\n"
        "Exempel:\n"
        '- "Hur mådde jag förra veckan?"\n'
        '- "Vad hände i mars?"\n'
        '- "Vilka har jag träffat senaste månaden?"'
    )


async def _clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    _chat_history.pop(chat_id, None)
    await update.message.reply_text("Konversationshistorik rensad.")


async def _handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle voice messages and audio files — create diary entry."""
    if not _is_allowed(update.effective_user.id):
        await update.message.reply_text("Du har inte behörighet att använda denna bot.")
        return

    voice = update.message.voice or update.message.audio
    if not voice:
        return

    await update.message.reply_text("Tar emot ljud, bearbetar din dagboksinlägg...")

    # Download the audio file
    file = await context.bot.get_file(voice.file_id)
    audio_dir = settings.audio_dir
    audio_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    # Telegram voice messages are .oga (ogg/opus), audio files keep their format
    suffix = "oga" if update.message.voice else (voice.file_name or "audio").rsplit(".", 1)[-1]
    filepath = audio_dir / f"{timestamp}.{suffix}"

    await file.download_to_drive(filepath)
    logger.info(f"Telegram: saved audio {filepath.name} ({filepath.stat().st_size} bytes)")

    # Process in a thread to not block the bot
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, process_audio, filepath)
        await update.message.reply_text("Dagboksinlägg skapad och bearbetad!")
    except Exception as e:
        logger.exception("Error processing Telegram audio")
        await update.message.reply_text(f"Fel vid bearbetning: {e}")


async def _handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text messages — chat with the diary."""
    if not _is_allowed(update.effective_user.id):
        await update.message.reply_text("Du har inte behörighet att använda denna bot.")
        return

    question = update.message.text
    if not question:
        return

    chat_id = update.effective_chat.id
    history = _get_history(chat_id)

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    try:
        # Step 0: Edit/correction command?
        edit_cmd = await detect_edit(question, history or None)
        if edit_cmd.is_edit:
            count, dates = apply_edit(edit_cmd)
            answer = format_edit_confirmation(edit_cmd, count, dates)
        else:
            # Step 1: Analyze query
            intent = await analyze_query(question, history or None)
            # Step 2: Retrieve context
            diary_context = await smart_retrieve(intent)
            # Step 3: Generate answer (with Telegram formatting)
            answer = await chat_query(question, diary_context, history or None, "telegram")

        _append_history(chat_id, "user", question)
        _append_history(chat_id, "assistant", answer)

        await _reply(update.message, answer)

    except Exception as e:
        logger.exception("Error handling Telegram chat")
        await update.message.reply_text(f"Fel: {e}")


_application: Application | None = None


async def start_telegram_bot():
    """Start the Telegram bot (call during app lifespan startup)."""
    global _application

    token = settings.TELEGRAM_BOT_TOKEN.strip()
    if not token:
        logger.info("TELEGRAM_BOT_TOKEN not set, skipping Telegram bot")
        return

    _application = Application.builder().token(token).build()

    _application.add_handler(CommandHandler("start", _start_command))
    _application.add_handler(CommandHandler("clear", _clear_command))
    _application.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, _handle_voice))
    _application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_text))

    await _application.initialize()
    await _application.start()
    await _application.updater.start_polling(drop_pending_updates=True)
    logger.info("Telegram bot started")


async def stop_telegram_bot():
    """Stop the Telegram bot (call during app lifespan shutdown)."""
    global _application
    if _application is None:
        return

    await _application.updater.stop()
    await _application.stop()
    await _application.shutdown()
    _application = None
    logger.info("Telegram bot stopped")
