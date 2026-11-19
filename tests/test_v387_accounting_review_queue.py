from pathlib import Path
from jinja2 import Environment

ROOT = Path(__file__).resolve().parents[1]
B = (ROOT / "profitos/routes/bank_sync.py").read_text(encoding="utf-8")
T = (ROOT / "templates/banking.html").read_text(encoding="utf-8")


def test_template_parses():
    Environment().parse(T)


def test_review_queue_exists_and_prioritizes_pending_operations():
    assert "accounting_review_queue" in B
    assert "transaction_states[t['id']]['state'] != 'pending'" in B
    assert "if sug.get('automation_eligible')" in B


def test_ui_exposes_accounting_review_queue_and_human_control():
    # v518 — ce qui doit tenir, c'est la file et la promesse de contrôle humain, pas leur
    # formulation : un libellé figé par un test est un libellé qu'on n'ose plus améliorer.
    assert "accounting_review_queue.stable" in T and "accounting_review_queue.confirm" in T
    assert "Habitudes stables :" in T
    assert "À confirmer :" in T
    promesse = [l for l in T.split("\n") if "criture" in l and "sans votre" in l]
    assert promesse, "l'écran doit affirmer qu'aucune écriture n'est enregistrée sans l'accord de l'utilisateur"
