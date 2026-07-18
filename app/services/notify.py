"""One-shot Telegram sender for out-of-band notifications (e.g. the daily sync job).

Kept separate from `app.services.telegram` so standalone scripts can send a message
without importing the whole chat/llm/search/pipeline stack that the polling bot pulls in.
Sends a single message per call using a short-lived `telegram.Bot`.
"""

import logging

from telegram import Bot

from app.config import settings

logger = logging.getLogger(__name__)


def _resolve_chat_id() -> str | None:
    """Notification chat id: explicit override, else first allowed Telegram user."""
    if settings.HEALTH_SYNC_NOTIFY_CHAT_ID.strip():
        return settings.HEALTH_SYNC_NOTIFY_CHAT_ID.strip()
    allowed = [u.strip() for u in settings.TELEGRAM_ALLOWED_USERS.split(",") if u.strip()]
    return allowed[0] if allowed else None


async def notify(text: str) -> bool:
    """Send `text` to the configured chat. Returns True if sent, False if skipped.

    No-op (logs a warning) when the bot token or chat id isn't configured, so the
    caller never fails just because notifications aren't set up.
    """
    if not settings.TELEGRAM_BOT_TOKEN:
        logger.warning("notify skipped: TELEGRAM_BOT_TOKEN not set — %s", text)
        return False
    chat_id = _resolve_chat_id()
    if not chat_id:
        logger.warning("notify skipped: no chat id configured — %s", text)
        return False

    bot = Bot(settings.TELEGRAM_BOT_TOKEN)
    async with bot:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
    return True
