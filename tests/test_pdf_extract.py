"""PDF text extraction (bytes -> plain text) for protocol import."""

from pathlib import Path

import pytest

from backend.pdf_extract import PdfExtractError, extract_pdf_text

FIXTURES = Path(__file__).parent / "fixtures"


def _bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_extract_pdf_text_reads_typed_protocol():
    text = extract_pdf_text(_bytes("sample_protocol.pdf"))
    assert "Add 200 uL lysis buffer" in text
    assert "Incubate 10 minutes" in text


def test_extract_pdf_text_empty_for_textless_pdf():
    # A scan-like PDF (graphics only, no text layer) yields no extractable text.
    assert extract_pdf_text(_bytes("blank_scan.pdf")) == ""


def test_extract_pdf_text_raises_on_garbage():
    with pytest.raises(PdfExtractError):
        extract_pdf_text(b"this is not a pdf")
