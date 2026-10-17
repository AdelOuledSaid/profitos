from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def test_v175_version():
    assert 'APP_VERSION = "1.7.5"' in (ROOT/'profitos/config.py').read_text(encoding='utf-8')

def test_landing_is_sme_positioned():
    t=(ROOT/'templates/landing.html').read_text(encoding='utf-8')
    assert 'FINANCIAL OPERATING SYSTEM POUR VOTRE ENTREPRISE' in t
    assert 'entreprises BTP' not in t and 'POUR LE BTP' not in t
    assert 'Client ABC' in t

def test_manifest_is_not_construction_only():
    t=(ROOT/'static/manifest.json').read_text(encoding='utf-8')
    assert 'Construction' not in t
    assert 'SMEs' in t

def test_financial_brain_uses_selected_index():
    t=(ROOT/'profitos/routes/financial_brain.py').read_text(encoding='utf-8')
    assert "SELECT price_index_name FROM app_settings WHERE id=1" in t
    assert "WHERE index_name=?" in t
    assert "index_name='BT01'" not in t

def test_new_schema_default_is_neutral():
    t=(ROOT/'profitos/runtime.py').read_text(encoding='utf-8')
    assert "price_index_name TEXT DEFAULT 'INDICE'" in t
    assert "index_name TEXT DEFAULT 'INDICE'" in t
