from __future__ import annotations
import asyncio
from dataclasses import replace
import pytest
from livros_baltigo.operations import OperationsApp
from livros_baltigo.errors import TelegramError, UserError
from livros_baltigo.models import SearchSpec
from livros_baltigo import views


def update(text='', data=None, uid=123, private=True):
    msg={'message_id':1,'text':text,'from':{'id':uid,'is_bot':False},'chat':{'id':uid,'type':'private' if private else 'group'}}
    if data is None:return {'update_id':10,'message':msg}
    return {'update_id':11,'callback_query':{'id':'CB','from':{'id':uid,'is_bot':False},'data':data,'message':msg}}

@pytest.mark.parametrize('command',['/start','/inicio','/menu','/buscar','/buscar Dom Casmurro','/biblioteca','/favoritos',
    '/historico','/filtros','/limites','/limite','/id','/privacidade','/ajuda','/comprovante','/status','/testarfonte'])
async def test_every_command_has_a_response(settings,telegram,source,store,command):
    bot=OperationsApp(settings,telegram,source,store)
    await bot.handle(update(command))
    assert telegram.sent or telegram.edits or telegram.photos
    text=' '.join(x[1] for x in telegram.sent)+' '.join(x[2] for x in telegram.edits)
    assert 'falha interna' not in text.casefold()

@pytest.mark.parametrize('data',['menu:search','menu:home','menu:settings','menu:quota','menu:help','menu:privacy',
    'favpage:1','histpage:1','admin:status','admin:check','setting:language:english','setting:extension:pdf'])
async def test_every_home_button_answers_callback_and_renders(settings,telegram,source,store,data):
    bot=OperationsApp(settings,telegram,source,store)
    await bot.handle(update(data=data))
    assert telegram.answers
    assert telegram.sent or telegram.edits or telegram.photos
    assert not any('falha interna' in x[1].casefold() for x in telegram.sent)

@pytest.mark.parametrize('data',['admin:status','admin:check'])
async def test_non_admin_does_not_get_admin_access(settings,telegram,source,store,data):
    bot=OperationsApp(replace(settings,public_access=True),telegram,source,store)
    await bot.handle(update(data=data,uid=456))
    assert any('administrador' in x[1] for x in telegram.sent)
    source.quota.assert_not_awaited()

async def test_search_result_opens_and_favorite_roundtrip(settings,telegram,source,store,book):
    bot=OperationsApp(settings,telegram,source,store)
    await bot.handle(update('Dom Casmurro'))
    assert await store.book(book.key)
    for data in (f'book:{book.key}',f'favorite:{book.key}:1',f'favorite:{book.key}:0'):
        bot.last_action.clear()
        await bot.handle(update(data=data))
    assert not await store.is_favorite(123,book.key)
    assert len(telegram.markups)==2

async def test_stolen_search_session_is_not_reused(settings,telegram,source,store):
    bot=OperationsApp(replace(settings,public_access=True),telegram,source,store)
    ident=await store.session(123,SearchSpec('Dom Casmurro'),3600)
    await bot.handle(update(uid=456,data=f'page:{ident}:1'))
    source.search.assert_not_awaited()
    assert any('outro' in x[1] for x in telegram.sent)

async def test_expired_callback_is_explained(settings,telegram,source,store):
    bot=OperationsApp(settings,telegram,source,store)
    await bot.handle(update(data='no-longer-valid'))
    assert telegram.answers
    assert any('válido' in x[1] or 'expirou' in x[1] for x in telegram.sent)

async def test_group_does_not_query_catalog(settings,telegram,source,store):
    bot=OperationsApp(settings,telegram,source,store)
    await bot.handle(update('Dom Casmurro',private=False))
    source.search.assert_not_awaited()
    assert not telegram.sent

async def test_metadata_failure_does_not_stop_polling_initialization(settings,telegram,source,store):
    original=telegram.call
    async def call(method,values=None):
        if method=='setMyCommands':raise TelegramError(429,retry_after=1)
        return await original(method,values)
    telegram.call=call
    bot=OperationsApp(settings,telegram,source,store)
    await bot.initialize()
    assert bot.downloads.worker and not bot.downloads.worker.done()
    await bot.downloads.stop()

async def test_source_protection_message_does_not_promise_a_fix_after_wait(settings,telegram,source,store):
    bot=OperationsApp(settings,telegram,source,store)
    source.search.side_effect=UserError('Protection','source_protected')
    await bot.handle(update('Dom Casmurro'))
    text=' '.join(x[2] for x in telegram.edits)
    assert 'HTTP 513' in text
    assert 'Aguardar não garante' in text

async def test_pdf_callback_uses_the_queue(settings,telegram,source,store,book):
    await store.save_books([book])
    bot=OperationsApp(settings,telegram,source,store)
    bot.downloads.start()
    try:
        await bot.handle(update(data=f'download:{book.key}'))
        await asyncio.wait_for(bot.downloads.queue.join(),2)
        assert len(telegram.documents)==1
        assert await store.last_delivery(123)
    finally: await bot.downloads.stop()

@pytest.mark.parametrize('language',['portuguese','english','spanish','french','german','any'])
@pytest.mark.parametrize('extension',['any','pdf','epub'])
def test_all_filter_buttons_fit_callback_limit(language,extension):
    for row in views.settings_keyboard({'language':language,'extension':extension},True):
        for item in row:
            assert len(item['callback_data'].encode())<=64
