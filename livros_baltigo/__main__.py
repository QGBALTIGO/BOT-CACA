from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import sys
import time

from filelock import FileLock, Timeout

from .app import BotApp
from .config import Settings
from .errors import TelegramError, UserError
from .network import new_session
from .provider import ZLibrary
from .scraper import HTMLSource
from .storage import Store
from .telegram import Telegram
from .runtime import prepare_data


async def execute(settings: Settings, doctor=False):
    async with new_session() as api, new_session() as files, new_session() as telegram_session:
        telegram = Telegram(settings.bot_token, telegram_session)
        mode = os.getenv("BOOK_SOURCE_MODE", "html").lower().strip()
        if mode not in {"html", "api"}:
            raise ValueError("BOOK_SOURCE_MODE deve ser html ou api")
        source = (HTMLSource if mode == "html" else ZLibrary)(settings, api, files)
        if doctor:
            me = await telegram.call("getMe")
            print(f"Telegram: conectado a @{me.get('username', '(sem username)')}")
            webhook = await telegram.call("getWebhookInfo")
            if webhook.get("url"):
                raise UserError("Existe um webhook ativo. Não inicie polling com esse token sem revisar a integração.")
            quota = await source.quota()
            print("Z-Library: resposta de perfil reconhecida.")
            print(f"Cota restante informada: {quota.remaining if quota.remaining is not None else 'não informada'}")
            print("Diagnóstico concluído. Nenhum livro foi solicitado ou baixado.")
            return
        temporary = settings.data_dir / "tmp"
        temporary.mkdir(parents=True, exist_ok=True)
        # Só é executado com o lock exclusivo adquirido, antes de iniciar trabalhos.
        for path in temporary.glob("job-*"):
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
        app = BotApp(settings, telegram, source, Store(settings.data_dir / "library.sqlite3"))
        await app.run()


def main() -> int:
    parser = argparse.ArgumentParser(description="Livros Baltigo — bot Telegram")
    parser.add_argument("--env", default=".env", help="Arquivo de configuração local")
    parser.add_argument("--check", action="store_true", help="Validar apenas a configuração; sem rede")
    parser.add_argument("--doctor", action="store_true", help="Testar Telegram e perfil da fonte; sem baixar livros")
    parser.add_argument("--health", action="store_true", help="Healthcheck do processo em execução")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("aiohttp").setLevel(logging.ERROR)
    # Arquivos de dados e sessão locais não ficam legíveis para outros usuários no Unix.
    os.umask(0o077)
    try:
        settings = Settings.load(args.env)
        if args.check:
            print("Configuração válida. Conexões e credenciais ainda não foram testadas.")
            return 0
        if args.health:
            try:
                data = json.loads((settings.data_dir / "health.json").read_text())
                healthy = (time.time() - data["updated"] < 60 and time.time() - data["last_poll"] < 150 and data["worker_alive"])
                return 0 if healthy else 1
            except (OSError, ValueError, KeyError):
                return 1
        prepare_data(settings.data_dir)
        with FileLock(str(settings.data_dir / "bot.lock"), timeout=0):
            asyncio.run(execute(settings, args.doctor))
        return 0
    except Timeout:
        print("Já há outro processo usando esta pasta de dados.", file=sys.stderr)
    except ValueError as exc:
        print(f"Configuração: {exc}", file=sys.stderr)
    except UserError as exc:
        print(exc.message, file=sys.stderr)
    except TelegramError as exc:
        print(f"Falha de conexão ao Telegram (código {exc.code}). Verifique o token e a rede.", file=sys.stderr)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        # Não imprimir repr/tracebacks com URLs, tokens ou respostas do provedor.
        print(f"Falha de inicialização: {type(exc).__name__}. Revise a configuração e os serviços.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
