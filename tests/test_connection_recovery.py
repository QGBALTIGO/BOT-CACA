"""Regressions for network failure being mislabeled as account/source limits."""
import asyncio
import json
import logging
import socket
import ssl
import time
from dataclasses import replace
from email.utils import formatdate
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
from aiohttp import web
import pytest

from conftest import Session, Response
from livros_baltigo.availability import network_kind, pause_message, retry_after_seconds
from livros_baltigo.errors import UserError
from livros_baltigo.models import Quota, SearchSpec
from livros_baltigo.network import new_session
from livros_baltigo.operations import OperationsApp
from livros_baltigo.provider import ZLibrary
from livros_baltigo.scraper import HTMLSource
from livros_baltigo.transport_audit import trace_config, probe_origin


@pytest.mark.parametrize('klass', [ZLibrary, HTMLSource])
async def test_reset_then_quota_keeps_network_reason(settings, klass):
    api = Session([ConnectionResetError(104, 'redacted')])
    source = klass(settings, api, Session([]))
    for attempt in range(2):
        with pytest.raises(UserError) as caught:
            await source.quota()
        assert caught.value.code == 'network'
        assert 'espera é local' in caught.value.message
        assert 'fonte pediu' not in caught.value.message.lower()
    assert len(api.calls) == 1


async def test_html_reset_then_quota_keeps_reason(settings):
    api = Session([ConnectionResetError(104, 'test')])
    source = HTMLSource(settings, api, Session([]))
    with pytest.raises(UserError) as first:
        await source.get_html('/s/Teste')
    with pytest.raises(UserError) as second:
        await source.quota()
    assert first.value.code == second.value.code == 'network'
    assert len(api.calls) == 1


@pytest.mark.parametrize('status,code', [(401, 'auth'), (403, 'blocked'), (429, 'rate_limit'), (503, 'unavailable')])
async def test_http_cause_preserved(settings, status, code):
    api = Session([Response({}, status=status, headers={'Retry-After': '180'})])
    source = ZLibrary(settings, api, Session([]))
    for _ in range(2):
        with pytest.raises(UserError) as caught:
            await source.quota()
        assert caught.value.code == code
    assert len(api.calls) == 1
    if status == 429:
        assert source.retry_at - time.monotonic() > 170


@pytest.mark.parametrize('status,code', [(401, 'html_auth'), (403, 'blocked'), (429, 'rate_limit')])
async def test_html_pause_cause_preserved(settings, status, code):
    api = Session([Response({}, status=status, headers={'Retry-After': '180'})])
    source = HTMLSource(settings, api, Session([]))
    for _ in range(2):
        with pytest.raises(UserError) as caught:
            await source.get_html('/s/Teste')
        assert caught.value.code == code
    assert len(api.calls) == 1


async def test_concurrent_login_failure_only_sends_once(settings):
    cfg = replace(settings, user_id='', user_key='', email='test@example.invalid', password='FAKE')
    api = Session([ConnectionResetError(104, 'test')])
    source = ZLibrary(cfg, api, Session([]))
    results = await asyncio.gather(*(source.quota() for _ in range(25)), return_exceptions=True)
    assert len(api.calls) == 1
    assert all(isinstance(e, UserError) and e.code == 'network' for e in results)


async def test_expired_local_pause_allows_recovery(settings):
    source = ZLibrary(settings, Session([Response({'user': {'downloads_limit': 20, 'downloads_today': 2}})]), Session([]))
    source.pause('network')
    source.retry_at = time.monotonic() - 1
    assert (await source.quota()).remaining == 18


async def test_repeat_does_not_extend_the_pause(settings):
    source = ZLibrary(settings, Session([]), Session([]))
    source.pause('network', 30)
    deadline = source.retry_at
    for _ in range(10):
        with pytest.raises(UserError):
            await source.quota()
    assert source.retry_at == deadline


@pytest.mark.parametrize('error,code', [(socket.gaierror(-3,'secret'), 'source_dns'),
                                      (ssl.SSLError('secret'),'source_tls'),
                                      (TimeoutError('secret'),'source_timeout'),
                                      (ConnectionResetError(104,'secret'),'network')])
def test_specific_connection_error_types(error, code):
    assert network_kind(error) == code
    assert 'secret' not in pause_message(code, 12)


@pytest.mark.parametrize('header,seconds', [('10',10), (' 120 ',120), ('0',1), (None,60), ('invalid',60), ('3600',3600)])
def test_retry_after_headers(header, seconds):
    assert retry_after_seconds(header) == seconds


