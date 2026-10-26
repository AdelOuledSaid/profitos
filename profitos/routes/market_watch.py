"""Veille de votre secteur : indicateurs officiels choisis selon l'activité de
l'entreprise, alertes, révision de prix et actualités (v469)."""
from profitos.runtime import *
from profitos import market_watch as MW

REFRESH_COOLDOWN_MIN = 10


def _eid():
    from profitos.entities import current_entity_id
    return current_entity_id()


def _company_texts(c, eid):
    texts = []
    try:
        row = c.execute('SELECT activities FROM company WHERE id=1').fetchone()
        if row and row['activities']:
            texts.append(row['activities'])
    except Exception:
        pass
    try:
        from profitos.corporate_tax import get_fiscal_profile
        texts.append(get_fiscal_profile(c, eid).get('activite') or '')
    except Exception:
        pass
    return texts


def _effective_sector(settings, texts):
    if settings.get('sector') in MW.SECTORS:
        return settings['sector'], False
    guess = MW.sector_from_naf(settings.get('naf')) or MW.suggest_sector(*texts)
    return guess or 'autre', True


def _cards(ac, settings):
    cards = []
    for code in MW.series_for(settings):
        defn = MW.definition(code, settings.get('custom'))
        if not defn:
            continue
        obs = MW.observations(ac, code)
        meta = MW.series_meta(ac, code) or {}
        st = MW.series_stats(defn, obs, meta)
        cards.append({'code': code, 'defn': defn, 'stat': st, 'meta': meta, 'alerts': MW.alerts(st, settings),
                      'spark': MW.sparkline(obs), 'url': MW.source_url(defn),
                      'title': meta.get('title') or defn['label']})
    return cards


