"""v480 — Moteur de vérification commun et Mission Control : détecter, vérifier, agir,
contrôler le résultat."""
import re
from datetime import date, timedelta

import pytest

import profitos.runtime as rt
from profitos import mission_control as MC
from profitos import verification as V
from profitos.accounting import paris_today
from test_v470_encaissement import app, flashes, issue_invoice, post, signup  # noqa: F401


def mail(monkeypatch, sent):
    import profitos.routes.invoicing as INV
    fake = lambda to, subject, html, **k: sent.append(to) or {'sent': True}  # noqa: E731
    monkeypatch.setattr(rt, 'send_email', fake)
    monkeypatch.setattr(INV, 'send_email', fake)


def cash(org, amount=50000.0):
    from profitos.entities import set_cash_balance
    c = rt.tenant_cx_direct(org)
    set_cash_balance(c, None, amount, paris_today().isoformat(), rt.now())
    c.commit(); c.close()


# --------------------------------------------------------------------- moteur
def test_niveaux_et_actions():
    ok, unk, ko = V.check('a', True), V.check('b', None), V.check('c', False)
    assert V.level([ok]) == V.READY and V.level([ok, unk]) == V.TO_CHECK and V.level([ok, unk, ko]) == V.BLOCKED
    f = V.finding('k', 'm', 'impaye', 'important', 'T', checks=[ok, unk], action={'kind': 'execute', 'op': 'x', 'label': 'Go'})
    assert f['level'] == V.TO_CHECK and not f['action_allowed'] and not V.can_execute(f)
    f = V.finding('k', 'm', 'impaye', 'important', 'T', checks=[ok], action={'kind': 'execute', 'op': 'x', 'label': 'Go'})
    assert V.can_execute(f)
    assert not V.can_execute(V.finding('k', 'm', 'x', 'info', 'T', checks=[ok], action={'kind': 'link', 'label': 'L'}))
    with pytest.raises(ValueError):
        V.finding('k', 'm', 'x', 'grave', 'T', checks=[])
    u = V.unavailable('m', 'banque', RuntimeError('x'))
    assert u['level'] == V.TO_CHECK and 'indisponible' in u['title']


def test_cycle_de_vie_du_suivi(tmp_path):
    import sqlite3
    c = sqlite3.connect(tmp_path / 't.db')
    c.row_factory = sqlite3.Row
    d0 = date(2026, 10, 1)
    mk = lambda key, kind='impaye': V.finding(key, 'm', kind, 'important', key, checks=[V.check('x', True)])  # noqa: E731
    vis = V.sync(c, None, [mk('a'), mk('b'), mk('t', 'tresorerie')], d0)
    assert [f['key'] for f in vis] == ['a', 'b', 't'] and vis[0]['track']['status'] == V.OPEN
    V.record_action(c, None, 'a', 'Relancer', 'Relance n°1 envoyée', 'u@x', d0, 7)
    # Avant la date de contrôle : en contrôle. Après, toujours présent : rouvert avec la raison.
    assert V.sync(c, None, [mk('a'), mk('b'), mk('t', 'tresorerie')], d0 + timedelta(days=3))[0]['track']['status'] == V.IN_CONTROL
    a = V.sync(c, None, [mk('a'), mk('b'), mk('t', 'tresorerie')], d0 + timedelta(days=8))[0]['track']
    assert a['status'] == V.OPEN and 'Toujours présent' in a['note'] and a['reopen_count'] == 1
    # Disparition : résolu avec la note du détecteur ; un type non contrôlé n'est pas soldé.
    V.sync(c, None, [mk('b')], d0 + timedelta(days=9), resolve_note=lambda it: f"réglé ({it['item_key']})",
           evaluated={'impaye'})
    rows = {r['item_key']: r for r in V.items(c, None)}
    assert rows['a']['status'] == V.RESOLVED and rows['a']['resolution'] == 'réglé (a)'
    assert rows['t']['status'] == V.OPEN                       # trésorerie non recalculée : rien n'est conclu
    # Réapparition après résolution : rouvert.
    assert V.sync(c, None, [mk('a'), mk('b')], d0 + timedelta(days=10), evaluated={'impaye'})[0]['track']['note'].startswith('Réapparu')
    # Écarté : masqué ; reporté : masqué jusqu'à la date ; rétabli : revient.
    V.dismiss(c, None, 'b', 'litige connu', 'u@x', d0)
    V.snooze(c, None, 'a', 7, 'u@x', d0 + timedelta(days=10))
    assert V.sync(c, None, [mk('a'), mk('b')], d0 + timedelta(days=11), evaluated={'impaye'}) == []
    assert [f['key'] for f in V.sync(c, None, [mk('a'), mk('b')], d0 + timedelta(days=18), evaluated={'impaye'})] == ['a']
    V.restore(c, None, 'b')
    assert {f['key'] for f in V.sync(c, None, [mk('a'), mk('b')], d0 + timedelta(days=18), evaluated={'impaye'})} == {'a', 'b'}


