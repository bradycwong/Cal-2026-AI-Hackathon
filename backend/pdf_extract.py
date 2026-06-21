"""pdf_extract.py — PDF bytes -> plain text for paste-to-import.

A PDF is just another *source of prose*: extract its text here, then hand it to
the existing ``protocol_import.import_protocol`` pipeline (compound-step splitting,
deterministic fallback, validation, file write). Kept in its own module so the
bytes->text concern stays isolated and independently testable.

``pypdf`` is imported lazily so importing the backend never hard-fails on a missing
optional dependency — only an actual PDF upload exercises it.
"""

from __future__ import annotations

from io import BytesIO


class PdfExtractError(ValueError):
    """The PDF could not be parsed (corrupt, encrypted, or not a PDF)."""


def extract_pdf_text(data: bytes) -> str:
    """Extract a PDF's text, preserving page/line structure for the splitter.

    Returns the concatenated page text, stripped. A PDF with no text layer (e.g.
    a scanned image) yields ``""`` — the caller turns that into a friendly error.
    Raises ``PdfExtractError`` when the bytes can't be parsed as a PDF.
    """
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    try:
        reader = PdfReader(BytesIO(data))
        pages = [page.extract_text() or "" for page in reader.pages]
    except (PyPdfError, OSError, ValueError) as exc:
        raise PdfExtractError(str(exc)) from exc
    return "\n".join(pages).strip()
