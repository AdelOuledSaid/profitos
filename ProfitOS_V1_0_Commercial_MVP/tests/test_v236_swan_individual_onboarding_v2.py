from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
S=(ROOT/"profitos"/"swan_baas.py").read_text(encoding="utf-8")
R=(ROOT/"profitos"/"routes"/"swan_baas.py").read_text(encoding="utf-8")
T=(ROOT/"templates"/"swan_settings.html").read_text(encoding="utf-8")

def _service_block():
    return S.split("def create_individual_onboarding_v2_sandbox(",1)[1].split("def request_card(",1)[0]

def _route_block():
    return R.split("def swan_individual_onboarding_v2_create():",1)[1].split("@app.route('/settings/swan/test-connection'",1)[0]

def test_uses_current_swan_individual_onboarding_api():
    b=_service_block()
    assert "createIndividualAccountHolderOnboarding" in b
    assert "CreateIndividualAccountHolderOnboardingInput!" in b
    assert "CreateIndividualAccountHolderOnboardingSuccessPayload" in b

def test_individual_mutation_is_hard_blocked_outside_sandbox():
    b=_service_block()
    assert 'current_environment() != "sandbox"' in b
    assert 'environment="sandbox"' in b

def test_route_builds_required_france_payload():
    b=_route_block()
    for token in ["'accountInfo'", "'country': 'FRA'", "'accountAdmin'", "'employmentStatus'",
                  "'preferredLanguage': 'fr'", "'monthlyIncome'", "'address'",
                  "'unitedStatesTaxInfo'", "'isUnitedStatesPerson': False"]:
        assert token in b

def test_route_requires_explicit_sandbox_confirmation():
    b=_route_block()
    assert "confirm_individual_sandbox" in b
    assert "!= 'yes'" in b

def test_ui_has_csrf_and_no_siren_input():
    assert "swan_individual_onboarding_v2_create" in T
    block=T.split("Onboarding individuel Swan — Sandbox",1)[1].split("Onboarding Swan v2 — pré-vérification",1)[0]
    assert 'name="csrf_token"' in block
    assert 'name="registration_number"' not in block
    assert 'name="siren"' not in block.lower()

def test_result_exposes_onboarding_url():
    assert "individual_onboarding_result.get('onboarding_url')" in T
