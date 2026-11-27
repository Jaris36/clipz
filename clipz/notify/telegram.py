"""Minimal Telegram Bot API client (official HTTPS API, bot token only).

Only four calls are used: sendMessage, getUpdates, answerCallbackQuery, editMessageReplyMarkup.
DryRunClient prints instead of sending, used whenever TELEGRAM_BOT_TOKEN is empty.
"""
from __future__ import annotations

import json
import time
from typing import Any, Protocol

API = "https://api.telegram.org"


class TelegramError(RuntimeError):
    """Telegram answered and said no (bad token, bad chat id, bad markup). Safe to retry later."""


class DeliveryUnknown(RuntimeError):
    """The request may or may not have reached Telegram (timeout, dropped connection).
    The notifier treats this as sent, so a message is never delivered twice."""


Buttons = list[list[tuple[str, str]]]   # rows of (label, callback_data)


def keyboard(buttons: Buttons | None) -> dict | None:
    if not buttons:
        return None
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in buttons]}


class Client(Protocol):
    def send_message(self, chat_id: str, text: str, buttons: Buttons | None = None) -> int | None: ...
    def get_updates(self, offset: int | None, timeout: int = 0) -> list[dict]: ...
    def answer_callback(self, callback_id: str, text: str = "") -> None: ...
    def edit_buttons(self, chat_id: str, message_id: int, buttons: Buttons | None) -> None: ...


class TelegramClient:
    def __init__(self, token: str, http: Any = None):
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN is empty")
        import httpx
        self._base = f"{API}/bot{token}"
        self._http = http or httpx.Client(timeout=httpx.Timeout(10.0, read=70.0))

    def _call(self, method: str, payload: dict, sent_once: bool = False) -> Any:
        import httpx
        try:
            r = self._http.post(f"{self._base}/{method}", json=payload)
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            if sent_once:
                raise DeliveryUnknown(f"{method}: {type(e).__name__}") from e
            raise TelegramError(f"{method}: {type(e).__name__}") from e
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code == 429:
            raise TelegramError(f"{method}: rate limited, retry after "
                                f"{(body.get('parameters') or {}).get('retry_after', '?')}s")
        if not body.get("ok"):
            # Never include the URL in errors: it contains the bot token.
            raise TelegramError(f"{method}: HTTP {r.status_code} {body.get('description', '')}".strip())
        return body["result"]

    def send_message(self, chat_id: str, text: str, buttons: Buttons | None = None) -> int | None:
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                   "link_preview_options": {"is_disabled": True}}
        kb = keyboard(buttons)
        if kb:
            payload["reply_markup"] = kb
        return self._call("sendMessage", payload, sent_once=True).get("message_id")

    def get_updates(self, offset: int | None, timeout: int = 0) -> list[dict]:
        payload: dict = {"timeout": timeout, "allowed_updates": ["callback_query", "message"]}
        if offset is not None:
            payload["offset"] = offset
        return self._call("getUpdates", payload)

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    def edit_buttons(self, chat_id: str, message_id: int, buttons: Buttons | None) -> None:
        self._call("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": message_id,
                                              "reply_markup": keyboard(buttons) or {"inline_keyboard": []}})


class DryRunClient:
    """Prints messages instead of sending. Has no updates, so no buttons can be tapped."""

    def __init__(self, out=print):
        self.out = out
        self.sent: list[dict] = []

    def send_message(self, chat_id: str, text: str, buttons: Buttons | None = None) -> int | None:
        self.sent.append({"chat_id": chat_id, "text": text, "buttons": buttons})
        labels = " | ".join(t for row in (buttons or []) for t, _ in row)
        self.out(f"[notify dry-run] to {chat_id or '(no chat id)'}:\n{text}" + (f"\n  [{labels}]" if labels else ""))
        return len(self.sent)

    def get_updates(self, offset: int | None, timeout: int = 0) -> list[dict]:
        if timeout:
            time.sleep(min(timeout, 60))
        return []

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        pass

    def edit_buttons(self, chat_id: str, message_id: int, buttons: Buttons | None) -> None:
        pass


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)
