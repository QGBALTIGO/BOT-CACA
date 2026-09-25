from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

import aiohttp

from .errors import UserError


def public_ip(address) -> bool:
    return (address.is_global and not address.is_multicast and not address.is_unspecified
            and not address.is_reserved and not getattr(address, "is_site_local", False))


def validate_url(url: str, allowed_hosts: frozenset[str] | None = None) -> str:
    """Somente HTTPS público; aplicada também a CADA redirecionamento."""
    if not isinstance(url, str) or len(url) > 4096 or any(ord(c) < 33 for c in url):
        raise UserError("O serviço retornou um endereço inválido.", "unsafe_url")
    try:
        p = urlsplit(url)
        if p.scheme != "https" or not p.hostname or p.username or p.password or p.port not in {None, 443}:
            raise ValueError
        host = p.hostname.encode("idna").decode("ascii").lower().rstrip(".")
    except (ValueError, UnicodeError) as exc:
        raise UserError("O serviço retornou um endereço não autorizado.", "unsafe_url") from exc
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise UserError("Endereço de rede privada bloqueado.", "unsafe_url")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not public_ip(address):
        raise UserError("Endereço de rede privada bloqueado.", "unsafe_url")
    if allowed_hosts is not None and host not in allowed_hosts:
        raise UserError(
            f"O host de arquivo {host} não está autorizado. O administrador precisa verificar "
            "esse host e adicioná-lo a ZLIB_FILE_HOSTS no servidor.", "host_not_allowed"
        )
    return url


class PublicResolver(aiohttp.abc.AbstractResolver):
    """Filtra os IPs que o conector de fato utilizará, não só uma consulta prévia."""

    def __init__(self):
        self.inner = aiohttp.resolver.ThreadedResolver()

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET):
        records = await self.inner.resolve(host, port, family)
        if not records or any(not public_ip(ipaddress.ip_address(r["host"])) for r in records):
            raise OSError("Destino DNS não público bloqueado")
        return records

    async def close(self):
        await self.inner.close()


def new_session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(resolver=PublicResolver(), limit=20, ttl_dns_cache=60),
        cookie_jar=aiohttp.DummyCookieJar(),
        timeout=aiohttp.ClientTimeout(total=45, connect=10, sock_read=30),
        trust_env=False,
        headers={"User-Agent": "LivrosBaltigo/0.1 (+private Telegram client)"},
    )


async def limited_body(response, limit: int) -> bytes:
    chunks = bytearray()
    async for chunk in response.content.iter_chunked(64 * 1024):
        chunks.extend(chunk)
        if len(chunks) > limit:
            raise UserError("A resposta do serviço excedeu o limite de segurança.", "response_too_large")
    return bytes(chunks)
