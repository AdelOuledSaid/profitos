"""Révision comptable et diagnostics de clôture."""
from datetime import datetime
import json
import re

COURANTE_ITEMS = [
    ("Rapprochement bancaire du mois effectué", 'banking'),
    ("Toutes les factures clients du mois sont enregistrées", 'invoicing_list'),
    ("Toutes les factures fournisseurs du mois sont enregistrées", 'purchase_list'),
    ("Notes de frais du mois toutes traitées (validées ou rejetées)", 'expense_reports_list'),
    ("TVA du mois vérifiée (collectée/déductible cohérentes)", 'vat_summary'),
    ("Échéances d'emprunts du mois réglées", 'loans_list'),
    ("Redevances de crédit-bail du mois réglées", 'finance_leases_list'),
    ("Aucune anomalie détectée sur les imports Recover/Save", None),
]
ANNUELLE_ITEMS = [
    ("Rapprochement bancaire au dernier jour de l'exercice", 'banking'),
    ("Toutes les factures clients de l'exercice sont enregistrées", 'invoicing_list'),
    ("Toutes les factures fournisseurs de l'exercice sont enregistrées", 'purchase_list'),
    ("Dotations aux amortissements de l'exercice comptabilisées", 'fixed_assets_list'),
    ("Échéanciers d'emprunts vérifiés (soldes cohérents)", 'loans_list'),
    ("Engagements hors bilan de crédit-bail à jour pour l'annexe", 'finance_leases_list'),
    ("Charges constatées d'avance (CCA) comptabilisées", 'cutoff_list'),
    ("Factures non parvenues (FNP) comptabilisées", 'cutoff_list'),
    ("Factures à établir (FAE) comptabilisées", 'cutoff_list'),
    ("Lettrage des comptes clients à jour", None),
    ("Lettrage des comptes fournisseurs à jour", None),
    ("TVA de l'exercice réconciliée sur les 12 mois", 'vat_summary'),
    ("Comptes d'attente (467000/471) soldés ou justifiés", 'accounting_chart'),
    ("Provisions pour risques et charges revues", None),
    ("Créances douteuses ou litigieuses identifiées", None),
    ("Période clôturée dans ProfitOS une fois tout vérifié", 'accounting_closure'),
]

def seed_review_items(conn, review_id, review_type):
    items = ANNUELLE_ITEMS if review_type == 'annuelle' else COURANTE_ITEMS
    for i,(label,route_name) in enumerate(items):
        conn.execute('INSERT INTO review_items(review_id,item_order,label,linked_route) VALUES(?,?,?,?)',(review_id,i,label,route_name))
    conn.commit()

def create_review(conn, review_type, period_label, entity_id=None, created_by=None):
    if review_type not in ('courante','annuelle'):
        raise ValueError(f"Type de révision inconnu : {review_type!r} (attendu courante ou annuelle).")
    conn.execute('INSERT INTO reviews(entity_id,review_type,period_label,status,created_at,created_by) VALUES(?,?,?,?,?,?)',(entity_id,review_type,period_label,'in_progress',datetime.utcnow().isoformat(),created_by))
    conn.commit(); rid=conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    seed_review_items(conn,rid,review_type); return rid

def toggle_review_item(conn,item_id,checked,note=None,checked_by=None):
    if checked:
        conn.execute('UPDATE review_items SET checked=1,note=?,checked_by=?,checked_at=? WHERE id=?',(note,checked_by,datetime.utcnow().isoformat(),item_id))
    else:
        conn.execute('UPDATE review_items SET checked=0,note=?,checked_by=NULL,checked_at=NULL WHERE id=?',(note,item_id))
    conn.commit()

def review_progress(conn,review_id):
    row=conn.execute('SELECT COUNT(*) total,COALESCE(SUM(checked),0) done FROM review_items WHERE review_id=?',(review_id,)).fetchone()
    return row['done'],row['total']

def _entity_clause(alias, entity_id):
    return (f'{alias}.entity_id=?',(entity_id,)) if entity_id is not None else (f'{alias}.entity_id IS NULL',())

