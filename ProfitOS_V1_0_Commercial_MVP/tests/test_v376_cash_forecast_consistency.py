"""Cash Intelligence — cohérence de la prévision 90 j (correctifs).

Reproduit le jeu de données observé en production le 07/10/2026 :
- solde de 20 000 € déclaré le 24/08/2026 ;
- factures clients avec une échéance en 2030 (hors horizon) ;
- facture fournisseur GOOGLE échue depuis le 30/08/2026 ;
- 2 dépenses sur les 90 derniers jours.

Bugs couverts :
1. les flux après J+90 étaient ramenés à J+90 (pic vertical en fin de courbe, point bas J+89) ;
2. les flux du jour 0 (échus / dus aujourd'hui) étaient ignorés par les courbes ;
3. l'optimiste comptait 112 % des factures et pouvait finir sous le probable ;
4. chaque courbe avait sa propre échelle verticale ;
5. KPI 30/60/90 j, point bas et courbes étaient calculés par trois codes différents ;
6. un solde déclaré il y a 44 jours affichait « STABLE » sans réserve.
"""
from datetime import date, timedelta

from profitos.routes.cash_intelligence import (
    HORIZON_DAYS, _simulate_curve, _horizon_day, compute_cash_forecast,
)

TODAY = date(2026, 10, 7)


def _inv(i, number, client, total, due, paid=0.0, **extra):
    row = {'id': i, 'invoice_number': number, 'client_name': client, 'total': total,
           'paid_total': paid, 'due_date': due, 'issue_date': '2026-08-01'}
    row.update(extra)
    return row


def _purchase(i, supplier, number, total, due, paid=0.0):
    return {'id': i, 'supplier_name': supplier, 'invoice_number': number, 'total': total,
            'paid_total': paid, 'due_date': due, 'issue_date': '2026-08-01'}


SALES = [
    _inv(1, 'FA-2026-021', 'Test fac elc', 7827, '2030-10-30'),
    _inv(2, 'FA-2026-029', 'Dupont Conseil SAS', 1710, '2030-12-30'),
    _inv(3, 'FA-2026-037', 'Dupont Conseil SAS', 1482, '2030-02-10'),
    _inv(4, 'FA-2026-023', 'Dupont Conseil SAS', 1200, '2030-03-10'),
    _inv(5, 'FA-2026-015', 'Client rejeté', 900, '2026-11-01', held=True),
]
PURCHASES = [
    _purchase(1, 'GOOGLE PAYMENT LIMITED', 'GOOGLE-TEST-001', 277, '2026-08-30'),
    _purchase(2, 'NOVALYS FOURNITURES SAS', 'F-2026-0923-001', 200, '2026-10-23', paid=50),
    _purchase(3, 'FOURNISSEUR SANDBOX', 'TEST-PROFITOS-001', 120, '2026-11-03'),
]
EXPENSES = [
    {'vendor': 'A', 'description': 'x', 'amount': 2000, 'expense_date': '2026-09-15', 'category': 'c'},
    {'vendor': 'B', 'description': 'y', 'amount': 1051, 'expense_date': '2026-08-20', 'category': 'c'},
]


def _hold(inv):
    return 'rejetée' if inv.get('held') else None


def _forecast(sales=SALES, purchases=PURCHASES, expenses=EXPENSES, cash=20000, as_of='2026-08-24'):
    return compute_cash_forecast(cash, as_of, sales, purchases, expenses, einvoice_hold=_hold, today=TODAY)


def test_horizon_day_excludes_beyond_horizon_and_keeps_day_zero():
    assert _horizon_day(91) is None
    assert _horizon_day(90) == 90
    assert _horizon_day(0) == 1
    assert _horizon_day(-30) == 1


def test_receivables_beyond_90_days_do_not_spike_the_curve():
    f = _forecast()
    for curve in f['curves']:
        values = curve['values']
        # Aucun encaissement dans l'horizon : la courbe ne peut que descendre.
        assert all(b <= a for a, b in zip(values, values[1:])), curve['mode']
        assert curve['end_90'] == curve['minimum']
    assert len(f['receivables_beyond']) == 4
    assert f['receivables_beyond_total'] == 7827 + 1710 + 1482 + 1200
    assert f['receivables_in_horizon'] == []


def test_far_future_due_dates_are_flagged_as_suspicious():
    f = _forecast()
    assert {r['invoice_number'] for r in f['suspicious_receivables']} == {
        'FA-2026-021', 'FA-2026-029', 'FA-2026-037', 'FA-2026-023'}


