"""Atendimento provisório: menus reais, catálogo explicitamente indisponível.

Não autentica na fonte, não pesquisa e não baixa livros.
"""
from __future__ import annotations
import asyncio
import logging
import os
import re
import signal
import sqlite3
import time
from pathlib import Path
from aiohttp import web
from filelock import FileLock
from .errors import TelegramError
from .network import new_session
from .runtime import prepare_data
from .telegram import Telegram

log = logging.getLogger(__name__)
LANGUAGES = {"any": "Todos os idiomas", "portuguese": "Português", "english": "Inglês", "spanish": "Espanhol", "french": "Francês", "italian": "Italiano", "german": "Alemão", "japanese": "Japonês", "chinese": "Chinês", "russian": "Russo"}
FORMATS = {"any": "EPUB e PDF", "epub": "EPUB", "pdf": "PDF"}
NOTICE = "🛠 <b>Catálogo em preparação</b>\nA integração de livros ainda não está instalada. Buscas e downloads estão indisponíveis; nenhum resultado será simulado."

def button(text, action):
    return {"text": text, "callback_data": action}

def home(language="portuguese", extension="any"):
    text = ("📚 <b>Livros Baltigo</b>\n<i>Sua próxima leitura começa aqui.</i>\n\n" + NOTICE + "\n\nVocê já pode ajustar suas preferências.\n" + f"🌐 {LANGUAGES.get(language, LANGUAGES['any'])} · {FORMATS.get(extension, FORMATS['any'])}")
    return text, [[button("🌐 Idioma", "languages"), button("📄 Formato", "formats")], [button("📖 Buscar livro", "search")], [button("ℹ️ Como funciona", "help"), button("🩺 Situação", "status")]]

class Preferences:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS preferences (uid INTEGER PRIMARY KEY, language TEXT NOT NULL, extension TEXT NOT NULL)")
        self.db.commit()
    def get(self, uid):
        row = self.db.execute("SELECT language,extension FROM preferences WHERE uid=?", (uid,)).fetchone()
        return row or ("portuguese", "any")
    def set(self, uid, field, value):
        allowed = LANGUAGES if field == "language" else FORMATS if field == "extension" else {}
        if value not in allowed:
            raise ValueError("Preferência inválida")
        language, extension = self.get(uid)
        if field == "language": language = value
        else: extension = value
        self.db.execute("INSERT INTO preferences VALUES(?,?,?) ON CONFLICT(uid) DO UPDATE SET language=excluded.language, extension=excluded.extension", (uid, language, extension))
        self.db.commit()
    def close(self): self.db.close()

class Reception:
    def __init__(self, tg, prefs, admins):
        self.tg, self.prefs, self.admins = tg, prefs, admins
        self.last_poll = 0.0
        self.stop = asyncio.Event()
    def status(self):
        healthy = self.last_poll > 0 and time.monotonic() - self.last_poll < 100 and not self.stop.is_set()
        return {"status": "ok" if healthy else "starting_or_unhealthy", "mode": "reception_only", "telegram_polling": healthy, "catalog_available": False}
    async def handle(self, update):
        query = update.get("callback_query")
        message = query.get("message", {}) if query else update.get("message", {})
        user = query.get("from", {}) if query else message.get("from", {})
        chat = message.get("chat", {})
        uid = user.get("id")
        if chat.get("type") != "private" or not isinstance(uid, int) or chat.get("id") != uid:
            if query: await self.tg.answer(query["id"], "Abra o bot na conversa privada.")
            return
        action = query.get("data", "") if query else str(message.get("text", "/start")).split(" ", 1)[0]
        if query: await self.tg.answer(query["id"])
        if action.startswith("lang:"):
            value = action[5:]
            if value not in LANGUAGES: return
            self.prefs.set(uid, "language", value)
            action = "languages"
        elif action.startswith("fmt:"):
            value = action[4:]
            if value not in FORMATS: return
            self.prefs.set(uid, "extension", value)
            action = "formats"
        language, extension = self.prefs.get(uid)
        back = [[button("↩️ Voltar à biblioteca", "home")]]
        if action in {"home", "/start", "/menu"}:
            text, keyboard = home(language, extension)
        elif action in {"languages", "/filtros"}:
            text = "🌐 <b>Idioma da sua leitura</b>\nEscolha o idioma preferido. O catálogo ainda está em preparação."
            choices = [button(("✓ " if key == language else "") + label, "lang:" + key) for key, label in LANGUAGES.items()]
            keyboard = [choices[i:i+2] for i in range(0, len(choices), 2)] + back
        elif action == "formats":
            text = "📄 <b>Formato preferido</b>\nEPUB permite ajustar a leitura; PDF preserva a diagramação."
            keyboard = [[button(("✓ " if key == extension else "") + label, "fmt:" + key)] for key, label in FORMATS.items()] + back
        elif action in {"help", "/ajuda"}:
            text = "📚 <b>Uma biblioteca, sem complicação</b>\n\nO atendimento e as preferências já funcionam. A busca de livros e a entrega de arquivos dependem da conclusão da instalação do catálogo.\n\nNão envie senhas ou tokens nesta conversa."
            keyboard = back
        elif action in {"status", "/status"}:
            text = "🩺 <b>Situação do Livros Baltigo</b>\n\n✅ Atendimento no Telegram\n✅ Preferências de idioma e formato\n⏳ Busca e entrega de livros indisponíveis"
            if uid in self.admins:
                text += "\n\n<b>Administração</b>\nA publicação do módulo do catálogo está pendente. Este processo não valida o login da fonte nem consome downloads."
            keyboard = back
        elif action == "/id":
            text, keyboard = f"🔖 Seu ID: <code>{uid}</code>", back
        else:
            text, keyboard = "📖 <b>Sua próxima leitura</b>\n\n" + NOTICE + "\n\nSua mensagem não foi enviada à fonte e não consumiu downloads.", back
        if query:
            try:
                await self.tg.edit(uid, message["message_id"], text, keyboard)
                return
            except TelegramError as exc:
                if exc.code != 400: raise
        await self.tg.message(uid, text, keyboard)
    async def poll(self):
        offset = 0
        while not self.stop.is_set():
            try:
                updates = await self.tg.call("getUpdates", {"offset": offset, "timeout": 20, "limit": 10, "allowed_updates": ["message", "callback_query"]}, timeout=35)
                self.last_poll = time.monotonic()
                for update in updates:
                    try: await self.handle(update)
                    except TelegramError as exc: log.warning("Resposta não confirmada: code=%s", exc.code)
                    offset = max(offset, update["update_id"] + 1)
            except TelegramError as exc:
                if exc.code in {401, 409}: raise
                log.warning("Consulta Telegram: code=%s", exc.code)
                try: await asyncio.wait_for(self.stop.wait(), timeout=max(3, min(exc.retry_after or 3, 60)))
                except asyncio.TimeoutError: pass

