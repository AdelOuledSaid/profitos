from pathlib import Path
from jinja2 import Environment

ROOT=Path(__file__).resolve().parents[1]
B=(ROOT/'profitos/routes/bank_sync.py').read_text(encoding='utf-8')
T=(ROOT/'templates/banking.html').read_text(encoding='utf-8')


def test_template_parses():
    Environment().parse(T)


def test_learning_is_direction_aware():
    assert "direction='debit' if float(amount) < 0 else 'credit'" in B
    assert "signature=_learning_pattern(tx['label'], tx['amount'])" in B


def test_learning_requires_exact_pattern_and_blocks_conflicts():
    assert "(r['pattern'] or '') == pattern" in B
    assert "Habitudes contradictoires : aucune proposition automatique" in B
    assert "len(choices) != 1" in B


def test_ui_exposes_confidence_level_and_keeps_human_validation():
    assert "Confiance {{ sug.confidence_label" in T
    assert 'Validation humaine requise.' in T
