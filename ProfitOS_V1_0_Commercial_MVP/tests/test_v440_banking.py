"""v440 — Banque : pagination Powens (opérations à venir / supprimées), montants à la
française dans les rapprochements, TVA sur immobilisations, compte 512 par compte
bancaire, relevés importés enregistrés comme opérations (colonnes Débit / Crédit,
dédoublonnage) et état de rapprochement bancaire."""
import io

import pytest

from tests.test_v421_invoice_compliance import _no_boamp, app, _post, _flashes, _signup  # noqa: F401


def _cx(org_id):
    from profitos import runtime as rt
    from profitos import db as dbmod
    return dbmod.connect_tenant(org_id, rt.tenant_db(org_id))


def _account(c, key='acc1', code=None, balance=None, balance_date=None, provider='import'):
    c.execute("INSERT INTO bank_accounts(provider,provider_account_id,name,accounting_code,balance,statement_balance_date,last_synced_at) "
              "VALUES(?,?,?,?,?,?,?)", (provider, key, f'Compte {key}', code, balance, balance_date, '2026-01-01T00:00:00'))


def _tx(c, tid, amount, day='2026-03-10', label='Opération', key='acc1', provider='import'):
    c.execute("INSERT INTO bank_transactions(provider,provider_transaction_id,provider_account_id,transaction_date,label,amount,raw_status) "
              "VALUES(?,?,?,?,?,?,'booked')", (provider, tid, key, day, label, amount))
    return c.execute("SELECT id FROM bank_transactions WHERE provider=? AND provider_transaction_id=?", (provider, tid)).fetchone()['id']


def _entry_lines(c, source_type, source_id):
    e = c.execute("SELECT id FROM accounting_entries WHERE source_type=? AND source_id=?", (source_type, source_id)).fetchone()
    return {l['account_code']: (l['debit'], l['credit']) for l in
            c.execute('SELECT * FROM accounting_entry_lines WHERE entry_id=?', (e['id'],)).fetchall()}


# ------------------------------------------------------------------ TVA immobilisation
def test_bank_categorization_vat_on_fixed_asset_goes_to_445620():
    from profitos.routes.bank_sync import _bank_categorization_lines
    lines = _bank_categorization_lines('218300', '512000', 1200.0, 20, True, 'Ordinateur')
    assert {l['account_code']: l.get('debit', 0) for l in lines} == {'218300': 1000.0, '445620': 200.0, '512000': 0}
    lines = _bank_categorization_lines('606400', '512000', 120.0, 20, True, 'Fournitures')
    assert '445660' in {l['account_code'] for l in lines}


# ------------------------------------------------------------------ rapprochement fournisseur
def test_purchase_reconciliation_accepts_french_amount_and_uses_account_ledger(app):
    client, org_id = _signup(app)
    c = _cx(org_id)
    c.execute("INSERT INTO accounting_chart_of_accounts(code,label,account_class,is_active) VALUES('512100','Banque — Qonto',5,1)")
    _account(c, code='512100')
    tx_id = _tx(c, 't1', -1234.56, label='PRLV FOURNISSEUR')
    c.execute("INSERT INTO purchase_invoices(supplier_name,invoice_number,issue_date,subtotal,vat_amount,total,status,created_at,category) "
              "VALUES('Fournisseur SA','F-1','2026-03-01',1028.80,205.76,1234.56,'unpaid','2026-03-01','autre')")
    pid = c.execute("SELECT id FROM purchase_invoices").fetchone()['id']
    c.commit(); c.close()
    r = _post(client, '/banking/rapprochement-achat/confirmer',
              {'bank_transaction_id': tx_id, 'purchase_invoice_id': pid, 'matched_amount': '1 234,56'})
    assert r.status_code == 302, r.status_code
    c = _cx(org_id)
    pay = c.execute("SELECT * FROM purchase_invoice_payments").fetchone()
    assert pay['amount'] == 1234.56
    assert _entry_lines(c, 'purchase_invoice_payment_v2', pay['id']) == {'401000': (1234.56, 0), '512100': (0, 1234.56)}
    assert c.execute("SELECT status FROM purchase_invoices WHERE id=?", (pid,)).fetchone()['status'] == 'paid'
    c.close()


def test_reconciliation_rejects_unreadable_amount_without_error(app):
    client, org_id = _signup(app)
    c = _cx(org_id)
    _account(c)
    tx_id = _tx(c, 't1', -50)
    c.execute("INSERT INTO purchase_invoices(supplier_name,invoice_number,subtotal,vat_amount,total,status,created_at) "
              "VALUES('F','F-2',50,0,50,'unpaid','2026-03-01')")
    pid = c.execute("SELECT id FROM purchase_invoices").fetchone()['id']
    c.commit(); c.close()
    r = _post(client, '/banking/rapprochement-achat/confirmer',
              {'bank_transaction_id': tx_id, 'purchase_invoice_id': pid, 'matched_amount': 'cinquante'})
    assert r.status_code == 302
    assert any('invalide' in m for m in _flashes(client))
    c = _cx(org_id)
    assert c.execute("SELECT COUNT(*) n FROM purchase_invoice_payments").fetchone()['n'] == 0
    c.close()


