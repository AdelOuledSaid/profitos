"""v421 — FEC (virgule décimale, auxiliaires, PieceRef, DateLet, EcritureNum
chronologique, « | » échappé) et à-nouveaux de clôture (résultat en 120/129)."""
import pytest

from tests.test_v421_invoice_compliance import _no_boamp, app, _post, _flashes, _signup  # noqa: F401


def _cx(org_id):
    from profitos import runtime as rt
    from profitos import db as dbmod
    return dbmod.connect_tenant(org_id, rt.tenant_db(org_id))


def test_fec_format_aux_codes_lettering_and_chronological_numbers(app):
    from profitos.accounting import create_entry, generate_fec, set_lettrage, _next_lettrage_code
    client, org_id = _signup(app)
    c = _cx(org_id)
    sale = create_entry(c, 'VE', '2025-03-10', 'Facture FA-2025-001 — Client | Alpha',
                        [{'account_code': '411000', 'debit': 1234.5, 'auxiliary_name': 'Client | Alpha'},
                         {'account_code': '706000', 'credit': 1028.75},
                         {'account_code': '445710', 'credit': 205.75}],
                        source_type='outgoing_invoice', source_id=1, piece_ref='FA-2025-001')
    later = create_entry(c, 'BQ', '2025-03-20', 'Règlement', [{'account_code': '512000', 'debit': 10},
                                                              {'account_code': '627000', 'credit': 10}])
    # Saisi après coup mais daté du 15/03 : il doit précéder l'écriture du 20/03.
    pay = create_entry(c, 'BQ', '2025-03-15', 'Règlement FA-2025-001',
                       [{'account_code': '512000', 'debit': 1234.5},
                        {'account_code': '411000', 'credit': 1234.5, 'auxiliary_name': 'Client | Alpha'}],
                       piece_ref='VIR-778')
    lines = c.execute("SELECT id FROM accounting_entry_lines WHERE account_code='411000'").fetchall()
    set_lettrage(c, [r['id'] for r in lines], _next_lettrage_code(c), '2025-03-16')
    c.commit()
    rows = generate_fec(c, '2025-01-01', '2025-12-31')
    c.close()
    header, data = rows[0], rows[1:]
    idx = {name: i for i, name in enumerate(header)}
    assert [r[idx['JournalCode']] for r in data] == ['BQ', 'BQ', 'BQ', 'BQ', 'VE', 'VE', 'VE']
    bq = [(r[idx['EcritureNum']], r[idx['EcritureDate']]) for r in data if r[0] == 'BQ']
    assert bq == [('BQ-2025-00001', '20250315'), ('BQ-2025-00001', '20250315'),
                  ('BQ-2025-00002', '20250320'), ('BQ-2025-00002', '20250320')]
    client_line = next(r for r in data if r[0] == 'VE' and r[idx['CompteNum']] == '411000')
    assert client_line[idx['Debit']] == '1234,50' and client_line[idx['Credit']] == '0,00'
    assert client_line[idx['CompAuxNum']] == 'C00001' and client_line[idx['CompAuxLib']] == 'Client / Alpha'
    assert client_line[idx['PieceRef']] == 'FA-2025-001'
    assert client_line[idx['EcritureLet']] and client_line[idx['DateLet']] == '20250316'
    assert all('|' not in cell for r in data for cell in r)
    pay_line = next(r for r in data if r[0] == 'BQ' and r[idx['CompteNum']] == '411000')
    assert pay_line[idx['PieceRef']] == 'VIR-778' and pay_line[idx['CompAuxNum']] == 'C00001'
    assert sale and later and pay


def test_opening_entries_carry_balances_and_result_to_120(app):
    from profitos.accounting import create_entry, compute_bilan
    client, org_id = _signup(app)
    c = _cx(org_id)
    create_entry(c, 'VE', '2025-06-01', 'Vente', [{'account_code': '411000', 'debit': 1200, 'auxiliary_name': 'Client A'},
                                                  {'account_code': '706000', 'credit': 1000},
                                                  {'account_code': '445710', 'credit': 200}])
    create_entry(c, 'AC', '2025-07-01', 'Achat', [{'account_code': '606400', 'debit': 300},
                                                  {'account_code': '401000', 'credit': 300, 'auxiliary_name': 'Fournisseur B'}])
    create_entry(c, 'OD', '2025-08-01', 'Apport en compte courant', [{'account_code': '512000', 'debit': 500},
                                                                    {'account_code': '455000', 'credit': 500}])
    c.close()
    r = _post(client, '/comptabilite/cloture/a-nouveaux', {'exercice_start': '2025-01-01', 'exercice_end': '2025-12-31'})
    assert r.status_code == 302, _flashes(client)
    c = _cx(org_id)
    entry = c.execute("SELECT * FROM accounting_entries WHERE journal_code='AN'").fetchone()
    assert entry['entry_date'] == '2026-01-01'
    lines = {(l['account_code'], l['auxiliary_name'] or ''): (l['debit'], l['credit'])
             for l in c.execute('SELECT * FROM accounting_entry_lines WHERE entry_id=?', (entry['id'],)).fetchall()}
    assert lines[('120000', '')] == (0, 700.0)       # 1 000 - 300
    assert lines[('411000', 'Client A')] == (1200.0, 0)
    assert lines[('401000', 'Fournisseur B')] == (0, 300.0)
    assert lines[('455000', '')] == (0, 500.0)
    assert ('706000', '') not in lines
    cls = c.execute("SELECT account_class FROM accounting_chart_of_accounts WHERE code='455000'").fetchone()[0]
    assert cls == 4
    actif, passif, _ = compute_bilan(c, '2026-12-31', '2026-01-01')
    c.close()
    assert actif['total'] == passif['total'] == 1700.0
    # 455000 en dettes, jamais en capitaux propres.
    assert passif['capitaux_propres'] == 700.0 and passif['dettes'] == 1000.0
    _post(client, '/comptabilite/cloture/a-nouveaux', {'exercice_start': '2025-01-01', 'exercice_end': '2025-12-31'})
    assert any('déjà été générés' in m for m in _flashes(client))


