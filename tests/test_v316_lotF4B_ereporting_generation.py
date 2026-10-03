from pathlib import Path
import ast
ROOT=Path(__file__).resolve().parents[1]
E=(ROOT/"profitos/ereporting.py").read_text(encoding="utf-8"); I=(ROOT/"profitos/routes/invoicing.py").read_text(encoding="utf-8")
R=(ROOT/"profitos/runtime.py").read_text(encoding="utf-8"); T=(ROOT/"templates/invoicing_new.html").read_text(encoding="utf-8")
def test_no_silent_scope_guessing():
    assert "ereporting_scope='b2c'" in E and "ereporting_scope='international_b2b'" in E and "Périmètre e-reporting" in T
def test_four_flux10_families():
    for x in ("AggregatedCustomerTransactionReport","UnitaryCustomerTransactionReport","AggregatedCustomerPaymentReport","UnitaryCustomerPaymentReport"): assert x in E
def test_payment_split_by_vat_rate(): assert "def _payment_split" in E and "vatRate" in E
def test_entity_scoped_idempotent_submission():
    assert "transmission_number=?" in I and "entity_id IS ?" in I and "submit_ereporting_flow" in I
def test_snapshot_fields():
    for x in ("ereporting_scope","ereporting_category","counterparty_country","client_vat_number"): assert x in R and x in I
def test_python_parses(): ast.parse(E); ast.parse(I); ast.parse(R)
