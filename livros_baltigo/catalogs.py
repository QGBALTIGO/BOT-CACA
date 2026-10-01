"""Independent catalog routing: bounded fallback, isolated failures, stable pagination."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from urllib.parse import urlsplit

from .catalog import SOURCES
from .errors import UserError
from .models import SearchSpec

log = logging.getLogger(__name__)


class CatalogRouter:
    def __init__(self, settings, legacy, gutenberg, extra=None):
        self.settings = settings
        self.providers = {'zlibrary': legacy, 'gutenberg': gutenberg}
        for key, provider in (extra or {}).items():
            if key not in SOURCES or key in {'auto', 'zlibrary', 'gutenberg'}:
                raise ValueError('Unknown extra catalog')
            self.providers[key] = provider
        self.enabled = set(extra or {})
        if settings.source_configured:
            self.enabled.add('zlibrary')
        if settings.public_catalog:
            self.enabled.add('gutenberg')
        self.states = {name: {'status': 'not_verified' if name in self.enabled else
                       'not_configured' if name == 'zlibrary' else 'disabled'}
                       for name in SOURCES if name != 'auto'}
        self.legacy_retry_at = 0.0

    @property
    def available(self):
        return bool(self.enabled)

    @property
    def state(self):
        states = [self.states[name]['status'] for name in self.enabled]
        if states and all(s == 'ready' for s in states):
            return 'ready'
        if 'ready' in states:
            return 'partial'
        if 'not_verified' in states:
            return 'not_verified'
        return 'unavailable'

    def provider(self, source):
        if source not in SOURCES or source == 'auto':
            raise UserError('Fonte desconhecida. Escolha uma opção em /fontes.', 'invalid_source')
        if source not in self.enabled or source not in self.providers:
            raise UserError(f'{SOURCES[source]} não está configurado ou está desativado. Escolha outra fonte em /fontes.', 'source_disabled')
        return self.providers[source]

    def for_book(self, book):
        return self.provider(book.source)

    def _failed(self, source, error):
        self.states[source] = {'status': 'unavailable', 'error': error.code}
        if source == 'zlibrary':
            self.legacy_retry_at = time.monotonic() + 120

    async def _search_one(self, source, spec, page, limit):
        provider = self.provider(source)
        try:
            async with asyncio.timeout(10 if source == 'zlibrary' else 28):
                result = await provider.search(replace(spec, source=source), page, limit)
            self.states[source] = {'status': 'ready', 'result_count': len(result.books)}
            return replace(result, source=source)
        except TimeoutError as exc:
            error = UserError(f'{SOURCES[source]} demorou a responder. Escolha outra fonte em /fontes.', 'source_timeout')
            self._failed(source, error)
            raise error from exc
        except UserError as exc:
            if exc.code not in {'invalid_filter', 'invalid_search', 'refine_search', 'expired_search', 'invalid_page'}:
                self._failed(source, exc)
            raise
        except Exception as exc:
            # One parser/network failure must not disable every independent catalog.
            error = UserError(f'{SOURCES[source]} não pôde concluir a consulta. Tente outro catálogo.', 'provider_error')
            log.warning('Falha isolada de catálogo: source=%s; type=%s', source, type(exc).__name__)
            self._failed(source, error)
            raise error from exc

    async def search(self, spec, page=1, limit=8):
        if spec.source not in SOURCES:
            raise UserError('Fonte de busca inválida.', 'invalid_source')
        if spec.source != 'auto':
            return await self._search_one(spec.source, spec, page, limit)
        first = [name for name in ('zlibrary', 'gutenberg', 'archive') if name in self.enabled]
        if time.monotonic() < self.legacy_retry_at and 'zlibrary' in first:
            first.remove('zlibrary')
        second = [name for name in ('usp', 'ufpb') if name in self.enabled]
        if not first and not second:
            raise UserError('Nenhuma fonte está disponível agora. Consulte /fontes.', 'catalog_unavailable')
        failures, empty = [], None
        for batch in (first, second):
            responses = await asyncio.gather(*(self._search_one(name, spec, page, limit) for name in batch), return_exceptions=True)
            for source, result in zip(batch, responses):
                if isinstance(result, BaseException):
                    if isinstance(result, asyncio.CancelledError):
                        raise result
                    message = result.message if isinstance(result, UserError) else 'Falha de consulta.'
                    failures.append(f'{SOURCES[source]}: {message}')
                    continue
                if result.books:
                    notice = result.notice
                    if source != 'zlibrary' and self.settings.source_configured:
                        notice = (notice + ' ' if notice else '') + 'Acervo independente; estes resultados não confirmam o funcionamento do Z-Library.'
                    return replace(result, notice=notice)
                empty = result
        if empty is not None:
            notice = 'Nenhuma edição encontrada nas fontes que responderam.'
            if failures or self.legacy_retry_at > time.monotonic():
                notice += ' Há fontes indisponíveis; isso não confirma ausência nos outros acervos.'
            return replace(empty, notice=notice)
        raise UserError('Não consegui consultar as fontes.\n\n' + '\n'.join(failures), 'catalog_unavailable')

    async def details(self, book):
        return await self.for_book(book).details(book)

    async def cover(self, url):
        host = urlsplit(url).hostname or ''
        if host in {'www.gutenberg.org', 'gutenberg.org'}:
            return await self.provider('gutenberg').cover(url)
        for name in ('archive', 'usp', 'ufpb'):
            if name not in self.enabled:
                continue
            provider = self.providers[name]
            try:
                provider.allowed_url(url)
            except UserError:
                continue
            return await provider.cover(url)
        if 'zlibrary' in self.enabled:
            return await self.provider('zlibrary').cover(url)
        return None

    async def quota(self):
        try:
            async with asyncio.timeout(10):
                return await self.provider('zlibrary').quota()
        except TimeoutError as exc:
            raise UserError('A consulta ao saldo do Z-Library expirou. As fontes públicas não dependem desse saldo.', 'source_timeout') from exc

    async def probe(self):
        async def check(source):
            if source not in self.enabled:
                return
            provider = self.providers[source]
            query = getattr(provider, 'probe_query', 'Dom Casmurro')
            if not isinstance(query, str):
                query = 'Dom Casmurro'
            try:
                await self._search_one(source, SearchSpec(query, 'portuguese', 'pdf', source), 1, 1)
            except UserError:
                pass
        await asyncio.gather(*(check(source) for source in self.providers))
        return {'status': self.state, 'sources': {k: dict(v) for k, v in self.states.items()}, 'download_tested': False}
