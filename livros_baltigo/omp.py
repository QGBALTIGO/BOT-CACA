"""HTML adapters for university OMP catalogs and publisher-provided free downloads."""
from __future__ import annotations

import asyncio
import re
import time
from collections import OrderedDict
from dataclasses import replace
from urllib.parse import urlencode, urljoin, urlsplit

from .errors import UserError
from .gutenberg import LANGS
from .models import Book, SearchPage, plain
from .open_http import OpenHTTP, cc_license, document

CATALOGS = {
    'usp': ('https://www.livrosabertos.abcd.usp.br', '/portaldelivrosUSP', 'Livros Abertos USP'),
    'ufpb': ('https://www.editora.ufpb.br', '/press5/index.php/UFPB', 'Editora UFPB'),
}


class UniversityBooks(OpenHTTP):
    def __init__(self, settings, session, files, source='usp'):
        super().__init__(settings, session, files)
        if source not in CATALOGS:
            raise ValueError('Unknown university catalog')
        self.source = source
        self.origin, self.prefix, self.name = CATALOGS[source]
        self.host = urlsplit(self.origin).hostname
        self.probe_query = 'Pesquisa em educação' if source == 'usp' else 'Cuidados em saúde bucal'
        self.indices = OrderedDict()
        self.records = OrderedDict()
        self.index_lock = asyncio.Lock()
        self.record_lock = asyncio.Lock()

    def allowed_url(self, url):
        candidate = urljoin(self.origin + '/', url)
        self.safe_url(candidate)
        p = urlsplit(candidate)
        if p.hostname != self.host or not p.path.startswith((self.prefix + '/', '/public/', '/press5/public/')):
            raise UserError('Endereço externo não autorizado nesta fonte.', 'unsafe_url')
        return candidate

    def parse_search(self, raw):
        soup = document(raw)
        if not soup.select_one('.page_search'):
            raise UserError('A página de busca da universidade mudou de formato.', 'layout_changed')
        cards = soup.select('.page_search .obj_monograph_summary')
        if len(cards) > 1500:
            raise UserError('A busca retornou muitas obras. Refine o título ou autor.', 'invalid_search')
        result, seen = [], set()
        for card in cards:
            link = card.select_one('.title a[href]')
            if not link:
                raise UserError('Uma obra veio sem identificação.', 'layout_changed')
            url = self.allowed_url(str(link['href']))
            match = re.fullmatch(re.escape(self.prefix) + r'/catalog/book/(\d{1,9})/?', urlsplit(url).path)
            if not match:
                raise UserError('Identificação da obra universitária inválida.', 'layout_changed')
            number = match[1]
            if number not in seen:
                result.append(number)
                seen.add(number)
        # OMP returns all matching monographs in these verified catalogs. If a
        # future theme introduces pagination, do not silently lose its results.
        if soup.select_one('.page_search a.next[href], .page_search a[rel=next]'):
            raise UserError('A paginação da universidade mudou; refine a busca.', 'layout_changed')
        return result

    def parse_record(self, raw, number):
        soup = document(raw)
        block = soup.select_one('.obj_monograph_full')
        if block is None:
            raise UserError('A ficha da universidade mudou de formato.', 'layout_changed')
        def meta(name):
            return [str(n.get('content', '')).strip() for n in soup.select('meta[name]') if n.get('name') == name]
        if meta('DC.Identifier') and str(number) not in meta('DC.Identifier'):
            raise UserError('A ficha não corresponde ao livro selecionado.', 'schema')
        title_node = block.select_one('h1.title')
        title = plain(title_node.get_text(' ', strip=True), 500) if title_node else ''
        if not title:
            raise UserError('O título não foi informado pela fonte.', 'schema')
        licenses = meta('DC.Rights') + [str(a.get('href', '')) for a in block.select('.item.license a[href]')]
        license_url = cc_license(licenses)
        # UFPB also publishes explicitly free native PDFs with retained copyright,
        # rather than a Creative Commons license. Accept only its own matching
        # publisher/copyright metadata AND its advertised citation PDF endpoint.
        publisher_pdf = (self.source == 'ufpb'
            and 'Editora UFPB' in meta('DC.Source')
            and any('Editora UFPB' in value and 'Copyright' in value for value in meta('DC.Rights'))
            and bool(meta('citation_pdf_url')))
        if not license_url and not publisher_pdf:
            raise UserError('Esta edição não informa licença aberta ou download gratuito verificável da editora.', 'rights_unverified')
        lang = (next(iter(meta('DC.Language')), '') or next(iter(meta('citation_language')), '')).casefold()
        lang = lang.replace('-', '_').split('_')[0]
        language = next((name for name, code in LANGS.items() if code == lang), lang)
        authors = meta('citation_author') or meta('DC.Creator.PersonalName')
        author = plain(', '.join(authors), 500) or 'Autor não informado'
        abstract = block.select_one('.item.abstract .value')
        description = plain(abstract.get_text(' ', strip=True), 2200) if abstract else ''
        description += ('\nLicença: ' + license_url if license_url else
            '\nDownload disponibilizado pela Editora UFPB. '
            + plain(' '.join(meta('DC.Rights')), 300)
            + '. Acesso gratuito não significa domínio público ou licença para redistribuição.')
        image = block.select_one('.item.cover img[src]')
        cover = self.allowed_url(str(image['src'])) if image else ''
        year = next(iter(meta('citation_publication_date')), '')[:4]
        files = {}
        for link in block.select('.item.files a.cmp_download_link[href]'):
            label = link.get_text(' ', strip=True).casefold()
            extension = 'epub' if 'epub' in label else 'pdf' if 'pdf' in label else ''
            if not extension or extension in files:
                continue
            url = self.allowed_url(str(link['href']))
            p = urlsplit(url)
            pattern = re.escape(self.prefix) + r'/catalog/(?:view|download)/' + re.escape(number) + r'/\d+/\d+/?'
            if not re.fullmatch(pattern, p.path) or p.query or p.fragment:
                raise UserError('Link de edição universitária inválido.', 'unsafe_url')
            # OMP advertises its viewer; /download is the corresponding file
            # endpoint also present in citation_pdf_url for the verified edition.
            url = url.replace('/catalog/view/', '/catalog/download/')
            if not license_url and (extension != 'pdf' or url not in meta('citation_pdf_url')):
                continue
            book = Book(number, 'omp_' + extension, title, author, language, extension,
                        year=year, publisher=self.name, description=description,
                        cover=cover, source=self.source,
                        source_url=self.origin + self.prefix + '/catalog/book/' + number)
            files[extension] = (book, url)
        if not files:
            raise UserError('Esta ficha não oferece um PDF ou EPUB integral aberto.', 'file_missing')
        return files

    async def _record(self, number):
        if not isinstance(number, str) or not re.fullmatch(r'\d{1,9}', number):
            raise UserError('Obra universitária inválida.', 'invalid_book')
        async with self.record_lock:
            cached = self.records.get(number)
            if cached and cached[0] > time.monotonic():
                self.records.move_to_end(number)
                return cached[1]
            raw, _ = await self._get(self.origin + self.prefix + '/catalog/book/' + number)
            records = self.parse_record(raw, number)
            self.records[number] = (time.monotonic() + 21600, records)
            while len(self.records) > 256:
                self.records.popitem(last=False)
            return records

    async def search(self, spec, page=1, limit=8):
        if spec.language not in {*LANGS, 'any'} or spec.extension not in {'any', 'pdf', 'epub'}:
            raise UserError('Filtro inválido.', 'invalid_filter')
        if not 1 <= page <= 1000 or not 1 <= limit <= 10 or not 1 <= len(spec.query.strip()) <= 200:
            raise UserError('Busca inválida.', 'invalid_search')
        key = (spec.query.casefold(), spec.language, spec.extension)
        async with self.index_lock:
            snapshot = self.indices.get(key)
            if not snapshot or snapshot['expires'] <= time.monotonic():
                raw, _ = await self._get(self.origin + self.prefix + '/search?' + urlencode({'query': spec.query}))
                snapshot = {'expires': time.monotonic() + self.settings.search_ttl,
                            'ids': self.parse_search(raw), 'cursor': 0, 'books': [], 'skipped': 0}
                self.indices[key] = snapshot
                while len(self.indices) > 64:
                    self.indices.popitem(last=False)
            start = (page - 1) * limit
            stop = start + limit
            examined = 0
            while len(snapshot['books']) <= stop and snapshot['cursor'] < len(snapshot['ids']):
                if examined >= 12:
                    # Continue on the next page without claiming the source is empty.
                    if len(snapshot['books']) < stop:
                        raise UserError('Muitas obras sem este idioma/formato. Refine a busca ou amplie os filtros.', 'refine_search')
                    break
                number = snapshot['ids'][snapshot['cursor']]
                try:
                    records = await self._record(number)
                except UserError as exc:
                    if exc.code not in {'file_missing', 'rights_unverified'}:
                        raise
                    snapshot['skipped'] += 1
                    records = {}
                snapshot['cursor'] += 1
                examined += 1
                for extension in ('pdf', 'epub'):
                    if extension not in records or spec.extension not in {'any', extension}:
                        continue
                    book = records[extension][0]
                    if spec.language == 'any' or spec.language == book.language:
                        snapshot['books'].append(book)
            more = len(snapshot['books']) > stop or snapshot['cursor'] < len(snapshot['ids'])
            notice = ('Downloads disponibilizados pela Editora UFPB; direitos e origem na ficha.'
                      if self.source == 'ufpb' else
                      'Edições abertas da universidade; a licença e a origem constam da ficha.')
            if snapshot['skipped']:
                notice += ' Fichas sem arquivo integral ou acesso verificável foram excluídas.'
            return SearchPage(snapshot['books'][start:stop], page, more, source=self.source, notice=notice)

    async def details(self, book):
        if book.source != self.source:
            raise UserError('Fonte da edição inválida.', 'invalid_book')
        entry = (await self._record(book.id)).get(book.extension)
        if not entry:
            raise UserError('Este formato não está disponível; escolha outra edição.', 'file_missing')
        return entry[0]

    async def file_info(self, book):
        await self.details(book)
        entry = (await self._record(book.id))[book.extension]
        return entry[1], book.extension
