"""Emails envoyés au nom d'une organisation + erreurs génériques + logs masqués."""
import logging
import sys
import types
from pathlib import Path

import pytest

import profitos.runtime as rt
from profitos import create_app
from profitos.config import DevelopmentConfig

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, 'AUTH_DB', tmp_path / 'auth.db')
    monkeypatch.setattr(rt, 'TENANTS', tmp_path)
    rt.init_auth_db()
    ac = rt.auth_cx()
    ac.execute("INSERT INTO organizations(id,name,plan,status,created_at) VALUES(77,'Menuiserie Dupont','PRO','ACTIVE_PAID',?)", (rt.now(),))
    ac.execute("INSERT INTO users(id,email,password_hash,created_at) VALUES(5,'patron@dupont.fr','x',?)", (rt.now(),))
    ac.execute("INSERT INTO memberships(user_id,organization_id,role,created_at) VALUES(5,77,'OWNER',?)", (rt.now(),))
    ac.commit(); ac.close()
    rt.migrate_tenant(77, force=True)
    tc = rt.tenant_cx_direct(77)
    tc.execute("INSERT INTO company(id,name,address,postal_code,city,siret) VALUES(1,'Dupont SARL','3 rue des Lilas','69001','Lyon','12345678900011')")
    tc.commit(); tc.close()
    app = create_app(DevelopmentConfig)
    app.config.update(TESTING=True)
    return app


class FakeResend:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail
        self.api_key = None
        outer = self

        class Emails:
            SendParams = dict

            @staticmethod
            def send(params):
                if outer.fail:
                    raise RuntimeError('API error for client@exemple.fr')
                outer.sent.append(params)
                return {'id': 'em_1'}
        self.Emails = Emails


def _install(monkeypatch, fake):
    mod = types.ModuleType('resend')
    mod.Emails = fake.Emails
    monkeypatch.setitem(sys.modules, 'resend', mod)
    monkeypatch.setenv('RESEND_API_KEY', 're_test')
    monkeypatch.setenv('RESEND_FROM_EMAIL', 'ProfitOS <noreply@profitos.fr>')


def test_mask_email():
    assert rt.mask_email('alice@exemple.fr') == 'a***@exemple.fr'
    assert rt.mask_email('') == '***'


def test_on_behalf_of_org_sets_sender_reply_to_and_footer(env, monkeypatch):
    fake = FakeResend()
    _install(monkeypatch, fake)
    with env.test_request_context('/'):
        org = {'id': 77, 'name': 'Menuiserie Dupont'}
        r = rt.send_email('client@exemple.fr', 'Facture FA-1', '<html><body><p>Bonjour</p></body></html>', on_behalf_of_org=org)
    assert r['sent']
    params = fake.sent[0]
    assert params['from'] == 'Menuiserie Dupont via ProfitOS <noreply@profitos.fr>'
    assert params['reply_to'] == 'patron@dupont.fr'
    assert 'Dupont SARL' in params['html'] and '3 rue des Lilas 69001 Lyon' in params['html']
    assert 'SIRET 12345678900011' in params['html']
    assert params['html'].index('Dupont SARL') < params['html'].index('</body>')


def test_explicit_reply_to_wins_and_plain_sends_unchanged(env, monkeypatch):
    fake = FakeResend()
    _install(monkeypatch, fake)
    with env.test_request_context('/'):
        rt.send_email('x@y.fr', 's', '<p>a</p>', reply_to='compta@dupont.fr', on_behalf_of_org=77)
        rt.send_email('x@y.fr', 's', '<p>a</p>')
    assert fake.sent[0]['reply_to'] == 'compta@dupont.fr'
    assert fake.sent[0]['from'].startswith('Menuiserie Dupont via ProfitOS')
    assert fake.sent[1]['from'] == 'ProfitOS <noreply@profitos.fr>'
    assert 'reply_to' not in fake.sent[1]


def test_failure_returns_generic_french_message_and_masks_logs(env, monkeypatch, caplog):
    _install(monkeypatch, FakeResend(fail=True))
    with env.test_request_context('/'), caplog.at_level(logging.INFO):
        r = rt.send_email('client@exemple.fr', 's', '<p>a</p>')
    assert not r['sent']
    assert r['error'] == rt.EMAIL_SEND_FAILED_MESSAGE
    assert 'client@exemple.fr' not in r['error']
    assert 'client@exemple.fr' not in caplog.text
    assert 'c***@exemple.fr' in caplog.text


def test_simulated_send_is_logged_as_ops_warning(env, monkeypatch, caplog):
    monkeypatch.delenv('RESEND_API_KEY', raising=False)
    monkeypatch.delenv('SMTP_HOST', raising=False)
    with env.test_request_context('/'), caplog.at_level(logging.INFO):
        r = rt.send_email('client@exemple.fr', 'Réinitialisation', '<p>a</p>')
    assert r['dry_run']
    assert 'EMAIL_SIMULATED' in caplog.text and 'c***@exemple.fr' in caplog.text


def test_customer_facing_call_sites_send_on_behalf_of_org():
    inv = (ROOT / 'profitos/routes/invoicing.py').read_text(encoding='utf-8')
    act = (ROOT / 'profitos/routes/actions.py').read_text(encoding='utf-8')
    assert inv.count('html,on_behalf_of_org=org)') == 3
    assert 'on_behalf_of_org=current_org()' in act
