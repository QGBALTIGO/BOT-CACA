"""Internet Archive public-domain texts only; no loans, login, or DRM removal."""
from __future__ import annotations

import asyncio
import re
import time
from collections import OrderedDict
from dataclasses import replace
from urllib.parse import quote, urlencode, urlsplit

from .errors import UserError
from .gutenberg import LANGS
from .models import Book, SearchPage, plain, to_int
from .open_http import OpenHTTP, cc_license, read_json

BASE = 'https://archive.org'
IA_LANGS = {'portuguese': 'por', 'english': 'eng', 'spanish': 'spa', 'french': 'fre',
            'italian': 'ita', 'german': 'ger', 'russian': 'rus', 'chinese': 'chi', 'japanese': 'jpn'}
IDENT = r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}'


def values(value):
    return value if isinstance(value, list) else [value] if value is not None else []


def public_domain(metadata):
    license_url = cc_license(values(metadata.get('licenseurl')))
    return bool(license_url and '/publicdomain/' in license_url)


def restricted(metadata):
    return str(metadata.get('access-restricted-item', '')).lower() in {'true', '1'} or any(
        tag in {'inlibrary', 'printdisabled', 'borrowable', 'stream_only'} for tag in values(metadata.get('collection')))


class InternetArchive(OpenHTTP):
    source = 'archive'
    probe_query = 'Dom Casmurro'

    def __init__(self, settings, session, files):
        super().__init__(settings, session, files)
        self.pages = OrderedDict()
        self.records = OrderedDict()
        self.record_lock = asyncio.Lock()

    def allowed_url(self, url):
        self.safe_url(url)
        p = urlsplit(url)
        if p.hostname in {'archive.org', 'www.archive.org'}:
            good = p.path == '/advancedsearch.php' or p.path.startswith(('/metadata/', '/download/', '/services/img/'))
        else:
            good = bool(re.fullmatch(r'(?:ia|dn)\d+\.[a-z0-9-]+\.archive\.org', p.hostname or '')) and (p.path.startswith(('/download/', '/items/')) or bool(re.match(r'^/\d{1,3}/items/', p.path)))
        if not good:
            raise UserError('Endereço externo não autorizado pelo Internet Archive.', 'unsafe_url')
        return url

    def parse_search(self, raw, spec):
        data = read_json(raw)
        response = data.get('response')
        if not isinstance(response, dict) or not isinstance(response.get('docs'), list):
            raise UserError('A busca Archive mudou de formato.', 'schema')
        total = to_int(response.get('numFound'))
        if total is None or total < 0:
            raise UserError('O catálogo não informou uma contagem válida.', 'schema')
        books, seen = [], set()
        for row in response['docs']:
            number = row.get('identifier', '')
            if not isinstance(number, str) or not re.fullmatch(IDENT, number):
                raise UserError('Identificação Archive inválida.', 'schema')
            if number in seen or not public_domain(row) or restricted(row):
                continue
            seen.add(number)
            title = plain(row.get('title'), 500)
            if not title:
                raise UserError('Obra Archive sem título.', 'schema')
            lang = next(iter(values(row.get('language'))), '')
            language = next((name for name in IA_LANGS if str(lang).casefold() in {IA_LANGS[name], LANGS[name], name}), str(lang))
            if spec.language != 'any' and language != spec.language:
                continue
            formats = [str(v).casefold() for v in values(row.get('format'))]
            for extension in ('pdf', 'epub'):
                present = any((fmt in {'pdf', 'text pdf', 'image container pdf', 'additional text pdf'} if extension == 'pdf' else fmt == 'epub') for fmt in formats)
                if not present or spec.extension not in {'any', extension}:
                    continue
                books.append(Book(number, 'ia_' + extension, title,
                    plain(row.get('creator'), 500) or 'Autor não informado', language, extension,
                    year=plain(row.get('year'), 12), publisher='Internet Archive',
                    description='Obra identificada como domínio público pela fonte. Edições para empréstimo não são oferecidas.',
                    source='archive', source_url=BASE + '/details/' + number))
        return books, total

    async def search(self, spec, page=1, limit=8):
        if spec.language not in {*IA_LANGS, 'any'} or spec.extension not in {'any', 'pdf', 'epub'}:
            raise UserError('Filtro Archive inválido.', 'invalid_filter')
        if not 1 <= page <= 1000 or not 1 <= limit <= 10 or not 1 <= len(spec.query.strip()) <= 200:
            raise UserError('Busca Archive inválida.', 'invalid_search')
        terms = ' AND '.join(re.findall(r'\w+', spec.query, re.UNICODE))
        if not terms:
            return SearchPage([], page, False, source='archive')
        query = (f'mediatype:texts AND (title:({terms}) OR creator:({terms})) '
                 'AND licenseurl:*publicdomain* AND -collection:inlibrary '
                 'AND -collection:printdisabled AND -access-restricted-item:true')
        if spec.language != 'any':
            query += f' AND (language:{IA_LANGS[spec.language]} OR language:{LANGS[spec.language]})'
        offset = (page - 1) * limit
        selected, fetched = [], 0
        for source_page in range(1, 151):
            key = (query, spec.extension, source_page)
            cached = self.pages.get(key)
            if cached and cached[0] > time.monotonic():
                books, total = cached[1:]
            else:
                if fetched >= 3:
                    raise UserError('A paginação anterior expirou. Refaça a busca.', 'expired_search')
                params = {'q': query, 'output': 'json', 'rows': 25, 'page': source_page,
                          'sort[]': 'identifier asc',
                          'fl[]': 'identifier,title,creator,language,licenseurl,rights,format,year,collection,access-restricted-item'}
                raw, _ = await self._get(BASE + '/advancedsearch.php?' + urlencode(params))
                books, total = self.parse_search(raw, spec)
                self.pages[key] = (time.monotonic() + self.settings.search_ttl, books, total)
                fetched += 1
                while len(self.pages) > 128:
                    self.pages.popitem(last=False)
            more = source_page * 25 < total
            if offset >= len(books):
                offset -= len(books)
            else:
                take = min(limit - len(selected), len(books) - offset)
                selected.extend(books[offset:offset + take])
                offset += take
                if len(selected) == limit:
                    return SearchPage(selected, page, offset < len(books) or more, source='archive')
                offset = 0
            if not more:
                return SearchPage(selected, page, False, source='archive')
        raise UserError('Refine a busca do Archive.', 'invalid_page')

    def parse_record(self, raw, book):
        data = read_json(raw)
        metadata = data.get('metadata')
        if not isinstance(metadata, dict) or metadata.get('identifier') != book.id:
            raise UserError('A ficha Archive não corresponde à obra escolhida.', 'schema')
        if restricted(metadata) or data.get('is_dark') or not public_domain(metadata):
            raise UserError('Esta edição não é um arquivo público de domínio público. Empréstimos e arquivos restritos não são oferecidos.', 'rights_unverified')
        files = data.get('files')
        if not isinstance(files, list):
            raise UserError('A fonte não informou os arquivos da obra.', 'schema')
        choices = []
        for file in files:
            name = file.get('name', '')
            if (not isinstance(name, str) or not name.lower().endswith('.' + book.extension)
                    or str(file.get('private', '')).lower() in {'true', '1'}
                    or 'encrypted' in str(file.get('format', '')).lower()):
                continue
            size = to_int(file.get('size'))
            if size is not None and 0 < size <= self.settings.max_file_bytes:
                choices.append((size, name))
        if not choices:
            raise UserError('Nenhum arquivo deste formato cabe no limite do bot. Escolha outra edição.', 'file_missing')
        size, name = min(choices)
        url = self.allowed_url(BASE + '/download/' + book.id + '/' + quote(name, safe=''))
        license_url = cc_license(values(metadata.get('licenseurl')))
        fresh = replace(book, title=plain(metadata.get('title'), 500) or book.title,
            author=plain(metadata.get('creator'), 500) or book.author,
            description=plain(metadata.get('description'), 2200) + '\nLicença: ' + license_url,
            size=size, cover=BASE + '/services/img/' + book.id)
        return fresh, url

    async def _record(self, book):
        if book.source != 'archive' or not re.fullmatch(IDENT, book.id) or book.extension not in {'pdf', 'epub'}:
            raise UserError('Edição Archive inválida.', 'invalid_book')
        async with self.record_lock:
            cached = self.records.get(book.key)
            if cached and cached[0] > time.monotonic():
                return cached[1:]
            raw, _ = await self._get(BASE + '/metadata/' + book.id)
            fresh, url = self.parse_record(raw, book)
            self.records[book.key] = (time.monotonic() + 21600, fresh, url)
            while len(self.records) > 128:
                self.records.popitem(last=False)
            return fresh, url

    async def details(self, book):
        return (await self._record(book))[0]

    async def file_info(self, book):
        fresh, url = await self._record(book)
        return url, fresh.extension
