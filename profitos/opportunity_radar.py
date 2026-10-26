"""Opportunity Radar (v483) : des opportunités tirées de faits vérifiés, jamais promises.

Constats au format du moteur de vérification (profitos/verification.py) :
- votre secteur et vous : évolution sur un an de votre chiffre d'affaires facturé et de
  l'indice INSEE de chiffre d'affaires de votre activité, sur les mêmes trois mois (la
  comparaison sur un an neutralise la saisonnalité, que la série soit brute ou CVS-CJO) ;
- clients dormants : ont acheté dans les 18 derniers mois, plus rien depuis 6 mois ;
- clients en croissance : achats des 6 derniers mois en nette hausse ;
- dépendance : un client pèse une part importante du chiffre d'affaires des 12 derniers mois ;
- appels d'offres ouverts correspondant à votre profil (veille BOAMP).
Chaque constat montre les chiffres et leur source ; aucune hausse de chiffre d'affaires
n'est annoncée.
"""
import re
from datetime import date, timedelta

from profitos import verification as V

MODULE = 'opportunity_radar'
KINDS = ('radar_secteur', 'radar_dormant', 'radar_croissance', 'radar_dependance', 'radar_appels_offres')
KIND_LABELS = {'radar_secteur': 'Votre secteur et vous', 'radar_dormant': 'Clients dormants',
               'radar_croissance': 'Clients en croissance', 'radar_dependance': 'Dépendance client',
               'radar_appels_offres': "Appels d'offres"}
DORMANT_MIN, GROWTH_MIN, GROWTH_RATE, TOP = 500.0, 2000.0, 0.30, 5
DEPENDENCE_SHARE, DEPENDENCE_MIN_TOTAL = 0.40, 5000.0
SECTOR_GAP_PTS = 5.0
MONTHS = ['janvier', 'février', 'mars', 'avril', 'mai', 'juin', 'juillet', 'août', 'septembre', 'octobre', 'novembre',
          'décembre']


def _money(x):
    from profitos.runtime import fr_number
    return f'{fr_number(x, 2)} €'


def _pct(x, sign=True):
    s = f"{x:+.1f}" if sign else f"{x:.1f}"
    return s.replace('.', ',') + ' %'


