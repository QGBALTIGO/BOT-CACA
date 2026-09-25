"""HTML catalogue adapter. Ordinary authenticated HTTP; no block/quota bypass.

Selectors describe z-bookcard and legacy result cards documented by the
community client. A live check is still necessary when the site changes.
"""
from __future__ import annotations
import asyncio
import re
import time
from dataclasses import replace
from urllib.parse import quote, urlencode, urljoin, urlsplit
import aiohttp
from bs4 import BeautifulSoup
from .catalog import LANGUAGES, normalized
from .errors import UserError
from .models import Book, SearchPage, SearchSpec, parse_bytes
from .network import limited_body, validate_url
from .provider import ZLibrary


def same_origin(base: str, value: str) -> str:
    target = validate_url(urljoin(base + '/', value))
    a, b = urlsplit(base), urlsplit(target)
    if (a.hostname, a.port or 443) != (b.hostname, b.port or 443):
        raise UserError('A página redirecionou para outra origem. Nenhuma credencial foi enviada.', 'foreign_origin')
    return target


def html_document(raw: str):
    doc = BeautifulSoup(raw, 'html.parser')
    # A hidden login modal is common on result pages; it is NOT proof of logout.
    if doc.select_one('#challenge-form, #cf-challenge-running, .h-captcha, .g-recaptcha'):
        raise UserError('A fonte pediu verificação humana. A consulta foi interrompida.', 'captcha')
    title = doc.title.get_text(' ', strip=True).casefold() if doc.title else ''
    if title in {'just a moment...', 'attention required! | cloudflare'}:
        raise UserError('Verificação humana exigida pela fonte.', 'captcha')
    for item in doc(['script', 'style', 'noscript']):
        item.decompose()
    return doc


def text(node, selector):
    found = node.select_one(selector)
    return found.get_text(' ', strip=True) if found else ''


def parse_results(raw: str, base: str, page: int) -> SearchPage:
    doc = html_document(raw)
    cards = doc.select('z-bookcard')
    if not cards:
        cards = doc.select('#searchResultBox .resItemBox, #searchResultBox .book-item')
    if not cards:
        if doc.select_one('#searchResultBox .notFound, .search-empty, #searchResultBox.notFound, .notFound'):
            return SearchPage([], page, False)
        code = 'html_auth' if doc.select_one('form input[type=password]') else 'layout_changed'
        raise UserError('Não foi possível reconhecer a lista de livros no HTML da fonte.', code)
    books, seen = [], set()
    for card in cards[:100]:
        anchor = card.select_one('a[href*="/book/"]')
        href = str(card.get('href') or (anchor.get('href') if anchor else '') or '')
        candidate = same_origin(base, href)
        match = re.match(r'^/book/(\d{1,20})/([A-Za-z0-9_-]{1,100})(?:/|$)', urlsplit(candidate).path)
        if not match:
            raise UserError('Um resultado da fonte perdeu seu identificador de edição.', 'layout_changed')
        title = text(card, '[slot=title], h3, [itemprop=name]') or str(card.get('title') or '')
        if not title and anchor:
            title = anchor.get_text(' ', strip=True)
        if not title:
            raise UserError('Um resultado da fonte veio sem título.', 'layout_changed')
        image = card.select_one('img[data-src], img[src]')
        cover = str(image.get('data-src') or image.get('src') or '') if image else ''
        item = Book.from_source({
            'id': match[1], 'hash': match[2], 'title': title,
            'author': text(card, '[slot=author], .authors, [itemprop=author]'),
            'language': card.get('language') or text(card, '.property_language .property_value'),
            'extension': card.get('extension') or text(card, '.property__file .property_value').split(',')[0],
            'year': card.get('year') or text(card, '.property_year .property_value'),
            'publisher': card.get('publisher') or '',
            'filesize': card.get('filesize') or card.get('size'),
            'cover': urljoin(base + '/', cover) if cover else '',
        })
        if item.key not in seen:
            books.append(item)
            seen.add(item.key)
    next_link = doc.select_one('a[rel=next], .paginator .next a, a.paginator-next')
    script_pages = re.search(r'pagesTotal\s*:\s*(\d+)', raw)
    if script_pages:
        more = page < int(script_pages[1])
    else:
        numbers = []
        for link in doc.select('.paginator a[href], .pagination a[href]'):
            found = re.search(r'[?&]page=(\d+)', str(link['href']))
            if found: numbers.append(int(found[1]))
        more = next_link is not None or any(n > page for n in numbers)
    return SearchPage(books, page, more)


def parse_details(raw: str, base: str, book: Book) -> Book:
    doc = html_document(raw)
    title = text(doc, 'h1[itemprop=name], h1')
    if not title:
        raise UserError('A ficha do livro mudou de formato.', 'layout_changed')
    image = doc.select_one('.book-cover img, img[itemprop=image]')
    cover = str(image.get('src') or image.get('data-src') or '') if image else book.cover
    return replace(book, title=title[:500],
        author=text(doc, '.bookAuthors, .book-authors') or book.author,
        description=text(doc, '#bookDescriptionBox, [itemprop=description]')[:3000] or book.description,
        publisher=text(doc, '.property_publisher .property_value') or book.publisher,
        year=text(doc, '.property_year .property_value') or book.year,
        language=text(doc, '.property_language .property_value') or book.language,
        cover=urljoin(base + '/', cover) if cover else '')