def run_review_diagnostics(conn, review_id, entity_id=None, run_by=None):
    """Snapshot de contrôles objectifs. Ne crée/modifie aucune écriture comptable."""
    ef, ep = _entity_clause('e', entity_id)
    issues=[]
    unbalanced=conn.execute(f'''SELECT COUNT(*) n,COALESCE(SUM(ABS(x.d-x.c)),0) amount FROM (
        SELECT e.id,COALESCE(SUM(l.debit),0) d,COALESCE(SUM(l.credit),0) c
        FROM accounting_entries e JOIN accounting_entry_lines l ON l.entry_id=e.id
        WHERE {ef} GROUP BY e.id HAVING ABS(COALESCE(SUM(l.debit),0)-COALESCE(SUM(l.credit),0))>0.005) x''',ep).fetchone()
    if unbalanced['n']:
        issues.append(('UNBALANCED_ENTRIES','blocker','Écritures comptables déséquilibrées',unbalanced['n'],unbalanced['amount'],None))
    suspense=conn.execute(f'''SELECT COUNT(*) n,COALESCE(SUM(ABS(l.debit-l.credit)),0) amount
        FROM accounting_entry_lines l JOIN accounting_entries e ON e.id=l.entry_id
        WHERE {ef} AND (l.account_code='467000' OR LEFT(l.account_code,3)='471') AND ABS(l.debit-l.credit)>0.005''',ep).fetchone()
    if suspense['n']:
        issues.append(('SUSPENSE_ACCOUNTS','blocker','Comptes d’attente 467000/471 à solder ou justifier',suspense['n'],suspense['amount'],None))
    unlettered=conn.execute(f'''SELECT COUNT(*) n,COALESCE(SUM(ABS(l.debit-l.credit)),0) amount
        FROM accounting_entry_lines l JOIN accounting_entries e ON e.id=l.entry_id
        WHERE {ef} AND (LEFT(l.account_code,3) IN ('401','411'))
          AND (l.lettrage_code IS NULL OR TRIM(l.lettrage_code)='') AND ABS(l.debit-l.credit)>0.005''',ep).fetchone()
    if unlettered['n']:
        issues.append(('UNLETTERED_THIRDPARTY','warning','Lignes clients/fournisseurs non lettrées à revoir',unlettered['n'],unlettered['amount'],None))
    # Pièces fournisseurs manquantes : contrôle documentaire, limité à l'entité active.
    pf = 'entity_id=?' if entity_id is not None else 'entity_id IS NULL'; pp=(entity_id,) if entity_id is not None else ()
    missing=conn.execute(f'''SELECT COUNT(*) n,COALESCE(SUM(total),0) amount FROM purchase_invoices
        WHERE {pf} AND COALESCE(total,0)>0 AND (document_path IS NULL OR TRIM(document_path)='')''',pp).fetchone()
    if missing['n']:
        issues.append(('MISSING_PURCHASE_DOCS','warning','Factures fournisseurs sans justificatif attaché',missing['n'],missing['amount'],None))

    # v396 — consolidation de clôture annuelle : contrôles objectifs supplémentaires.
    review=conn.execute('SELECT review_type,period_label FROM reviews WHERE id=?',(review_id,)).fetchone()
    if review and review['review_type']=='annuelle':
        import re
        year_match=re.search(r'(?<!\d)(20\d{2})(?!\d)', review['period_label'] or '')
        if year_match:
            review_year=year_match.group(1)
            year_end=f'{review_year}-12-31'

            af='a.entity_id=?' if entity_id is not None else 'a.entity_id IS NULL'
            ap=(entity_id,year_end,review_year) if entity_id is not None else (year_end,review_year)
            missing_dep=conn.execute(f'''SELECT COUNT(*) n,COALESCE(SUM(a.purchase_amount),0) amount
                FROM fixed_assets a
                WHERE {af} AND a.status='active' AND a.purchase_date<=?
                  AND NOT EXISTS (
                    SELECT 1 FROM fixed_asset_depreciation_runs r
                    WHERE r.asset_id=a.id AND r.period_label=?
                  )''',ap).fetchone()
            if missing_dep['n']:
                issues.append(('MISSING_DEPRECIATION','blocker',
                    'Dotations aux amortissements de l’exercice non comptabilisées',
                    missing_dep['n'],missing_dep['amount'],{'year':review_year}))

            cf='c.entity_id=?' if entity_id is not None else 'c.entity_id IS NULL'
            cp=(entity_id,year_end) if entity_id is not None else (year_end,)
            bad_cutoff=conn.execute(f'''SELECT COUNT(*) n,COALESCE(SUM(c.amount),0) amount
                FROM cutoff_entries c
                LEFT JOIN accounting_entries e ON e.id=c.entry_id
                WHERE {cf} AND c.period_end_date<=?
                  AND (e.id IS NULL OR e.source_type<>'cutoff')''',cp).fetchone()
            if bad_cutoff['n']:
                issues.append(('INVALID_CUTOFF','blocker',
                    'CCA/FNP/FAE sans écriture comptable de cut-off valide',
                    bad_cutoff['n'],bad_cutoff['amount'],{'year':review_year}))

            # v398 — TVA annuelle : prévalidation uniquement sur des préparations fiscales
            # explicitement validées et dont les contrôles enregistrés sont tous OK.
            # Une CA12 annuelle peut couvrir l'exercice ; sinon chaque mois doit être
            # couvert par au moins une préparation validée (ex. CA3 mensuelles).
            vf='entity_id=?' if entity_id is not None else 'entity_id IS NULL'
            vp=(entity_id,year_end,f'{review_year}-01-01') if entity_id is not None else (year_end,f'{review_year}-01-01')
            vat_rows=conn.execute(f'''SELECT declaration_type,period_start,period_end,status,checks_json
                FROM tax_declaration_preparations
                WHERE {vf} AND status='validated'
                  AND period_start<=? AND period_end>=?
                ORDER BY period_start,period_end''',vp).fetchall()

            covered_months=set()
            invalid_validated=0
            import calendar
            from datetime import date as _date
            for vr in vat_rows:
                try:
                    checks=json.loads(vr['checks_json'] or '[]')
                except (TypeError,ValueError,json.JSONDecodeError):
                    checks=[]
                checks_ok=bool(checks) and all(bool(x.get('ok')) for x in checks if isinstance(x,dict))
                if not checks_ok:
                    invalid_validated += 1
                    continue
                try:
                    ps=_date.fromisoformat(str(vr['period_start'])[:10])
                    pe=_date.fromisoformat(str(vr['period_end'])[:10])
                except (TypeError,ValueError):
                    invalid_validated += 1
                    continue
                for month in range(1,13):
                    first=_date(int(review_year),month,1)
                    last=_date(int(review_year),month,calendar.monthrange(int(review_year),month)[1])
                    if ps<=first and pe>=last:
                        covered_months.add(month)

            missing_months=[m for m in range(1,13) if m not in covered_months]
            if missing_months or invalid_validated:
                details={'year':review_year,'missing_months':missing_months,
                         'invalid_validated_preparations':invalid_validated}
                label=(f'TVA {review_year} non entièrement réconciliée : '
                       f'{len(missing_months)} mois non couverts')
                if invalid_validated:
                    label += f', {invalid_validated} préparation(s) validée(s) avec contrôles incomplets'
                issues.append(('VAT_RECONCILIATION_INCOMPLETE','warning',label,
                    len(missing_months)+invalid_validated,0,details))

            # v399 — provisions pour risques et charges.
            # La comptabilité permet de détecter les comptes/mouvements de provision,
            # mais pas de prouver qu'un risque juridique/économique inexistant a été "revu".
            # Le contrôle reste donc toujours informatif et à validation humaine.
            prov_balance=conn.execute(f'''SELECT COUNT(*) n,
                    COALESCE(SUM(l.credit-l.debit),0) balance
                FROM accounting_entry_lines l
                JOIN accounting_entries e ON e.id=l.entry_id
                WHERE {ef} AND e.entry_date<=?
                  AND LEFT(l.account_code,2)='15' ''',ep+(year_end,)).fetchone()
            prov_moves=conn.execute(f'''SELECT COUNT(*) n,
                    COALESCE(SUM(ABS(l.debit-l.credit)),0) amount
                FROM accounting_entry_lines l
                JOIN accounting_entries e ON e.id=l.entry_id
                WHERE {ef} AND e.entry_date BETWEEN ? AND ?
                  AND (LEFT(l.account_code,4)='6815' OR LEFT(l.account_code,4)='7815')''',
                ep+(f'{review_year}-01-01',year_end)).fetchone()
            if prov_balance['n'] or prov_moves['n']:
                issues.append(('PROVISIONS_REVIEW_REQUIRED','warning',
                    'Provisions comptables détectées : revue humaine requise',
                    int(prov_balance['n'] or 0)+int(prov_moves['n'] or 0),
                    abs(float(prov_balance['balance'] or 0)),
                    {'year':review_year,
                     'provision_lines':int(prov_balance['n'] or 0),
                     'provision_balance':float(prov_balance['balance'] or 0),
                     'year_movements':int(prov_moves['n'] or 0),
                     'movement_amount':float(prov_moves['amount'] or 0)}))

            entity_key=int(entity_id) if entity_id is not None else 0
            closure=conn.execute(
                'SELECT closed_until FROM accounting_entity_closure WHERE entity_key=?',
                (entity_key,)
            ).fetchone()
            if not closure or not closure['closed_until'] or closure['closed_until'] < year_end:
                issues.append(('PERIOD_NOT_CLOSED','warning',
                    f'Exercice {review_year} non clôturé/verrouillé jusqu’au 31/12',
                    1,0,{'year_end':year_end}))
    meta=conn.execute(f'SELECT COUNT(*) n,COALESCE(MAX(e.id),0) mx FROM accounting_entries e WHERE {ef}',ep).fetchone()
    now=datetime.utcnow().isoformat()
    conn.execute('INSERT INTO review_diagnostic_runs(review_id,entity_id,run_at,run_by,blocker_count,warning_count,entry_count,snapshot_max_entry_id) VALUES(?,?,?,?,?,?,?,?)',(
        review_id,entity_id,now,run_by,sum(1 for x in issues if x[1]=='blocker'),sum(1 for x in issues if x[1]=='warning'),meta['n'],meta['mx']))
    run_id=conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    for code,severity,label,count,amount,details in issues:
        conn.execute('INSERT INTO review_diagnostic_issues(run_id,issue_code,severity,label,item_count,amount,details) VALUES(?,?,?,?,?,?,?)',(run_id,code,severity,label,count,float(amount or 0),json.dumps(details) if details else None))
    conn.commit()
    return latest_review_diagnostics(conn,review_id)

