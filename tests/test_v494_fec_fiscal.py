"""v494 — Contrôles fiscaux sur un historique repris par FEC.

Défauts trouvés avec le dossier de test « Atelier Lumen » :
1. le FEC ne transporte ni le taux ni la base de TVA : tout le chiffre d'affaires paraissait
   « sans TVA collectée » et une facture comptée à 19,6 % passait inaperçue ;
2. les liquidations de TVA reprises n'étaient pas reconnues : la TVA impayée était surestimée ;
3. le solde de l'IS N-1 payé en mai était compté comme un acompte de N ;
4. une facture fournisseur saisie deux fois n'était signalée nulle part."""
from datetime import date

import pytest

import profitos.runtime as rt
from profitos import corporate_tax as CT
from profitos import fiscal_controls as FC
from profitos import ledger as L
from test_v470_encaissement import app, signup  # noqa: F401

TODAY = date(2026, 11, 7)
HEAD = ('JournalCode|JournalLib|EcritureNum|EcritureDate|CompteNum|CompteLib|CompAuxNum|CompAuxLib|PieceRef|'
        'PieceDate|EcritureLib|Debit|Credit|EcritureLet|DateLet|ValidDate|Montantdevise|Idevise')


def fec(entries):
    """entries : (journal, n°, date AAAAMMJJ, pièce, libellé, [(compte, aux, débit, crédit)])."""
    rows = [HEAD]
    for j, num, d, piece, label, lines in entries:
        for acc, aux, deb, cre in lines:
            rows.append('|'.join([j, j, f'{j}{num:04d}', d, acc, acc, aux or '', aux or '', piece, d, label,
                                  f'{deb:.2f}'.replace('.', ','), f'{cre:.2f}'.replace('.', ','), '', '', d, '', '']))
    return ('\r\n'.join(rows) + '\r\n').encode('utf-8')


def sale(num, d, piece, ht, vat):
    return ('VE', num, d, piece, f'Facture {piece}', [('411000', 'C1', ht + vat, 0), ('706000', None, 0, ht), ('445710', None, 0, vat)])


def bank(num, d, piece, label, account, amount):
    return ('BQ', num, d, piece, label, [(account, None, amount, 0), ('512000', None, 0, amount)])


IS_2025 = 20000.0
ACOMPTE = 5000.0

FEC_2025 = fec([
    ('AN', 1, '20250101', 'AN', 'Reprise', [('512000', None, 50000, 0), ('101000', None, 0, 50000)]),
    sale(1, '20250315', 'FA-2025-001', 100000, 20000),
    ('OD', 1, '20251231', 'IS2025', 'IS 2025', [('695000', None, IS_2025, 0), ('444000', None, 0, IS_2025)]),
])
FEC_2026 = fec([
    sale(1, '20260210', 'FA-2026-001', 50000, 10000),
    sale(2, '20260504', 'FA-2026-002', 4500, 882),                       # 19,6 % au lieu de 20 %
    sale(3, '20260720', 'FA-2026-003', 2000, 200),                        # 10 %
    ('AC', 1, '20260617', 'F001-77', 'Facture F001-77', [('601000', None, 8000, 0), ('445660', None, 1600, 0),
                                                         ('401000', 'F001', 0, 9600)]),
    ('AC', 2, '20260617', 'F001-77', 'Facture F001-77', [('601000', None, 8000, 0), ('445660', None, 1600, 0),
                                                         ('401000', 'F001', 0, 9600)]),   # saisie en double
    ('OD', 1, '20260731', 'TVA-07', 'Liquidation TVA 07', [('445710', None, 1000, 0), ('445510', None, 0, 1000)]),
    ('OD', 2, '20260930', 'TVA-09', 'Liquidation TVA 09', [('445710', None, 600, 0), ('445510', None, 0, 600)]),
    bank(1, '20260316', 'ACO1', 'Acompte IS 1', '444000', ACOMPTE),
    bank(2, '20260515', 'SOLDE', 'Solde IS 2025', '444000', IS_2025),
    bank(3, '20260615', 'ACO2', 'Acompte IS 2', '444000', ACOMPTE),
    # l'acompte du 15/09 n'est pas payé ; la TVA de juillet (1 000 €) non plus
])


