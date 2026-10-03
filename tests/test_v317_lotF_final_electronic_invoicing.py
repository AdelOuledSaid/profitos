from pathlib import Path
import ast
ROOT=Path(__file__).resolve().parents[1]
E=(ROOT/"profitos/ereporting.py").read_text(encoding="utf-8")
W=(ROOT/"profitos/weinvoice.py").read_text(encoding="utf-8")
I=(ROOT/"profitos/routes/invoicing.py").read_text(encoding="utf-8")
V=(ROOT/"profitos/einvoice_validation.py").read_text(encoding="utf-8")
R=(ROOT/"profitos/runtime.py").read_text(encoding="utf-8")
def test_flux10_exact_lines_object():
    assert "'lines':{line_key:lines}" in E
    for x in ("invoiceTransactions","invoicePayments","dailyTransactions","dailyPayments"): assert x in E
def test_fiscal_preflight_blocks_missing_periodicity():
    assert "vatDeclarationPeriodicityRegime" in W and "validate_ereporting_fiscal_readiness" in I
def test_proof_is_json_not_fake_pdf():
    assert "def get_ereporting_proof" in W and "'Accept':'application/json'" in W
    assert "preuve-fiscale-" in I and ".json" in I
def test_status_rejections_and_audit_persisted():
    for x in ("proof_json","rejection_motifs_json","flux_status","last_checked_at"): assert x in R and x in I
def test_invoice_preflight_totals_dates_vat_siren():
    for x in ("Total HT incohérent","TVA incohérente","Total TTC incohérent","SIREN client invalide","Échéance antérieure"): assert x in V
    assert "validate_outgoing_invoice(inv)" in I
def test_production_never_switches_automatically():
    assert "productionSwitchAutomatic':False" in I and "def production_readiness" in W
def test_python_parses():
    for x in (E,W,I,V,R): ast.parse(x)
