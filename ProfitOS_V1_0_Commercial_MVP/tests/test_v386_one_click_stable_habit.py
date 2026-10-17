from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
B = (ROOT / "profitos" / "routes" / "bank_sync.py").read_text(encoding="utf-8")
T = (ROOT / "templates" / "banking.html").read_text(encoding="utf-8")


def test_stable_habit_threshold_remains_conservative():
    # v393 conserve le seuil v385/v386 et autorise aussi la famille salaire
    # lorsqu'elle est cohérente.
    assert "confirmations >= 4 and score >= 90" in B
    assert "source in ('directionnelle', 'famille salaire')" in B
    assert "automation_eligible=" in B


def test_one_click_stable_habit_keeps_explicit_user_action():
    # Une habitude stable ne doit jamais être comptabilisée silencieusement.
    assert "Valider" in T
