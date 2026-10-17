"""Comptabilité complète — lot 1 : exercices, saisie manuelle d'écritures (OD) avec
pièces jointes et contrepassation, grand livre, balances, reprise d'un FEC."""
import os
import uuid
from datetime import timedelta

from profitos.runtime import *
from profitos import storage as _storage
from profitos.accounting import AccountingError, create_entry, paris_today
from profitos import ledger as L

ATTACHMENT_EXTENSIONS = {'.pdf', '.png', '.jpg', '.jpeg', '.webp', '.heic', '.xlsx', '.xls', '.csv', '.docx', '.txt'}
MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024
ENTRY_FORM_ROWS = 6


def _eid():
    from profitos.entities import current_entity_id
    return current_entity_id()


def _who():
    u = current_user()
    return (u['email'] if u else None)


def _period_args(conn, eid):
    """Période demandée (?du=&au=), par défaut l'exercice en cours jusqu'à aujourd'hui."""
    fy = L.fiscal_year_for(conn, paris_today(), eid)
    du = request.args.get('du') or fy['start'].isoformat()
    au = request.args.get('au') or min(fy['end'], paris_today()).isoformat()
    try:
        L._d(du); L._d(au)
    except ValueError:
        du, au = fy['start'].isoformat(), fy['end'].isoformat()
    if du > au:
        du, au = au, du
    return du, au, fy


def _entry_or_404(conn, entry_id, eid):
    e = conn.execute('SELECT * FROM accounting_entries WHERE id=? AND entity_id IS ?', (entry_id, eid)).fetchone()
    if not e:
        abort(404)
    return e


