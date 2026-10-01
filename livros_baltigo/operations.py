"""Operational status: local limits, source availability, and verified receipts are separate."""
from __future__ import annotations
import math
import time
from .app import BotApp
from .availability import pause_message
from .catalogs import CatalogRouter
from .errors import UserError
from .models import esc
from . import views, __version__

CONNECTION_ERRORS = {'network', 'source_dns', 'source_tls', 'source_timeout',
                     'rate_limit', 'blocked', 'source_protected', 'auth', 'html_auth', 'cooldown', 'unavailable'}


class OperationsApp(BotApp):
    def friendly_error(self, uid: int, error: UserError) -> str:
        if error.code in CONNECTION_ERRORS and not isinstance(self.source, CatalogRouter):
            until = getattr(self.source, 'retry_at', 0)
            remaining = max(0, math.ceil(until - time.monotonic())) if isinstance(until, (int, float)) else 0
            return pause_message(error.code, remaining)
        return super().friendly_error(uid, error)

    async def show_quota(self, uid: int, chat: int, edit=None):
        used = await self.store.daily_usage(uid, self.downloads.today())
        local_left = max(0, self.settings.daily_limit - used)
        zone = ('horário de Mato Grosso do Sul' if self.settings.timezone == 'America/Campo_Grande'
                else self.settings.timezone)
        lines = ['📊 <b>Suas leituras de hoje</b>', '',
                 f'<b>Neste bot:</b> {used} de {self.settings.daily_limit} pedidos utilizados ou reservados.',
                 f'Você ainda pode fazer até <b>{local_left} pedidos</b>, se houver disponibilidade no catálogo.',
                 f'Renovação: meia-noite ({esc(zone)}).']
        if isinstance(self.source, CatalogRouter) and self.settings.public_catalog:
            lines.extend(['', '<b>Project Gutenberg</b>',
                          'Não usa a conta nem o saldo do Z-Library. O limite diário do bot continua valendo.'])
        if self.settings.source_configured:
            lines.extend(['', '<b>Conta Z-Library</b>'])
            try:
                quota = await self.source.quota()
                if quota.remaining is None:
                    lines.append('O catálogo respondeu, mas não informou o saldo. Disponibilidade não confirmada.')
                else:
                    lines.append(f'{quota.remaining} downloads disponíveis na conta compartilhada.')
            except UserError as exc:
                lines.extend(['📡 <b>Saldo não consultado</b>', esc(self.friendly_error(uid, exc), 650),
                              'Isso não é uma confirmação de limite esgotado.'])
        lines.extend(['', '<i>Consultar esta tela não solicita arquivos. Pedidos sem confirmação do Telegram '
                      'continuam reservados para evitar duplicações.</i>'])
        keyboard = [[views.button('↻ Atualizar limites', 'menu:quota')],
                    [views.button('📂 Fontes', 'menu:sources'), views.button('📚 Início', 'menu:home')]]
        await self._send(chat, '\n'.join(lines), keyboard, edit)

    async def show_status(self, uid: int, chat: int):
        if uid not in self.settings.admin_ids:
            raise UserError('Esta função é exclusiva do administrador.', 'admin_only')
        stats = await self.store.stats()
        state = self.source.state if isinstance(self.source, CatalogRouter) else getattr(self, 'catalog_state', 'not_verified')
        labels = {'ready': 'Consultas verificadas', 'partial': 'Parcial: fontes independentes',
                  'unavailable': 'Conexão indisponível', 'not_verified': 'Ainda não verificado'}
        worker = self.downloads.worker
        text = (f'🩺 <b>{esc(self.settings.brand)} · Administração</b>\n\n'
                f'Versão: {__version__}\nCatálogo: {labels.get(state, "Ainda não verificado")}\n'
                f'Usuários: {stats["users"]} · Favoritos: {stats["favorites"]}\n'
                f'Envios com comprovante: {stats.get("verified_deliveries", 0)}\n'
                f'Histórico de conclusões, incluindo versões antigas: {stats["delivered_30d"]}\n'
                f'Fila: {self.downloads.queue.qsize()} pedidos aguardando\n'
                f'Processador de downloads: {"ativo" if worker and not worker.done() else "parado"}\n\n'
                '<i>Consulta aprovada não comprova entrega. /testarpdf usa o fluxo real; '
                '/comprovante mostra o último envio confirmado da sua conta.</i>')
        await self.tg.message(chat, text, views.home_keyboard(True))
