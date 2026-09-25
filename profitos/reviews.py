"""Révision comptable — feuilles de travail structurées, courante
(mensuelle) et annuelle (clôture), avec liens directs vers les modules
concernés déjà construits dans ProfitOS quand c'est pertinent.

Ce module ne calcule rien : c'est un outil de suivi/workflow pour
l'expert-comptable ou le gérant, pas un moteur comptable.
"""
from datetime import datetime

# (label, route_name_ou_None) — route_name doit être un nom de route
# paramétrable sans argument obligatoire, sinon laisser None.
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
    ("Comptes d'attente (467000) soldés ou justifiés", 'accounting_chart'),
    ("Provisions pour risques et charges revues", None),
    ("Créances douteuses ou litigieuses identifiées", None),
    ("Période clôturée dans ProfitOS une fois tout vérifié", 'accounting_closure'),
]


def seed_review_items(conn, review_id, review_type):
    """Copie les items du modèle par défaut (courante ou annuelle) dans une
    nouvelle instance de révision. Chaque instance a ses propres items,
    indépendants du modèle — cocher un item sur une révision ne modifie
    jamais le modèle ni les autres révisions."""
    items = ANNUELLE_ITEMS if review_type == 'annuelle' else COURANTE_ITEMS
    for i, (label, route_name) in enumerate(items):
        conn.execute(
            'INSERT INTO review_items(review_id,item_order,label,linked_route) VALUES(?,?,?,?)',
            (review_id, i, label, route_name),
        )
    conn.commit()


def create_review(conn, review_type, period_label, entity_id=None, created_by=None):
    """Crée une nouvelle révision (courante ou annuelle) pour une période
    donnée, avec sa checklist pré-remplie depuis le modèle par défaut."""
    if review_type not in ('courante', 'annuelle'):
        raise ValueError(f"Type de révision inconnu : {review_type!r} (attendu courante ou annuelle).")
    conn.execute(
        'INSERT INTO reviews(entity_id,review_type,period_label,status,created_at,created_by) VALUES(?,?,?,?,?,?)',
        (entity_id, review_type, period_label, 'in_progress', datetime.utcnow().isoformat(), created_by),
    )
    conn.commit()
    review_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    seed_review_items(conn, review_id, review_type)
    return review_id


def toggle_review_item(conn, item_id, checked, note=None, checked_by=None):
    """Coche/décoche un item de la checklist, avec une note optionnelle
    (feuille de travail). Décocher efface aussi qui/quand l'avait coché,
    pour ne jamais laisser une trace trompeuse d'une validation retirée."""
    if checked:
        conn.execute(
            'UPDATE review_items SET checked=1,note=?,checked_by=?,checked_at=? WHERE id=?',
            (note, checked_by, datetime.utcnow().isoformat(), item_id),
        )
    else:
        conn.execute(
            'UPDATE review_items SET checked=0,note=?,checked_by=NULL,checked_at=NULL WHERE id=?',
            (note, item_id),
        )
    conn.commit()


def review_progress(conn, review_id):
    """Renvoie (coché, total) pour une révision donnée."""
    row = conn.execute(
        'SELECT COUNT(*) total, COALESCE(SUM(checked),0) done FROM review_items WHERE review_id=?',
        (review_id,),
    ).fetchone()
    return row['done'], row['total']


def complete_review(conn, review_id):
    """Marque une révision comme terminée. Refuse si des items restent
    décochés — une révision \"terminée\" avec des cases vides n'a aucun
    sens et masquerait un oubli."""
    done, total = review_progress(conn, review_id)
    if done < total:
        raise ValueError(f"{total - done} point(s) de la checklist ne sont pas encore validés.")
    conn.execute(
        "UPDATE reviews SET status='completed',completed_at=? WHERE id=?",
        (datetime.utcnow().isoformat(), review_id),
    )
    conn.commit()
