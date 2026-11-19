"""v523 — Audit de l'impôt sur les sociétés sur des dossiers de référence.

Chaque cas est calculé à la main d'après le barème en vigueur (taux normal 25 %, taux
réduit 15 % jusqu'à 42 500 € par période de douze mois pour les PME éligibles,
contribution sociale de 3,3 % au-delà de 763 000 € d'IS), puis comparé au calcul du
logiciel. Un écart ici se traduit par un montant faux porté sur la liasse 2065.
"""
from datetime import date

import pytest

from profitos.corporate_tax import _months, compute_is, pme_eligible

ANNEE = (date(2026, 1, 1), date(2026, 12, 31))


# ---------------------------------------------------------------------------
# Durée de l'exercice : elle commande le plafond du taux réduit
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('debut, fin, attendu', [
    (date(2026, 1, 1), date(2026, 12, 31), 12),       # exercice civil
    (date(2024, 1, 1), date(2024, 12, 31), 12),       # année bissextile
    (date(2026, 1, 1), date(2026, 9, 15), 8.5),       # exemple du BOI-IS-LIQ-20-20 § 50
    (date(2026, 1, 1), date(2026, 6, 30), 6),         # premier exercice de six mois
    (date(2026, 1, 1), date(2027, 6, 30), 18),        # exercice long
    (date(2026, 2, 1), date(2027, 1, 31), 12),        # exercice décalé
    (date(2026, 1, 31), date(2026, 2, 28), 1),        # v523 : quantième absent du mois d'arrivée
    (date(2026, 1, 31), date(2026, 3, 15), 1.5),      # idem, avec jours résiduels
])
def test_exercise_duration(debut, fin, attendu):
    assert abs(_months(debut, fin) - attendu) < 0.001, f'{debut} → {fin}'


# ---------------------------------------------------------------------------
# Dossiers de référence
# ---------------------------------------------------------------------------

def test_pme_benefit_under_the_reduced_ceiling(app=None):
    """30 000 € de bénéfice, PME éligible : tout au taux réduit. 30 000 × 15 % = 4 500 €."""
    r = compute_is(30_000, *ANNEE, turnover=250_000, eligible_pme=True)
    assert r['base_taux_reduit'] == 30_000.0 and r['base_taux_normal'] == 0.0
    assert r['is_total'] == 4_500
    assert r['contribution_sociale'] == 0 and r['cs_exempt'] is True
    assert r['is_du'] == 4_500.0


def test_pme_benefit_straddling_the_ceiling():
    """100 000 € : 42 500 à 15 % (6 375) + 57 500 à 25 % (14 375) = 20 750 €."""
    r = compute_is(100_000, *ANNEE, turnover=800_000, eligible_pme=True)
    assert r['base_taux_reduit'] == 42_500.0 and r['base_taux_normal'] == 57_500.0
    assert r['is_taux_reduit'] == 6_375.0 and r['is_taux_normal'] == 14_375.0
    assert r['is_total'] == 20_750


def test_company_not_eligible_pays_the_full_rate():
    """Sans éligibilité PME, aucun taux réduit : 100 000 × 25 % = 25 000 €."""
    r = compute_is(100_000, *ANNEE, turnover=12_000_000, eligible_pme=False)
    assert r['plafond_taux_reduit'] == 0.0 and r['base_taux_reduit'] == 0.0
    assert r['is_total'] == 25_000


def test_short_exercise_prorates_the_reduced_ceiling():
    """Exercice de six mois : le plafond du taux réduit est ramené à 21 250 €.
    50 000 € de bénéfice → 21 250 à 15 % (3 187,50) + 28 750 à 25 % (7 187,50) = 10 375 €."""
    r = compute_is(50_000, date(2026, 1, 1), date(2026, 6, 30), turnover=400_000, eligible_pme=True)
    assert r['plafond_taux_reduit'] == 21_250.0
    assert r['is_total'] == 10_375


def test_long_exercise_raises_the_ceiling():
    """Exercice de dix-huit mois : plafond porté à 63 750 € (42 500 × 18/12)."""
    r = compute_is(100_000, date(2026, 1, 1), date(2027, 6, 30), turnover=900_000, eligible_pme=True)
    assert r['plafond_taux_reduit'] == 63_750.0
    assert r['is_total'] == round(63_750 * 0.15 + 36_250 * 0.25)


