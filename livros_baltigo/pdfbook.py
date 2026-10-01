"""Deterministic typesetting of a public ebook's complete UTF-8 text.

This is not an original scanned edition, a translation, or an AI summary.
All source words, including credits and license, are retained; prose line wraps are reflowed.
"""
from __future__ import annotations
import html
import re
import threading
from pathlib import Path
from .errors import UserError

_font_lock = threading.Lock()


def text_to_pdf(text: str, destination: Path, title: str, author: str) -> int:
    import reportlab
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
    from reportlab.lib.enums import TA_LEFT
    if not text.strip() or len(text) > 2_000_000:
        raise UserError('O texto desta edição excede o limite de conversão. Escolha EPUB.', 'conversion_limit')
    with _font_lock:
        if 'LivrosText' not in pdfmetrics.getRegisteredFontNames():
            font_dir = Path(reportlab.__file__).parent / 'fonts'
            pdfmetrics.registerFont(TTFont('LivrosText', str(font_dir / 'Vera.ttf')))
            pdfmetrics.registerFont(TTFont('LivrosTitle', str(font_dir / 'VeraBd.ttf')))
    # Never silently replace unsupported characters with empty squares.
    glyphs = pdfmetrics.getFont('LivrosText').face.charWidths
    chars = set(text + title + author) - set('\r\n\t\f')
    if any(ord(char) not in glyphs for char in chars):
        raise UserError('Esta edição possui caracteres não suportados na conversão PDF. Escolha EPUB para preservar o original.', 'conversion_alphabet')
    body = ParagraphStyle('book', fontName='LivrosText', fontSize=10.5,
                          leading=15.5, spaceAfter=9, alignment=TA_LEFT, splitLongWords=True)
    heading = ParagraphStyle('title', parent=body, fontName='LivrosTitle', fontSize=21, leading=28, spaceAfter=18)
    small = ParagraphStyle('small', parent=body, fontSize=9, leading=13)
    story = [Spacer(1, 60), Paragraph(html.escape(title), heading),
             Paragraph(html.escape(author), body), Spacer(1, 32),
             Paragraph('Edição para leitura em PDF', body),
             Paragraph('Preparada a partir do texto integral do Project Gutenberg. '
                       'Não reproduz a paginação da edição impressa. '
                       'Créditos, ortografia e licença constam no texto a seguir.', small), PageBreak()]
    # Reflow hard-wrapped prose without changing words. Preserve visibly indented verse/tables.
    normalized = text.replace('\r\n', '\n').replace('\r', '\n').replace('\f', '\n\n')
    for block in re.split(r'\n[ \t]*\n', normalized):
        if block.strip():
            lines = block.splitlines()
            indented = len(lines) > 1 and sum(bool(re.match(r'^[ \t]{2,}\S', line)) for line in lines) >= len(lines) / 2
            rendered = '<br/>'.join(html.escape(line) for line in lines) if indented else html.escape(' '.join(line.strip() for line in lines))
            story.append(Paragraph(rendered, body))
    if len(story) > 40000:
        raise UserError('A estrutura desta edição é muito extensa. Escolha EPUB.', 'conversion_limit')
    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont('LivrosText', 8)
        canvas.drawRightString(doc.pagesize[0]-42, 24, str(doc.page))
        canvas.restoreState()
    try:
        doc = SimpleDocTemplate(str(destination), pagesize=(432,648),
                                leftMargin=42,rightMargin=42,topMargin=42,bottomMargin=40,
                                title=title,author=author)
        doc.build(story, onFirstPage=footer, onLaterPages=footer)
        return destination.stat().st_size
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
