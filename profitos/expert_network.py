"""Expert Network (v485) : un professionnel qualifié quand une intervention humaine est utile.

Chaîne : Mission Control détecte et vérifie → l'entreprise demande une mission à un expert
de l'annuaire privé → l'expert reçoit un dossier factuel (uniquement les éléments que
l'entreprise a cochés, avec son consentement explicite) → devis, acceptation, échanges,
livrable → le résultat est contrôlé sur les données (le constat d'origine doit disparaître).

Principes :
- annuaire privé, tenu par l'opérateur de ProfitOS ; un expert d'une profession réglementée
  (expertise comptable, conseil juridique) n'est proposé qu'avec son numéro d'inscription,
  vérifié manuellement par l'opérateur ;
- l'expert n'a pas de compte ni d'accès aux données : un lien personnel, secret (seule son
  empreinte est conservée) et limité dans le temps, vers un dossier figé au moment de l'envoi ;
- aucun paiement ni commission dans cette version : les honoraires sont convenus directement
  entre l'entreprise et l'expert (règles des professions réglementées : voir la documentation).
"""
import hashlib
import json
import re
import secrets
from datetime import date, datetime, timedelta, timezone

DOMAINS = {
    'comptabilite': {'label': 'Comptabilité', 'examples': 'Révision, clôture, contrôle fiscal',
                     'regulated': 'expert-comptable', 'registry': "Ordre des experts-comptables"},
    'juridique': {'label': 'Juridique', 'examples': 'Contrats, CGV, recouvrement, questions réglementaires',
                  'regulated': 'avocat', 'registry': 'Barreau'},
    'tresorerie': {'label': 'Trésorerie et financement', 'examples': 'Diagnostic, prévisions, recherche de financement',
                   'regulated': None, 'registry': None},
    'rh': {'label': 'Ressources humaines', 'examples': 'Paie, recrutement, organisation', 'regulated': None, 'registry': None},
    'marketing': {'label': 'Marketing et commercial', 'examples': 'Acquisition de clients, communication',
                  'regulated': None, 'registry': None},
    'informatique': {'label': 'Informatique', 'examples': 'Automatisation, cybersécurité', 'regulated': None, 'registry': None},
}
STATUSES = {'envoyee': 'Envoyée, en attente de devis', 'devis': 'Devis reçu', 'acceptee': 'Devis accepté, mission en cours',
            'livree': 'Livrable remis, à valider', 'terminee': 'Terminée', 'refusee': 'Devis refusé',
            'declinee': "Déclinée par l'expert", 'annulee': 'Annulée'}
OPEN_STATUSES = ('envoyee', 'devis', 'acceptee', 'livree')
SHARE_ITEMS = {'identite': "Identité de l'entreprise (dénomination, SIREN, adresse, code APE, contact)",
               'constat': 'Le constat de Mission Control et ses preuves (contrôles, montants, sources)'}
LINK_DAYS = 90
# Constats Mission Control pour lesquels un expert peut être utile -> domaine proposé.
KIND_DOMAINS = {'fiscal': 'comptabilite', 'dossier': 'comptabilite', 'banque': 'comptabilite', 'pieces': 'comptabilite',
                'impaye': 'juridique', 'contrat_ecart': 'juridique', 'contrat_preavis': 'juridique',
                'contrat_hors_periode': 'juridique', 'tresorerie': 'tresorerie', 'radar_dependance': 'marketing',
                'radar_secteur': 'marketing', 'radar_dormant': 'marketing'}


