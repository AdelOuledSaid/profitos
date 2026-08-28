from pathlib import Path


def test_decision_constraint_js_targets_checkboxes_not_hidden_inputs():
    js = Path("static/app.js").read_text(encoding="utf-8")
    assert "input[type=\"checkbox\"][name=\"allow_delay\"]" in js
    assert "input[type=\"checkbox\"][name=\"allow_installments\"]" in js
    assert "input[type=\"checkbox\"][name=\"allow_financing\"]" in js


def test_decision_constraint_js_does_not_use_ambiguous_name_only_selectors():
    js = Path("static/app.js").read_text(encoding="utf-8")
    assert "form.querySelector('[name=\"allow_delay\"]')" not in js
    assert "form.querySelector('[name=\"allow_installments\"]')" not in js
    assert "form.querySelector('[name=\"allow_financing\"]')" not in js
