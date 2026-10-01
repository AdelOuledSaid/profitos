from profitos.db import _translate_statement, _sqlite_placeholders_to_pg


def pg(sql):
    return _sqlite_placeholders_to_pg(_translate_statement(sql))


def test_is_parameter_becomes_null_safe_postgres_equality():
    out = pg("SELECT * FROM t WHERE entity_id IS ?")
    assert "entity_id IS NOT DISTINCT FROM %s" in out
    assert "entity_id IS %s" not in out


def test_is_not_parameter_becomes_null_safe_postgres_inequality():
    out = pg("SELECT * FROM t WHERE entity_id IS NOT ?")
    assert "entity_id IS DISTINCT FROM %s" in out


def test_qualified_identifier_is_identifier_is_translated():
    out = pg("SELECT 1 FROM payments p JOIN invoices i ON p.entity_id IS i.entity_id")
    assert "p.entity_id IS NOT DISTINCT FROM i.entity_id" in out

def test_null_predicate_is_untouched():
    out = pg("SELECT * FROM t WHERE deleted_at IS NULL")
    assert "deleted_at IS NULL" in out
