"""Synchronize optional Telegram metadata once; honor rate limits across restarts."""
from __future__ import annotations
import hashlib
import json
import logging
import time
from .errors import TelegramError

log = logging.getLogger(__name__)


async def sync_interface(telegram, settings, commands, bot_id):
    operations = []
    for language in ('', 'pt', 'en', 'es'):
        for scope in ({'type': 'default'}, {'type': 'all_private_chats'}):
            operations.append(('setMyCommands', {'commands': commands, 'scope': scope, 'language_code': language}))
    for uid in sorted(settings.admin_ids):
        for language in ('', 'pt', 'en', 'es'):
            operations.append(('setMyCommands', {'commands': commands, 'scope': {'type': 'chat', 'chat_id': uid}, 'language_code': language}))
    for method, field, text in (
        ('setMyName', 'name', settings.brand[:64]),
        ('setMyDescription', 'description', 'Sua próxima leitura começa aqui. Busque por título ou autor, salve seus favoritos e receba edições disponíveis em EPUB ou PDF. Tudo pelos botões, em português.'),
        ('setMyShortDescription', 'short_description', 'Livros, favoritos e novas leituras. Busque pelo título ou autor, sem precisar decorar comandos.'),
    ):
        for language in ('', 'pt', 'en', 'es'):
            operations.append((method, {field: text, 'language_code': language}))
    fingerprint = hashlib.sha256(json.dumps([bot_id, operations], sort_keys=True).encode()).hexdigest()
    path = settings.data_dir / 'interface-state.json'
    try:
        state = json.loads(path.read_text())
        if ((state.get('fingerprint') == fingerprint or (state.get('bot_id') == bot_id and state.get('rate_limited')))
                and state.get('retry_at', 0) > time.time()):
            return
    except (OSError, ValueError, TypeError, AttributeError):
        state = {}
    complete = True
    rate_limited = False
    retry_at = time.time() + 86400
    for method, payload in operations:
        try:
            await telegram.call(method, payload)
        except TelegramError as exc:
            complete = False
            log.warning('Interface opcional pendente: method=%s; code=%s', method, exc.code)
            if exc.code == 429:
                rate_limited = True
                # Do not hammer other locales or restart to evade Retry-After.
                retry_at = time.time() + max(60, exc.retry_after)
                break
            retry_at = time.time() + 3600
    try:
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'fingerprint': fingerprint, 'bot_id': bot_id, 'complete': complete, 'retry_at': retry_at, 'rate_limited': rate_limited}))
        temporary.replace(path)
    except OSError:
        log.warning('Cache de interface indisponível; atendimento permanece ativo.')
