"""v472 — « Votre semaine » en tête du rapport du lundi : encaissements, paiements,
échéances fiscales et alertes de la veille."""
import re
from datetime import timedelta


import profitos.runtime as rt
from profitos import market_watch as MW
from test_v470_encaissement import app, flashes, issue_invoice, post, signup  # noqa: F401


def week_html(app, org, monkeypatch):
    sent = []
    monkeypatch.setattr(rt, 'send_email', lambda to, subject, html, **k: sent.append(html) or {'sent': True})
    with app.test_request_context():
        digest = rt.compute_weekly_digest(org)
        ac = rt.auth_cx()
        row = ac.execute('SELECT * FROM organizations WHERE id=?', (org,)).fetchone()
        ac.close()
        rt.send_weekly_email(dict(row), digest)
    return digest, (sent[0] if sent else '')


def test_votre_semaine(app, monkeypatch):
    cl, org = signup(app)
    today = rt.paris_today()
    late = issue_invoice(cl, 'a@example.com', price='1000')          # 1 200 € TTC
    soon = issue_invoice(cl, 'b@example.com', price='500')           # 600 € TTC
    c = rt.tenant_cx_direct(org)
    c.execute('UPDATE outgoing_invoices SET issue_date=?, due_date=? WHERE id=?',
              ((today - timedelta(days=40)).isoformat(), (today - timedelta(days=10)).isoformat(), late))
    c.execute('UPDATE outgoing_invoices SET due_date=? WHERE id=?', ((today + timedelta(days=3)).isoformat(), soon))
    c.execute("""INSERT INTO purchase_invoices(supplier_name,invoice_number,issue_date,due_date,subtotal,vat_amount,total,status,created_at)
                 VALUES('Grossiste','A-1',?,?,250,50,300,'unpaid',?)""",
              ((today - timedelta(days=20)).isoformat(), (today + timedelta(days=5)).isoformat(), rt.now()))
    c.execute("""INSERT INTO purchase_invoices(supplier_name,invoice_number,issue_date,due_date,subtotal,vat_amount,total,status,created_at)
                 VALUES('Plus tard','A-2',?,?,1000,200,1200,'unpaid',?)""",
              ((today - timedelta(days=1)).isoformat(), (today + timedelta(days=40)).isoformat(), rt.now()))
    c.commit()
    MW.save_settings(c, None, dict(MW.DEFAULT_SETTINGS, sector='batiment'), rt.now())
    c.close()
    ac = rt.auth_cx()
    MW.store(ac, 'BT01', {'title': "Index BT01", 'freq': 'M', 'last_update': None,
                          'obs': [('2025-01', 130.0), ('2025-12', 134.0), ('2026-01', 140.0)]})
    ac.close()
    digest, html = week_html(app, org, monkeypatch)
    w = digest['week']
    assert w['receivables'] == {'overdue_total': 1200.0, 'overdue_count': 1, 'soon_total': 600.0, 'soon_count': 1}
    assert w['payables'] == {'total': 300.0, 'count': 1}
    assert w['watch'] and w['watch'][0]['label'].endswith('(BT01)') and digest['has_signal']
    text = re.sub(r'<[^>]+>', '', html).replace('\u202f', ' ').replace('\xa0', ' ').replace('&nbsp;', ' ')
    assert 'Votre semaine' in text and '1 200 € de factures clients échues (1)' in text
    assert '600 € attendus sous 7 jours (1)' in text and '300 € de factures fournisseurs' in text
    assert 'Veille de votre secteur' in text and '+7,69 % sur un an' in text
    assert 'Échéances fiscales' not in text            # aucune écriture : pas de TVA « à néant » rappelée
    assert 'Aucune nouveauté' not in text


def test_semaine_calme(app, monkeypatch):
    cl, org = signup(app)
    digest, html = week_html(app, org, monkeypatch)
    assert not digest['week']['has_signal'] and digest['week']['watch'] == []      # veille jamais configurée
    assert 'Votre semaine' not in html and 'Aucune nouveauté' in html