def test_overdue_supplier_invoice_is_counted_and_flagged():
    f = _forecast()
    google = f['supplier_payables'][0]
    assert google['overdue_days'] == (TODAY - date(2026, 8, 30)).days
    assert f['overdue_payables_count'] == 1 and f['overdue_payables_total'] == 277
    probable = f['curves'][1]
    daily = f['daily_burn']
    # Appliquée à J+1, pas ignorée.
    assert round(probable['values'][1], 2) == round(20000 - daily - 277, 2)


def test_partial_payment_is_reported_only_when_it_exists():
    f = _forecast()
    by_number = {p['description']: p for p in f['supplier_payables']}
    assert by_number['Facture F-2026-0923-001']['paid'] == 50
    assert by_number['Facture F-2026-0923-001']['amount'] == 150
    assert by_number['Facture GOOGLE-TEST-001']['paid'] == 0


def test_kpis_point_bas_and_probable_curve_share_one_projection():
    sales = SALES + [_inv(9, 'FA-2026-050', 'Client proche', 3000, '2026-10-27')]
    f = _forecast(sales=sales)
    probable = f['curves'][1]
    for h in (30, 60, 90):
        assert f['horizons'][h] == probable['values'][h]
    assert f['min_cash'] == probable['minimum']
    assert f['min_day'] == probable['min_day']


def test_scenarios_are_ordered_every_day():
    sales = SALES + [
        _inv(9, 'FA-2026-050', 'Client proche', 3000, '2026-10-27'),
        _inv(10, 'FA-2026-051', 'Client en retard', 800, '2026-09-20'),
        _inv(11, 'FA-2026-052', 'Client limite', 500, '2026-12-30'),
    ]
    f = _forecast(sales=sales)
    prudent, probable, optimiste = f['curves']
    for d in range(HORIZON_DAYS + 1):
        assert prudent['values'][d] <= probable['values'][d] <= optimiste['values'][d], d
    assert prudent['minimum'] <= probable['minimum'] <= optimiste['minimum']


def test_optimistic_never_collects_more_than_invoiced():
    sales = [_inv(9, 'FA-2026-050', 'Client proche', 3000, '2026-10-27')]
    f = _forecast(sales=sales, purchases=[], expenses=[])
    assert f['curves'][2]['end_90'] == 20000 + 3000


def test_receivable_due_today_is_not_lost_in_any_scenario():
    sales = [_inv(9, 'FA-2026-050', 'Client', 1000, TODAY.isoformat())]
    f = _forecast(sales=sales, purchases=[], expenses=[])
    assert f['curves'][1]['end_90'] == 21000
    assert f['curves'][2]['end_90'] == 21000  # optimiste : décalé à -10 j, ramené à J+1


def test_what_if_today_counts_the_top_receivable():
    f = _forecast(purchases=[], expenses=[])
    curve = _simulate_curve(20000, 0, f['receivables'], mode='probable', top_delay=0, today=TODAY)
    assert curve['end_90'] == 20000 + 7827
    never = _simulate_curve(20000, 0, f['receivables'], mode='probable', top_delay=-1, today=TODAY)
    assert never['end_90'] == 20000


def test_all_curves_start_from_the_same_point_on_a_shared_scale():
    sales = SALES + [_inv(9, 'FA-2026-050', 'Client proche', 3000, '2026-10-27')]
    f = _forecast(sales=sales)
    starts = {c['points'].split(' ')[0] for c in f['curves']}
    assert len(starts) == 1
    assert f['chart']['lo'] < min(min(c['values']) for c in f['curves'])
    assert f['chart']['hi'] > max(max(c['values']) for c in f['curves'])


def test_stale_balance_is_not_reported_as_stable():
    f = _forecast()
    assert f['balance_age_days'] == 44 and f['balance_stale']
    assert f['alert_level'] == 'VIGILANCE'
    assert '44 jours' in f['alert']
    fresh = _forecast(as_of=TODAY.isoformat())
    assert fresh['alert_level'] == 'STABLE' and not fresh['balance_stale']


def test_held_receivables_stay_out_of_the_forecast():
    f = _forecast()
    assert [h['invoice_number'] for h in f['held_receivables']] == ['FA-2026-015']
    assert all(r['invoice_number'] != 'FA-2026-015' for r in f['receivables'])


def test_burn_reliability_flag():
    assert _forecast()['burn_reliable'] is False
    many = [{'vendor': str(i), 'description': '', 'amount': 100,
             'expense_date': (TODAY - timedelta(days=i * 5)).isoformat(), 'category': ''} for i in range(8)]
    assert _forecast(expenses=many)['burn_reliable'] is True