def test_fee_and_categorization_use_the_bank_account_ledger(app):
    client, org_id = _signup(app)
    c = _cx(org_id)
    c.execute("INSERT INTO accounting_chart_of_accounts(code,label,account_class,is_active) VALUES('512200','Banque 2',5,1)")
    _account(c, code='512200')
    tx_id = _tx(c, 't1', -36.0, label='Abonnement logiciel')
    c.commit(); c.close()
    _post(client, f'/banking/transactions/{tx_id}/categoriser', {'account_code': '651000', 'vat_rate': '20'})
    c = _cx(org_id)
    assert _entry_lines(c, 'bank_categorization', tx_id) == {'651000': (30.0, 0), '445660': (6.0, 0), '512200': (0, 36.0)}
    c.close()


def test_ledger_account_setting_only_accepts_51_accounts(app):
    client, org_id = _signup(app)
    c = _cx(org_id)
    _account(c)
    aid = c.execute("SELECT id FROM bank_accounts").fetchone()['id']
    c.commit(); c.close()
    _post(client, f'/banking/comptes/{aid}/compte-comptable', {'accounting_code': '606400'})
    c = _cx(org_id)
    assert c.execute("SELECT accounting_code FROM bank_accounts WHERE id=?", (aid,)).fetchone()['accounting_code'] is None
    c.execute("INSERT INTO accounting_chart_of_accounts(code,label,account_class,is_active) VALUES('512100','Banque 2',5,1)")
    c.commit(); c.close()
    _post(client, f'/banking/comptes/{aid}/compte-comptable', {'accounting_code': '512100'})
    c = _cx(org_id)
    assert c.execute("SELECT accounting_code FROM bank_accounts WHERE id=?", (aid,)).fetchone()['accounting_code'] == '512100'
    c.close()


# ------------------------------------------------------------------ Powens
def test_powens_sync_paginates_and_skips_coming_and_deleted(app, monkeypatch):
    from profitos.routes import bank_sync as bs
    _, org_id = _signup(app)
    monkeypatch.setattr(bs, '_renew_token', lambda uid: 'tok')
    monkeypatch.setattr(bs, '_api_url', lambda path: 'https://powens.test' + path)
    monkeypatch.setattr(bs, 'POWENS_PAGE_SIZE', 2)
    pages = {
        0: [{'id': 1, 'id_account': 9, 'value': -10, 'date': '2026-03-01', 'wording': 'A'},
            {'id': 2, 'id_account': 9, 'value': 20, 'date': '2026-03-02', 'wording': 'B'}],
        2: [{'id': 3, 'id_account': 9, 'value': -5, 'date': '2026-03-03', 'wording': 'C', 'coming': True},
            {'id': 4, 'id_account': 9, 'value': 7, 'date': '2026-03-04', 'wording': 'D'}],
        4: [{'id': 5, 'id_account': 9, 'value': -10, 'date': '2026-02-27', 'wording': 'E', 'deleted': '2026-03-05 10:00:00'}],
    }
    calls = []

    class Resp:
        def __init__(self, payload): self.payload = payload; self.status_code = 200; self.ok = True
        def json(self): return self.payload
        def raise_for_status(self): pass

    def fake_get(url, headers=None, params=None, timeout=None):
        if url.endswith('/accounts'):
            return Resp({'accounts': [{'id': 9, 'name': 'Courant', 'balance': 1000}]})
        calls.append(params['offset'])
        return Resp({'transactions': pages.get(params['offset'], [])})

    monkeypatch.setattr(bs.requests, 'get', fake_get)
    monkeypatch.setattr(bs, '_json', lambda r: r.json())
    c = _cx(org_id)
    _account(c, key='9', provider='powens')  # E déjà présente depuis une synchronisation précédente
    _tx(c, '5', -10, day='2026-02-27', key='9', provider='powens')
    c.commit()
    n_accounts, n_tx = bs._sync_powens(c, {'id': 0, 'provider_user_id': 'u1', 'entity_id': None})
    ids = sorted(r['provider_transaction_id'] for r in c.execute("SELECT provider_transaction_id FROM bank_transactions WHERE provider='powens'").fetchall())
    c.close()
    assert calls == [0, 2, 4]
    assert ids == ['1', '2', '4']  # 3 à venir ignorée, 5 supprimée par la banque retirée
    assert n_tx == 3


