"""Télédéclarations : préparation, contrôle, dépôt et suivi des déclarations fiscales
transmises par un partenaire EDI agréé par la DGFiP.

Déclarations prises en charge :
- TVA_CA3   : déclaration de TVA 3310-CA3 (canal EDI-TVA) ;
- LIASSE    : liasse fiscale 2065 + 2033 (régime simplifié) ou 2050 à 2059 (régime
              normal) (canal EDI-TDFC) ;
- IS_2572   : relevé de solde de l'impôt sur les sociétés (canal EDI-Paiement) ;
- IS_2571   : relevé d'acompte d'impôt sur les sociétés (canal EDI-Paiement) ;
- CVAE_1330 : déclaration de la valeur ajoutée et des effectifs (canal EDI-TDFC).

Chaque déclaration est figée au moment du dépôt (contenu JSON + empreinte SHA-256),
une seule déclaration active par période (dépôt rectificatif possible après
acceptation), et son cycle de vie est tracé : déposée → accusé de réception →
acceptée, ou rejetée avec ses motifs.

Connecteurs : « simulation » (par défaut, aucune transmission réelle) ou « partenaire »
(API HTTP JSON d'un partenaire EDI, activée uniquement par configuration :
EDI_PARTNER_ENABLED=1, EDI_PARTNER_API_URL, EDI_PARTNER_API_KEY). Le contrat d'échange
du connecteur partenaire est décrit dans PartnerConnector ; il s'adapte au partenaire
retenu sans toucher au reste du module.
"""
import hashlib
import json
import os
from datetime import date, datetime, timezone

SCHEMA = 'profitos.teledeclaration.v1'

TYPES = {
    'TVA_CA3': {'label': 'Déclaration de TVA', 'form': '3310-CA3', 'channel': 'EDI-TVA'},
    'LIASSE': {'label': 'Liasse fiscale', 'form': '2065', 'channel': 'EDI-TDFC'},
    'IS_2572': {'label': "Relevé de solde de l'impôt sur les sociétés", 'form': '2572', 'channel': 'EDI-Paiement'},
    'IS_2571': {'label': "Relevé d'acompte d'impôt sur les sociétés", 'form': '2571', 'channel': 'EDI-Paiement'},
    'CVAE_1330': {'label': 'Déclaration de valeur ajoutée (CVAE)', 'form': '1330-CVAE', 'channel': 'EDI-TDFC'},
}

STATUS_LABELS = {
    'deposited': 'Déposée chez le partenaire EDI',
    'acknowledged': "Accusé de réception de l'administration",
    'accepted': 'Acceptée',
    'rejected': 'Rejetée',
}
ACTIVE_STATUSES = ('deposited', 'acknowledged', 'accepted')


class TeledeclarationError(ValueError):
    pass


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def luhn_ok(number):
    digits = [int(c) for c in str(number)]
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def siren_of(identity):
    siret = ''.join(ch for ch in str(identity.get('siret') or '') if ch.isdigit())
    return siret[:9] if len(siret) >= 9 else ''


# ------------------------------------------------------------------ construction
def _boxes_ca3(form):
    boxes = [{'code': '01', 'label': 'Ventes, prestations de services (base HT taxée)', 'amount': form['l01']}]
    for r in form['rates']:
        boxes.append({'code': r['line'], 'label': f"{r['label']} — base HT", 'amount': r['base'], 'kind': 'base'})
        boxes.append({'code': r['line'], 'label': f"{r['label']} — TVA due", 'amount': r['vat'], 'kind': 'taxe'})
    for code, key, label in (('16', 'l16', 'Total de la TVA brute due'),
                             ('19', 'l19', 'TVA déductible sur immobilisations'),
                             ('20', 'l20', 'TVA déductible sur autres biens et services'),
                             ('22', 'l22', 'Report du crédit de la déclaration précédente'),
                             ('23', 'l23', 'Total de la TVA déductible'),
                             ('25', 'l25', 'Crédit de TVA'),
                             ('27', 'l27', 'Crédit à reporter'),
                             ('28', 'l28', 'TVA nette due'),
                             ('32', 'l32', 'Total à payer')):
        boxes.append({'code': code, 'label': label, 'amount': form[key]})
    return boxes


