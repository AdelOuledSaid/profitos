from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def _function_block(text, function_name):
    match = re.search(
        rf"^def {re.escape(function_name)}\([^\n]*\):\n(?P<body>(?:^[ \t]+.*\n|^\s*\n)*)",
        text,
        flags=re.MULTILINE,
    )
    assert match, f"Fonction {function_name} introuvable"
    return match.group(0)


def test_paid_feature_requires_active_paid_status():
    text = (ROOT / "profitos" / "feature_access.py").read_text(encoding="utf-8")
    block = _function_block(text, "current_plan_is_paid")

    assert "is_paid_plan" in block
    assert "ACTIVE_PAID" in block
    assert "status" in block
    assert "plan" in block


def test_missing_stripe_is_fail_closed_in_production():
    text = (ROOT / "profitos" / "runtime.py").read_text(encoding="utf-8")
    block = _function_block(text, "org_has_access")

    assert "BILLING_ENABLED" in block
    assert "PROFITOS_ENV" in block
    assert "production" in block.lower()

    # En production, l'absence de Stripe ne doit pas aboutir à un `return True`
    # inconditionnel dans la branche `if not BILLING_ENABLED`.
    branch = re.search(
        r"if\s+not\s+BILLING_ENABLED\s*:\s*(?P<body>.*?)(?=^\s{4}(?:if|return)\b|\Z)",
        block,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert branch, "Branche `if not BILLING_ENABLED` introuvable"
    body = branch.group("body")
    assert "PROFITOS_ENV" in body
    assert "production" in body.lower()
