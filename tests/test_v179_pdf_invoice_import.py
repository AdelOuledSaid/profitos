from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def test_pdf_invoice_upload_ui_and_allowlist():
    t=(ROOT/'templates'/'upload.html').read_text(encoding='utf-8')
    r=(ROOT/'profitos'/'runtime.py').read_text(encoding='utf-8')
    assert '.pdf' in t
    assert 'multiple' in t
    assert "'.pdf'" in r

def test_pdf_invoice_parser_present():
    t=(ROOT/'profitos'/'routes'/'imports.py').read_text(encoding='utf-8')
    for x in ['def _extract_invoice_pdf', 'PdfReader', 'montant TTC', 'PDF sans texte exploitable', "request.files.getlist('file')"]:
        assert x in t