def build_declaration(conn, decl_type, period_start, period_end, entity_id=None, identity=None, regime='normal',
                      settings=None, instalment_amount=None):
    """Construit le contenu canonique d'une déclaration (sans rien enregistrer) et la
    liste des anomalies bloquantes / avertissements."""
    if decl_type not in TYPES:
        raise TeledeclarationError("Type de déclaration inconnu.")
    start = date.fromisoformat(str(period_start)[:10])
    end = date.fromisoformat(str(period_end)[:10])
    if start > end:
        raise TeledeclarationError("Période invalide.")
    identity = identity or {}
    meta = dict(TYPES[decl_type])
    errors, warnings = [], []
    boxes, payment, annexes = [], None, None
    if decl_type == 'TVA_CA3':
        from profitos.vat_return import ca3_form
        form = ca3_form(conn, start.isoformat(), end.isoformat(), entity_id)
        boxes = _boxes_ca3(form)
        payment = form['l32'] or None
        if not form['liquidated']:
            warnings.append("L'écriture de liquidation de TVA de la période n'est pas encore passée.")
        if not any(b['amount'] for b in boxes):
            warnings.append("Déclaration à néant : aucune opération de TVA sur la période.")
    else:
        from profitos import corporate_tax as CT
        from profitos.liasse_normal import liasse_normale, liasse_normale_rows
        settings = settings if settings is not None else CT.get_fiscal_settings(conn, entity_id, end.isoformat())
        if decl_type == 'LIASSE' and regime == 'simplifie':
            from profitos.liasse import liasse_2033, liasse_rows
            l33 = liasse_2033(conn, start, end, entity_id)
            meta['annexes'] = '2033-A à 2033-G'
            boxes = []
            for r in liasse_rows(l33):
                if r['Tableau'] == '2033-A Actif':
                    boxes.append({'code': r['Case'], 'label': f"2033-A — {r['Libellé']} (brut)", 'amount': r['Brut']})
                    if r.get('Case amort.'):
                        boxes.append({'code': r['Case amort.'], 'label': f"2033-A — {r['Libellé']} (amortissements)",
                                      'amount': r['Amortissements / dépréciations']})
                else:
                    boxes.append({'code': r['Case'], 'label': f"{r['Tableau']} — {r['Libellé']}", 'amount': r['Net']})
            if not l33['balanced']:
                errors.append("Le bilan 2033-A n'est pas équilibré.")
        else:
            l = liasse_normale(conn, start, end, entity_id, settings=settings)
            if decl_type == 'LIASSE':
                meta['annexes'] = '2050 à 2059'
                boxes = [{'code': r['Case'], 'label': f"{r['Formulaire']} — {r['Libellé']}", 'amount': r['Montant'],
                          'form': r['Formulaire']} for r in liasse_normale_rows(l)]
                if not l['balanced']:
                    errors.append("Le bilan (2050 / 2051) n'est pas équilibré.")
                for msg in l['checks']:
                    if 'déséquilibré' not in msg:
                        warnings.append(msg)
            elif decl_type in ('IS_2572', 'IS_2571'):
                eligible = CT.pme_eligible(l['chiffre_affaires'], start, end, settings.get('capital_libere'),
                                           settings.get('detention_pp'))
                acomptes = settings.get('acomptes_verses')
                if acomptes in (None, ''):
                    acomptes = CT.acomptes_from_ledger(conn, start, end, entity_id)
                res = CT.compute_is(l['fiscal']['XN'], start, end, l['chiffre_affaires'], eligible,
                                    credits=settings.get('credits_impot') or 0, acomptes=acomptes)
                if decl_type == 'IS_2572':
                    boxes = [{'code': 'IS', 'label': 'Impôt sur les sociétés', 'amount': res['is_total']},
                             {'code': 'CS', 'label': 'Contribution sociale', 'amount': res['contribution_sociale']},
                             {'code': 'CI', 'label': "Crédits d'impôt imputés", 'amount': res['credits']},
                             {'code': 'AC', 'label': 'Acomptes versés', 'amount': res['acomptes_verses']},
                             {'code': 'SO', 'label': 'Solde à payer' if res['solde'] >= 0 else 'Excédent à restituer',
                              'amount': abs(res['solde'])}]
                    payment = res['solde'] if res['solde'] > 0 else None
                else:
                    amount = float(instalment_amount if instalment_amount is not None else round(res['is_total'] / 4))
                    if amount <= 0:
                        errors.append("Aucun acompte d'impôt sur les sociétés n'est dû (IS de référence nul).")
                    boxes = [{'code': 'AC', 'label': "Montant de l'acompte", 'amount': amount},
                             {'code': 'IR', 'label': 'IS de référence (dernier exercice clos)', 'amount': res['is_total']}]
                    payment = amount or None
            elif decl_type == 'CVAE_1330':
                cv = CT.compute_cvae(l['va']['OX'], l['va']['OG'], start, end)
                if not cv['declaration_obligatoire']:
                    warnings.append("Chiffre d'affaires inférieur ou égal à 152 500 € : la déclaration 1330-CVAE n'est pas obligatoire.")
                boxes = [{'code': 'CA', 'label': "Chiffre d'affaires de référence", 'amount': cv['turnover']},
                         {'code': 'VA', 'label': 'Valeur ajoutée produite', 'amount': cv['valeur_ajoutee']},
                         {'code': 'VP', 'label': 'Valeur ajoutée plafonnée', 'amount': cv['va_plafonnee']},
                         {'code': 'CV', 'label': 'CVAE due', 'amount': cv['cvae']}]
    if decl_type == 'LIASSE':
        from profitos.automation import precontrols
        for x in precontrols(conn, start, end, entity_id):
            if x['level'] == 'warning':
                warnings.append(x['label'] + (f" ({x['detail']})" if x['detail'] else '') + '.')
    siren = siren_of(identity)
    if not siren or not luhn_ok(siren):
        errors.append("SIREN de l'entreprise absent ou invalide : complétez le profil entreprise.")
    if not identity.get('name'):
        errors.append("Dénomination de l'entreprise absente : complétez le profil entreprise.")
    from profitos.accounting import paris_today
    if end >= paris_today():
        warnings.append("La période n'est pas terminée : le dépôt sera refusé tant qu'elle n'est pas close.")
        errors.append("Période non terminée.")
    payload = {
        'schema': SCHEMA, 'type': decl_type, 'form': meta['form'], 'channel': meta['channel'],
        'annexes': meta.get('annexes'),
        'declarant': {'siren': siren, 'siret': ''.join(ch for ch in str(identity.get('siret') or '') if ch.isdigit()),
                      'name': identity.get('name') or '', 'vat_number': identity.get('vat_number') or ''},
        'period': {'start': start.isoformat(), 'end': end.isoformat()},
        'boxes': [dict(b, amount=int(round(float(b['amount'] or 0)))) for b in boxes],
        'payment': {'amount': int(round(float(payment)))} if payment else None,
    }
    return {'payload': payload, 'errors': errors, 'warnings': warnings, 'meta': meta}


