from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def test_feature_decorator_exists():
    t=(ROOT/'profitos'/'feature_access.py').read_text(encoding='utf-8')
    assert 'def requires_feature(feature_name):' in t
    assert 'feature_enabled(plan, feature_name)' in t
    assert 'PLAN_FEATURE_BLOCKED' in t
    assert 'return redirect(url_for("billing"))' in t


def test_grow_refresh_is_protected():
    t=(ROOT/'profitos'/'routes'/'main.py').read_text(encoding='utf-8')
    block=t.split("def grow_refresh():",1)[0][-500:]
    assert "@requires_feature('advanced_features')" in block


def test_company_save_does_not_bypass_feature_gate():
    t=(ROOT/'profitos'/'routes'/'main.py').read_text(encoding='utf-8')
    assert "feature_enabled(org['plan'],'advanced_features')" in t
    assert 'sync_grow()' in t


def test_dce_history_hidden_without_advanced_features():
    t=(ROOT/'profitos'/'routes'/'main.py').read_text(encoding='utf-8')
    assert "can_use_advanced=bool(org and feature_enabled(org['plan'],'advanced_features'))" in t
    assert "kind=='GROW' and can_use_advanced" in t


def test_action_send_is_protected():
    t=(ROOT/'profitos'/'routes'/'actions.py').read_text(encoding='utf-8')
    block=t.split("def action_send(aid):",1)[0][-500:]
    assert "@requires_active_plan" in block
    assert "@requires_feature('advanced_features')" in block


def test_dce_upload_is_protected():
    t=(ROOT/'profitos'/'routes'/'dce.py').read_text(encoding='utf-8')
    block=t.split("def dce_upload(opportunity_id):",1)[0][-500:]
    assert "@requires_active_plan" in block
    assert "@requires_feature('advanced_features')" in block
