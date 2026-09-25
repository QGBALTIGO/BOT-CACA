from __future__ import annotations

from .models import Book, SearchPage, esc, clip
from .catalog import LANGUAGES, FORMATS, BookGroup



def button(text: str, callback: str) -> dict:
    if len(callback.encode()) > 64:
        raise ValueError("Callback exceeds Telegram's 64-byte limit")
    return {"text": clip(text, 80), "callback_data": callback}


def home_keyboard(admin: bool = False):
    rows = [
        [button("🔎 Buscar livro", "menu:search")],
        [button("📚 Minha biblioteca", "favpage:1"), button("🕘 Histórico", "histpage:1")],
        [button("🌐 Idioma e formato", "menu:settings"), button("📊 Meus limites", "menu:quota")],
        [button("Como funciona", "menu:help"), button("Privacidade", "menu:privacy")],
    ]
    if admin:
        rows.append([button("Administração", "admin:status"), button("Testar conexão", "admin:check")])
    return rows


def book_keyboard(book: Book, favorite: bool, back: str = ""):
    rows = []
    if book.extension in {"pdf", "epub"}:
        rows.append([button(f"📥 Receber {book.extension.upper()}", f"download:{book.key}")])
    extra = ":" + back.split(":", 1)[1] if back.startswith("editions:") else ""
    rows.append([button("🔖 Remover dos favoritos" if favorite else "🔖 Salvar nos favoritos", f"favorite:{book.key}:{0 if favorite else 1}{extra}")])
    if back:
        rows.append([button("← Escolher outra edição", back)])
    rows.append([button("📚 Minha biblioteca", "favpage:1"), button("🔎 Nova busca", "menu:search")])
    return rows


def book_card(book: Book, detailed: bool = True) -> str:
    # Limites calculados antes de escapar HTML; a legenda fica abaixo de 1.024 UTF-16.
    bits = [f"📖 <b>{esc(book.title, 160)}</b>", esc(book.author, 100), ""]
    metadata = [book.extension.upper() or "Formato não informado", language_name(book.language)]
    if book.year:
        metadata.append(book.year)
    if book.size is not None:
        metadata.append(f"{book.size / 1_000_000:.1f} MB".replace(".", ","))
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
        details = " · ".join(x for x in (book.extension.upper(), language_name(book.language), book.year) if x)
        lines.append(f"<b>{index}. {esc(book.title, 110)}</b>\n{esc(book.author, 65)}\n{esc(details, 80)}\n")
        rows.append([button(f"{index}. {book.title}", f"book:{book.key}")])
    navigation = []
    if page.page > 1:
        navigation.append(button("← Anterior", f"{prefix}:{page.page - 1}"))
    if page.has_next and page.page < 1000:
        navigation.append(button("Próxima →", f"{prefix}:{page.page + 1}"))
    if navigation:
        rows.append(navigation)
    rows.append([button("🌐 Idioma e formato", "menu:settings"), button("🔎 Nova busca", "menu:search")])
    return "\n".join(lines), rows


