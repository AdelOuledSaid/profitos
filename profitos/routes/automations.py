"""Écrans des automatisations comptables : écritures récurrentes, échéancier fiscal et
contrôles avant liasse."""
from datetime import date, timedelta

from profitos.runtime import *
from profitos.accounting import paris_today
from profitos import automation as A

AUTOMATIC_JOBS = [
    ('recurring_entries', 'Écritures récurrentes', 'Chaque jour à 5 h'),
    ('fiscal_deadlines', 'Rappels des échéances fiscales', 'Chaque jour à 8 h : 7 jours et la veille, puis relance hebdomadaire sans justificatif'),
    ('teledeclaration_status', 'Suivi des télédéclarations', 'Toutes les heures'),
    ('recurring_invoices', 'Factures récurrentes (brouillons)', 'Chaque jour à 6 h'),
    ('bank_sync', 'Synchronisation bancaire', 'Chaque jour à 5 h'),
    ('einvoice_inbound_sync', 'Réception des factures électroniques', 'Toutes les heures'),
    ('weekly_accountant_export', "Export hebdomadaire à l'expert-comptable", 'Le lundi à 8 h'),
]


def _eid():
    from profitos.entities import current_entity_id
    return current_entity_id()


def _form_lines(form):
    lines = []
    for i in range(1, 7):
        code = (form.get(f'account_{i}') or '').strip()
        debit = (form.get(f'debit_{i}') or '').strip()
        credit = (form.get(f'credit_{i}') or '').strip()
        if not (code or debit or credit):
            continue
        lines.append({'account_code': code, 'label': (form.get(f'label_{i}') or '').strip(),
                      'debit': form_float(debit) if debit else 0.0, 'credit': form_float(credit) if credit else 0.0})
    return lines


def register(app):
    @app.route('/comptabilite/ecritures-recurrentes', methods=['GET', 'POST'])
    @login_required
    def recurring_entries():
        eid = _eid()
        c = cx()
        if request.method == 'POST':
            action = request.form.get('action')
            try:
                if action == 'create':
                    A.create_recurring_entry(c, eid, request.form.get('label'), request.form.get('journal_code') or 'OD',
                                             _form_lines(request.form), request.form.get('frequency') or 'monthly',
                                             request.form.get('start_date') or paris_today().isoformat(),
                                             request.form.get('end_date') or None, current_user()['email'], now())
                    log_activity('RECURRING_ENTRY_CREATED', f"Écriture récurrente créée : {request.form.get('label', '')[:80]}")
                    flash("Écriture récurrente enregistrée : elle sera passée automatiquement à chaque échéance.")
                elif action == 'deactivate':
                    A.deactivate_recurring_entry(c, eid, int(request.form.get('template_id') or 0))
                    flash('Écriture récurrente arrêtée : les écritures déjà passées sont conservées.')
                elif action == 'run':
                    created, errors = A.generate_due_entries(c, eid, paris_today(), created_by=current_user()['email'])
                    flash(f"{len(created)} écriture(s) passée(s)." if created else 'Aucune écriture récurrente échue.')
                    for e in errors[:3]:
                        flash(f"Non passée : {e}")
            except (A.AutomationError, ValueError) as exc:
                flash(f"Enregistrement impossible : {exc}")
            c.close()
            return redirect(url_for('recurring_entries'))
        rows = A.list_recurring_entries(c, eid)
        journals = c.execute('SELECT code,label FROM accounting_journals ORDER BY code').fetchall()
        c.close()
        return render_template('recurring_entries.html', rows=rows, journals=journals, frequencies=A.FREQUENCIES,
                               today=paris_today().isoformat())

    @app.route('/comptabilite/echeances')
    @login_required
    def fiscal_calendar_page():
        from profitos import ledger as L
        eid = _eid()
        c = cx()
        today = paris_today()
        deadlines = A.upcoming_deadlines(c, eid, today, horizon_days=90)
        overdue = A.overdue_declarations(c, eid, today)
        to_verify = A.receipts_to_verify(c, eid)
        current = L.fiscal_year_for(c, today, eid)
        closed = L.fiscal_year_for(c, current['start'] - timedelta(days=1), eid)
        controls = A.precontrols(c, closed['start'], closed['end'], eid, today=today)
        c.close()
        ac = auth_cx()
        runs = {}
        try:
            for name, _label, _when in AUTOMATIC_JOBS:
                runs[name] = ac.execute(
                    'SELECT status,finished_at,detail FROM job_runs WHERE job_name=? AND organization_id=? '
                    'ORDER BY id DESC LIMIT 1', (name, session['org_id'])).fetchone()
        except Exception:
            runs = {}
        finally:
            ac.close()
        return render_template('fiscal_calendar.html', deadlines=deadlines, overdue=overdue, to_verify=to_verify, controls=controls, closed=closed,
                               jobs=AUTOMATIC_JOBS, runs=runs, today=today)


    def _fiscal_review():
        from profitos import fiscal_controls as FC
        eid = _eid()
        c = cx()
        try:
            return FC.review(c, eid, paris_today())
        finally:
            c.close()

    @app.route('/comptabilite/controles-fiscaux')
    @login_required
    def fiscal_controls():
        return render_template('fiscal_controls.html', r=_fiscal_review())

    @app.route('/comptabilite/controles-fiscaux/dossier')
    @login_required
    def fiscal_controls_dossier():
        from flask import make_response
        from profitos.entities import resolve_entity
        r = _fiscal_review()
        c = cx()
        try:
            try:
                identity = dict(resolve_entity(c, _eid()))
            except Exception:
                identity = {}
        finally:
            c.close()
        if not identity.get('name'):
            ac = auth_cx()
            try:
                row = ac.execute('SELECT name FROM organizations WHERE id=?', (session.get('org_id'),)).fetchone()
                identity['name'] = row['name'] if row else ''
            finally:
                ac.close()
        resp = make_response(render_template('fiscal_controls_dossier.html', r=r, identity=identity))
        resp.headers['Cache-Control'] = 'no-store'
        return resp


