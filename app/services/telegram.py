import asyncio
import logging
import re
from datetime import datetime
from pathlib import Path

from telegram import Update
from telegram.error import BadRequest, NetworkError, TimedOut
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.config import settings
from app.services.audio_summary import (
    detect_audio_summary_request,
    generate_audio_summary,
)
from app.services.edits import apply_edit, detect_edit, format_edit_confirmation, reanalyze_affected_entries
from app.services.llm import chat_query
from app.services.photos import photos_for_answer, process_photo, save_photo_bytes
from app.services.pipeline import process_audio
from app.services.search import analyze_query, smart_retrieve

logger = logging.getLogger(__name__)


class _TelegramNetworkErrorFilter(logging.Filter):
    """Collapse Telegram polling network blips into a single friendly line.

    Why: api.telegram.org routinely closes long-poll connections; PTB catches
    it, logs the full traceback at ERROR, and retries. The traceback is noise.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        exc_info = record.exc_info
        exc = exc_info[1] if exc_info else None
        if isinstance(exc, (NetworkError, TimedOut)):
            record.msg = "Telegram polling network blip (%s: %s) — auto-retrying."
            record.args = (type(exc).__name__, exc)
            record.exc_info = None
            record.exc_text = None
            record.levelno = logging.WARNING
            record.levelname = "WARNING"
        return True


# Per-chat conversation history (chat_id -> list of message dicts)
_chat_history: dict[int, list[dict]] = {}
MAX_HISTORY = 6
MAX_PHOTOS_PER_REPLY = 10


async def _send_photos_for_answer(message, answer: str):
    """If the answer references diary dates that have photos, send them."""
    for p in photos_for_answer(answer, limit=MAX_PHOTOS_PER_REPLY):
        path = settings.photos_dir / p["filename"]
        if not path.exists():
            logger.warning(f"Photo file missing: {path}")
            continue
        desc = (p.get("description") or "").strip()
        caption = f"{p['date']}: {desc}" if desc else p["date"]
        caption = caption[:1024]
        try:
            with path.open("rb") as fh:
                await message.reply_photo(photo=fh, caption=caption)
        except Exception:
            logger.exception(f"Failed to send photo {path.name}")


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
        "Skriv en fråga för att chatta med din dagbok.\n"
        "Be om en ljudsammanfattning så får du Dagboksradion som ljudfil.\n\n"
        "Exempel:\n"
        '- "Hur mådde jag förra veckan?"\n'
        '- "Vad hände i mars?"\n'
        '- "Ge mig en ljudsammanfattning för idag"\n'
        '- "Gör en podcast av juni"\n'
        "- /summary månaden\n"
        "- /summary ytd"
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


async def _handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle photos (and image documents) — describe and attach to today's entry."""
    if not _is_allowed(update.effective_user.id):
        await update.message.reply_text("Du har inte behörighet att använda denna bot.")
        return

    if update.message.photo:
        # Use the largest available size
        photo = update.message.photo[-1]
        suffix = "jpg"
    elif update.message.document and (update.message.document.mime_type or "").startswith("image/"):
        photo = update.message.document
        suffix = (photo.file_name or "image.jpg").rsplit(".", 1)[-1]
    else:
        return

    caption = update.message.caption

    await update.message.reply_text("Tar emot bild, beskriver...")

    file = await context.bot.get_file(photo.file_id)
    data = bytes(await file.download_as_bytearray())
    path = save_photo_bytes(data, suffix=suffix)
    logger.info(f"Telegram: saved photo {path.name} ({len(data)} bytes)")

    entry_date = datetime.now().date().isoformat()
    try:
        _, description = await process_photo(path, entry_date, caption=caption)
    except Exception as e:
        logger.exception("Error processing Telegram photo")
        await update.message.reply_text(f"Fel vid bildbeskrivning: {e}")
        return

    if description:
        await _reply(update.message, f"<b>Bild sparad ({entry_date})</b>\n{description}")
    else:
        await update.message.reply_text(f"Bild sparad ({entry_date}), men ingen beskrivning kunde genereras.")


