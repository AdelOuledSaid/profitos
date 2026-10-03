from pathlib import Path
T=Path("profitos/routes/accounting.py").read_text(encoding="utf-8")

def test_vat_post_actions_are_explicitly_allowlisted():
    block=T.split("def vat_summary():",1)[1].split("\n    @app.",1)[0]
    assert "allowed_actions = {'prepare','lock','file','reopen','fiscal_prepare','fiscal_validate'}" in block
    assert "if action not in allowed_actions:" in block
    assert "abort(400)" in block

def test_locked_vat_cannot_be_silently_downgraded_to_prepared():
    block=T.split("def vat_summary():",1)[1].split("\n    @app.",1)[0]
    assert "declaration['status']=='locked' and action=='prepare'" in block
    assert "Cette période TVA est verrouillée. Rouvrez-la avant de la modifier." in block

def test_fiscal_workpaper_actions_are_explicitly_allowlisted():
    block=T.split("def fiscal_workpapers():",1)[1].split("\n    @app.",1)[0]
    assert "if action not in {'prepare','validate'}:" in block
    assert "abort(400)" in block