def register(app):
    register_invoice_link(app)

    @app.route('/veille', methods=['GET', 'POST'])
    @login_required
    @requires_active_plan
    @require_area('pilotage')
    def market_watch():
        eid = _eid()
        c = cx()
        settings = MW.load_settings(c, eid)
        if request.method == 'POST':
            action = request.form.get('action')
            user = current_user()['email']
            ac = auth_cx()
            try:
                if action == 'settings':
                    sector = request.form.get('sector') or None
                    naf = re.sub(r'[^0-9A-Za-z.]', '', request.form.get('naf') or '')[:7].upper()
                    chosen = [x for x in request.form.getlist('series') if x in MW.CATALOGUE]
                    try:
                        a12 = form_float(request.form.get('alert_12m') or '5')
                        a1 = form_float(request.form.get('alert_1p') or '2')
                        if not (0 <= a12 <= 100 and 0 <= a1 <= 100):
                            raise ValueError
                    except ValueError:
                        flash("Seuils d'alerte invalides (entre 0 et 100 %).")
                        return redirect(url_for('market_watch'))
                    settings.update({'sector': sector if sector in MW.SECTORS else None, 'naf': naf,
                                     'series': chosen if request.form.get('custom_selection') else None,
                                     'alert_12m': a12, 'alert_1p': a1})
                    settings = MW.save_settings(c, eid, settings, now(), user)
                    # Secteur connu de la comparaison anonyme (choix explicite ou code APE).
                    try:
                        ac.execute('UPDATE organizations SET market_sector=? WHERE id=?',
                                   (settings['sector'] or MW.sector_from_naf(settings['naf']), session['org_id']))
                        ac.commit()
                    except Exception:
                        current_app.logger.exception('secteur de comparaison non enregistré')
                    log_activity('MARKET_WATCH_SETTINGS', 'Veille : réglages mis à jour')
                    flash('Réglages de la veille enregistrés.')
                    sector, _g = _effective_sector(settings, _company_texts(c, eid))
                    missing = [x for x in MW.series_for(dict(settings, sector=sector)) if not MW.series_meta(ac, x)]
                    if missing:
                        MW.refresh(ac, missing, custom=settings['custom'])
                elif action == 'add_custom':
                    try:
                        idbank = MW.parse_idbank(request.form.get('idbank'))
                    except MW.MarketWatchError as exc:
                        flash(str(exc))
                        return redirect(url_for('market_watch'))
                    if any(x['idbank'] == idbank for x in settings['custom']):
                        flash('Cet indicateur est déjà suivi.')
                        return redirect(url_for('market_watch'))
                    if len(settings['custom']) >= MW.MAX_CUSTOM:
                        flash(f'{MW.MAX_CUSTOM} indicateurs personnels au plus.')
                        return redirect(url_for('market_watch'))
                    label = (request.form.get('label') or '').strip()[:120]
                    code = MW.custom_code(idbank)
                    result = MW.refresh(ac, [code], custom=[{'idbank': idbank, 'label': label}])[code]
                    meta = MW.series_meta(ac, code) or {}
                    if result != 'ok' and 'inconnue' in result:
                        flash(f"Série {idbank} introuvable à l'INSEE : vérifiez l'identifiant.")
                        return redirect(url_for('market_watch'))
                    settings['custom'].append({'idbank': idbank, 'label': label or (meta.get('title') or '')[:120]})
                    settings = MW.save_settings(c, eid, settings, now(), user)
                    flash('Indicateur ajouté.' if result == 'ok' else
                          f"Indicateur ajouté ; données indisponibles pour l'instant ({result}), nouvel essai chaque jour.")
                elif action == 'remove_custom':
                    idbank = request.form.get('idbank') or ''
                    settings['custom'] = [x for x in settings['custom'] if x['idbank'] != idbank]
                    settings = MW.save_settings(c, eid, settings, now(), user)
                    flash('Indicateur retiré.')
                elif action == 'refresh':
                    sector, _g = _effective_sector(settings, _company_texts(c, eid))
                    codes = MW.series_for(dict(settings, sector=sector))
                    recent = [x for x in codes if (MW.series_meta(ac, x) or {}).get('fetched_at', '') >
                              (datetime.utcnow() - timedelta(minutes=REFRESH_COOLDOWN_MIN)).isoformat()]
                    todo = [x for x in codes if x not in recent]
                    report = MW.refresh(ac, todo, custom=settings['custom']) if todo else {}
                    failed = [k for k, v in report.items() if v != 'ok']
                    flash('Indicateurs à jour.' if not failed else
                          f"{len(failed)} indicateur(s) indisponible(s) pour l'instant : nouvel essai automatique chaque jour.")
            finally:
                ac.close()
                c.close()
            return redirect(url_for('market_watch'))

        texts = _company_texts(c, eid)
        c.close()
        sector, guessed = _effective_sector(settings, texts)
        view_settings = dict(settings, sector=sector)
        ac = auth_cx()
        try:
            cards = _cards(ac, view_settings)
            news = MW.news_for(ac, sector)
        finally:
            ac.close()
        return render_template('market_watch.html', cards=cards, settings=settings, sector=sector, guessed=guessed,
                               sectors=MW.SECTORS, catalogue=MW.CATALOGUE, common=MW.COMMON,
                               followed=MW.series_for(view_settings), news=news,
                               news_configured=bool(MW.configured_feeds()), max_custom=MW.MAX_CUSTOM,
                               adjustment_labels=MW.ADJUSTMENT_LABELS, adjustment_short=MW.ADJUSTMENT_SHORT,
                               naf_labels=MW.NAF_CHECK_LABELS)

    @app.route('/veille/indicateur/<code>')
    @login_required
    @requires_active_plan
    @require_area('pilotage')
    def market_watch_series(code):
        c = cx()
        settings = MW.load_settings(c, _eid())
        c.close()
        defn = MW.definition(code, settings.get('custom'))
        if not defn or (defn.get('custom') and not any(MW.custom_code(x['idbank']) == code for x in settings['custom'])):
            abort(404)
        ac = auth_cx()
        try:
            obs = MW.observations(ac, code)
            meta = MW.series_meta(ac, code) or {}
            dims = MW.stored_dimensions(ac, defn['idbank']) if defn['source'] == 'insee' else None
        finally:
            ac.close()
        raw = MW.is_raw_activity(defn, meta)
        rows = []
        for i, (period, value) in enumerate(obs):
            prev = obs[i - 1][1] if i else None
            change = None
            if prev and not raw:
                change = round(value - prev, 2) if defn['unit'] == '%' else round((value - prev) / prev * 100, 2)
            rows.append({'period': period, 'label': MW.period_label(period), 'value': value, 'change': change})
        return render_template('market_watch_series.html', defn=defn, meta=meta, rows=list(reversed(rows)),
                               price_index=MW.is_price_index(defn), adjustment_labels=MW.ADJUSTMENT_LABELS,
                               naf_labels=MW.NAF_CHECK_LABELS,
                               dims=dims,
                               stat=MW.series_stats(defn, obs, meta), spark=MW.sparkline(obs, 640, 140, 6),
                               url=MW.source_url(defn), title=meta.get('title') or defn['label'])

    @app.route('/veille/revision')
    @login_required
    @requires_active_plan
    @require_area('pilotage')
    def market_watch_revision():
        c = cx()
        settings = MW.load_settings(c, _eid())
        texts = _company_texts(c, _eid())
        c.close()
        sector, _g = _effective_sector(settings, texts)
        codes = [x for x in MW.series_for(dict(settings, sector=sector)) if MW.is_price_index(MW.definition(x, settings['custom']))]
        a = request.args
        code = a.get('code') if a.get('code') in codes else (codes[0] if codes else None)
        ac = auth_cx()
        try:
            obs = MW.observations(ac, code) if code else []
        finally:
            ac.close()
        periods = [p for p, _v in reversed(obs)]
        values = dict(obs)
        result, error, base_v, new_v = None, None, None, None
        if a.get('amount'):
            try:
                amount = form_float(a.get('amount'))
                fixed = form_float(a.get('fixed') or '15')
                decimals = {'3': 3, '4': 4, 'none': None}.get(a.get('decimals') or '4', 4)
                base_v = form_float(a.get('base_value')) if (a.get('base_value') or '').strip() else values.get(a.get('base'))
                new_v = form_float(a.get('new_value')) if (a.get('new_value') or '').strip() else values.get(a.get('new'))
                if base_v is None or new_v is None:
                    raise MW.MarketWatchError("Choisissez les deux périodes de l'indice (ou saisissez ses valeurs).")
                if amount < 0:
                    raise MW.MarketWatchError('Montant négatif.')
                result = MW.revision(amount, base_v, new_v, fixed, decimals)
            except ValueError:
                error = 'Saisie invalide.'
            except MW.MarketWatchError as exc:
                error = str(exc)
        drafts = []
        if result and can_access('invoicing'):
            c = cx()
            drafts = c.execute("SELECT id, client_name, total, created_at FROM outgoing_invoices WHERE status='draft' AND entity_id IS ? "
                               "ORDER BY id DESC LIMIT 30", (_eid(),)).fetchall()
            c.close()
        return render_template('market_watch_revision.html', codes=codes, code=code, periods=periods, drafts=drafts,
                               labels={p: MW.period_label(p) for p in periods}, args=a, result=result, error=error,
                               base_v=base_v, new_v=new_v,
                               definitions={x: MW.definition(x, settings['custom']) for x in codes})


