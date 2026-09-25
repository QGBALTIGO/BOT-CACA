"""Safe error classification and retry messages. No provider data is rendered."""
from __future__ import annotations

import asyncio
import math
import socket
import ssl
import time
from email.utils import parsedate_to_datetime


def retry_after_seconds(value: str | None, now: float | None = None) -> int:
    """Honor a valid Retry-After, including an HTTP date; never shorten it."""
    now = time.time() if now is None else now
    try:
        if value and value.strip().isdigit():
            return max(1, int(value.strip()))
        if value:
            return max(1, math.ceil(parsedate_to_datetime(value).timestamp() - now))
    except (ValueError, TypeError, OverflowError):
        pass
    return 60


def network_kind(exc: BaseException) -> str:
    error = getattr(exc, 'os_error', None) or exc
    if isinstance(error, socket.gaierror):
        return 'source_dns'
    if isinstance(error, ssl.SSLError) or 'SSL' in type(exc).__name__ or 'Certificate' in type(exc).__name__:
        return 'source_tls'
    if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
        return 'source_timeout'
    if type(error).__name__ == 'UnsafeDNS':
        return 'unsafe_url'
    return 'network'


def pause_message(code: str, remaining: int = 0) -> str:
    reasons = {
        'network': 'A conexão com o catálogo foi interrompida. Isso não indica que seus downloads acabaram.',
        'source_dns': 'O servidor não conseguiu localizar o endereço do catálogo.',
        'source_tls': 'Não foi possível estabelecer uma conexão segura com o catálogo. A validação de segurança foi mantida.',
        'source_timeout': 'O catálogo não respondeu dentro do tempo de espera.',
        'rate_limit': 'O catálogo limitou temporariamente as consultas (HTTP 429).',
        'blocked': 'O catálogo recusou a consulta (HTTP 403). O administrador precisa verificar o acesso.',
        'auth': 'O catálogo não aceitou a sessão. O administrador precisa verificar o acesso à conta.',
        'html_auth': 'A sessão do catálogo precisa ser verificada pelo administrador.',
        'unavailable': 'O catálogo está indisponível no momento.',
        'unsafe_url': 'A conexão foi interrompida por uma verificação de segurança.',
        'cooldown': 'O bot está aguardando antes de permitir outra consulta.',
    }
    text = reasons.get(code, reasons['cooldown'])
    if remaining > 0:
        interval = f'{remaining} segundos' if remaining < 120 else f'{math.ceil(remaining / 60)} minutos'
        if code == 'rate_limit':
            text += f' Nova consulta permitida em aproximadamente {interval}.'
        else:
            text += f' O bot aguarda {interval} antes de permitir outra tentativa; essa espera é local.'
    return text
