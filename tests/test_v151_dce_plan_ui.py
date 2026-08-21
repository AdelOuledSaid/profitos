from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def test_dce_plan_gate_present():
    t=(ROOT/'templates'/'detail.html').read_text(encoding='utf-8')
    assert "advanced_plan = auth_org and auth_org.plan in ('PRO','BUSINESS')" in t
    assert "Analyse DCE · Pro" in t
    assert "L'analyse automatique des DCE est disponible avec les formules Pro et Business." in t


def test_dce_upload_form_preserved():
    t=(ROOT/'templates'/'detail.html').read_text(encoding='utf-8')
    assert "url_for('dce_upload',opportunity_id=o.id)" in t
    assert 'name="file"' in t
    assert "Analyser le document" in t


def test_dce_results_inside_advanced_plan_gate():
    t=(ROOT/'templates'/'detail.html').read_text(encoding='utf-8')
    dce_marker=t.index("TENDER INTELLIGENCE")
    gate=t.index("{% if advanced_plan %}", dce_marker)
    results=t.index("{% for d in dce_items %}")
    assert gate < results
