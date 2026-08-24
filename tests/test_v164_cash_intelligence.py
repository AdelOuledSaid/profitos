from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def test_v164_version():
    assert 'APP_VERSION = "1.6.6"' in (ROOT/'profitos/config.py').read_text(encoding='utf-8')

def test_cash_intelligence_registered():
    t=(ROOT/'profitos/__init__.py').read_text(encoding='utf-8')
    assert 'cash_intelligence.register(app)' in t

def test_cash_intelligence_route_and_scenarios():
    t=(ROOT/'profitos/routes/cash_intelligence.py').read_text(encoding='utf-8')
    assert "@app.route('/cash-intelligence',methods=['GET','POST'])" in t
    assert 'for delay in (7,30,60)' in t
    assert 'daily_burn=observed_90/90.0' in t

def test_cash_balance_is_persisted():
    t=(ROOT/'profitos/runtime.py').read_text(encoding='utf-8')
    assert 'CREATE TABLE IF NOT EXISTS financial_settings' in t

def test_cash_intelligence_does_not_claim_unknown_future_expenses():
    t=(ROOT/'templates/cash_intelligence.html').read_text(encoding='utf-8')
    assert 'Les dépenses futures non encore enregistrées' in t
    assert 'ne sont pas inventées' in t

def test_cash_intelligence_in_navigation():
    assert "url_for('cash_intelligence')" in (ROOT/'templates/base.html').read_text(encoding='utf-8')
