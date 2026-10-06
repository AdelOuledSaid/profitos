from pathlib import Path
from jinja2 import Environment

ROOT = Path(__file__).resolve().parents[1]
B = (ROOT / "profitos/routes/bank_sync.py").read_text(encoding="utf-8")
T = (ROOT / "templates/banking.html").read_text(encoding="utf-8")


def test_template_parses():
    Environment().parse(T)


def test_stable_habit_threshold_remains_conservative():
    assert "confirmations >= 4 and score >= 90" in B
    assert "source == 'directionnelle'" in B


def test_stable_habit_prefills_all_accounting_fields():
    assert 'value="{{ sug.account_code if sug and sug.account_code else \'\' }}"' in T
    assert 'value="{{ sug.vat_rate if sug and sug.vat_rate is not none else \'\' }}"' in T
    assert "sug.category==cat" in T


def test_one_click_is_only_for_eligible_stable_habit():
    assert "{% if sug and sug.automation_eligible %}Valider en 1 clic{% else %}Valider{% endif %}" in T
    assert "Habitude stable : catégorie, compte et TVA préremplis. Vérifiez puis validez en 1 clic." in T


def test_no_silent_auto_posting():
    # v386 changes UX only: submission is still the explicit user POST.
    assert 'method="post" action="{{ url_for(\'banking_transaction_categorize\',tx_id=t.id) }}"' in T
