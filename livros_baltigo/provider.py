"""Adaptador EAPI não oficial. Não contorna cotas, autenticação ou CAPTCHA.

Referências de protocolo (não há código de terceiros incorporado):
https://github.com/baroxyton/zlibrary-eapi-documentation
https://github.com/bipinkrish/Zlibrary-API
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import socket
import re
import time
import unicodedata
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urljoin, urlsplit

import aiohttp

from .config import Settings
from .catalog import LANGUAGES
from .errors import UserError
from .models import Book, Quota, SearchPage, SearchSpec, to_int
from .network import limited_body, validate_url
from .availability import pause_message, retry_after_seconds, network_kind

Progress = Callable[[int, int | None], Awaitable[None]]


def filename_for(book: Book, extension: str) -> str:
    if extension not in {"pdf", "epub"}:
        raise UserError("Esta versão envia arquivos EPUB e PDF.", "unsupported_format")
    title = unicodedata.normalize("NFKC", f"{book.title} - {book.author}")
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", title)
    title = "".join(c for c in title if unicodedata.category(c) != "Cf")
    title = title[:120].strip(". ") or "livro"
    # Evita nomes reservados do Windows e caminhos fornecidos pela fonte.
    if title.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL", "COM1", "LPT1"}:
        title = "livro_" + title
    return f"{title}.{extension}"


class ZLibrary:
    def __init__(self, settings: Settings, api_session, file_session):
        self.settings = settings
        self.api = api_session
        self.files = file_session
        self.base = settings.base_url.rstrip("/")
        self.user_id = settings.user_id
        self.user_key = settings.user_key
        self.auth_lock = asyncio.Lock()
        self.retry_at = 0.0
        self.retry_code = ""
        host = (urlsplit(self.base).hostname or "").lower()
        self.file_hosts = settings.file_hosts | {host}

    def pause(self, code: str, seconds: float = 30):
        """Local retry protection retains the cause; it is not a download quota."""
        self.retry_code = code
        self.retry_at = time.monotonic() + max(1, seconds)

    def check_pause(self):
        remaining = math.ceil(self.retry_at - time.monotonic())
        if remaining > 0:
            code = self.retry_code or "cooldown"
            raise UserError(pause_message(code, remaining), code)

    def network_error(self, exc: BaseException) -> UserError:
        code = network_kind(exc)
        error = getattr(exc, "os_error", None) or exc
        logging.getLogger(__name__).warning(
            "Fonte indisponível: categoria=%s tipo=%s errno=%s",
            code, type(error).__name__, getattr(error, "errno", None))
        self.pause(code, 30)
        return UserError(pause_message(code, 30), code)

    def _headers(self) -> dict[str, str]:
        result = {"Accept": "application/json"}
        if self.user_id and self.user_key:
            result.update({
                "remix-userid": self.user_id, "remix-userkey": self.user_key,
                "Cookie": f"remix_userid={self.user_id}; remix_userkey={self.user_key}; siteLanguageV2=en",
            })
        return result

    async def ensure_login(self):
        self.check_pause()
        if not self.settings.source_configured:
            raise UserError("O bot está conectado ao Telegram, mas a integração com o Z-Library ainda não foi configurada. O administrador precisa definir o domínio e as credenciais nas variáveis do Railway. Não envie senhas pelo chat.", "setup_required")
        if self.user_id and self.user_key:
            return
        async with self.auth_lock:
            if self.user_id and self.user_key:
                return
            payload = await self._request("POST", "/eapi/user/login", data={
                "email": self.settings.email, "password": self.settings.password,
            }, authenticate=False)
            user = payload.get("user")
            if not isinstance(user, dict):
                raise UserError("Login não confirmado. Verifique a conta e eventual verificação adicional no site.", "auth")
            user_id = str(user.get("id") or "")
            user_key = str(user.get("remix_userkey") or "")
            if not user_id.isdigit() or not user_key or len(user_key) > 1024 or any(c in user_key for c in "\r\n;"):
                raise UserError("A API não retornou uma sessão válida.", "auth")
            self.user_id, self.user_key = user_id, user_key

    async def _request(self, method: str, path: str, *, data=None, authenticate=True) -> dict:
        if authenticate:
            await self.ensure_login()
        self.check_pause()
        url = validate_url(self.base + path)
        try:
            async with self.api.request(method, url, data=data, headers=self._headers(), allow_redirects=False) as response:
                if response.status == 429:
                    self.pause("rate_limit", retry_after_seconds(response.headers.get("Retry-After")))
                    self.check_pause()
                if response.status == 401:
                    self.pause("auth", 60)
                    raise UserError("Sessão expirada ou inválida. O administrador precisa atualizar as credenciais no servidor.", "auth")
                if response.status == 403:
                    self.pause("blocked", 60)
                    raise UserError("A fonte bloqueou a requisição. Verifique o domínio e a conta; o bot não contorna esse bloqueio.", "blocked")
                if 300 <= response.status < 400:
                    raise UserError("O domínio da API redirecionou. Verifique ZLIB_BASE_URL antes de enviar credenciais a outro endereço.", "api_redirect")
                if response.status == 404:
                    raise UserError("Rota não encontrada na fonte. O domínio ou a API pode ter mudado.", "not_found")
                if response.status != 200:
                    self.pause("unavailable", 30)
                    raise UserError("A fonte está indisponível no momento.", "unavailable")
                raw = await limited_body(response, 2_000_000)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            raise self.network_error(exc) from exc
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise UserError("A fonte respondeu com uma página inesperada, não com dados da API.", "schema") from exc
        if not isinstance(payload, dict):
            raise UserError("O formato da resposta da fonte mudou.", "schema")
        if payload.get("success") in (False, 0, "0"):
            error = str(payload.get("error") or payload.get("message") or "").lower()[:2000]
            if any(word in error for word in ("limit", "quota", "downloads exceeded")):
                raise UserError("O limite da conta na fonte foi atingido. Nenhum limite será contornado.", "quota")
            if any(word in error for word in ("auth", "login", "password", "session", "unauthorized")):
                raise UserError("A fonte não aceitou a sessão. Confira as credenciais no servidor.", "auth")
            raise UserError("A fonte recusou a operação. Verifique sua conta no serviço.", "source_rejected")
        return payload

    async def search(self, spec: SearchSpec, page: int = 1, limit: int = 8) -> SearchPage:
        if not 1 <= page <= 1000 or not 1 <= len(spec.query) <= 200:
            raise UserError("Busca inválida. Use entre 1 e 200 caracteres.", "invalid_search")
        if spec.language not in LANGUAGES or spec.extension not in {"epub", "pdf", "any"}:
            raise UserError("Filtro inválido.", "invalid_filter")
        data = [("message", spec.query), ("page", str(page)), ("limit", str(limit))]
        if spec.language != "any":
            data.append(("languages[]", spec.language))
        for extension in ([spec.extension] if spec.extension != "any" else ["epub", "pdf"]):
            data.append(("extensions[]", extension))
        payload = await self._request("POST", "/eapi/book/search", data=data)
        rows = payload.get("books")
        if not isinstance(rows, list):
            raise UserError("A fonte não retornou uma lista de livros reconhecível.", "schema")
        books: list[Book] = []
        seen: set[str] = set()
        for row in rows:
            try:
                book = Book.from_source(row)
            except (ValueError, TypeError, AttributeError):
                continue
            if book.key not in seen:
                books.append(book)
                seen.add(book.key)
        pagination = payload.get("pagination")
        pagination = pagination if isinstance(pagination, dict) else {}
        total = to_int(payload.get("total") or payload.get("total_count") or pagination.get("total"))
        if total is not None:
            has_next = page * limit < total
        elif "has_next" in payload:
            has_next = payload["has_next"] in (True, 1, "1", "true")
        else:
            # Sem total, uma página cheia permite consultar a próxima; pode estar vazia.
            has_next = len(rows) >= limit
        return SearchPage(books[:limit], page, has_next, total)

    async def details(self, book: Book) -> Book:
        payload = await self._request("GET", f"/eapi/book/{book.id}/{book.hash}")
        row = payload.get("book")
        if not isinstance(row, dict):
            raise UserError("Detalhes indisponíveis neste momento.", "schema")
        merged = book.to_dict() | {k: v for k, v in row.items() if v is not None}
        merged["id"], merged["hash"] = book.id, book.hash
        try:
            return Book.from_source(merged)
        except ValueError as exc:
            raise UserError("Detalhes inválidos recebidos da fonte.", "schema") from exc

    async def quota(self) -> Quota:
        payload = await self._request("GET", "/eapi/user/profile")
        user = payload.get("user")
        if not isinstance(user, dict):
            raise UserError("A fonte não informou o perfil da conta.", "schema")
        return Quota(to_int(user.get("downloads_limit")), to_int(user.get("downloads_today")))

    async def file_info(self, book: Book) -> tuple[str, str]:
        payload = await self._request("GET", f"/eapi/book/{book.id}/{book.hash}/file")
        info = payload.get("file")
        if not isinstance(info, dict) or not info.get("downloadLink"):
            raise UserError("A fonte não disponibilizou um arquivo para esta edição.", "file_missing")
        extension = str(info.get("extension") or book.extension).lower().strip(". ")
        if extension not in {"pdf", "epub"}:
            raise UserError("Esta versão envia apenas EPUB e PDF. Escolha outra edição.", "unsupported_format")
        url = urljoin(self.base + "/", str(info["downloadLink"]))
        return validate_url(url, self.file_hosts), extension

    async def download(self, url: str, extension: str, destination: Path, progress: Progress) -> int:
        current = url
        try:
            for _ in range(6):
                validate_url(current, self.file_hosts)
                async with self.files.get(
                    current, allow_redirects=False,
                    **({"headers": self._headers()} if urlsplit(current).netloc == urlsplit(self.base).netloc else {}),
                    timeout=aiohttp.ClientTimeout(total=600, connect=15, sock_read=60),
                ) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location:
                            raise UserError("Redirecionamento de arquivo inválido.", "unsafe_url")
                        current = urljoin(current, location)
                        continue
                    if response.status != 200:
                        raise UserError("O arquivo não pôde ser baixado. A cota da fonte pode ter sido consumida; confira /limite.", "download_http")
                    content_type = response.headers.get("Content-Type", "").lower()
                    if any(x in content_type for x in ("text/html", "application/json", "javascript", "x-msdownload")):
                        raise UserError("A fonte retornou uma página ou resposta inesperada no lugar do livro.", "invalid_file")
                    total = to_int(response.headers.get("Content-Length"))
                    maximum = self.settings.max_file_bytes
                    if total is not None and total > maximum:
                        raise UserError("O arquivo excede o tamanho configurado para envio pelo Telegram.", "file_too_large")
                    count = 0
                    head = bytearray()
                    with destination.open("wb") as output:
                        async for chunk in response.content.iter_chunked(128 * 1024):
                            count += len(chunk)
                            if count > maximum:
                                raise UserError("O arquivo excede o limite de tamanho; o download foi interrompido.", "file_too_large")
                            if len(head) < 1024:
                                head.extend(chunk[:1024 - len(head)])
                            await asyncio.to_thread(output.write, chunk)
                            await progress(count, total)
                    if count == 0 or (extension == "pdf" and not bytes(head).lstrip().startswith(b"%PDF-")):
                        raise UserError("O conteúdo recebido não corresponde a um PDF válido.", "invalid_file")
                    if extension == "epub" and not bytes(head).startswith(b"PK\x03\x04"):
                        raise UserError("O conteúdo recebido não corresponde a um contêiner EPUB.", "invalid_file")
                    if total is not None and count != total and not response.headers.get("Content-Encoding"):
                        raise UserError("O download terminou incompleto.", "incomplete_file")
                    return count
            raise UserError("Excesso de redirecionamentos no download.", "redirect_loop")
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            destination.unlink(missing_ok=True)
            raise UserError("O download foi interrompido. Confira /limite antes de tentar novamente.", "download_network") from exc
        except BaseException:
            destination.unlink(missing_ok=True)
            raise

    async def cover(self, url: str) -> bytes | None:
        if not url:
            return None
        try:
            current = urljoin(self.base + "/", url)
            for _ in range(4):
                validate_url(current)
                async with self.files.get(current, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=12)) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        current = urljoin(current, response.headers.get("Location", ""))
                        continue
                    if response.status != 200:
                        return None
                    body = await limited_body(response, 2_000_000)
                    if body.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n")):
                        return body
                    return None
        except (UserError, aiohttp.ClientError, asyncio.TimeoutError, OSError):
            return None
        return None
