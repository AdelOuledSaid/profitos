"""v485 — Expert Network : annuaire privé vérifié, demande depuis Mission Control avec
consentement, dossier figé, lien expert sécurisé, devis, livrable, résultat contrôlé."""
import re
from datetime import timedelta

import pytest

import profitos.runtime as rt
from profitos import expert_network as EN
from profitos import verification as V
from profitos.accounting import paris_today
from test_v470_encaissement import CSRF, app, flashes, issue_invoice, post, signup  # noqa: F401
from test_v480_mission_control import cash, mail, overdue  # noqa: F401
from test_v482_once_done import complete

T = paris_today()


def add_expert(domain='comptabilite', verified=True, **kw):
    ac = rt.auth_cx()
    data = dict({'name': 'Claire Martin', 'firm': 'Cabinet Martin', 'domain': domain, 'email': 'claire@cabinet.example',
                 'registration_number': '14-2026-0001', 'specialties': 'Clôture, fiscalité', 'status': 'actif'}, **kw)
    eid = EN.save_expert(ac, data, 'admin@profitos.example', verified=verified)
    ac.close()
    return eid


def text(html):
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', html)).replace('&#39;', "'").replace(' ', ' ').replace('\xa0', ' ')


def capture(monkeypatch):
    sent = []
    import profitos.routes.expert_network as R
    monkeypatch.setattr(R, 'send_email', lambda to, subject, html, **k: sent.append((to, subject, html)) or {'sent': True})
    return sent


def expert_client(app):
    cl = app.test_client()
    with cl.session_transaction() as s:
        s['csrf_token'] = CSRF
    return cl


def epost(cl, url, data):
    with cl.session_transaction() as s:
        s['csrf_token'] = CSRF
    return cl.post(url, data=dict(data, csrf_token=CSRF))


# ------------------------------------------------------------------- annuaire
def test_annuaire_et_professions_reglementees(app):
    with pytest.raises(EN.ExpertError, match="numéro d'inscription"):
        add_expert(registration_number='')
    unverified = add_expert(name='Non Vérifié', verified=False)
    ok = add_expert(name='Claire Martin')
    mkt = add_expert(domain='marketing', name='Léa Com', registration_number='', email='lea@com.example', verified=False)
    ac = rt.auth_cx()
    names = {e['name'] for e in EN.experts(ac)}
    assert names == {'Claire Martin', 'Léa Com'}                          # non vérifié : jamais proposé
    # Numéro modifié : la vérification tombe.
    EN.save_expert(ac, {**EN.expert(ac, ok), 'registration_number': '99-0000'}, 'admin@profitos.example', ok)
    assert not EN.proposable(EN.expert(ac, ok))
    EN.save_expert(ac, {**EN.expert(ac, mkt), 'status': 'suspendu'}, 'admin@profitos.example', mkt)
    assert EN.experts(ac) == []
    ac.close()
    assert unverified


def test_administration_reservee(app, monkeypatch):
    cl, org = signup(app)
    assert cl.get('/admin/experts').status_code == 403
    ac = rt.auth_cx()
    email = ac.execute('SELECT email FROM users ORDER BY id DESC LIMIT 1').fetchone()['email']
    ac.close()
    monkeypatch.setenv('PLATFORM_ADMIN_EMAILS', f'autre@x.example, {email}')
    assert cl.get('/admin/experts').status_code == 200
    post(cl, '/admin/experts', {'name': 'Maître Durand', 'domain': 'juridique', 'email': 'durand@avocat.example',
                                'registration_number': 'Barreau de Lyon', 'verified': '1', 'status': 'actif'})
    assert any('Expert enregistré' in m for m in flashes(cl))
    t = text(cl.get('/admin/experts').get_data(as_text=True))
    assert 'Maître Durand' in t and 'Barreau de Lyon ✓' in t
    t = text(cl.get('/experts').get_data(as_text=True))
    assert 'Maître Durand' in t and 'Barreau : n° Barreau de Lyon — inscription vérifiée par ProfitOS le' in t
    assert 'ProfitOS ne perçoit aucune commission' in t


