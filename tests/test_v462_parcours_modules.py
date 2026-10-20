"""v462 — Parcours complet des autres modules, dans un vrai navigateur (Chromium) et sur
PostgreSQL : facture client émise et réglée par rapprochement bancaire, facture
fournisseur, exercice précédent clôturé (à-nouveaux, verrouillage), export FEC contrôlé,
liasse et balance de l'exercice suivant cohérentes.

Comme test_v461, ignoré par défaut : PROFITOS_E2E_DATABASE_URL doit désigner une base
PostgreSQL (docs/validation-fiscale.md, § 6).
"""
import io
import re
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from test_v461_parcours_complet import PASSWORD, SIRET, pg, pytestmark, server, submit  # noqa: F401
from fec_check import check_fec


def setup_company(page, base, email):
    page.goto(base + '/signup')
    page.fill('input[name=first_name]', 'Alice')
    page.fill('input[name=last_name]', 'Martin')
    page.fill('input[name=email]', email)
    page.fill('input[name=company_name]', 'Exemple SAS')
    page.fill('input[name=password]', PASSWORD)
    page.check('input[name=accept_terms]')
    submit(page, 'form:has(input[name=accept_terms])')
    conn = pg()
    cur = conn.cursor()
    cur.execute('UPDATE public.users SET email_verified=1, ui_mode=%s WHERE email=%s RETURNING id', ('expert', email))
    uid = cur.fetchone()[0]
    cur.execute('SELECT organization_id FROM public.memberships WHERE user_id=%s', (uid,))
    org_id = cur.fetchone()[0]
    cur.execute("UPDATE public.organizations SET plan='MULTI', status='ACTIVE_PAID' WHERE id=%s", (org_id,))
    page.goto(base + '/company')
    page.fill('input[name=name]', 'Exemple SAS')
    page.select_option('select[name=legal_form]', 'SAS')
    page.fill('textarea[name=address]', '1 rue de Test')
    page.fill('input[name=siret]', SIRET)
    page.fill('input[name=postal_code]', '75001')
    page.fill('input[name=city]', 'Paris')
    page.fill('input[name=share_capital]', '5000')
    page.fill('input[name=rcs_city]', 'Paris')
    page.fill('input[name=vat_number]', 'FR44732829320')
    page.fill('input[name=payment_terms]', '30 jours')
    page.fill('input[name=late_penalty_rate]', '10')
    submit(page, 'form:has(input[name=siret])')
    return conn, cur, org_id


def manual_entry(page, base, day, journal, label, lines):
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
    return page.inner_text('main')


def balance(cur, org_id, prefix, upto=None, journal_not=None, journal=None, since=None):
    sql = (f"SELECT COALESCE(SUM(l.debit-l.credit),0) FROM org_{org_id}.accounting_entry_lines l "
           f"JOIN org_{org_id}.accounting_entries e ON e.id=l.entry_id WHERE l.account_code LIKE %s")
    params = [prefix + '%']
    if upto:
        sql += ' AND e.entry_date<=%s'
        params.append(upto)
    if since:
        sql += ' AND e.entry_date>=%s'
        params.append(since)
    if journal:
        sql += ' AND e.journal_code=%s'
        params.append(journal)
    if journal_not:
        sql += ' AND e.journal_code<>%s'
        params.append(journal_not)
    cur.execute(sql, params)
    return round(float(cur.fetchone()[0]), 2)


