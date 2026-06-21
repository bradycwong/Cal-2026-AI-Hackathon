"""PDF text extraction (bytes -> plain text) for protocol import."""

from pathlib import Path

import pytest

from backend.pdf_extract import PdfExtractError, extract_pdf_text, reflow_pdf_text

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


# --- reflow: turn messy PDF line breaks back into one logical step per line ---

def test_reflow_rejoins_wrapped_numbered_steps():
    raw = "\n".join([
        "1. Add 200 uL of lysis buffer to the sample tube and mix",
        "thoroughly by pipetting up and down.",
        "2. Incubate at 30 degrees C for 10 minutes.",
    ])
    steps = reflow_pdf_text(raw).split("\n")
    assert steps == [
        "Add 200 uL of lysis buffer to the sample tube and mix thoroughly by pipetting up and down.",
        "Incubate at 30 degrees C for 10 minutes.",
    ]


def test_reflow_handles_word_per_line_numbered():
    raw = "\n".join(["1.", "Add", "200", "uL", "lysis", "buffer.", "2.", "Incubate", "10", "minutes."])
    assert reflow_pdf_text(raw).split("\n") == ["Add 200 uL lysis buffer.", "Incubate 10 minutes."]


def test_reflow_splits_unnumbered_prose_into_sentences():
    raw = "\n".join([
        "Add 200 uL of lysis buffer and mix thoroughly by pipetting up",
        "and down ten times. Incubate at room temperature for 5 minutes.",
        "Centrifuge at 13000 g for 1 minute.",
    ])
    steps = reflow_pdf_text(raw).split("\n")
    assert len(steps) == 3
    assert steps[0].startswith("Add 200 uL")
    assert steps[1].startswith("Incubate at room temperature")
    assert steps[2].startswith("Centrifuge")


def test_reflow_does_not_split_on_decimal_points():
    # "1.5" must not be read as a step marker, and the decimal must not split a sentence.
    raw = "Add 1.5 mL of ethanol to the tube."
    assert reflow_pdf_text(raw).split("\n") == ["Add 1.5 mL of ethanol to the tube."]


def test_reflow_empty_is_empty():
    assert reflow_pdf_text("   \n  \n") == ""
