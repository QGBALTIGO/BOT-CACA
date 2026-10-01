"""Project Gutenberg's documented OPDS/RDF interface; no account credentials.

The generated PDF is explicitly identified as a typeset copy of the full text.
Search metadata and EPUB files retain their actual source. Requests are bounded,
paced, cached and never retry access denials through proxies or mirrors.
"""
from __future__ import annotations
import asyncio
import re
import time
import zipfile
from collections import OrderedDict
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit, unquote
from xml.etree import ElementTree as ET
import aiohttp
from .errors import UserError
from .models import Book, SearchPage, SearchSpec, Quota, plain, to_int
from .network import limited_body, validate_url
from .availability import retry_after_seconds
from .pdfbook import text_to_pdf

BASE = 'https://www.gutenberg.org'
HOSTS = frozenset({'www.gutenberg.org','gutenberg.org'})
NS = {'a':'http://www.w3.org/2005/Atom','d':'http://purl.org/dc/terms/',
      'p':'http://www.gutenberg.org/2009/pgterms/','r':'http://www.w3.org/1999/02/22-rdf-syntax-ns#'}
R = '{'+NS['r']+'}'
LANGS = {'portuguese':'pt','english':'en','spanish':'es','french':'fr','italian':'it',
         'german':'de','russian':'ru','chinese':'zh','japanese':'ja'}
PDF_LANGS = {'portuguese','english','spanish','french','italian','german'}


def pg_url(url: str) -> str:
    parts_before_join = urlsplit(url)
    decoded_path = unquote(parts_before_join.path)
    if any(piece in {'.', '..'} for piece in decoded_path.split('/')) or '\\' in decoded_path:
        raise UserError('Caminho Gutenberg inseguro.', 'unsafe_url')
    target = urljoin(BASE+'/', url)
    parts = urlsplit(target)
    # Old RDF records name the same public host over HTTP; request HTTPS only.
    if parts.scheme == 'http' and parts.hostname in HOSTS:
        target = 'https://' + target[len('http://'):]
    validate_url(target, HOSTS)
    if not urlsplit(target).path.startswith(('/ebooks/', '/cache/epub/', '/files/')):
        raise UserError('Caminho de arquivo Gutenberg não autorizado.', 'unsafe_url')
    return target


def xml(raw: bytes, root: str):
    if len(raw)>2_000_000 or b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise UserError('XML do catálogo recusado por segurança.', 'schema')
    try:
        node = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise UserError('O catálogo Gutenberg retornou uma resposta inválida.', 'schema') from exc
    if node.tag != root:
        raise UserError('O catálogo Gutenberg não retornou o formato esperado.', 'schema')
    return node


def parse_feed(raw: bytes, spec: SearchSpec) -> tuple[list[Book], str]:
    feed = xml(raw, '{'+NS['a']+'}feed')
    books, seen = [], set()
    for entry in feed.findall('a:entry',NS)[:100]:
        ident = entry.findtext('a:id','',NS).strip()
        # The live OPDS feed encodes an empty search as a navigation entry.
        # It is not an ebook; do not reject the entire source as malformed.
        empty_title = plain(entry.findtext('a:title','',NS), 100).casefold()
        if (empty_title == 'no records found.'
                and ident in {BASE+'/ebooks.opds/', 'http://www.gutenberg.org/ebooks.opds/'}
                and any(link.get('rel') == 'subsection' and link.get('href') == '/ebooks.opds/'
                        for link in entry.findall('a:link', NS))):
            continue
        match = re.fullmatch(r'https?://(?:www\.)?gutenberg\.org/ebooks/(\d{1,9})(?:\.opds)?',ident)
        if not match:
            raise UserError('Identificador inválido na busca Gutenberg.', 'schema')
        number=match[1]
        if number in seen:continue
        seen.add(number)
        title = plain(entry.findtext('a:title','',NS),500)
        suffix = re.search(r'\s*\(([^()]+)\)\s*$',title)
        language = suffix.group(1).casefold() if suffix else 'english'
        if suffix and language in LANGS:
            title=title[:suffix.start()]
        elif suffix:
            language='english'
        if spec.language != 'any' and language != spec.language:continue
        author=plain(entry.findtext('a:content','',NS),500) or 'Autor não informado'
        for extension in (['pdf','epub'] if spec.extension=='any' else [spec.extension]):
            if extension=='pdf' and language not in PDF_LANGS:continue
            books.append(Book(number,'pg_'+extension,title,author,language,extension,
                              source='gutenberg',publisher='Project Gutenberg',
                              source_url=f'{BASE}/ebooks/{number}',
                              description=('PDF preparado a partir do texto integral; não é um fac-símile.'
                                           if extension=='pdf' else 'Arquivo EPUB da edição Project Gutenberg.')))
    next_url=''
    for link in feed.findall('a:link',NS):
        if link.get('rel')=='next':
            next_url=pg_url(link.get('href',''))
            if urlsplit(next_url).path.rstrip('/')!='/ebooks/search.opds':
                raise UserError('Paginação Gutenberg inválida.', 'schema')
    return books,next_url


