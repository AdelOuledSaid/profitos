from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
S = (ROOT/"profitos"/"swan_baas.py").read_text(encoding="utf-8")

def _block():
    return S.split("def company_registry_data_fr(",1)[1].split("def request_new_account(",1)[0]

def test_swan_companyinfo_uses_real_legal_form_field():
    b = _block()
    assert "legalFormCode" in b
    assert "\n          legalForm\n" not in b

def test_legal_form_code_is_normalized_for_existing_profitos_ui():
    b = _block()
    assert 'info["legalForm"] = info.get("legalFormCode")' in b

def test_rne_query_remains_read_only():
    b = _block()
    assert "mutation " not in b
    assert "... on CompanyInfo {" in b
    assert "companyInfoRegistryData" in b