MANDATE_ROLES = ('OWNER', 'ADMIN')


def _rows_from_form(form, prefix, fields, n):
    out = []
    for i in range(1, n + 1):
        row = {f: (form.get(f'{prefix}_{f}_{i}') or '').strip() for f in fields}
        if not row.get('name'):
            continue
        out.append(row)
    return out


def register_fiscal(app):
    from profitos import corporate_tax as CT
    from profitos.teledeclaration import luhn_ok

    @app.route('/comptabilite/profil-fiscal', methods=['GET', 'POST'])
    @login_required
    def fiscal_profile():
        eid = _eid()
        c = cx()
        profile = CT.get_fiscal_profile(c, eid)
        if request.method == 'POST' and not fiscal_write_allowed():
            c.close()
            log_security_event('FISCAL_WRITE_DENIED', 'BLOCKED', target='fiscal_profile')
            flash(FISCAL_WRITE_DENIED)
            return redirect(url_for('fiscal_profile'))
        if request.method == 'POST':
            f = request.form
            errors = []
            sh_fields = ('kind', 'name', 'first_name', 'siren', 'pct', 'shares', 'address', 'postal_code', 'city', 'country', 'birth_date')
            sub_fields = ('legal_form', 'name', 'siren', 'pct', 'address', 'postal_code', 'city', 'country')
            shareholders = _rows_from_form(f, 'sh', sh_fields, 8)
            subsidiaries = _rows_from_form(f, 'sub', sub_fields, 6)
            for r in shareholders + subsidiaries:
                try:
                    pct = form_float(r.get('pct') or 0)
                except ValueError:
                    pct = -1
                if not 0 <= pct <= 100:
                    errors.append(f"{r['name']} : pourcentage de détention invalide.")
                r['pct'] = pct
                if 'shares' in r:
                    try:
                        r['shares'] = form_float(r.get('shares') or 0)
                        if r['shares'] < 0:
                            raise ValueError
                    except ValueError:
                        errors.append(f"{r['name']} : nombre de parts invalide.")
                siren = ''.join(ch for ch in r.get('siren', '') if ch.isdigit())
                if siren and (len(siren) != 9 or not luhn_ok(siren)):
                    errors.append(f"{r['name']} : SIREN invalide.")
                r['siren'] = siren
            premises = []
            for i in range(1, 6):
                addr = (f.get(f'loc_address_{i}') or '').strip()[:200]
                inv = ''.join(ch for ch in (f.get(f'loc_invariant_{i}') or '') if ch.isalnum()).upper()[:20]
                if not (addr or inv):
                    continue
                try:
                    rent = form_float(f.get(f'loc_rent_{i}') or 0)
                except ValueError:
                    rent = -1
                if rent < 0:
                    errors.append(f"Local {addr or inv} : loyer annuel invalide.")
                premises.append({'invariant': inv, 'address': addr, 'rent': max(rent, 0.0)})
            if (f.get('date_creation') or '').strip():
                try:
                    date.fromisoformat(f.get('date_creation').strip())
                except ValueError:
                    errors.append('Date de création invalide.')
            data = {
                'tax_regime': f.get('tax_regime') if f.get('tax_regime') in ('normal', 'simplifie') else 'normal',
                'vat_filing': f.get('vat_filing') if f.get('vat_filing') in CT.VAT_FILINGS else 'ca3_mensuelle',
                'activite': (f.get('activite') or '').strip()[:200], 'pme_innovante': bool(f.get('pme_innovante')),
                'groupe': bool(f.get('groupe')), 'societe_mere_siren': ''.join(ch for ch in f.get('societe_mere_siren', '') if ch.isdigit())[:9],
                'expert_comptable': (f.get('expert_comptable') or '').strip()[:200],
                'expert_comptable_tel': (f.get('expert_comptable_tel') or '').strip()[:30],
                'conseil': (f.get('conseil') or '').strip()[:200], 'signataire': (f.get('signataire') or '').strip()[:120],
                'signataire_qualite': (f.get('signataire_qualite') or '').strip()[:80], 'mel': (f.get('mel') or '').strip()[:120],
                'shareholders': shareholders, 'subsidiaries': subsidiaries,
                'date_creation': (f.get('date_creation') or '').strip()[:10],
                'premises': premises,
            }
            if current_role() in MANDATE_ROLES and f.get('mandate_iban') is not None:
                from profitos.fr_identifiers import iban_is_valid
                iban = ''.join((f.get('mandate_iban') or '').split()).upper()
                if not iban and not f.get('mandate_clear'):
                    iban = profile['mandate'].get('iban') or ''
                elif iban and not iban_is_valid(iban):
                    errors.append("IBAN du compte de prélèvement invalide.")
                bic = (f.get('mandate_bic') or '').strip().upper()
                if bic and not re.fullmatch(r'[A-Z]{6}[A-Z0-9]{2}([A-Z0-9]{3})?', bic):
                    errors.append('BIC invalide.')
                if f.get('mandate_clear'):
                    data['mandate'] = dict(CT.DEFAULT_PROFILE['mandate'])
                else:
                    data['mandate'] = {'iban': iban, 'bic': bic, 'holder': (f.get('mandate_holder') or '').strip()[:120],
                                       'rum': (f.get('mandate_rum') or '').strip()[:35], 'signed_at': (f.get('mandate_signed_at') or '').strip()[:10],
                                       'bank_confirmed': bool(f.get('mandate_bank_confirmed'))}
            if errors:
                c.close()
                for e in errors[:5]:
                    flash(e)
                return redirect(url_for('fiscal_profile'))
            CT.save_fiscal_profile(c, eid, data, now(), current_user()['email'])
            c.close()
            log_activity('FISCAL_PROFILE', 'Profil fiscal mis à jour')
            if 'mandate' in data:
                log_security_event('FISCAL_MANDATE_UPDATED', 'SUCCESS', target='mandat de prélèvement')
            flash('Profil fiscal enregistré.')
            return redirect(url_for('fiscal_profile'))
        c.close()
        return render_template('fiscal_profile.html', p=profile, vat_filings=CT.VAT_FILINGS,
                               can_mandate=current_role() in MANDATE_ROLES, mandate_ok=CT.mandate_ready(profile),
                               masked=CT.masked_iban(profile['mandate'].get('iban')))

    @app.route('/comptabilite/tva/ca12')
    @login_required
    def accounting_ca12():
        from profitos import ledger as L
        from profitos.vat_return import ca12_form, CA12_BOXES
        eid = _eid()
        c = cx()
        current = L.fiscal_year_for(c, paris_today(), eid)
        previous = L.fiscal_year_for(c, current['start'] - timedelta(days=1), eid)
        choice = request.args.get('exercice') or 'precedent'
        fy = current if choice == 'courant' else previous
        settings = CT.get_fiscal_settings(c, eid, fy['end'].isoformat())
        form = ca12_form(c, fy['start'].isoformat(), fy['end'].isoformat(), eid, acomptes=settings.get('tva_acomptes_verses'))
        c.close()
        return render_template('accounting_ca12.html', f=form, boxes=CA12_BOXES, fy=fy, choice=choice,
                               current=current, previous=previous)