def payload_hash(payload):
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


# ------------------------------------------------------------------ connecteurs
class SimulationConnector:
    """Aucune transmission : simule le cycle du partenaire EDI (dépôt, accusé de
    réception, acceptation) pour tester le parcours de bout en bout."""
    name = 'simulation'
    production = False

    def submit(self, payload, digest):
        return {'remote_id': 'SIM-' + digest[:12].upper(), 'status': 'deposited'}

    def fetch_status(self, remote_id, current_status, payload):
        if current_status == 'deposited':
            return {'status': 'acknowledged', 'ack_number': 'AR-' + remote_id[4:12],
                    'message': "Accusé de réception simulé : aucune transmission réelle."}
        if current_status == 'acknowledged':
            return {'status': 'accepted', 'message': 'Déclaration intégrée (simulation).'}
        return {'status': current_status}


class PartnerConnector:
    """Connecteur HTTP JSON vers le partenaire EDI.

    Contrat attendu (à aligner sur l'API du partenaire retenu) :
      POST {EDI_PARTNER_API_URL}/declarations      corps = contenu canonique ProfitOS
           → 201 {"id": "...", "status": "deposited"}
      GET  {EDI_PARTNER_API_URL}/declarations/{id}
           → 200 {"status": "deposited|acknowledged|accepted|rejected",
                  "ack_number": "...", "rejection_reasons": ["..."]}
    Authentification : en-tête Authorization: Bearer {EDI_PARTNER_API_KEY}.
    """
    name = 'partenaire'
    production = True

    def __init__(self, base_url, api_key, timeout=20):
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.timeout = timeout

    def _headers(self, digest=None):
        h = {'Authorization': f'Bearer {self.api_key}', 'Content-Type': 'application/json',
             'User-Agent': 'ProfitOS-EDI/1'}
        if digest:
            h['Idempotency-Key'] = digest
        return h

    def submit(self, payload, digest):
        import requests
        resp = requests.post(f'{self.base_url}/declarations', json=payload, headers=self._headers(digest),
                             timeout=self.timeout)
        if resp.status_code not in (200, 201, 202):
            raise TeledeclarationError(f"Le partenaire EDI a refusé le dépôt (HTTP {resp.status_code}).")
        data = resp.json()
        if not data.get('id'):
            raise TeledeclarationError("Réponse du partenaire EDI sans identifiant de dépôt.")
        return {'remote_id': str(data['id'])[:120], 'status': data.get('status') or 'deposited'}

    def fetch_status(self, remote_id, current_status, payload):
        import requests
        resp = requests.get(f'{self.base_url}/declarations/{remote_id}', headers=self._headers(), timeout=self.timeout)
        if resp.status_code != 200:
            raise TeledeclarationError(f"Statut indisponible chez le partenaire EDI (HTTP {resp.status_code}).")
        data = resp.json()
        status = data.get('status') if data.get('status') in STATUS_LABELS else current_status
        return {'status': status, 'ack_number': data.get('ack_number'),
                'rejection_reasons': data.get('rejection_reasons') or [], 'message': data.get('message')}


