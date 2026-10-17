from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def test_lot13_purchase_pdf_import_present():
    r=(ROOT/'profitos'/'routes'/'invoicing.py').read_text(encoding='utf-8')
    t=(ROOT/'templates'/'purchase_new.html').read_text(encoding='utf-8')
    for x in ['def _purchase_pdf_extract', 'def purchase_import_pdf', 'pending_document',
              'HT + TVA', 'aucune création automatique']:
        assert x in (r+t)
