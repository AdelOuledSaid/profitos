"""Export SEPA (virements groupés) — génère un fichier XML au format ISO
20022 pain.001.001.03 (SEPA Credit Transfer), à uploader manuellement dans
l'espace banque en ligne de l'entreprise pour exécuter un lot de paiements
fournisseurs en une fois.

Ce module ne déclenche AUCUN virement réel — il produit uniquement le
fichier. L'exécution reste entièrement entre les mains de la banque de
l'entreprise, via son propre portail. Une vraie exécution automatisée
nécessiterait un partenaire bancaire agréé (Treezor, Swan, Bridge...) ou un
accès direct à l'API de la banque — hors de portée ici, comme pour le
Compte Pro.

Format vérifié structurellement (balises, imbrication, montants) mais pas
contre une vraie plateforme bancaire — teste le premier fichier généré avec
un montant symbolique avant un usage en production, ou fais-le valider par
ta banque.
"""
import re
import uuid
import xml.sax.saxutils as saxutils
from datetime import date, datetime


def validate_iban(iban):
    """Valide un IBAN par l'algorithme de contrôle mod-97 (ISO 7064) —
    purement mathématique, fiable sans dépendance externe. Vérifie le
    format ET la somme de contrôle, pas l'existence réelle du compte."""
    if not iban:
        return False
    cleaned = iban.replace(' ', '').upper()
    if not re.match(r'^[A-Z]{2}\d{2}[A-Z0-9]{10,30}$', cleaned):
        return False
    rearranged = cleaned[4:] + cleaned[:4]
    numeric = ''.join(str(int(ch, 36)) for ch in rearranged)
    return int(numeric) % 97 == 1


def format_iban(iban):
    """Formate un IBAN en groupes de 4 caractères pour l'affichage (ex:
    FR76 1234 5678 ...). N'affecte jamais la valeur stockée, uniquement
    l'affichage."""
    cleaned = (iban or '').replace(' ', '').upper()
    return ' '.join(cleaned[i:i + 4] for i in range(0, len(cleaned), 4))


def _xml_escape(text):
    return saxutils.escape(str(text or ''))


def generate_sepa_xml(company, payments):
    """Génère le contenu XML pain.001.001.03 pour un lot de virements.

    company : dict-like avec 'name', 'iban', 'bic'.
    payments : liste de dicts {supplier_name, iban, bic, amount, reference}.

    Lève ValueError (rien généré) si l'IBAN/BIC de l'entreprise est absent
    ou invalide, si la liste est vide, ou si un des virements a un IBAN
    invalide — jamais un fichier partiellement correct envoyé à une banque."""
    if not payments:
        raise ValueError("Aucun paiement à inclure dans le lot.")
    company_iban = (company.get('iban') or '').replace(' ', '').upper()
    company_bic = (company.get('bic') or '').replace(' ', '').upper()
    if not validate_iban(company_iban):
        raise ValueError("L'IBAN de l'entreprise est manquant ou invalide — renseigne-le dans le profil entreprise.")
    if not company_bic:
        raise ValueError("Le BIC de l'entreprise est manquant — renseigne-le dans le profil entreprise.")

    total = 0.0
    for p in payments:
        iban = (p.get('iban') or '').replace(' ', '').upper()
        if not validate_iban(iban):
            raise ValueError(f"IBAN invalide pour {p.get('supplier_name', '?')} : {p.get('iban', '(vide)')!r}.")
        if not p.get('bic'):
            raise ValueError(f"BIC manquant pour {p.get('supplier_name', '?')}.")
        amount = float(p.get('amount') or 0)
        if amount <= 0:
            raise ValueError(f"Montant invalide pour {p.get('supplier_name', '?')} : {amount}.")
        total += amount
    total = round(total, 2)

    msg_id = f"PROFITOS-{uuid.uuid4().hex[:16].upper()}"
    created = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S')
    exec_date = date.today().isoformat()
    n = len(payments)
    company_name_esc = _xml_escape(company.get('name', ''))

    transfers = []
    for i, p in enumerate(payments, start=1):
        iban = (p.get('iban') or '').replace(' ', '').upper()
        bic = (p.get('bic') or '').replace(' ', '').upper()
        amount = round(float(p.get('amount') or 0), 2)
        end_to_end = f"PMT-{msg_id[-8:]}-{i:04d}"
        transfers.append(f"""      <CdtTrfTxInf>
        <PmtId>
          <EndToEndId>{_xml_escape(end_to_end)}</EndToEndId>
        </PmtId>
        <Amt>
          <InstdAmt Ccy="EUR">{amount:.2f}</InstdAmt>
        </Amt>
        <CdtrAgt>
          <FinInstnId><BIC>{_xml_escape(bic)}</BIC></FinInstnId>
        </CdtrAgt>
        <Cdtr>
          <Nm>{_xml_escape(p.get('supplier_name', ''))}</Nm>
        </Cdtr>
        <CdtrAcct>
          <Id><IBAN>{_xml_escape(iban)}</IBAN></Id>
        </CdtrAcct>
        <RmtInf>
          <Ustrd>{_xml_escape(p.get('reference', ''))}</Ustrd>
        </RmtInf>
      </CdtTrfTxInf>""")

    xml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pain.001.001.03" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <CstmrCdtTrfInitn>
    <GrpHdr>
      <MsgId>{_xml_escape(msg_id)}</MsgId>
      <CreDtTm>{created}</CreDtTm>
      <NbOfTxs>{n}</NbOfTxs>
      <CtrlSum>{total:.2f}</CtrlSum>
      <InitgPty>
        <Nm>{company_name_esc}</Nm>
      </InitgPty>
    </GrpHdr>
    <PmtInf>
      <PmtInfId>{_xml_escape(msg_id)}-1</PmtInfId>
      <PmtMtd>TRF</PmtMtd>
      <NbOfTxs>{n}</NbOfTxs>
      <CtrlSum>{total:.2f}</CtrlSum>
      <PmtTpInf>
        <SvcLvl><Cd>SEPA</Cd></SvcLvl>
      </PmtTpInf>
      <ReqdExctnDt>{exec_date}</ReqdExctnDt>
      <Dbtr>
        <Nm>{company_name_esc}</Nm>
      </Dbtr>
      <DbtrAcct>
        <Id><IBAN>{_xml_escape(company_iban)}</IBAN></Id>
      </DbtrAcct>
      <DbtrAgt>
        <FinInstnId><BIC>{_xml_escape(company_bic)}</BIC></FinInstnId>
      </DbtrAgt>
      <ChrgBr>SLEV</ChrgBr>
{chr(10).join(transfers)}
    </PmtInf>
  </CstmrCdtTrfInitn>
</Document>"""
    return xml_content, msg_id, total
