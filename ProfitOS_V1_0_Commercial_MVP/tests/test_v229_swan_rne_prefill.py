from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
S=(ROOT/"profitos"/"swan_baas.py").read_text(encoding="utf-8")
R=(ROOT/"profitos"/"routes"/"swan_baas.py").read_text(encoding="utf-8")
T=(ROOT/"templates"/"swan_settings.html").read_text(encoding="utf-8")

def test_current_swan_rne_query_is_read_only():
    block=S.split("def company_registry_data_fr(",1)[1].split("def request_new_account(",1)[0]
    assert "companyInfoRegistryData" in block
    assert "residencyAddressCountry" in block
    assert '"FRA"' in block
    assert "mutation " not in block

def test_siren_is_strictly_validated():
    block=S.split("def company_registry_data_fr(",1)[1].split("def request_new_account(",1)[0]
    assert "len(siren) != 9" in block

def test_rne_route_is_csrf_backed_and_no_account_mutation():
    assert "/settings/swan/company-registry" in R
    assert "company_registry_data_fr(siren)" in R
    assert 'name="csrf_token"' in T
    assert "Rechercher dans le RNE via Swan" in T

def test_legacy_onboarding_is_not_enabled_by_this_pass():
    # 48C intentionally validates Swan/RNE before any v2 write mutation.
    block=R.split("def swan_company_registry():",1)[1].split("def swan_test_connection():",1)[0]
    assert "request_new_account(" not in block
    assert "request_card(" not in block
