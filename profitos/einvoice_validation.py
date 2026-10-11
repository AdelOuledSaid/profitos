import json, math
from datetime import date
ALLOWED_VAT={0,0.9,1.05,1.75,2.1,5.5,7,8.5,9.2,9.6,10,13,19.6,20,20.6}

def validate_outgoing_invoice(inv):
    errors=[]
    number=(inv['invoice_number'] or '').strip()
    if not number: errors.append("Numéro de facture obligatoire.")
    try:
        issue=date.fromisoformat(str(inv['issue_date'])[:10])
    except Exception:
        issue=None; errors.append("Date d'émission invalide.")
    due_raw=inv['due_date']
    if due_raw:
        try:
            due=date.fromisoformat(str(due_raw)[:10])
            if issue and due<issue: errors.append("Échéance antérieure à la date d'émission.")
        except Exception: errors.append("Date d'échéance invalide.")
    try: items=json.loads(inv['line_items'] or '[]')
    except Exception: items=[]; errors.append("Lignes de facture illisibles.")
    if not items: errors.append("Au moins une ligne de facture est obligatoire.")
    calc_ht=calc_vat=0.0
    for i,line in enumerate(items,1):
        try:
            net=float(line.get('line_total')); rate=float(line.get('vat_rate') or 0)
            if not math.isfinite(net) or not math.isfinite(rate): raise ValueError
            if rate not in ALLOWED_VAT: errors.append(f"Ligne {i} : taux de TVA non admis.")
            calc_ht+=net; calc_vat+=round(net*rate/100,2)
        except Exception: errors.append(f"Ligne {i} : montant/TVA invalide.")
    def money(x):
        try:
            y=float(x); return y if math.isfinite(y) else None
        except Exception:return None
    ht,tva,ttc=money(inv['subtotal']),money(inv['vat_amount']),money(inv['total'])
    if None in (ht,tva,ttc): errors.append("Totaux de facture invalides.")
    else:
        if abs(round(calc_ht,2)-round(ht,2))>0.02: errors.append("Total HT incohérent avec les lignes.")
        if abs(round(calc_vat,2)-round(tva,2))>0.02: errors.append("TVA incohérente avec les lignes.")
        if abs(round(ht+tva,2)-round(ttc,2))>0.02: errors.append("Total TTC incohérent.")
    siren=(inv['client_siren'] or '').strip() if 'client_siren' in inv.keys() else ''
    if siren and (len(siren)!=9 or not siren.isdigit()): errors.append("SIREN client invalide.")
    return errors
