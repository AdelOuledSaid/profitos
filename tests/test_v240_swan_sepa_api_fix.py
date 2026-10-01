from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
S=(ROOT/"profitos"/"swan_baas.py").read_text(encoding="utf-8")

def test_sepa_beneficiary_matches_current_swan_schema():
    assert "isMyOwnIban" not in S
    assert "'sepaBeneficiary': {'iban': iban, 'name': beneficiary_name, 'save': False}" in S
    assert "'mode': 'Regular'" in S
    assert "'idempotencyKey':" in S

def test_sandbox_guard_and_consent_are_preserved():
    assert "current_environment() != 'sandbox'" in S
    assert "consentUrl" in S
