from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
S=(ROOT/"profitos"/"swan_baas.py").read_text(encoding="utf-8")
R=(ROOT/"profitos"/"routes"/"swan_baas.py").read_text(encoding="utf-8")
T=(ROOT/"templates"/"swan_settings.html").read_text(encoding="utf-8")

def _create_block():
    return S.split("def create_company_onboarding_v2_sandbox(",1)[1].split("def request_card(",1)[0]

def test_v2_creation_uses_current_swan_mutation_and_typed_input():
    b=_create_block()
    assert "createCompanyAccountHolderOnboarding" in b
    assert "CreateCompanyAccountHolderOnboardingInput!" in b
    assert "CreateCompanyAccountHolderOnboardingSuccessPayload" in b
    assert "onboardingUrl" in b
    assert "OnboardingInvalidStatusInfo" in b

def test_v2_creation_is_hard_blocked_outside_sandbox():
    b=_create_block()
    assert 'current_environment() != "sandbox"' in b
    assert 'environment="sandbox"' in b

def test_route_requires_explicit_sandbox_confirmation_and_csrf_form():
    assert "/settings/swan/onboarding-v2/create" in R
    assert "confirm_sandbox" in R
    assert "current_environment() != 'sandbox'" in R
    assert "create_company_onboarding_v2_sandbox(input_data)" in R
    assert "url_for('swan_onboarding_v2_create')" in T
    assert 'name="csrf_token"' in T
    assert 'name="confirm_sandbox"' in T

def test_france_payload_has_required_business_and_related_individual_fields():
    for x in ("businessActivity","businessActivityDescription","monthlyPaymentVolume",
              "regulatoryClassification","relatedIndividuals","birthInfo",
              "unitedStatesTaxInfo","legalRepresentative","ultimateBeneficialOwner"):
        assert x in R

def test_rne_preview_is_preserved_for_onboarding_form():
    assert "registry_preview=session.get('swan_registry_preview')" in R
