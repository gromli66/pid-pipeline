"""
PDF → PNG рендер для загрузки P&ID схем.

Модели pipeline обучались на 300 DPI сканах (~4900×3500 px), поэтому PDF
рендерим в 300 DPI. Защитный лимит PDF_MAX_SIDE масштабирует вниз большие
форматы (A0/A1), чтобы не плодить гигантские растры и не превышать UI-лимит.

Зависимость: PyMuPDF (fitz) — без системных зависимостей (poppler не нужен).
"""

import io
from typing import Optional, Tuple

import fitz  # PyMuPDF

_POINTS_PER_INCH = 72.0


def get_pdf_page_count(pdf_bytes: bytes) -> int:
    """Число страниц в PDF."""
    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            return doc.page_count
    except Exception as exc:
        raise ValueError(f"Не удалось открыть PDF: {exc}")


def render_pdf_page_to_png(
    pdf_bytes: bytes,
    page: int = 1,
    dpi: int = 300,
    max_side: Optional[int] = None,
) -> Tuple[bytes, Tuple[int, int], int]:
    """
    Отрендерить одну страницу PDF в PNG.

    Args:
        pdf_bytes: содержимое PDF.
        page: номер страницы, 1-based.
        dpi: целевой DPI (по умолчанию 300 — как обучались модели).
        max_side: если задан и длинная сторона при `dpi` превышает его —
            масштаб уменьшается пропорционально (эффективный DPI < dpi).

    Returns:
        (png_bytes, (width, height), n_pages)

    Raises:
        ValueError: пустой/битый PDF или номер страницы вне диапазона.
    """
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:  # fitz.FileDataError и пр. → 400, не 500
        raise ValueError(f"Не удалось открыть PDF: {exc}")
    with doc:
        n_pages = doc.page_count
        if n_pages == 0:
            raise ValueError("PDF не содержит страниц")
        if page < 1 or page > n_pages:
            raise ValueError(f"Страница {page} вне диапазона 1..{n_pages}")

        pg = doc.load_page(page - 1)
        zoom = dpi / _POINTS_PER_INCH

        # Защитный лимит по длинной стороне
        rect = pg.rect  # в точках (1/72 дюйма)
        long_px = max(rect.width, rect.height) * zoom
        if max_side and long_px > max_side:
            zoom *= max_side / long_px

        matrix = fitz.Matrix(zoom, zoom)
        pix = pg.get_pixmap(matrix=matrix, alpha=False)
        png_bytes = pix.tobytes("png")
        return png_bytes, (pix.width, pix.height), n_pages
