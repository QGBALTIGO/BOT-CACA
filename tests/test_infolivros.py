import json
from dataclasses import replace
from unittest.mock import AsyncMock
import pytest
from livros_baltigo.infolivros import InfoLivros, BASE
from livros_baltigo.models import SearchSpec
from livros_baltigo.errors import UserError

INDEX = b'''<html><ul><li><a class="az-book-link" href="/livro/orgulho-e-preconceito-jane-austen/">
<span class="az-book-title">Orgulho e Preconceito</span><span class="az-book-author">Jane Austen</span></a></li>
<li><a class="az-book-link" href="/livro/persuasao-jane-austen/">
<span class="az-book-title">Persuasao</span><span class="az-book-author">Jane Austen</span></a></li></ul></html>'''


def raw_record(book, price='0', url=None):
    data = {'@type':'Book','name':book.title,'author':{'name':book.author},'url':book.source_url,
            'inLanguage':'pt','offers':{'price':price},'description':'Descrição da fonte.','datePublished':'1813'}
    href = url or 'https://pdf.infolivros.org/PT/aldus/orgulho-e-preconceito-jane-austen.pdf'
    return ('<script type="application/ld+json">' + json.dumps(data) + '</script>'
            + '<a class="pdf-download" href="' + href + '">Baixar PDF</a>').encode()


async def test_local_index_search_by_title_author_accents_and_empty(settings):
    source = InfoLivros(settings, None, None)
    source._get = AsyncMock(return_value=(INDEX,'text/html'))
    title = await source.search(SearchSpec('Orgulho e Preconceito'),1,8)
    assert len(title.books)==1 and title.books[0].title=='Orgulho e Preconceito'
    first = await source.search(SearchSpec('Jane Austen'),1,1)
    second = await source.search(SearchSpec('Jane Austen'),2,1)
    assert first.has_next and not second.has_next
    assert first.books[0].key != second.books[0].key
    assert not (await source.search(SearchSpec('zzqvnonexistentbookzz'))).books
    assert (await source.search(SearchSpec('persuasão'))).books
    assert source._get.await_count==1


@pytest.mark.parametrize('extension,language',[('epub','portuguese'),('pdf','english')])
async def test_unavailable_formats_not_fabricated(settings, extension, language):
    source=InfoLivros(settings,None,None)
    source._get=AsyncMock()
    assert not (await source.search(SearchSpec('Austen',language,extension))).books
    source._get.assert_not_awaited()


def test_book_metadata_preserved_and_public_file_selected(settings):
    source=InfoLivros(settings,None,None)
    book=source.parse_index(INDEX)[0]
    fresh,url=source.parse_record(raw_record(book),book)
    assert fresh.title==book.title and fresh.author==book.author
    assert fresh.year=='1813' and fresh.description=='Descrição da fonte.'
    assert url.startswith('https://pdf.infolivros.org/PT/')


@pytest.mark.parametrize('price',['10',None,'NaN','-1'])
def test_no_paid_or_unknown_offer_treated_as_free(settings,price):
    source=InfoLivros(settings,None,None);book=source.parse_index(INDEX)[0]
    with pytest.raises(UserError):source.parse_record(raw_record(book,price=price),book)


@pytest.mark.parametrize('url',['https://evil.example/test.pdf','http://pdf.infolivros.org/PT/a.pdf',
    'https://127.0.0.1/PT/a.pdf','https://pdf.infolivros.org/PT/../private.pdf',
    'https://pdf.infolivros.org/PT/a.pdf?token=anything'])
def test_foreign_and_unsafe_downloads_rejected(settings,url):
    source=InfoLivros(settings,None,None);book=source.parse_index(INDEX)[0]
    with pytest.raises(UserError):source.parse_record(raw_record(book,url=url),book)


async def test_record_cache_does_not_mix_books(settings):
    source=InfoLivros(settings,None,None);book=source.parse_index(INDEX)[0]
    source._get=AsyncMock(return_value=(raw_record(book),'text/html'))
    await source.details(book); await source.details(book)
    source._get.assert_not_awaited()
    with pytest.raises(UserError, match='apenas para consulta'):
        await source.file_info(book)
    with pytest.raises(UserError):await source.details(replace(book,source_url='https://evil.example'))


def test_unknown_index_not_silently_empty(settings):
    with pytest.raises(UserError):InfoLivros(settings,None,None).parse_index(b'<h1>Layout changed</h1>')


def test_external_catalog_never_displays_fake_download_button(settings):
    from livros_baltigo import views
    source=InfoLivros(settings,None,None);book=source.parse_index(INDEX)[0]
    for favorite in (True,False):
        rows=views.book_keyboard(book,favorite)
        assert rows[0][0]['url']==book.source_url
        assert not any(b.get('callback_data','').startswith('download:') for row in rows for b in row)
    assert 'Consulta externa' in views.book_card(book)


async def test_external_catalog_does_not_reserve_download_quota(settings,store,telegram):
    from livros_baltigo.jobs import Downloads
    source=InfoLivros(settings,None,None);book=source.parse_index(INDEX)[0]
    downloads=Downloads(settings,store,source,telegram)
    downloads.accepting=True
    with pytest.raises(UserError) as exc:
        await downloads.enqueue(123,123,1,book)
    assert exc.value.code=='external_only'
    assert await store.daily_usage(123,downloads.today())==0
    assert downloads.queue.empty()


async def test_external_catalog_never_requests_blocked_pdf(settings,tmp_path):
    source=InfoLivros(settings,None,None)
    source._get=AsyncMock()
    with pytest.raises(UserError):
        await source.download('https://pdf.infolivros.org/PT/test.pdf','pdf',tmp_path/'book.pdf',AsyncMock())
    source._get.assert_not_awaited()
