"""Reproducible public-source verification. Never imports/uses Telegram credentials."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from .archive import InternetArchive
from .config import Settings
from .errors import UserError
from .gutenberg import Gutenberg
from .models import SearchSpec
from .network import new_session
from .omp import UniversityBooks
from .infolivros import InfoLivros


async def verify(output: Path):
    output.mkdir(parents=True, exist_ok=True)
    settings = Settings(bot_token='', admin_ids=frozenset(), base_url='', data_dir=output)
    report = {'version': '0.5.2', 'telegram_delivery_tested': False,
              'account_credentials_used': False, 'sources': {}, 'status': 'failed'}
    async with new_session() as session, new_session() as files:
        sources = {'gutenberg': Gutenberg(settings, session, files),
                   'archive': InternetArchive(settings, session, files),
                   'usp': UniversityBooks(settings, session, files, 'usp'),
                   'ufpb': UniversityBooks(settings, session, files, 'ufpb'),
                   'infolivros': InfoLivros(settings, session, files)}
        for name, provider in sources.items():
            result = {'checks': [], 'status': 'failed'}
            report['sources'][name] = result
            try:
                query = getattr(provider, 'probe_query', 'Dom Casmurro')
                page = await provider.search(SearchSpec(query, 'portuguese', 'pdf', name), 1, 2)
                assert page.books, 'positive_search_returned_no_books'
                result['checks'].append({'search': query, 'results': len(page.books), 'has_next': page.has_next})
                empty = await provider.search(SearchSpec('zzqvnonexistentbookzz', 'portuguese', 'pdf', name), 1, 1)
                assert not empty.books and not empty.has_next, 'empty_search_failed'
                result['checks'].append({'empty_search': 'passed'})
                if getattr(provider, 'supports_delivery', True) is False:
                    fresh = await provider.details(page.books[0])
                    result['status'] = 'search_only'
                    result['download_verified'] = False
                    result['limitation'] = 'PDF endpoint refused the server in run 36882386478; only external-page access is enabled.'
                    result['checks'].append({'external_page': fresh.source_url})
                    continue
                async def progress(current, total):
                    pass
                downloaded = False
                for book in page.books:
                    try:
                        fresh = await provider.details(book)
                        url, extension = await provider.file_info(fresh)
                        path = output / (name + '.' + extension)
                        count = await provider.download(url, extension, path, progress)
                        assert count > 1000 and path.read_bytes().startswith(b'%PDF-'), 'invalid_pdf'
                        result['checks'].append({'download': 'passed', 'format': extension,
                            'title': fresh.title, 'source_url': fresh.source_url, 'bytes': count,
                            'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
                        downloaded = True
                        break
                    except UserError as exc:
                        if exc.code not in {'file_too_large', 'file_missing'}:
                            raise
                        result['checks'].append({'unavailable_edition': book.id, 'reason': exc.code})
                assert downloaded, 'no_pdf_download_validated'
                result['status'] = 'passed'
            except Exception as exc:
                result['error'] = exc.code if isinstance(exc, UserError) else type(exc).__name__
                # Error descriptions may contain user data in other components;
                # this check only uses public constant search terms and no accounts.
                print(json.dumps({'source': name, 'status': 'failed', 'error': result['error']}), flush=True)
            finally:
                (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print(json.dumps({'source': name, **result}, ensure_ascii=False), flush=True)
    report['verification_scope'] = 'Four PDF delivery sources plus one external-search-only catalog; not five PDF sources.'
    report['status'] = 'passed' if all(item['status'] in {'passed', 'search_only'} for item in report['sources'].values()) else 'failed'
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=Path('public-052-verification'))
    args = parser.parse_args()
    result = asyncio.run(verify(args.output))
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
