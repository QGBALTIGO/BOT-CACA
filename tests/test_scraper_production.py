from unittest.mock import AsyncMock
from dataclasses import replace
import pytest
from livros_baltigo.scraper import parse_results, parse_details, HTMLSource, same_origin
from livros_baltigo.errors import UserError
from livros_baltigo.models import SearchSpec, Quota
from livros_baltigo.app import BotApp
from conftest import Session, Response


def cards(start=1, count=10, more=False):
    rows = ''.join(f'<z-bookcard href="/book/{i}/hash{i}/livro.html" language="Portuguese" extension="pdf" year="1900" filesize="1 MB"><div slot="title">Livro {i}</div><div slot="author">Autor de teste</div><img data-src="/cover.jpg"></z-bookcard>' for i in range(start, start+count))
    return '<div id="searchResultBox">'+rows+'</div>'+ ('<a rel="next" href="?page=2">Próxima</a>' if more else '')


def test_modern_cards_with_hidden_login_modal():
    page=parse_results(cards(1,3)+'<div hidden><form><input type="password"></form></div>', 'https://catalog.example', 1)
    assert len(page.books)==3 and page.books[0].size==1_000_000
    assert page.books[0].author=='Autor de teste'

@pytest.mark.parametrize('raw,code', [('<h1>Site mudou</h1>','layout_changed'),('<form><input type="password"></form>','html_auth'),('<form id="challenge-form"></form>','captcha'),('<title>Just a moment...</title>','captcha')])
def test_unrecognized_or_blocked_html(raw,code):
    with pytest.raises(UserError) as exc:parse_results(raw,'https://catalog.example',1)
    assert exc.value.code==code


def test_explicit_empty():
    assert not parse_results('<div class="notFound">Nada</div>','https://catalog.example',1).books


def test_duplicate_editions_removed_without_merging_formats():
    a=cards(1,2)
    result=parse_results(a+a,'https://catalog.example',1)
    assert [b.id for b in result.books]==['1','2']

@pytest.mark.parametrize('url',['https://other.example/book/1/hash','http://catalog.example/a','https://127.0.0.1/a','javascript:alert(1)'])
def test_cross_origin_and_private_links(url):
    with pytest.raises(UserError):same_origin('https://catalog.example',url)

async def test_page_boundary_no_lost_books(settings):
    source=HTMLSource(settings,Session([]),Session([]))
    source.get_html=AsyncMock(side_effect=[cards(1,50,True),cards(51,50)])
    a=await source.search(SearchSpec('Teste'),7,8)
    b=await source.search(SearchSpec('Teste'),8,8)
    assert [x.id for x in a.books]==[str(i) for i in range(49,57)]
    assert [x.id for x in b.books]==[str(i) for i in range(57,65)]
    assert source.get_html.await_count==2

async def test_filters_encoded_and_cache_reused(settings):
    source=HTMLSource(settings,Session([]),Session([]))
    source.get_html=AsyncMock(return_value=cards(1,20))
    a=await source.search(SearchSpec('José & Maria','portuguese','pdf'))
    b=await source.search(SearchSpec('José & Maria','portuguese','pdf'),2)
    assert a.books[0].id=='1' and b.books[0].id=='9'
    path=source.get_html.call_args.args[0]
    assert '%26' in path and 'languages%5B%5D=portuguese' in path and 'extensions%5B%5D=pdf' in path
    assert source.get_html.await_count==1

async def test_arbitrary_deep_paging_is_bounded(settings):
    source=HTMLSource(settings,Session([]),Session([]))
    source.get_html=AsyncMock(return_value=cards(1,50,True))
    with pytest.raises(UserError) as exc:await source.search(SearchSpec('Teste'),900,8)
    assert exc.value.code=='expired_search' and source.get_html.await_count==2

@pytest.mark.parametrize('status,code',[(401,'html_auth'),(403,'blocked'),(429,'rate_limit')])
async def test_block_is_not_retried(settings,status,code):
    api=Session([Response(body=b'',status=status)])
    source=HTMLSource(settings,api,Session([]))
    with pytest.raises(UserError) as exc:await source.get_html('/s/Teste')
    assert exc.value.code==code
    with pytest.raises(UserError):await source.get_html('/s/Teste')
    assert len(api.calls)==1

async def test_redirect_never_leaks_credentials(settings):
    api=Session([Response(body=b'',status=302,headers={'Location':'https://other.example/steal'})])
    source=HTMLSource(settings,api,Session([]))
    with pytest.raises(UserError) as exc:await source.get_html('/s/Teste')
    assert exc.value.code=='foreign_origin' and len(api.calls)==1

async def test_validated_startup_never_downloads(settings,telegram,source,store):
    bot=BotApp(settings,telegram,source,store)
    await bot.probe_source()
    assert bot.catalog_state=='ready'
    source.file_info.assert_not_called()
    source.download.assert_not_called()
    assert len(telegram.sent)==1

async def test_unavailable_source_keeps_menu_usable(settings,telegram,source,store):
    source.quota.side_effect=UserError('Sem conexão','source_dns')
    bot=BotApp(settings,telegram,source,store)
    await bot.probe_source()
    await bot.show_home(123)
    assert bot.catalog_state=='unavailable'
    assert 'indisponível' in telegram.sent[-1][1]
    source.search.assert_not_called()


def test_details_preserve_edition_identity(book):
    result=parse_details('<h1>Livro teste</h1><div id="bookDescriptionBox">Resumo <b>bonito</b></div>','https://catalog.example',book)
    assert result.id==book.id and result.hash==book.hash and result.description=='Resumo bonito'
