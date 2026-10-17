from pathlib import Path
I=Path("profitos/routes/imports.py").read_text(encoding="utf-8")
V=Path("profitos/routes/invoicing.py").read_text(encoding="utf-8")
E=Path("profitos/routes/expenses.py").read_text(encoding="utf-8")
H=Path("profitos/document_extraction.py").read_text(encoding="utf-8")

def test_scanned_pdf_fallback_uses_typed_signal():
    assert "class PdfTextUnavailable(ValueError)" in H
    assert "raise PdfTextUnavailable" in I
    assert "raise PdfTextUnavailable" in V
    assert "raise PdfTextUnavailable" in E

def test_no_obsolete_scanned_pdf_not_supported_message():
    assert "Les PDF scannés ne sont pas encore pris en charge" not in V
    assert "Pas d'OCR : un scan image est refusé" not in I

def test_financial_imports_keep_ai_fallbacks():
    assert "_ai_extract_pdf_fields" in I
    assert "_ai_extract_bank_statement_rows" in I
    assert "_purchase_ai_extract" in V
    assert "_receipt_ai_extract" in E
