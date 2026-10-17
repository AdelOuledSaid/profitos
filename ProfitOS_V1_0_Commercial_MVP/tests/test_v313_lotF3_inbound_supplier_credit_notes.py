from pathlib import Path
import ast
ROOT=Path(__file__).resolve().parents[1]
I=(ROOT/"profitos/routes/invoicing.py").read_text(encoding="utf-8")
A=(ROOT/"profitos/accounting.py").read_text(encoding="utf-8")
R=(ROOT/"profitos/runtime.py").read_text(encoding="utf-8")
W=(ROOT/"profitos/weinvoice.py").read_text(encoding="utf-8")
def test_inbound_credit_detection_and_reference():
    assert "_is_inbound_credit_note" in I and "{'261','381','396','502','503'}" in I
    assert "BillingReference" in I and "InvoiceReferencedDocument" in I
def test_credit_requires_original_purchase():
    assert "facture fournisseur d'origine introuvable" in I and "original_purchase_id" in R
def test_supplier_credit_accounting_reversal():
    assert "def generate_purchase_credit_entry" in A
    assert "'401000','debit':credit['total']" in A
    assert "'445660','credit':credit['vat_amount']" in A
def test_entity_scope_and_deduplication():
    assert "idx_purchase_credit_weinvoice_remote" in R
    assert "purchase_credit_notes WHERE entity_id IS ? AND weinvoice_invoice_id=?" in I
def test_original_document_endpoint():
    assert "/v1/invoice-queries/{remote_id}/file" in W
    assert "download_inbound_invoice_original" in I
def test_python_parses():
    ast.parse(I); ast.parse(A); ast.parse(R); ast.parse(W)
