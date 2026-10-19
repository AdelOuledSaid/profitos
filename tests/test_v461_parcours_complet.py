"""v461 — Parcours fiscal complet, comme un vrai client, dans un vrai navigateur (Chromium,
Playwright) et sur PostgreSQL : inscription, profil, écritures, liquidation de TVA,
préparation de deux CA3, récapitulatif PDF, dépôt par l'entreprise, archivage et contrôle
de l'accusé, confirmation, puis relances de la tâche planifiée (retard, écarts, échéance à
venir) avec de vraies dates simulées.

Test lourd, ignoré par défaut (et en CI). Pour le lancer :

    PROFITOS_E2E_DATABASE_URL="postgresql://user@/base_vide?host=/tmp&port=5432" pytest tests/test_v461_parcours_complet.py

La base doit être vide (elle est remplie par le test) ; Chromium doit être installé pour
Playwright. Voir docs/validation-fiscale.md, § 6.
"""
import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

PG_URL = os.environ.get('PROFITOS_E2E_DATABASE_URL', '')
pytestmark = pytest.mark.skipif(not PG_URL.startswith(('postgres://', 'postgresql://')),
                                reason='parcours navigateur + PostgreSQL : définir PROFITOS_E2E_DATABASE_URL')

ROOT = Path(__file__).resolve().parents[1]
SIRET = '73282932000074'
SIREN_SPACED = '732 829 320'
PASSWORD = 'Brume-Cactus-Velo-48'

JOB_HARNESS = r'''
import json, sys
from datetime import date
today = date.fromisoformat(sys.argv[1])
import profitos.accounting as acc
acc.paris_today = lambda: today
from profitos import create_app, jobs, runtime
sent = []
runtime.send_email = lambda to, subject, html, **k: sent.append({'to': to, 'subject': subject, 'html': html}) or {'sent': True}
app = create_app()
report = jobs.run_job(app, 'fiscal_deadlines', force=True)
json.dump({'sent': sent, 'failed': {str(k): str(v) for k, v in (report.get('failed') or {}).items()}}, open(sys.argv[2], 'w'))
'''


def _free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def _env(tmp):
    env = {k: v for k, v in os.environ.items()
           if k not in ('EDI_PARTNER_ENABLED', 'EDI_SIMULATION_ENABLED', 'FISCAL_DECLARATIONS_ENABLED',
                        'FISCAL_DECLARATIONS_PILOT_ORGS', 'RESEND_API_KEY', 'SMTP_HOST', 'REDIS_URL')}
    env.update({'DATABASE_URL': PG_URL, 'PROFITOS_ENV': 'development', 'PROFITOS_SECRET_KEY': uuid.uuid4().hex * 2,
                'PROFITOS_UPLOAD_DIR': str(tmp / 'uploads'), 'APP_BASE_URL': 'http://127.0.0.1'})
    return env


