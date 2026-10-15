"""Écrans des automatisations comptables : écritures récurrentes, échéancier fiscal et
contrôles avant liasse."""
from datetime import timedelta

from profitos.runtime import *
from profitos.accounting import paris_today
from profitos import automation as A

AUTOMATIC_JOBS = [
    ('recurring_entries', 'Écritures récurrentes', 'Chaque jour à 5 h'),
    ('fiscal_deadlines', 'Rappels des échéances fiscales', 'Chaque jour à 8 h, 7 jours et la veille'),
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
        return render_template('fiscal_calendar.html', deadlines=deadlines, controls=controls, closed=closed,
                               jobs=AUTOMATIC_JOBS, runs=runs, today=today)
