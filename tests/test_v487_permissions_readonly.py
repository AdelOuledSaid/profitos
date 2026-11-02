"""v487: garde-fous en lecture seule pour les opérations sensibles.

Tests structurels indépendants de Flask; les tests HTTP restent à exécuter
avec une base de test isolée et les dépendances du projet.
"""
import ast
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parent.parent
CASES = [
    ("profitos/routes/mission_control.py", "mission_control_execute"),
    ("profitos/routes/mission_control.py", "mission_control_track"),
    ("profitos/routes/mission_control.py", "mission_control_decision"),
    ("profitos/routes/expert_network.py", "expert_mission_action"),
]

@pytest.mark.parametrize("filename,function", CASES)
def test_readonly_guard_precedes_business_logic(filename, function):
    tree = ast.parse((ROOT / filename).read_text(encoding="utf-8"))
    matches = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == function]
    assert len(matches) == 1
    first = matches[0].body[0]
    assert isinstance(first, ast.If)
    assert isinstance(first.test, ast.Compare)
    assert isinstance(first.test.left, ast.Call)
    assert isinstance(first.test.left.func, ast.Name) and first.test.left.func.id == "current_role"
    assert len(first.test.ops) == 1 and isinstance(first.test.ops[0], ast.Eq)
    assert isinstance(first.test.comparators[0], ast.Constant) and first.test.comparators[0].value == "LECTURE"
    assert len(first.body) == 1 and isinstance(first.body[0], ast.Expr)
    call = first.body[0].value
    assert isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "abort"
    assert len(call.args) == 1 and isinstance(call.args[0], ast.Constant) and call.args[0].value == 403
