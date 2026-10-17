"""Un CREATE TABLE passé par execute() (et non executescript()) doit être traduit pour
PostgreSQL : AUTOINCREMENT -> SERIAL, REAL -> DOUBLE PRECISION (erreur vue en production
sur la table job_runs des tâches planifiées)."""
from profitos.db import _translate_statement


def test_create_table_via_execute_is_translated():
    sql = _translate_statement('''CREATE TABLE IF NOT EXISTS job_runs(
        id INTEGER PRIMARY KEY AUTOINCREMENT, amount REAL, name TEXT)''')
    assert 'AUTOINCREMENT' not in sql and 'SERIAL PRIMARY KEY' in sql
    assert 'DOUBLE PRECISION' in sql and ' REAL' not in sql


def test_other_statements_untouched():
    assert _translate_statement('SELECT 1') == 'SELECT 1'
