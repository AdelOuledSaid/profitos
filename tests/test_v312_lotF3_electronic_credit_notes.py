from pathlib import Path
import ast
ROOT=Path(__file__).resolve().parents[1]
R=(ROOT/"profitos/routes/invoicing.py").read_text(encoding="utf-8")
RT=(ROOT/"profitos/runtime.py").read_text(encoding="utf-8")
T=(ROOT/"templates/invoicing_credit_detail.html").read_text(encoding="utf-8")
def test_credit_note_facturx_381_reference():
    assert "def generate_credit_facturx_xml" in R
    assert "type_code.text='381'" in R
    assert "InvoiceReferencedDocument" in R
    assert "original_invoice_number" in R
def test_credit_note_submission_scoped_idempotent():
    assert "/facturation/avoir/<int:credit_id>/electronique/envoyer" in R
    assert "submit_invoice_file(settings['weinvoice_company_id']" in R
    assert 'idem=f"credit-' in R
    assert "WHERE id=? AND entity_id IS ?" in R
def test_credit_note_remote_state_schema():
    for name in ("weinvoice_invoice_id","weinvoice_status","weinvoice_regulatory_code","weinvoice_last_sync_at","weinvoice_last_error"):
        assert name in RT
    assert "idx_credit_weinvoice_remote" in RT
def test_credit_note_ui_csrf_and_sync():
    assert "invoicing_credit_electronic_send" in T
    assert "invoicing_credit_electronic_sync" in T
    assert 'name="csrf_token"' in T
def test_modified_python_parses():
    ast.parse(R); ast.parse(RT)