def _d(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _month(d):
    return f'{d.year}-{d.month:02d}'


def _shift(ym, n):
    y, m = int(ym[:4]), int(ym[5:7])
    y, m = divmod((y * 12 + m - 1) + n, 12)
    return f'{y}-{m + 1:02d}'


def _label(ym):
    return f"{MONTHS[int(ym[5:7]) - 1]} {ym[:4]}"


def client_key(name, siren=None):
    s = re.sub(r'\D', '', str(siren or ''))
    if len(s) == 9:
        return 'siren:' + s
    from profitos.contract_guardian import supplier_key
    return 'nom:' + supplier_key(name)


# --------------------------------------------------------------------- données
def sales(c, eid):
    """Ventes facturées HT (factures émises moins acomptes déjà facturés, moins avoirs) :
    [(date, clé client, nom client, montant)]."""
    rows = c.execute("""SELECT issue_date, client_name, client_siren, COALESCE(subtotal,0) - COALESCE(deposit_applied_subtotal,0) AS ht
                        FROM outgoing_invoices WHERE entity_id IS ? AND COALESCE(status,'draft') NOT IN ('draft','cancelled')
                        AND issue_date IS NOT NULL""", (eid,)).fetchall()
    out = [(_d(r['issue_date']), client_key(r['client_name'], r['client_siren']), (r['client_name'] or '').strip(), float(r['ht'] or 0))
           for r in rows if _d(r['issue_date'])]
    try:
        credits = c.execute("""SELECT cn.issue_date, cn.client_name, oi.client_siren, COALESCE(cn.subtotal,0) AS ht
                               FROM outgoing_credit_notes cn LEFT JOIN outgoing_invoices oi ON oi.id=cn.original_invoice_id
                               WHERE cn.entity_id IS ? AND COALESCE(cn.status,'issued')='issued' AND cn.issue_date IS NOT NULL""",
                            (eid,)).fetchall()
    except Exception:
        try:
            c.rollback()
        except Exception:
            pass
        credits = []
    out += [(_d(r['issue_date']), client_key(r['client_name'], r['client_siren']), (r['client_name'] or '').strip(), -float(r['ht'] or 0))
            for r in credits if _d(r['issue_date'])]
    return out


def monthly(rows):
    out = {}
    for d, _k, _n, amount in rows:
        out[_month(d)] = out.get(_month(d), 0.0) + amount
    return out


# ------------------------------------------------------------------- détection
def detect_sector(c, eid, today, ac):
    from profitos import market_watch as MW
    settings = MW.load_settings(c, eid)
    codes = MW.activity_series(settings.get('naf'), settings.get('sector'))
    if not codes:
        return []
    code = codes[0]
    defn = MW.definition(code)
    meta = MW.series_meta(ac, code) or {}
    obs = dict(MW.observations(ac, code))
    rows = sales(c, eid)
    if not rows or meta.get('status') != 'ok' or not obs:
        return []
    per_month = monthly(rows)
    first = min(d for d, *_ in rows)
    current = _month(today)
    # Dernier mois complet pour lequel l'indice du secteur est publié sur les deux fenêtres.
    for m in sorted(obs, reverse=True):
        if not re.fullmatch(r'\d{4}-\d{2}', m) or m >= current:
            continue
        window = [_shift(m, -i) for i in (2, 1, 0)]
        prior = [_shift(x, -12) for x in window]
        if all(x in obs for x in window + prior):
            break
    else:
        return []
    if _month(first) > prior[0]:
        return []                                         # historique de facturation trop court
    mine_w = sum(per_month.get(x, 0.0) for x in window)
    mine_p = sum(per_month.get(x, 0.0) for x in prior)
    if mine_p <= 0:
        return []
    sector_w = sum(obs[x] for x in window) / 3
    sector_p = sum(obs[x] for x in prior) / 3
    mine = (mine_w / mine_p - 1) * 100
    sector = (sector_w / sector_p - 1) * 100
    gap = mine - sector
    span = f"{_label(window[0])} à {_label(window[-1])}"
    naf_check = meta.get('naf_check')
    checks = [V.check('Indice officiel de votre activité', True, f"{defn['label']} (INSEE, série {defn['idbank']}).", 'Veille (INSEE)'),
              V.check("Activité de la série vérifiée", True if naf_check in ('dimensions', 'titre') else None,
                      {'dimensions': "Vérifiée sur les dimensions officielles.", 'titre': "Vérifiée sur l'intitulé officiel."}
                      .get(naf_check, "Non vérifiée automatiquement : contrôlez la série."), 'Veille (INSEE)'),
              V.check('Mêmes mois comparés sur un an', True, f"{span}, contre les mêmes mois un an plus tôt "
                      "(saisonnalité neutralisée).", 'Calcul ProfitOS'),
              V.check('Votre historique de facturation couvre la période', True,
                      f"Premières factures en {_label(_month(first))}.", 'Factures ProfitOS')]
    if gap <= -SECTOR_GAP_PTS:
        title = f"Votre chiffre d'affaires progresse moins vite que votre secteur ({_pct(mine)} contre {_pct(sector)})"
        severity = 'important'
        detail = ("Sur la même période, l'activité de votre secteur évolue mieux que vos ventes : c'est un constat, pas une "
                  "prévision. Regardez quels clients ou quelles offres expliquent l'écart (clients dormants ci-dessous).")
    elif gap >= SECTOR_GAP_PTS:
        title = f"Votre chiffre d'affaires progresse plus vite que votre secteur ({_pct(mine)} contre {_pct(sector)})"
        severity = 'info'
        detail = "Vous gagnez des parts d'activité sur la période : un constat sur les mois écoulés, pas une promesse pour la suite."
    else:
        title = f"Votre chiffre d'affaires évolue comme votre secteur ({_pct(mine)} contre {_pct(sector)})"
        severity = 'info'
        detail = 'Écart de moins de 5 points avec votre secteur sur la période.'
    evidence = [V.evidence('Votre chiffre d\'affaires HT', f"{_money(mine_w)} contre {_money(mine_p)}", 'Factures ProfitOS'),
                V.evidence('Indice du secteur (moyenne)', f"{sector_w:.1f} contre {sector_p:.1f}".replace('.', ','),
                           'INSEE', meta.get('last_update')),
                V.evidence('Correction de la série', MW.ADJUSTMENT_SHORT.get(meta.get('adjustment') or 'INCONNU', '—'), 'INSEE')]
    return [V.finding(f'radar:secteur:{code}:{window[-1]}', MODULE, 'radar_secteur', severity, title,
                      checks=checks, evidence=evidence, detail=detail,
                      action={'kind': 'link', 'label': "Voir l'indice du secteur", 'endpoint': 'market_watch_series',
                              'args': {'code': code}}, follow_up_days=31, ref={'code': code, 'month': window[-1]})]


def _client_links(c, eid):
    try:
        rows = c.execute('SELECT id, name, siren FROM invoicing_clients WHERE entity_id IS ?', (eid,)).fetchall()
    except Exception:
        try:
            c.rollback()
        except Exception:
            pass
        return {}
    return {client_key(r['name'], r['siren']): r['id'] for r in rows}


def detect_clients(c, eid, today):
    rows = sales(c, eid)
    if not rows:
        return []
    links = _client_links(c, eid)
    d6, d12, d18 = today - timedelta(days=182), today - timedelta(days=365), today - timedelta(days=548)
    agg = {}
    for d, k, name, amount in rows:
        a = agg.setdefault(k, {'name': name, 'last6': 0.0, 'prev6': 0.0, 'before': 0.0, 'y12': 0.0, 'last_date': d})
        if d >= a['last_date']:
            a['last_date'], a['name'] = d, name or a['name']
        if d > d6:
            a['last6'] += amount
        elif d > d12:
            a['prev6'] += amount
        if d18 < d <= d6:
            a['before'] += amount
        if d > d12:
            a['y12'] += amount

    def link(k):
        cid = links.get(k)
        return ({'kind': 'link', 'label': 'Ouvrir la fiche client', 'endpoint': 'invoicing_client_detail', 'args': {'client_id': cid}}
                if cid else {'kind': 'link', 'label': 'Voir les clients', 'endpoint': 'invoicing_clients'})
    out = []
    dormant = sorted(((k, a) for k, a in agg.items() if a['last6'] <= 0.005 and a['before'] >= DORMANT_MIN),
                     key=lambda x: -x[1]['before'])[:TOP]
    for k, a in dormant:
        out.append(V.finding(
            f'radar:dormant:{k}', MODULE, 'radar_dormant', 'info',
            f"{a['name']} n'a rien commandé depuis le {a['last_date'].strftime('%d/%m/%Y')}", amount=a['before'],
            checks=[V.check('Achats entre 18 et 6 mois', True, f"{_money(a['before'])} HT facturés.", 'Factures ProfitOS'),
                    V.check('Aucune facture depuis 6 mois', True, f"Dernière facture le {a['last_date'].strftime('%d/%m/%Y')}.",
                            'Factures ProfitOS')],
            detail="Un client qui a déjà acheté est souvent le plus simple à relancer : prenez de ses nouvelles ou proposez "
                   "une offre adaptée.", action=link(k), follow_up_days=60, ref={'client': k}))
    growing = sorted(((k, a) for k, a in agg.items()
                      if a['last6'] >= GROWTH_MIN and a['prev6'] > 0 and a['last6'] / a['prev6'] - 1 >= GROWTH_RATE),
                     key=lambda x: -(x[1]['last6'] - x[1]['prev6']))[:TOP]
    for k, a in growing:
        rate = (a['last6'] / a['prev6'] - 1) * 100
        out.append(V.finding(
            f'radar:croissance:{k}', MODULE, 'radar_croissance', 'info',
            f"{a['name']} : {_pct(rate)} d'achats sur les 6 derniers mois", amount=round(a['last6'] - a['prev6'], 2),
            checks=[V.check('Achats des 6 derniers mois', True, f"{_money(a['last6'])} HT.", 'Factures ProfitOS'),
                    V.check('Achats des 6 mois précédents', True, f"{_money(a['prev6'])} HT.", 'Factures ProfitOS')],
            detail="Un client dont les besoins augmentent : l'occasion de proposer un contrat cadre, un volume ou un service "
                   "complémentaire. Constat sur les mois écoulés, sans garantie pour la suite.",
            action=link(k), follow_up_days=60, ref={'client': k}))
    total12 = sum(a['y12'] for a in agg.values())
    if total12 >= DEPENDENCE_MIN_TOTAL and len([a for a in agg.values() if a['y12'] > 0]) >= 2:
        k, top = max(agg.items(), key=lambda x: x[1]['y12'])
        share = top['y12'] / total12
        if share >= DEPENDENCE_SHARE:
            out.append(V.finding(
                f'radar:dependance:{k}', MODULE, 'radar_dependance', 'important' if share >= 0.5 else 'info',
                f"{top['name']} représente {_pct(share * 100, False)} de votre chiffre d'affaires sur 12 mois",
                amount=top['y12'],
                checks=[V.check("Chiffre d'affaires des 12 derniers mois", True, f"{_money(total12)} HT, dont {_money(top['y12'])} "
                                f"pour ce client.", 'Factures ProfitOS')],
                detail="Un retard ou une baisse de commandes de ce client pèserait fortement sur votre trésorerie : "
                       "diversifier votre clientèle réduit ce risque.",
                action=link(k), alternatives=[{'label': 'Simuler son impact', 'endpoint': 'decision_simulator'}],
                follow_up_days=90, ref={'client': k}))
    return out


def detect_tenders(c, eid, today):
    rows = c.execute("""SELECT id, title, buyer, score, deadline, value FROM opportunities WHERE type='GROW' AND status='OPEN'
                        AND entity_id IS ? AND (deadline IS NULL OR deadline>=?) ORDER BY score DESC, deadline""",
                     (eid, today.isoformat())).fetchall()
    if not rows:
        return []
    best = rows[0]
    soon = [r for r in rows if r['deadline'] and _d(r['deadline']) and (_d(r['deadline']) - today).days <= 10]
    return [V.finding(
        'radar:appels_offres', MODULE, 'radar_appels_offres', 'important' if soon else 'info',
        f"{len(rows)} appel(s) d'offres ouvert(s) correspondant à votre profil",
        checks=[V.check('Annonces officielles (BOAMP)', True, 'Avis de marchés publics publiés au BOAMP.', 'BOAMP'),
                V.check('Date limite non dépassée', True,
                        f"{len(soon)} avec une date limite dans les 10 jours." if soon else 'Aucune date limite dans les 10 jours.',
                        'BOAMP')],
        evidence=[V.evidence('Le mieux noté', f"{best['title'][:90]} — {best['buyer'] or 'acheteur non précisé'}", 'BOAMP',
                             best['deadline'])],
        action={'kind': 'link', 'label': "Voir les appels d'offres", 'endpoint': 'grow'}, follow_up_days=14,
        ref={'count': len(rows)})]


def detect(c, eid, today):
    from profitos.runtime import auth_cx
    out, evaluated = [], set()
    ac = auth_cx()
    try:
        for kinds, name, fn in ((('radar_secteur',), 'votre secteur et vous', lambda: detect_sector(c, eid, today, ac)),
                                (('radar_dormant', 'radar_croissance', 'radar_dependance'), 'clients', lambda: detect_clients(c, eid, today)),
                                (('radar_appels_offres',), "appels d'offres", lambda: detect_tenders(c, eid, today))):
            try:
                out += fn()
                evaluated.update(kinds)
            except Exception as exc:
                try:
                    c.rollback()
                except Exception:
                    pass
                out.append(V.unavailable(MODULE, name, exc))
    finally:
        ac.close()
    evaluated.add('controle_indisponible')
    return out, evaluated