async def _send_audio_summary(
    message, period_type: str, period_key: str, style: str | None = None
) -> str:
    """Generate and send the audio summary; returns a text caption to show."""
    result = await generate_audio_summary(period_type, period_key, style=style)
    if not result.get("audio_path"):
        return f"Inga dagboksanteckningar hittades för {result.get('label', period_key)}."

    audio_path = Path(result["audio_path"])
    caption = (
        f"<b>Dagboksradion — {result['label']}</b>\n"
        f"{result['entry_count']} dagboksanteckning(ar)"
    )
    try:
        with audio_path.open("rb") as fh:
            await message.reply_audio(
                audio=fh,
                title=f"Dagboksradion — {result['label']}",
                performer="Dagboksradion",
                caption=caption[:1024],
                parse_mode="HTML",
            )
    except Exception:
        logger.exception(f"Failed to send audio summary {audio_path.name}")
        return f"Kunde inte skicka ljudfilen för {result['label']}."
    return ""  # success — audio carries the message


async def _summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/summary <period>` — generate an audio summary on demand.

    Examples:
      /summary idag
      /summary 2026-06-13
      /summary månaden
      /summary 2026-06
      /summary år
      /summary 2026
      /summary ytd
    """
    if not _is_allowed(update.effective_user.id):
        return

    args_text = " ".join(context.args).strip() if context.args else ""
    if not args_text:
        await update.message.reply_text(
            "Användning: /summary <period> [stil]\n\n"
            "Exempel:\n"
            "  /summary idag\n"
            "  /summary 2026-06-13\n"
            "  /summary månaden\n"
            "  /summary 2026-06\n"
            "  /summary ytd\n"
            "  /summary 2026\n\n"
            "Stilar (valfritt): sakligt (bara fakta) eller roasta (skämtsamt).\n"
            "  /summary roasta igår\n"
            "  /summary sakligt 2026-06"
        )
        return

    await update.message.reply_text(
        f"Genererar Dagboksradion för \"{args_text}\"... det här kan ta en stund."
    )
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action="record_voice"
    )

    try:
        # Reuse the LLM-based detector for natural-language period parsing.
        intent = await detect_audio_summary_request(args_text)
        if not (intent.is_audio_summary and intent.period_type and intent.period_key):
            await update.message.reply_text(
                "Förstod inte perioden. Försök med t.ex. 'idag', '2026-06', 'ytd' eller '2026'."
            )
            return
        err = await _send_audio_summary(
            update.message, intent.period_type, intent.period_key, style=intent.style
        )
        if err:
            await update.message.reply_text(err)
    except Exception as e:
        logger.exception("Error generating audio summary via /summary")
        await update.message.reply_text(f"Fel: {e}")


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
        # Step 0a: Audio summary request?
        audio_req = await detect_audio_summary_request(question, history or None)
        if audio_req.is_audio_summary and audio_req.period_type and audio_req.period_key:
            await update.message.reply_text(
                "Genererar Dagboksradion... det här kan ta en stund."
            )
            await context.bot.send_chat_action(chat_id=chat_id, action="record_voice")
            err = await _send_audio_summary(
                update.message,
                audio_req.period_type,
                audio_req.period_key,
                style=audio_req.style,
            )
            if err:
                await update.message.reply_text(err)
            _append_history(chat_id, "user", question)
            _append_history(
                chat_id, "assistant", f"[Skickade ljudsammanfattning för {audio_req.period_key}]"
            )
            return

        # Step 0b: Edit/correction command?
        edit_cmd = await detect_edit(question, history or None)
        if edit_cmd.is_edit:
            count, dates = apply_edit(edit_cmd)
            if dates:
                await reanalyze_affected_entries(dates)
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
        if not edit_cmd.is_edit:
            await _send_photos_for_answer(update.message, answer)

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

    _network_filter = _TelegramNetworkErrorFilter()
    logging.getLogger("telegram.ext.Updater").addFilter(_network_filter)
    logging.getLogger("telegram.ext").addFilter(_network_filter)

    _application = Application.builder().token(token).build()

    _application.add_handler(CommandHandler("start", _start_command))
    _application.add_handler(CommandHandler("clear", _clear_command))
    _application.add_handler(CommandHandler("summary", _summary_command))
    _application.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, _handle_voice))
    _application.add_handler(
        MessageHandler(filters.PHOTO | filters.Document.IMAGE, _handle_photo)
    )
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