@pytest.fixture(scope='module')
def server():
    tmp = Path(tempfile.mkdtemp(prefix='profitos-e2e-'))
    port = _free_port()
    env = _env(tmp)
    log = open(tmp / 'server.log', 'w')
    proc = subprocess.Popen([sys.executable, '-c',
                             "from profitos import create_app; create_app().run(host='127.0.0.1', port=%d, use_reloader=False)" % port],
                            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    import urllib.request
    base = f'http://127.0.0.1:{port}'
    for _ in range(120):
        try:
            if urllib.request.urlopen(base + '/healthz', timeout=1).status == 200:
                break
        except Exception:
            time.sleep(0.5)
    else:
        proc.terminate()
        pytest.fail('serveur non démarré : ' + (tmp / 'server.log').read_text()[-2000:])
    yield {'base': base, 'tmp': tmp, 'env': env}
    proc.terminate()
    proc.wait(10)


def pg():
    psycopg2 = pytest.importorskip('psycopg2')
    conn = psycopg2.connect(PG_URL)
    conn.autocommit = True
    return conn


def run_reminders(server, today, to):
    out = server['tmp'] / f'job-{today}.json'
    script = server['tmp'] / 'job_harness.py'
    script.write_text(JOB_HARNESS)
    res = subprocess.run([sys.executable, str(script), today.isoformat(), str(out)], cwd=ROOT, env=dict(server['env'], PYTHONPATH=os.pathsep.join([str(ROOT), server['env'].get('PYTHONPATH', '')])),
                         capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, res.stderr[-3000:]
    data = json.loads(out.read_text())
    assert not data['failed'], data['failed']
    return [m for m in data['sent'] if m['to'] == to]          # autres organisations de la base ignorées


def receipt_pdf(lines):
    from fpdf import FPDF
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font('Helvetica', '', 11)
    for line in lines:
        pdf.cell(0, 8, line, new_x='LMARGIN', new_y='NEXT')
    return bytes(pdf.output())


def fr(amount):
    return f"{amount:,.2f}".replace(',', ' ').replace('.', ',')


def month_bounds(first):
    nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return first, nxt - timedelta(days=1)


def submit(page, form, extra=''):
    """Clique le bouton d'envoi du formulaire et attend la page suivante."""
    page.locator(f'{form} button:not([type=button]){extra}').first.click()
    page.wait_for_load_state('networkidle')


def pdf_text(data):
    from pypdf import PdfReader
    return '\n'.join(p.extract_text() or '' for p in PdfReader(io.BytesIO(data)).pages)


def test_parcours_complet_navigateur_postgresql(server):
    sync_api = pytest.importorskip('playwright.sync_api')
    base = server['base']
    today = datetime.now(ZoneInfo('Europe/Paris')).date()
    m1_start, m1_end = month_bounds((today.replace(day=1) - timedelta(days=1)).replace(day=1))      # mois précédent
    m2_start, m2_end = month_bounds((m1_start - timedelta(days=1)).replace(day=1))                    # le mois d'avant
    due_m2 = date(m1_start.year, m1_start.month, 15)
    email = f'e2e-{uuid.uuid4().hex[:8]}@example.com'
    console_errors, server_errors = [], []

    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(accept_downloads=True, locale='fr-FR')
        page.on('console', lambda m: m.type == 'error' and console_errors.append(f'{page.url}: {m.text}'))
        page.on('response', lambda r: r.status >= 500 and server_errors.append(f'{r.status} {r.url}'))
        page.on('dialog', lambda d: d.accept())                          # confirmations « Êtes-vous sûr ? »

        # 1. Inscription
        page.goto(base + '/signup')
        page.fill('input[name=first_name]', 'Alice')
        page.fill('input[name=last_name]', 'Martin')
        page.fill('input[name=email]', email)
        page.fill('input[name=company_name]', 'Exemple SAS')
        page.fill('input[name=password]', PASSWORD)
        page.check('input[name=accept_terms]')
        submit(page, 'form:has(input[name=accept_terms])')
        # Adresse vérifiée et abonnement payé (hors périmètre : lien de l'e-mail et Stripe).
        conn = pg()
        cur = conn.cursor()
        cur.execute('UPDATE public.users SET email_verified=1, ui_mode=%s WHERE email=%s RETURNING id', ('expert', email))
        uid = cur.fetchone()[0]
        cur.execute('SELECT organization_id FROM public.memberships WHERE user_id=%s', (uid,))
        org_id = cur.fetchone()[0]
        cur.execute("UPDATE public.organizations SET plan='MULTI', status='ACTIVE_PAID' WHERE id=%s", (org_id,))

        # 2. Profil de l'entreprise et profil fiscal
        page.goto(base + '/company')
        page.fill('input[name=name]', 'Exemple SAS')
        page.fill('input[name=siret]', SIRET)
        page.fill('input[name=postal_code]', '75001')
        page.fill('input[name=city]', 'Paris')
        submit(page, 'form:has(input[name=siret])')
        page.goto(base + '/comptabilite/profil-fiscal')
        page.select_option('select[name=tax_regime]', 'normal')
        page.select_option('select[name=vat_filing]', 'ca3_mensuelle')
        page.fill('input[name=signataire]', 'Alice Martin')
        submit(page, 'form:has(select[name=vat_filing])')

        # 3. Écritures du mois M-2 et M-1 (saisie manuelle)
        def entry(day, journal, label, lines):
            page.goto(base + '/comptabilite/ecritures/nouvelle')
            page.select_option('select[name=journal]', journal)
            page.fill('input[name=entry_date]', day.isoformat())
            page.fill('input[name=label]', label)
            for i, (account, debit, credit) in enumerate(lines):
                page.fill(f'input[name=compte_{i}]', account)
                if debit:
                    page.fill(f'input[name=debit_{i}]', str(debit).replace('.', ','))
                if credit:
                    page.fill(f'input[name=credit_{i}]', str(credit).replace('.', ','))
            submit(page, 'form:has(input[name=compte_0])', ':not([name=next])')
            assert 'Écriture enregistrée' in page.content(), page.content()[-1500:]

        entry(m2_start + timedelta(days=9), 'VE', 'Ventes M-2', [('411000', 12000, 0), ('706000', 0, 10000), ('445710', 0, 2000)])
        entry(m1_start + timedelta(days=9), 'VE', 'Ventes M-1', [('411000', 24000, 0), ('706000', 0, 20000), ('445710', 0, 4000)])
        entry(m1_start + timedelta(days=11), 'AC', 'Achats M-1', [('607000', 5000, 0), ('445660', 1000, 0), ('401000', 0, 6000)])

        # 4. CA3 et liquidation de la TVA de chaque mois
        for start, end, due in ((m2_start, m2_end, 2000), (m1_start, m1_end, 3000)):
            page.goto(f'{base}/comptabilite/tva/ca3?date_from={start}&date_to={end}')
            text = page.inner_text('main')
            assert re.search(r'%d[\s\u00a0\u202f]000' % (due // 1000), text), text[:3000]
            submit(page, 'form[action$="/liquider"]')
        cur.execute(f'SELECT COALESCE(SUM(credit-debit),0) FROM org_{org_id}.accounting_entry_lines WHERE account_code=%s', ('445510',))
        assert float(cur.fetchone()[0]) == 5000                          # 2 000 + 3 000 à décaisser

        # 5. Tâche planifiée : la CA3 de M-2 est en retard depuis la veille.
        sent = run_reminders(server, due_m2 + timedelta(days=1), email)
        assert sent and sent[0]['to'] == email and 'Déclarations en retard' in sent[0]['subject']
        assert '3310-CA3' in sent[0]['html'] and 'EN RETARD' in sent[0]['html']

        # 6. Préparation de la CA3 de M-2 depuis la liste (« Autre déclaration »)
        page.goto(base + '/comptabilite/teledeclarations')
        page.select_option('select[name=type]', 'TVA_CA3')
        page.fill('form[method=get]:has(select[name=type]) input[name=du]', m2_start.isoformat())
        page.fill('form[method=get]:has(select[name=type]) input[name=au]', m2_end.isoformat())
        submit(page, 'form[method=get]:has(select[name=type])')
        assert 'impots.gouv.fr' in page.content()
        submit(page, 'form[action$="/preparer-depot"]')
        assert 'Déclaration préparée' in page.content()
        m2_url = page.url
        assert re.search(r'/teledeclarations/\d+$', m2_url)

        # 7. Récapitulatif PDF
        with page.expect_download() as dl:
            page.click('a:has-text("Télécharger le récapitulatif PDF")')
        data = Path(dl.value.path()).read_bytes()
        assert data[:4] == b'%PDF'
        text = pdf_text(data)
        assert '3310' in text and ('732829320' in text.replace(' ', '')) and '2' in text

        # 8. Dépôt sur impots.gouv.fr (hors ProfitOS) puis accusé avec un montant erroné
        bad = receipt_pdf(['Accuse de reception de votre declaration', 'Declaration de TVA 3310-CA3',
                           f'SIREN : {SIREN_SPACED}', f'Periode du {m2_start:%d/%m/%Y} au {m2_end:%d/%m/%Y}',
                           'Montant a payer : 2 500,00 EUR'])
        page.set_input_files('input[name=receipt]', files=[{'name': 'accuse-m2.pdf', 'mimeType': 'application/pdf', 'buffer': bad}])
        page.fill('input[name=filed_on]', today.isoformat())
        submit(page, 'form[action$="/justificatif"]')
        html = page.content()
        assert 'Écarts entre l' in html and 'Justificatif à vérifier' in html and 'Accept' not in html.split('Aucun de ces statuts')[0]

        # 9. Relance « écarts » le lendemain du dépôt
        sent = run_reminders(server, today + timedelta(days=1), email)
        assert any('ÉCARTS' in s['html'] for s in sent)

        # 10. Confirmation après vérification, avec explication obligatoire
        page.check('input[name=verified]')
        page.fill('input[name=note]', 'Pénalité de 500 € payée en même temps, montant déclaré correct.')
        submit(page, 'form[action$="/confirmer"]')
        html = page.content()
        assert 'Confirmé manuellement' in html and 'Dépôt confirmé' in html

        # 11. CA3 de M-1 depuis la liste, accusé concordant : confirmée par preuve officielle
        page.goto(base + '/comptabilite/teledeclarations')
        page.click(f'a[href*="type=TVA_CA3"][href*="du={m1_start}"]')
        page.wait_for_load_state('networkidle')
        submit(page, 'form[action$="/preparer-depot"]')
        good = receipt_pdf(['Accuse de reception de votre declaration', 'Declaration de TVA 3310-CA3',
                            f'SIREN : {SIREN_SPACED}', f'Periode du {m1_start:%d/%m/%Y} au {m1_end:%d/%m/%Y}',
                            'Montant a payer : 3 000,00 EUR'])
        page.set_input_files('input[name=receipt]', files=[{'name': 'accuse-m1.pdf', 'mimeType': 'application/pdf', 'buffer': good}])
        page.fill('input[name=filed_on]', today.isoformat())
        submit(page, 'form[action$="/justificatif"]')
        html = page.content()
        assert 'Justificatif concordant' in html and 'Confirmé manuellement' not in html
        assert 'Dépôt confirmé' in html

        # 12. Plus de relance pour les déclarations justifiées ; rappel de l'échéance à venir
        sent = run_reminders(server, today + timedelta(days=7), email)
        assert not any('ÉCARTS' in s['html'] for s in sent)
        for day in (due_m2 + timedelta(days=7), date(today.year, today.month, 15) + timedelta(days=1)):
            assert not any('EN RETARD' in s['html'] for s in run_reminders(server, day, email)), day
        nxt_due = (today.replace(day=28) + timedelta(days=4)).replace(day=15)
        upcoming = run_reminders(server, nxt_due - timedelta(days=7), email)
        assert any('dans 7 jours' in s['html'] and '3310-CA3' in s['html'] for s in upcoming)

        # 13. Liste et base : statuts attendus, aucune acceptation
        page.goto(base + '/comptabilite/teledeclarations')
        html = page.content()
        assert html.count('Dépôt confirmé') >= 2 and 'Acceptée' not in html
        cur.execute(f"SELECT period_start, status, mode, receipt_stored IS NOT NULL FROM org_{org_id}.teledeclarations ORDER BY id")
        rows = cur.fetchall()
        assert [(str(r[0]), r[1], r[2], r[3]) for r in rows] == [(m2_start.isoformat(), 'confirmed', 'manual', True),
                                                                 (m1_start.isoformat(), 'confirmed', 'manual', True)]
        # Justificatif téléchargeable depuis la déclaration
        page.goto(m2_url)
        with page.expect_download() as dl:
            page.click('a:has-text("accuse-m2.pdf")')
        assert Path(dl.value.path()).read_bytes()[:4] == b'%PDF'

        # 14. Pages fiscales : ni erreur serveur ni erreur de console
        for url in ('/comptabilite/teledeclarations', '/comptabilite/echeances', '/comptabilite/profil-fiscal',
                    f'/comptabilite/tva/ca3?date_from={m1_start}&date_to={m1_end}', '/comptabilite/liasse-2050',
                    '/comptabilite/impot-societes', '/comptabilite/das2', '/comptabilite/cfe', '/comptabilite/ifu',
                    '/aide'):
            resp = page.goto(base + url)
            assert resp.status < 500, url
        browser.close()
        conn.close()
    assert not server_errors, server_errors
    assert not console_errors, console_errors
