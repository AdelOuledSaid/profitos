"""Écrans DAS2 (honoraires versés) et 2777 (prélèvements sur dividendes)."""
from datetime import date

from profitos.runtime import *
from profitos.accounting import paris_today, AccountingError
from profitos import tiers_declarations as TD


def _eid():
    from profitos.entities import current_entity_id
    return current_entity_id()


def _fy_end_md(c, eid, year):
    from profitos import ledger as L
    fy = L.fiscal_year_for(c, date(year, 12, 31), eid)
    return (fy['end'].month, fy['end'].day)


def register(app):
    @app.route('/comptabilite/das2', methods=['GET', 'POST'])
    @login_required
    def das2_page():
        today = paris_today()
        try:
            year = int(request.values.get('annee') or today.year - 1)
        except ValueError:
            year = today.year - 1
        if not 2000 <= year <= today.year:
            year = today.year - 1
        eid = _eid()
        c = cx()
        if request.method == 'POST' and not fiscal_write_allowed():
            c.close()
            log_security_event('FISCAL_WRITE_DENIED', 'BLOCKED', target='das2_page')
            flash(FISCAL_WRITE_DENIED)
            return redirect(url_for('das2_page', annee=year))
        if request.method == 'POST':
            f = request.form
            rows, errors = {}, []
            for i in range(1, 201):
                key = (f.get(f'key_{i}') or '').strip()
                if not key:
                    continue
                raw = (f.get(f'amount_{i}') or '').strip()
                try:
                    amount = form_float(raw) if raw else None
                except ValueError:
                    errors.append(f"Montant invalide pour {key}.")
                    continue
                if amount is not None and amount < 0:
                    errors.append(f"Montant négatif pour {key}.")
                    continue
                siret = ''.join(ch for ch in f.get(f'siret_{i}', '') if ch.isalnum()).upper()[:20]
                cat = f.get(f'category_{i}') if f.get(f'category_{i}') in TD.DAS2_CATEGORIES else None
                rows[key[:120]] = {'include': bool(f.get(f'include_{i}')), 'siret': siret,
                                   'address': (f.get(f'address_{i}') or '').strip()[:200],
                                   'profession': (f.get(f'profession_{i}') or '').strip()[:80],
                                   'category': cat, 'amount': amount}
            if errors:
                c.close()
                for e in errors[:5]:
                    flash(e)
                return redirect(url_for('das2_page', annee=year))
            TD.save_das2(c, eid, year, rows, now(), current_user()['email'])
            c.close()
            log_activity('DAS2_SAVED', f'DAS2 {year} mise à jour')
            flash('DAS2 enregistrée.')
            return redirect(url_for('das2_page', annee=year))
        data = TD.das2(c, year, eid, _fy_end_md(c, eid, year))
        c.close()
        if request.args.get('format') in ('xlsx', 'csv'):
            return export_response([{'Bénéficiaire': r['name'], 'SIRET': r['siret'], 'Adresse': r['address'],
                                     'Profession': r['profession'], 'Catégorie': TD.DAS2_CATEGORIES.get(r['category'], r['category']),
                                     'Montant TTC': r['amount']} for r in data['included']], f'das2-{year}')
        return render_template('das2.html', d=data, year=year, categories=TD.DAS2_CATEGORIES,
                               years=list(range(today.year - 1, today.year - 5, -1)))

    @app.route('/comptabilite/dividendes-2777', methods=['GET', 'POST'])
    @login_required
    def rcm_2777_page():
        from profitos import corporate_tax as CT
        today = paris_today()
        raw = (request.values.get('mois') or '').strip()
        eid = _eid()
        c = cx()
        months = TD.dividend_months(c, eid)
        try:
            month = date.fromisoformat(raw + '-01') if raw else (months[0] if months else today.replace(day=1))
        except ValueError:
            month = months[0] if months else today.replace(day=1)
        if request.method == 'POST' and not fiscal_write_allowed():
            c.close()
            log_security_event('FISCAL_WRITE_DENIED', 'BLOCKED', target='rcm_2777_page')
            flash(FISCAL_WRITE_DENIED)
            return redirect(url_for('rcm_2777_page', mois=month.strftime('%Y-%m')))
        if request.method == 'POST':
            try:
                part = (request.form.get('part_pp') or '').strip()
                part_pp = form_float(part) if part else None
                dispensed = form_float(request.form.get('dispensed'))
                if part_pp is not None and not 0 <= part_pp <= 100:
                    raise ValueError('part hors de 0 à 100 %')
                if dispensed < 0:
                    raise ValueError('montant négatif')
            except ValueError as exc:
                c.close()
                flash(f"Saisie invalide : {exc}.")
                return redirect(url_for('rcm_2777_page', mois=month.strftime('%Y-%m')))
            TD.save_store(c, eid, f"rcm-{month.strftime('%Y-%m')}", {'part_pp': part_pp, 'dispensed': dispensed},
                          now(), current_user()['email'])
            c.close()
            flash('Paramètres de la déclaration 2777 enregistrés.')
            return redirect(url_for('rcm_2777_page', mois=month.strftime('%Y-%m')))
        data = TD.rcm_2777(c, month, eid, CT.get_fiscal_profile(c, eid))
        c.close()
        return render_template('rcm_2777.html', d=data, months=months, month=month)

    @app.route('/comptabilite/ifu', methods=['GET', 'POST'])
    @login_required
    def ifu_page():
        from profitos import corporate_tax as CT
        today = paris_today()
        try:
            year = int(request.values.get('annee') or today.year - 1)
        except ValueError:
            year = today.year - 1
        if not 2000 <= year <= today.year:
            year = today.year - 1
        eid = _eid()
        c = cx()
        if request.method == 'POST' and not fiscal_write_allowed():
            c.close()
            log_security_event('FISCAL_WRITE_DENIED', 'BLOCKED', target='ifu_page')
            flash(FISCAL_WRITE_DENIED)
            return redirect(url_for('ifu_page', annee=year))
        if request.method == 'POST':
            keys = [k[:120] for k in request.form.getlist('dispensed')][:50]
            TD.save_store(c, eid, f'ifu-{year}', {'dispensed': keys}, now(), current_user()['email'])
            c.close()
            flash('IFU enregistré.')
            return redirect(url_for('ifu_page', annee=year))
        data = TD.ifu_2561(c, year, eid, CT.get_fiscal_profile(c, eid))
        c.close()
        if request.args.get('format') in ('xlsx', 'csv'):
            return export_response([{'Bénéficiaire': b['name'], 'Type': 'Personne morale' if b['kind'] == 'PM' else 'Personne physique',
                                     'Adresse': b['address'], 'Né(e) le': b['birth_date'], 'Dividendes bruts': b['gross'],
                                     '2DC': b['box_2DC'], '2CK': b['box_2CK'], '2BH': b['box_2BH'],
                                     'Prélèvements sociaux retenus': b['ps']} for b in data['rows']], f'ifu-2561-{year}')
        return render_template('ifu_2561.html', d=data, year=year, years=list(range(today.year - 1, today.year - 5, -1)))

    @app.route('/comptabilite/cfe', methods=['GET', 'POST'])
    @login_required
    def cfe_page():
        from profitos import corporate_tax as CT
        from profitos import local_taxes as LT
        today = paris_today()
        try:
            year = int(request.values.get('annee') or today.year)
        except ValueError:
            year = today.year
        if not 2000 <= year <= today.year + 1:
            year = today.year
        eid = _eid()
        c = cx()
        if request.method == 'POST' and not fiscal_write_allowed():
            c.close()
            log_security_event('FISCAL_WRITE_DENIED', 'BLOCKED', target='cfe_page')
            flash(FISCAL_WRITE_DENIED)
            return redirect(url_for('cfe_page', annee=year))
        if request.method == 'POST':
            action = request.form.get('action')
            try:
                if action == 'entry':
                    data = LT.get_cfe(c, eid, year)
                    if data.get('entry_id'):
                        raise AccountingError(f"La CFE {year} est déjà comptabilisée.")
                    entry_date = request.form.get('entry_date') or date(year, 12, 15).isoformat()
                    date.fromisoformat(entry_date)
                    entry_id = LT.generate_cfe_entry(c, year, data.get('montant'), entry_date, eid, current_user()['email'])
                    LT.save_cfe(c, eid, year, {'entry_id': entry_id}, now(), current_user()['email'])
                    c.close()
                    log_activity('CFE_ENTRY', f'CFE {year} comptabilisée')
                    flash('Charge de CFE comptabilisée (635110 / 447000).')
                    return redirect(url_for('accounting_entry_detail', entry_id=entry_id))
                f = request.form
                montant = (f.get('montant') or '').strip()
                n1 = (f.get('montant_n1') or '').strip()
                values = {'montant': form_float(montant) if montant else None, 'montant_n1': form_float(n1) if n1 else None,
                          'mensualise': bool(f.get('mensualise')), 'changements': bool(f.get('changements'))}
                if any(v is not None and v < 0 for v in (values['montant'], values['montant_n1'])):
                    raise ValueError('montant négatif')
                LT.save_cfe(c, eid, year, values, now(), current_user()['email'])
                flash('CFE enregistrée : les échéances sont mises à jour.')
            except (AccountingError, ValueError) as exc:
                flash(str(exc) if isinstance(exc, AccountingError) else f"Saisie invalide : {exc}.")
            c.close()
            return redirect(url_for('cfe_page', annee=year))
        data = LT.get_cfe(c, eid, year)
        profile = CT.get_fiscal_profile(c, eid)
        sched = LT.cfe_schedule(year, data, profile.get('date_creation'))
        c.close()
        return render_template('cfe.html', d=data, s=sched, year=year,
                               years=list(range(today.year + 1, today.year - 4, -1)), today=today)