def automatic_review_item_statuses(conn, review_id, items, diag_run=None, diag_issues=None):
    """Prévalidation informative des points objectivement contrôlables.

    Ne modifie jamais review_items.checked : la validation finale reste humaine.
    """
    issue_codes={row['issue_code'] for row in (diag_issues or [])}
    status_by_order={}
    if not diag_run:
        return status_by_order

    # Mapping strict : "ok" seulement quand le diagnostic correspondant a réellement été exécuté
    # et qu'aucune anomalie de ce contrôle n'est présente.
    checks={
        3: ('MISSING_DEPRECIATION', 'Amortissements contrôlés automatiquement'),
        6: ('INVALID_CUTOFF', 'Cut-off contrôlé automatiquement'),
        7: ('INVALID_CUTOFF', 'Cut-off contrôlé automatiquement'),
        8: ('INVALID_CUTOFF', 'Cut-off contrôlé automatiquement'),
        9: ('UNLETTERED_THIRDPARTY', 'Lettrage tiers contrôlé automatiquement'),
        10: ('UNLETTERED_THIRDPARTY', 'Lettrage tiers contrôlé automatiquement'),
        11: ('VAT_RECONCILIATION_INCOMPLETE', 'TVA annuelle contrôlée sur les préparations fiscales validées'),
        12: ('SUSPENSE_ACCOUNTS', 'Comptes d’attente contrôlés automatiquement'),
        # La clôture (point 15) est traitée séparément ci-dessous : absence d'alerte != période clôturée.
    }
    for item in items:
        order=item['item_order']
        if order not in checks:
            continue
        code,label=checks[order]
        if code in issue_codes:
            status_by_order[order]={
                'state':'review',
                'label':'À vérifier automatiquement',
                'detail':label,
            }
        else:
            status_by_order[order]={
                'state':'ok',
                'label':'Contrôle auto OK',
                'detail':label + ' · validation finale manuelle',
            }

    # Clôture stricte : la période appartient à la révision, pas au snapshot diagnostic.
    if any(i['item_order']==15 for i in items):
        review_row=conn.execute(
            'SELECT period_label, entity_id FROM reviews WHERE id=?',
            (review_id,)
        ).fetchone()
        period_label=(review_row['period_label'] if review_row else '')
        review_entity_id=(review_row['entity_id'] if review_row else None)
        m=re.search(r'(?<!\d)(20\d{2})(?!\d)', str(period_label or ''))
        if m:
            year=int(m.group(1))
            year_end=f"{year}-12-31"
            entity_key=int(review_entity_id) if review_entity_id is not None else 0
            row=conn.execute("""SELECT closed_until FROM accounting_entity_closure
                                WHERE entity_key=?""",(entity_key,)).fetchone()
            closed_until=(row['closed_until'] if row else None)
            if closed_until and str(closed_until)[:10] >= year_end:
                status_by_order[15]={
                    'state':'ok',
                    'label':'Période réellement clôturée',
                    'detail':f'Clôture comptable enregistrée jusqu’au {str(closed_until)[:10]} · validation finale manuelle',
                }
            else:
                status_by_order[15]={
                    'state':'review',
                    'label':'Période à clôturer',
                    'detail':f'Aucune clôture comptable couvrant le {year_end} n’est enregistrée.',
                }
        else:
            status_by_order[15]={
                'state':'review',
                'label':'Clôture à vérifier',
                'detail':'Année de clôture non déterminée.',
            }

    # Provisions (point 13) : jamais de vert automatique.
    # L'absence d'écriture en compte 15/6815/7815 ne prouve pas l'absence de risque à provisionner.
    if any(i['item_order']==13 for i in items):
        if 'PROVISIONS_REVIEW_REQUIRED' in issue_codes:
            status_by_order[13]={
                'state':'review',
                'label':'Provisions à revoir',
                'detail':'Des comptes ou mouvements de provisions ont été détectés · validation humaine requise.',
            }
        else:
            status_by_order[13]={
                'state':'review',
                'label':'Revue humaine requise',
                'detail':"Aucune provision comptable détectée, mais l’absence de risque à provisionner ne peut pas être confirmée automatiquement.",
            }

    # Justificatifs fournisseurs : information utile rattachée au point "factures fournisseurs".
    if 2 in [i['item_order'] for i in items]:
        if 'MISSING_PURCHASE_DOCS' in issue_codes:
            status_by_order[2]={
                'state':'review',
                'label':'Justificatifs à vérifier',
                'detail':'Le diagnostic a détecté des factures fournisseurs sans justificatif.',
            }
        else:
            status_by_order[2]={
                'state':'ok',
                'label':'Justificatifs auto OK',
                'detail':'Aucun justificatif fournisseur manquant détecté · validation finale manuelle',
            }
    return status_by_order

def latest_review_diagnostics(conn, review_id):
    run=conn.execute('SELECT * FROM review_diagnostic_runs WHERE review_id=? ORDER BY id DESC LIMIT 1',(review_id,)).fetchone()
    if not run: return None,[]
    issues=conn.execute("SELECT * FROM review_diagnostic_issues WHERE run_id=? ORDER BY CASE severity WHEN 'blocker' THEN 0 ELSE 1 END,id",(run['id'],)).fetchall()
    return run,issues

def complete_review(conn,review_id,blocker_count=0):
    done,total=review_progress(conn,review_id)
    if done<total: raise ValueError(f"{total-done} point(s) de la checklist ne sont pas encore validés.")
    if blocker_count:
        raise ValueError(f"{blocker_count} anomalie(s) comptable(s) bloquante(s) doivent être corrigées avant clôture de la révision.")
    conn.execute("UPDATE reviews SET status='completed',completed_at=? WHERE id=?",(datetime.utcnow().isoformat(),review_id)); conn.commit()