def settings_keyboard(preferences: dict, repeat: bool = False):
    rows, row = [], []
    for value, title in LANGUAGES.items():
        row.append(button(("✓ " if preferences["language"] == value else "") + title, f"setting:language:{value}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([button(("✓ " if preferences["extension"] == value else "") + title, f"setting:extension:{value}") for value, title in FORMATS.items()])
    if repeat:
        rows.append([button("🔎 Aplicar à última busca", "menu:repeat")])
    rows.append([button("🔎 Nova busca", "menu:search"), button("📚 Início", "menu:home")])
    return rows


LANGUAGE_NAMES = {
    "portuguese": "Português", "português": "Português", "pt": "Português",
    "pt-br": "Português", "por": "Português", "brazilian portuguese": "Português",
    "english": "Inglês", "en": "Inglês", "eng": "Inglês",
    "spanish": "Espanhol", "es": "Espanhol", "spa": "Espanhol",
    "french": "Francês", "fr": "Francês", "german": "Alemão", "de": "Alemão",
    "italian": "Italiano", "it": "Italiano", "japanese": "Japonês", "ja": "Japonês",
    "chinese": "Chinês", "zh": "Chinês", "russian": "Russo", "ru": "Russo",
}


def language_name(value: str) -> str:
    value = value.strip()
    return LANGUAGE_NAMES.get(value.casefold(), value or "Idioma não informado")


def progress_text(title: str, received: int, total: int | None) -> str:
    received = max(0, received)
    header = f"📥 <b>Preparando sua leitura</b>\n{esc(title, 100)}\n\n"
    if total and total > 0:
        percent = min(100, int(received * 100 / total))
        filled = min(10, percent // 10)
        bar = "▰" * filled + "▱" * (10 - filled)
        amount = f"{received / 1e6:.1f} de {total / 1e6:.1f} MB".replace(".", ",")
        return header + f"{bar} {percent}%\n{amount}"
    amount = f"{received / 1e6:.1f} MB recebidos".replace(".", ",")
    return header + amount + "\nO tamanho total ainda não foi informado."


PUBLIC_ERRORS = {
    "source_dns": "O endereço do catálogo não respondeu no servidor. O administrador precisa verificar a conexão.",
    "layout_changed": "A página do catálogo mudou de formato. O administrador precisa revisar a integração.",
    "html_auth": "A sessão do catálogo precisa ser verificada pelo administrador.",
    "quota_unknown": "Não foi possível confirmar os downloads disponíveis. O pedido foi pausado, sem solicitar o arquivo.",
    "setup_required": "O catálogo ainda está sendo conectado. Sua biblioteca continua disponível; tente buscar novamente mais tarde.",
    "auth": "A conexão com o catálogo precisa ser renovada pelo administrador. Tente novamente mais tarde.",
    "blocked": "O catálogo não está aceitando consultas neste momento. Tente novamente mais tarde.",
    "api_redirect": "O endereço do catálogo precisa ser atualizado pelo administrador.",
    "schema": "O catálogo respondeu de uma forma inesperada. Tente novamente mais tarde.",
    "quota": "Os downloads disponíveis para a comunidade terminaram por enquanto. Aguarde a renovação da conta conectada.",
    "local_quota": "Você já utilizou seu limite de hoje. Consulte Meus limites no menu para acompanhar a renovação.",
    "invalid_callback": "Este botão expirou. Abra o menu para continuar.",
}



def home_text(brand: str, preferences: dict) -> str:
    return (f"📚 <b>{esc(brand, 80)}</b>\n<i>Um bom livro. Uma nova descoberta.</i>\n\n"
            "<b>Qual será sua próxima leitura?</b>\n"
            "Envie o título, autor ou ISBN. Eu organizo as opções para você escolher.\n\n"
            f"🌐 {esc(LANGUAGES.get(preferences['language'], 'Todos os idiomas'))}"
            f"  ·  {esc(FORMATS.get(preferences['extension'], 'EPUB e PDF'))}\n"
            "<i>Se houver várias edições, você escolhe o idioma e o arquivo antes de receber.</i>")


def group_results(page: SearchPage, query: str, ident: str, groups: list[BookGroup], targets: list[str], label: str):
    lines = ["🔎 <b>Na estante de resultados</b>", f"<i>{esc(query, 200)}</i>",
             esc(label, 90), f"Página {page.page}", ""]
    rows = []
    if not groups:
        lines += ["<b>Ainda não encontrei essa leitura.</b>",
                  "Tente o título original, o nome do autor ou amplie o idioma."]
        rows.append([button("🌐 Tentar em todos os idiomas", f"searchall:{ident}")])
    for n, (group, target) in enumerate(zip(groups, targets), 1):
        langs = list(dict.fromkeys(language_name(b.language) for b in group.books))
        formats = list(dict.fromkeys(b.extension.upper() for b in group.books if b.extension))
        count = len(group.books)
        line = f"{count} edições nesta página" if count > 1 else " · ".join(formats + langs)
        lines.append(f"<b>{n}. {esc(group.title, 100)}</b>\n{esc(group.author, 65)}\n{esc(line, 95)}\n")
        suffix = f" · {count} edições" if count > 1 else ""
        rows.append([button(f"{n}. {clip(group.title, 48)}{suffix}", target)])
    navigation = []
    if page.page > 1:
        navigation.append(button("← Anterior", f"page:{ident}:{page.page - 1}"))
    if page.has_next and page.page < 1000:
        navigation.append(button("Próxima →", f"page:{ident}:{page.page + 1}"))
    if navigation:
        rows.append(navigation)
    rows.append([button("🌐 Filtros", "menu:settings"), button("🔎 Nova busca", "menu:search")])
    rows.append([button("📚 Início", "menu:home")])
    return "\n".join(lines), rows


def edition_results(books: list[Book], ident: str, page: int = 1):
    size = 6
    start = (page - 1) * size
    selected = books[start:start + size]
    lines = ["📖 <b>Escolha sua edição</b>", f"<b>{esc(books[0].title, 150)}</b>",
             esc(books[0].author, 100), "",
             "O mesmo livro pode ter idiomas, formatos ou edições diferentes.",
             f"{len(books)} opções deste resultado · Página {page}", ""]
    rows = []
    for n, b in enumerate(selected, start):
        fields = [b.extension.upper() or "Formato não informado", language_name(b.language)]
        if b.year:
            fields.append(b.year)
        if b.size is not None:
            fields.append(f"{b.size / 1_000_000:.1f} MB".replace(".", ","))
        meta = " · ".join(fields)
        lines.append(f"<b>{n + 1}. {esc(meta, 110)}</b>" + (f"\n{esc(b.publisher, 100)}" if b.publisher else ""))
        rows.append([button(f"{n + 1}. {meta}", f"open:{ident}:{n}")])
    nav = []
    if page > 1:
        nav.append(button("← Anterior", f"editions:{ident}:{page - 1}"))
    if start + size < len(books):
        nav.append(button("Mais edições →", f"editions:{ident}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([button("🌐 Filtros", "menu:settings"), button("🔎 Nova busca", "menu:search")])
    rows.append([button("📚 Início", "menu:home")])
    return "\n".join(lines), rows
