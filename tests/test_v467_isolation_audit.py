"""v467 — Audit systématique de l'isolation entre entreprises.

Deux entreprises A et B sont remplies dans tous les modules (facturation, achats et
justificatifs, banque, comptabilité, immobilisations, emprunts, déclarations). A reçoit
plus d'enregistrements que B, si bien que ses identifiants les plus élevés n'existent
pas chez B. Connecté en B, on parcourt ensuite TOUTES les routes GET de l'application
(exports et téléchargements compris) avec les identifiants de A, puis on envoie un POST à
toutes les routes avec ces identifiants : aucune page ne doit contenir une donnée de A,
aucune requête ne doit échouer en erreur serveur, et les données de A doivent rester
strictement identiques. Tourne sur SQLite (une base par entreprise) et sur PostgreSQL
(un schéma par entreprise) quand DATABASE_URL est défini.
"""
import hashlib
import io
import re
import uuid

import pytest

import profitos.runtime as rt
from profitos import create_app
from profitos.config import DevelopmentConfig

CSRF = 'c' * 64
MARK_A = 'Zorglubium'          # nom présent uniquement dans les données de A
PDF = b'%PDF-1.4\n% Zorglubium justificatif\n'
SKIP_GET = ('logout', '/oauth', 'callback', '/connect', 'webhook', 'sw.js', 'service-worker', '/static', 'swan', 'powens',
            'gocardless', 'stripe', 'billing/portal', 'checkout')
SKIP_POST = ('logout', 'delete-account', 'supprimer-compte', 'billing', 'stripe', 'webhook', '/api/', 'swan', 'gocardless',
             'powens', 'banking/connect', 'oauth', '2fa', 'mot-de-passe', 'password', 'email', 'signup', 'login', 'reset',
             'verify', 'organisation/suppression', 'org-deletion', 'mes-donnees')
TABLES = ('outgoing_invoices', 'outgoing_invoice_payments', 'outgoing_quotes', 'invoicing_clients', 'purchase_invoices',
          'suppliers', 'bank_transactions', 'bank_accounts', 'accounting_entries', 'accounting_entry_lines', 'fixed_assets',
          'loans', 'loan_installments', 'teledeclarations', 'fiscal_profile', 'fiscal_settings', 'company_profile',
          'invoices', 'expenses', 'app_settings')


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, 'AUTH_DB', tmp_path / 'auth.db')
    monkeypatch.setattr(rt, 'TENANTS', tmp_path)
    from profitos import storage
    monkeypatch.setattr(storage, 'UPLOAD_ROOT', tmp_path / 'uploads')
    for var in ('RESEND_API_KEY', 'SMTP_HOST', 'EDI_PARTNER_ENABLED', 'FISCAL_DECLARATIONS_ENABLED'):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv('PROFITOS_ENV', 'development')
    a = create_app(DevelopmentConfig)
    a.config.update(TESTING=True)
    try:
        rt.limiter.enabled = False
    except Exception:
        pass
    return a


class Client:
    def __init__(self, app, company, tag):
        self.cl = app.test_client()
        self.tag = tag
        with self.cl.session_transaction() as s:
            s['csrf_token'] = CSRF
        self.cl.post('/signup', data={'csrf_token': CSRF, 'accept_terms': '1', 'first_name': 'Alice', 'last_name': tag,
                                      'email': f'{uuid.uuid4().hex[:10]}@example.com', 'password': 'Brume-Cactus-Velo-48',
                                      'company_name': company})
        with self.cl.session_transaction() as s:
            self.org, uid = s['org_id'], s['user_id']
        ac = rt.auth_cx()
        ac.execute("UPDATE organizations SET plan='MULTI',status='ACTIVE_PAID' WHERE id=?", (self.org,))
        ac.execute("UPDATE users SET email_verified=1,ui_mode='expert' WHERE id=?", (uid,))
        ac.commit(); ac.close()

    def post(self, url, data=None, **kw):
        with self.cl.session_transaction() as s:
            s['csrf_token'] = CSRF
        d = dict(data or {})
        d.setdefault('csrf_token', CSRF)
        return self.cl.post(url, data=d, **kw)

    def get(self, url):
        return self.cl.get(url)


