from __future__ import annotations
import asyncio
import hashlib
from dataclasses import replace
from unittest.mock import AsyncMock
import pytest
from livros_baltigo.jobs import Downloads, Job
from livros_baltigo.receipts import document_receipt
from livros_baltigo.errors import TelegramError, UserError
from livros_baltigo.models import Quota


def receipt(chat=123):
    return {'message_id': 90, 'chat': {'id': chat}, 'document': {'file_id': 'FILE_ID', 'file_size': 48}}

@pytest.mark.parametrize('payload', [None, True, {}, [], {'message_id':90},
    {'message_id':True,'chat':{'id':123},'document':{'file_id':'X'}},
    {'message_id':0,'chat':{'id':123},'document':{'file_id':'X'}},
    {'message_id':90,'chat':{'id':'123'},'document':{'file_id':'X'}},
    {'message_id':90,'chat':{'id':123},'document':{'file_id':''}},
    {'message_id':90,'chat':{'id':123},'document':{'file_id':'X','file_size':False}},
    {'message_id':90,'chat':{'id':123},'document':{'file_id':'X','file_size':-1}},
    receipt(999)])
def test_incomplete_or_wrong_receipt_is_unknown_not_success(payload):
    with pytest.raises(TelegramError) as exc:
        document_receipt(payload, 123)
    assert exc.value.code == 0


def test_valid_receipt_requires_actual_message_and_chat():
    r = document_receipt(receipt(), 123)
    assert (r.chat_id, r.message_id, r.file_id) == (123,90,'FILE_ID')


async def prepared(settings, store, source, telegram, book):
    await store.save_books([book])
    ident = await store.reserve_job(123, book.key, '2026-10-01', 5)
    return Downloads(settings,store,source,telegram), Job(ident,123,123,5,book)

async def status(store, ident):
    return await store.run(lambda c:c.execute('SELECT status FROM jobs WHERE id=?',(ident,)).fetchone()[0])

async def test_document_delivery_has_durable_real_receipt(settings,store,source,telegram,book):
    worker,job=await prepared(settings,store,source,telegram,book)
    await worker._process(job)
    assert await status(store,job.id)=='done'
    proof=await store.last_delivery(123)
    assert proof['message_id'] > 0
    assert proof['chat_id']==123
    assert proof['sha256']==hashlib.sha256(telegram.documents[0][1]).hexdigest()
    assert not list((settings.data_dir/'tmp').glob('job-*'))
    assert 'Comprovante Telegram' in telegram.edits[-1][2]
    assert await store.last_delivery(999) is None

@pytest.mark.parametrize('payload',[None,{},receipt(999)])
async def test_unknown_receipt_never_becomes_success(settings,store,source,telegram,book,payload):
    worker,job=await prepared(settings,store,source,telegram,book)
    telegram.document=AsyncMock(return_value=payload)
    await worker._process(job)
    assert await status(store,job.id)=='uncertain'
    assert await store.last_delivery(123) is None
    telegram.document.assert_awaited_once()
    assert not list((settings.data_dir/'tmp').glob('job-*'))

@pytest.mark.parametrize('error,expected',[(TelegramError(0),'uncertain'),(TelegramError(403),'failed'),
    (TelegramError(429,retry_after=15),'failed'),(ValueError('bad response'),'uncertain')])
async def test_no_automatic_duplicate_after_write_failure(settings,store,source,telegram,book,error,expected):
    worker,job=await prepared(settings,store,source,telegram,book)
    telegram.document=AsyncMock(side_effect=error)
    await worker._process(job)
    assert await status(store,job.id)==expected
    assert await store.last_delivery(123) is None
    telegram.document.assert_awaited_once()

@pytest.mark.parametrize('quota',[Quota(None,None),Quota(5,None),Quota(-1,0),Quota(5,-1),Quota(5,5)])
async def test_invalid_quota_does_not_request_or_send_file(settings,store,source,telegram,book,quota):
    worker,job=await prepared(settings,store,source,telegram,book)
    source.quota.return_value=quota
    await worker._process(job)
    assert await status(store,job.id)=='failed'
    source.file_info.assert_not_called()
    assert not telegram.documents

async def test_restart_after_sending_is_uncertain(settings,store,book):
    await store.save_books([book])
    ident=await store.reserve_job(123,book.key,'2026-10-01',5)
    await store.job_status(ident,'sending')
    await store.init()
    assert await status(store,ident)=='uncertain'
    assert await store.daily_usage(123,'2026-10-01')==1

@pytest.mark.parametrize('state',['running','queued'])
async def test_restart_before_sending_releases_failed_reservation(settings,store,book,state):
    await store.save_books([book])
    ident=await store.reserve_job(123,book.key,'2026-10-01',5)
    await store.job_status(ident,state)
    await store.init()
    assert await status(store,ident)=='interrupted'
    assert await store.daily_usage(123,'2026-10-01')==0

async def test_cancellation_during_send_is_uncertain(settings,store,source,telegram,book):
    worker,job=await prepared(settings,store,source,telegram,book)
    telegram.document=AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await worker._process(job)
    assert await status(store,job.id)=='uncertain'
    assert not list((settings.data_dir/'tmp').glob('job-*'))

async def test_daily_reservation_concurrency_is_atomic(settings,store,book):
    await store.save_books([book])
    async def reserve():
        try: return await store.reserve_job(123,book.key,'2026-10-01',3)
        except UserError: return None
    values=await asyncio.gather(*(reserve() for _ in range(12)))
    assert len([x for x in values if x is not None])==3

async def test_worker_continues_after_failed_source(settings,store,source,telegram,book):
    worker,job=await prepared(settings,store,source,telegram,book)
    source.file_info.side_effect=UserError('Unavailable','source_protected')
    await worker._process(job)
    assert await status(store,job.id)=='failed'
    assert not telegram.documents
    assert 'HTTP 513' in telegram.edits[-1][2] or 'Unavailable' in telegram.edits[-1][2]
