"""Operational presentation, separate from book and edition navigation."""
from __future__ import annotations
import math
import time
from .app import BotApp
from .availability import pause_message
from .errors import UserError
from .models import esc
from . import views, __version__

CONNECTION_ERRORS = {'network', 'source_dns', 'source_tls', 'source_timeout',
                     'rate_limit', 'blocked', 'auth', 'html_auth', 'cooldown', 'unavailable'}


class OperationsApp(BotApp):
    def friendly_error(self, uid: int, error: UserError) -> str:
        if error.code in CONNECTION_ERRORS:
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
                 f'Renovação: meia-noite ({esc(zone)}).', '', '<b>Conta do catálogo</b>']
        try:
            quota = await self.source.quota()
            if quota.remaining is None:
                lines.append('O catálogo respondeu, mas não informou o saldo. Disponibilidade não confirmada.')
            else:
                lines.append(f'{quota.remaining} downloads disponíveis na conta compartilhada.')
        except UserError as exc:
            self.catalog_state, self.catalog_error = 'unavailable', exc.code
            lines.extend(['📡 <b>Saldo não consultado</b>', esc(self.friendly_error(uid, exc), 650),
                          '', 'Não foi possível verificar a cota da conta. Isso não é uma confirmação de limite esgotado.'])
        lines.extend(['', '<i>O limite deste bot e o saldo da conta do catálogo são contagens diferentes. '
                      'Esta tela não solicita arquivos.</i>'])
        keyboard = [[views.button('↻ Atualizar limites', 'menu:quota')],
                    [views.button('📚 Início', 'menu:home')]]
        await self._send(chat, '\n'.join(lines), keyboard, edit)

    async def show_status(self, uid: int, chat: int):
        if uid not in self.settings.admin_ids:
            raise UserError('Esta função é exclusiva do administrador.', 'admin_only')
        stats = await self.store.stats()
        state = getattr(self, 'catalog_state', 'not_verified')
        labels = {'ready': 'Último teste de consulta aprovado', 'unavailable': 'Conexão indisponível',
                  'not_verified': 'Ainda não verificado'}
        text = (f'🩺 <b>{esc(self.settings.brand)} · Administração</b>\n\n'
                f'Versão: {__version__}\nCatálogo: {labels.get(state, "Ainda não verificado")}\n'
                f'Usuários: {stats["users"]} · Favoritos: {stats["favorites"]}\n'
                f'Envios concluídos: {stats["delivered_30d"]}\n'
                f'Fila: {self.downloads.queue.qsize()} pedidos aguardando\n\n'
                '<i>Telegram conectado não significa catálogo disponível. '
                'Testar conexão verifica perfil e busca, sem baixar livros.</i>')
        await self.tg.message(chat, text, views.home_keyboard(True))
