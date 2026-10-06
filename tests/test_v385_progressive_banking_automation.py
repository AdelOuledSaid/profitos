from pathlib import Path
from jinja2 import Environment

ROOT=Path(__file__).resolve().parents[1]
B=(ROOT/'profitos/routes/bank_sync.py').read_text(encoding='utf-8')
T=(ROOT/'templates/banking.html').read_text(encoding='utf-8')


def test_template_parses():
    Environment().parse(T)


def test_only_stable_directional_learning_is_automation_eligible():
    body=B[B.index('def _accounting_suggestion'):B.index('def _cfg')]
    assert "source == 'directionnelle'" in body
    assert 'confirmations >= 4' in body
    assert 'score >= 90' in body
    assert 'automation_eligible=False' in body


def test_ambiguous_and_legacy_rules_never_get_fast_path():
    body=B[B.index('def _accounting_suggestion'):B.index('def _cfg')]
    assert 'Habitudes contradictoires : aucune proposition automatique' in body
    assert "learned, ambiguous=consensus(legacy)" in body
    assert "automation_eligible=(source == 'directionnelle'" in body


def test_ui_explains_progressive_automation_without_auto_posting():
    assert 'Habitude stable : validation en un clic prête.' in T
    assert 'Validation humaine requise.' in T
    assert 'automation_eligible' in T
