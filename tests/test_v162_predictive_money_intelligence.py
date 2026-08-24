from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def test_v162_predictive_engine_present():
    t=(ROOT/'profitos/routes/money_hunter.py').read_text(encoding='utf-8')
    assert 'def _payment_intelligence(' in t
    assert 'payment_risk_score' in t
    assert 'next_best_action' in t
    assert 'recovery_factor' in t

def test_v162_uses_observed_history_and_learning_loop():
    t=(ROOT/'profitos/routes/money_hunter.py').read_text(encoding='utf-8')
    assert 'FROM invoices GROUP BY customer' in t
    assert "a.status='SENT'" in t
    assert "a.status='DONE'" in t
    assert "a.status='CANCELLED'" in t
    assert "'OBSERVÉ'" in t
    assert "'INSUFFISANT'" in t

def test_v162_ui_explains_predictive_signals():
    t=(ROOT/'templates/money_hunter.html').read_text(encoding='utf-8')
    assert 'Risque paiement' in t
    assert 'Next Best Action' in t
    assert 'evidence_level' in t

def test_v162_version():
    t=(ROOT/'profitos/config.py').read_text(encoding='utf-8')
    assert 'APP_VERSION = "1.6.9"' in t