def fill(c, name, copies):
    c.post('/company', {'name': name, 'legal_form': 'SAS', 'siret': '73282932000074', 'vat_number': 'FR44732829320',
                        'address': '1 rue Test', 'postal_code': '75001', 'city': 'Paris', 'share_capital': '1000',
                        'rcs_city': 'Paris', 'payment_terms': '30 jours', 'late_penalty_rate': '10'})
    c.post('/comptabilite/profil-fiscal', {'tax_regime': 'normal', 'vat_filing': 'ca3_mensuelle', 'signataire': f'{name} Gérant'})
    for i in range(copies):
        c.post('/facturation/clients/nouveau', {'name': f'{name} Client {i}', 'email': f'c{i}@example.com',
                                                'address': '2 rue X, 75002 Paris', 'siren': '732829320'})
        r = c.post('/facturation/nouvelle', {'client_name': f'{name} Client {i}', 'client_address': '2 rue X\n75002 Paris',
                                             'client_siren': '732829320', 'due_date': '2026-11-30', 'service_date': '2026-10-01',
                                             'operation_nature': 'services', 'label_1': f'{name} mission', 'qty_1': '1',
                                             'price_1': '1000', 'vat_1': '20'})
        inv = re.search(r'/facturation/(\d+)', r.headers.get('Location', ''))
        if inv:
            c.post(f'/facturation/{inv.group(1)}/marquer-emise')
            c.post(f'/facturation/{inv.group(1)}/reglement', {'amount': '200', 'payment_date': rt.paris_today().isoformat(), 'payment_method': 'bank'})
        c.post('/facturation/devis/nouveau', {'client_name': f'{name} Client {i}', 'client_address': '2 rue X',
                                              'valid_until': '2026-11-30', 'operation_nature': 'services', 'label_1': f'{name} devis',
                                              'qty_1': '1', 'price_1': '500', 'vat_1': '20'})
        c.post('/facturation/achats/nouvelle', {'supplier_name': f'{name} Fournisseur {i}', 'invoice_number': f'F-{name}-{i}',
                                                'issue_date': '2026-10-01', 'due_date': '2026-10-31', 'subtotal': '100',
                                                'vat_amount': '20', 'category': 'autre',
                                                'document': (io.BytesIO(PDF.replace(b'Zorglubium', name.encode())), f'{name}-{i}.pdf')},
               content_type='multipart/form-data')
        c.post('/comptabilite/emprunts/nouveau', {'lender_name': f'{name} Banque {i}', 'principal_amount': '10000',
                                                  'annual_rate': '3', 'start_date': '2026-01-05', 'duration_months': '24'})
        c.post('/comptabilite/immobilisations', {'label': f'{name} Ordinateur {i}', 'purchase_date': '2026-02-01',
                                                 'purchase_amount': '1 500,00', 'asset_account': '218300', 'depreciation_account': '281830',
                                                 'useful_life_years': '3',
                                                 'method': 'linear'})
        c.post('/comptabilite/ecritures/nouvelle', {'journal': 'OD', 'entry_date': '2026-10-02', 'label': f'{name} écriture {i}',
                                                    'compte_0': '606400', 'debit_0': '10', 'compte_1': '512000', 'credit_1': '10'})
    bank = ("Date;Libellé;Débit;Crédit;Solde\r\n" + ''.join(
        f"0{i + 1}/10/2026;VIR {name.upper()} {i};;1 200,00;{5000 + i}\r\n" for i in range(copies)))
    c.post('/upload/bank-statement', {'file': (io.BytesIO(bank.encode()), 'b.csv'), 'bank_account_name': f'{name} Banque'},
           content_type='multipart/form-data')
    for month, last in (('09', '30'), ('08', '31'), ('07', '31'))[:copies]:
        c.post('/comptabilite/teledeclarations/preparer-depot', {'type': 'TVA_CA3', 'du': f'2026-{month}-01',
                                                                 'au': f'2026-{month}-{last}', 'regime': 'normal'})


def snapshot(org):
    c = rt.tenant_cx_direct(org)
    out = {}
    for t in TABLES:
        try:
            rows = c.execute(f'SELECT * FROM {t} ORDER BY 1').fetchall()
        except Exception:
            try:
                c.rollback()
            except Exception:
                pass
            continue
        out[t] = hashlib.sha256(repr([tuple(r) for r in rows]).encode()).hexdigest()
    c.close()
    return out


