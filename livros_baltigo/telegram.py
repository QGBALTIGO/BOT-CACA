"""Cliente enxuto da Bot API oficial; sem enviar o token ao provedor de livros."""
from __future__ import annotations

import asyncio
import io
import json
import time
import weakref
from pathlib import Path

import aiohttp

from .errors import TelegramError
from .network import limited_body


class Telegram:
    def __init__(self, token: str, session):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.session = session
        self._gate = asyncio.Lock()
        self._last = 0.0
        self._chats: dict[int, float] = {}
        self._chat_locks = weakref.WeakValueDictionary()

    async def _global_pace(self):
        async with self._gate:
            delay = self._last + 0.05 - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()

    async def _pace(self, chat_id: int | None):
        if chat_id is None:
            await self._global_pace()
            return
        # One busy conversation must not hold the global lock for other readers.
        lock = self._chat_locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            delay = self._chats.get(chat_id, 0.0) + 1.05 - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            await self._global_pace()
            self._chats[chat_id] = time.monotonic()
            if len(self._chats) > 5000:
                self._chats = {key: value for key, value in self._chats.items() if value > self._last - 60}

    async def call(self, method: str, data: dict | None = None, *, file=None, timeout=65):
        values = data or {}
        if method != "getUpdates":
            await self._pace(values.get("chat_id"))
        form = None
        handle = None
        try:
            if file:
                field, content, filename = file
                handle = content.open("rb") if isinstance(content, Path) else io.BytesIO(content)
                form = aiohttp.FormData()
                for key, value in values.items():
                    form.add_field(key, json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list, bool)) else str(value))
                form.add_field(field, handle, filename=filename, content_type="application/octet-stream")
            async with self.session.post(
                self.base + method,
                data=form, json=None if form is not None else values,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=timeout, connect=15, sock_read=timeout),
            ) as response:
                if 300 <= response.status < 400:
                    raise TelegramError(response.status, "Redirecionamento da Bot API recusado")
                raw = await limited_body(response, 4_000_000)
                try:
                    payload = json.loads(raw)
                except (ValueError, UnicodeDecodeError) as exc:
                    raise TelegramError(response.status, "Resposta inválida") from exc
                if not isinstance(payload, dict) or not payload.get("ok"):
                    code = int(payload.get("error_code", response.status)) if isinstance(payload, dict) else response.status
                    details = payload if isinstance(payload, dict) else {}
                    parameters = details.get("parameters") or {}
                    raise TelegramError(code, str(details.get("description", "")), int(parameters.get("retry_after", 0)))
                return payload.get("result")
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            # 0 = resultado desconhecido. Escritas NÃO são repetidas automaticamente.
            raise TelegramError(0, "Falha de rede; resultado da operação desconhecido") from exc
        finally:
            if handle is not None:
                handle.close()

    async def message(self, chat: int, text: str, keyboard=None):
        values = {"chat_id": chat, "text": text, "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}
        if keyboard:
            values["reply_markup"] = {"inline_keyboard": keyboard}
        return await self.call("sendMessage", values)

    async def edit(self, chat: int, message: int, text: str, keyboard=None):
        values = {"chat_id": chat, "message_id": message, "text": text, "parse_mode": "HTML", "link_preview_options": {"is_disabled": True},
                  "reply_markup": {"inline_keyboard": keyboard or []}}
        try:
            return await self.call("editMessageText", values)
        except TelegramError as exc:
            if exc.code == 400 and "message is not modified" in exc.description.lower():
                return None
            raise

    async def markup(self, chat: int, message: int, keyboard):
        try:
            return await self.call("editMessageReplyMarkup", {"chat_id": chat, "message_id": message,
                                  "reply_markup": {"inline_keyboard": keyboard}})
        except TelegramError as exc:
            if exc.code == 400 and "message is not modified" in exc.description.lower():
                return None
            raise

    async def answer(self, query_id: str, text: str = "", alert=False):
        try:
            await self.call("answerCallbackQuery", {"callback_query_id": query_id, "text": text[:180], "show_alert": alert})
        except TelegramError:
            # Callback vencido não cancela ações válidas nem derruba o processo.
            pass

    async def photo(self, chat: int, image: bytes, caption: str, keyboard):
        return await self.call("sendPhoto", {"chat_id": chat, "caption": caption, "parse_mode": "HTML",
                               "reply_markup": {"inline_keyboard": keyboard}}, file=("photo", image, "capa.jpg"))

    async def document(self, chat: int, path: Path, filename: str, caption: str):
        return await self.call("sendDocument", {"chat_id": chat, "caption": caption, "parse_mode": "HTML"},
                               file=("document", path, filename), timeout=600)
