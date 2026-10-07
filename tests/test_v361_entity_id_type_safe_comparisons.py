"""PostgreSQL refuse de comparer un entier et du texte. Or entity_id est un ENTIER sur les tables
créées récemment (paiements, avoirs) mais du TEXTE sur les bases anciennes où la colonne a été
ajoutée par migration (factures émises et d'achat). Une comparaison « colonne IS colonne » entre
les deux provoquait « operator does not exist: integer = text » (page des créances en erreur 500).
SQLite ne s'en plaint pas, donc la suite de tests locale ne pouvait pas le voir."""
import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORTABLE = "COALESCE(CAST({a} AS TEXT),'') = COALESCE(CAST({b} AS TEXT),'')"


def _source(rel):
    return (ROOT / rel).read_text(encoding='utf-8')


def test_no_column_to_column_is_comparison_on_entity_id():
    pattern = re.compile(r"entity_id\s+IS\s+(?!NOT\b|NULL\b|DISTINCT\b)[A-Za-z_]\w*\.")
    offenders = []
    for path in list((ROOT / 'profitos').rglob('*.py')):
        for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{number}")
    assert not offenders, "comparaison entier/texte possible sur PostgreSQL : " + ", ".join(offenders)


def test_receivables_cash_intelligence_and_ereporting_use_the_portable_form():
    inv = _source('profitos/routes/invoicing.py')
    assert PORTABLE.format(a='cn.entity_id', b='i.entity_id') in inv
    assert PORTABLE.format(a='p.entity_id', b='i.entity_id') in inv
    cash = _source('profitos/routes/cash_intelligence.py')
    assert PORTABLE.format(a='p.entity_id', b='oi.entity_id') in cash
    assert PORTABLE.format(a='p.entity_id', b='pi.entity_id') in cash
    assert PORTABLE.format(a='i.entity_id', b='p.entity_id') in _source('profitos/ereporting.py')


def test_portable_predicate_matches_across_text_and_integer_columns():
    conn = sqlite3.connect(':memory:')
    conn.execute("CREATE TABLE inv(id INTEGER, entity_id TEXT)")
    conn.execute("CREATE TABLE pay(invoice_id INTEGER, entity_id INTEGER, amount REAL)")
    conn.executemany("INSERT INTO inv VALUES(?,?)", [(1, None), (2, '5'), (3, '6')])
    conn.executemany("INSERT INTO pay VALUES(?,?,?)", [(1, None, 10), (2, 5, 20), (3, 5, 30)])
    rows = dict(conn.execute(
        "SELECT i.id, COALESCE((SELECT SUM(p.amount) FROM pay p WHERE p.invoice_id=i.id AND "
        + PORTABLE.format(a='p.entity_id', b='i.entity_id') + "),0) FROM inv i").fetchall())
    assert rows == {1: 10.0, 2: 20.0, 3: 0}
