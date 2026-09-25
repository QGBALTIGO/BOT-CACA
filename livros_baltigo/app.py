from __future__ import annotations

import asyncio
import json
import logging
import re
import signal
import time
import weakref

from . import __version__
from .catalog import group_books
from collections import OrderedDict
from .errors import TelegramError, UserError
from .jobs import Downloads
from .models import SearchSpec, esc
from .provider import ZLibrary
from .storage import Store
from .telegram import Telegram
from . import views

log = logging.getLogger(__name__)
COMMANDS = [
    ("inicio", "Abrir o menu"), ("buscar", "Buscar por título, autor ou ISBN"),
    ("biblioteca", "Minha biblioteca"), ("historico", "Livros enviados nos últimos 30 dias"),
    ("filtros", "Escolher idioma e formato"), ("limites", "Consultar limites"),
    ("id", "Mostrar meu ID"), ("privacidade", "Dados e funcionamento"),
    ("ajuda", "Como usar o bot"),
]


class BotApp:
    def __init__(self, settings, telegram: Telegram, source: ZLibrary, store: Store):
        self.settings, self.tg, self.source, self.store = settings, telegram, source, store
        self.downloads = Downloads(settings, store, source, telegram)
        self.stop_event = asyncio.Event()
        self.source_gate = asyncio.Semaphore(2)
        self.users: weakref.WeakValueDictionary[int, asyncio.Lock] = weakref.WeakValueDictionary()
        self.last_action: dict[int, float] = {}
        self.started = time.time()
        self.last_poll = 0.0
        self.pending: set[asyncio.Task] = set()
        self.username = ""
        self.search_locks = weakref.WeakValueDictionary()
        self.detail_locks = weakref.WeakValueDictionary()
        self.detail_cache = OrderedDict()
        self.cover_cache = OrderedDict()
        self.cover_bytes = 0
        self.catalog_state = "not_verified"
        self.catalog_error = ""
        self.probe_task = None

    async def initialize(self):
        await self.store.init()
        me = await self.tg.call("getMe")
        self.username = me.get("username", "")
        webhook = await self.tg.call("getWebhookInfo")
        if webhook.get("url"):
            raise UserError("Este token já tem um webhook configurado. Use um bot novo ou remova conscientemente a integração anterior antes de iniciar polling.", "existing_webhook")
        commands = [{"command": command, "description": description} for command, description in COMMANDS]
        # Substitui inclusive os menus antigos por idioma deixados por outras integrações.
        for language in ("", "pt", "en", "es"):
            for scope in ({"type": "default"}, {"type": "all_private_chats"}):
                await self.tg.call("setMyCommands", {"commands": commands, "scope": scope, "language_code": language})
        for uid in self.settings.admin_ids:
            for language in ("", "pt", "en", "es"):
                try:
                    await self.tg.call("setMyCommands", {"commands": commands, "scope": {"type": "chat", "chat_id": uid}, "language_code": language})
                except TelegramError as exc:
                    # Um administrador que ainda não abriu o bot não impede o início.
                    log.warning("Menu individual pendente: código=%s", exc.code)
        for method, field, text in (
            ("setMyName", "name", self.settings.brand[:64]),
            ("setMyDescription", "description", "Sua próxima leitura começa aqui. Busque por título ou autor, salve seus favoritos e receba edições disponíveis em EPUB ou PDF. Tudo pelos botões, em português."),
            ("setMyShortDescription", "short_description", "Livros, favoritos e novas leituras. Busque pelo título ou autor, sem precisar decorar comandos."),
        ):
            for language in ("", "pt", "en", "es"):
                try:
                    await self.tg.call(method, {field: text, "language_code": language})
                except TelegramError as exc:
                    log.warning("Metadados não atualizados: método=%s; código=%s", method, exc.code)
        self.downloads.start()
        log.info("Identidade Telegram validada: @%s; interface pt-BR; fonte=%s", self.username, "configurada_nao_validada" if self.settings.source_configured else "aguardando_credenciais")

    async def probe_source(self):
        """One bounded startup check; no file request or download."""
        report = {"event": "catalog_probe", "download_tested": False}
        try:
            async with asyncio.timeout(40):
                async with self.source_gate:
                    quota = await self.source.quota()
                    page = await self.source.search(SearchSpec("Dom Casmurro"), 1, self.settings.page_size)
            self.catalog_state = "ready"
            report.update(status="ready", result_count=len(page.books), quota_known=quota.remaining is not None)
        except UserError as exc:
            self.catalog_state, self.catalog_error = "unavailable", exc.code
            report.update(status="unavailable", error=exc.code)
        except Exception as exc:
            self.catalog_state, self.catalog_error = "unavailable", "probe_failed"
            report.update(status="unavailable", error=type(exc).__name__)
        log.info("Diagnóstico do catálogo: %s", json.dumps(report))
        marker = self.settings.data_dir / ("catalog-notice-" + __version__)
        if not marker.exists():
            notice = ("📚 <b>Livros Baltigo atualizado</b>\n\n"
                      "Busca, escolha de edições, idiomas e favoritos estão instalados.\n\n")
            notice += ("✅ Consulta real à fonte concluída. Envie um título para começar."
                       if self.catalog_state == "ready" else
                       "⚠️ A conexão com a fonte ainda falhou no teste real. Os menus funcionam, mas a busca depende dessa conexão. Consulte Administração → Testar conexão.")
            for uid in sorted(self.settings.admin_ids)[:1]:
                try:
                    await self.tg.message(uid, notice, views.home_keyboard(True))
                    await asyncio.to_thread(marker.write_text, "sent", encoding="utf-8")
                except TelegramError as exc:
                    log.warning("Aviso de atualização pendente: code=%s", exc.code)

    async def _send(self, chat: int, text: str, keyboard=None, edit=None):
        if edit and "text" in edit:
            try:
                return await self.tg.edit(chat, edit["message_id"], text, keyboard)
            except TelegramError as exc:
                if exc.code not in {400, 403}:
                    raise
        return await self.tg.message(chat, text, keyboard)

    def friendly_error(self, uid: int, error: UserError) -> str:
        return error.message if uid in self.settings.admin_ids else views.PUBLIC_ERRORS.get(error.code, error.message)

    async def show_home(self, chat: int, edit=None):
        admin = chat in self.settings.admin_ids
        preferences = await self.store.user(chat)
        text = views.home_text(self.settings.brand, preferences)
        if not self.settings.source_configured:
            if admin:
                text += "\n\n<b>Configuração pendente</b>\nAs buscas aguardam ZLIB_BASE_URL e as credenciais da conta nas variáveis privadas do Railway. Use Testar conexão depois de configurá-las."
            else:
                text += "\n\n<i>O catálogo está em preparação. As buscas estarão disponíveis assim que a conexão for concluída.</i>"
        if self.settings.source_configured and self.catalog_state == "unavailable":
            text += "\n\n⚠️ <i>A conexão com o catálogo está indisponível. Seus favoritos e filtros continuam salvos.</i>"
        await self._send(chat, text, views.home_keyboard(admin), edit)

    async def show_settings(self, uid: int, chat: int, edit=None):
        preferences = await self.store.user(uid)
        last = await self.store.latest_session(uid)
        text = ("🌐 <b>Sua leitura, do seu jeito</b>\n\n"
                f"Idioma: <b>{views.LANGUAGES[preferences['language']]}</b>\n"
                f"Formato: <b>{views.FORMATS[preferences['extension']]}</b>\n\n"
                "Toque para escolher. As preferências ficam salvas só para você.\n"
                "<i>O filtro encontra edições no idioma escolhido; não traduz os livros.</i>")
        if last:
            text += f"\n\nÚltima busca: <i>{esc(last.query, 100)}</i>"
        await self._send(chat, text, views.settings_keyboard(preferences, bool(last)), edit)

    async def show_search(self, uid: int, chat: int, spec: SearchSpec, ident: str, page=1, edit=None):
        message = edit
        if message and "text" in message:
            await self.tg.edit(chat, message["message_id"], "🔎 Buscando livros…")
        else:
            message = await self.tg.message(chat, "🔎 Buscando livros…")
        try:
            result = await self.search_page(spec, page)
            groups = group_books(result.books)
            targets = []
            for group in groups:
                if len(group.books) == 1:
                    targets.append(f"book:{group.books[0].key}")
                else:
                    choice = await self.store.choice(uid, list(group.books), self.settings.session_ttl)
                    targets.append(f"editions:{choice}:1")
            text, keyboard = views.group_results(result, spec.query, ident, groups, targets,
                f"{views.LANGUAGES[spec.language]} · {views.FORMATS[spec.extension]}")
            await self._send(chat, text, keyboard, message)
        except UserError as exc:
            await self._send(chat, esc(self.friendly_error(uid, exc), 1000), views.home_keyboard(), message)

    async def search_page(self, spec: SearchSpec, page: int):
        key = spec.cache_key(page, self.settings.page_size)
        lock = self.search_locks.setdefault(key, asyncio.Lock())
        async with lock:
            result = await self.store.cached_page(key)
            if result is None:
                async with self.source_gate:
                    result = await self.source.search(spec, page, self.settings.page_size)
                await self.store.cache_page(key, result, self.settings.search_ttl)
            return result

    async def show_editions(self, uid: int, chat: int, ident: str, page=1, edit=None):
        books = await self.store.get_choice(uid, ident)
        if not books:
            raise UserError("Esta seleção expirou ou pertence a outra pessoa. Faça uma nova busca.", "expired_search")
        if not 1 <= page <= (len(books) + 5) // 6:
            raise UserError("Página de edições inválida.", "invalid_page")
        text, keyboard = views.edition_results(books, ident, page)
        await self._send(chat, text, keyboard, edit)

    async def book_details(self, book):
        lock = self.detail_locks.setdefault(book.key, asyncio.Lock())
        async with lock:
            item = self.detail_cache.get(book.key)
            if item and item[0] > time.monotonic():
                self.detail_cache.move_to_end(book.key)
                return item[1]
            async with self.source_gate:
                fresh = await self.source.details(book)
            await self.store.save_books([fresh])
            self.detail_cache[book.key] = (time.monotonic() + 600, fresh)
            self.detail_cache.move_to_end(book.key)
            while len(self.detail_cache) > 128:
                self.detail_cache.popitem(last=False)
            return fresh

    async def book_cover(self, url):
        if not url:
            return None
        item = self.cover_cache.get(url)
        if item and item[0] > time.monotonic():
            self.cover_cache.move_to_end(url)
            return item[1]
        try:
            image = await asyncio.wait_for(self.source.cover(url), timeout=4)
        except (asyncio.TimeoutError, UserError):
            return None
        if image and len(image) <= 2_000_000:
            previous = self.cover_cache.pop(url, None)
            if previous:
                self.cover_bytes -= len(previous[1])
            self.cover_cache[url] = (time.monotonic() + 600, image)
            self.cover_bytes += len(image)
            while self.cover_bytes > 8_000_000 or len(self.cover_cache) > 32:
                _, old = self.cover_cache.popitem(last=False)
                self.cover_bytes -= len(old[1])
        return image

    async def new_search(self, uid: int, chat: int, query: str, edit=None):
        query = " ".join(query.split())
        if not 1 <= len(query) <= 200:
            raise UserError("Digite um título, autor ou ISBN com até 200 caracteres.", "invalid_query")
        preferences = await self.store.user(uid)
        spec = SearchSpec(query, preferences["language"], preferences["extension"])
        ident = await self.store.session(uid, spec, self.settings.session_ttl)
        await self.show_search(uid, chat, spec, ident, edit=edit)

    async def show_book(self, uid: int, chat: int, key: str, back: str = "", edit=None):
        book = await self.store.book(key)
        if book is None:
            raise UserError("Esta edição não está mais no cache. Faça uma nova busca.", "expired_book")
        try:
            book = await self.book_details(book)
        except UserError:
            # Uma indisponibilidade dos detalhes não apaga os metadados da busca.
            pass
        favorite = await self.store.is_favorite(uid, key)
        keyboard = views.book_keyboard(book, favorite, back)
        caption = views.book_card(book)
        image = await self.book_cover(book.cover)
        if image:
            try:
                await self.tg.photo(chat, image, caption, keyboard)
                return
            except TelegramError as exc:
                # Não reenviar após timeout: a foto pode já ter sido entregue.
                if exc.code not in {400, 413}:
                    raise
        await self._send(chat, caption, keyboard, edit)

    async def show_shelf(self, uid: int, chat: int, page=1, history=False, edit=None):
        result = await self.store.shelf(uid, page, self.settings.page_size, history)
        title = "Histórico · últimos 30 dias" if history else "Minha biblioteca"
        text, keyboard = views.results(result, title, "histpage" if history else "favpage")
        if not result.books:
            text = f"<b>{title}</b>\n\n" + ("Nenhum envio concluído nesta página." if history else "Nenhum favorito nesta página. Busque um livro e toque em Salvar nos favoritos.")
        await self._send(chat, text, keyboard, edit)

    async def show_quota(self, uid: int, chat: int, edit=None):
        used = await self.store.daily_usage(uid, self.downloads.today())
        text = ["<b>Seus limites</b>", "", f"Limite local: {used}/{self.settings.daily_limit} pedidos reservados ou concluídos hoje.",
                f"Renovação local: meia-noite em {esc(self.settings.timezone)}."]
        try:
            quota = await self.source.quota()
            text.extend(["", "<b>Conta conectada à fonte</b>"])
            if quota.remaining is None:
                text.append("A fonte não informou uma cota completa. Não há estimativa presumida.")
            else:
                text.append(f"Disponíveis: {quota.remaining} · Usados: {quota.used} · Limite: {quota.limit}")
        except UserError as exc:
            text.extend(["", esc(self.friendly_error(uid, exc), 400)])
        text.extend(["", "A conta da fonte é compartilhada pelos usuários autorizados deste bot. Seu período de renovação pode ser diferente do limite local. Falhas após solicitar o arquivo podem consumir a cota da fonte."])
        await self._send(chat, "\n".join(text), views.home_keyboard(), edit)

    async def show_status(self, uid: int, chat: int):
        if uid not in self.settings.admin_ids:
            raise UserError("Este comando é exclusivo do administrador.", "admin_only")
        stats = await self.store.stats()
        alive = self.downloads.worker is not None and not self.downloads.worker.done()
        await self.tg.message(chat, f"<b>{esc(self.settings.brand)} · diagnóstico</b>\n\n"
                              f"Fonte: {'configurada (conexão não validada)' if self.settings.source_configured else 'aguardando credenciais'}\n"
                              f"Versão: {__version__}\nAcesso: {'público' if self.settings.public_access else 'privado'}\n"
                              f"Fila: {self.downloads.queue.qsize()} aguardando\nWorker: {'ativo' if alive else 'parado'}\n"
                              f"Usuários registrados: {stats['users']}\nFavoritos: {stats['favorites']}\n"
                              f"Envios nos últimos 30 dias: {stats['delivered_30d']}\n"
                              f"Em execução: {int((time.time() - self.started) / 60)} minutos")

    async def check_source(self, uid: int, chat: int):
        if uid not in self.settings.admin_ids:
            raise UserError("Esta função é exclusiva do administrador.", "admin_only")
        notice = await self.tg.message(chat, "Verificando perfil, cota e busca. Nenhum arquivo será solicitado…")
        try:
            async with self.source_gate:
                quota = await self.source.quota()
                page = await self.source.search(SearchSpec("Dom Casmurro", "portuguese", "any"), 1, 1)
            self.catalog_state, self.catalog_error = "ready", ""
            value = str(quota.remaining) if quota.remaining is not None else "não informada"
            text = ("<b>Conexão de consulta validada</b>\n\n"
                    f"Perfil e busca responderam corretamente.\nCota restante: {value}.\n"
                    f"Resultados na página de teste: {len(page.books)}.\n\n"
                    "Nenhum livro foi baixado. O envio de arquivo ainda deve ser validado com uma edição autorizada.")
        except UserError as exc:
            self.catalog_state, self.catalog_error = "unavailable", exc.code
            text = f"<b>A conexão ainda não está pronta</b>\n\n{esc(exc.message, 800)}"
        await self._send(chat, text, views.home_keyboard(True), notice)

    async def _callback(self, uid: int, chat: int, data: str, message: dict):
        if data == "menu:search":
            await self._send(chat, "<b>Qual será a próxima leitura?</b>\n\nEnvie o nome do livro, autor ou ISBN.\nExemplo: <i>Dom Casmurro</i>", views.home_keyboard(), edit=message)
        elif data == "menu:repeat":
            last = await self.store.latest_session(uid)
            if not last:
                raise UserError("Sua busca expirou. Envie novamente o nome do livro.", "expired_search")
            await self.new_search(uid, chat, last.query, edit=message)
        elif re.fullmatch(r"searchall:[a-f0-9]{12}", data):
            spec = await self.store.get_session(uid, data.split(":")[1])
            if spec is None:
                raise UserError("Sua busca expirou. Envie novamente o nome do livro.", "expired_search")
            broad = SearchSpec(spec.query, "any", spec.extension)
            ident = await self.store.session(uid, broad, self.settings.session_ttl)
            await self.show_search(uid, chat, broad, ident, edit=message)
        elif re.fullmatch(r"editions:[a-f0-9]{12}:\d{1,4}", data):
            _, ident, page = data.split(":")
            await self.show_editions(uid, chat, ident, int(page), message)
        elif re.fullmatch(r"open:[a-f0-9]{12}:\d{1,4}", data):
            _, ident, index = data.split(":")
            books = await self.store.get_choice(uid, ident)
            index = int(index)
            if books is None or not 0 <= index < len(books):
                raise UserError("Esta edição expirou ou pertence a outra pessoa. Faça uma nova busca.", "expired_search")
            await self.show_book(uid, chat, books[index].key, f"editions:{ident}:{index // 6 + 1}", message)
        elif data == "menu:settings":
            await self.show_settings(uid, chat, message)
        elif data == "menu:quota":
            await self.show_quota(uid, chat, message)
        elif data == "menu:help":
            await self._command(uid, chat, "/ajuda")
        elif data == "menu:privacy":
            await self._command(uid, chat, "/privacidade")
        elif data == "admin:status":
            await self.show_status(uid, chat)
        elif data == "admin:check":
            await self.check_source(uid, chat)
        elif data == "menu:home":
            await self.show_home(chat, message)
        elif re.fullmatch(r"page:[a-f0-9]{12}:\d{1,4}", data):
            _, ident, page = data.split(":")
            spec = await self.store.get_session(uid, ident)
            if spec is None:
                raise UserError("Esta busca expirou ou pertence a outro usuário. Faça uma nova busca.", "expired_search")
            await self.show_search(uid, chat, spec, ident, int(page), message)
        elif re.fullmatch(r"book:[a-f0-9]{16}", data):
            await self.show_book(uid, chat, data.split(":")[1], edit=message)
        elif re.fullmatch(r"favorite:[a-f0-9]{16}:[01](?::[a-f0-9]{12}:\d{1,4})?", data):
            _, key, enabled, *context = data.split(":")
            back = ""
            if context:
                choices = await self.store.get_choice(uid, context[0])
                if not choices or key not in {b.key for b in choices}:
                    raise UserError("Esta seleção expirou ou pertence a outra pessoa.", "expired_search")
                back = f"editions:{context[0]}:{context[1]}"
            book = await self.store.book(key)
            if book is None:
                raise UserError("Livro não encontrado. Faça uma nova busca.", "expired_book")
            await self.store.favorite(uid, key, enabled == "1")
            await self.tg.markup(chat, message["message_id"], views.book_keyboard(book, enabled == "1", back))
        elif re.fullmatch(r"(?:favpage|histpage):\d{1,4}", data):
            action, page = data.split(":")
            await self.show_shelf(uid, chat, int(page), action == "histpage", message)
        elif data.startswith("setting:") and len(data.split(":")) == 3:
            _, field, value = data.split(":")
            await self.store.preferences(uid, field, value)
            await self.show_settings(uid, chat, message)
        elif re.fullmatch(r"download:[a-f0-9]{16}", data):
            book = await self.store.book(data.split(":")[1])
            if book is None:
                raise UserError("Esta edição expirou. Faça uma nova busca.", "expired_book")
            if not self.settings.source_configured:
                raise UserError(views.PUBLIC_ERRORS["setup_required"], "setup_required")
            progress = await self.tg.message(chat, "📥 Preparando o pedido…")
            try:
                _, position = await self.downloads.enqueue(uid, chat, progress["message_id"], book)
                # O worker assume a mensagem daqui em diante, evitando sobrescrever progresso mais novo.
                log.info("Pedido aceito na fila; posição inicial=%s", position)
            except UserError as exc:
                await self.tg.edit(chat, progress["message_id"], esc(self.friendly_error(uid, exc), 800))
        else:
            raise UserError("Este botão não é mais válido. Abra o menu para continuar.", "invalid_callback")

    async def _command(self, uid: int, chat: int, text: str):
        head, _, arg = text.partition(" ")
        command = head.split("@")[0].lower()
        if command in {"/start", "/inicio", "/menu"}:
            await self.show_home(chat)
        elif command == "/buscar":
            if arg.strip():
                await self.new_search(uid, chat, arg)
            else:
                await self.tg.message(chat, "Escreva o título, autor ou ISBN na conversa. Exemplo: Dom Casmurro.", views.home_keyboard())
        elif command in {"/favoritos", "/biblioteca"}:
            await self.show_shelf(uid, chat)
        elif command == "/historico":
            await self.show_shelf(uid, chat, history=True)
        elif command == "/filtros":
            await self.show_settings(uid, chat)
        elif command in {"/limite", "/limites"}:
            await self.show_quota(uid, chat)
        elif command == "/testarfonte":
            await self.check_source(uid, chat)
        elif command == "/status":
            await self.show_status(uid, chat)
        elif command == "/privacidade":
            await self.tg.message(chat, "<b>Privacidade e funcionamento</b>\n\n"
                                  "Este bot guarda seu ID, filtros, favoritos e histórico local de envios por até 30 dias. "
                                  "Favoritos e filtros persistem até serem removidos pelo administrador. Buscas ficam em sessões por até uma hora e em cache por 15 minutos.\n\n"
                                  "O Telegram processa as mensagens; a fonte recebe as consultas e solicitações de arquivos usando a conta configurada no servidor. "
                                  "Arquivos temporários são apagados ao final de cada pedido. O histórico da conta na fonte não é apagado pelo bot.\n\n"
                                  "Não envie senhas, tokens ou sessões pelo chat. Solicite ao administrador a remoção dos dados locais quando necessário. "
                                  "Use somente conteúdos e acessos para os quais tenha autorização.")
        else:
            await self.tg.message(chat, "<b>Como usar</b>\n\n"
                                  "Digite um título, autor ou ISBN. Abra uma edição, salve nos favoritos ou solicite o arquivo.\n\n"
                                  "Use os botões Filtros para idioma e formato, Minha biblioteca para favoritos e Meus limites para consultar os downloads disponíveis.\n\n"
                                  "A busca depende da fonte externa e não garante disponibilidade de todo título. O bot não remove DRM, não contorna CAPTCHA e não aumenta cotas.", views.home_keyboard())

    async def handle(self, update: dict):
        callback = update.get("callback_query")
        message = callback.get("message", {}) if callback else update.get("message", {})
        sender = callback.get("from", {}) if callback else message.get("from", {})
        uid = sender.get("id")
        chat_data = message.get("chat", {})
        chat = chat_data.get("id")
        if not isinstance(uid, int) or not isinstance(chat, int) or sender.get("is_bot"):
            return
        if chat_data.get("type") != "private":
            if callback:
                await self.tg.answer(callback["id"], "Use o bot na conversa privada.")
            return
        text = str(message.get("text", ""))
        if not callback and text.split(" ")[0].split("@")[0].lower() == "/id":
            await self.tg.message(chat, f"Seu ID no Telegram: <code>{uid}</code>")
            return
        if not self.settings.allows(uid):
            if callback:
                await self.tg.answer(callback["id"], "Este bot é privado.")
            await self.tg.message(chat, f"Este bot é privado. Peça autorização ao administrador.\nSeu ID: <code>{uid}</code>")
            return
        now = time.monotonic()
        if now - self.last_action.get(uid, 0) < (0.2 if callback else 0.6):
            if callback:
                await self.tg.answer(callback["id"], "Aguarde um instante entre os cliques.")
                return
            return
        if callback:
            await self.tg.answer(callback["id"])
        self.last_action[uid] = now
        if len(self.last_action) > 5000:
            self.last_action = {key: value for key, value in self.last_action.items() if value > now - 60}
        lock = self.users.setdefault(uid, asyncio.Lock())
        async with lock:
            try:
                await self.store.user(uid)
                if callback:
                    await self._callback(uid, chat, str(callback.get("data", "")), message)
                elif re.search(r"\d{5,20}:[A-Za-z0-9_-]{30,}", text):
                    await self.tg.message(chat, "Não envie tokens pelo chat. Configure o arquivo .env no servidor. Um token exposto deve ser revogado no BotFather.")
                elif text.startswith("/"):
                    await self._command(uid, chat, text)
                elif text.strip():
                    await self.new_search(uid, chat, text)
                else:
                    await self.tg.message(chat, "Envie o título, autor ou ISBN em texto para pesquisar.")
            except UserError as exc:
                await self.tg.message(chat, esc(self.friendly_error(uid, exc), 1000), views.home_keyboard(uid in self.settings.admin_ids))
            except TelegramError as exc:
                log.warning("Falha Telegram no atendimento: code=%s", exc.code)
            except Exception as exc:
                log.error("Falha no atendimento: %s", type(exc).__name__)
                try:
                    await self.tg.message(chat, "Ocorreu uma falha interna. Toque em Início para tentar novamente.")
                except TelegramError:
                    pass

    async def housekeeping(self):
        counter = 0
        while True:
            try:
                worker_alive = self.downloads.worker is not None and not self.downloads.worker.done()
                data = {"updated": time.time(), "last_poll": self.last_poll, "worker_alive": worker_alive}
                path = self.settings.data_dir / "health.json"
                await asyncio.to_thread(path.write_text, json.dumps(data), encoding="utf-8")
                if counter % 60 == 0:
                    await self.store.prune()
                counter += 1
            except Exception as exc:
                log.error("Falha de manutenção: %s", type(exc).__name__)
            await asyncio.sleep(15)

    def _finished(self, task: asyncio.Task):
        self.pending.discard(task)
        if not task.cancelled():
            error = task.exception()
            if error:
                log.error("Atualização falhou: %s", type(error).__name__)

    async def run(self):
        maintenance = None
        health_server = None
        try:
            await self.initialize()
            from .health import start_server
            health_server = await start_server(self)
            self.probe_task = asyncio.create_task(self.probe_source(), name="source-probe")
            log.info("Bot iniciado; acesso=%s; versão=%s", "público" if self.settings.public_access else "privado", __version__)
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, self.stop_event.set)
                except (NotImplementedError, RuntimeError):
                    pass
            maintenance = asyncio.create_task(self.housekeeping())
            offset = None
            backoff = 1
            while not self.stop_event.is_set():
                try:
                    values = {"timeout": 30, "limit": 20, "allowed_updates": ["message", "callback_query"]}
                    if offset is not None:
                        values["offset"] = offset
                    updates = await self.tg.call("getUpdates", values)
                    self.last_poll = time.time()
                    backoff = 1
                    for update in updates or []:
                        offset = update["update_id"] + 1
                        while len(self.pending) >= 16:
                            await asyncio.wait(self.pending, return_when=asyncio.FIRST_COMPLETED)
                        task = asyncio.create_task(self.handle(update))
                        self.pending.add(task)
                        task.add_done_callback(self._finished)
                except TelegramError as exc:
                    if exc.code in {401, 409}:
                        raise UserError("Token inválido ou outro processo está usando este bot. Interrompa a execução duplicada.", "telegram_conflict") from exc
                    delay = min(60, max(backoff, exc.retry_after))
                    log.warning("Polling indisponível; code=%s; nova tentativa em %ss", exc.code, delay)
                    try:
                        await asyncio.wait_for(self.stop_event.wait(), timeout=delay)
                    except asyncio.TimeoutError:
                        pass
                    backoff = min(60, backoff * 2)
        finally:
            for task in self.pending:
                task.cancel()
            if self.pending:
                await asyncio.gather(*self.pending, return_exceptions=True)
            if maintenance:
                maintenance.cancel()
                await asyncio.gather(maintenance, return_exceptions=True)
            if self.probe_task:
                self.probe_task.cancel()
                await asyncio.gather(self.probe_task, return_exceptions=True)
            await self.downloads.stop()
            if health_server is not None:
                await health_server.cleanup()
            (self.settings.data_dir / "health.json").unlink(missing_ok=True)
