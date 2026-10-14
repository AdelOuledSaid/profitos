"""v437 — Comptabilité complète, lot 1 : exercices, saisie manuelle (OD) avec pièce
jointe et contrepassation, grand livre, balances, reprise d'un FEC."""
import io
from datetime import date

import pytest

from support_v4xx import app, flashes, post, signup, tenant  # noqa: F401
from profitos import ledger as L
from profitos.accounting import AccountingError

FEC_HEADER = ("JournalCode|JournalLib|EcritureNum|EcritureDate|CompteNum|CompteLib|CompAuxNum|CompAuxLib|"
              "PieceRef|PieceDate|EcritureLib|Debit|Credit|EcritureLet|DateLet|ValidDate|Montantdevise|Idevise")


def fec(*rows):
    return (FEC_HEADER + "\r\n" + "\r\n".join(rows) + "\r\n").encode("utf-8")


SAMPLE_FEC = fec(
    "VT|Ventes|1|20250115|411000|Clients|C1|Dupont SARL|F001|20250115|Facture F001|1200,00|0,00||||",
    "VT|Ventes|1|20250115|706000|Prestations|||F001|20250115|Facture F001|0,00|1000,00||||",
    "VT|Ventes|1|20250115|445710|TVA collectée|||F001|20250115|Facture F001|0,00|200,00||||",
    "BQ|Banque|2|20250120|512000|Banque|||V1|20250120|Apport capital|5000,00|0,00||||",
    "BQ|Banque|2|20250120|101300|Capital souscrit appelé versé|||V1|20250120|Apport capital|0,00|5000,00||||",
)


def _entry(client, lines, journal='OD', entry_date='2026-03-10', label='Provision litige', **extra):
    data = {'journal': journal, 'entry_date': entry_date, 'label': label, 'lignes': '6'}
    for i, (acc, debit, credit) in enumerate(lines):
        data[f'compte_{i}'] = acc
        data[f'debit_{i}'] = debit
        data[f'credit_{i}'] = credit
    data.update(extra)
    return post(client, '/comptabilite/ecritures/nouvelle', data)


# ------------------------------------------------------------------ exercices
def test_fiscal_years_declared_overlap_and_default(app):
    _, _, org, _ = signup(app, 'Exercice SAS')
    c = tenant(org)
    try:
        fy = L.fiscal_year_for(c, date(2026, 5, 4))
        assert (fy['start'], fy['end'], fy['declared']) == (date(2026, 1, 1), date(2026, 12, 31), False)
        L.add_fiscal_year(c, '2025-07-01', '2026-06-30')
        fy = L.fiscal_year_for(c, date(2026, 5, 4))
        assert fy['declared'] and fy['start'] == date(2025, 7, 1)
        nxt = L.fiscal_year_for(c, date(2026, 9, 1))  # suivant : même rythme 1er juillet
        assert (nxt['start'], nxt['end']) == (date(2026, 7, 1), date(2027, 6, 30))
        with pytest.raises(AccountingError, match='chevauche'):
            L.add_fiscal_year(c, '2026-01-01', '2026-12-31')
        with pytest.raises(AccountingError, match='24 mois'):
            L.add_fiscal_year(c, '2027-07-01', '2029-12-31')
    finally:
        c.close()


def test_fiscal_year_form_and_closure_prefill(app):
    client, _, _, _ = signup(app, 'Exercice2 SAS')
    post(client, '/comptabilite/exercices', {'start_date': '2025-11-15', 'end_date': '2026-12-31'})
    html = client.get('/comptabilite/cloture').get_data(as_text=True)
    assert '15/11/2025' in html and 'Exercices comptables' in html


# ------------------------------------------------------------------ saisie / contrepassation
def test_manual_entry_balanced_with_attachment(app):
    client, _, org, _ = signup(app, 'Saisie SAS')
    r = _entry(client, [('681500 Dotations', '1 500,00', ''), ('151100', '', '1500')],
               piece=(io.BytesIO(b'%PDF-1.4 test'), 'pv.pdf'), piece_ref='PV-2026-01')
    assert r.status_code == 302 and '/comptabilite/ecritures/' in r.headers['Location']
    c = tenant(org)
    e = c.execute("SELECT * FROM accounting_entries WHERE label='Provision litige'").fetchone()
    lines = c.execute('SELECT account_code,debit,credit FROM accounting_entry_lines WHERE entry_id=? ORDER BY line_order', (e['id'],)).fetchall()
    att = c.execute('SELECT * FROM accounting_entry_attachments WHERE entry_id=?', (e['id'],)).fetchone()
    c.close()
    assert e['journal_code'] == 'OD' and e['piece_number'].startswith('OD-2026-') and e['piece_ref'] == 'PV-2026-01'
    assert [(l['account_code'], l['debit'], l['credit']) for l in lines] == [('681500', 1500.0, 0.0), ('151100', 0.0, 1500.0)]
    page = client.get(r.headers['Location']).get_data(as_text=True)
    assert 'pv.pdf' in page and 'Contrepasser' in page
    d = client.get(f"/comptabilite/ecritures/{e['id']}/pieces/{att['id']}")
    assert d.status_code == 200 and d.data == b'%PDF-1.4 test'


