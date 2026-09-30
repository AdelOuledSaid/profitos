from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
S=(ROOT/"profitos"/"swan_baas.py").read_text(encoding="utf-8")

def _block():
    return S.split("def company_registry_data_fr(",1)[1].split("def request_new_account(",1)[0]

def test_rne_does_not_hardcode_rejected_payload_types():
    b=_block()
    assert "CompanyInfoRegistryDataSuccessPayload" not in b
    assert "OnboardingIndividualRepresentative" not in b
    assert "CompanyRegistryNotFoundRejection" not in b

def test_rne_probes_actual_sandbox_typename():
    b=_block()
    assert "companyInfoRegistryData(input: $input)" in b
    assert "__typename" in b
    assert "payload_type = payload.get" in b

def test_rne_uses_graphql_introspection_before_fragment():
    b=_block()
    assert "__type(name: $name)" in b
    assert '"companyInfo" not in fields' in b
    assert "... on {payload_type}" in b

def test_rne_remains_read_only():
    b=_block()
    assert "mutation " not in b
    assert "registrationNumber" in b
    assert "residencyAddressCountry" in b