# --------------------------------------------------------------- données de test
def overdue(org, inv_id, days=20):
    c = rt.tenant_cx_direct(org)
    due = (paris_today() - timedelta(days=days)).isoformat()
    c.execute('UPDATE outgoing_invoices SET due_date=?, issue_date=? WHERE id=?',
              (due, (paris_today() - timedelta(days=days + 30)).isoformat(), inv_id))
    c.commit(); c.close()


def bank(org, synced_days_ago=0, balance=5000.0, txs=()):
    c = rt.tenant_cx_direct(org)
    when = (paris_today() - timedelta(days=synced_days_ago)).isoformat()
    c.execute("""INSERT INTO bank_accounts(provider,provider_account_id,name,balance,last_synced_at,statement_balance_date,entity_id)
                 VALUES('import','ACC1','Compte',?,?,?,NULL)""", (balance, when, when))
    for i, (amount, label, days) in enumerate(txs):
        c.execute("""INSERT INTO bank_transactions(provider,provider_transaction_id,provider_account_id,transaction_date,label,amount)
                     VALUES('import',?,'ACC1',?,?,?)""", (f'T{i}', (paris_today() - timedelta(days=days)).isoformat(), label, amount))
    c.commit(); c.close()


def page(cl):
    html = cl.get('/mission-control').get_data(as_text=True)
    return html, re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', html)).replace('&#39;', "'")


# ------------------------------------------------------------------- impayés
def test_impaye_sans_banque_a_verifier(app):
    cl, org = signup(app)
    inv = issue_invoice(cl, price='1000')
    overdue(org, inv, 40)
    html, text = page(cl)
    assert "À vérifier avant d'agir (2)" in text and 'Prévision de trésorerie incomplète' in text and 'impayée depuis 40 jour(s)' in text
    assert 'Aucun compte bancaire relié' in text and 'Critique' in text
    assert 'Envoyer la relance' not in html                 # pas d'action tant que la banque n'est pas vérifiée


def test_impaye_verifie_puis_relance_et_controle(app, monkeypatch):
    sent = []
    mail(monkeypatch, sent)
    cl, org = signup(app)
    inv = issue_invoice(cl, price='1000')
    overdue(org, inv, 20)
    cash(org)
    bank(org, 0, txs=[(250.0, 'VIR SANS RAPPORT', 3)])
    html, text = page(cl)
    assert 'Prêt à traiter (1)' in text and 'Aucun virement correspondant non rapproché' in text
    assert 'Envoyer la relance n°1' in html
    post(cl, '/mission-control/agir', {'key': f'impaye:outgoing:{inv}'})
    msg = flashes(cl)
    assert any('Relance n°1 envoyée à client@example.com' in m and 'Résultat contrôlé automatiquement' in m for m in msg), msg
    assert sent.count('client@example.com') == 1
    html, text = page(cl)
    assert 'Action faite, résultat en contrôle (1)' in text and 'Envoyer la relance' not in html
    assert 'Contrôle du résultat à partir du' in text
    # Rejouer le formulaire : refusé (le point n'est plus ouvert).
    post(cl, '/mission-control/agir', {'key': f'impaye:outgoing:{inv}'})
    assert any("n'est plus à traiter" in m for m in flashes(cl)) and sent.count('client@example.com') == 1
    # Le client paie : au contrôle suivant, résolu avec la date du règlement.
    c = rt.tenant_cx_direct(org)
    c.execute("INSERT INTO outgoing_invoice_payments(entity_id,invoice_id,amount,payment_date,created_at) VALUES(NULL,?,1200,?,?)",
              (inv, paris_today().isoformat(), rt.now()))
    c.execute("UPDATE outgoing_invoices SET status='paid', paid_at=? WHERE id=?", (rt.now(), inv))
    c.commit(); c.close()
    html, text = page(cl)
    assert 'Résolu récemment' in text and 'Facture réglée (dernier règlement enregistré le' in text
    assert 'Relance n°1 envoyée' in text