def partner_configured():
    return bool(os.environ.get('EDI_PARTNER_ENABLED') == '1' and os.environ.get('EDI_PARTNER_API_URL', '').startswith('https://')
                and os.environ.get('EDI_PARTNER_API_KEY'))


def get_connector():
    if partner_configured():
        return PartnerConnector(os.environ['EDI_PARTNER_API_URL'], os.environ['EDI_PARTNER_API_KEY'])
    return SimulationConnector()


# ------------------------------------------------------------------ stockage
def _ef(entity_id):
    return ('entity_id=?', (entity_id,)) if entity_id else ('entity_id IS NULL', ())


def active_declaration(conn, entity_id, decl_type, period_start, period_end):
    ef, ep = _ef(entity_id)
    return conn.execute(
        f"""SELECT * FROM teledeclarations WHERE {ef} AND decl_type=? AND period_start=? AND period_end=?
            AND status IN ('deposited','acknowledged','accepted') ORDER BY id DESC LIMIT 1""",
        (*ep, decl_type, str(period_start)[:10], str(period_end)[:10])).fetchone()


def submit_declaration(conn, built, entity_id=None, user=None, corrective=False, connector=None):
    """Dépose une déclaration contrôlée. Refuse s'il reste une anomalie bloquante ou si une
    déclaration est déjà en cours pour la période (sauf rectificative après acceptation)."""
    if built['errors']:
        raise TeledeclarationError('Dépôt impossible : ' + ' '.join(built['errors']))
    payload = built['payload']
    existing = active_declaration(conn, entity_id, payload['type'], payload['period']['start'], payload['period']['end'])
    if existing and not (corrective and existing['status'] == 'accepted'):
        raise TeledeclarationError("Une déclaration est déjà déposée pour cette période"
                                   + (" : attendez son acceptation pour déposer une rectificative." if existing['status'] != 'accepted'
                                      else " : cochez « déclaration rectificative » pour la corriger."))
    connector = connector or get_connector()
    payload = dict(payload, generated_at=_now(), corrective=bool(corrective and existing))
    digest = payload_hash(payload)
    result = connector.submit(payload, digest)
    now = _now()
    conn.execute(
        """INSERT INTO teledeclarations(entity_id,decl_type,form,channel,period_start,period_end,payload_json,payload_hash,
               status,mode,provider,remote_id,corrective,replaces_id,payment_amount,submitted_by,submitted_at,updated_at,history_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (entity_id, payload['type'], payload['form'], payload['channel'], payload['period']['start'], payload['period']['end'],
         json.dumps(payload, ensure_ascii=False), digest, result.get('status') or 'deposited',
         'production' if connector.production else 'simulation', connector.name, result['remote_id'],
         1 if payload['corrective'] else 0, existing['id'] if (existing and payload['corrective']) else None,
         (payload.get('payment') or {}).get('amount'), user, now, now,
         json.dumps([{'at': now, 'status': result.get('status') or 'deposited', 'message': 'Dépôt effectué'}], ensure_ascii=False)))
    conn.commit()
    return conn.execute('SELECT last_insert_rowid()').fetchone()[0]


def refresh_status(conn, row, connector=None):
    """Interroge le partenaire et enregistre le nouveau statut (avec son historique)."""
    if row['status'] in ('accepted', 'rejected'):
        return row['status']
    connector = connector or (SimulationConnector() if row['mode'] == 'simulation' else get_connector())
    payload = json.loads(row['payload_json'] or '{}')
    res = connector.fetch_status(row['remote_id'], row['status'], payload)
    status = res.get('status') or row['status']
    if status == row['status'] and not res.get('ack_number'):
        return status
    history = json.loads(row['history_json'] or '[]')
    now = _now()
    history.append({'at': now, 'status': status, 'message': res.get('message') or STATUS_LABELS.get(status, status)})
    reasons = res.get('rejection_reasons') or []
    rejection = json.dumps(reasons, ensure_ascii=False) if reasons else None
    if res.get('ack_number') and not row['ack_number']:
        conn.execute('UPDATE teledeclarations SET ack_number=?,ack_at=? WHERE id=?', (str(res['ack_number'])[:120], now, row['id']))
    conn.execute('UPDATE teledeclarations SET status=?,rejection_json=?,history_json=?,updated_at=? WHERE id=?',
                 (status, rejection, json.dumps(history, ensure_ascii=False), now, row['id']))
    conn.commit()
    return status


def list_declarations(conn, entity_id=None, limit=50):
    ef, ep = _ef(entity_id)
    return conn.execute(f'SELECT * FROM teledeclarations WHERE {ef} ORDER BY id DESC LIMIT ?', (*ep, limit)).fetchall()
