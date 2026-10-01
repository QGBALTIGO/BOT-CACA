from __future__ import annotations

import asyncio
import hashlib
import logging
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .errors import TelegramError, UserError
from .models import Book, esc
from .receipts import document_receipt
from .catalogs import CatalogRouter
from .catalog import SOURCES
from .provider import filename_for
from . import views

log = logging.getLogger(__name__)


@dataclass
class Job:
    id: int
    user_id: int
    chat: int
    message: int
    book: Book


class Downloads:
    def __init__(self, settings, store, source, telegram):
        self.settings, self.store, self.source, self.tg = settings, store, source, telegram
        self.queue: asyncio.Queue[Job] = asyncio.Queue(settings.queue_size)
        self.active_users: set[int] = set()
        self.submit_lock = asyncio.Lock()
        self.worker: asyncio.Task | None = None
        self.accepting = False

    def today(self) -> str:
        return datetime.now(ZoneInfo(self.settings.timezone)).date().isoformat()

    def start(self):
        self.accepting = True
        self.worker = asyncio.create_task(self._work(), name="download-worker")

    async def stop(self):
        self.accepting = False
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        while not self.queue.empty():
            job = self.queue.get_nowait()
            await self.store.job_status(job.id, "interrupted")
            self.active_users.discard(job.user_id)
            self.queue.task_done()

    async def enqueue(self, uid: int, chat: int, message: int, book: Book) -> tuple[int, int]:
        async with self.submit_lock:
            if not self.accepting:
                raise UserError("A fila não está disponível neste momento.", "queue_unavailable")
            if uid in self.active_users:
                raise UserError("Você já tem um livro na fila ou em envio. Aguarde a conclusão desse pedido.", "duplicate_job")
            if self.queue.full():
                raise UserError("A fila está cheia. Tente novamente mais tarde.", "queue_full")
            if book.extension not in {"pdf", "epub"}:
                raise UserError("Escolha uma edição EPUB ou PDF.", "unsupported_format")
            if book.size is not None and book.size > self.settings.max_file_bytes:
                raise UserError("Esta edição é maior que o limite de envio do bot. Escolha uma edição menor.", "file_too_large")
            ident = await self.store.reserve_job(uid, book.key, self.today(), self.settings.daily_limit)
            position = len(self.active_users) + 1
            try:
                await self.tg.edit(chat, message, f"📚 Pedido #{ident} na fila\nPosição inicial: {position}\n\nEsta mensagem será atualizada durante o download e o envio.")
            except TelegramError:
                pass
            self.active_users.add(uid)
            self.queue.put_nowait(Job(ident, uid, chat, message, book))
            return ident, position

    async def status(self, job: Job, text: str):
        try:
            await self.tg.edit(job.chat, job.message, text)
        except TelegramError:
            pass

    async def _process(self, job: Job):
        delivered = False
        sending = False
        try:
            await self.store.job_status(job.id, "running")
            provider = self.source.for_book(job.book) if isinstance(self.source, CatalogRouter) else self.source
            if getattr(provider, "requires_quota", True) is not False:
                await self.status(job, "📊 Verificando o limite da conta conectada…")
                quota = await provider.quota()
                if quota.remaining is None or quota.limit < 0 or quota.used < 0:
                    raise UserError("A fonte não informou uma cota válida. O pedido foi pausado para evitar consumo desconhecido.", "quota_unknown")
                if quota.remaining <= 0:
                    raise UserError("O limite da conta na fonte foi atingido. Consulte /limite.", "quota")
            await self.status(job, "📥 Solicitando a edição à fonte selecionada…")
            url, extension = await provider.file_info(job.book)
            self.settings.data_dir.joinpath("tmp").mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="job-", dir=self.settings.data_dir / "tmp") as folder:
                path = Path(folder) / f"livro.{extension}"
                last_edit = 0.0
                async def progress(received: int, total: int | None):
                    nonlocal last_edit
                    now = time.monotonic()
                    if now - last_edit < 3:
                        return
                    last_edit = now
                    await self.status(job, views.progress_text(job.book.title, received, total))
                await provider.download(url, extension, path, progress)
                await self.status(job, "📤 Enviando o arquivo ao Telegram…")
                def file_digest():
                    with path.open("rb") as handle:
                        return hashlib.file_digest(handle, "sha256").hexdigest()
                digest = await asyncio.to_thread(file_digest)
                await self.store.job_status(job.id, "sending")
                sending = True
                result = await self.tg.document(job.chat, path, filename_for(job.book, extension),
                                       f"📖 <b>{esc(job.book.title, 160)}</b>\n{esc(job.book.author, 100)}\n\n{extension.upper()} · {esc(views.language_name(job.book.language), 50)}\nFonte: {esc(SOURCES.get(job.book.source, job.book.source), 60)}"
                                       + ("\nPDF diagramado a partir do texto integral; não é fac-símile." if job.book.source == "gutenberg" and extension == "pdf" else "")
                                       + f"\n🔖 {esc(self.settings.brand, 80)} · Boa leitura!")
                receipt = document_receipt(result, job.chat)
                delivered = True
                await self.store.confirm_delivery(job.id, receipt, digest)
                log.info("Entrega confirmada: job_id=%s; message_id=%s; bytes=%s", job.id, receipt.message_id, path.stat().st_size)
                await self.status(job, f"✅ Livro enviado. Boa leitura!\nComprovante Telegram: mensagem {receipt.message_id}.\nO livro já aparece no Histórico.")
        except asyncio.CancelledError:
            await self.store.job_status(job.id, "done" if delivered else ("uncertain" if sending else "interrupted"))
            raise
        except UserError as exc:
            await self.store.job_status(job.id, "failed")
            message = exc.message if job.user_id in self.settings.admin_ids else views.PUBLIC_ERRORS.get(exc.code, exc.message)
            await self.status(job, f"Não foi possível concluir.\n\n{esc(message, 800)}")
        except TelegramError as exc:
            uncertain = sending and (exc.code == 0 or exc.code >= 500)
            await self.store.job_status(job.id, "uncertain" if uncertain else "failed")
            if uncertain:
                text = "A confirmação do Telegram não chegou. Confira se o arquivo apareceu na conversa antes de solicitar novamente. Não repetirei o envio automaticamente."
            else:
                text = "O Telegram não aceitou o envio. A cota da fonte pode ter sido consumida. Consulte /limite."
            await self.status(job, text)
            log.warning("Entrega falhou: Telegram code=%s", exc.code)
        except Exception as exc:
            # A crash after beginning the write must not invite an automatic duplicate.
            await self.store.job_status(job.id, "done" if delivered else ("uncertain" if sending else "failed"))
            text = ("O envio ocorreu, mas houve uma falha ao salvar seu comprovante. Confira a conversa."
                    if delivered else "O pedido encontrou uma falha interna. Confira a conversa antes de pedir novamente.")
            await self.status(job, text)
            log.error("Falha no worker: %s", type(exc).__name__)

    async def _work(self):
        while True:
            job = await self.queue.get()
            try:
                await self._process(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Uma falha de banco não deve encerrar silenciosamente o worker.
                log.error("Falha de infraestrutura do worker: %s", type(exc).__name__)
            finally:
                self.active_users.discard(job.user_id)
                self.queue.task_done()