def test_virement_probable_bloque_la_relance(app, monkeypatch):
    sent = []
    mail(monkeypatch, sent)
    cl, org = signup(app)
    inv = issue_invoice(cl, price='1000')                    # 1 200 € TTC
    overdue(org, inv, 20)
    bank(org, 0, txs=[(1200.0, 'VIR CLIENT DURAND', 2)])
    html, text = page(cl)
    assert 'Un virement de 1 200,00 € du' in text.replace(' ', ' ').replace('\xa0', ' ')
    assert 'rapprochez-le avant de relancer' in text and 'Rapprocher le virement' in text
    assert 'Envoyer la relance' not in html
    # Forcer le POST : la vérification refaite côté serveur refuse.
    post(cl, '/mission-control/agir', {'key': f'impaye:outgoing:{inv}'})
    assert any('vérifications non validées' in m for m in flashes(cl)) and 'client@example.com' not in sent
    # Les relances automatiques le respectent aussi.
    post(cl, '/facturation/encaissement', {'qr': '1', 'auto_reminders': '1', 'step_1': '3', 'step_2': '15', 'step_3': '30'})
    from profitos import jobs
    ac = rt.auth_cx()
    row = dict(ac.execute('SELECT * FROM organizations WHERE id=?', (org,)).fetchone())
    ac.close()
    assert 'suspendue(s) : virement à rapprocher' in jobs.JOBS['invoice_reminders'].func(row, {})
    assert 'client@example.com' not in sent


def test_releve_ancien_et_relance_trop_recente(app, monkeypatch):
    mail(monkeypatch, [])
    cl, org = signup(app)
    inv = issue_invoice(cl)
    overdue(org, inv, 20)
    bank(org, synced_days_ago=10)
    _h, text = page(cl)
    assert 'plus de 3 jours' in text
    c = rt.tenant_cx_direct(org)
    c.execute('UPDATE bank_accounts SET last_synced_at=?, statement_balance_date=?', (paris_today().isoformat(),) * 2)
    c.execute("INSERT INTO invoice_reminders(entity_id,invoice_id,recipient_email,sent_at,reminder_number) VALUES(NULL,?,?,?,1)",
              (inv, 'client@example.com', (paris_today() - timedelta(days=2)).isoformat()))
    c.commit(); c.close()
    cash(org)
    html, text = page(cl)
    assert "Action impossible pour l'instant (1)" in text and 'Dernière relance le' in text and 'prochaine possible le' in text
    assert 'Envoyer la relance' not in html


