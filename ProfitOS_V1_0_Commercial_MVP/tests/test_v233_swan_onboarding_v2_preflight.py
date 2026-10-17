from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
S=(ROOT/"profitos"/"swan_baas.py").read_text(encoding="utf-8")
R=(ROOT/"profitos"/"routes"/"swan_baas.py").read_text(encoding="utf-8")
T=(ROOT/"templates"/"swan_settings.html").read_text(encoding="utf-8")

def _block():
    # Pass 50A inserts the real Sandbox creation function before request_card.
    # Limit this check strictly to the preflight function itself.
    return S.split("def company_onboarding_v2_preflight(",1)[1].split("def create_company_onboarding_v2_sandbox(",1)[0]

def test_preflight_checks_current_v2_mutation_and_inputs():
    b=_block()
    assert "createCompanyAccountHolderOnboarding" in b
    assert "CreateCompanyAccountHolderOnboardingInput" in b
    assert "CompanyInfoInput" in b
    for field in ("accountInfo","accountAdmin","company","legalFormCode","relatedIndividuals"):
        assert field in b

def test_preflight_is_read_only():
    b=_block()
    assert 'query ProfitOSCompanyOnboardingV2Preflight' in b
    assert "mutation ProfitOS" not in b

def test_preflight_route_and_csrf_ui_exist():
    assert "/settings/swan/onboarding-v2/preflight" in R
    assert "company_onboarding_v2_preflight()" in R
    assert "url_for('swan_onboarding_v2_preflight')" in T
    assert 'name="csrf_token"' in T
