"""Telegram Bot API client - sendMessage + editMessageText."""
import asyncio
import os

import httpx

# Telegram throttles bursts with 429 + parameters.retry_after. Retry once or
# twice when the wait is short; a long wait isn't worth stalling the scan for.
_MAX_RETRIES = 2
_MAX_RETRY_AFTER = 5.0


class TelegramClient:
    def __init__(self, token: str | None = None, chat_id: str | None = None):
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")
        # One keep-alive client for every alert: a fresh AsyncClient per message
        # paid a full TLS handshake to api.telegram.org on each send, right on
        # the alert's critical path. Created lazily inside the event loop.
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=15.0)
        return self._client

    async def close(self):
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def _call(self, method: str, payload: dict) -> dict:
        if not self.token or not self.chat_id:
            return {"ok": False, "error": "missing_credentials"}
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        payload = {"chat_id": self.chat_id, **payload}
        for attempt in range(_MAX_RETRIES + 1):
            resp = await self._http().post(url, json=payload)
            try:
                data = resp.json()
            except Exception:
                return {"ok": False, "status": resp.status_code, "body": resp.text}
            if resp.status_code == 429 and attempt < _MAX_RETRIES:
                wait = float((data.get("parameters") or {}).get("retry_after") or 1)
                if wait <= _MAX_RETRY_AFTER:
                    await asyncio.sleep(wait)
                    continue
            return data
        return data

    async def send_message(self, text: str) -> dict:
        return await self._call("sendMessage", {
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })

    async def edit_message(self, message_id: int, text: str) -> dict:
        """Replace the text of an already-sent message (no new notification)."""
        return await self._call("editMessageText", {
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })
