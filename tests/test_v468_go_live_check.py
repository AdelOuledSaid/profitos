"""v465 à v468 — Contrôle avant la mise en vente (scripts/go_live_check.py) : chaque
service tiers doit être en mode production, sans jamais afficher un secret."""
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('go_live_check', ROOT / 'scripts' / 'go_live_check.py')
G = importlib.util.module_from_spec(spec)
spec.loader.exec_module(G)

SECRETS = {'WEINVOICE_CLIENT_SECRET': 'wi-secret-AAAA', 'WEINVOICE_INVOICE_CLIENT_SECRET': 'wi-secret-BBBB',
           'SWAN_CLIENT_SECRET': 'swan-secret-CCCC', 'STRIPE_SECRET_KEY': 'sk_live_DDDDDDDD',
           'STRIPE_WEBHOOK_SECRET': 'whsec_EEEE', 'RESEND_API_KEY': 're_FFFF', 'BACKUP_PASSPHRASE': 'G' * 24,
           'POWENS_CLIENT_SECRET': 'pw-secret-HHHH', 'BACKUP_S3_SECRET_ACCESS_KEY': 'IIII'}

READY = dict(SECRETS, **{
    'APP_BASE_URL': 'https://app.profitos.fr',
    'WEINVOICE_ENV': 'production', 'WEINVOICE_CLIENT_ID': 'wi', 'WEINVOICE_INVOICE_CLIENT_ID': 'wi2',
    'WEINVOICE_WEBHOOK_SECRET': 'w1', 'WEINVOICE_INVOICE_WEBHOOK_SECRET': 'w2',
    'SWAN_CLIENT_ID': 'swan', 'SWAN_ENVIRONMENT': 'live', 'SWAN_WEBHOOK_SECRET': 's',
    'SWAN_OAUTH_REDIRECT_URI': 'https://app.profitos.fr/compte-pro/oauth/callback',
    'STRIPE_PRICE_STARTER_ID': 'price_1', 'STRIPE_PRICE_PRO_ID': 'price_2', 'STRIPE_PRICE_BUSINESS_ID': 'price_3',
    'STRIPE_PRICE_MULTI_ID': 'price_4', 'RESEND_FROM_EMAIL': 'ProfitOS <noreply@profitos.fr>',
    'BACKUP_S3_BUCKET': 'b', 'BACKUP_S3_ACCESS_KEY_ID': 'k',
})   # mentions légales (LEGAL_*) : contrôlées par profitos.legal, testées ailleurs


def levels(items, section=None):
    return {(it['section'], it['label']): it['level'] for it in items if section is None or it['section'].startswith(section)}


def failing(items):
    return [it['label'] for it in items if it['level'] == G.FAIL]


def test_sandbox_et_cles_de_test_bloquent():
    env = dict(READY, WEINVOICE_ENV='sandbox', SWAN_ENVIRONMENT='sandbox', STRIPE_SECRET_KEY='sk_test_XXXX',
               RESEND_FROM_EMAIL='onboarding@resend.dev', EDI_SIMULATION_ENABLED='1')
    bad = failing(G.configuration_checks(env))
    for label in ('Environnement sandbox', 'Swan : environnement sandbox', 'Clé de test', 'Expéditeur : domaine resend.dev',
                  'Simulation EDI active en production'):
        assert label in bad, (label, bad)


def test_configuration_de_production_complete():
    items = G.configuration_checks(READY)
    blocking = [it for it in items if it['level'] == G.FAIL and it['section'] != 'Mise en vente']
    assert not blocking, blocking
    # Déclarations fermées par défaut : simple information.
    assert levels(items)[('Mise en vente', 'Déclarations fiscales fermées')] == G.INFO
    opened = G.configuration_checks(dict(READY, FISCAL_DECLARATIONS_ENABLED='1'))
    assert levels(opened)[('Mise en vente', 'Déclarations fiscales ouvertes à tous')] == G.WARN