def register(app):
    # ------------------------------------------------------------------ exercices
    @app.route('/comptabilite/exercices', methods=['POST'])
    @login_required
    def accounting_fiscal_year_add():
        c = cx()
        try:
            L.add_fiscal_year(c, request.form.get('start_date'), request.form.get('end_date'), _eid())
            log_activity('ACCOUNTING_FISCAL_YEAR', f"Exercice déclaré {request.form.get('start_date')} → {request.form.get('end_date')}")
            flash("Exercice enregistré.")
        except AccountingError as exc:
            flash(str(exc))
        finally:
            c.close()
        return redirect(url_for('accounting_closure') + '#exercices')

    # ------------------------------------------------------------------ saisie
    @app.route('/comptabilite/ecritures/nouvelle', methods=['GET', 'POST'])
    @login_required
    def accounting_entry_new():
        eid = _eid()
        c = cx()
        journals = c.execute('SELECT code,label FROM accounting_journals ORDER BY code').fetchall()
        accounts = c.execute(
            'SELECT code,label FROM accounting_chart_of_accounts WHERE is_active=1 AND (entity_id IS NULL OR entity_id=?) ORDER BY code',
            (eid,)).fetchall()
        try:
            rows = max(ENTRY_FORM_ROWS, min(40, int(request.values.get('lignes') or ENTRY_FORM_ROWS)))
        except ValueError:
            rows = ENTRY_FORM_ROWS
        form = request.form if request.method == 'POST' else {}
        if request.method == 'POST':
            lines = []
            for i in range(rows):
                code = (request.form.get(f'compte_{i}') or '').strip().split(' ')[0]
                debit = parse_amount(request.form.get(f'debit_{i}')) or 0
                credit = parse_amount(request.form.get(f'credit_{i}')) or 0
                if not code and not debit and not credit:
                    continue
                lines.append({'account_code': code, 'debit': debit, 'credit': credit,
                              'label': (request.form.get(f'libelle_{i}') or '').strip()[:250] or None,
                              'auxiliary_name': (request.form.get(f'tiers_{i}') or '').strip()[:120] or None})
            label = (request.form.get('label') or '').strip()[:250]
            journal = (request.form.get('journal') or 'OD').strip()
            entry_date = request.form.get('entry_date') or paris_today().isoformat()
            if not label:
                flash("Le libellé de l'écriture est obligatoire.")
            else:
                try:
                    entry_id = create_entry(c, journal, entry_date, label, lines, created_by=_who(), entity_id=eid,
                                            piece_ref=(request.form.get('piece_ref') or '').strip()[:120] or None)
                    upload = request.files.get('piece')
                    if upload and upload.filename:
                        try:
                            _save_attachment(c, entry_id, upload)
                        except ValueError as exc:
                            flash(f"Écriture enregistrée, mais pièce jointe non ajoutée : {exc}")
                    log_activity('ACCOUNTING_MANUAL_ENTRY', f"Écriture manuelle {journal} du {entry_date} : {label}")
                    c.close()
                    flash("Écriture enregistrée.")
                    if request.form.get('next') == 'new':
                        return redirect(url_for('accounting_entry_new'))
                    return redirect(url_for('accounting_entry_detail', entry_id=entry_id))
                except (AccountingError, ValueError) as exc:
                    flash(f"Écriture non enregistrée : {exc}")
        c.close()
        return render_template('accounting_entry_form.html', journals=journals, accounts=accounts, rows=rows,
                               form=form, today=paris_today().isoformat())

    def _save_attachment(conn, entry_id, upload):
        ext = os.path.splitext(upload.filename)[1].lower()[:10]
        if ext not in ATTACHMENT_EXTENSIONS:
            raise ValueError("pièce jointe refusée (PDF, image, Excel, CSV, Word ou texte).")
        data = upload.read(MAX_ATTACHMENT_BYTES + 1)
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise ValueError("pièce jointe trop volumineuse (15 Mo au plus).")
        stored = uuid.uuid4().hex + ext
        _storage.write_bytes('accounting_attachments', session['org_id'], stored, data)
        conn.execute('INSERT INTO accounting_entry_attachments(entry_id,filename,stored_name,uploaded_by,uploaded_at) '
                     'VALUES(?,?,?,?,?)', (entry_id, secure_filename(upload.filename)[:200] or 'piece' + ext,
                                          stored, _who(), now()))
        conn.commit()

    @app.route('/comptabilite/ecritures/<int:entry_id>')
    @login_required
    def accounting_entry_detail(entry_id):
        eid = _eid()
        c = cx()
        e = _entry_or_404(c, entry_id, eid)
        lines = c.execute('SELECT * FROM accounting_entry_lines WHERE entry_id=? ORDER BY line_order', (entry_id,)).fetchall()
        attachments = c.execute('SELECT * FROM accounting_entry_attachments WHERE entry_id=? ORDER BY id', (entry_id,)).fetchall()
        reversal = c.execute("SELECT id,piece_number,entry_date FROM accounting_entries WHERE source_type='reversal' "
                             "AND source_id=? AND entity_id IS ?", (entry_id, eid)).fetchone()
        reversed_of = (c.execute('SELECT id,piece_number FROM accounting_entries WHERE id=?', (e['source_id'],)).fetchone()
                       if e['source_type'] == 'reversal' and e['source_id'] else None)
        labels = {r['code']: r['label'] for r in c.execute('SELECT code,label FROM accounting_chart_of_accounts').fetchall()}
        c.close()
        return render_template('accounting_entry_detail.html', e=e, lines=lines, attachments=attachments,
                               reversal=reversal, reversed_of=reversed_of, labels=labels,
                               today=paris_today().isoformat())

    @app.route('/comptabilite/ecritures/<int:entry_id>/contrepasser', methods=['POST'])
    @login_required
    def accounting_entry_reverse(entry_id):
        eid = _eid()
        c = cx()
        _entry_or_404(c, entry_id, eid)
        try:
            new_id = L.reverse_entry(c, entry_id, request.form.get('reversal_date') or paris_today().isoformat(),
                                     created_by=_who(), entity_id=eid)
            log_activity('ACCOUNTING_REVERSAL', f"Contrepassation de l'écriture #{entry_id}")
            flash("Écriture contrepassée.")
            return redirect(url_for('accounting_entry_detail', entry_id=new_id))
        except AccountingError as exc:
            flash(f"Contrepassation impossible : {exc}")
            return redirect(url_for('accounting_entry_detail', entry_id=entry_id))
        finally:
            c.close()

    @app.route('/comptabilite/ecritures/<int:entry_id>/pieces', methods=['POST'])
    @login_required
    def accounting_entry_attach(entry_id):
        eid = _eid()
        c = cx()
        _entry_or_404(c, entry_id, eid)
        upload = request.files.get('piece')
        try:
            if not upload or not upload.filename:
                raise ValueError("aucun fichier sélectionné.")
            _save_attachment(c, entry_id, upload)
            flash("Pièce jointe ajoutée.")
        except ValueError as exc:
            flash(f"Pièce non ajoutée : {exc}")
        finally:
            c.close()
        return redirect(url_for('accounting_entry_detail', entry_id=entry_id))

    @app.route('/comptabilite/ecritures/<int:entry_id>/pieces/<int:attachment_id>')
    @login_required
    def accounting_entry_attachment(entry_id, attachment_id):
        eid = _eid()
        c = cx()
        _entry_or_404(c, entry_id, eid)
        att = c.execute('SELECT * FROM accounting_entry_attachments WHERE id=? AND entry_id=?',
                        (attachment_id, entry_id)).fetchone()
        c.close()
        if not att:
            abort(404)
        try:
            data = _storage.read_bytes('accounting_attachments', session['org_id'], att['stored_name'])
        except (OSError, ValueError):
            abort(404)
        import mimetypes
        mime = mimetypes.guess_type(att['filename'])[0] or 'application/octet-stream'
        return Response(data, mimetype=mime, headers={
            'Content-Disposition': f'attachment; filename="{att["filename"]}"',
            'X-Content-Type-Options': 'nosniff'})

    # ------------------------------------------------------------------ grand livre / balance
    @app.route('/comptabilite/grand-livre')
    @login_required
    def accounting_general_ledger():
        eid = _eid()
        c = cx()
        du, au, fy = _period_args(c, eid)
        compte_de = (request.args.get('compte_de') or '').strip()
        compte_a = (request.args.get('compte_a') or '').strip()
        tiers = (request.args.get('tiers') or '').strip()
        gl = L.general_ledger(c, du, au, eid, compte_de or None, compte_a or None, tiers or None)
        c.close()
        if request.args.get('format') in ('xlsx', 'csv'):
            return export_response(L.ledger_rows(gl), f'grand-livre-{du}-{au}')
        return render_template('accounting_ledger.html', gl=gl, du=du, au=au, fy=fy,
                               compte_de=compte_de, compte_a=compte_a, tiers=tiers)

    @app.route('/comptabilite/balance')
    @login_required
    def accounting_trial_balance():
        eid = _eid()
        c = cx()
        du, au, fy = _period_args(c, eid)
        kind = request.args.get('type') if request.args.get('type') in ('generale', 'clients', 'fournisseurs') else 'generale'
        tb = L.trial_balance(c, du, au, eid, kind)
        c.close()
        if request.args.get('format') in ('xlsx', 'csv'):
            return export_response(L.balance_rows(tb), f'balance-{kind}-{du}-{au}')
        return render_template('accounting_balance.html', tb=tb, du=du, au=au, fy=fy, kind=kind)

    # ------------------------------------------------------------------ reprise FEC
    @app.route('/comptabilite/reprise', methods=['GET', 'POST'])
    @login_required
    def accounting_fec_import():
        eid = _eid()
        c = cx()
        if request.method == 'POST':
            upload = request.files.get('fec')
            mode = 'soldes' if request.form.get('mode') == 'soldes' else 'ecritures'
            if not upload or not upload.filename:
                flash("Sélectionnez le fichier FEC exporté de votre ancien logiciel.")
            else:
                try:
                    data = upload.read(L.MAX_FEC_BYTES + 1)
                    result = L.import_fec(c, data, secure_filename(upload.filename), mode, eid, _who(),
                                          request.form.get('opening_date') or None)
                    log_activity('ACCOUNTING_FEC_IMPORT', f"Reprise FEC ({mode}) : {result['entries']} écriture(s)")
                    if mode == 'soldes':
                        flash(f"Soldes d'ouverture repris ({pluralize(result['lines'], 'ligne')} lues). "
                              f"{pluralize(result['accounts_created'], 'compte créé', 'comptes créés')}.")
                    else:
                        flash(f"{pluralize(result['entries'], 'écriture importée', 'écritures importées')} "
                              f"({result['date_from']:%d/%m/%Y} → {result['date_to']:%d/%m/%Y}), "
                              f"{pluralize(result['accounts_created'], 'compte créé', 'comptes créés')}.")
                    c.close()
                    return redirect(url_for('accounting_trial_balance'))
                except AccountingError as exc:
                    flash(f"Import annulé, rien n'a été enregistré : {exc}")
        batches = c.execute('SELECT * FROM accounting_import_batches WHERE entity_key=? ORDER BY id DESC',
                            (int(eid) if eid else 0,)).fetchall()
        c.close()
        return render_template('accounting_fec_import.html', batches=batches)

    # ------------------------------------------------------------------ TVA CA3
    def _vat_period():
        today = paris_today()
        first_this_month = today.replace(day=1)
        last_prev = first_this_month - timedelta(days=1)
        du = request.values.get('date_from') or last_prev.replace(day=1).isoformat()
        au = request.values.get('date_to') or last_prev.isoformat()
        try:
            if L._d(du) > L._d(au):
                du, au = au, du
        except ValueError:
            du, au = last_prev.replace(day=1).isoformat(), last_prev.isoformat()
        return du, au

    @app.route('/comptabilite/tva/ca3')
    @login_required
    def accounting_ca3():
        from profitos.vat_return import ca3_form
        du, au = _vat_period()
        c = cx()
        form = ca3_form(c, du, au, _eid())
        c.close()
        return render_template('accounting_ca3.html', f=form, date_from=du, date_to=au)

    @app.route('/comptabilite/tva/ca3/liquider', methods=['POST'])
    @login_required
    def accounting_ca3_liquidate():
        from profitos.vat_return import generate_liquidation_entry
        du, au = _vat_period()
        c = cx()
        try:
            entry_id = generate_liquidation_entry(c, du, au, _eid(), created_by=_who())
            log_activity('ACCOUNTING_VAT_LIQUIDATION', f"Liquidation de TVA {du} → {au}")
            flash("Écriture de liquidation de TVA enregistrée.")
            return redirect(url_for('accounting_entry_detail', entry_id=entry_id))
        except AccountingError as exc:
            flash(f"Liquidation impossible : {exc}")
            return redirect(url_for('accounting_ca3', date_from=du, date_to=au))
        finally:
            c.close()

    # ------------------------------------------------------------------ liasse 2033
    @app.route('/comptabilite/liasse-2033')
    @login_required
    def accounting_liasse_2033():
        from profitos.liasse import liasse_2033, liasse_rows
        from profitos.liasse_2033_annexes import annexes_2033, annexes_2033_rows
        from profitos.liasse_annexes import form_2065
        from profitos import corporate_tax as CT
        from profitos.entities import resolve_entity
        eid = _eid()
        c = cx()
        fy, prior, choice, current, previous = _fiscal_years(c, eid)
        data = liasse_2033(c, fy['start'], fy['end'], eid)
        ln, settings, is_res, _cv, _cal, _a = _liasse_and_tax(c, eid, fy, prior)
        profile = CT.get_fiscal_profile(c, eid)
        try:
            identity = dict(resolve_entity(c, eid))
        except Exception:
            identity = {}
        ax = annexes_2033(c, fy['start'], fy['end'], eid, profile=profile, settings=settings, liasse=ln)
        from profitos import local_taxes as LT
        dl = LT.decloyer(profile, LT.rent_booked(c, fy['start'], fy['end'], eid))
        f2065 = form_2065(identity, dict(profile, tax_regime='simplifie'), ln, is_res, fy['start'], fy['end'])
        c.close()
        if request.args.get('format') in ('xlsx', 'csv'):
            extra = [{'Tableau': r['Tableau'], 'Case': r['Case'], 'Libellé': r['Libellé'], 'Net': r['Montant']}
                     for r in annexes_2033_rows(ax, f2065)]
            extra += [{'Tableau': 'DECLOYER', 'Case': r['Case'], 'Libellé': r['Libellé'], 'Net': r['Montant']}
                      for r in LT.decloyer_rows(dl)]
            return export_response(liasse_rows(data) + extra, f"liasse-2033-{fy['end'].isoformat()}")
        return render_template('accounting_liasse_2033.html', l=data, fy=fy, choice=choice,
                               current=current, previous=previous, ax=ax, f2065=f2065, is_res=is_res, dl=dl)

    # ------------------------------------------------------------------ régime normal 2050-2059, IS, CVAE
    def _fiscal_years(c, eid):
        current = L.fiscal_year_for(c, paris_today(), eid)
        previous = L.fiscal_year_for(c, current['start'] - timedelta(days=1), eid)
        before = L.fiscal_year_for(c, previous['start'] - timedelta(days=1), eid)
        choice = request.args.get('exercice') or 'precedent'
        if choice == 'courant':
            return current, previous, choice, current, previous
        return previous, before, 'precedent', current, previous

    def _liasse_and_tax(c, eid, fy, prior):
        from profitos.liasse_normal import liasse_normale
        from profitos import corporate_tax as CT
        settings = CT.get_fiscal_settings(c, eid, fy['end'].isoformat())
        data = liasse_normale(c, fy['start'], fy['end'], eid, settings=settings,
                              previous=(prior['start'], prior['end']))
        eligible = CT.pme_eligible(data['chiffre_affaires'], fy['start'], fy['end'],
                                   settings.get('capital_libere'), settings.get('detention_pp'))
        acomptes = settings.get('acomptes_verses')
        acomptes_auto = CT.acomptes_from_ledger(c, fy['start'], fy['end'], eid)
        if acomptes is None or acomptes == '':
            acomptes = acomptes_auto
        is_res = CT.compute_is(data['fiscal']['XN'], fy['start'], fy['end'], data['chiffre_affaires'], eligible,
                               credits=settings.get('credits_impot') or 0, acomptes=acomptes)
        cvae = CT.compute_cvae(data['va']['OX'], data['va']['OG'], fy['start'], fy['end'])
        calendar = CT.fiscal_calendar(fy['start'], fy['end'], is_res, cvae)
        return data, settings, is_res, cvae, calendar, acomptes_auto

    @app.route('/comptabilite/liasse-2050')
    @login_required
    def accounting_liasse_2050():
        from profitos.liasse_normal import liasse_normale_rows
        eid = _eid()
        c = cx()
        fy, prior, choice, current, previous = _fiscal_years(c, eid)
        data, settings, is_res, cvae, calendar, _a = _liasse_and_tax(c, eid, fy, prior)
        from profitos import corporate_tax as CT
        from profitos.liasse_annexes import annexes, annexes_rows, form_2065
        from profitos.entities import resolve_entity
        profile = CT.get_fiscal_profile(c, eid)
        try:
            identity = dict(resolve_entity(c, eid))
        except Exception:
            identity = {}
        ax = annexes(c, fy['start'], fy['end'], eid, profile=profile, settings=settings)
        f2065 = form_2065(identity, profile, data, is_res, fy['start'], fy['end'])
        from profitos import local_taxes as LT
        dl = LT.decloyer(profile, LT.rent_booked(c, fy['start'], fy['end'], eid))
        c.close()
        if request.args.get('format') in ('xlsx', 'csv'):
            return export_response(liasse_normale_rows(data) + annexes_rows(ax, f2065) + LT.decloyer_rows(dl),
                                   f"liasse-2050-2059-{fy['end'].isoformat()}")
        from profitos.automation import precontrols
        c = cx()
        controls = precontrols(c, fy['start'], fy['end'], eid, today=paris_today())
        c.close()
        return render_template('accounting_liasse_2050.html', l=data, fy=fy, choice=choice,
                               current=current, previous=previous, is_res=is_res, controls=controls,
                               ax=ax, f2065=f2065, profile=profile, dl=dl)

    @app.route('/comptabilite/impot-societes', methods=['GET', 'POST'])
    @login_required
    def accounting_corporate_tax():
        from profitos import corporate_tax as CT
        eid = _eid()
        c = cx()
        fy, prior, choice, current, previous = _fiscal_years(c, eid)
        if request.method == 'POST':
            action = request.form.get('action')
            try:
                if action == 'settings':
                    def _rows(prefix):
                        out = []
                        for i in range(1, 9):
                            amt = (request.form.get(f'{prefix}_amount_{i}') or '').strip()
                            if not amt:
                                continue
                            out.append({'code': (request.form.get(f'{prefix}_code_{i}') or '').strip()[:3],
                                        'label': (request.form.get(f'{prefix}_label_{i}') or '').strip()[:120],
                                        'amount': form_float(amt)})
                        return out
                    raw_acomptes = (request.form.get('acomptes_verses') or '').strip()
                    raw_tva = (request.form.get('tva_acomptes_verses') or '').strip()
                    CT.save_fiscal_settings(c, eid, fy['end'].isoformat(), {
                        'capital_libere': bool(request.form.get('capital_libere')),
                        'detention_pp': bool(request.form.get('detention_pp')),
                        'deficits_anterieurs': form_float(request.form.get('deficits_anterieurs')),
                        'credits_impot': form_float(request.form.get('credits_impot')),
                        'acomptes_verses': form_float(raw_acomptes) if raw_acomptes else None,
                        'reintegrations': _rows('reint'), 'deductions': _rows('deduc'),
                        'mvlt_anterieures': form_float(request.form.get('mvlt_anterieures')),
                        'tva_acomptes_verses': form_float(raw_tva) if raw_tva else None,
                    }, now(), _who())
                    log_activity('FISCAL_SETTINGS', f"Paramètres fiscaux de l'exercice clos le {fy['end'].isoformat()}")
                    flash('Paramètres fiscaux enregistrés : la liasse et l\'impôt sont recalculés.')
                elif action == 'entry':
                    _d, _s, is_res, _cv, _cal, _aa = _liasse_and_tax(c, eid, fy, prior)
                    entry_id = CT.generate_is_entry(c, fy['end'], is_res['is_du'], eid, _who())
                    log_activity('CORPORATE_TAX_ENTRY', f"IS comptabilisé ({fr_number(is_res['is_du'], 0)} €)")
                    flash("Écriture d'impôt sur les sociétés enregistrée.")
                    c.close()
                    return redirect(url_for('accounting_entry_detail', entry_id=entry_id))
            except ValueError as exc:
                flash(f"Saisie invalide : {exc}" if not isinstance(exc, AccountingError) else str(exc))
            c.close()
            return redirect(url_for('accounting_corporate_tax', exercice=choice))
        data, settings, is_res, cvae, calendar, acomptes_auto = _liasse_and_tax(c, eid, fy, prior)
        c.close()
        return render_template('accounting_corporate_tax.html', l=data, fy=fy, choice=choice, current=current,
                               previous=previous, settings=settings, is_res=is_res, cvae=cvae, calendar=calendar,
                               acomptes_auto=acomptes_auto, today=paris_today())