def parse_rdf(raw: bytes, book: Book) -> tuple[Book, str]:
    root=xml(raw, '{'+NS['r']+'}RDF')
    ebook=root.find('p:ebook',NS)
    if ebook is None or ebook.get(R+'about') not in {f'ebooks/{book.id}',f'{BASE}/ebooks/{book.id}',f'http://www.gutenberg.org/ebooks/{book.id}'}:
        raise UserError('A ficha não corresponde à edição escolhida.', 'schema')
    rights=ebook.findtext('d:rights','',NS)
    if 'public domain' not in rights.casefold():
        raise UserError('Esta edição não foi identificada como domínio público pela fonte.', 'rights_unverified')
    title=plain(ebook.findtext('d:title','',NS),500)
    if not title:raise UserError('Título ausente na ficha Gutenberg.', 'schema')
    authors=[plain(e.findtext('p:name','',NS),200) for e in ebook.findall('d:creator/p:agent',NS)]
    lang=ebook.findtext('d:language/r:Description/r:value','',NS).casefold()
    language=next((name for name,code in LANGS.items() if code==lang),lang or book.language)
    choices=[];cover=''
    for item in ebook.findall('d:hasFormat/p:file',NS):
        url=item.get(R+'about','')
        types=[x.text or '' for x in item.findall('d:format/r:Description/r:value',NS)]
        if any(t.startswith('image/jpeg') for t in types) and '.cover.medium.' in url:
            cover=pg_url(url)
        wanted = ('text/plain' if book.extension=='pdf' else 'application/epub+zip')
        if any(t.startswith(wanted) for t in types):
            priority=(0 if '.txt.utf-8' in url or '.epub3.' in url else 1)
            choices.append((priority,pg_url(url),to_int(item.findtext('d:extent','',NS))))
    if not choices:
        raise UserError('Este formato não está disponível nesta obra. Escolha outra edição.', 'file_missing')
    _,url,size=sorted(choices,key=lambda x:x[0])[0]
    if book.extension=='pdf' and language not in PDF_LANGS:
        raise UserError('Use EPUB para preservar os caracteres desta edição.', 'conversion_alphabet')
    note=('PDF preparado a partir do texto integral do Project Gutenberg, com créditos e licença. Não é um fac-símile da edição impressa.'
          if book.extension=='pdf' else 'EPUB original disponibilizado pelo Project Gutenberg, com créditos e licença da edição.')
    return replace(book,title=title,author=', '.join(filter(None,authors)) or book.author,
                   language=language,cover=cover,publisher='Project Gutenberg',
                   description=note,size=size if book.extension=='epub' else None,
                   source_url=f'{BASE}/ebooks/{book.id}'),url


