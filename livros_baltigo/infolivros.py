"""InfoLivros complete-book index and advertised free PDF links. No category articles."""
from __future__ import annotations
import asyncio
import io
import json
import re
import time
from collections import OrderedDict
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin, urlsplit
from .catalog import normalized, LANGUAGES
from .errors import UserError
from .models import Book, SearchPage, plain
from .open_http import OpenHTTP, document

BASE = 'https://infolivros.org'
SLUG = r'[a-z0-9]+(?:-[a-z0-9]+)*'


class InfoLivros(OpenHTTP):
    source = 'infolivros'
    supports_delivery = False  # Live PDF endpoint refused the server; do not retry as a download source.
    probe_query = 'Orgulho e Preconceito'

    def __init__(self, settings, session, files):
        super().__init__(settings, session, files)
        self.index = None
        self.index_until = 0.0
        self.index_lock = asyncio.Lock()
        self.record_lock = asyncio.Lock()
        self.records = OrderedDict()

    def allowed_url(self, url):
        self.safe_url(url)
        p = urlsplit(url)
        allowed = ((p.hostname in {'infolivros.org', 'www.infolivros.org'} and p.path.startswith('/livro/'))
                   or (p.hostname == 'pdf.infolivros.org' and p.path.startswith('/PT/') and p.path.lower().endswith('.pdf'))
                   or (p.hostname == 'cdn.infolibros.org' and p.path.startswith('/images/')))
        if not allowed or p.query or p.fragment:
            raise UserError('Endereço externo não autorizado nesta fonte.', 'unsafe_url')
        return url

    def parse_index(self, raw):
        soup = document(raw)
        links = soup.select('a.az-book-link')
        if not links or len(links) > 5000:
            raise UserError('O índice de livros completos mudou de formato.', 'layout_changed')
        books, seen = [], set()
        for link in links:
            url = self.allowed_url(urljoin(BASE, str(link.get('href', ''))))
            match = re.fullmatch('/livro/(' + SLUG + ')/', urlsplit(url).path)
            title = link.select_one('.az-book-title')
            author = link.select_one('.az-book-author')
            if not match or title is None or author is None:
                raise UserError('Uma entrada do catálogo veio sem título, autor ou identificação.', 'layout_changed')
            slug = match[1]
            if slug in seen:
                continue
            seen.add(slug)
            books.append(Book(slug, 'il_pdf', plain(title.get_text(' ', strip=True), 500),
                plain(author.get_text(' ', strip=True), 500), 'portuguese', 'pdf',
                source=self.source, source_url=BASE + '/livro/' + slug + '/', publisher='InfoLivros'))
        return books

    async def search(self, spec, page=1, limit=8):
        if spec.language not in LANGUAGES or spec.extension not in {'any', 'pdf', 'epub'}:
            raise UserError('Filtro inválido.', 'invalid_filter')
        if not 1 <= page <= 1000 or not 1 <= limit <= 10 or not 1 <= len(spec.query.strip()) <= 200:
            raise UserError('Busca inválida.', 'invalid_search')
        if spec.language not in {'portuguese', 'any'} or spec.extension == 'epub':
            return SearchPage([], page, False, source=self.source, notice='Esta integração consulta os livros completos em PDF e português do InfoLivros.')
        async with self.index_lock:
            if self.index is None or self.index_until <= time.monotonic():
                raw, _ = await self._get(BASE + '/livro/')
                self.index = self.parse_index(raw)
                self.index_until = time.monotonic() + 86400
            catalog = self.index
        query = normalized(spec.query)
        terms = [term for term in query.split() if term not in {'a','o','as','os','e','de','do','da','dos','das','por'}]
        if not terms:
            terms = query.split()
        ranked = []
        for book in catalog:
            title, author = normalized(book.title), normalized(book.author)
            if terms and all(term in title + ' ' + author for term in terms):
                rank = 0 if title == query else 1 if title.startswith(query) else 2 if all(t in title for t in terms) else 3
                ranked.append((rank, title, book.id, book))
        books = [item[-1] for item in sorted(ranked, key=lambda x: x[:3])]
        offset = (page - 1) * limit
        return SearchPage(books[offset:offset+limit], page, offset+limit < len(books), len(books), self.source,
            'Consulta de livros completos. O acesso é pelo site; envio de PDF pelo bot indisponível nesta fonte.')

    def parse_record(self, raw, book):
        soup = document(raw)
        metadata = None
        for script in soup.select('script[type="application/ld+json"]')[:20]:
            try:
                data = json.loads(script.get_text())
            except (ValueError, TypeError):
                continue
            candidates = data if isinstance(data, list) else data.get('@graph', [data]) if isinstance(data, dict) else []
            for item in candidates:
                if isinstance(item, dict) and item.get('@type') == 'Book':
                    metadata = item
                    break
        if not metadata or str(metadata.get('url', '')).rstrip('/') != book.source_url.rstrip('/'):
            raise UserError('A ficha não corresponde à obra selecionada.', 'schema')
        offers = metadata.get('offers')
        try:
            is_free = isinstance(offers, dict) and Decimal(str(offers.get('price', 'NaN'))) == 0
        except InvalidOperation:
            is_free = False
        if not is_free or metadata.get('inLanguage') not in {'pt', 'pt-BR', 'pt_BR'}:
            raise UserError('Esta edição não informa um PDF gratuito em português.', 'file_missing')
        links = soup.select('a.pdf-download[href]')
        urls = list(dict.fromkeys(self.allowed_url(str(a['href'])) for a in links))
        urls = [url for url in urls if urlsplit(url).hostname == 'pdf.infolivros.org']
        if len(urls) != 1:
            raise UserError('A ficha não oferece um link único de PDF completo.', 'file_missing')
        title = plain(metadata.get('name'), 500)
        if not title:
            raise UserError('A ficha não informou o título.', 'schema')
        author = metadata.get('author', {})
        if isinstance(author, list):
            author = ', '.join(plain(a, 200) for a in author)
        image = metadata.get('image', '')
        if isinstance(image, dict):
            image = image.get('url', '')
        try:
            cover = self.allowed_url(image) if image else ''
        except UserError:
            cover = ''
        fresh = replace(book, title=title, author=plain(author, 500) or book.author,
            description=plain(metadata.get('description'), 2500),
            year=plain(metadata.get('datePublished'), 20)[:4], cover=cover)
        return fresh, urls[0]

    async def _record(self, book):
        if book.source != self.source or not re.fullmatch(SLUG, book.id) or book.extension != 'pdf':
            raise UserError('Edição InfoLivros inválida.', 'invalid_book')
        expected = BASE + '/livro/' + book.id + '/'
        if book.source_url != expected:
            raise UserError('Origem da edição inválida.', 'invalid_book')
        async with self.record_lock:
            cached = self.records.get(book.key)
            if cached and cached[0] > time.monotonic():
                return cached[1:]
            raw, _ = await self._get(expected)
            fresh, url = self.parse_record(raw, book)
            self.records[book.key] = (time.monotonic()+21600, fresh, url)
            while len(self.records) > 128:
                self.records.popitem(last=False)
            return fresh, url

    async def details(self, book):
        if book.source != self.source or not re.fullmatch(SLUG, book.id) or book.source_url != BASE + '/livro/' + book.id + '/':
            raise UserError('Origem da edição inválida.', 'invalid_book')
        return replace(book, description='Acesso pelo site da fonte. O endpoint de PDF recusou o servidor; este catálogo não envia arquivos pelo bot.')

    async def file_info(self, book):
        raise UserError('Esta fonte está disponível apenas para consulta. Abra o livro no site.', 'external_only')

    async def download(self, url, extension, destination, progress):
        raise UserError('Esta fonte não envia arquivos pelo bot. Use Abrir no site.', 'external_only')

    async def cover(self, url):
        try:
            raw, mime = await self._get(url, 2_000_000, file=True)
            if raw.startswith((b'\xff\xd8\xff', b'\x89PNG\r\n\x1a\n')):
                return raw
            if raw.startswith(b'RIFF') and raw[8:12] == b'WEBP':
                from PIL import Image
                with Image.open(io.BytesIO(raw)) as image:
                    if image.width * image.height > 16_000_000:
                        return None
                    image.thumbnail((1280, 1280))
                    result = io.BytesIO()
                    image.convert('RGB').save(result, format='JPEG', quality=85)
                    return result.getvalue()
        except Exception:
            # Cover failure must not prevent reading or downloading the edition.
            pass
        return None
