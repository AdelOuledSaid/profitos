from pathlib import Path
import ast

ROOT=Path(__file__).resolve().parents[1]
INV=(ROOT/'profitos/routes/invoicing.py').read_text(encoding='utf-8')
RUNTIME=(ROOT/'profitos/runtime.py').read_text(encoding='utf-8')

def test_invoicing_list_is_registered_inside_register():
    tree=ast.parse(INV)
    register=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='register')
    names={n.name for n in register.body if isinstance(n,ast.FunctionDef)}
    assert '_purchase_paid_total' in names
    assert '_purchase_balance' in names
    assert 'purchase_mark_paid' in names
    assert 'invoicing_list' in names
    assert 'invoicing_new' in names

def test_purchase_budget_migration_is_postgresql_compatible():
    assert 'INSERT OR REPLACE INTO purchase_budgets_v24' not in RUNTIME
    assert 'INSERT INTO purchase_budgets_v24' in RUNTIME
    assert 'ON CONFLICT(category,entity_key) DO UPDATE SET' in RUNTIME