# ------------------------------------------------------- mission de bout en bout
def test_mission_depuis_mission_control(app, monkeypatch):
    sent = capture(monkeypatch)
    mail(monkeypatch, [])
    expert_id = add_expert(domain='juridique', name='Maître Durand', email='durand@avocat.example',
                           registration_number='Barreau de Lyon')
    cl, org = signup(app)
    complete(cl)
    inv = issue_invoice(cl, price='4000')
    overdue(org, inv, 40)
    cash(org)
    key = f'impaye:outgoing:{inv}'
    mc = cl.get('/mission-control').get_data(as_text=True)
    assert f'/experts/demande?source=impaye:outgoing:{inv}&amp;domaine=juridique' in mc and 'Faire appel à un expert' in mc
    page = text(cl.get(f'/experts/demande?source={key}&domaine=juridique').get_data(as_text=True))
    assert 'Constat à l\'origine de la demande' in page and 'impayée depuis 40 jour(s)' in page and 'Maître Durand' in page
    # Sans consentement : rien n'est envoyé.
    form = {'source': key, 'domaine': 'juridique', 'expert_id': str(expert_id), 'title': 'Recouvrement facture',
            'request': 'Mise en demeure puis injonction de payer si besoin.', 'share': ['identite', 'constat']}
    post(cl, '/experts/demande', form)
    assert any("Cochez l'autorisation" in m for m in flashes(cl)) and sent == []
    r = post(cl, '/experts/demande', dict(form, consent='1'))
    mission_id = int(r.headers['Location'].rsplit('/', 1)[1])
    assert any('Demande envoyée à Maître Durand' in m for m in flashes(cl))
    to, subject, html = sent[-1]
    assert to == 'durand@avocat.example' and 'Demande de mission' in subject
    link = re.search(r'href="(https?://[^"]+/expert/mission/[^"]+)"', html).group(1)
    token = link.rsplit('/', 1)[1]
    # Le lien n'est jamais stocké en clair.
    ac = rt.auth_cx()
    stored = ac.execute('SELECT token_hash FROM expert_mission_links').fetchone()['token_hash']
    ac.close()
    assert token not in stored and len(stored) == 64
    # Côté expert : dossier figé, limité aux éléments autorisés.
    ex = expert_client(app)
    t = text(ex.get(f'/expert/mission/{token}').get_data(as_text=True))
    assert 'Recouvrement facture' in t and 'Constat vérifié par ProfitOS' in t and 'impayée depuis 40 jour(s)' in t
    assert 'SIREN : 732829320' in t and 'IBAN' not in t                    # pas de coordonnées bancaires transmises
    assert ex.get('/expert/mission/faux-jeton').status_code == 404
    # Devis.
    epost(ex, f'/expert/mission/{token}', {'op': 'quote', 'amount': '0', 'delay_days': '10', 'text': 'x'})
    epost(ex, f'/expert/mission/{token}', {'op': 'quote', 'amount': '450,00', 'delay_days': '10',
                                           'text': 'Mise en demeure et suivi.'})
    assert sent[-1][0] == org_email(org) and 'devis' in text(sent[-1][2])
    t = text(cl.get(f'/experts/missions/{mission_id}').get_data(as_text=True))
    assert '450,00 € HT — réalisation en 10 jour(s)' in t and 'Accepter le devis' in t
    # Acceptation : le constat d'origine passe « en contrôle », pas résolu.
    post(cl, f'/experts/missions/{mission_id}/action', {'op': 'accept'})
    c = rt.tenant_cx_direct(org)
    item = [r for r in V.items(c, None) if r['item_key'] == key][0]
    c.close()
    assert item['status'] == V.IN_CONTROL and 'Maître Durand mandaté' in item['action_result']
    assert sent[-1][0] == 'durand@avocat.example' and 'Devis accepté' in sent[-1][1]
    # Livrable (lien https uniquement), validation, évaluation.
    epost(ex, f'/expert/mission/{token}', {'op': 'deliver', 'text': 'Mise en demeure envoyée.', 'url': 'javascript:alert(1)'})
    epost(ex, f'/expert/mission/{token}', {'op': 'deliver', 'text': 'Mise en demeure envoyée le 2 novembre.',
                                           'url': 'https://docs.example/med.pdf'})
    t = text(cl.get(f'/experts/missions/{mission_id}').get_data(as_text=True))
    assert 'Livrable remis, à valider' in t and 'Mise en demeure envoyée le 2 novembre' in t and 'javascript' not in t
    assert "Le constat est toujours détecté dans les données" in t
    post(cl, f'/experts/missions/{mission_id}/action', {'op': 'complete', 'rating': '5', 'note': 'Réactif.'})
    # Mission close : le lien ne fonctionne plus, l'évaluation est enregistrée.
    assert ex.get(f'/expert/mission/{token}').status_code == 404
    ac = rt.auth_cx()
    assert EN.experts(ac)[0]['rating_avg'] == 5.0
    ac.close()
    # Le client paie : le constat disparaît, le résultat est confirmé sur la mission.
    c = rt.tenant_cx_direct(org)
    c.execute("INSERT INTO outgoing_invoice_payments(entity_id,invoice_id,amount,payment_date,created_at) VALUES(NULL,?,4800,?,?)",
              (inv, T.isoformat(), rt.now()))
    c.execute("UPDATE outgoing_invoices SET status='paid' WHERE id=?", (inv,))
    c.commit(); c.close()
    cl.get('/mission-control')
    t = text(cl.get(f'/experts/missions/{mission_id}').get_data(as_text=True))
    assert 'Résultat contrôlé : Constat résolu le' in t and 'Facture réglée' in t


