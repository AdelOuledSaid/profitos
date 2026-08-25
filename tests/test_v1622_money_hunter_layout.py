from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def test_money_hunter_has_single_recommendation_list():
    t=(ROOT/'templates/money_hunter.html').read_text(encoding='utf-8')
    assert '<h2>Les prochaines décisions recommandées</h2>' not in t
    assert 'for r in brief.recommendations' in t
    assert 'money-priorities' in t

def test_money_hunter_css_cache_busted():
    t=(ROOT/'templates/base.html').read_text(encoding='utf-8')
    # Le versionnage manuel ('v=1622') a été remplacé par un cache-busting automatique
    # basé sur la date de modification du fichier (voir profitos.runtime.asset_url).
    assert "asset_url('style.css')" in t

def test_money_hunter_v1622_css_present():
    t=(ROOT/'static/style.css').read_text(encoding='utf-8')
    assert 'V1.6.2.2 — Money Hunter layout + cache refresh' in t
    assert '.money-priorities .money-decision' in t