class Gutenberg:
    requires_quota=False
    def __init__(self, settings, session, files):
        self.settings,self.api,self.files=settings,session,files
        self.pages=OrderedDict();self.records=OrderedDict()
        self.gate=asyncio.Lock();self.last=0.0;self.retry_at=0.0;self.retry_code=''

    async def _get(self,url: str,limit: int=2_000_000, *, file=False):
        if time.monotonic()<self.retry_at:
            raise UserError('Gutenberg está em pausa após uma recusa temporária. Tente mais tarde.',self.retry_code)
        current=pg_url(url)
        session=self.files if file else self.api
        async with self.gate:
            await asyncio.sleep(max(0,self.last+2.05-time.monotonic()))
            self.last=time.monotonic()
        try:
            for _ in range(5):
                async with session.get(current,allow_redirects=False,
                    headers={'User-Agent':'LivrosBaltigo/0.5 (OPDS ebook client)'},
                    timeout=aiohttp.ClientTimeout(total=40,connect=12,sock_read=25)) as response:
                    if response.status in {301,302,303,307,308}:
                        location=response.headers.get('Location')
                        if not location:raise UserError('Redirecionamento incompleto.', 'schema')
                        current=pg_url(urljoin(current,location));continue
                    if response.status in {403,429,503}:
                        self.retry_code='rate_limit' if response.status==429 else 'blocked'
                        self.retry_at=time.monotonic()+retry_after_seconds(response.headers.get('Retry-After'))
                        raise UserError('Gutenberg recusou temporariamente a consulta. Nenhum bloqueio foi contornado.',self.retry_code)
                    if response.status!=200:
                        raise UserError('A edição Gutenberg não está disponível neste momento.','file_missing' if response.status==404 else 'unavailable')
                    return await limited_body(response,limit),response.headers.get('Content-Type','').lower()
            raise UserError('Excesso de redirecionamentos Gutenberg.','redirect_loop')
        except (aiohttp.ClientError,asyncio.TimeoutError,OSError) as exc:
            raise UserError('A conexão com Gutenberg não respondeu. Tente mais tarde.','source_timeout') from exc

    async def search(self,spec: SearchSpec,page=1,limit=8):
        if spec.language not in {*LANGS,'any'} or spec.extension not in {'any','pdf','epub'}:
            raise UserError('Filtro Gutenberg inválido.','invalid_filter')
        if not 1<=page<=1000 or not 1<=limit<=10 or not 1<=len(spec.query.strip())<=200:
            raise UserError('Busca Gutenberg inválida.','invalid_search')
        query=spec.query+((' l.'+LANGS[spec.language]) if spec.language!='any' else '')
        url=BASE+'/ebooks/search.opds/?'+urlencode({'query':query})
        offset=(page-1)*limit;selected=[];fetched=0;visited=set()
        for _ in range(150):
            if url in visited:raise UserError('Paginação circular recusada.','schema')
            visited.add(url)
            key=(url,spec.extension,spec.language)
            cached=self.pages.get(key)
            if cached and cached[0]>time.monotonic():
                books,next_url=cached[1:]
            else:
                if fetched>=2:
                    raise UserError('A paginação anterior expirou. Refaça a busca para continuar.','expired_search')
                raw,_=await self._get(url);fetched+=1
                books,next_url=parse_feed(raw,spec)
                self.pages[key]=(time.monotonic()+self.settings.search_ttl,books,next_url)
                while len(self.pages)>128:self.pages.popitem(last=False)
            if offset>=len(books):offset-=len(books)
            else:
                take=min(limit-len(selected),len(books)-offset)
                selected.extend(books[offset:offset+take]);offset+=take
                if len(selected)==limit:
                    return SearchPage(selected,page,offset<len(books) or bool(next_url),source='gutenberg')
                offset=0
            if not next_url:return SearchPage(selected,page,False,source='gutenberg')
            url=next_url
        raise UserError('Refine a busca para consultar menos páginas.','invalid_page')

    async def _record(self,book):
        if book.source!='gutenberg' or not re.fullmatch(r'\d{1,9}',book.id) or book.extension not in {'pdf','epub'}:
            raise UserError('Edição Gutenberg inválida.','invalid_book')
        cached=self.records.get(book.key)
        if cached and cached[0]>time.monotonic():return cached[1:]
        raw,_=await self._get(f'{BASE}/cache/epub/{book.id}/pg{book.id}.rdf')
        fresh,url=parse_rdf(raw,book)
        self.records[book.key]=(time.monotonic()+21600,fresh,url)
        while len(self.records)>128:self.records.popitem(last=False)
        return fresh,url

    async def details(self,book):return (await self._record(book))[0]

    async def file_info(self,book):
        fresh,url=await self._record(book)
        return url,fresh.extension

    async def quota(self):return Quota(None,None)  # Not metered; never invent a balance.

    async def cover(self,url):
        if not url:return None
        try:
            raw,_=await self._get(url,2_000_000,file=True)
            return raw if raw.startswith((b'\xff\xd8\xff',b'\x89PNG\r\n\x1a\n')) else None
        except UserError:return None

    async def download(self,url,extension,destination,progress):
        try:
            maximum=min(self.settings.max_file_bytes,2_000_000) if extension=='pdf' else self.settings.max_file_bytes
            raw,mime=await self._get(url,maximum,file=True)
            if any(x in mime for x in ('text/html','application/json','javascript')):
                raise UserError('A fonte retornou uma página no lugar do livro.','invalid_file')
            await progress(len(raw),len(raw))
            if extension=='pdf':
                try:text=raw.decode('utf-8-sig')
                except UnicodeDecodeError as exc:
                    raise UserError('A codificação do texto não permite conversão fiel. Escolha EPUB.','invalid_file') from exc
                if '*** START OF THE PROJECT GUTENBERG EBOOK' not in text or '*** END OF THE PROJECT GUTENBERG EBOOK' not in text:
                    raise UserError('O texto recebido não contém os marcadores de integridade da edição.','incomplete_file')
                title_match=re.search(r'^Title:\s*(.+)$',text,re.M)
                author_match=re.search(r'^Author:\s*(.+)$',text,re.M)
                title=title_match.group(1).strip() if title_match else 'Edição Project Gutenberg'
                author=author_match.group(1).strip() if author_match else 'Project Gutenberg'
                conversion=asyncio.create_task(asyncio.to_thread(text_to_pdf,text,destination,title,author))
                try:
                    count=await asyncio.shield(conversion)
                except asyncio.CancelledError:
                    # Finish disk work before the caller cleans its TemporaryDirectory.
                    await asyncio.gather(conversion,return_exceptions=True)
                    raise
            elif extension=='epub':
                if not raw.startswith(b'PK\x03\x04'):raise UserError('O arquivo não é EPUB.','invalid_file')
                await asyncio.to_thread(destination.write_bytes,raw)
                with zipfile.ZipFile(destination) as package:
                    item=package.getinfo('mimetype')
                    if item.file_size>128 or package.read(item)!=b'application/epub+zip' or 'META-INF/container.xml' not in package.namelist():
                        raise UserError('Estrutura EPUB inválida.','invalid_file')
                count=len(raw)
            else:raise UserError('Formato não suportado.','unsupported_format')
            if count>self.settings.max_file_bytes:raise UserError('O arquivo excede o limite do bot.','file_too_large')
            return count
        except (zipfile.BadZipFile,KeyError) as exc:
            destination.unlink(missing_ok=True)
            raise UserError('O arquivo EPUB veio incompleto.','invalid_file') from exc
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
