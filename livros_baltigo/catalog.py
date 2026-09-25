"""Conservative edition grouping. Never guesses translated titles or missing authors."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .models import Book

LANGUAGES = {
    "portuguese": "Português", "english": "Inglês", "spanish": "Espanhol",
    "french": "Francês", "italian": "Italiano", "german": "Alemão",
    "japanese": "Japonês", "chinese": "Chinês", "russian": "Russo",
    "any": "Todos os idiomas",
}
FORMATS = {"any": "EPUB e PDF", "epub": "EPUB", "pdf": "PDF"}


def normalized(text: str) -> str:
    value = unicodedata.normalize("NFKD", text.casefold())
    value = "".join(c for c in value if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^\w]+", " ", value).split())


def work_key(book: Book) -> str:
    title = re.sub(r"\s*(?:\[(?:epub|pdf)\]|\((?:epub|pdf)\))\s*$", "", book.title, flags=re.I)
    author = normalized(book.author)
    if not author or author in {"autor nao informado", "unknown", "unknown author", "anonimo"} or not normalized(title):
        return f"edition:{book.key}"
    # Keep volume numbers, subtitles and edition wording; no fuzzy title-only merge.
    return normalized(title) + "|" + author


@dataclass(frozen=True)
class BookGroup:
    books: tuple[Book, ...]

    @property
    def title(self):
        return self.books[0].title

    @property
    def author(self):
        return self.books[0].author


def group_books(books: list[Book]) -> list[BookGroup]:
    groups: dict[str, list[Book]] = {}
    seen: set[str] = set()
    for book in books:
        if book.key in seen:
            continue
        seen.add(book.key)
        groups.setdefault(work_key(book), []).append(book)
    return [BookGroup(tuple(values)) for values in groups.values()]
