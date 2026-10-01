from __future__ import annotations

import asyncio
import io
import json
import sqlite3
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from livros_baltigo.catalogs import CatalogRouter
from livros_baltigo.gutenberg import Gutenberg, parse_feed, parse_rdf, pg_url, xml, NS
from livros_baltigo.models import Book, SearchSpec, SearchPage
from livros_baltigo.pdfbook import text_to_pdf
from livros_baltigo.operations import OperationsApp
from livros_baltigo.errors import UserError
from livros_baltigo.storage import Store, SCHEMA
from livros_baltigo import views

FIXTURES = Path(__file__).parent / 'fixtures' / 'gutenberg'

@pytest.fixture
def pgbook():
    return Book('55752','pg_pdf','Dom Casmurro','Machado de Assis',language='portuguese',extension='pdf',source='gutenberg')

@pytest.fixture
def public(pgbook):
    p = AsyncMock()
    p.requires_quota = False
    p.search.return_value = SearchPage([pgbook],1,False,source='gutenberg')
    p.details.return_value = pgbook
    p.file_info.return_value = ('https://www.gutenberg.org/ebooks/55752.txt.utf-8','pdf')
    p.cover.return_value = None
    async def download(url,ext,path,progress):
        path.write_bytes(b'%PDF-1.4\n% deterministic unit-test fixture\n%%EOF')
        await progress(path.stat().st_size,path.stat().st_size)
    p.download.side_effect = download
    return p

@pytest.mark.parametrize('ext,count',[('any',2),('pdf',1),('epub',1)])
def test_actual_opds_metadata(ext,count):
    books,nxt = parse_feed((FIXTURES/'search.xml').read_bytes(),SearchSpec('Dom Casmurro',extension=ext))
    assert len(books)==count and all(b.id=='55752' and b.source=='gutenberg' for b in books)
    assert books[0].title=='Dom Casmurro'
    assert not nxt

@pytest.mark.parametrize('ext,end',[('pdf','.txt.utf-8'),('epub','.epub3.images')])
def test_actual_rdf_metadata(pgbook,ext,end):
    book,url=parse_rdf((FIXTURES/'55752.rdf').read_bytes(),replace(pgbook,extension=ext))
    assert book.language=='portuguese' and book.title=='Dom Casmurro'
    assert url.endswith(end)
    assert 'Gutenberg' in book.description
    assert book.source_url.endswith('/ebooks/55752')

@pytest.mark.parametrize('url',['https://evil.example/ebooks/1','http://127.0.0.1/ebooks/1',
    'https://www.gutenberg.org.evil.example/ebooks/1','https://www.gutenberg.org:444/ebooks/1',
    'https://name:password@www.gutenberg.org/ebooks/1','https://www.gutenberg.org/admin',
    'file:///etc/passwd','https://www.gutenberg.org/ebooks/../admin'])
def test_public_host_allowlist(url):
    with pytest.raises(UserError):pg_url(url)

@pytest.mark.parametrize('raw',[b'<html>login</html>',b'<!DOCTYPE foo [<!ENTITY x "a">]><feed/>',b'not xml',b'x'*2000001])
def test_unknown_and_unsafe_xml_rejected(raw):
    with pytest.raises(UserError):xml(raw,'{'+NS['a']+'}feed')

def test_mismatched_edition_and_unverified_rights(pgbook):
    raw=(FIXTURES/'55752.rdf').read_bytes()
    with pytest.raises(UserError):parse_rdf(raw,replace(pgbook,id='1'))
    with pytest.raises(UserError):parse_rdf(raw.replace(b'Public domain in the USA.',b'All rights reserved.'),pgbook)

def test_source_keys_do_not_collide(pgbook):
    assert pgbook.key!=replace(pgbook,source='zlibrary').key
    assert SearchSpec('x',source='gutenberg').cache_key(1,8)!=SearchSpec('x',source='zlibrary').cache_key(1,8)

async def test_public_pdf_independent_of_legacy_credentials(settings,source,public,store,telegram):
    settings=replace(settings,base_url='',email='',password='',user_id='',user_key='')
    settings.validate()
    router=CatalogRouter(settings,source,public)
    bot=OperationsApp(settings,telegram,router,store)
    bot.downloads.start()
    try:
        await bot._command(123,123,'/testarpdf')
        await asyncio.wait_for(bot.downloads.queue.join(),3)
        source.quota.assert_not_awaited()
        source.download.assert_not_awaited()
        public.quota.assert_not_awaited()
        assert len(telegram.documents)==1
        assert (await store.last_delivery(123))['message_id']>0
    finally:await bot.downloads.stop()

async def test_legacy_block_uses_explicit_independent_source(settings,source,public):
    source.search.side_effect=UserError('HTTP 513','source_protected')
    router=CatalogRouter(settings,source,public)
    result=await router.search(SearchSpec('Dom Casmurro'))
    assert result.source=='gutenberg' and 'independente' in result.notice
    assert router.state=='partial'
    await router.search(SearchSpec('Dom Casmurro'))
    assert source.search.await_count==1  # no repeated login attempts during local pause

async def test_both_failures_not_converted_to_empty_search(settings,source,public):
    source.search.side_effect=UserError('denied','source_protected')
    public.search.side_effect=UserError('offline','source_timeout')
    router=CatalogRouter(settings,source,public)
    with pytest.raises(UserError,match='Não consegui consultar'):await router.search(SearchSpec('anything'))

async def test_explicit_selection_never_changes_catalog(settings,source,public):
    source.search.side_effect=UserError('denied','source_protected')
    router=CatalogRouter(settings,source,public)
    with pytest.raises(UserError):await router.search(SearchSpec('Dom',source='zlibrary'))
    public.search.assert_not_awaited()