class HTMLSource(ZLibrary):
    def __init__(self, settings, api_session, file_session):
        super().__init__(settings, api_session, file_session)
        self.html_lock = asyncio.Lock()
        self.last_html = 0.0
        self.html_pages = {}

    async def get_html(self, path: str) -> str:
        await self.ensure_login()
        current = same_origin(self.base, path)
        async with self.html_lock:
            if time.monotonic() < self.retry_at:
                raise UserError('A fonte pediu uma pausa. Tente mais tarde.', 'rate_limit')
            await asyncio.sleep(max(0, 1 - (time.monotonic() - self.last_html)))
            self.last_html = time.monotonic()
            try:
                for _ in range(4):
                    async with self.api.get(current, headers={**self._headers(), 'Accept': 'text/html'}, allow_redirects=False) as response:
                        code = response.status
                        if code in {301, 302, 303, 307, 308}:
                            location = response.headers.get('Location', '')
                            if not location:
                                raise UserError('Redirecionamento sem destino.', 'html_redirect')
                            current = same_origin(self.base, urljoin(current, location))
                            continue
                        if code in {401, 403, 429}:
                            self.retry_at = time.monotonic() + 60
                            error = {401: 'html_auth', 403: 'blocked', 429: 'rate_limit'}[code]
                            raise UserError('A fonte não autorizou esta consulta. Nenhum bloqueio será contornado.', error)
                        if code != 200:
                            raise UserError('A página solicitada não está disponível.', 'not_found' if code == 404 else 'unavailable')
                        mime = response.headers.get('Content-Type', '').lower()
                        if mime and not any(x in mime for x in ('text/html', 'application/xhtml+xml')):
                            raise UserError('A fonte não retornou uma página HTML.', 'not_html')
                        raw = await limited_body(response, 2_000_000)
                    value = raw.decode('utf-8', errors='replace')
                    html_document(value)
                    return value
                raise UserError('A página redirecionou muitas vezes.', 'redirect_loop')
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                self.retry_at = time.monotonic() + 30
                code = 'source_dns' if isinstance(getattr(exc, 'os_error', None), OSError) and getattr(exc.os_error, 'errno', 0) in {-2, -3, 11001} else 'network'
                raise UserError('O servidor não conseguiu alcançar o endereço da fonte.', code) from exc

    async def search(self, spec: SearchSpec, page=1, limit=8):
        if not 1 <= page <= 1000 or not 1 <= limit <= 10 or not 1 <= len(spec.query.strip()) <= 200:
            raise UserError('Busca inválida.', 'invalid_search')
        if spec.language not in LANGUAGES or spec.extension not in {'any', 'epub', 'pdf'}:
            raise UserError('Filtro inválido.', 'invalid_filter')
        # One site page may contain 50 books. Keep all of them and paginate locally
        # instead of dropping books after the first eight.
        source_page, offset = 1, (page - 1) * limit
        selected, fetched = [], 0
        while source_page <= 100:
            cache_key = (spec.query.casefold(), spec.language, spec.extension, source_page)
            entry = self.html_pages.get(cache_key)
            if entry and entry[0] > time.monotonic():
                chunk = entry[1]
            else:
                if fetched >= 2:
                    raise UserError('A paginação expirou. Refaça a busca para continuar.', 'expired_search')
                params = [('page', str(source_page))]
                if spec.language != 'any': params.append(('languages[]', spec.language))
                for extension in ([spec.extension] if spec.extension != 'any' else ['epub', 'pdf']):
                    params.append(('extensions[]', extension))
                html = await self.get_html('/s/' + quote(spec.query, safe='') + '?' + urlencode(params))
                fetched += 1
                chunk = parse_results(html, self.base, source_page)
                self.html_pages[cache_key] = (time.monotonic() + self.settings.search_ttl, chunk)
                if len(self.html_pages) > 128:
                    self.html_pages.pop(next(iter(self.html_pages)))
            if offset >= len(chunk.books):
                offset -= len(chunk.books)
            else:
                take = min(limit - len(selected), len(chunk.books) - offset)
                selected.extend(chunk.books[offset:offset + take])
                offset += take
                if len(selected) == limit:
                    return SearchPage(selected, page, offset < len(chunk.books) or chunk.has_next)
                offset = 0
            if not chunk.has_next:
                return SearchPage(selected, page, False)
            source_page += 1
        raise UserError('Refine a busca para consultar menos páginas.', 'invalid_page')

    async def details(self, book: Book):
        return parse_details(await self.get_html(f'/book/{book.id}/{book.hash}'), self.base, book)

    async def file_info(self, book: Book):
        # File delivery still uses the authenticated file endpoint, which exposes
        # the selected edition and is subject to the SAME account quota.
        return await super().file_info(book)
