"""v488 — Copilote : faits calculés et sourcés, rédaction par l'IA contrôlée (aucun chiffre
inventé), rapport calculé sans IA, problèmes issus de Mission Control."""
import re
from datetime import timedelta

import profitos.runtime as rt
from profitos import copilot as CP
from profitos.accounting import paris_today
from test_v470_encaissement import app, flashes, issue_invoice, post, signup  # noqa: F401
from test_v480_mission_control import bank, cash, overdue  # noqa: F401

T = paris_today()


def text(html):
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', html)).replace('&#39;', "'").replace(' ', ' ').replace('\xa0', ' ')


def setup(app):
    cl, org = signup(app)
    inv = issue_invoice(cl, price='4000')              # 4 800 € TTC, 4 000 € HT
    overdue(org, inv, 40)
    issue_invoice(cl, price='1500')                    # émise aujourd'hui : 1 500 € HT ce mois-ci
    cash(org, 20000.0)
    bank(org, 0, balance=12000.0)
    return cl, org


def dossier(org):
    c = rt.tenant_cx_direct(org)
    d = CP.build(c, None, T, accounting=True)
    c.close()
    return d


def fact(d, label_start):
    return next(f for f in d['facts'] if f['label'].startswith(label_start))


def test_faits_calcules_et_sources(app):
    cl, org = setup(app)
    d = dossier(org)
    assert fact(d, "Chiffre d'affaires facturé HT — ")['raw'] in (1500.0, 5500.0)   # selon le mois de la facture antidatée
    assert fact(d, 'Factures clients échues non réglées')['raw'] == 4800.0
    assert fact(d, 'Nombre de factures clients échues')['raw'] == 1
    assert '40 jours de retard' in fact(d, 'Plus ancien impayé')['value']
    assert fact(d, 'Solde bancaire connu')['raw'] == 12000.0
    for f in d['facts']:
        assert f['id'] and f['source'] and f['calc'] is not None


def test_garde_fou_des_chiffres(app):
    cl, org = setup(app)
    d = dossier(org)
    unpaid = fact(d, 'Factures clients échues non réglées')
    bank_f = fact(d, 'Solde bancaire connu')
    good = (f"Vous avez 4 800,00 € d'impayés [{unpaid['id']}] et un solde de 12 000,00 € [{bank_f['id']}] au "
            f"{T.strftime('%d/%m/%Y')}. Relancez en priorité.")
    assert CP.check_answer(good, d) == (True, [])
    ok, why = CP.check_answer(f"Vous avez 7 300 € d'impayés [{unpaid['id']}].", d)
    assert not ok and 'absent(s) des faits : 7 300' in why[0]
    ok, why = CP.check_answer("Votre solde est de 12 000,00 € [F99].", d)
    assert not ok and 'F99' in why[0]
    ok, why = CP.check_answer("Votre solde est de 12 000,00 €.", d)
    assert not ok and 'sans référence' in why[0]
    assert CP.check_answer("Tout va bien, aucune action urgente.", d)[0]


def test_page_sans_ia_rapport_calcule(app, monkeypatch):
    monkeypatch.setattr(rt, 'ANTHROPIC_API_KEY', None)
    cl, org = setup(app)
    t = text(cl.get('/copilote').get_data(as_text=True))
    assert 'Synthèse du mois' in t and 'Faits et calculs' in t
    assert "4 800,00 € de factures échues non réglées" in t and 'Problèmes à résoudre' in t
    assert 'impayée depuis 40 jour(s)' in t
    t = text(post(cl, '/copilote', {'question': CP.SYSTEM[:20]}).get_data(as_text=True))
    assert "Rédaction par l'IA indisponible (L'IA n'est pas configurée sur ce serveur.)" in t


def test_reponse_ia_verifiee_ou_ecartee(app, monkeypatch):
    monkeypatch.setattr(rt, 'ANTHROPIC_API_KEY', 'test-key')
    cl, org = setup(app)
    d = dossier(org)
    unpaid = fact(d, 'Factures clients échues non réglées')
    sent = []

    def fake_http(payload, headers):
        sent.append(payload)
        return 200, {'content': [{'type': 'text', 'text': f"Priorité : 4 800,00 € d'impayés [{unpaid['id']}]."}]}
    real = CP.ask_ai
    monkeypatch.setattr(CP, 'ask_ai', lambda q, dos: real(q, dos, http=fake_http))
    t = text(post(cl, '/copilote', {'question': 'Que dois-je faire ?'}).get_data(as_text=True))
    assert "Priorité : 4 800,00 € d'impayés" in t and 'chaque chiffre a été vérifié' in t
    content = sent[0]['messages'][0]['content']
    assert 'Faits :' in content and 'Que dois-je faire ?' in content and 'IBAN' not in content
    assert "n'invente aucun chiffre" in sent[0]['system']
    # Réponse avec un chiffre inventé : écartée, rapport calculé affiché.
    monkeypatch.setattr(CP, 'ask_ai', lambda q, dos: f"Vous perdez 15 000 € par mois [{unpaid['id']}].")
    t = text(post(cl, '/copilote', {'question': 'Combien je perds ?'}).get_data(as_text=True))
    assert "Réponse de l'IA écartée par le contrôle des chiffres" in t and '15 000' in t and 'Synthèse du mois' in t
    assert 'Vous perdez' not in t


def test_quota_ia_atteint(app, monkeypatch):
    monkeypatch.setattr(rt, 'ANTHROPIC_API_KEY', 'test-key')
    cl, org = setup(app)
    import profitos.plan_usage as PU

    def no_quota(*a, **k):
        raise ValueError('quota IA mensuel atteint')
    monkeypatch.setattr(PU, 'consume_ai_extraction_quota', no_quota)
    t = text(post(cl, '/copilote', {'question': 'Analyse du mois'}).get_data(as_text=True))
    assert 'quota IA mensuel atteint' in t and 'Synthèse du mois' in t