class ExpertError(ValueError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _eur(x):
    from profitos.runtime import fr_number
    return f'{fr_number(x, 2)} €'


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


# ------------------------------------------------------------------ annuaire (base partagée)
def ensure_directory(ac):
    ac.execute('''CREATE TABLE IF NOT EXISTS expert_partners(
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, firm TEXT, domain TEXT NOT NULL, specialties TEXT,
        description TEXT, email TEXT NOT NULL, phone TEXT, city TEXT, profession TEXT, registration_number TEXT,
        registration_verified_at TEXT, registration_verified_by TEXT, insurance TEXT, status TEXT NOT NULL DEFAULT 'actif',
        created_at TEXT, updated_at TEXT)''')
    ac.execute('''CREATE TABLE IF NOT EXISTS expert_mission_links(
        token_hash TEXT PRIMARY KEY, organization_id INTEGER NOT NULL, mission_id INTEGER NOT NULL, expert_id INTEGER NOT NULL,
        expires_at TEXT NOT NULL, revoked INTEGER DEFAULT 0, created_at TEXT)''')
    ac.execute('''CREATE TABLE IF NOT EXISTS expert_feedback(
        id INTEGER PRIMARY KEY AUTOINCREMENT, expert_id INTEGER NOT NULL, organization_id INTEGER, mission_id INTEGER,
        rating INTEGER, comment TEXT, created_at TEXT)''')
    ac.commit()


EXPERT_FIELDS = ('name', 'firm', 'domain', 'specialties', 'description', 'email', 'phone', 'city', 'profession',
                 'registration_number', 'insurance', 'status')


def clean_expert(data):
    out = {k: ' '.join(str(data.get(k) or '').split())[:1000 if k == 'description' else 200] for k in EXPERT_FIELDS}
    if not out['name'] or not out['email']:
        raise ExpertError('Nom et e-mail obligatoires.')
    if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', out['email']):
        raise ExpertError('E-mail invalide.')
    if out['domain'] not in DOMAINS:
        raise ExpertError('Domaine inconnu.')
    out['status'] = out['status'] if out['status'] in ('actif', 'suspendu') else 'actif'
    reg = DOMAINS[out['domain']]['regulated']
    if reg:
        out['profession'] = reg
        if not out['registration_number']:
            raise ExpertError(f"Profession réglementée : indiquez le numéro d'inscription ({DOMAINS[out['domain']]['registry']}).")
    return out


def save_expert(ac, data, admin_email, expert_id=None, verified=False):
    ensure_directory(ac)
    row = clean_expert(data)
    now = _now()
    if expert_id:
        current = ac.execute('SELECT * FROM expert_partners WHERE id=?', (expert_id,)).fetchone()
        if not current:
            raise ExpertError('Expert introuvable.')
        changed_reg = (current['registration_number'] or '') != row['registration_number']
        sets = ','.join(f'{k}=?' for k in EXPERT_FIELDS)
        ac.execute(f'UPDATE expert_partners SET {sets}, updated_at=? WHERE id=?', (*[row[k] for k in EXPERT_FIELDS], now, expert_id))
        if changed_reg and not verified:
            # Numéro modifié : la vérification précédente ne vaut plus.
            ac.execute('UPDATE expert_partners SET registration_verified_at=NULL, registration_verified_by=NULL WHERE id=?', (expert_id,))
    else:
        cur = ac.execute(f'INSERT INTO expert_partners({",".join(EXPERT_FIELDS)},created_at,updated_at) VALUES({",".join("?" * len(EXPERT_FIELDS))},?,?)',
                         (*[row[k] for k in EXPERT_FIELDS], now, now))
        expert_id = cur.lastrowid
    if verified:
        ac.execute('UPDATE expert_partners SET registration_verified_at=?, registration_verified_by=? WHERE id=?',
                   (now, admin_email, expert_id))
    ac.commit()
    return expert_id


def experts(ac, domain=None, proposable_only=True):
    ensure_directory(ac)
    rows = [dict(r) for r in ac.execute('SELECT * FROM expert_partners ORDER BY domain, name').fetchall()]
    if domain:
        rows = [r for r in rows if r['domain'] == domain]
    for r in rows:
        r['proposable'] = proposable(r)
        fb = ac.execute('SELECT COUNT(*) n, AVG(rating) avg FROM expert_feedback WHERE expert_id=? AND rating IS NOT NULL',
                        (r['id'],)).fetchone()
        r['ratings'], r['rating_avg'] = int(fb['n'] or 0), (round(float(fb['avg']), 1) if fb['avg'] is not None else None)
    return [r for r in rows if r['proposable']] if proposable_only else rows


def proposable(e):
    """Un expert n'est proposé aux entreprises que s'il est actif et, pour une profession
    réglementée, si son inscription a été vérifiée par l'opérateur."""
    if not e or e.get('status') != 'actif':
        return False
    if DOMAINS.get(e['domain'], {}).get('regulated'):
        return bool(e.get('registration_number') and e.get('registration_verified_at'))
    return True


def expert(ac, expert_id):
    ensure_directory(ac)
    r = ac.execute('SELECT * FROM expert_partners WHERE id=?', (expert_id,)).fetchone()
    return dict(r) if r else None


# ------------------------------------------------------------------ missions (base de l'entreprise)
def ensure_missions(c):
    c.execute('''CREATE TABLE IF NOT EXISTS expert_missions(
        id INTEGER PRIMARY KEY AUTOINCREMENT, entity_id INTEGER, expert_id INTEGER NOT NULL, expert_name TEXT, expert_email TEXT,
        domain TEXT, title TEXT NOT NULL, request TEXT, source_key TEXT, dossier TEXT, shared_items TEXT,
        consent_at TEXT, consent_by TEXT, status TEXT NOT NULL DEFAULT 'envoyee',
        quote_amount REAL, quote_delay_days INTEGER, quote_text TEXT, quote_at TEXT,
        accepted_at TEXT, delivered_at TEXT, closed_at TEXT, created_by TEXT, created_at TEXT, updated_at TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS expert_mission_events(
        id INTEGER PRIMARY KEY AUTOINCREMENT, mission_id INTEGER NOT NULL, at TEXT, actor TEXT, kind TEXT, text TEXT, url TEXT)''')
    c.commit()


def build_dossier(c, eid, items, finding=None, owner_email=None):
    """Dossier factuel figé, limité aux éléments cochés par l'entreprise."""
    dossier = {'prepared_at': _now()}
    if 'identite' in items:
        from profitos import once_done as OD
        info = OD.dossier(c, eid, owner_email)
        keep = ('name', 'legal_form', 'siren', 'address', 'naf', 'representant', 'qualite', 'email', 'telephone')
        dossier['identite'] = [{'label': info[k]['label'], 'value': info[k]['value']} for k in keep if k in info and info[k]['value']]
    if 'constat' in items and finding:
        dossier['constat'] = {'title': finding['title'], 'detail': finding.get('detail') or '',
                              'amount': finding.get('amount'), 'amount_label': finding.get('amount_label'),
                              'checks': [{'label': ch['label'], 'ok': ch['ok'], 'detail': ch['detail'], 'source': ch.get('source')}
                                         for ch in finding['checks']],
                              'evidence': [{'label': e['label'], 'value': e['value'], 'source': e['source'], 'as_of': e.get('as_of')}
                                           for e in finding.get('evidence') or []]}
    return dossier


def create_mission(c, ac, org_id, eid, expert_row, title, request_text, items, consent, user, finding=None, owner_email=None):
    """Crée la mission, fige le dossier et produit le lien de l'expert. Retourne (id, lien secret)."""
    if not proposable(expert_row):
        raise ExpertError("Cet expert n'est pas disponible.")
    title = ' '.join(str(title or '').split())[:160]
    request_text = (request_text or '').strip()[:4000]
    if not title or not request_text:
        raise ExpertError('Indiquez un titre et décrivez votre besoin.')
    items = [i for i in items if i in SHARE_ITEMS]
    if 'constat' in items and not finding:
        items.remove('constat')
    if not consent:
        raise ExpertError("Cochez l'autorisation de transmission : rien n'est envoyé à l'expert sans votre accord.")
    ensure_missions(c)
    now = _now()
    dossier = build_dossier(c, eid, items, finding, owner_email)
    cur = c.execute("""INSERT INTO expert_missions(entity_id,expert_id,expert_name,expert_email,domain,title,request,source_key,dossier,
                       shared_items,consent_at,consent_by,status,created_by,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'envoyee',?,?,?)""",
                    (eid, expert_row['id'], expert_row['name'], expert_row['email'], expert_row['domain'], title, request_text,
                     finding['key'] if finding else None, json.dumps(dossier, ensure_ascii=False, default=str),
                     json.dumps(items), now, user, user, now, now))
    mission_id = cur.lastrowid
    event(c, mission_id, 'client', 'partage', f"Autorisation de transmission donnée par {user} : " +
          ('; '.join(SHARE_ITEMS[i] for i in items) if items else 'aucun élément du compte, seulement la demande') + '.')
    c.commit()
    token = secrets.token_urlsafe(32)
    ensure_directory(ac)
    ac.execute("""INSERT INTO expert_mission_links(token_hash,organization_id,mission_id,expert_id,expires_at,created_at)
                  VALUES(?,?,?,?,?,?)""",
               (_hash(token), org_id, mission_id, expert_row['id'],
                (datetime.now(timezone.utc) + timedelta(days=LINK_DAYS)).isoformat(timespec='seconds'), now))
    ac.commit()
    return mission_id, token


def resolve_link(ac, token):
    """(organization_id, mission_id, expert_id) pour un lien valide, sinon None."""
    if not token or len(token) > 100:
        return None
    ensure_directory(ac)
    r = ac.execute('SELECT * FROM expert_mission_links WHERE token_hash=?', (_hash(token),)).fetchone()
    if not r or r['revoked'] or r['expires_at'] < _now():
        return None
    return r['organization_id'], r['mission_id'], r['expert_id']


def revoke_links(ac, org_id, mission_id):
    ensure_directory(ac)
    ac.execute('UPDATE expert_mission_links SET revoked=1 WHERE organization_id=? AND mission_id=?', (org_id, mission_id))
    ac.commit()


def event(c, mission_id, actor, kind, text, url=None):
    c.execute('INSERT INTO expert_mission_events(mission_id,at,actor,kind,text,url) VALUES(?,?,?,?,?,?)',
              (mission_id, _now(), actor, kind, (text or '')[:4000], url))


def mission(c, eid, mission_id):
    ensure_missions(c)
    r = c.execute('SELECT * FROM expert_missions WHERE id=? AND entity_id IS ?', (mission_id, eid)).fetchone()
    return _view(c, r)


def mission_any_entity(c, mission_id):
    ensure_missions(c)
    return _view(c, c.execute('SELECT * FROM expert_missions WHERE id=?', (mission_id,)).fetchone())


def _view(c, r):
    if not r:
        return None
    m = dict(r)
    m['dossier'] = json.loads(m.get('dossier') or '{}')
    m['shared_items'] = json.loads(m.get('shared_items') or '[]')
    m['status_label'] = STATUSES.get(m['status'], m['status'])
    m['domain_label'] = DOMAINS.get(m['domain'], {}).get('label', m['domain'])
    m['events'] = [dict(e) for e in c.execute('SELECT * FROM expert_mission_events WHERE mission_id=? ORDER BY id',
                                               (m['id'],)).fetchall()]
    return m


def missions(c, eid):
    ensure_missions(c)
    return [dict(r, status_label=STATUSES.get(r['status'], r['status']), domain_label=DOMAINS.get(r['domain'], {}).get('label', r['domain']))
            for r in c.execute('SELECT * FROM expert_missions WHERE entity_id IS ? ORDER BY id DESC', (eid,)).fetchall()]


def _transition(c, m, allowed, new_status, **fields):
    if m['status'] not in allowed:
        raise ExpertError(f"Action impossible : la mission est « {STATUSES.get(m['status'], m['status'])} ».")
    fields['status'] = new_status
    fields['updated_at'] = _now()
    sets = ','.join(f'{k}=?' for k in fields)
    c.execute(f'UPDATE expert_missions SET {sets} WHERE id=?', (*fields.values(), m['id']))


def clean_url(url):
    url = (url or '').strip()[:500]
    if url and not re.fullmatch(r'https://[^\s<>"]+', url):
        raise ExpertError('Lien invalide : une adresse https:// est attendue.')
    return url or None


# --- actions de l'expert (lien sécurisé)
def expert_quote(c, m, amount, delay_days, text):
    if amount is None or amount <= 0:
        raise ExpertError('Montant du devis invalide.')
    if not 1 <= int(delay_days or 0) <= 365:
        raise ExpertError('Délai de réalisation : entre 1 et 365 jours.')
    text = (text or '').strip()[:4000]
    if not text:
        raise ExpertError('Décrivez le contenu de la prestation.')
    _transition(c, m, ('envoyee', 'devis'), 'devis', quote_amount=round(amount, 2), quote_delay_days=int(delay_days),
                quote_text=text, quote_at=_now())
    event(c, m['id'], 'expert', 'devis', f"{_eur(amount)} HT, réalisation en {int(delay_days)} jour(s). {text}")
    c.commit()


def expert_decline(c, m, reason):
    _transition(c, m, ('envoyee', 'devis'), 'declinee', closed_at=_now())
    event(c, m['id'], 'expert', 'statut', 'Mission déclinée' + (f" : {reason.strip()[:500]}" if (reason or '').strip() else '.'))
    c.commit()


def expert_deliver(c, m, text, url):
    text = (text or '').strip()[:4000]
    if not text:
        raise ExpertError('Décrivez le livrable remis.')
    url = clean_url(url)
    _transition(c, m, ('acceptee', 'livree'), 'livree', delivered_at=_now())
    event(c, m['id'], 'expert', 'livrable', text, url)
    c.commit()


def message(c, m, actor, text, url=None):
    text = (text or '').strip()[:4000]
    if not text:
        raise ExpertError('Message vide.')
    if m['status'] not in OPEN_STATUSES:
        raise ExpertError('La mission est close : plus de message possible.')
    event(c, m['id'], actor, 'message', text, clean_url(url))
    c.execute('UPDATE expert_missions SET updated_at=? WHERE id=?', (_now(), m['id']))
    c.commit()


# --- actions de l'entreprise
def client_accept(c, m, user):
    _transition(c, m, ('devis',), 'acceptee', accepted_at=_now())
    event(c, m['id'], 'client', 'statut', f"Devis accepté par {user} ({_eur(m['quote_amount'])} HT). Les honoraires se règlent "
                                          "directement auprès de l'expert.")
    c.commit()


def client_refuse(c, m, user, reason):
    _transition(c, m, ('devis',), 'refusee', closed_at=_now())
    event(c, m['id'], 'client', 'statut', f"Devis refusé par {user}" + (f" : {reason.strip()[:500]}" if (reason or '').strip() else '.'))
    c.commit()


def client_complete(c, m, user, note):
    _transition(c, m, ('livree',), 'terminee', closed_at=_now())
    event(c, m['id'], 'client', 'statut', f"Mission validée par {user}" + (f" : {note.strip()[:500]}" if (note or '').strip() else '.'))
    c.commit()


def client_cancel(c, m, user):
    _transition(c, m, ('envoyee', 'devis', 'acceptee'), 'annulee', closed_at=_now())
    event(c, m['id'], 'client', 'statut', f"Mission annulée par {user} ; le lien de l'expert est désactivé.")
    c.commit()


def source_result(c, eid, m):
    """Résultat contrôlé du constat d'origine (Mission Control) : None si la mission n'en vient pas."""
    if not m.get('source_key'):
        return None
    from profitos import verification as V
    V.ensure_table(c)
    r = c.execute('SELECT status, resolved_at, resolution FROM verification_items WHERE entity_id IS ? AND item_key=?',
                  (eid, m['source_key'])).fetchone()
    if not r:
        return {'resolved': None, 'text': "Constat d'origine introuvable dans le suivi."}
    if r['status'] == V.RESOLVED:
        return {'resolved': True, 'text': f"Constat résolu le {date.fromisoformat(r['resolved_at'][:10]).strftime('%d/%m/%Y')} : {r['resolution']}"}
    return {'resolved': False, 'text': 'Le constat est toujours détecté dans les données : le résultat n\'est pas encore confirmé.'}
