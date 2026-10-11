from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
INV=(ROOT/'profitos/routes/invoicing.py').read_text(encoding='utf-8')


def test_purchase_form_dates_are_strictly_validated_and_canonicalized():
    helper=INV.split('def _purchase_form_date',1)[1].split('def _purchase_pdf_date',1)[0]
    assert 'date.fromisoformat(value).isoformat()' in helper
    assert 'AAAA-MM-JJ' in helper


def test_purchase_new_validates_dates_before_insert():
    block=INV.split('def purchase_new',1)[1].split('def supplier_detail',1)[0]
    assert '_purchase_form_date(request.form.get(\'issue_date\')' in block
    assert '_purchase_form_date(request.form.get(\'due_date\')' in block
    assert 'issue_date,\n                       due_date,' in block
    assert "request.form.get('issue_date') or None" not in block
    assert "request.form.get('due_date') or None" not in block


def test_purchase_edit_validates_dates_before_update():
    block=INV.split('def purchase_edit',1)[1].split('def purchase_document_upload',1)[0]
    assert '_purchase_form_date(request.form.get(\'issue_date\')' in block
    assert '_purchase_form_date(request.form.get(\'due_date\')' in block
    assert '(supplier_id,supplier_name,number,issue_date,' in block
    assert "request.form.get('issue_date') or None" not in block
    assert "request.form.get('due_date') or None" not in block
