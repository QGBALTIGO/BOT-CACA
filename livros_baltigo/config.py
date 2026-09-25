from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


def ids(value: str) -> frozenset[int]:
    try:
        result = frozenset(int(x.strip()) for x in value.split(",") if x.strip())
    except ValueError as exc:
        raise ValueError("IDs de usuários precisam ser números separados por vírgula.") from exc
    if any(x <= 0 for x in result):
        raise ValueError("IDs de usuários devem ser positivos.")
    return result


@dataclass(frozen=True)
class Settings:
    bot_token: str = field(repr=False)
    admin_ids: frozenset[int]
    base_url: str
    user_id: str = field(default="", repr=False)
    user_key: str = field(default="", repr=False)
    email: str = field(default="", repr=False)
    password: str = field(default="", repr=False)
    allowed_ids: frozenset[int] = frozenset()
    public_access: bool = False
    brand: str = "Livros Baltigo"
    data_dir: Path = Path("data")
    timezone: str = "America/Campo_Grande"
    daily_limit: int = 5
    max_file_bytes: int = 49_000_000
    queue_size: int = 20
    page_size: int = 8
    file_hosts: frozenset[str] = frozenset()
    search_ttl: int = 900
    session_ttl: int = 3600
    setup_mode: bool = False

    @property
    def source_configured(self) -> bool:
        return bool(self.base_url and ((self.user_id and self.user_key) or (self.email and self.password)))

    def allows(self, user_id: int) -> bool:
        return self.public_access or user_id in self.admin_ids or user_id in self.allowed_ids

    def validate(self) -> None:
        if not re.fullmatch(r"\d{5,20}:[A-Za-z0-9_-]{30,}", self.bot_token):
            raise ValueError("Preencha BOT_TOKEN com o token de um bot novo do BotFather.")
        if not self.admin_ids:
            raise ValueError("Preencha ADMIN_IDS. O bot não inicia sem administrador.")
        source_values = (self.base_url, self.user_id, self.user_key, self.email, self.password)
        if not (self.setup_mode and not any(source_values)):
            parts = urlsplit(self.base_url)
            if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
                    or parts.path not in {"", "/"} or parts.query or parts.fragment
                    or parts.port not in {None, 443}):
                raise ValueError("ZLIB_BASE_URL precisa ser a origem HTTPS verificada da sua conta, sem caminho.")
            if not ((self.user_id and self.user_key) or (self.email and self.password)):
                raise ValueError("Configure ZLIB_USER_ID/ZLIB_USER_KEY ou ZLIB_EMAIL/ZLIB_PASSWORD.")
        if self.user_id and not self.user_id.isdigit():
            raise ValueError("ZLIB_USER_ID deve conter somente números.")
        if self.user_key and (len(self.user_key) > 1024 or any(c in self.user_key for c in "\r\n;")):
            raise ValueError("ZLIB_USER_KEY inválido.")
        if not 1 <= self.daily_limit <= 1000:
            raise ValueError("DAILY_LIMIT_PER_USER deve ficar entre 1 e 1000.")
        if not 100_000 <= self.max_file_bytes <= 50_000_000:
            raise ValueError("MAX_FILE_BYTES deve ficar entre 100000 e 50000000 nesta versão.")
        if not 1 <= self.queue_size <= 100:
            raise ValueError("QUEUE_SIZE deve ficar entre 1 e 100.")
        if not 1 <= self.page_size <= 10:
            raise ValueError("PAGE_SIZE deve ficar entre 1 e 10.")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("TIMEZONE inválido. Instale tzdata no Windows.") from exc
        for host in self.file_hosts:
            if not re.fullmatch(r"[a-z0-9.-]+", host) or "*" in host or "/" in host:
                raise ValueError("ZLIB_FILE_HOSTS aceita nomes exatos de host, sem URL ou curingas.")

    @classmethod
    def load(cls, env_file: str | Path = ".env") -> Settings:
        load_dotenv(env_file, override=False)
        get = lambda name, default="": os.getenv(name, default).strip()
        public = get("PUBLIC_ACCESS", "false").lower()
        if public not in {"true", "false"}:
            raise ValueError("PUBLIC_ACCESS deve ser true ou false.")
        setup = get("SETUP_MODE", "false").lower()
        if setup not in {"true", "false"}:
            raise ValueError("SETUP_MODE deve ser true ou false.")
        try:
            value = cls(
                setup_mode=setup == "true",
                bot_token=get("BOT_TOKEN"), admin_ids=ids(get("ADMIN_IDS")),
                base_url=get("ZLIB_BASE_URL").rstrip("/"),
                user_id=get("ZLIB_USER_ID"), user_key=get("ZLIB_USER_KEY"),
                email=get("ZLIB_EMAIL"), password=os.getenv("ZLIB_PASSWORD", ""),
                allowed_ids=ids(get("ALLOWED_USER_IDS")), public_access=public == "true",
                brand=get("BOT_NAME", "Livros Baltigo")[:80] or "Livros Baltigo",
                data_dir=Path(get("DATA_DIR", "data")),
                timezone=get("TIMEZONE", "America/Campo_Grande"),
                daily_limit=int(get("DAILY_LIMIT_PER_USER", "5")),
                max_file_bytes=int(get("MAX_FILE_BYTES", "49000000")),
                queue_size=int(get("QUEUE_SIZE", "20")), page_size=int(get("PAGE_SIZE", "8")),
                file_hosts=frozenset(x.strip().lower() for x in get("ZLIB_FILE_HOSTS").split(",") if x.strip()),
            )
        except ValueError as exc:
            raise ValueError("Revise os IDs e valores numéricos do .env.") from exc
        value.validate()
        return value
