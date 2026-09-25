from __future__ import annotations

from .models import Book, SearchPage, esc, clip

LANGUAGES = {"portuguese": "Português", "english": "Inglês", "spanish": "Espanhol", "any": "Todos os idiomas"}
FORMATS = {"any": "EPUB e PDF", "epub": "EPUB", "pdf": "PDF"}


def button(text: str, callback: str) -> dict:
    if len(callback.encode()) > 64:
        raise ValueError("Callback exceeds Telegram's 64-byte limit")
    return {"text": clip(text, 80), "callback_data": callback}


def home_keyboard():
    return [
        [button("🔎 Buscar livro", "menu:search")],
        [button("❤️ Minha biblioteca", "favpage:1"), button("🕘 Histórico", "histpage:1")],
        [button("⚙️ Filtros", "menu:settings"), button("📊 Meus limites", "menu:quota")],
    ]


def book_keyboard(book: Book, favorite: bool):
    rows = []
    if book.extension in {"pdf", "epub"}:
        rows.append([button(f"📥 Receber {book.extension.upper()}", f"download:{book.key}")])
    rows.append([button("❤️ Remover dos favoritos" if favorite else "🤍 Salvar nos favoritos", f"favorite:{book.key}:{0 if favorite else 1}")])
    rows.append([button("❤️ Minha biblioteca", "favpage:1"), button("🔎 Nova busca", "menu:search")])
    return rows


def book_card(book: Book, detailed: bool = True) -> str:
    # Limites calculados antes de escapar HTML; a legenda fica abaixo de 1.024 UTF-16.
    bits = [f"<b>{esc(book.title, 160)}</b>", esc(book.author, 100), ""]
    metadata = [book.extension.upper() or "Formato não informado", book.language or "Idioma não informado"]
    if book.year:
        metadata.append(book.year)
    if book.size is not None:
        metadata.append(f"{book.size / 1_000_000:.1f} MB")
    bits.append(esc(" · ".join(metadata), 120))
    if book.publisher:
        bits.append(f"Editora: {esc(book.publisher, 70)}")
    if detailed and book.description:
        bits.extend(["", esc(book.description, 360)])
    return "\n".join(bits)


def results(page: SearchPage, title: str, prefix: str, spec_label: str = "") -> tuple[str, list]:
    lines = [f"<b>{esc(title, 220)}</b>"]
    if spec_label:
        lines.append(esc(spec_label, 100))
    lines.extend([f"Página {page.page}", ""])
    rows = []
    if not page.books:
        lines.append("Nenhum livro nesta página. Tente outro nome, autor ou idioma nos filtros.")
    for index, book in enumerate(page.books, start=1):
        details = " · ".join(x for x in (book.extension.upper(), book.language, book.year) if x)
        lines.append(f"<b>{index}. {esc(book.title, 110)}</b>\n{esc(book.author, 65)}\n{esc(details, 80)}\n")
        rows.append([button(f"{index}. {book.title}", f"book:{book.key}")])
    navigation = []
    if page.page > 1:
        navigation.append(button("← Anterior", f"{prefix}:{page.page - 1}"))
    if page.has_next and page.page < 1000:
        navigation.append(button("Próxima →", f"{prefix}:{page.page + 1}"))
    if navigation:
        rows.append(navigation)
    rows.append([button("⚙️ Filtros", "menu:settings"), button("🔎 Nova busca", "menu:search")])
    return "\n".join(lines), rows


def settings_keyboard(preferences: dict):
    rows = []
    for value, title in LANGUAGES.items():
        rows.append([button(("✓ " if preferences["language"] == value else "") + title, f"setting:language:{value}")])
    rows.append([button(("✓ " if preferences["extension"] == value else "") + title, f"setting:extension:{value}") for value, title in FORMATS.items()])
    rows.append([button("🔎 Buscar", "menu:search")])
    return rows