def http_app(bot):
    app = web.Application()
    async def health(request):
        data = bot.status()
        ok = data["status"] == "ok" and request.path != "/ready"
        return web.json_response(data, status=200 if ok else 503, headers={"Cache-Control": "no-store"})
    app.router.add_get("/health", health)
    app.router.add_get("/ready", health)
    return app

async def run(token, admins, directory):
    async with new_session() as session:
        tg = Telegram(token, session)
        me = await tg.call("getMe")
        info = await tg.call("getWebhookInfo")
        if info.get("url"):
            raise RuntimeError("Webhook existente: migração não realizada automaticamente")
        prefs = Preferences(directory / "reception.sqlite3")
        bot = Reception(tg, prefs, admins)
        runner = web.AppRunner(http_app(bot), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8080"))).start()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try: asyncio.get_running_loop().add_signal_handler(sig, bot.stop.set)
            except NotImplementedError: pass
        try:
            commands = [{"command": "start", "description": "Abrir a biblioteca"}, {"command": "filtros", "description": "Escolher idioma"}, {"command": "status", "description": "Ver situação do serviço"}, {"command": "ajuda", "description": "Como funciona"}]
            await tg.call("setMyCommands", {"commands": commands})
            await tg.call("setMyShortDescription", {"short_description": "📚 Livros Baltigo — atendimento ativo. Catálogo em preparação."})
            log.info("Telegram validado: @%s; webhook ausente; comandos PT configurados; catalog_available=false", me.get("username"))
            for admin in sorted(admins)[:1]:
                marker = directory / "reception-notified"
                if not marker.exists():
                    try:
                        text, keyboard = home()
                        await tg.message(admin, "✅ <b>Atendimento iniciado no Railway</b>\n\n" + text, keyboard)
                        marker.write_text("sent", encoding="utf-8")
                        log.info("Mensagem de teste entregue ao administrador")
                    except TelegramError as exc:
                        log.info("Mensagem de teste não entregue: code=%s; administrador pode abrir /start", exc.code)
            await bot.poll()
        finally:
            bot.stop.set()
            await runner.cleanup()
            prefs.close()

def main():
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("aiohttp").setLevel(logging.ERROR)
    token = os.getenv("BOT_TOKEN", "").strip()
    if not re.fullmatch(r"\d{5,20}:[A-Za-z0-9_-]{30,}", token):
        raise SystemExit("BOT_TOKEN inválido; revise a variável privada")
    try:
        admins = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
        if not admins or min(admins) <= 0: raise ValueError
        directory = Path(os.getenv("DATA_DIR", "data"))
        prepare_data(directory)
        with FileLock(str(directory / "bot.lock"), timeout=0):
            asyncio.run(run(token, admins, directory))
    except Exception as exc:
        log.error("Inicialização interrompida: %s", type(exc).__name__)
        raise SystemExit(1)

if __name__ == "__main__": main()
