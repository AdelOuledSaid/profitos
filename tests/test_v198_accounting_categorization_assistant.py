from pathlib import Path
from jinja2 import Environment

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / "profitos/routes/bank_sync.py").read_text(encoding="utf-8")
TPL = (ROOT / "templates/banking.html").read_text(encoding="utf-8")


def test_template_parses():
    Environment().parse(TPL)


def test_learning_is_direction_aware_and_exact():
    assert "(r['pattern'] or '') == pattern" in SRC
    assert "consensus(directional)" in SRC


def test_conflicting_habits_do_not_create_automatic_proposal():
    assert "Habitudes contradictoires : aucune proposition automatique" in SRC


def test_ui_shows_score_and_requires_explicit_validation():
    assert 'Confiance {{ sug.confidence_label' in TPL
    assert '{{ sug.confidence_score }} %' in TPL
    assert 'Validation humaine requise.' in TPL
    assert 'name="account_code"' in TPL
    assert 'name="vat_rate"' in TPL
    # v386: stable habits use a Jinja conditional for the explicit submit label.
    assert "{% if sug and sug.automation_eligible %}Valider en 1 clic{% else %}Valider{% endif %}" in TPL


def test_stable_habit_still_requires_user_post():
    assert 'method="post"' in TPL
    assert "Habitude stable : catégorie, compte et TVA préremplis. Vérifiez puis validez en 1 clic." in TPL