def org_email(org):
    ac = rt.auth_cx()
    e = ac.execute('SELECT u.email FROM users u JOIN memberships m ON m.user_id=u.id WHERE m.organization_id=? LIMIT 1',
                   (org,)).fetchone()['email']
    ac.close()
    return e


def test_annulation_isolement_et_expiration(app, monkeypatch):
    sent = capture(monkeypatch)
    expert_id = add_expert(domain='marketing', name='Léa Com', registration_number='', email='lea@com.example')
    cl, org = signup(app)
    r = post(cl, '/experts/demande', {'domaine': 'marketing', 'expert_id': str(expert_id), 'title': 'Relance clients dormants',
                                      'request': 'Campagne de réactivation.', 'consent': '1'})
    mission_id = int(r.headers['Location'].rsplit('/', 1)[1])
    token = re.search(r'/expert/mission/([^"]+)"', sent[-1][2]).group(1)
    ex = expert_client(app)
    t = text(ex.get(f'/expert/mission/{token}').get_data(as_text=True))
    assert 'aucun élément du compte, seulement la demande' in t and 'SIREN' not in t
    # Une autre entreprise ne voit pas la mission.
    cl2, _o2 = signup(app)
    assert cl2.get(f'/experts/missions/{mission_id}').status_code == 404
    # Expiration.
    ac = rt.auth_cx()
    ac.execute('UPDATE expert_mission_links SET expires_at=?', ((T - timedelta(days=1)).isoformat(),))
    ac.commit(); ac.close()
    assert ex.get(f'/expert/mission/{token}').status_code == 404
    ac = rt.auth_cx()
    ac.execute('UPDATE expert_mission_links SET expires_at=?', ((T + timedelta(days=30)).isoformat(),))
    ac.commit(); ac.close()
    # Annulation : lien désactivé, messages impossibles.
    post(cl, f'/experts/missions/{mission_id}/action', {'op': 'cancel'})
    assert any('lien de l\'expert est désactivé' in m for m in flashes(cl))
    assert ex.get(f'/expert/mission/{token}').status_code == 404
    post(cl, f'/experts/missions/{mission_id}/action', {'op': 'message', 'text': 'encore ?'})
    assert any('close' in m for m in flashes(cl))


def test_transitions_interdites_et_lecture_seule(app, monkeypatch):
    capture(monkeypatch)
    expert_id = add_expert(domain='tresorerie', name='Paul Finance', registration_number='', email='paul@fi.example')
    cl, org = signup(app)
    r = post(cl, '/experts/demande', {'domaine': 'tresorerie', 'expert_id': str(expert_id), 'title': 'Prévisionnel',
                                      'request': 'Plan de trésorerie 12 mois.', 'consent': '1'})
    mission_id = int(r.headers['Location'].rsplit('/', 1)[1])
    post(cl, f'/experts/missions/{mission_id}/action', {'op': 'accept'})          # pas de devis : refusé
    assert any('Action impossible' in m for m in flashes(cl))
    post(cl, f'/experts/missions/{mission_id}/action', {'op': 'complete'})
    assert any('Action impossible' in m for m in flashes(cl))
    # Expert suspendu : plus proposé dans une nouvelle demande.
    ac = rt.auth_cx()
    EN.save_expert(ac, {**EN.expert(ac, expert_id), 'status': 'suspendu'}, 'admin@profitos.example', expert_id)
    ac.close()
    post(cl, '/experts/demande', {'domaine': 'tresorerie', 'expert_id': str(expert_id), 'title': 'x', 'request': 'y', 'consent': '1'})
    assert any("n'est pas disponible" in m for m in flashes(cl))


def test_montants_au_format_francais(app, monkeypatch):
    sent = capture(monkeypatch)
    expert_id = add_expert(domain='marketing', name='Léa Com', registration_number='', email='lea@com.example')
    cl, org = signup(app)
    r = post(cl, '/experts/demande', {'domaine': 'marketing', 'expert_id': str(expert_id), 'title': 'Campagne',
                                      'request': 'Réactivation.', 'consent': '1'})
    mission_id = int(r.headers['Location'].rsplit('/', 1)[1])
    token = re.search(r'/expert/mission/([^"]+)"', sent[-1][2]).group(1)
    epost(expert_client(app), f'/expert/mission/{token}', {'op': 'quote', 'amount': '1 250,50', 'delay_days': '5', 'text': 'Plan.'})
    post(cl, f'/experts/missions/{mission_id}/action', {'op': 'accept'})
    t = text(cl.get(f'/experts/missions/{mission_id}').get_data(as_text=True))
    assert 'Devis : 1 250,50 € HT, réalisation en 5 jour(s)' in t and '(1 250,50 € HT)' in t and '1250.50' not in t