def test_unbalanced_or_unknown_account_entry_is_refused(app):
    client, _, org, _ = signup(app, 'Desequilibre SAS')
    r = _entry(client, [('606100', '100', ''), ('401000', '', '90')], label='Déséquilibrée')
    assert r.status_code == 200 and 'déséquilibrée' in r.get_data(as_text=True)
    assert 'value="Déséquilibrée"' in r.get_data(as_text=True)  # saisie conservée
    _entry(client, [('999999', '100', ''), ('401000', '', '100')], label='Compte inconnu')
    c = tenant(org)
    assert c.execute("SELECT COUNT(*) n FROM accounting_entries WHERE label IN ('Déséquilibrée','Compte inconnu')").fetchone()['n'] == 0
    c.close()


def test_reversal_once_and_not_of_a_reversal(app):
    client, _, org, _ = signup(app, 'Contrepasse SAS')
    _entry(client, [('606100', '120', ''), ('401000', '', '120')], label='Achat à annuler')
    c = tenant(org)
    eid = c.execute("SELECT id FROM accounting_entries WHERE label='Achat à annuler'").fetchone()['id']
    c.close()
    r = post(client, f'/comptabilite/ecritures/{eid}/contrepasser', {'reversal_date': '2026-03-31'})
    c = tenant(org)
    rev = c.execute("SELECT * FROM accounting_entries WHERE source_type='reversal' AND source_id=?", (eid,)).fetchone()
    lines = c.execute('SELECT account_code,debit,credit FROM accounting_entry_lines WHERE entry_id=? ORDER BY line_order', (rev['id'],)).fetchall()
    c.close()
    assert rev['entry_date'] == '2026-03-31' and rev['label'].startswith('Contrepassation')
    assert [(l['account_code'], l['debit'], l['credit']) for l in lines] == [('606100', 0.0, 120.0), ('401000', 120.0, 0.0)]
    post(client, f'/comptabilite/ecritures/{eid}/contrepasser', {'reversal_date': '2026-04-01'})
    assert 'déjà été contrepassée' in ' '.join(flashes(client))
    post(client, f"/comptabilite/ecritures/{rev['id']}/contrepasser", {'reversal_date': '2026-04-01'})
    assert 'ne peut pas elle-même' in ' '.join(flashes(client))


def test_entry_in_closed_period_is_refused(app):
    client, _, org, _ = signup(app, 'Cloture SAS')
    post(client, '/comptabilite/cloture', {'closed_until': '2026-01-31'})
    r = _entry(client, [('606100', '50', ''), ('401000', '', '50')], entry_date='2026-01-15', label='Trop tard')
    assert 'clôturée' in r.get_data(as_text=True)


# ------------------------------------------------------------------ grand livre / balance
def test_ledger_report_running_balance_and_trial_balance(app):
    client, _, org, _ = signup(app, 'Livre SAS')
    _entry(client, [('512000', '1000', ''), ('101300', '', '1000')], entry_date='2026-01-10', label='Capital')
    _entry(client, [('606100', '200', ''), ('512000', '', '200')], entry_date='2026-02-10', label='Fournitures')
    _entry(client, [('411000', '600', ''), ('706000', '', '600')], entry_date='2026-03-05', label='Vente',
           tiers_0='Dupont SARL')
    html = client.get('/comptabilite/grand-livre?du=2026-02-01&au=2026-12-31&compte_de=512000&compte_a=512000').get_data(as_text=True)
    assert 'Report au 01/02/2026' in html and '1\u00a0000,00' in html and '800,00' in html
    c = tenant(org)
    gl = L.general_ledger(c, '2026-02-01', '2026-12-31', None, '512000', '512000')
    tb = L.trial_balance(c, '2026-01-01', '2026-12-31')
    clients = L.trial_balance(c, '2026-01-01', '2026-12-31', kind='clients')
    c.close()
    acc = gl['accounts'][0]
    assert acc['report'] == 1000.0 and acc['lines'][-1]['running'] == 800.0
    assert tb['totals']['balanced'] and tb['totals']['debit'] == tb['totals']['credit'] == 1800.0
    assert [l['key'] for l in clients['lines']] == ['Dupont SARL'] and clients['lines'][0]['closing_debit'] == 600.0
    for url in ('/comptabilite/balance', '/comptabilite/balance?type=clients', '/comptabilite/balance?type=fournisseurs'):
        assert client.get(url).status_code == 200
    x = client.get('/comptabilite/grand-livre?format=csv&du=2026-01-01&au=2026-12-31')
    assert x.status_code == 200 and 'Compte' in x.get_data().decode('utf-8-sig')
    x = client.get('/comptabilite/balance?format=xlsx&du=2026-01-01&au=2026-12-31')
    assert x.status_code == 200 and x.data[:2] == b'PK'


