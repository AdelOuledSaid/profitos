from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
R=(ROOT/"profitos"/"routes"/"swan_baas.py").read_text(encoding="utf-8")
T=(ROOT/"templates"/"swan_settings.html").read_text(encoding="utf-8")

def test_rne_lookup_persists_validated_siren():
    assert "['registrationNumber'] =" in R
    assert "company_registry_data_fr(" in R

def test_onboarding_uses_server_side_rne_siren_only():
    block=R.split("def swan_onboarding_v2_create():",1)[1].split("def swan_connection_test",1)[0]
    assert "preview.get('registrationNumber')" in block
    assert "request.form.get('registration_number')" not in block
    assert "SIREN RNE validé absent" in block

def test_siren_is_visible_and_locked():
    assert "SIREN validé par le RNE" in T
    assert "registry_preview.get('registrationNumber','')" in T
    assert "readonly" in T
    assert 'name="registration_number"' not in T

def test_changing_company_requires_new_rne_lookup():
    assert "relancez la recherche RNE pour changer d'entreprise" in T
