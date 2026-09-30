from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
S=(ROOT/"profitos"/"swan_baas.py").read_text(encoding="utf-8")

def _block():
    return S.split("def company_registry_data_fr(",1)[1].split("def request_new_account(",1)[0]

def test_rne_handles_company_info_as_direct_success_object():
    b=_block()
    assert 'payload_type != "CompanyInfo"' in b
    assert "... on CompanyInfo {" in b
    assert "companyInfo {" not in b

def test_rne_fetches_stable_company_fields_directly():
    b=_block()
    for field in ("name", "legalForm", "registrationDate", "addressLine1", "city", "postalCode", "country"):
        assert field in b

def test_rne_does_not_reintroduce_wrong_payload_types():
    b=_block()
    assert "CompanyInfoRegistryDataSuccessPayload" not in b
    assert "CompanyRegistryNotFoundRejection" not in b
    assert "OnboardingIndividualRepresentative" not in b

def test_rne_remains_read_only():
    b=_block()
    assert "mutation " not in b
    assert "companyInfoRegistryData" in b
