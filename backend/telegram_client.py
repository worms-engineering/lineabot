"""Telegram Bot API client - only sendMessage."""
import os
import httpx


class TelegramClient:
    def __init__(self, token: str | None = None, chat_id: str | None = None):
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")

    async def send_message(self, text: str) -> dict:
        if not self.token or not self.chat_id:
            return {"ok": False, "error": "missing_credentials"}
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                url,
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
            try:
                return resp.json()
            except Exception:
                return {"ok": False, "status": resp.status_code, "body": resp.text}