# ------------------------------------------------------------------ relevé importé
CSV = ("Date;Libellé;Débit;Crédit;Solde\r\n"
       "01/03/2026;VIR CLIENT ALPHA;;1 200,00;6 200,00\r\n"
       "02/03/2026;PRLV ASSURANCE;85,40;;6 114,60\r\n"
       "02/03/2026;CB CAFE;3,50;;6 111,10\r\n"
       "02/03/2026;CB CAFE;3,50;;6 107,60\r\n")


def _upload(client, text, name='Qonto'):
    return _post(client, '/upload/bank-statement',
                 {'file': (io.BytesIO(text.encode('utf-8')), 'releve.csv'), 'bank_account_name': name},
                 content_type='multipart/form-data')


def test_statement_import_stores_debit_credit_lines_once(app):
    client, org_id = _signup(app)
    r = _upload(client, CSV)
    assert r.status_code == 200, _flashes(client)
    c = _cx(org_id)
    rows = c.execute("SELECT transaction_date,label,amount FROM bank_transactions WHERE provider='import' ORDER BY id").fetchall()
    assert [(r['transaction_date'], r['amount']) for r in rows] == [
        ('2026-03-01', 1200.0), ('2026-03-02', -85.4), ('2026-03-02', -3.5), ('2026-03-02', -3.5)]
    acct = c.execute("SELECT * FROM bank_accounts WHERE provider='import'").fetchone()
    assert acct['name'] == 'Qonto' and acct['balance'] == 6107.6 and acct['statement_balance_date'] == '2026-03-02'
    c.close()
    r2 = _upload(client, CSV)  # même relevé : aucun doublon
    c = _cx(org_id)
    assert c.execute("SELECT COUNT(*) n FROM bank_transactions WHERE provider='import'").fetchone()['n'] == 4
    c.close()
    assert 'déjà importées' in r2.get_data(as_text=True)
    page = client.get('/banking').get_data(as_text=True)
    assert 'PRLV ASSURANCE' in page and 'Relevé importé' in page


# ------------------------------------------------------------------ état de rapprochement
def test_reconciliation_statement_explains_the_gap(app):
    from profitos import bank_reconciliation as br
    from profitos.accounting import create_entry
    client, org_id = _signup(app)
    c = _cx(org_id)
    _account(c, balance=1000.0, balance_date='2026-03-31')
    _tx(c, 't1', 1100.0, day='2026-03-05', label='Apport')
    cat = _tx(c, 't2', -100.0, day='2026-03-10', label='Fournitures')
    _tx(c, 't3', 50.0, day='2026-04-02', label='Après la date')
    c.commit(); c.close()
    _post(client, '/banking/transactions/%d/categoriser' % cat, {'account_code': '606400'})
    c = _cx(org_id)
    # Chèque émis, comptabilisé mais pas encore débité par la banque.
    create_entry(c, 'BQ', '2026-03-20', 'Chèque 0001 fournisseur',
                 [{'account_code': '401000', 'debit': 40, 'auxiliary_name': 'X'}, {'account_code': '512000', 'credit': 40}])
    rep = br.reconciliation_statement(c, None, '512000', '2026-03-31')
    c.close()
    assert rep['bank_balance'] == 1000.0
    assert rep['ledger_balance'] == -140.0
    assert [u['remaining'] for u in rep['unbooked']] == [1100.0]  # apport pas encore comptabilisé
    assert rep['adjusted_ledger'] == 960.0
    assert rep['gap'] == 40.0
    assert [u['label'] for u in rep['unmatched']] == ['Chèque 0001 fournisseur']
    assert rep['unexplained'] == 0.0
    # Au 2 avril, le solde est recalculé avec le mouvement postérieur au relevé.
    c = _cx(org_id)
    assert br.bank_balance_at(c, c.execute("SELECT * FROM bank_accounts").fetchone(), '2026-04-02') == 1050.0
    assert br.bank_balance_at(c, c.execute("SELECT * FROM bank_accounts").fetchone(), '2026-03-07') == 1100.0
    c.close()
    page = client.get('/banking/etat-rapprochement?date=2026-03-31&solde=1 000,00').get_data(as_text=True)
    assert 'État de rapprochement bancaire' in page and 'Chèque 0001 fournisseur' in page


def test_reconciliation_statement_detects_missing_opening_balance(app):
    from profitos import bank_reconciliation as br
    _, org_id = _signup(app)
    c = _cx(org_id)
    _account(c, balance=6062.70, balance_date='2026-03-10')
    _tx(c, 't1', 1200.0, day='2026-03-01')
    _tx(c, 't2', -137.30, day='2026-03-10')
    c.commit()
    rep = br.reconciliation_statement(c, None, '512000', '2026-03-31')
    c.close()
    assert rep['opening_bank'] == 5000.0 and rep['gap'] == 5000.0 and rep['missing_opening'] is True