def test_retry_after_http_date():
    assert retry_after_seconds(formatdate(1700000300, usegmt=True), now=1700000000) == 300


@pytest.mark.parametrize('code', ['network','source_dns','source_timeout','source_tls','auth','blocked','cooldown'])
def test_local_pause_is_never_attributed_to_server(code):
    result = pause_message(code, 30)
    assert 'espera é local' in result and 'HTTP 429' not in result


async def test_quota_screen_has_two_distinct_counters(settings, store, telegram, source):
    app = OperationsApp(settings, telegram, source, store)
    source.quota.side_effect = UserError('internal error', 'network')
    source.retry_at = time.monotonic() + 10
    await app.show_quota(9876, 9876)
    text = telegram.sent[-1][1]
    assert '0 de ' in text and 'Saldo não consultado' in text
    assert 'não é uma confirmação de limite esgotado' in text
    source.file_info.assert_not_called()
    source.download.assert_not_called()
    assert await store.daily_usage(9876, app.downloads.today()) == 0


async def test_successful_quota_screen(settings, store, telegram, source):
    source.quota.return_value = Quota(20, 3)
    app = OperationsApp(settings, telegram, source, store)
    await app.show_quota(9876, 9876)
    assert '17 downloads disponíveis' in telegram.sent[-1][1]
    assert 'Saldo não consultado' not in telegram.sent[-1][1]


async def test_unknown_quota_not_rendered_as_zero(settings, store, telegram, source):
    source.quota.return_value = Quota(None, None)
    app = OperationsApp(settings, telegram, source, store)
    await app.show_quota(9876, 9876)
    assert 'Disponibilidade não confirmada' in telegram.sent[-1][1]
    assert '0 downloads disponíveis' not in telegram.sent[-1][1]


async def test_status_does_not_claim_configured_means_verified(settings, store, telegram, source):
    app = OperationsApp(settings, telegram, source, store)
    app.catalog_state = 'unavailable'
    await app.show_status(123, 123)
    assert 'Conexão indisponível' in telegram.sent[-1][1]


async def test_trace_redacts_secrets_on_real_local_http(caplog):
    caplog.set_level(logging.INFO)
    application = web.Application()
    async def reply(request):
        await request.read()
        return web.Response(text='private response not logged')
    application.router.add_post('/', reply)
    runner = web.AppRunner(application, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with aiohttp.ClientSession(trace_configs=[trace_config('127.0.0.1')]) as client:
            async with client.post(f'http://127.0.0.1:{port}/?secret=URLSECRET',
                                   data={'password':'BODYSECRET'}, headers={'Cookie':'COOKIESECRET'}) as response:
                await response.read()
        assert all(secret not in caplog.text for secret in ['URLSECRET','BODYSECRET','COOKIESECRET','private response not logged'])
        payloads = [json.loads(r.message) for r in caplog.records if 'source_transport' in r.message]
        assert payloads[-1]['http_status'] == 200
        assert payloads[-1]['headers_sent'] and payloads[-1]['body_sent'] and payloads[-1]['connection_ready']
    finally:
        await runner.cleanup()


async def test_trace_does_not_observe_other_host(caplog):
    config = trace_config('source.example')
    ctx = config.trace_config_ctx()
    params = SimpleNamespace(url=SimpleNamespace(host='api.telegram.org'),method='POST')
    await config.on_request_start[0](None, ctx, params)
    await config.on_request_exception[0](None, ctx, SimpleNamespace(exception=OSError('BOTSECRET')))
    assert 'BOTSECRET' not in caplog.text and not ctx.enabled


async def test_public_origin_probe_never_sends_credentials():
    api = Session([Response({}, status=302, headers={'Location':'https://other.example/private'})])
    result = await probe_origin(api, 'https://catalog.example')
    assert result['credentials_sent'] is False and result['http_status'] == 302
    assert len(api.calls) == 1 and api.calls[0][0] == 'GET'
    assert not api.calls[0][2].get('data')
    assert api.calls[0][2]['allow_redirects'] is False
    assert api.calls[0][2]['headers'] == {'Accept':'text/html'}


async def test_connection_report_redacts_error_message():
    result = await probe_origin(Session([ConnectionResetError(104,'PRIVATESECRET')]), 'https://catalog.example')
    assert result['error'] == 'ConnectionResetError' and 'PRIVATESECRET' not in str(result)


async def test_session_audit_optional_and_cookie_free():
    async with new_session(audit_host='catalog.example') as session:
        assert len(session.trace_configs) == 1 and isinstance(session.cookie_jar, aiohttp.DummyCookieJar)
    async with new_session() as session:
        assert not session.trace_configs