def test_loss_makes_no_tax_due():
    """Un résultat déficitaire ne produit jamais d'IS, ni d'IS négatif."""
    r = compute_is(-50_000, *ANNEE, turnover=300_000, eligible_pme=True)
    assert r['base'] == 0.0 and r['is_total'] == 0 and r['is_du'] == 0.0


def test_zero_result_is_handled():
    r = compute_is(0, *ANNEE, turnover=300_000, eligible_pme=True)
    assert r['is_total'] == 0 and r['solde'] == 0.0


# ---------------------------------------------------------------------------
# Contribution sociale de 3,3 %
# ---------------------------------------------------------------------------

def test_social_contribution_applies_above_the_allowance():
    """Grande entreprise, 5 M€ de bénéfice : IS 1 250 000 €, contribution sociale
    3,3 % × (1 250 000 − 763 000) = 16 071 €."""
    r = compute_is(5_000_000, *ANNEE, turnover=40_000_000, eligible_pme=False)
    assert r['is_total'] == 1_250_000
    assert r['contribution_sociale'] == round((1_250_000 - 763_000) * 0.033)
    assert r['cs_exempt'] is False


def test_no_social_contribution_below_the_allowance():
    """IS inférieur à 763 000 € : pas de contribution sociale, même hors PME."""
    r = compute_is(2_000_000, *ANNEE, turnover=40_000_000, eligible_pme=False)
    assert r['is_total'] == 500_000 and r['contribution_sociale'] == 0


def test_small_company_is_exempt_from_social_contribution():
    r = compute_is(4_000_000, *ANNEE, turnover=5_000_000, eligible_pme=True)
    assert r['cs_exempt'] is True and r['contribution_sociale'] == 0


# ---------------------------------------------------------------------------
# Éligibilité au taux réduit
# ---------------------------------------------------------------------------

def test_eligibility_requires_all_three_conditions():
    ok = pme_eligible(5_000_000, *ANNEE, capital_libere=True, detention_personnes_physiques=True)
    assert ok is True
    assert pme_eligible(12_000_000, *ANNEE, capital_libere=True,
                        detention_personnes_physiques=True) is False, 'CA trop élevé'
    assert pme_eligible(5_000_000, *ANNEE, capital_libere=False,
                        detention_personnes_physiques=True) is False, 'capital non libéré'
    assert pme_eligible(5_000_000, *ANNEE, capital_libere=True,
                        detention_personnes_physiques=False) is False, 'détention insuffisante'


def test_eligibility_turnover_is_annualised():
    """Un CA de 6 M€ sur six mois vaut 12 M€ sur douze : l'entreprise n'est pas PME."""
    assert pme_eligible(6_000_000, date(2026, 1, 1), date(2026, 6, 30),
                        capital_libere=True, detention_personnes_physiques=True) is False
    assert pme_eligible(4_000_000, date(2026, 1, 1), date(2026, 6, 30),
                        capital_libere=True, detention_personnes_physiques=True) is True


# ---------------------------------------------------------------------------
# Acomptes, crédits et solde
# ---------------------------------------------------------------------------

def test_credits_and_instalments_reduce_the_balance():
    """20 750 € d'IS, 2 000 € de crédits d'impôt, 15 000 € d'acomptes → solde 3 750 €."""
    r = compute_is(100_000, *ANNEE, turnover=800_000, eligible_pme=True,
                   credits=2_000, acomptes=15_000)
    assert r['is_du'] == 18_750.0
    assert r['solde'] == 3_750.0


def test_overpaid_instalments_give_a_refund_not_a_negative_tax():
    """Trop d'acomptes : le solde devient négatif (restitution), l'IS lui reste positif."""
    r = compute_is(30_000, *ANNEE, turnover=250_000, eligible_pme=True, acomptes=10_000)
    assert r['is_total'] == 4_500 and r['solde'] == -5_500.0


def test_tax_is_rounded_to_the_euro():
    """L'IS se liquide à l'euro : aucun centime ne doit subsister."""
    r = compute_is(33_333.33, *ANNEE, turnover=250_000, eligible_pme=True)
    assert r['is_total'] == float(int(r['is_total']))
