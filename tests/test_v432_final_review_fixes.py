"""Corrections issues de la vérification indépendante : pages juridiques, preflight,
doublons d'écritures bancaires de la société mère, contrôle des filiales, paiement."""
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LEGAL_ENV = {'LEGAL_COMPANY_NAME': 'Exemple Logiciels SAS', 'LEGAL_FORM': 'SAS', 'LEGAL_SIREN': '732829320',
             'LEGAL_ADDRESS': '1 rue de la Paix, 75002 Paris', 'LEGAL_PUBLICATION_DIRECTOR': 'Camille Martin',
             'LEGAL_CONTACT_EMAIL': 'contact@exemple.fr', 'LEGAL_SHARE_CAPITAL': '10 000', 'LEGAL_RCS_CITY': 'Paris'}


def _client():
    from profitos import create_app
    from profitos.config import DevelopmentConfig
    app = create_app(DevelopmentConfig)
    app.config.update(TESTING=True)
    return app.test_client()


def test_legal_pages_use_configured_identity(monkeypatch):
    for k, v in LEGAL_ENV.items():
        monkeypatch.setenv(k, v)
    c = _client()
    html = c.get('/mentions-legales').get_data(as_text=True)
    # La forme juridique déjà présente dans la raison sociale n'est pas répétée (« SAS, SAS »).
    assert 'Exemple Logiciels, SAS' in html and 'SAS, SAS' not in html
    for value in ('732829320', 'Camille Martin', 'contact@exemple.fr', 'RCS de Paris', 'Render Services'):
        assert value in html
    assert 'à compléter' not in html and 'doivent correspondre' not in html
    privacy = c.get('/politique-confidentialite').get_data(as_text=True)
    for value in ('Anthropic', 'Stripe', 'Resend', 'Powens', 'CNIL', '12 mois', 'sous-traitant'):
        assert value in privacy
    cgv = c.get('/cgv').get_data(as_text=True)
    for value in ('hors taxes', '40 €', 'L441-10', 'art. 28', 'droit français', '12 mois'):
        assert value in cgv


def test_legal_pages_flag_missing_identity(monkeypatch):
    for k in LEGAL_ENV:
        monkeypatch.delenv(k, raising=False)
    html = _client().get('/mentions-legales').get_data(as_text=True)
    assert 'raison sociale à compléter' in html


def test_preflight_requires_legal_identity_and_secrets():
    import importlib.util
    spec = importlib.util.spec_from_file_location('pf', ROOT / 'scripts' / 'cloud_preflight.py')
    pf = importlib.util.module_from_spec(spec); spec.loader.exec_module(pf)
    assert pf.check_legal({}) and not pf.check_legal(LEGAL_ENV)
    errs = pf.check_secrets({})
    assert any('PROFITOS_DATA_KEY' in e for e in errs) and any('BACKUP_PASSPHRASE' in e for e in errs)
    assert pf.check_secrets({'PROFITOS_DATA_KEY': 'k' * 40, 'BACKUP_PASSPHRASE': 'p' * 20}) == []
    # « /tmpfoo » n'est pas /tmp
    assert not any('/tmp' in e for e in pf.check_upload_dir({'PROFITOS_UPLOAD_DIR': '/tmpfoo-not-writable-xyz/u'}) if 'pointer' in e)


def test_bank_entry_unique_even_for_parent_entity():
    src = (ROOT / 'profitos' / 'runtime.py').read_text(encoding='utf-8')
    assert 'uq_accounting_entries_bank_source_all' in src
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE accounting_entries(id INTEGER PRIMARY KEY, entity_id INTEGER, source_type TEXT, source_id TEXT)')
    db.execute("""CREATE UNIQUE INDEX uq ON accounting_entries(COALESCE(CAST(entity_id AS TEXT),''), source_type, source_id)
                  WHERE source_type IN ('bank_fee','bank_categorization')""")
    db.execute("INSERT INTO accounting_entries(entity_id,source_type,source_id) VALUES(NULL,'bank_fee','7')")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO accounting_entries(entity_id,source_type,source_id) VALUES(NULL,'bank_fee','7')")


def test_checkout_idempotency_key_is_unique_per_attempt():
    src = (ROOT / 'profitos' / 'routes' / 'account.py').read_text(encoding='utf-8')
    assert 'now_ts//300' not in src and 'secrets.token_hex(8)' in src


def test_subsidiary_identity_is_validated():
    src = (ROOT / 'profitos' / 'routes' / 'entities.py').read_text(encoding='utf-8')
    block = src.split('def entity_new():', 1)[1].split('return render_template', 1)[0]
    assert 'parse_company_form' in block