async def test_search_snapshot_pins_source_and_preferences(settings,source,public,store,telegram,pgbook):
    source.search.side_effect=UserError('denied','source_protected')
    router=CatalogRouter(settings,source,public)
    bot=OperationsApp(settings,telegram,router,store)
    await bot.new_search(123,123,'Dom Casmurro')
    last=await store.latest_session(123)
    assert last.source=='gutenberg'
    assert await store.book(pgbook.key)
    await store.preferences(123,'source','gutenberg')
    await bot.new_search(123,123,'Dom Casmurro')
    assert (await store.user(123))['source']=='gutenberg'
    assert source.search.await_count==1

async def test_probe_sources_no_downloads_or_quota_calls(settings,source,public):
    source.search.side_effect=UserError('denied','source_protected')
    router=CatalogRouter(settings,source,public)
    report=await router.probe()
    assert report['status']=='partial' and report['download_tested'] is False
    source.download.assert_not_awaited();public.download.assert_not_awaited()
    public.quota.assert_not_awaited()

async def test_public_paginated_results_retain_all_formats(settings):
    source=Gutenberg(settings,None,None)
    raw=(FIXTURES/'search.xml').read_bytes()
    source._get=AsyncMock(return_value=(raw,'application/atom+xml'))
    first=await source.search(SearchSpec('Dom Casmurro'),1,1)
    second=await source.search(SearchSpec('Dom Casmurro'),2,1)
    assert first.books[0].extension=='pdf' and second.books[0].extension=='epub'
    assert first.has_next and not second.has_next
    assert source._get.await_count==1

async def test_public_download_converts_full_text(settings,tmp_path):
    source=Gutenberg(settings,None,None)
    text='Title: Meu livro\nAuthor: Autor\n\n*** START OF THE PROJECT GUTENBERG EBOOK TEST ***\n\nConteúdo completo com acentuação: ação.\n\n*** END OF THE PROJECT GUTENBERG EBOOK TEST ***\n\nLicença integral.'
    source._get=AsyncMock(return_value=(text.encode(),'text/plain; charset=utf-8'))
    path=tmp_path/'book.pdf'
    await source.download('https://www.gutenberg.org/ebooks/1.txt.utf-8','pdf',path,AsyncMock())
    assert path.read_bytes().startswith(b'%PDF-') and b'%%EOF' in path.read_bytes()[-30:]

@pytest.mark.parametrize('raw,mime',[(b'<html>denied</html>','text/html'),(b'Title: x\ntruncated','text/plain'),(b'\xff\xff','text/plain')])
async def test_public_rejects_non_books(settings,tmp_path,raw,mime):
    source=Gutenberg(settings,None,None)
    source._get=AsyncMock(return_value=(raw,mime))
    target=tmp_path/'book.pdf'
    with pytest.raises(UserError):await source.download('https://www.gutenberg.org/ebooks/1.txt.utf-8','pdf',target,AsyncMock())
    assert not target.exists()

async def test_native_epub_integrity(settings,tmp_path):
    source=Gutenberg(settings,None,None)
    data=io.BytesIO()
    with zipfile.ZipFile(data,'w') as z:
        z.writestr('mimetype',b'application/epub+zip')
        z.writestr('META-INF/container.xml','<container/>')
    source._get=AsyncMock(return_value=(data.getvalue(),'application/epub+zip'))
    target=tmp_path/'book.epub'
    await source.download('https://www.gutenberg.org/ebooks/1.epub.images','epub',target,AsyncMock())
    assert target.read_bytes()==data.getvalue()

@pytest.mark.parametrize('raw',[b'<html>',b'PK\x03\x04broken'])
async def test_broken_epub_removed(settings,tmp_path,raw):
    source=Gutenberg(settings,None,None)
    source._get=AsyncMock(return_value=(raw,'application/epub+zip'))
    target=tmp_path/'book.epub'
    with pytest.raises(UserError):await source.download('https://www.gutenberg.org/ebooks/1.epub.images','epub',target,AsyncMock())
    assert not target.exists()

def test_pdf_missing_glyph_is_not_silently_replaced(tmp_path):
    with pytest.raises(UserError,match='caracteres'):
        text_to_pdf('漢字',tmp_path/'bad.pdf','Book','Author')
    assert not (tmp_path/'bad.pdf').exists()

async def test_old_db_schema_upgrades_without_losing_users(tmp_path):
    path=tmp_path/'old.sqlite3'
    with sqlite3.connect(path) as c:
        c.executescript(SCHEMA)
        c.execute("INSERT INTO users VALUES (123,'portuguese','pdf',1)")
        c.execute("INSERT INTO sessions VALUES ('abc',123,'Dom Casmurro','portuguese','pdf',9999999999)")
    store=Store(path)
    await store.init()
    assert (await store.user(123))['source']=='auto'
    assert (await store.get_session(123,'abc')).query=='Dom Casmurro'
    await store.init()  # idempotent migration
    assert (await store.user(123))['extension']=='pdf'

async def test_source_buttons_status_and_quota_do_not_disable_public(settings,source,public,store,telegram):
    router=CatalogRouter(settings,source,public)
    router.states['gutenberg']={'status':'ready'}
    source.quota.side_effect=UserError('HTTP 513','source_protected')
    bot=OperationsApp(settings,telegram,router,store)
    await bot.show_sources(123,123)
    await bot.show_quota(123,123)
    assert router.states['gutenberg']['status']=='ready'
    assert 'Project Gutenberg' in telegram.sent[0][1]
    for row in views.home_keyboard(True):
        for button in row:assert len(button['callback_data'].encode())<=64
