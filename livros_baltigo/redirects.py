"""Bounded API redirects within the same HTTPS origin and logical endpoint.

307/308 retain the method/body. No automatic cross-origin forwarding, CAPTCHA
handling, transport retry, proxy, certificate relaxation or file replay.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from http.cookies import SimpleCookie
from urllib.parse import urljoin, urlsplit, urlunsplit
import asyncio
import json
import logging
import time

import aiohttp
from yarl import URL

from .errors import UserError
from .network import validate_url

log = logging.getLogger(__name__)
REDIRECT_CODES = {301, 302, 303, 307, 308}


def origin(value: str) -> tuple:
    parsed = urlsplit(validate_url(value))
    return parsed.scheme, parsed.hostname.lower(), parsed.port or 443


def route_class(value: str) -> str:
    path = urlsplit(value).path.rstrip('/')
    if path == '':
        return 'root'
    if path == '/eapi/user/login':
        return 'api_login'
    if path == '/eapi/user/profile':
        return 'api_profile'
    if path.startswith('/eapi/'):
        return 'api_other'
    if any(item in path.lower() for item in ('challenge', 'captcha', 'cdn-cgi')):
        return 'verification'
    return 'other'


def redirect_target(original: str, current: str, location: str, status: int, method: str) -> str:
    if not isinstance(location, str) or not location or len(location) > 4096 or any(ord(c) < 33 for c in location):
        raise UserError('O catálogo retornou um redirecionamento sem destino válido.', 'api_redirect_invalid')
    target = validate_url(urljoin(current, location))
    if origin(target) != origin(original):
        raise UserError('O catálogo apontou para outro domínio. O acesso foi interrompido sem encaminhar suas credenciais.', 'api_redirect')
    if status not in {307, 308} and not (method in {'GET', 'HEAD'} and status in {301, 302}):
        raise UserError('O redirecionamento do catálogo mudaria o método da operação. Nenhum reenvio foi feito.', 'api_redirect_method')
    initial, final = urlsplit(original), urlsplit(target)
    if not initial.path.startswith('/eapi/') or final.path.rstrip('/') != initial.path.rstrip('/'):
        raise UserError('O site desviou a chamada da API para outra página. O login ainda não foi confirmado.', 'api_redirect_route')
    if final.fragment:
        raise UserError('A API retornou uma referência de página, não um destino válido para a operação.', 'api_redirect_invalid')
    return urlunsplit(final)


class APIRedirects:
    """Session cookies are private to this source, never attached to other origins."""

    def __init__(self, session):
        self.session = session
        self.jar = None
        self.access_pause = None

    def _headers(self, url, headers):
        values = dict(headers or {})
        cookies = self.jar.filter_cookies(URL(url))
        # Explicit account credentials win over website cookies. Only a verified
        # JSON login response can select the application's authenticated account.
        cookies.pop('remix_userid', None)
        cookies.pop('remix_userkey', None)
        explicit = SimpleCookie()
        explicit.load(values.pop('Cookie', ''))
        cookies.update(explicit)
        if cookies:
            values['Cookie'] = cookies.output(header='', sep=';').strip()
        return values

    @asynccontextmanager
    async def request(self, method, url, *, data=None, headers=None):
        if self.access_pause and self.access_pause[0] > time.monotonic():
            raise UserError(self.access_pause[1], 'source_protected')
        original = validate_url(url)
        method = method.upper()
        if self.jar is None:
            self.jar = aiohttp.CookieJar()
        current = original
        seen = set()
        async with asyncio.timeout(40):
            for hop in range(4):
                values = self._headers(current, headers)
                state = (str(URL(current)), values.get('Cookie', ''))
                if state in seen:
                    raise UserError('O catálogo está repetindo o mesmo redirecionamento. A operação foi interrompida; não é uma confirmação de senha incorreta.', 'api_redirect_loop')
                seen.add(state)
                async with self.session.request(method, current, data=data, headers=values,
                                                allow_redirects=False) as response:
                    if response.status in {513, 517}:
                        # Read only a bounded error excerpt to classify the failure;
                        # neither the response nor cookies appear in logs or messages.
                        excerpt = bytearray()
                        async for part in response.content.iter_chunked(8192):
                            excerpt.extend(part[:max(0, 65536 - len(excerpt))])
                            if len(excerpt) >= 65536:
                                break
                        wall = b'diamwall' in bytes(excerpt).lower()
                        message = (
                            'O site bloqueou a conexão automatizada do bot (DiamWall). '
                            'A conta ainda não foi validada. É necessário um acesso à API autorizado pelo serviço; '
                            'isso não é uma confirmação de senha incorreta ou cota esgotada.'
                            if wall else
                            f'O site recusou a conexão do bot (HTTP {response.status}). '
                            'O login não foi confirmado e nenhum livro foi solicitado.'
                        )
                        self.access_pause = (time.monotonic() + 300, message)
                        log.warning('%s', json.dumps({
                            'event': 'api_access_refused', 'http_status': response.status,
                            'protection': 'diamwall' if wall else 'not_identified',
                            'local_pause_seconds': 300, 'files_requested': False,
                        }))
                        raise UserError(message, 'source_protected')
                    if response.status not in REDIRECT_CODES:
                        yield response
                        return
                    location = response.headers.get('Location', '')
                    report = {'event': 'api_redirect_check', 'http_status': response.status,
                              'hop': hop + 1, 'followed': False}
                    try:
                        # Validate before any follow-up request or accepting cookies.
                        target = redirect_target(original, current, location, response.status, method)
                        parts = urlsplit(target)
                        report.update(same_origin=True, route=route_class(target),
                                      same_path=parts.path == urlsplit(current).path,
                                      query_changed=parts.query != urlsplit(current).query)
                        response_cookies = getattr(response, 'cookies', None)
                        if response_cookies:
                            if len(response_cookies) > 64:
                                raise UserError('O catálogo retornou uma sessão inesperada.', 'api_redirect_invalid')
                            self.jar.update_cookies(response_cookies, response_url=URL(current))
                        next_headers = self._headers(target, headers)
                        report['cookies_changed'] = next_headers.get('Cookie', '') != values.get('Cookie', '')
                        if (str(URL(target)), next_headers.get('Cookie', '')) in seen:
                            raise UserError('O catálogo está repetindo o mesmo redirecionamento. A operação foi interrompida; não é uma confirmação de senha incorreta.', 'api_redirect_loop')
                        if hop == 3:
                            raise UserError('O catálogo redirecionou a operação mais vezes que o esperado. A consulta foi interrompida.', 'api_redirect_limit')
                        current = target
                        report['followed'] = True
                    except UserError as exc:
                        report['error'] = exc.code
                        # Class labels only: never log paths, query values, cookies,
                        # passwords, tokens, request bodies or response bodies.
                        try:
                            report['route'] = route_class(urljoin(current, location))
                        except (ValueError, TypeError):
                            report['route'] = 'invalid'
                        raise
                    finally:
                        log.info('%s', json.dumps(report))
        raise UserError('O redirecionamento não foi concluído.', 'api_redirect_limit')
