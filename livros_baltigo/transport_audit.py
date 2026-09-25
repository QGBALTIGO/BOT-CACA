"""Bounded, redacted diagnostics for the configured origin only.

Uses the normal verified HTTPS client. No proxy, mirror, TLS downgrade, header
impersonation, CAPTCHA handling, credential logging or file requests.
"""
from __future__ import annotations
import json
import logging
import time
from urllib.parse import urlsplit, urljoin
import aiohttp
from .errors import UserError

log = logging.getLogger(__name__)


def redirect_summary(location: str, current: str) -> dict:
    """Only the public destination hostname is retained; no path or query."""
    if not location:
        return {}
    from .network import validate_url
    try:
        target = urlsplit(validate_url(urljoin(current, location)))
        original = urlsplit(current)
        return {'redirect_host': target.hostname,
                'redirect_same_origin': (target.hostname, target.port or 443) == (original.hostname, original.port or 443)}
    except (UserError, ValueError, TypeError):
        return {'redirect_unsafe': True}


def trace_config(host: str) -> aiohttp.TraceConfig:
    trace = aiohttp.TraceConfig()

    async def begin(session, ctx, params):
        ctx.enabled = params.url.host == host
        ctx.state = {'event': 'source_transport', 'method': params.method,
                     'dns_resolved': False, 'connection_ready': False,
                     'headers_sent': False, 'body_sent': False}
        ctx.started = time.monotonic()

    def event(field):
        async def record(session, ctx, params):
            if getattr(ctx, 'enabled', False):
                ctx.state[field] = True
        return record

    async def body(session, ctx, params):
        if getattr(ctx, 'enabled', False) and params.chunk:
            ctx.state['body_sent'] = True

    async def end(session, ctx, params):
        if not getattr(ctx, 'enabled', False):
            return
        if 300 <= params.response.status < 400:
            ctx.state.update(redirect_summary(params.response.headers.get('Location', ''), str(params.url)))
        ctx.state.update(http_status=params.response.status,
                         elapsed_ms=round((time.monotonic() - ctx.started) * 1000))
        log.info('%s', json.dumps(ctx.state))

    async def failure(session, ctx, params):
        if not getattr(ctx, 'enabled', False):
            return
        error = getattr(params.exception, 'os_error', None) or params.exception
        ctx.state.update(error=type(error).__name__, errno=getattr(error, 'errno', None),
                         elapsed_ms=round((time.monotonic() - ctx.started) * 1000))
        # No str(exception), URL, request body, headers, cookies or response text.
        log.warning('%s', json.dumps(ctx.state))

    trace.on_request_start.append(begin)
    trace.on_dns_resolvehost_end.append(event('dns_resolved'))
    trace.on_dns_cache_hit.append(event('dns_resolved'))
    trace.on_connection_create_end.append(event('connection_ready'))
    trace.on_connection_reuseconn.append(event('connection_ready'))
    trace.on_request_headers_sent.append(event('headers_sent'))
    trace.on_request_chunk_sent.append(body)
    trace.on_request_end.append(end)
    trace.on_request_exception.append(failure)
    return trace


async def probe_origin(session, base: str) -> dict:
    """One ordinary GET to the origin root; no login and no redirects."""
    from .network import validate_url
    result = {'event': 'public_origin_probe', 'credentials_sent': False,
              'files_requested': False}
    try:
        parsed = urlsplit(validate_url(base))
        if parsed.path not in {'', '/'} or parsed.query or parsed.fragment:
            raise ValueError('origin_required')
        async with session.get(base.rstrip('/') + '/', allow_redirects=False,
                               headers={'Accept': 'text/html'},
                               timeout=aiohttp.ClientTimeout(total=12, connect=8)) as response:
            result['http_status'] = response.status
            if 300 <= response.status < 400:
                result.update(redirect_summary(response.headers.get('Location', ''), base))
            result['is_html'] = 'text/html' in response.headers.get('Content-Type', '')
    except (aiohttp.ClientError, OSError, ValueError, UserError) as exc:
        error = getattr(exc, 'os_error', None) or exc
        result.update(error=type(error).__name__, errno=getattr(error, 'errno', None))
    log.info('%s', json.dumps(result))
    return result
