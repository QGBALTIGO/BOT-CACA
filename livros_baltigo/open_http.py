"""Bounded anonymous HTTP for independent open catalogs. No credentials or proxies."""
from __future__ import annotations

import asyncio
import io
import json
import re
import time
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

import aiohttp
from bs4 import BeautifulSoup

from .availability import retry_after_seconds
from .errors import UserError
from .network import limited_body, validate_url


def document(raw: bytes):
    if len(raw) > 2_000_000:
        raise UserError('A página do catálogo excedeu o limite.', 'response_too_large')
    soup = BeautifulSoup(raw, 'html.parser')
    title = soup.title.get_text(' ', strip=True).casefold() if soup.title else ''
    if soup.select_one('#challenge-form, #cf-challenge-running, .h-captcha, .g-recaptcha') or title in {
        'just a moment...', 'attention required! | cloudflare', 'access denied', 'access refused'
    }:
        raise UserError('A fonte exige verificação humana. Escolha outro catálogo.', 'captcha')
    return soup


def read_json(raw: bytes):
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise UserError('O catálogo retornou metadados inválidos.', 'schema') from exc
    if not isinstance(data, dict):
        raise UserError('O catálogo retornou metadados inválidos.', 'schema')
    return data


def cc_license(values) -> str:
    """Accept explicit Creative Commons edition licenses, not generic site footers."""
    for value in values:
        if not isinstance(value, str):
            continue
        p = urlsplit(value.strip())
        if (p.scheme in {'https', 'http'} and p.hostname in {'creativecommons.org', 'www.creativecommons.org'}
                and re.fullmatch(r'/(?:licenses/(?:by|by-sa|by-nd|by-nc|by-nc-sa|by-nc-nd)/[1-4]\.0(?:/[a-z]{2})?|publicdomain/(?:mark|zero)/1\.0)/?', p.path)):
            return 'https://creativecommons.org' + p.path
    return ''


class OpenHTTP:
    requires_quota = False
    request_interval = 1.1

    def __init__(self, settings, session, files):
        self.settings, self.api, self.files = settings, session, files
        self.gate = asyncio.Lock()
        self.last = 0.0
        self.retry_at = 0.0
        self.retry_code = ''

    def allowed_url(self, url: str) -> str:
        """Subclasses must authorize every request and every redirect."""
        raise NotImplementedError

    def safe_url(self, url: str) -> str:
        p = urlsplit(url)
        decoded = unquote(p.path)
        if '\\' in decoded or any(part in {'.', '..'} for part in decoded.split('/')):
            raise UserError('Caminho de arquivo inválido.', 'unsafe_url')
        return validate_url(url)

    async def _get(self, url: str, limit=2_000_000, *, file=False):
        current = self.allowed_url(url)
        session = self.files if file else self.api
        try:
            for _ in range(5):
                async with self.gate:
                    if time.monotonic() < self.retry_at:
                        raise UserError('A fonte está em pausa após recusar o acesso. Escolha outra.', self.retry_code)
                    await asyncio.sleep(max(0, self.last + self.request_interval - time.monotonic()))
                    self.last = time.monotonic()
                async with session.get(current, allow_redirects=False,
                        headers={'User-Agent': 'LivrosBaltigo/0.5.1 (public ebook catalog client)'},
                        timeout=aiohttp.ClientTimeout(total=35, connect=10, sock_read=25)) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        from urllib.parse import urljoin
                        location = response.headers.get('Location')
                        if not location:
                            raise UserError('A fonte retornou um redirecionamento incompleto.', 'schema')
                        current = self.allowed_url(urljoin(current, location))
                        continue
                    if response.status in {401, 403, 429, 503, 513}:
                        self.retry_code = 'rate_limit' if response.status == 429 else 'blocked'
                        self.retry_at = time.monotonic() + max(60, retry_after_seconds(response.headers.get('Retry-After')))
                        raise UserError('A fonte recusou o acesso. Nenhum bloqueio foi contornado; escolha outro catálogo.', self.retry_code)
                    if response.status != 200:
                        raise UserError('O arquivo ou catálogo não está disponível agora.', 'file_missing' if response.status == 404 else 'unavailable')
                    length = response.headers.get('Content-Length', '')
                    if length.isdigit() and int(length) > limit:
                        raise UserError('O arquivo excede o limite de envio do bot.', 'file_too_large')
                    raw = await limited_body(response, limit)
                    # A server may close cleanly with truncated content when it reports no length.
                    return raw, response.headers.get('Content-Type', '').lower()
            raise UserError('A fonte redirecionou muitas vezes.', 'redirect_loop')
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            raise UserError('A fonte demorou a responder. Escolha outro catálogo ou tente mais tarde.', 'source_timeout') from exc

    async def download(self, url, extension, destination: Path, progress):
        try:
            raw, mime = await self._get(url, self.settings.max_file_bytes, file=True)
            if any(t in mime for t in ('text/html', 'application/json', 'javascript')):
                raise UserError('A fonte enviou uma página, não um livro.', 'invalid_file')
            if extension == 'pdf':
                if not raw.startswith(b'%PDF-') or b'%%EOF' not in raw[-4096:]:
                    raise UserError('O PDF recebido está incompleto ou não é válido.', 'invalid_file')
            elif extension == 'epub':
                try:
                    with zipfile.ZipFile(io.BytesIO(raw)) as package:
                        info = package.getinfo('mimetype')
                        if info.file_size > 128 or package.read(info) != b'application/epub+zip' or 'META-INF/container.xml' not in package.namelist():
                            raise ValueError('invalid EPUB structure')
                except (zipfile.BadZipFile, KeyError, ValueError, RuntimeError) as exc:
                    raise UserError('O EPUB recebido está incompleto ou não é válido.', 'invalid_file') from exc
            else:
                raise UserError('Escolha uma edição PDF ou EPUB.', 'unsupported_format')
            await progress(len(raw), len(raw))
            # A bounded <=49MB write is synchronous to prevent a cancelled thread
            # recreating a file after the worker has removed its temporary directory.
            destination.write_bytes(raw)
            return len(raw)
        except BaseException:
            destination.unlink(missing_ok=True)
            raise

    async def cover(self, url):
        if not url:
            return None
        try:
            raw, _ = await self._get(url, 2_000_000, file=True)
            return raw if raw.startswith((b'\xff\xd8\xff', b'\x89PNG\r\n\x1a\n')) else None
        except UserError:
            return None