def register_invoice_link(app):
    @app.post('/veille/revision/facture')
    @login_required
    @requires_active_plan
    def market_watch_revision_invoice():
        from profitos.routes.invoicing import append_draft_line
        f = request.form
        c = cx()
        settings = MW.load_settings(c, _eid())
        code = f.get('code') or ''
        defn = MW.definition(code, settings.get('custom'))
        back = url_for('market_watch_revision', **{k: f.get(k) for k in ('code', 'amount', 'base', 'new', 'fixed', 'decimals',
                                                                            'base_value', 'new_value') if f.get(k)})
        try:
            if not MW.is_price_index(defn):
                raise ValueError('Indice inconnu.')
            ac = auth_cx()
            try:
                values = dict(MW.observations(ac, code))
            finally:
                ac.close()
            base_v = form_float(f.get('base_value')) if (f.get('base_value') or '').strip() else values.get(f.get('base'))
            new_v = form_float(f.get('new_value')) if (f.get('new_value') or '').strip() else values.get(f.get('new'))
            if base_v is None or new_v is None:
                raise ValueError("Choisissez les deux périodes de l'indice.")
            amount = form_float(f.get('amount'))
            decimals = {'3': 3, '4': 4, 'none': None}.get(f.get('decimals') or '4', 4)
            res = MW.revision(amount, base_v, new_v, form_float(f.get('fixed') or '15'), decimals)
            if abs(res['difference']) < 0.01:
                raise ValueError('Révision nulle : aucune ligne à ajouter.')
            vat = form_float(f.get('vat_rate') or '20')
            base_l = MW.period_label(f.get('base')) if f.get('base') and not (f.get('base_value') or '').strip() else f"{base_v:g}"
            new_l = MW.period_label(f.get('new')) if f.get('new') and not (f.get('new_value') or '').strip() else f"{new_v:g}"
            label = (f"Révision de prix — {defn['label']} : {base_l} → {new_l}, coefficient "
                     f"{fr_number(res['coefficient'], 4)} sur {fr_number(amount, 2)} € HT")
            invoice_id = int(f.get('invoice_id') or 0)
            append_draft_line(c, invoice_id, _eid(), label, res['difference'], vat)
        except (ValueError, MW.MarketWatchError) as exc:
            c.close()
            flash(str(exc) or 'Saisie invalide.')
            return redirect(back)
        c.close()
        log_activity('PRICE_REVISION_LINE', f'Ligne de révision ajoutée à la facture brouillon {invoice_id}')
        flash('Ligne de révision ajoutée à la facture brouillon.')
        return redirect(url_for('invoicing_detail', invoice_id=invoice_id))