# ---------------------------------------------------- autres constats et suivi
def test_fournisseur_tresorerie_et_suivi_manuel(app):
    cl, org = signup(app)
    c = rt.tenant_cx_direct(org)
    for i, (total, days, status) in enumerate([(800.0, 2, 'approved'), (4000.0, -3, 'approved'), (300.0, -5, 'pending')]):
        c.execute("""INSERT INTO purchase_invoices(supplier_name,invoice_number,issue_date,due_date,subtotal,vat_amount,total,status,
                     created_at,validation_status,entity_id) VALUES(?,?,?,?,?,0,?,'unpaid',?,?,NULL)""",
                  (f'Fourn {i}', f'A-{i}', paris_today().isoformat(), (paris_today() - timedelta(days=days)).isoformat(),
                   total, total, rt.now(), status))
    c.commit(); c.close()
    bank(org, 0, balance=1000.0)
    html, text = page(cl)
    assert 'Facture fournisseur A-0 — Fourn 0 : échue depuis 2 jour(s)' in text      # tri : la plus ancienne d'abord
    assert 'pour 800,00 € de paiements dus' in text.replace(' ', ' ')
    assert 'Priorisez ou négociez un délai' in text                                    # A-1 : 4 800 € cumulés > 1 000 €
    assert 'En attente de validation interne' in text
    # « J'ai traité » sur un lien : en contrôle, puis résolu quand la facture est payée.
    post(cl, '/mission-control/suivi', {'key': 'fournisseur:1', 'op': 'fait'})
    assert any('ProfitOS vérifiera le' in m for m in flashes(cl))
    c = rt.tenant_cx_direct(org)
    c.execute("UPDATE purchase_invoices SET status='paid', paid_at=? WHERE invoice_number='A-0'", (rt.now(),))
    c.commit(); c.close()
    _h, text = page(cl)
    assert 'Facture fournisseur réglée le' in text
    # Écarter exige un motif.
    post(cl, '/mission-control/suivi', {'key': 'fournisseur:3', 'op': 'ecarter', 'reason': ''})
    assert any('Indiquez pourquoi' in m for m in flashes(cl))
    post(cl, '/mission-control/suivi', {'key': 'fournisseur:3', 'op': 'ecarter', 'reason': 'Facture contestée'})
    _h, text = page(cl)
    assert 'Écartés ou reportés' in text and 'Facture contestée' in text


def test_mouvements_bancaires_et_controle_indisponible(app, monkeypatch):
    cl, org = signup(app)
    bank(org, 0, txs=[(-45.0, 'PRLV ABONNEMENT', 12), (90.0, 'REMISE', 10), (-20.0, 'CB', 2)])
    _h, text = page(cl)
    assert '2 mouvement(s) bancaire(s) non traité(s) depuis plus de 7 jours' in text
    monkeypatch.setattr(MC, 'DETECTORS', MC.DETECTORS + [('panne', 'test panne', lambda c, e, t: 1 / 0)])
    _h, text = page(cl)
    assert 'Contrôle « test panne » indisponible' in text and 'ZeroDivisionError' in text


def test_veille_suspendue_et_rien_a_signaler(app):
    cl, org = signup(app)
    cash(org)
    _h, text = page(cl)
    assert 'Rien à signaler sur les données actuelles' in text
    from profitos import market_watch as MW
    post(cl, '/veille', {'action': 'settings', 'sector': 'restauration', 'alert_12m': '5', 'alert_1p': '2'})
    ac = rt.auth_cx()
    MW.store(ac, 'CA_RESTAURATION', error='Métadonnées contradictoires : x — ' + MW.SUSPENDED)
    ac.close()
    _h, text = page(cl)
    assert "Indicateur suspendu : Chiffre d'affaires — restauration" in text


def test_acces_et_menu(app):
    cl, org = signup(app)
    assert 'Mission Control' in cl.get('/').get_data(as_text=True)
    ac = rt.auth_cx()
    ac.execute("UPDATE organizations SET status='CANCELED' WHERE id=?", (org,))
    ac.commit(); ac.close()
    r = cl.get('/mission-control')
    assert r.status_code in (302, 403)


def test_tache_de_suivi(app, monkeypatch):
    mail(monkeypatch, [])
    cl, org = signup(app)
    inv = issue_invoice(cl)
    overdue(org, inv, 20)
    from profitos import jobs
    ac = rt.auth_cx()
    row = dict(ac.execute('SELECT * FROM organizations WHERE id=?', (org,)).fetchone())
    ac.close()
    assert jobs.JOBS['mission_control_followup'].func(row, {}) == '1 point(s) suivi(s)'
    c = rt.tenant_cx_direct(org)
    assert [r['item_key'] for r in V.items(c, None)] == [f'impaye:outgoing:{inv}']
    c.close()
