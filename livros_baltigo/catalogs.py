"""Explicit catalog routing. A failed provider never masquerades as an empty search."""
from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from urllib.parse import urlsplit

from .catalog import SOURCES
from .errors import UserError
from .models import SearchSpec


class CatalogRouter:
    def __init__(self, settings, legacy, gutenberg):
        self.settings = settings
        self.providers = {'zlibrary': legacy, 'gutenberg': gutenberg}
        self.states = {
            'zlibrary': {'status': 'not_verified' if settings.source_configured else 'not_configured'},
            'gutenberg': {'status': 'not_verified' if settings.public_catalog else 'disabled'},
        }
        self.legacy_retry_at = 0.0

    @property
    def available(self):
        return self.settings.source_configured or self.settings.public_catalog

    @property
    def state(self):
        states = [v['status'] for v in self.states.values() if v['status'] not in {'disabled', 'not_configured'}]
        if states and all(s == 'ready' for s in states):
            return 'ready'
        if 'ready' in states:
            return 'partial'
        if 'not_verified' in states:
            return 'not_verified'
        return 'unavailable'

    def provider(self, source):
        if source not in self.providers:
            raise UserError('Fonte desconhecida. Escolha uma opção em /fontes.', 'invalid_source')
        enabled = self.settings.source_configured if source == 'zlibrary' else self.settings.public_catalog
        if not enabled:
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
        # Explicit selection does not bypass a provider's own rate-limit or access-denial cooldown.
        try:
            async with asyncio.timeout(10 if source == 'zlibrary' else 60):
                result = await provider.search(replace(spec, source=source), page, limit)
            self.states[source] = {'status': 'ready', 'result_count': len(result.books)}
            return replace(result, source=source)
        except TimeoutError as exc:
            error = UserError(f'{SOURCES[source]} demorou a responder. Escolha outra fonte em /fontes.', 'source_timeout')
            self._failed(source, error)
            raise error from exc
        except UserError as exc:
            self._failed(source, exc)
            raise

    async def search(self, spec, page=1, limit=8):
        if spec.source not in SOURCES:
            raise UserError('Fonte de busca inválida.', 'invalid_source')
        if spec.source != 'auto':
            return await self._search_one(spec.source, spec, page, limit)
        order = []
        if self.settings.source_configured and time.monotonic() >= self.legacy_retry_at:
            order.append('zlibrary')
        if self.settings.public_catalog:
            order.append('gutenberg')
        if not order:
            raise UserError('Nenhuma fonte está disponível agora. Consulte /fontes.', 'catalog_unavailable')
        failures, empty = [], None
        for source in order:
            try:
                result = await self._search_one(source, spec, page, limit)
                if result.books:
                    notice = ''
                    if source == 'gutenberg' and self.settings.source_configured:
                        notice = ('Resultados do acervo independente do Project Gutenberg. '
                                  'Esta lista não confirma o funcionamento do Z-Library.')
                    return replace(result, notice=notice)
                empty = result
            except UserError as exc:
                failures.append(f'{SOURCES[source]}: {exc.message}')
        if empty is not None:
            notice = ('Uma fonte respondeu sem resultados. Outras fontes não puderam ser consultadas. '
                      'Isso não significa que o livro não exista.' if failures or self.legacy_retry_at > time.monotonic() else '')
            return replace(empty, notice=notice)
        raise UserError('Não consegui consultar as fontes.\n\n' + '\n'.join(failures), 'catalog_unavailable')

    async def details(self, book):
        return await self.for_book(book).details(book)

    async def cover(self, url):
        source = 'gutenberg' if urlsplit(url).hostname in {'www.gutenberg.org', 'gutenberg.org'} else 'zlibrary'
        return await self.provider(source).cover(url)

    async def quota(self):
        try:
            async with asyncio.timeout(10):
                return await self.provider('zlibrary').quota()
        except TimeoutError as exc:
            raise UserError('A consulta ao saldo do Z-Library expirou. A fonte pública não depende desse saldo.', 'source_timeout') from exc

    async def probe(self):
        async def check(source):
            if self.states[source]['status'] in {'disabled', 'not_configured'}:
                return
            try:
                # Lookup only. Neither an account download nor a Telegram message is sent.
                result = await self._search_one(source, SearchSpec('Dom Casmurro', 'portuguese', 'pdf', source), 1, 1)
                if not result.books:
                    self.states[source] = {'status': 'empty_probe', 'result_count': 0}
            except UserError:
                pass
            except Exception as exc:
                self.states[source] = {'status': 'unavailable', 'error': type(exc).__name__}
        await asyncio.gather(*(check(source) for source in self.providers))
        return {'status': self.state, 'sources': {k: dict(v) for k, v in self.states.items()}, 'download_tested': False}
