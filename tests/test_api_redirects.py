from dataclasses import replace
from http.cookies import SimpleCookie
from unittest.mock import Mock

import pytest

from livros_baltigo.errors import UserError
from livros_baltigo.provider import ZLibrary
from livros_baltigo.redirects import APIRedirects, redirect_target
from conftest import Response, Session

BASE = 'https://catalog.example/eapi/user/login'


@pytest.mark.parametrize('status', [307, 308])
@pytest.mark.parametrize('location', [BASE + '/', '/eapi/user/login/', '?language=pt', BASE + '?language=pt'])
def test_same_endpoint_target(status, location):
    target = redirect_target(BASE, BASE, location, status, 'POST')
    assert target.startswith('https://catalog.example/eapi/user/login')


@pytest.mark.parametrize('target', [
    'http://catalog.example/eapi/user/login',
    'https://other.example/eapi/user/login',
    'https://catalog.example.other.example/eapi/user/login',
    'https://catalog.example@other.example/eapi/user/login',
    'https://catalog.example:8443/eapi/user/login',
    'https://127.0.0.1/eapi/user/login',
    '//other.example/eapi/user/login',
    '/book/1/edition', '/eapi/user/delete', '/', '/cdn-cgi/challenge',
    '/eapi/user/login/../delete', '/eapi/user/%6cogin',
    '/eapi/user/login#section', '', '\r\nhttps://other.example/',
])
async def test_unapproved_target_never_receives_body(target):
    session = Session([Response(status=307, headers={'Location': target})])
    client = APIRedirects(session)
    with pytest.raises(UserError):
        async with client.request('POST', BASE, data={'password': 'ARTIFICIAL'}):
            pytest.fail('A redirect must not count as an authenticated response')
    assert len(session.calls) == 1
    assert session.calls[0][1] == BASE


@pytest.mark.parametrize('status', [301, 302, 303])
async def test_post_is_not_silently_changed_to_get(status):
    session = Session([Response(status=status, headers={'Location': BASE + '/'})])
    with pytest.raises(UserError) as error:
        async with APIRedirects(session).request('POST', BASE, data={'email':'example'}):
            pass
    assert error.value.code == 'api_redirect_method'
    assert len(session.calls) == 1


async def test_login_and_profile_after_canonical_redirect(settings):
    cfg = replace(settings, user_id='', user_key='', email='test@example.invalid', password='ARTIFICIAL')
    session = Session([
        Response(status=307, headers={'Location': '/eapi/user/login/'}),
        Response({'success': 1, 'user': {'id': 42, 'remix_userkey': 'ARTIFICIAL_SESSION'}}),
        Response({'user': {'downloads_limit': 10, 'downloads_today': 2}}),
    ])
    source = ZLibrary(cfg, session, Session([]))
    quota = await source.quota()
    assert quota.remaining == 8
    assert [x[0] for x in session.calls] == ['POST', 'POST', 'GET']
    assert session.calls[0][2]['data'] == session.calls[1][2]['data']
    assert session.calls[1][1] == BASE + '/'
    assert all(x[2]['allow_redirects'] is False for x in session.calls)
    assert not any('/file' in x[1] for x in session.calls)


async def test_same_url_loop_stops_without_replaying_password(caplog):
    session = Session([Response(status=307, headers={'Location': BASE})])
    with pytest.raises(UserError) as error:
        async with APIRedirects(session).request('POST', BASE, data={'password': 'NOT_FOR_LOGS'}):
            pass
    assert error.value.code == 'api_redirect_loop'
    assert len(session.calls) == 1
    assert 'NOT_FOR_LOGS' not in caplog.text


async def test_ordinary_response_cookie_can_complete_same_url_session():
    redirect = Response(status=307, headers={'Location': BASE})
    redirect.cookies = SimpleCookie('site_session=ARTIFICIAL; Path=/; Secure')
    session = Session([redirect, Response({'success':True})])
    async with APIRedirects(session).request('POST', BASE) as response:
        assert response.status == 200
    assert 'site_session=ARTIFICIAL' in session.calls[1][2]['headers']['Cookie']


async def test_cookie_refresh_chain_is_bounded():
    responses = []
    for i in range(4):
        r = Response(status=307, headers={'Location': BASE})
        r.cookies = SimpleCookie(f'site_session=fake{i}; Path=/; Secure')
        responses.append(r)
    session = Session(responses)
    with pytest.raises(UserError) as error:
        async with APIRedirects(session).request('POST', BASE): pass
    assert error.value.code == 'api_redirect_limit'
    assert len(session.calls) == 4


async def test_unrelated_cookie_domain_does_not_break_loop_protection():
    r = Response(status=307, headers={'Location': BASE})
    r.cookies = SimpleCookie('site_session=ARTIFICIAL; Domain=other.example; Path=/; Secure')
    session = Session([r])
    with pytest.raises(UserError) as error:
        async with APIRedirects(session).request('POST', BASE): pass
    assert error.value.code == 'api_redirect_loop'
    assert len(session.calls) == 1


async def test_server_cookie_cannot_replace_explicit_identity():
    r = Response(status=307, headers={'Location': BASE + '/'})
    r.cookies = SimpleCookie('remix_userid=99; Path=/; Secure')
    session = Session([r, Response({})])
    async with APIRedirects(session).request('GET', BASE, headers={'Cookie':'remix_userid=42; remix_userkey=ARTIFICIAL'}): pass
    assert 'remix_userid=42' in session.calls[1][2]['headers']['Cookie']
    assert 'remix_userid=99' not in session.calls[1][2]['headers']['Cookie']


@pytest.mark.parametrize('status', [401,403,429,500])
async def test_failures_are_not_automatically_retried(status):
    session = Session([Response(status=status)])
    async with APIRedirects(session).request('POST', BASE) as response:
        assert response.status == status
    assert len(session.calls) == 1


async def test_no_secrets_or_raw_location_in_redirect_logs(caplog):
    caplog.set_level('INFO')
    session = Session([Response(status=307, headers={'Location':'https://other.example/PRIVATE?token=HIDDEN'})])
    with pytest.raises(UserError):
        async with APIRedirects(session).request('POST',BASE,data={'password':'PASSWORD'},headers={'Cookie':'remix_userkey=SECRET'}): pass
    for text in ['PRIVATE', 'HIDDEN', 'PASSWORD', 'SECRET']:
        assert text not in caplog.text
    assert 'api_redirect' in caplog.text
