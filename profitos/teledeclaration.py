"""Télédéclarations : préparation, contrôle, dépôt et suivi des déclarations fiscales
transmises par un partenaire EDI agréé par la DGFiP.

Déclarations prises en charge :
- TVA_CA3   : déclaration de TVA 3310-CA3 (canal EDI-TVA) ;
- TVA_CA12  : déclaration annuelle de TVA 3517-S du régime simplifié (canal EDI-TVA) ;
- TVA_3514  : acompte semestriel de TVA du régime simplifié (canal EDI-Paiement) ;
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
import re
import os
from datetime import date, datetime, timezone

SCHEMA = 'profitos.teledeclaration.v1'

INSTALMENT_TYPES = ('IS_2571', 'TVA_3514')
MAX_PERIOD_DAYS = 731          # premier exercice : 24 mois au plus
PARTNER_STATUSES = ('deposited', 'acknowledged', 'accepted', 'rejected')

TYPES = {
    'TVA_CA3': {'label': 'Déclaration de TVA', 'form': '3310-CA3', 'channel': 'EDI-TVA'},
    'LIASSE': {'label': 'Liasse fiscale', 'form': '2065', 'channel': 'EDI-TDFC'},
    'TVA_CA12': {'label': 'Déclaration annuelle de TVA (régime simplifié)', 'form': '3517-S CA12', 'channel': 'EDI-TVA'},
    'TVA_3514': {'label': "Acompte de TVA (régime simplifié)", 'form': '3514', 'channel': 'EDI-Paiement'},
    'IS_2572': {'label': "Relevé de solde de l'impôt sur les sociétés", 'form': '2572', 'channel': 'EDI-Paiement'},
    'IS_2571': {'label': "Relevé d'acompte d'impôt sur les sociétés", 'form': '2571', 'channel': 'EDI-Paiement'},
    'CVAE_1330': {'label': 'Déclaration de valeur ajoutée (CVAE)', 'form': '1330-CVAE', 'channel': 'EDI-TDFC'},
    'DAS2': {'label': 'Déclaration des honoraires, commissions et droits d\'auteur versés', 'form': 'DAS2', 'channel': 'EDI-TDFC'},
}

STATUS_LABELS = {
    'prepared': 'Préparée — à déposer sur impots.gouv.fr',
    # Dépôt fait par l'entreprise sur impots.gouv.fr (aucune de ces étapes ne vaut
    # acceptation par l'administration, que seul un accusé EDI peut constater) :
    'declared': 'Dépôt déclaré — justificatif non vérifié',
    'to_verify': 'Justificatif à vérifier — écarts détectés',
    'confirmed': 'Dépôt confirmé par l\'accusé officiel',
    'filed': 'Dépôt déclaré — justificatif non vérifié',  # ancien statut, assimilé à « déclaré »
    'deposited': 'Déposée chez le partenaire EDI',
    'acknowledged': "Accusé de réception de l'administration",
    'accepted': "Acceptée par l'administration (accusé EDI)",
    'rejected': 'Rejetée',
}
MANUAL_FILED_STATUSES = ('declared', 'to_verify', 'confirmed', 'filed')
def status_label(status, mode=None):
    """Libellé d'un statut ; un dépôt simulé est toujours signalé comme tel."""
    if mode == 'simulation':
        return {'accepted': 'Acceptée (simulation)', 'rejected': 'Rejetée (simulation)'}.get(
            status, STATUS_LABELS.get(status, status) + ' (simulation)') + " — rien n'a été transmis"
    return STATUS_LABELS.get(status, status)


ACTIVE_STATUSES = ('deposited', 'acknowledged', 'accepted') + MANUAL_FILED_STATUSES


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


def reference_is(conn, start, end, entity_id=None, settings=None):
    """IS de l'exercice (calcul de la 2572), servant de référence aux acomptes suivants."""
    from profitos import corporate_tax as CT
    from profitos.liasse_normal import liasse_normale
    settings = settings if settings is not None else CT.get_fiscal_settings(conn, entity_id, end.isoformat())
    l = liasse_normale(conn, start, end, entity_id, settings=settings)
    eligible = CT.pme_eligible(l['chiffre_affaires'], start, end, settings.get('capital_libere'), settings.get('detention_pp'))
    acomptes = settings.get('acomptes_verses')
    if acomptes in (None, ''):
        acomptes = CT.acomptes_from_ledger(conn, start, end, entity_id)
    return CT.compute_is(l['fiscal']['XN'], start, end, l['chiffre_affaires'], eligible,
                         credits=settings.get('credits_impot') or 0, acomptes=acomptes)


