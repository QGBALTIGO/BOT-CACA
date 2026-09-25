from __future__ import annotations

import hashlib
import html
import re
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from typing import Any


class _TextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.skip += 1
        elif tag in {"p", "br", "div", "li"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.skip = max(0, self.skip - 1)
        elif tag in {"p", "div", "li"}:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def plain(value: Any, limit: int = 3000) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = ", ".join(str(x) for x in value)
    if isinstance(value, dict):
        value = value.get("name", "")
    parser = _TextParser()
    parser.feed(str(value)[:20_000])
    return " ".join("".join(parser.parts).split())[:limit]


def clip(text: str, max_units: int) -> str:
    """Trunca por unidades UTF-16, usadas nos limites do Telegram."""
    encoded = text.encode("utf-16-le", errors="replace")
    if len(encoded) <= max_units * 2:
        return text
    return encoded[:max(0, (max_units - 1)) * 2].decode("utf-16-le", errors="ignore") + "…"


def esc(text: str, max_units: int = 3000) -> str:
    return html.escape(clip(text, max_units), quote=False)


def to_int(value: Any) -> int | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def parse_bytes(value: Any) -> int | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return max(0, int(value))
    text = str(value or "").strip().replace(",", ".")
    match = re.fullmatch(r"([\d.]+)\s*(B|KB|MB|GB|KiB|MiB|GiB)?", text, re.I)
    if not match:
        return None
    factors = {"b": 1, "kb": 1000, "mb": 10**6, "gb": 10**9,
               "kib": 1024, "mib": 1024**2, "gib": 1024**3}
    try:
        return int(float(match[1]) * factors[(match[2] or "b").lower()])
    except ValueError:
        return None


@dataclass(frozen=True)
class Book:
    id: str
    hash: str
    title: str
    author: str = "Autor não informado"
    language: str = ""
    extension: str = ""
    year: str = ""
    publisher: str = ""
    description: str = ""
    cover: str = ""
    size: int | None = None

    @property
    def key(self) -> str:
        return hashlib.blake2s(f"{self.id}:{self.hash}".encode(), digest_size=8).hexdigest()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_source(cls, row: dict) -> Book:
        ident = str(row.get("id", ""))
        book_hash = str(row.get("hash", ""))
        if not re.fullmatch(r"\d{1,20}", ident) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", book_hash):
            raise ValueError("Identificador de livro inválido")
        cover = row.get("cover", "")
        if isinstance(cover, dict):
            cover = cover.get("url", "")
        return cls(
            id=ident, hash=book_hash, title=plain(row.get("title"), 500) or "Sem título",
            author=plain(row.get("author") or row.get("authors"), 500) or "Autor não informado",
            language=plain(row.get("language"), 50),
            extension=str(row.get("extension") or "").lower().strip(". ")[:12],
            year=plain(row.get("year"), 12), publisher=plain(row.get("publisher"), 200),
            description=plain(row.get("description") or row.get("annotation")),
            cover=str(cover or "")[:4096],
            size=parse_bytes(row.get("filesize") or row.get("file_size") or row.get("size")),
        )


@dataclass(frozen=True)
class SearchSpec:
    query: str
    language: str = "portuguese"
    extension: str = "any"

    def cache_key(self, page: int, limit: int) -> str:
        text = f"{self.query.casefold()}|{self.language}|{self.extension}|{page}|{limit}"
        return hashlib.sha256(text.encode()).hexdigest()


@dataclass(frozen=True)
class SearchPage:
    books: list[Book]
    page: int
    has_next: bool
    total: int | None = None


@dataclass(frozen=True)
class Quota:
    limit: int | None
    used: int | None

    @property
    def remaining(self) -> int | None:
        if self.limit is None or self.used is None:
            return None
        return max(0, self.limit - self.used)
