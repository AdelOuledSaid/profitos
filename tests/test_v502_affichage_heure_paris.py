"""v502 — Les horodatages s'affichent à l'heure de Paris.

Les horodatages techniques (transmissions, événements) sont enregistrés en UTC par
runtime.now et s'affichaient sans conversion : une transmission faite à 19 h 05 apparaissait
à 18 h 05 en hiver, à 17 h 05 en été. Constaté sur l'historique de facturation électronique.
"""
from profitos.runtime import fr_date


def test_heure_d_hiver():
    """Novembre : Paris est à UTC+1."""
    assert fr_date('2026-11-14T18:05:12+00:00', True) == '14/11/2026 19:05'
    assert fr_date('2026-11-14T18:05:12Z', True) == '14/11/2026 19:05'


def test_heure_d_ete():
    """Juillet : Paris est à UTC+2. Le décalage n'est pas constant, d'où la conversion par
    fuseau plutôt que par ajout d'une heure."""
    assert fr_date('2026-07-14T18:05:00Z', True) == '14/07/2026 20:05'


def test_la_date_change_aussi_quand_il_le_faut():
    """23 h 30 UTC, c'est le lendemain à Paris : la date affichée doit suivre, sans quoi une
    opération apparaît la veille de son jour réel."""
    assert fr_date('2026-11-14T23:30:00Z', True) == '15/11/2026 00:30'
    assert fr_date('2026-11-14T23:30:00Z') == '15/11/2026'


def test_un_horodatage_sans_fuseau_n_est_pas_decale():
    """Déjà local : le convertir le décalerait une seconde fois."""
    assert fr_date('2026-11-14T18:05:00', True) == '14/11/2026 18:05'
    assert fr_date('2026-11-14 18:05:00', True) == '14/11/2026 18:05'


def test_les_dates_simples_sont_intactes():
    assert fr_date('2026-11-14') == '14/11/2026'
    assert fr_date('2026-11-14', True) == '14/11/2026'


def test_une_valeur_illisible_ne_fait_pas_echouer_la_page():
    """Le filtre n'a jamais levé d'exception sur une valeur inattendue, et la conversion de
    fuseau ne doit pas changer cela : une date mal formée reste affichée telle quelle, une
    date impossible mais bien formée reste découpée mécaniquement comme auparavant."""
    assert fr_date('bientôt') == 'bientôt'
    assert fr_date('2026-13-45T99:99:99Z', True) == '45/13/2026 99:99'   # inchangé depuis toujours
    assert fr_date('') == ''
    assert fr_date(None) is None