def build_declaration(conn, decl_type, period_start, period_end, entity_id=None, identity=None, regime='normal',
                      settings=None, instalment_amount=None, due_date=None):
    """Construit le contenu canonique d'une déclaration (sans rien enregistrer) et la
    liste des anomalies bloquantes / avertissements."""
    if decl_type not in TYPES:
        raise TeledeclarationError("Type de déclaration inconnu.")
    start = date.fromisoformat(str(period_start)[:10])
    end = date.fromisoformat(str(period_end)[:10])
    if start > end:
        raise TeledeclarationError("Période invalide.")
    if (end - start).days > MAX_PERIOD_DAYS:
        raise TeledeclarationError("Période trop longue : un exercice compte 24 mois au plus.")
    from profitos.accounting import paris_today as _today
    if start.year < 2000 or end.year > _today().year + 1:
        raise TeledeclarationError("Période hors des années gérées.")
    if instalment_amount is not None:
        try:
            instalment_amount = float(instalment_amount)
        except (TypeError, ValueError):
            raise TeledeclarationError("Montant d'acompte invalide.")
        if instalment_amount != instalment_amount or abs(instalment_amount) > 1e12:
            raise TeledeclarationError("Montant d'acompte invalide.")
    identity = identity or {}
    meta = dict(TYPES[decl_type])
    if decl_type in INSTALMENT_TYPES:
        due_date = date.fromisoformat(str(due_date)[:10]) if due_date else default_due_date(conn, decl_type, end, entity_id)
    else:
        due_date = None
    errors, warnings = [], []
    boxes, payment, annex_lists = [], None, None
    if decl_type == 'TVA_CA3':
        from profitos.vat_return import ca3_form
        form = ca3_form(conn, start.isoformat(), end.isoformat(), entity_id)
        boxes = _boxes_ca3(form)
        payment = form['l32'] or None
        if not form['liquidated']:
            warnings.append("L'écriture de liquidation de TVA de la période n'est pas encore passée.")
        from profitos.vat_return import unliquidated_before
        if unliquidated_before(conn, start.isoformat(), entity_id):
            warnings.append("La TVA d'une période précédente n'a pas été liquidée : le crédit à reporter (ligne 22) "
                            "peut être incomplet. Passez d'abord les liquidations des périodes antérieures.")
        if not any(b['amount'] for b in boxes):
            warnings.append("Déclaration à néant : aucune opération de TVA sur la période.")
    elif decl_type in ('TVA_CA12', 'TVA_3514'):
        from profitos.vat_return import ca12_form, CA12_BOXES
        from profitos import corporate_tax as CT
        fs = settings if settings is not None else CT.get_fiscal_settings(conn, entity_id, end.isoformat())
        form = ca12_form(conn, start.isoformat(), end.isoformat(), entity_id, acomptes=fs.get('tva_acomptes_verses'))
        warnings += form['warnings']
        if decl_type == 'TVA_CA12':
            for x in form['lines']:
                boxes.append({'code': x['line'], 'label': f"{x['label']} — base HT (case {x['base_code']})", 'amount': x['base'], 'kind': 'base'})
                boxes.append({'code': x['line'], 'label': f"{x['label']} — taxe due", 'amount': x['vat'], 'kind': 'taxe'})
            for line, code, label, key in CA12_BOXES:
                boxes.append({'code': line, 'label': label + (f' (case {code})' if code else ''), 'amount': form[key]})
            payment = form['l56'] or None
        else:
            prev = ca12_form(conn, start.isoformat(), end.isoformat(), entity_id)
            match = [a for a in prev['instalments'] if a['date'] == due_date] or prev['instalments'][:1]
            amount = float(instalment_amount if instalment_amount is not None else (match[0]['amount'] if match else 0))
            if amount <= 0:
                errors.append("Aucun acompte de TVA n'est dû (base de l'exercice précédent inférieure à 1 000 €).")
            boxes = [{'code': 'AC', 'label': "Montant de l'acompte", 'amount': amount},
                     {'code': '57', 'label': "Base de calcul des acomptes (ligne 57 de la dernière CA12)", 'amount': prev['l57']}]
            payment = amount or None
    elif decl_type == 'DAS2':
        from profitos.tiers_declarations import das2, DAS2_CATEGORIES
        if (start.month, start.day, end.month, end.day) != (1, 1, 12, 31) or start.year != end.year:
            errors.append("La DAS2 porte sur une année civile (du 1er janvier au 31 décembre).")
        d2 = das2(conn, end.year, entity_id)
        for r in d2['included']:
            boxes.append({'code': r['category'], 'label': f"{r['name']} — {DAS2_CATEGORIES.get(r['category'], r['category'])}"
                                                         + (f" — SIRET {r['siret']}" if r['siret'] else ''), 'amount': r['amount']})
        annex_lists = {'DAS2': [{k: r[k] for k in ('name', 'siret', 'address', 'profession', 'category', 'amount')}
                                for r in d2['included']]}
        errors += d2['issues']
        if not d2['included']:
            errors.append("Aucun bénéficiaire n'a reçu plus de 1 200 € sur l'année : pas de DAS2 à déposer.")
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
            from profitos.liasse_2033_annexes import annexes_2033, annexes_2033_rows
            from profitos.liasse_annexes import form_2065
            profile = CT.get_fiscal_profile(conn, entity_id)
            ln = liasse_normale(conn, start, end, entity_id, settings=settings)
            ax = annexes_2033(conn, start, end, entity_id, profile=profile, settings=settings, liasse=ln)
            eligible_s = CT.pme_eligible(ln['chiffre_affaires'], start, end, settings.get('capital_libere'), settings.get('detention_pp'))
            is_s = CT.compute_is(ln['fiscal']['XN'], start, end, ln['chiffre_affaires'], eligible_s,
                                 credits=settings.get('credits_impot') or 0)
            f2065 = form_2065(identity, dict(profile, tax_regime='simplifie'), ln, is_s, start, end)
            for r in annexes_2033_rows(ax, f2065):
                boxes.append({'code': r['Case'], 'label': f"{r['Tableau']} — {r['Libellé']}", 'amount': r['Montant']})
            annex_lists = {'2065': f2065, '2033-F': ax['capital'], '2033-G': ax['filiales'], '2033-C': ax['pv']}
            warnings += ax['checks']
            if not f2065['signataire']:
                warnings.append("Signataire de la liasse (2065) non renseigné : complétez le profil fiscal.")
        else:
            l = liasse_normale(conn, start, end, entity_id, settings=settings)
            if decl_type == 'LIASSE':
                meta['annexes'] = '2050 à 2059'
                from profitos.liasse_annexes import annexes, annexes_rows, form_2065
                profile = CT.get_fiscal_profile(conn, entity_id)
                ax = annexes(conn, start, end, entity_id, profile=profile, settings=settings)
                eligible_l = CT.pme_eligible(l['chiffre_affaires'], start, end, settings.get('capital_libere'), settings.get('detention_pp'))
                is_l = CT.compute_is(l['fiscal']['XN'], start, end, l['chiffre_affaires'], eligible_l,
                                     credits=settings.get('credits_impot') or 0)
                f2065 = form_2065(identity, profile, l, is_l, start, end)
                boxes = [{'code': r['Case'], 'label': f"{r['Formulaire']} — {r['Libellé']}", 'amount': r['Montant'],
                          'form': r['Formulaire']} for r in liasse_normale_rows(l) + annexes_rows(ax, f2065)]
                annex_lists = {'2065': f2065, '2059-F': ax['capital_2059f'], '2059-G': ax['filiales_2059g'],
                               '2059-A': ax['pv_2059a']}
                warnings += ax['checks']
                if not f2065['signataire']:
                    warnings.append("Signataire de la liasse (2065) non renseigné : complétez le profil fiscal.")
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
                    due_amount = CT.instalment_amount(res['is_total'], start, end)
                    ref12 = CT.reference_is_12m(res['is_total'], start, end)
                    amount = float(instalment_amount if instalment_amount is not None else due_amount)
                    if not due_amount:
                        msg = ("Dispense d'acomptes : l'IS de référence" + (" ramené à 12 mois" if abs(ref12 - res['is_total']) > 0.5 else '')
                               + f" ({round(ref12):,} €".replace(',', ' ') + ") ne dépasse pas 3 000 €.")
                        (warnings if amount > 0 else errors).append(msg)
                    elif amount <= 0:
                        errors.append("Montant de l'acompte nul.")
                    boxes = [{'code': 'AC', 'label': "Montant de l'acompte", 'amount': amount},
                             {'code': 'IR', 'label': 'IS de référence (dernier exercice clos)', 'amount': res['is_total']}]
                    if abs(ref12 - res['is_total']) > 0.5:
                        boxes.append({'code': 'IR12', 'label': "IS de référence ramené à 12 mois", 'amount': round(ref12, 2)})
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
        from profitos import local_taxes as LT
        from profitos.corporate_tax import get_fiscal_profile
        dl = LT.decloyer(get_fiscal_profile(conn, entity_id), LT.rent_booked(conn, start, end, entity_id))
        for r in LT.decloyer_rows(dl):
            boxes.append({'code': r['Case'], 'label': f"DECLOYER — {r['Libellé']}", 'amount': r['Montant'], 'form': 'DECLOYER'})
        if dl['premises']:
            annex_lists = dict(annex_lists or {}, DECLOYER=dl['premises'])
            meta['annexes'] = (meta.get('annexes') or '') + ' + DECLOYER'
        warnings += dl['warnings']
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
    if due_date and due_date <= end:
        errors.append("L'échéance de l'acompte doit suivre l'exercice de référence.")
    elif due_date and (due_date - end).days > 731:
        errors.append("L'échéance de l'acompte doit tomber dans les deux ans qui suivent l'exercice de référence.")
    if end >= paris_today():
        warnings.append("La période n'est pas terminée : le dépôt sera refusé tant qu'elle n'est pas close.")
        errors.append("Période non terminée.")
    pay = None
    if payment:
        from profitos import corporate_tax as CT2
        profile = CT2.get_fiscal_profile(conn, entity_id)
        pay = {'amount': int(round(float(payment)))}
        if CT2.mandate_ready(profile):
            m = profile['mandate']
            pay.update({'method': 'prelevement_edi', 'iban': ''.join(m['iban'].split()).upper(), 'bic': (m.get('bic') or '').upper(),
                        'holder': m.get('holder') or identity.get('name') or '', 'rum': m.get('rum')})
        else:
            pay['method'] = 'hors_edi'
            warnings.append("Aucun mandat de prélèvement enregistré : le paiement ne sera pas joint à la déclaration. "
                            "Renseignez le compte et le mandat dans le profil fiscal, ou payez depuis votre espace professionnel.")
    payload = {
        'schema': SCHEMA, 'type': decl_type, 'form': meta['form'], 'channel': meta['channel'],
        'annexes': meta.get('annexes'),
        'declarant': {'siren': siren, 'siret': ''.join(ch for ch in str(identity.get('siret') or '') if ch.isdigit()),
                      'name': identity.get('name') or '', 'vat_number': identity.get('vat_number') or ''},
        'period': {'start': start.isoformat(), 'end': end.isoformat()},
        'due_date': due_date.isoformat() if due_date else None,
        'boxes': [dict(b, amount=int(round(float(b['amount'] or 0)))) for b in boxes],
        'payment': pay,
        'annex_lists': annex_lists,
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


def active_declaration(conn, entity_id, decl_type, period_start, period_end, due_date=None, include_simulation=False):
    """Déclaration en cours pour la période. Les acomptes (2571, 3514) se distinguent en
    plus par leur échéance : plusieurs acomptes portent sur le même exercice de référence.
    Un dépôt simulé ne compte jamais comme un dépôt réel (échéancier, relances, doublons),
    sauf pour éviter deux simulations identiques (include_simulation)."""
    ef, ep = _ef(entity_id)
    df, dp = ('', ())
    if decl_type in INSTALMENT_TYPES and due_date:
        df, dp = ' AND due_date=?', (str(due_date)[:10],)
    sim = '' if include_simulation else " AND mode<>'simulation'"
    return conn.execute(
        f"""SELECT * FROM teledeclarations WHERE {ef} AND decl_type=? AND period_start=? AND period_end=?{df}{sim}
            AND status IN ('deposited','acknowledged','accepted','declared','to_verify','confirmed','filed') ORDER BY id DESC LIMIT 1""",
        (*ep, decl_type, str(period_start)[:10], str(period_end)[:10], *dp)).fetchone()


def default_due_date(conn, decl_type, base_end, entity_id=None, today=None):
    """Prochaine échéance d'acompte (ou la dernière de l'exercice suivant si toutes sont passées)."""
    from profitos.accounting import paris_today
    from profitos import ledger as L
    from datetime import timedelta
    today = today or paris_today()
    base_end = date.fromisoformat(str(base_end)[:10])
    nxt = L.fiscal_year_for(conn, base_end + timedelta(days=1), entity_id)
    if decl_type == 'TVA_3514':
        y = base_end.year + 1
        dates = [date(y, 7, 15), date(y, 12, 15)]
    else:
        from profitos.corporate_tax import instalment_dates
        dates = list(instalment_dates(nxt['start'], nxt['end']))
    upcoming = [d for d in dates if d >= today]
    return upcoming[0] if upcoming else dates[-1]


def submit_declaration(conn, built, entity_id=None, user=None, corrective=False, connector=None):
    """Dépose une déclaration contrôlée. Refuse s'il reste une anomalie bloquante ou si une
    déclaration est déjà en cours pour la période (sauf rectificative après acceptation)."""
    if built['errors']:
        raise TeledeclarationError('Dépôt impossible : ' + ' '.join(built['errors']))
    payload = built['payload']
    connector = connector or get_connector()
    existing = active_declaration(conn, entity_id, payload['type'], payload['period']['start'], payload['period']['end'],
                                  payload.get('due_date'), include_simulation=not connector.production)
    if existing and not (corrective and existing['status'] == 'accepted'):
        raise TeledeclarationError("Une déclaration est déjà déposée pour cette période"
                                   + (" : attendez son acceptation pour déposer une rectificative." if existing['status'] != 'accepted'
                                      else " : cochez « déclaration rectificative » pour la corriger."))
    payload = dict(payload, generated_at=_now(), corrective=bool(corrective and existing))
    digest = payload_hash(payload)
    result = connector.submit(payload, digest)
    now = _now()
    conn.execute(
        """INSERT INTO teledeclarations(entity_id,decl_type,form,channel,period_start,period_end,payload_json,payload_hash,
               status,mode,provider,remote_id,corrective,replaces_id,payment_amount,submitted_by,submitted_at,updated_at,history_json,due_date)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (entity_id, payload['type'], payload['form'], payload['channel'], payload['period']['start'], payload['period']['end'],
         json.dumps(payload, ensure_ascii=False), digest, result.get('status') or 'deposited',
         'production' if connector.production else 'simulation', connector.name, result['remote_id'],
         1 if payload['corrective'] else 0, existing['id'] if (existing and payload['corrective']) else None,
         (payload.get('payment') or {}).get('amount'), user, now, now,
         json.dumps([{'at': now, 'status': result.get('status') or 'deposited', 'message': 'Dépôt effectué'}], ensure_ascii=False),
         payload.get('due_date')))
    conn.commit()
    return conn.execute('SELECT last_insert_rowid()').fetchone()[0]


def refresh_status(conn, row, connector=None):
    """Interroge le partenaire et enregistre le nouveau statut (avec son historique)."""
    if row['status'] in ('accepted', 'rejected') or row['mode'] == 'manual':
        return row['status']
    connector = connector or (SimulationConnector() if row['mode'] == 'simulation' else get_connector())
    payload = json.loads(row['payload_json'] or '{}')
    res = connector.fetch_status(row['remote_id'], row['status'], payload)
    status = res.get('status') or row['status']
    if status not in PARTNER_STATUSES:
        # Statut inconnu renvoyé par le partenaire : ignoré (jamais un statut de dépôt manuel).
        status = row['status']
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


# ------------------------------------------------------------- dépôt par l'entreprise (EFI)
# Déclarations que l'entreprise peut saisir elle-même dans son espace professionnel sur
# impots.gouv.fr (procédure EFI). La liasse du régime normal (2050 à 2059) et la
# déclaration de valeur ajoutée 1330-CVAE ne s'y déposent pas : l'EDI est obligatoire.
EFI_GUIDES = {
    'TVA_CA3': "Espace professionnel → Déclarer → TVA : formulaire 3310-CA3 de la période, puis paiement en ligne.",
    'TVA_CA12': "Espace professionnel → Déclarer → TVA : déclaration annuelle 3517-S CA12, puis paiement du solde.",
    'TVA_3514': "Espace professionnel → Déclarer → TVA : avis d'acompte 3514 et paiement en ligne.",
    'IS_2571': "Espace professionnel → Déclarer → Impôt sur les sociétés : relevé d'acompte 2571 et paiement en ligne.",
    'IS_2572': "Espace professionnel → Déclarer → Impôt sur les sociétés : relevé de solde 2572 et paiement en ligne.",
    'LIASSE': "Espace professionnel → Déclarer → Résultat : déclaration 2065 et annexes 2033-A à 2033-G.",
    'DAS2': "Espace professionnel → Déclarer → Déclaration d'honoraires (DAS2) : un bénéficiaire par ligne.",
}
IMPOTS_GOUV_URL = 'https://www.impots.gouv.fr/accueil'
RECEIPT_EXTENSIONS = {'.pdf': b'%PDF-', '.png': b'\x89PNG', '.jpg': b'\xff\xd8\xff', '.jpeg': b'\xff\xd8\xff'}
MAX_RECEIPT_BYTES = 10 * 1024 * 1024


def filing_channel(decl_type, regime='normal'):
    """'efi' : déposable par l'entreprise sur impots.gouv.fr ; 'edi' : EDI obligatoire
    (expert-comptable ou partenaire EDI)."""
    if decl_type == 'CVAE_1330' or (decl_type == 'LIASSE' and regime != 'simplifie'):
        return 'edi'
    return 'efi'


def efi_guide(decl_type, regime='normal'):
    if filing_channel(decl_type, regime) == 'edi':
        return ("Dépôt EDI obligatoire : cette déclaration ne peut pas être saisie sur impots.gouv.fr. "
                "Transmettez le récapitulatif à votre expert-comptable ou à votre partenaire EDI, puis archivez ici "
                "l'accusé de réception qu'il vous remet.")
    return EFI_GUIDES.get(decl_type, "Espace professionnel → Déclarer.")


def prepared_declaration(conn, entity_id, decl_type, period_start, period_end, due_date=None):
    ef, ep = _ef(entity_id)
    df, dp = ('', ())
    if decl_type in INSTALMENT_TYPES and due_date:
        df, dp = ' AND due_date=?', (str(due_date)[:10],)
    return conn.execute(
        f"""SELECT * FROM teledeclarations WHERE {ef} AND decl_type=? AND period_start=? AND period_end=?{df}
            AND status='prepared' ORDER BY id DESC LIMIT 1""",
        (*ep, decl_type, str(period_start)[:10], str(period_end)[:10], *dp)).fetchone()


def prepare_manual(conn, built, entity_id=None, user=None, regime='normal', corrective=False):
    """Fige le contenu contrôlé d'une déclaration que l'entreprise va déposer elle-même.
    Une déclaration déjà préparée pour la même période est mise à jour ; une déclaration
    déjà déposée bloque, sauf rectificative."""
    if built['errors']:
        raise TeledeclarationError('Préparation impossible : ' + ' '.join(built['errors']))
    p_type = built['payload']['type']
    p = dict(built['payload'], generated_at=_now(), filing=filing_channel(p_type, regime), regime=regime)
    existing = active_declaration(conn, entity_id, p_type, p['period']['start'], p['period']['end'], p.get('due_date'))
    if existing and not corrective:
        raise TeledeclarationError("Une déclaration est déjà déposée pour cette période : cochez « déclaration "
                                   "rectificative » pour en préparer une nouvelle.")
    p['corrective'] = bool(corrective and existing)
    digest = payload_hash(p)
    now = _now()
    prev = prepared_declaration(conn, entity_id, p_type, p['period']['start'], p['period']['end'], p.get('due_date'))
    if prev:
        history = json.loads(prev['history_json'] or '[]')
        history.append({'at': now, 'status': 'prepared', 'message': 'Récapitulatif mis à jour'})
        conn.execute("""UPDATE teledeclarations SET payload_json=?,payload_hash=?,payment_amount=?,corrective=?,replaces_id=?,
                        submitted_by=?,submitted_at=?,updated_at=?,history_json=? WHERE id=?""",
                     (json.dumps(p, ensure_ascii=False), digest, (p.get('payment') or {}).get('amount'), 1 if p['corrective'] else 0,
                      existing['id'] if (existing and p['corrective']) else None, user, now, now,
                      json.dumps(history, ensure_ascii=False), prev['id']))
        conn.commit()
        return prev['id']
    conn.execute(
        """INSERT INTO teledeclarations(entity_id,decl_type,form,channel,period_start,period_end,payload_json,payload_hash,
               status,mode,provider,remote_id,corrective,replaces_id,payment_amount,submitted_by,submitted_at,updated_at,history_json,due_date)
           VALUES(?,?,?,?,?,?,?,?,'prepared','manual',?,NULL,?,?,?,?,?,?,?,?)""",
        (entity_id, p_type, p['form'], 'EFI (impots.gouv.fr)' if p['filing'] == 'efi' else p['channel'],
         p['period']['start'], p['period']['end'], json.dumps(p, ensure_ascii=False), digest,
         'impots.gouv.fr' if p['filing'] == 'efi' else 'expert-comptable / EDI',
         1 if p['corrective'] else 0, existing['id'] if (existing and p['corrective']) else None,
         (p.get('payment') or {}).get('amount'), user, now, now,
         json.dumps([{'at': now, 'status': 'prepared', 'message': 'Récapitulatif préparé'}], ensure_ascii=False),
         p.get('due_date')))
    conn.commit()
    return conn.execute('SELECT last_insert_rowid()').fetchone()[0]


def check_receipt(filename, data):
    """Contrôle du justificatif de dépôt : PDF ou image, 10 Mo au plus, contenu conforme."""
    import os as _os
    ext = _os.path.splitext(filename or '')[1].lower()
    if ext not in RECEIPT_EXTENSIONS:
        raise TeledeclarationError("Justificatif refusé : déposez un PDF ou une image (PNG, JPEG).")
    if not data:
        raise TeledeclarationError("Justificatif vide.")
    if len(data) > MAX_RECEIPT_BYTES:
        raise TeledeclarationError("Justificatif trop volumineux (10 Mo au plus).")
    if not data.startswith(RECEIPT_EXTENSIONS[ext]):
        raise TeledeclarationError("Le contenu du justificatif ne correspond pas à son format.")
    return ext


def filing_status(check):
    """Statut d'un dépôt fait par l'entreprise selon le contrôle de l'accusé joint :
    concordant → confirmed ; écarts → to_verify ; illisible ou absent → declared."""
    return {'concordant': 'confirmed', 'ecarts': 'to_verify'}.get((check or {}).get('status'), 'declared')


def confirm_filing(conn, row, user, note=''):
    """Confirmation manuelle d'un dépôt après vérification de l'accusé par un utilisateur
    habilité (accusé illisible automatiquement, ou écarts expliqués). Une justification est
    exigée quand des écarts ont été détectés. La confirmation est tracée."""
    if row['mode'] != 'manual' or row['status'] not in ('declared', 'to_verify', 'filed'):
        raise TeledeclarationError("Ce dépôt n'est pas en attente de vérification.")
    if not row['receipt_stored']:
        raise TeledeclarationError("Aucun justificatif archivé : joignez d'abord l'accusé de réception.")
    note = (note or '').strip()[:300]
    if row['status'] == 'to_verify' and len(note) < 10:
        raise TeledeclarationError("Expliquez pourquoi les écarts détectés sont sans conséquence (10 caractères au moins).")
    now = _now()
    check = json.loads(row['receipt_check'] or '{}') if row['receipt_check'] else {}
    check['manual_confirmation'] = {'by': user, 'at': now, 'note': note, 'previous_status': row['status']}
    history = json.loads(row['history_json'] or '[]')
    history.append({'at': now, 'status': 'confirmed',
                    'message': f"Accusé vérifié et dépôt confirmé manuellement par {user or 'un utilisateur'}"
                               + (f" : {note}" if note else '')})
    conn.execute("UPDATE teledeclarations SET status='confirmed',receipt_check=?,history_json=?,updated_at=? WHERE id=?",
                 (json.dumps(check, ensure_ascii=False), json.dumps(history, ensure_ascii=False), now, row['id']))
    conn.commit()


def record_filing(conn, row, stored_name, original_name, filed_on, reference=None, user=None, check=None):
    """Enregistre le dépôt fait par l'entreprise : le statut « déposée » n'est attribué
    qu'avec un justificatif officiel archivé."""
    if row['mode'] != 'manual' or row['status'] not in ('prepared',) + MANUAL_FILED_STATUSES:
        raise TeledeclarationError("Cette déclaration n'est pas à déposer par vos soins.")
    filed_on = date.fromisoformat(str(filed_on)[:10])
    from profitos.accounting import paris_today
    if filed_on > paris_today():
        raise TeledeclarationError("La date de dépôt ne peut pas être dans le futur.")
    if filed_on < date.fromisoformat(str(row['period_end'])[:10]):
        raise TeledeclarationError("La date de dépôt ne peut pas précéder la fin de la période déclarée.")
    other = active_declaration(conn, row['entity_id'], row['decl_type'], row['period_start'], row['period_end'], row['due_date'])
    if other and other['id'] != row['id'] and not row['corrective']:
        raise TeledeclarationError("Une autre déclaration est déjà enregistrée comme déposée pour cette période.")
    now = _now()
    history = json.loads(row['history_json'] or '[]')
    verdict = {'concordant': 'justificatif concordant', 'ecarts': 'écarts à vérifier',
               'non_verifiable': 'vérification automatique impossible'}.get((check or {}).get('status'), '')
    status = filing_status(check)
    history.append({'at': now, 'status': status,
                    'message': f"Déposée sur impots.gouv.fr le {filed_on.strftime('%d/%m/%Y')} — justificatif archivé"
                               + (f" (réf. {reference})" if reference else '') + (f" — {verdict}" if verdict else '')})
    conn.execute("""UPDATE teledeclarations SET status=?,receipt_stored=?,receipt_name=?,filed_at=?,filed_by=?,
                    ack_number=?,ack_at=?,updated_at=?,history_json=?,receipt_check=? WHERE id=?""",
                 (status, stored_name, original_name, filed_on.isoformat(), user, (reference or '')[:80] or None,
                  filed_on.isoformat(), now, json.dumps(history, ensure_ascii=False),
                  json.dumps(check, ensure_ascii=False) if check else None, row['id']))
    conn.commit()


# ------------------------------------------------------- contrôle automatique de l'accusé
FORM_KEYWORDS = {
    'TVA_CA3': ('3310', 'CA3'), 'TVA_CA12': ('3517', 'CA12'), 'TVA_3514': ('3514',),
    'IS_2571': ('2571',), 'IS_2572': ('2572',), 'DAS2': ('DAS2', 'DAS 2', 'HONORAIRES'),
    'CVAE_1330': ('1330',), 'LIASSE': ('2065', '2033', '2050'),
}
MONTHS_FR = ('janvier', 'fevrier', 'mars', 'avril', 'mai', 'juin', 'juillet', 'aout', 'septembre', 'octobre',
             'novembre', 'decembre')
MAX_RECEIPT_PAGES = 10


def _receipt_text(data, ext):
    """Texte d'un accusé PDF (pages limitées) ; None si illisible ou image."""
    if ext != '.pdf':
        return None
    try:
        import io
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            return None
        return '\n'.join((page.extract_text() or '') for page in reader.pages[:MAX_RECEIPT_PAGES])
    except Exception:
        return None


def _norm(text):
    import unicodedata
    t = unicodedata.normalize('NFKD', text or '')
    t = ''.join(ch for ch in t if not unicodedata.combining(ch)).lower()
    return t.replace(' ', ' ').replace(' ', ' ')


def analyze_receipt(data, ext, row, payload, text=None):
    """Compare l'accusé joint à la déclaration préparée : SIREN, formulaire, période et
    montant. Ne prouve pas l'acceptation par l'administration ; repère un mauvais fichier
    ou une erreur de saisie. Statuts : concordant, ecarts, non_verifiable."""
    text = text if text is not None else _receipt_text(data, ext)
    if not text or len(text.strip()) < 20:
        reason = ("image : lecture automatique impossible" if ext != '.pdf'
                  else "PDF sans texte lisible (document scanné ou protégé)")
        return {'status': 'non_verifiable', 'checks': [], 'reason': reason}
    low = _norm(text)
    digits_only = re.sub(r'[ .\-]', '', low)
    checks = []
    siren = ((payload.get('declarant') or {}).get('siren') or '')
    if siren:
        checks.append({'label': f'SIREN {siren}', 'ok': siren in digits_only})
    keys = FORM_KEYWORDS.get(row['decl_type'], ())
    if keys:
        compact = low.replace(' ', '').upper()
        checks.append({'label': f"Formulaire {row['form']}",
                       'ok': any(k.replace(' ', '').upper() in compact for k in keys)})
    start = date.fromisoformat(str(row['period_start'])[:10])
    end = date.fromisoformat(str(row['period_end'])[:10])
    if payload.get('due_date'):
        due = date.fromisoformat(payload['due_date'])
        candidates = {due.strftime('%d/%m/%Y'), due.isoformat(), f"{MONTHS_FR[due.month - 1]} {due.year}",
                      due.strftime('%m/%Y'), end.strftime('%d/%m/%Y')}
        label = f"Échéance du {due.strftime('%d/%m/%Y')}"
    elif (end - start).days <= 92:
        candidates = {end.strftime('%d/%m/%Y'), start.strftime('%d/%m/%Y'), end.isoformat(),
                      f"{MONTHS_FR[end.month - 1]} {end.year}", end.strftime('%m/%Y'), end.strftime('%m-%Y')}
        label = f"Période {start.strftime('%m/%Y') if start.month == end.month else start.strftime('%m') + ' à ' + end.strftime('%m/%Y')}"
    else:
        candidates = {end.strftime('%d/%m/%Y'), end.isoformat(), start.strftime('%d/%m/%Y'), end.strftime('%d%m%Y')}
        label = f"Exercice clos le {end.strftime('%d/%m/%Y')}"
    checks.append({'label': label, 'ok': any(c in low for c in candidates)})
    amount = (payload.get('payment') or {}).get('amount')
    if amount:
        n = int(round(float(amount)))
        variants = {str(n), f"{n:,}".replace(',', ' '), f"{n:,}".replace(',', '.')}
        found = any(re.search(r'(?<![\d])' + re.escape(v) + r'(?:[,.]\d{2})?(?![\d])', low) for v in variants)
        checks.append({'label': f"Montant de {f'{n:,}'.replace(',', ' ')} €", 'ok': found})
    status = 'concordant' if all(c['ok'] for c in checks) else 'ecarts'
    return {'status': status, 'checks': checks, 'reason': None}


# --------------------------------------------------------- ouverture des déclarations aux clients
def declarations_open(org_id=None):
    """Préparation et suivi des dépôts ouverts à cette organisation ?

    En production, fermé par défaut tant que les calculs fiscaux n'ont pas été validés sur
    des dossiers de référence (docs/validation-fiscale.md) : FISCAL_DECLARATIONS_ENABLED=1
    ouvre à tous, FISCAL_DECLARATIONS_PILOT_ORGS (identifiants séparés par des virgules)
    ouvre à des organisations pilotes. Toujours ouvert en développement et en test."""
    if os.environ.get('PROFITOS_ENV', 'production') != 'production':
        return True
    if os.environ.get('FISCAL_DECLARATIONS_ENABLED') == '1':
        return True
    pilots = {x.strip() for x in (os.environ.get('FISCAL_DECLARATIONS_PILOT_ORGS') or '').split(',') if x.strip()}
    return org_id is not None and str(org_id) in pilots
