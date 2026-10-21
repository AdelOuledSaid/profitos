"""Contrôle d'un fichier des écritures comptables (FEC) selon l'article A47 A-1 du LPF :
18 colonnes dans l'ordre, séparateur tabulation ou barre verticale, dates AAAAMMJJ,
montants à virgule décimale sans séparateur de milliers, champs obligatoires renseignés,
écritures équilibrées, numérotation continue par journal. Utilisé par les tests ; ce
contrôle ne remplace pas l'outil Test Compta Demat de la DGFiP."""
import re
from collections import defaultdict
from decimal import Decimal

COLUMNS = ['JournalCode', 'JournalLib', 'EcritureNum', 'EcritureDate', 'CompteNum', 'CompteLib', 'CompAuxNum',
           'CompAuxLib', 'PieceRef', 'PieceDate', 'EcritureLib', 'Debit', 'Credit', 'EcritureLet', 'DateLet',
           'ValidDate', 'Montantdevise', 'Idevise']
REQUIRED = ['JournalCode', 'JournalLib', 'EcritureNum', 'EcritureDate', 'CompteNum', 'CompteLib', 'PieceRef',
            'PieceDate', 'EcritureLib', 'Debit', 'Credit', 'ValidDate']
DATE = re.compile(r'^\d{8}$')
AMOUNT = re.compile(r'^-?\d+(,\d{1,2})?$')


def _date_ok(v):
    if not DATE.match(v):
        return False
    y, m, d = int(v[:4]), int(v[4:6]), int(v[6:])
    from datetime import date
    try:
        date(y, m, d)
        return True
    except ValueError:
        return False


def check_fec(data):
    errors = []
    try:
        text = data.decode('utf-8')
        encoding = 'utf-8'
    except UnicodeDecodeError:
        text = data.decode('iso-8859-15')
        encoding = 'iso-8859-15'
    lines = [l for l in text.replace('\r\n', '\n').split('\n') if l != '']
    if not lines:
        return {'errors': ['fichier vide'], 'entries': 0, 'total_debit': 0, 'total_credit': 0, 'encoding': encoding}
    sep = '\t' if '\t' in lines[0] else '|'
    header = lines[0].split(sep)
    if header[:18] != COLUMNS:
        errors.append(f'en-tête non conforme : {header[:18]}')
    balance = defaultdict(Decimal)
    numbers = defaultdict(set)
    total_d = total_c = Decimal(0)
    for n, line in enumerate(lines[1:], start=2):
        cells = line.split(sep)
        if len(cells) != len(header):
            errors.append(f'ligne {n} : {len(cells)} colonnes au lieu de {len(header)}')
            continue
        row = dict(zip(header, cells))
        for f in REQUIRED:
            if not row.get(f, '').strip():
                errors.append(f'ligne {n} : {f} vide')
        for f in ('EcritureDate', 'PieceDate', 'ValidDate'):
            if row.get(f) and not _date_ok(row[f]):
                errors.append(f'ligne {n} : {f} « {row[f]} » invalide')
        if row.get('DateLet') and not _date_ok(row['DateLet']):
            errors.append(f'ligne {n} : DateLet invalide')
        if row.get('EcritureLet') and not row.get('DateLet'):
            errors.append(f'ligne {n} : lettrage sans date de lettrage')
        for f in ('Debit', 'Credit'):
            if not AMOUNT.match(row.get(f, '')):
                errors.append(f'ligne {n} : montant {f} « {row.get(f)} » invalide')
        if not re.match(r'^\d{3}', row.get('CompteNum', '')):
            errors.append(f'ligne {n} : CompteNum « {row.get("CompteNum")} » hors plan comptable')
        try:
            d = Decimal(row['Debit'].replace(',', '.'))
            c = Decimal(row['Credit'].replace(',', '.'))
        except Exception:
            continue
        if d and c:
            errors.append(f'ligne {n} : débit et crédit renseignés')
        key = (row['JournalCode'], row['EcritureNum'])
        balance[key] += d - c
        numbers[row['JournalCode']].add(row['EcritureNum'])
        total_d += d
        total_c += c
    for key, v in balance.items():
        if v != 0:
            errors.append(f'écriture {key[1]} ({key[0]}) déséquilibrée de {v}')
    for journal, nums in numbers.items():
        seq = sorted(int(re.sub(r'\D', '', x)[-5:] or 0) for x in nums)
        if seq and seq != list(range(seq[0], seq[0] + len(seq))):
            errors.append(f'journal {journal} : numérotation non continue {seq}')
    return {'errors': errors[:50], 'entries': len(balance), 'total_debit': float(total_d), 'total_credit': float(total_c),
            'encoding': encoding, 'separator': sep}
