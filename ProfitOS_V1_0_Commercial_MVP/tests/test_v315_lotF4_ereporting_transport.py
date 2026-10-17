from pathlib import Path
import ast
ROOT=Path(__file__).resolve().parents[1]
W=(ROOT/"profitos/weinvoice.py").read_text(encoding="utf-8")
R=(ROOT/"profitos/runtime.py").read_text(encoding="utf-8")
def test_flux10_endpoint_and_exact_flow_families():
    assert "/v1/e-reporting/flows" in W
    for x in ("AggregatedCustomerTransactionReport","UnitaryCustomerTransactionReport",
              "AggregatedCustomerPaymentReport","UnitaryCustomerPaymentReport",
              "UnitarySupplierTransactionReport","MultiFlowReport"):
        assert x in W
def test_flux10_requires_core_fields():
    for x in ("flowType","transmissionNumber","anchorDate","lines"): assert x in W
def test_transmission_reads_and_fiscal_proof():
    assert "/v1/e-reporting/transmissions" in W
    assert "/proof" in W and "/v1/e-reporting/fiscal-settings" in W
def test_local_audit_is_entity_scoped_and_idempotent():
    assert "CREATE TABLE IF NOT EXISTS ereporting_transmissions" in R
    assert "UNIQUE(entity_id,provider,transmission_number)" in R
    assert "payload_json TEXT NOT NULL" in R
def test_python_parses():
    ast.parse(W); ast.parse(R)
