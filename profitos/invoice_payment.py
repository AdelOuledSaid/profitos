"""Encaissement des factures (v470-v471) : moyens de paiement proposés au client sur la
facture, et relances automatiques par étapes.

Paiement : QR code de virement SEPA (norme EPC069-12, reconnu par les applications
bancaires européennes) avec l'IBAN de l'émetteur, le reste à payer et le numéro de
facture en référence ; lien de paiement en ligne de l'entreprise (carte, prélèvement…
chez son propre prestataire) affiché comme bouton « Payer en ligne ». ProfitOS ne
manipule jamais les fonds.

Relances : un premier rappel courtois, un rappel ferme, puis un dernier avis avant
mise en demeure, envoyés par la tâche quotidienne aux délais choisis par l'entreprise ;
elles s'arrêtent dès que la facture est réglée (saisie, rapprochement bancaire ou avoir).
"""
import re

from profitos import qr
from profitos.sepa import validate_iban

STORE_KEY = 'encaissement'
DEFAULTS = {'qr': True, 'link': '', 'auto_reminders': False, 'steps': [3, 15, 30], 'excluded': []}
STEP_NAMES = ['Rappel courtois', 'Rappel ferme', 'Dernier avis avant mise en demeure']
MAX_STEP_DAYS = 120
MIN_GAP_DAYS = 5          # jamais deux relances à moins de 5 jours d'intervalle


class PaymentSettingsError(ValueError):
    pass


def load(conn, entity_id=None):
    from profitos.tiers_declarations import load_store
    data = dict(DEFAULTS)
    data.update(load_store(conn, entity_id, STORE_KEY) or {})
    steps = [int(x) for x in data.get('steps') or [] if str(x).isdigit()][:3]
    data['steps'] = steps if len(steps) == 3 else list(DEFAULTS['steps'])
    data['excluded'] = [int(x) for x in data.get('excluded') or [] if str(x).isdigit()]
    return data


def clean_link(value):
    value = (value or '').strip()
    if not value:
        return ''
    if len(value) > 300 or not re.fullmatch(r'https://[^\s<>"\']+', value):
        raise PaymentSettingsError("Le lien de paiement doit être une adresse https:// (celle fournie par votre prestataire de paiement).")
    return value


def clean_steps(values):
    try:
        steps = [int(str(v).strip()) for v in values]
    except ValueError:
        raise PaymentSettingsError('Délais de relance invalides.')
    if len(steps) != 3 or not all(1 <= s <= MAX_STEP_DAYS for s in steps) or not steps[0] < steps[1] < steps[2]:
        raise PaymentSettingsError(f'Les trois délais doivent être croissants, entre 1 et {MAX_STEP_DAYS} jours après l\'échéance.')
    return steps


def save(conn, entity_id, data, now_iso, user=None):
    from profitos.tiers_declarations import save_store
    clean = {'qr': bool(data.get('qr')), 'link': clean_link(data.get('link')),
             'auto_reminders': bool(data.get('auto_reminders')), 'steps': clean_steps(data.get('steps') or DEFAULTS['steps']),
             'excluded': sorted({int(x) for x in data.get('excluded') or [] if str(x).isdigit()})[-500:]}
    save_store(conn, entity_id, STORE_KEY, clean, now_iso, user)
    return clean


def options(invoice, seller, balance, settings):
    """Moyens de paiement à afficher pour une facture émise non soldée (sinon None)."""
    status = invoice['status'] if hasattr(invoice, 'keys') else invoice.get('status')
    if status not in ('sent', 'partially_paid') or balance is None or balance <= 0.005:
        return None
    number = invoice['invoice_number']
    iban = ''.join(str((seller or {}).get('iban') or '').split()).upper()
    bic = ''.join(str((seller or {}).get('bic') or '').split()).upper()
    out = {'balance': round(balance, 2), 'reference': f'Facture {number}', 'iban': None, 'bic': bic or None,
           'qr_svg': None, 'qr_matrix': None, 'link': settings.get('link') or None}
    if iban and validate_iban(iban):
        out['iban'] = ' '.join(iban[i:i + 4] for i in range(0, len(iban), 4))
        if settings.get('qr', True):
            payload = qr.epc_payload((seller or {}).get('name') or '', iban, balance, out['reference'], bic)
            out['qr_matrix'] = qr.matrix(payload)
            out['qr_svg'] = qr.svg(payload, module=None, label='QR code de virement SEPA')
            out['epc'] = payload
    if not out['iban'] and not out['link']:
        return None
    return out


def reminder_step(days_late, sent_count, steps):
    """Étape de relance due aujourd'hui (0, 1 ou 2), ou None. Une étape n'est envoyée
    qu'une fois ; une relance manuelle compte comme une étape."""
    if sent_count >= len(steps):
        return None
    if days_late >= steps[sent_count]:
        return sent_count
    return None


def reminder_text(step, number, total, balance, due_date_label):
    base = (f"la facture {number} (montant TTC {total} €), échue le {due_date_label}, présente un reste à payer "
            f"de {balance} €.")
    if step == 0:
        return (f"Bonjour, sauf erreur de notre part, {base} Il s'agit peut-être d'un simple oubli : vous pouvez la "
                "régler en quelques secondes depuis le lien ci-dessous. Si votre règlement est déjà parti, merci de ne pas "
                "tenir compte de ce message.")
    if step == 1:
        return (f"Bonjour, malgré notre précédent rappel, {base} Nous vous remercions de procéder à son règlement sous "
                "huit jours. Conformément à nos conditions de paiement, tout retard entraîne l'application de pénalités de "
                "retard et d'une indemnité forfaitaire pour frais de recouvrement de 40 € (art. L441-10 du Code de commerce).")
    return (f"Bonjour, malgré nos relances, {base} Sans règlement sous huit jours, nous serons contraints de vous adresser "
            "une mise en demeure, puis d'engager une procédure de recouvrement. Les pénalités de retard et l'indemnité "
            "forfaitaire de 40 € (art. L441-10 du Code de commerce) restent dues. Si un litige justifie ce retard, "
            "répondez à ce message pour nous en informer.")