# ------------------------------------------------------------------ reprise FEC
def test_fec_import_entries_then_duplicate_refused(app):
    client, _, org, _ = signup(app, 'Reprise SAS')
    r = post(client, '/comptabilite/reprise', {'mode': 'ecritures', 'fec': (io.BytesIO(SAMPLE_FEC), '123456789FEC20251231.txt')},
             content_type='multipart/form-data')
    assert r.status_code == 302
    c = tenant(org)
    n = c.execute("SELECT COUNT(*) n FROM accounting_entries WHERE piece_ref IN ('F001','V1')").fetchone()['n']
    journal = c.execute("SELECT * FROM accounting_journals WHERE code='VT'").fetchone()
    aux = c.execute("SELECT auxiliary_name FROM accounting_entry_lines WHERE account_code='411000' AND auxiliary_name='Dupont SARL'").fetchone()
    c.close()
    assert n == 2 and journal and journal['journal_type'] == 'OD' and aux
    r = post(client, '/comptabilite/reprise', {'mode': 'ecritures', 'fec': (io.BytesIO(SAMPLE_FEC), 'again.txt')},
             content_type='multipart/form-data')
    assert 'déjà été importé' in r.get_data(as_text=True)


def test_fec_import_opening_balances_posts_result(app):
    _, _, org, _ = signup(app, 'Soldes SAS')
    c = tenant(org)
    res = L.import_fec(c, SAMPLE_FEC, 'fec.txt', mode='soldes')
    e = c.execute("SELECT * FROM accounting_entries WHERE journal_code='AN'").fetchone()
    lines = {l['account_code']: (l['debit'], l['credit']) for l in
             c.execute('SELECT * FROM accounting_entry_lines WHERE entry_id=?', (e['id'],)).fetchall()}
    c.close()
    assert res['entries'] == 1 and e['entry_date'] == '2025-01-21'
    assert lines['411000'] == (1200.0, 0.0) and lines['512000'] == (5000.0, 0.0)
    assert lines['101300'] == (0.0, 5000.0) and lines['445710'] == (0.0, 200.0)
    assert lines['120000'] == (0.0, 1000.0)  # bénéfice repris en attente d'affectation
    assert '706000' not in lines


def test_fec_import_rejects_bad_files_atomically(app):
    _, _, org, _ = signup(app, 'Mauvais SAS')
    c = tenant(org)
    try:
        with pytest.raises(AccountingError, match='en-tête'):
            L.import_fec(c, b'a;b;c\n1;2;3\n', 'x.csv')
        bad = fec("OD|OD|9|20250101|606100|Achats|||P|20250101|Achat|100,00|0,00||||",
                  "OD|OD|9|20250101|401000|Fournisseurs|||P|20250101|Achat|0,00|90,00||||")
        with pytest.raises(AccountingError, match='déséquilibrées'):
            L.import_fec(c, SAMPLE_FEC + bad[len(FEC_HEADER) + 2:], 'mixed.txt')
        assert c.execute('SELECT COUNT(*) n FROM accounting_entries').fetchone()['n'] == 0
        c.execute("INSERT INTO accounting_entity_closure(entity_key,closed_until) VALUES(0,'2025-06-30')")
        c.commit()
        with pytest.raises(AccountingError, match='clôturée'):
            L.import_fec(c, SAMPLE_FEC, 'closed.txt')
        assert c.execute('SELECT COUNT(*) n FROM accounting_entries').fetchone()['n'] == 0
    finally:
        c.close()


def test_fec_montant_sens_variant_and_tab_separator():
    data = ("JournalCode\tJournalLib\tEcritureNum\tEcritureDate\tCompteNum\tCompteLib\tEcritureLib\tMontant\tSens\n"
            "AC\tAchats\tA1\t2025-03-01\t606100\tAchats\tAchat\t50,00\tD\n"
            "AC\tAchats\tA1\t2025-03-01\t401000\tFournisseurs\tAchat\t50,00\tC\n").encode('iso-8859-15')
    parsed = L.parse_fec(data)
    assert len(parsed['entries']) == 1 and parsed['entries'][0]['lines'][1]['credit'] == 50.0


# ------------------------------------------------------------------ accès
def test_pages_render_and_starter_is_locked(app):
    client, _, _, _ = signup(app, 'Pages SAS')
    for url in ('/comptabilite/ecritures/nouvelle', '/comptabilite/grand-livre', '/comptabilite/balance', '/comptabilite/reprise'):
        assert client.get(url).status_code == 200, url
    nav = client.get('/').get_data(as_text=True)
    assert 'Saisir une écriture' in nav and 'Grand livre' in nav
    starter, _, _, _ = signup(app, 'Starter SAS', plan='STARTER')
    r = starter.get('/comptabilite/grand-livre')
    assert r.status_code == 302 and '/billing' in r.headers['Location']