@pytest.fixture
def books(app):
    cl, org = signup(app)
    c = rt.tenant_cx_direct(org)
    L.import_fec(c, FEC_2025, '2025.txt', mode='ecritures')
    L.import_fec(c, FEC_2026, '2026.txt', mode='ecritures')
    yield c
    c.close()


def checks(c):
    return {ck['code']: ck for s in FC.review(c, None, TODAY)['sections'] for ck in s['checks']}


def test_taux_et_base_deduits_a_l_import(books):
    rows = books.execute("""SELECT e.piece_ref, l.vat_rate, l.vat_base FROM accounting_entry_lines l
                            JOIN accounting_entries e ON e.id=l.entry_id WHERE l.account_code='445710'
                            AND COALESCE(e.source_type,'')<>'vat_liquidation' ORDER BY e.entry_date""").fetchall()
    got = {r['piece_ref']: (r['vat_rate'], r['vat_base']) for r in rows}
    assert got['FA-2026-001'] == (20.0, 50000.0)
    assert got['FA-2026-002'] == (20.0, 4500.0)          # 19,6 % ramené au taux légal ; la TVA n'est pas modifiée
    assert got['FA-2026-003'] == (10.0, 2000.0)


def test_chiffre_d_affaires_rapproche_et_facture_au_mauvais_taux(books):
    ck = checks(books)
    assert ck['tva_ca']['ok'] is True                     # plus de fausse alerte « CA sans TVA »
    assert ck['tva_ca:en_cours']['ok'] is True
    assert 'tva_non_ventilee' not in ck and 'tva_non_ventilee:en_cours' not in ck
    taux = ck['tva_taux:en_cours']
    assert taux['ok'] is None and 'FA-2026-002' in taux['detail'] and '882,00' in taux['detail'].replace(' ', ' ')


def test_liquidations_reprises_et_tva_impayee(books):
    n = books.execute("SELECT COUNT(*) n FROM accounting_entries WHERE source_type='vat_liquidation'").fetchone()['n']
    assert n == 2
    ck = checks(books)['tva_impayee']
    assert ck['ok'] is None and ck['amount'] == 1000.0   # juillet seulement, pas la dernière liquidation


def test_solde_is_n_moins_1_n_est_pas_un_acompte(books):
    assert CT.acomptes_from_ledger(books, date(2026, 1, 1), TODAY, None) == 2 * ACOMPTE
    ck = checks(books)['is_acomptes']
    assert ck['ok'] is None and ck['amount'] > 0


def test_facture_fournisseur_en_double(books):
    ck = checks(books)['achat_doublon:en_cours']
    assert ck['ok'] is None and ck['amount'] == 9600.0 and 'F001-77' in ck['detail']
    assert checks(books)['achat_doublon']['ok'] is True   # exercice clos : rien


def test_doublon_signale_dans_mission_control(books, app):
    with app.test_request_context('/'):
        found = [f for f in FC.detect(books, None, TODAY) if 'achat_doublon' in f['key']]
    assert len(found) == 1 and found[0]['severity'] == 'important'


def test_liquidation_deja_passee_dans_veqalio_ne_bloque_pas_l_import(app):
    cl, org = signup(app)
    c = rt.tenant_cx_direct(org)
    L.import_fec(c, fec([('OD', 1, '20260731', 'TVA-07', 'Liquidation', [('445710', None, 10, 0), ('445510', None, 0, 10)])]),
                 'a.txt', mode='ecritures')
    # même date, autre fichier : l'écriture est reprise comme écriture ordinaire
    L.import_fec(c, fec([('OD', 9, '20260731', 'TVA-07b', 'Liquidation bis', [('445710', None, 5, 0), ('445510', None, 0, 5)])]),
                 'b.txt', mode='ecritures')
    assert c.execute("SELECT COUNT(*) n FROM accounting_entries WHERE journal_code='OD'").fetchone()['n'] == 2
    c.close()
