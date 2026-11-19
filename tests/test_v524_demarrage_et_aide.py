"""v524 — Ce que voit un nouveau client, et ce qu'il peut trouver tout seul.

Le démarrage proposait sept étapes de même apparence, sans ordre ni raison, et la visite
guidée commençait par les relances d'un client qui n'avait encore aucune facture. Le
guide, lui, faisait une quarantaine d'écrans d'affilée, sans recherche.

Ces contrôles protègent les trois corrections : un chemin de démarrage ordonné, une
visite qui suit ce chemin, et un guide où l'on cherche au lieu de parcourir.
"""
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP_JS = (ROOT / 'static/app.js').read_text(encoding='utf-8')
HELP = (ROOT / 'templates/help.html').read_text(encoding='utf-8')
SEARCH_JS = (ROOT / 'static/help-search.js').read_text(encoding='utf-8')
DASHBOARD = (ROOT / 'templates/dashboard.html').read_text(encoding='utf-8')
MAIN = (ROOT / 'profitos/routes/main.py').read_text(encoding='utf-8')


# ---------------------------------------------------------------------------
# La visite guidée suit le chemin du démarrage
# ---------------------------------------------------------------------------

def test_the_tour_starts_with_the_company_profile():
    """Elle commençait par « Relances clients » : un écran vide pour un compte neuf, et
    une étape qui n'a de sens qu'une fois des factures émises."""
    etapes = APP_JS.split('var STEPS = [', 1)[1].split('];', 1)[0]
    premiere = etapes.split('{', 1)[1].split('}', 1)[0]
    assert 'data-tour="company"' in premiere, 'la visite doit commencer par les informations d’entreprise'
    seconde = etapes.split('}', 1)[1].split('{', 1)[1].split('}', 1)[0]
    assert 'data-tour="invoicing"' in seconde, 'puis mener à la première facture'


def test_the_tour_targets_exist_in_the_menu():
    """Une étape dont la cible n'existe pas est silencieusement sautée : la visite
    paraîtrait alors plus courte, sans que personne ne le remarque."""
    base = (ROOT / 'templates/base.html').read_text(encoding='utf-8')
    etapes = APP_JS.split('var STEPS = [', 1)[1].split('];', 1)[0]
    import re
    for cible in re.findall(r"data-tour=\\?\"([a-z_-]+)\\?\"", etapes):
        assert f'data-tour="{cible}"' in base, f'cible de visite absente du menu : {cible}'


# ---------------------------------------------------------------------------
# Le démarrage dit par où commencer, et pourquoi
# ---------------------------------------------------------------------------

def test_each_step_carries_its_reason_and_its_action():
    etapes = MAIN.split('onboarding_steps=[', 1)[1].split('onboarding_next=', 1)[0]
    assert etapes.count("'why':") == etapes.count("'label':"), \
        'chaque étape doit dire à quoi elle sert'
    assert etapes.count("'action':") == etapes.count("'label':"), \
        'chaque étape doit porter le libellé de son bouton'


def test_the_next_step_is_the_first_unfinished_one():
    assert "onboarding_next=next((s for s in onboarding_steps if not s['done']), None)" in MAIN


def test_the_dashboard_highlights_one_step_and_does_not_repeat_it():
    assert 'db-next' in DASHBOARD
    assert 'onboarding_next.why' in DASHBOARD and 'onboarding_next.action' in DASHBOARD
    # la pastille correspondante est retirée de la liste du dessous
    assert 'if not (onboarding_next and s.label == onboarding_next.label)' in DASHBOARD


# ---------------------------------------------------------------------------
# Le vocabulaire
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('fichier', [
    'profitos/routes/invoicing.py', 'profitos/company_profile.py',
    'profitos/teledeclaration.py', 'templates/onboarding.html',
])
def test_the_word_denomination_is_gone_from_what_users_read(fichier):
    """« Dénomination » reste dans les formulaires fiscaux officiels, où c'est le terme
    exact ; il a disparu des messages adressés à l'utilisateur, qui ne le comprenait pas."""
    texte = (ROOT / fichier).read_text(encoding='utf-8')
    for ligne in texte.splitlines():
        if 'énomination' not in ligne:
            continue
        assert 'flash(' not in ligne and 'errors.append(' not in ligne and '<label>' not in ligne, \
            f'{fichier} : « dénomination » encore visible → {ligne.strip()[:90]}'


def test_email_failure_messages_say_what_happened_in_plain_words():
    """« mode simulation » ne veut rien dire pour un client ; ce qui compte est que son
    document n'est pas parti, et ce qu'il peut faire à la place."""
    account = (ROOT / 'profitos/routes/account.py').read_text(encoding='utf-8')
    invoicing = (ROOT / 'profitos/routes/invoicing.py').read_text(encoding='utf-8')
    assert "L'e-mail de confirmation n'a pas pu partir" in account
    assert 'mode simulation' not in account.split('def signup', 1)[1][:4000]
    assert "Téléchargez son PDF pour la transmettre vous-même." in invoicing


# ---------------------------------------------------------------------------
# Le guide se cherche
# ---------------------------------------------------------------------------

def test_the_guide_has_a_search_field_and_shortcuts():
    assert 'id="help-search"' in HELP
    assert 'help-search-count' in HELP
    assert HELP.count('data-help-query=') >= 4, 'des entrées directes sur les questions fréquentes'
    assert 'help-search.js' in HELP


def test_the_search_script_is_external_as_the_security_policy_requires():
    """La politique de sécurité n'autorise que les scripts servis par le site : un
    <script> en ligne serait bloqué et la recherche ne fonctionnerait pas."""
    assert '<script>' not in HELP, 'aucun script en ligne dans le guide'
    assert 'src="{{ asset_url(\'help-search.js\') }}"' in HELP


def test_the_guide_remains_readable_without_the_script():
    """Rien ne doit dépendre du script pour lire le guide : il ne masque qu'à la frappe."""
    assert 'hidden = !visible' in SEARCH_JS
    assert 'if (!champ || !contenu) return;' in SEARCH_JS


def test_the_search_filters_the_table_of_contents_too():
    """Un sommaire qui continue d'annoncer des sections masquées est trompeur."""
    assert "sommaire.querySelectorAll('a[href^=\"#\"]')" in SEARCH_JS
    assert 'li.hidden' in SEARCH_JS


def test_the_search_ignores_accents():
    """« trésorerie » doit se trouver en tapant « tresorerie »."""
    assert "normalize('NFD')" in SEARCH_JS