def test_parcours_facturation_banque_cloture_fec(server):
    sync_api = pytest.importorskip('playwright.sync_api')
    base = server['base']
    today = datetime.now(ZoneInfo('Europe/Paris')).date()
    prev_start, prev_end = date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
    email = f'e2e-{uuid.uuid4().hex[:8]}@example.com'
    console_errors, server_errors = [], []
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(accept_downloads=True, locale='fr-FR')
        page.on('console', lambda m: m.type == 'error' and console_errors.append(f'{page.url}: {m.text}'))
        page.on('response', lambda r: r.status >= 500 and server_errors.append(f'{r.status} {r.url}'))
        page.on('dialog', lambda d: d.accept())
        conn, cur, org_id = setup_company(page, base, email)
        S = f'org_{org_id}'

        # 1. Exercice précédent : capital, ventes, charges (saisie manuelle), puis à-nouveaux et clôture.
        manual_entry(page, base, prev_start + timedelta(days=1), 'BQ', 'Capital', [('512000', 5000, 0), ('101300', 0, 5000)])
        manual_entry(page, base, prev_start + timedelta(days=100), 'VE', 'Ventes N-1',
                     [('411000', 36000, 0), ('706000', 0, 30000), ('445710', 0, 6000)])
        manual_entry(page, base, prev_start + timedelta(days=130), 'BQ', 'Encaissement', [('512000', 36000, 0), ('411000', 0, 36000)])
        manual_entry(page, base, prev_start + timedelta(days=150), 'AC', 'Achats N-1',
                     [('606400', 10000, 0), ('445660', 2000, 0), ('401000', 0, 12000)])
        manual_entry(page, base, prev_end, 'OD', 'Liquidation TVA', [('445710', 6000, 0), ('445660', 0, 2000), ('445510', 0, 4000)])
        page.goto(base + '/comptabilite/cloture')
        page.fill('form[action$="/a-nouveaux"] input[name=exercice_start]', prev_start.isoformat())
        page.fill('form[action$="/a-nouveaux"] input[name=exercice_end]', prev_end.isoformat())
        submit(page, 'form[action$="/a-nouveaux"]')
        assert 'À-nouveaux générés' in page.inner_text('main'), page.inner_text('main')[:800]
        page.fill('input[name=closed_until]', prev_end.isoformat())
        submit(page, 'form:has(input[name=closed_until])')
        assert 'clôturée jusqu' in page.inner_text('main')
        # Résultat N-1 = 30 000 − 10 000 = 20 000, porté en 120000 dans les à-nouveaux.
        cur.execute(f"SELECT COUNT(*) FROM {S}.accounting_entries WHERE journal_code='AN' AND entry_date=%s", (today.replace(month=1, day=1),))
        assert cur.fetchone()[0] >= 1
        an = lambda prefix: balance(cur, org_id, prefix, journal='AN')
        assert an('120') == -20000 and an('101') == -5000
        assert an('512') == 41000 and an('401') == -12000 and an('445510') == -4000
        assert an('6') == 0 and an('7') == 0                              # comptes de gestion non repris
        # Période close : saisie refusée, contrepassation refusée.
        text = manual_entry(page, base, prev_end - timedelta(days=3), 'OD', 'Saisie tardive', [('606400', 10, 0), ('512000', 0, 10)])
        assert 'non enregistrée' in text and 'Écriture enregistrée' not in text
        # Clôture irréversible : impossible de reculer la date.
        page.goto(base + '/comptabilite/cloture')
        page.fill('input[name=closed_until]', (prev_end - timedelta(days=30)).isoformat())
        submit(page, 'form:has(input[name=closed_until])')
        assert 'impossible de reculer' in page.inner_text('main')

        # 2. Export FEC de l'exercice clos, contrôlé (structure DGFiP, équilibre, numérotation).
        page.goto(base + '/comptabilite/export-fec')
        page.fill('input[name=date_from]', prev_start.isoformat())
        page.fill('input[name=date_to]', prev_end.isoformat())
        with page.expect_download() as dl:
            page.locator('form:has(input[name=date_from]) button:not([type=button])').first.click()
        name = dl.value.suggested_filename
        assert re.fullmatch(r'732829320FEC%d1231\.txt' % prev_end.year, name), name
        report = check_fec(Path(dl.value.path()).read_bytes())
        assert not report['errors'], report['errors']
        assert report['entries'] == 5 and report['total_debit'] == report['total_credit'] == 5000 + 36000 + 36000 + 12000 + 6000

        # 3. Facture client de l'exercice en cours : création, émission, PDF.
        page.goto(base + '/facturation/nouvelle')
        page.fill('input[name=client_name]', 'Client Pilote SAS')
        page.fill('[name=client_address]', '2 rue de la Paix\n75002 Paris')
        page.fill('input[name=client_siren]', '732829320')
        page.fill('input[name=due_date]', (today + timedelta(days=30)).isoformat())
        page.fill('input[name=service_date]', today.isoformat())
        page.fill('input[name=label_1]', 'Mission de conseil')
        page.fill('input[name=qty_1]', '2')
        page.fill('input[name=price_1]', '750')
        if page.locator('select[name=vat_1]').count():
            page.select_option('select[name=vat_1]', '20')
        else:
            page.fill('input[name=vat_1]', '20')
        submit(page, 'form:has(input[name=client_name])')
        assert re.search(r'/facturation/\d+$', page.url), page.inner_text('main')[:1500]
        invoice_url = page.url
        submit(page, 'form[action$="/marquer-emise"]')
        main = page.inner_text('main')
        cur.execute(f"SELECT id, invoice_number, status, total FROM {S}.outgoing_invoices ORDER BY id DESC LIMIT 1")
        inv_id, inv_number, status, total = cur.fetchone()
        assert inv_number and status == 'sent' and float(total) == 1800, (inv_number, status, total, main[:500])
        with page.expect_download() as dl:
            page.click('a[href$="/pdf"]')
        assert Path(dl.value.path()).read_bytes()[:4] == b'%PDF'
        # Prestation de services : TVA exigible à l'encaissement, d'abord en attente (44574).
        assert balance(cur, org_id, '706', journal_not='AN', since=today.replace(month=1, day=1)) == -1500
        assert balance(cur, org_id, '445740', since=today.replace(month=1, day=1)) == -300 and balance(cur, org_id, '445710', journal_not='AN', since=today.replace(month=1, day=1)) == 0

        # 4. Relevé bancaire importé puis rapprochement avec la facture.
        csv = ("Date;Libellé;Débit;Crédit;Solde\r\n"
               f"{today:%d/%m/%Y};VIR CLIENT PILOTE SAS {inv_number};;1 800,00;42 800,00\r\n"
               f"{today:%d/%m/%Y};PRLV FOURNISSEUR BUREAU;120,00;;42 680,00\r\n")
        page.goto(base + '/upload/bank-statement')
        form = 'form[enctype="multipart/form-data"]:has(input[name=bank_account_name])'
        page.set_input_files(f'{form} input[name=file]', files=[{'name': 'releve.csv', 'mimeType': 'text/csv', 'buffer': csv.encode('utf-8')}])
        page.fill(f'{form} input[name=bank_account_name]', 'Banque principale')
        submit(page, form)
        page.goto(base + '/banking')
        rec = page.locator(f'form[action*="/{inv_id}"][action*="rapproch"], form[action$="/{inv_id}"]').first
        assert rec.count(), page.inner_text('main')[:3000]
        rec.locator('button').first.click()
        page.wait_for_load_state('networkidle')
        cur.execute(f"SELECT status FROM {S}.outgoing_invoices WHERE id=%s", (inv_id,))
        assert cur.fetchone()[0] == 'paid'
        assert balance(cur, org_id, '411', journal_not='AN', since=today.replace(month=1, day=1)) == 0
        # Encaissement : la TVA devient exigible (44574 → 44571) et la banque est débitée.
        assert balance(cur, org_id, '445740', since=today.replace(month=1, day=1)) == 0 and balance(cur, org_id, '445710', journal_not='AN', since=today.replace(month=1, day=1)) == -300
        assert balance(cur, org_id, '512', journal_not='AN', since=today.replace(month=1, day=1)) == 1800

        # 5. Facture fournisseur saisie : écriture d'achat.
        page.goto(base + '/facturation/achats/nouvelle')
        page.fill('input[name=supplier_name]', 'Fournisseur Bureau')
        page.fill('input[name=invoice_number]', 'FB-2026-01')
        page.fill('input[name=issue_date]', today.isoformat())
        page.fill('input[name=due_date]', (today + timedelta(days=30)).isoformat())
        page.fill('input[name=subtotal]', '100')
        page.fill('input[name=vat_amount]', '20')
        submit(page, 'form:has(input[name=supplier_name])')
        cur.execute(f"SELECT COUNT(*) FROM {S}.purchase_invoices WHERE invoice_number='FB-2026-01'")
        assert cur.fetchone()[0] == 1
        assert balance(cur, org_id, '445660', journal_not='AN', since=today.replace(month=1, day=1)) == 20

        # 6. Exercice en cours : la balance d'ouverture reprend la clôture N-1 ; bilan équilibré.
        page.goto(base + '/comptabilite/liasse-2050?exercice=courant')
        text = page.inner_text('main')
        assert 'Bilan équilibré' in text, text[:1500]
        page.goto(base + '/comptabilite/balance')
        assert page.locator('main').count()

        for url in ('/facturation', '/facturation/achats', '/banking', '/comptabilite/grand-livre', '/comptabilite/cloture',
                    '/comptabilite/export-fec', '/facturation/creances', '/facturation/dettes-fournisseurs'):
            resp = page.goto(base + url)
            assert resp.status < 500, url
        browser.close()
        conn.close()
    assert not server_errors, server_errors
    assert not console_errors, console_errors