def max_ids(org):
    c = rt.tenant_cx_direct(org)
    ids = {}
    for param, table in {'invoice_id': 'outgoing_invoices', 'quote_id': 'outgoing_quotes', 'purchase_id': 'purchase_invoices',
                         'client_id': 'invoicing_clients', 'loan_id': 'loans', 'asset_id': 'fixed_assets',
                         'entry_id': 'accounting_entries', 'supplier_id': 'suppliers', 'decl_id': 'teledeclarations',
                         'account_id': 'bank_accounts', 'tx_id': 'bank_transactions', 'transaction_id': 'bank_transactions',
                         'payment_id': 'outgoing_invoice_payments', 'installment_id': 'loan_installments'}.items():
        try:
            ids[param] = c.execute(f'SELECT MAX(id) m FROM {table}').fetchone()['m']
        except Exception:
            try:
                c.rollback()
            except Exception:
                pass
    c.close()
    return {k: v for k, v in ids.items() if v}


def test_aucune_fuite_entre_entreprises(app):
    a = Client(app, MARK_A, 'A')
    fill(a, MARK_A, 3)
    b = Client(app, 'Beta Conseil', 'B')
    fill(b, 'Beta Conseil', 1)
    ids_a, ids_b = max_ids(a.org), max_ids(b.org)
    # Les identifiants les plus élevés de A n'existent pas chez B.
    probe = {k: v for k, v in ids_a.items() if v > ids_b.get(k, 0)}
    assert {'invoice_id', 'purchase_id', 'entry_id', 'client_id', 'asset_id', 'decl_id', 'payment_id', 'loan_id',
            'quote_id', 'tx_id'} <= set(probe), (ids_a, ids_b)
    # A voit bien ses propres données (le test détecterait une fuite).
    assert MARK_A in a.get(f"/facturation/{ids_a['invoice_id']}").get_data(as_text=True)
    before = snapshot(a.org)

    leaks, errors, checked = [], [], 0
    for rule in sorted(app.url_map.iter_rules(), key=lambda r: r.rule):
        if 'GET' not in rule.methods or any(k in rule.rule for k in SKIP_GET):
            continue
        url = rule.rule
        if rule.arguments:
            if not all(arg in probe for arg in rule.arguments):
                continue
            for arg in rule.arguments:
                url = re.sub(r'<(?:int:)?' + arg + '>', str(probe[arg]), url)
        r = b.get(url)
        checked += 1
        if r.status_code >= 500:
            errors.append(('GET', url, r.status_code))
        body = r.get_data()
        if MARK_A.encode() in body or MARK_A.lower().encode() in body.lower():
            leaks.append(('GET', url, r.status_code))
    assert checked > 150
    assert not errors, errors
    assert not leaks, leaks

    garbage = {'amount': '1', 'label': 'x', 'name': 'x', 'status': 'paid', 'reason': 'x', 'note': 'x' * 12, 'verified': '1',
               'payment_date': '2026-10-05', 'filed_on': '2026-10-05', 'entry_date': '2026-10-05'}
    for rule in sorted(app.url_map.iter_rules(), key=lambda r: r.rule):
        if 'POST' not in rule.methods or not rule.arguments or any(k in rule.rule for k in SKIP_POST):
            continue
        if not all(arg in probe for arg in rule.arguments):
            continue
        url = rule.rule
        for arg in rule.arguments:
            url = re.sub(r'<(?:int:)?' + arg + '>', str(probe[arg]), url)
        for data in ({}, garbage):
            r = b.post(url, data)
            if r.status_code >= 500:
                errors.append(('POST', url, r.status_code))
            if MARK_A.encode() in r.get_data():
                leaks.append(('POST', url, r.status_code))
    assert not errors, errors
    assert not leaks, leaks
    # Les données de A sont strictement inchangées.
    assert snapshot(a.org) == before

    # Fichiers : les justificatifs de A ne sont accessibles qu'à A.
    from profitos import storage
    files_a = list((storage.UPLOAD_ROOT).rglob(f'*'))
    for f in files_a:
        if f.is_file() and MARK_A.encode() in f.read_bytes():
            assert f'org_{a.org}' in str(f) or f'/{a.org}/' in str(f), f
