"""Health HTTP para Railway. Não publica credenciais ou dados de usuários."""
from __future__ import annotations

import os
import time

from aiohttp import web

from . import __version__


def health_payload(app) -> dict:
    worker = app.downloads.worker
    poll_ok = app.last_poll > 0 and time.time() - app.last_poll < 150
    healthy = bool(worker and not worker.done() and poll_ok and not app.stop_event.is_set())
    return {
        "status": "ok" if healthy else "starting_or_unhealthy",
        "version": __version__,
        "catalog_state": getattr(app, "catalog_state", "not_verified"),
        "telegram_polling": poll_ok,
        "worker_alive": bool(worker and not worker.done()),
        "source": "configured_not_verified" if app.settings.source_configured else "awaiting_credentials",
    }


def create_app(bot) -> web.Application:
    app = web.Application()

    async def health(request):
        result = health_payload(bot)
        ready = result["status"] == "ok"
        if request.path == "/ready":
            ready = ready and bot.settings.source_configured and getattr(bot, "catalog_state", "not_verified") == "ready"
        return web.json_response(result, status=200 if ready else 503,
                                 headers={"Cache-Control": "no-store"})

    app.router.add_get("/health", health)
    app.router.add_get("/ready", health)
    return app


async def start_server(bot):
    raw = os.getenv("PORT", "")
    if not raw:
        return None
    port = int(raw)
    if not 1 <= port <= 65535:
        raise ValueError("PORT inválida.")
    runner = web.AppRunner(create_app(bot), access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, "0.0.0.0", port).start()
    except BaseException:
        await runner.cleanup()
        raise
    return runner
