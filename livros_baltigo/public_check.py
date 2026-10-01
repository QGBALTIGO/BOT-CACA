"""Bounded live verification of public catalogs; does not use Telegram or any account secret."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from .gutenberg import Gutenberg
from .models import SearchSpec
from .network import new_session


async def check(output: Path):
    output.mkdir(parents=True, exist_ok=True)
    settings = SimpleNamespace(search_ttl=900, max_file_bytes=49_000_000)
    report = {'telegram_delivery_tested': False, 'source': 'Project Gutenberg', 'checks': []}
    async with new_session() as api, new_session() as files:
        source = Gutenberg(settings, api, files)
        page = await source.search(SearchSpec('Dom Casmurro', 'portuguese', 'any', 'gutenberg'), 1, 8)
        if not any(b.id == '55752' for b in page.books):
            raise ValueError('Known book not found with actual language filter')
        report['checks'].append({'search': 'Dom Casmurro', 'results': len(page.books)})
        authors = await source.search(SearchSpec('Machado', 'portuguese', 'any', 'gutenberg'), 1, 8)
        if not authors.books:
            raise ValueError('Author search returned no results')
        if authors.has_next:
            next_page = await source.search(SearchSpec('Machado', 'portuguese', 'any', 'gutenberg'), 2, 8)
            if not next_page.books or {b.key for b in authors.books} & {b.key for b in next_page.books}:
                raise ValueError('Pagination missing or repeating entries')
        report['checks'].append({'search': 'Machado', 'results': len(authors.books), 'pagination': authors.has_next})
        chosen = next(b for b in page.books if b.id == '55752')
        async def progress(received, total):
            return None
        for extension in ('pdf', 'epub'):
            book = replace(chosen, extension=extension, hash='pg_' + extension)
            fresh = await source.details(book)
            url, fmt = await source.file_info(fresh)
            path = output / ('Dom_Casmurro.' + extension)
            size = await source.download(url, fmt, path, progress)
            with path.open('rb') as handle:
                digest = hashlib.file_digest(handle, 'sha256').hexdigest()
            report['checks'].append({'format': extension, 'bytes': size, 'sha256': digest,
                                     'title': fresh.title, 'source_url': fresh.source_url})
        report['status'] = 'passed'
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('public-verification'))
    args = parser.parse_args()
    asyncio.run(check(args.output))


if __name__ == '__main__':
    main()
