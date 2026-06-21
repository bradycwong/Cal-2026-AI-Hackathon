"""pdf_extract.py — PDF bytes -> plain text for paste-to-import.

A PDF is just another *source of prose*: extract its text here, then hand it to
the existing ``protocol_import.import_protocol`` pipeline (compound-step splitting,
deterministic fallback, validation, file write). Kept in its own module so the
bytes->text concern stays isolated and independently testable.

``pypdf`` is imported lazily so importing the backend never hard-fails on a missing
optional dependency — only an actual PDF upload exercises it.
"""

from __future__ import annotations

import re
from io import BytesIO

# A numbered/bulleted step marker at the start of a line. The lookahead requires a
# space or end-of-line after the marker so a decimal like "1.5 mL" is NOT mistaken
# for step marker "1.".
_MARKER_RE = re.compile(r"^\s*(?:\d+[.)]|[-*•])(?=\s|$)")
# Sentence boundary: terminal punctuation + space + a capital/digit starting the next.
_SENTENCE_RE = re.compile(r"(?<=[.;:])\s+(?=[A-Z0-9])")


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


def reflow_pdf_text(text: str) -> str:
    """Re-segment messy PDF-extracted text into one logical step per line.

    ``pypdf.extract_text`` inserts a newline at every PDF *layout* wrap, so a
    wrapped sentence fragments and a word-per-line layout explodes. The downstream
    parsers split on newlines, so we rebuild real step boundaries here:

    * If numbered/bulleted markers are present, accumulate each wrapped continuation
      line under the current marker and emit one step per marker.
    * Otherwise, collapse all whitespace and split on sentence boundaries.

    Applied to PDF text only; pasted text (whose newlines are intentional) is
    untouched.
    """
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    if not lines:
        return ""

    if any(_MARKER_RE.match(ln) for ln in lines):
        steps: list[str] = []
        current: list[str] = []
        for ln in lines:
            if _MARKER_RE.match(ln):
                if current:
                    steps.append(" ".join(current))
                current = [_MARKER_RE.sub("", ln).strip()]
            else:
                current.append(ln)  # wrapped continuation of the current step
        if current:
            steps.append(" ".join(current))
        return "\n".join(s.strip() for s in steps if s.strip())

    blob = re.sub(r"\s+", " ", " ".join(lines)).strip()
    parts = (p.strip() for p in _SENTENCE_RE.split(blob))
    return "\n".join(p for p in parts if p)