def test_bank_categorization_splits_vat_and_uses_bq_sequence_and_closure(app):
    client, org_id = _signup(app)
    c = _cx(org_id)
    c.execute("INSERT INTO bank_accounts(provider,provider_account_id,name) VALUES('test','acc1','Compte')")
    c.execute("INSERT INTO bank_transactions(provider,provider_transaction_id,provider_account_id,transaction_date,label,amount) "
              "VALUES('test','t1','acc1','2025-05-10','Fournitures bureau',-120.0)")
    c.execute("INSERT INTO bank_transactions(provider,provider_transaction_id,provider_account_id,transaction_date,label,amount) "
              "VALUES('test','t2','acc1','2025-01-10','Ancien mouvement',-50.0)")
    ids = [r['id'] for r in c.execute('SELECT id FROM bank_transactions ORDER BY id').fetchall()]
    c.execute("INSERT INTO accounting_entity_closure(entity_key,entity_id,closed_until,closed_at,closed_by) VALUES(0,NULL,'2025-03-31','x',1)")
    c.commit(); c.close()
    _post(client, f'/banking/transactions/{ids[0]}/categoriser',
          {'category': 'Fournitures', 'account_code': '606400', 'vat_rate': '20'})
    c = _cx(org_id)
    e = c.execute("SELECT * FROM accounting_entries WHERE source_type='bank_categorization'").fetchone()
    lines = {l['account_code']: (l['debit'], l['credit']) for l in
             c.execute('SELECT * FROM accounting_entry_lines WHERE entry_id=?', (e['id'],)).fetchall()}
    c.close()
    assert lines == {'606400': (100.0, 0), '445660': (20.0, 0), '512000': (0, 120.0)}
    assert e['piece_number'].startswith('BQ-2025-') and e['piece_ref'] == f'BANKCAT-{ids[0]}'
    # Période clôturée : refus, rien n'est écrit.
    _post(client, f'/banking/transactions/{ids[1]}/categoriser', {'account_code': '606400'})
    assert any('clôturée' in m for m in _flashes(client))
    c = _cx(org_id)
    assert c.execute("SELECT COUNT(*) n FROM accounting_entries WHERE source_type='bank_categorization'").fetchone()['n'] == 1
    c.close()


def test_depreciation_dated_period_end():
    from profitos.accounting import depreciation_period_end
    assert depreciation_period_end('2025') == '2025-12-31'
    assert depreciation_period_end('2024-02') == '2024-02-29'
    assert depreciation_period_end('2025-T2') == '2025-06-30'


def test_facturx_requires_fpdf_287_and_xmp_declares_fx_schema():
    from profitos import invoice_pdf
    from profitos.routes import invoicing as I
    version = invoice_pdf.fpdf_version()
    if version < (2, 8, 7):
        # fpdf2 2.7.9 installé ici : pas de PDF/A-3, la route affiche un message clair.
        assert not invoice_pdf.pdfa_supported()
        assert I._render_facturx({}, b'<x/>', 't', 'd') is None
    xmp = I.facturx_xmp('Facture n° FA-1 & co')
    assert '<fx:ConformanceLevel>EN 16931</fx:ConformanceLevel>' in xmp
    assert '<pdfaid:part>3</pdfaid:part>' in xmp and 'FA-1 &amp; co' in xmp


def test_einvoice_validator_and_ereporting_use_per_rate_vat():
    import json
    from profitos.einvoice_validation import validate_outgoing_invoice
    from profitos.ereporting import _vat_buckets
    items = [{'label': 'A', 'qty': 1, 'unit_price': 0.13, 'vat_rate': 20, 'line_total': 0.13},
             {'label': 'B', 'qty': 1, 'unit_price': 0.13, 'vat_rate': 20, 'line_total': 0.13}]
    inv = {'invoice_number': 'FA-1', 'issue_date': '2026-01-01', 'due_date': None, 'line_items': json.dumps(items),
           'subtotal': 0.26, 'vat_amount': 0.05, 'total': 0.31, 'client_siren': ''}
    assert validate_outgoing_invoice(inv) == []
    inv.update(vat_amount=0.06, total=0.32)   # TVA ligne à ligne : 0,03 + 0,03
    assert any('BR-CO-14' in e for e in validate_outgoing_invoice(inv))
    assert _vat_buckets(inv) == {20.0: (0.26, 0.05)}
