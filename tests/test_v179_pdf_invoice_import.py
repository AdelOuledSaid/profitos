from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def test_pdf_invoice_parser_present():
    t=(ROOT/'profitos'/'routes'/'imports.py').read_text(encoding='utf-8')
    h=(ROOT/'profitos'/'document_extraction.py').read_text(encoding='utf-8')

    for x in ['def _extract_invoice_pdf', 'PdfReader', 'montant TTC', "request.files.getlist('file')"]:
        assert x in t

    # v275: no-text PDF detection is typed rather than based on an error string.
    assert 'PdfTextUnavailable' in t
    assert 'raise PdfTextUnavailable' in t
    assert 'class PdfTextUnavailable(ValueError)' in h
    assert 'is_pdf_text_unavailable' in t
