from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def test_demo_page_professional_layout():
    t=(ROOT/'templates/demo_request.html').read_text(encoding='utf-8')
    for x in ['DÉMO PERSONNALISÉE','Réserver ma démo','Secteur d’activité','Taille de l’entreprise','Besoin principal','Demander ma démo','demo.css']:
        assert x in t

def test_demo_css_responsive():
    t=(ROOT/'static/demo.css').read_text(encoding='utf-8')
    assert '.demo-layout' in t
    assert '.demo-form' in t
    assert '@media(max-width:620px)' in t