def test_points_de_configuration_detectes():
    env = dict(READY, SWAN_OAUTH_REDIRECT_URI='https://autre.example/cb', STRIPE_WEBHOOK_SECRET='', BACKUP_PASSPHRASE='court')
    env.pop('WEINVOICE_INVOICE_WEBHOOK_SECRET')
    env.pop('STRIPE_PRICE_MULTI_ID')
    items = G.configuration_checks(env)
    lv = levels(items)
    assert lv[('Banque (Swan / Powens)', 'Swan : adresse de retour OAuth')] == G.FAIL
    assert lv[('Abonnements (Stripe)', 'Secret du webhook')] == G.FAIL
    assert lv[('Abonnements (Stripe)', 'Tarifs configurés : 3 sur 4')] == G.WARN
    assert lv[('Sauvegardes', 'Phrase de passe (16 caractères minimum)')] == G.FAIL
    assert lv[('Facture électronique (WeInvoice)', 'Signature des notifications (factures)')] == G.FAIL
    # S3 partiel : signalé, pas de plantage.
    partial = levels(G.configuration_checks(dict(READY, BACKUP_S3_ACCESS_KEY_ID='')))
    assert partial[('Sauvegardes', 'Pas de copie hors site')] == G.WARN
    # Services absents : avertissements, pas de blocage (sauf Stripe et e-mail).
    empty = G.configuration_checks({})
    assert set(failing(empty)) >= {'Non configuré', 'Aucun fournisseur'}
    assert levels(empty)[('Banque (Swan / Powens)', 'Aucune connexion bancaire configurée')] == G.WARN


def test_aucun_secret_affiche(capsys):
    G.main([], env=dict(READY, STRIPE_SECRET_KEY='sk_test_DDDDDDDD'))
    out = capsys.readouterr().out
    for value in SECRETS.values():
        assert value not in out
    assert 'point(s) bloquant(s)' in out


class _Row(dict):
    pass


class _Conn:
    def __init__(self, finished):
        self.finished = finished

    def execute(self, *_a):
        rows = [_Row(finished_at=self.finished)] if self.finished else []
        return type('R', (), {'fetchone': lambda s: rows[0] if rows else None})()


@pytest.mark.parametrize('hours, level', [(3, G.OK), (25, G.OK), (30, G.FAIL)])
def test_age_de_la_derniere_sauvegarde(hours, level):
    now = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
    item = G.last_backup(_Conn((now - timedelta(hours=hours)).isoformat()), now=now)
    assert item['level'] == level


def test_aucune_sauvegarde():
    assert G.last_backup(_Conn(None))['level'] == G.FAIL


def test_connexions_reelles_simulees(monkeypatch):
    from profitos import weinvoice, swan_baas
    calls = []
    monkeypatch.setattr(weinvoice, 'is_configured', lambda cs='management': True)
    monkeypatch.setattr(weinvoice, 'fetch_access_token', lambda credential_set='management', **k: calls.append(credential_set) or 'tok')

    def refuse():
        raise ValueError("Échec de l'authentification Swan (401)")
    monkeypatch.setattr(swan_baas, 'get_server_token', refuse)
    import profitos.runtime as rt
    monkeypatch.setattr(rt, 'auth_cx', lambda: _Conn((datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()))
    _Conn.close = lambda self: None
    env = {k: v for k, v in READY.items() if k != 'STRIPE_SECRET_KEY'}
    items = G.network_checks(env)
    lv = levels(items)
    assert calls == ['management', 'invoicing']
    assert lv[('Facture électronique (WeInvoice)', 'Connexion (émission) : jeton obtenu')] == G.OK
    assert lv[('Banque (Swan / Powens)', 'Swan : authentification refusée')] == G.FAIL
    assert any(k[0] == 'Sauvegardes' and v == G.OK for k, v in lv.items())
