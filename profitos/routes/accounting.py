from profitos.runtime import *
from profitos.accounting import AccountingError, generate_fec, fec_filename
import io


def register(app):
    @app.route('/comptabilite/plan-comptable', methods=['GET', 'POST'])
    @login_required
    def accounting_chart():
        c = cx()
        error = None
        if request.method == 'POST':
            code = (request.form.get('code') or '').strip()
            label = (request.form.get('label') or '').strip()
            try:
                klass = int((code or '0')[0])
            except (ValueError, IndexError):
                klass = 0
            collective = 1 if request.form.get('is_collective') else 0
            if not (code.isdigit() and 3 <= len(code) <= 8):
                error = "Le numéro de compte doit être composé uniquement de chiffres (3 à 8)."
            elif not label:
                error = "Le libellé est obligatoire."
            elif klass not in range(1, 8):
                error = "Le numéro de compte doit commencer par un chiffre de 1 à 7 (classe PCG)."
            else:
                existing = c.execute(
                    'SELECT code FROM accounting_chart_of_accounts WHERE code=?', (code,)
                ).fetchone()
                if existing:
                    error = f"Le compte {code} existe déjà."
                else:
                    c.execute(
                        'INSERT INTO accounting_chart_of_accounts'
                        '(code,label,account_class,is_collective,is_active,is_default,created_at)'
                        ' VALUES(?,?,?,?,1,0,?)',
                        (code, label, klass, collective, now()),
                    )
                    c.commit()
                    flash(f"Compte {code} — {label} ajouté.")
                    return redirect(url_for('accounting_chart'))

        accounts = c.execute(
            'SELECT * FROM accounting_chart_of_accounts WHERE is_active=1 ORDER BY code'
        ).fetchall()
        by_class = {}
        for a in accounts:
            by_class.setdefault(a['account_class'], []).append(a)
        class_labels = {
            1: 'Classe 1 — Comptes de capitaux', 2: 'Classe 2 — Immobilisations',
            4: 'Classe 4 — Comptes de tiers', 5: 'Classe 5 — Comptes financiers',
            6: 'Classe 6 — Comptes de charges', 7: 'Classe 7 — Comptes de produits',
        }
        return render_template('accounting_chart.html', by_class=by_class,
                                class_labels=class_labels, error=error)

    @app.route('/comptabilite/plan-comptable/<code>/desactiver', methods=['POST'])
    @login_required
    def accounting_chart_deactivate(code):
        c = cx()
        row = c.execute(
            'SELECT is_default FROM accounting_chart_of_accounts WHERE code=?', (code,)
        ).fetchone()
        if row and row['is_default']:
            flash("Un compte du plan comptable par défaut ne peut pas être désactivé.")
        elif row:
            c.execute('UPDATE accounting_chart_of_accounts SET is_active=0 WHERE code=?', (code,))
            c.commit()
            flash(f"Compte {code} désactivé.")
        return redirect(url_for('accounting_chart'))

    @app.route('/comptabilite/journaux', methods=['GET', 'POST'])
    @login_required
    def accounting_journals_view():
        c = cx()
        error = None
        if request.method == 'POST':
            code = (request.form.get('code') or '').strip().upper()
            label = (request.form.get('label') or '').strip()
            jtype = (request.form.get('journal_type') or 'OD').strip()
            if not (code.isalpha() and 1 <= len(code) <= 4):
                error = "Le code journal doit être composé uniquement de lettres (1 à 4)."
            elif not label:
                error = "Le libellé est obligatoire."
            else:
                existing = c.execute(
                    'SELECT code FROM accounting_journals WHERE code=?', (code,)
                ).fetchone()
                if existing:
                    error = f"Le journal {code} existe déjà."
                else:
                    c.execute(
                        'INSERT INTO accounting_journals(code,label,journal_type,is_default,created_at)'
                        ' VALUES(?,?,?,0,?)',
                        (code, label, jtype, now()),
                    )
                    c.commit()
                    flash(f"Journal {code} — {label} créé.")
                    return redirect(url_for('accounting_journals_view'))

        journals = c.execute('SELECT * FROM accounting_journals ORDER BY code').fetchall()
        counts = {r['journal_code']: r['n'] for r in c.execute(
            'SELECT journal_code,COUNT(*) n FROM accounting_entries GROUP BY journal_code'
        ).fetchall()}
        return render_template('accounting_journals.html', journals=journals, counts=counts, error=error)

    @app.route('/comptabilite/journaux/<code>')
    @login_required
    def accounting_journal_detail(code):
        c = cx()
        journal = c.execute('SELECT * FROM accounting_journals WHERE code=?', (code,)).fetchone()
        if not journal:
            c.close(); abort(404)
        entries = c.execute(
            'SELECT * FROM accounting_entries WHERE journal_code=? ORDER BY entry_date DESC, id DESC',
            (code,),
        ).fetchall()
        entry_lines = {}
        for e in entries:
            entry_lines[e['id']] = c.execute(
                'SELECT * FROM accounting_entry_lines WHERE entry_id=? ORDER BY line_order', (e['id'],)
            ).fetchall()
        return render_template('accounting_journal_detail.html', journal=journal,
                                entries=entries, entry_lines=entry_lines)

    @app.route('/comptabilite/lettrage/<account_code>', methods=['GET', 'POST'])
    @login_required
    def accounting_lettrage(account_code):
        c = cx()
        account = c.execute(
            'SELECT * FROM accounting_chart_of_accounts WHERE code=?', (account_code,)
        ).fetchone()
        if not account or not account['is_collective']:
            c.close()
            flash("Le lettrage n'est disponible que pour un compte collectif (411, 401...).")
            return redirect(url_for('accounting_chart'))

        error = None
        if request.method == 'POST':
            selected_ids = [int(x) for x in request.form.getlist('line_id')]
            if len(selected_ids) < 2:
                error = "Sélectionne au moins deux lignes à lettrer ensemble."
            else:
                placeholders = ','.join('?' * len(selected_ids))
                rows = c.execute(
                    f'SELECT l.id,l.debit,l.credit,l.lettrage_code,l.auxiliary_name,e.is_locked '
                    f'FROM accounting_entry_lines l JOIN accounting_entries e ON e.id=l.entry_id '
                    f'WHERE l.id IN ({placeholders}) AND l.account_code=?',
                    (*selected_ids, account_code),
                ).fetchall()
                if len(rows) != len(selected_ids):
                    error = "Sélection invalide."
                elif any(r['is_locked'] for r in rows):
                    error = "Une des lignes appartient à une période comptable clôturée — lettrage impossible."
                elif any(r['lettrage_code'] for r in rows):
                    error = "Une des lignes sélectionnées est déjà lettrée."
                elif len({r['auxiliary_name'] for r in rows}) > 1:
                    error = "Toutes les lignes sélectionnées doivent appartenir au même tiers (client ou fournisseur)."
                else:
                    total = sum(r['debit'] - r['credit'] for r in rows)
                    if abs(total) > 0.01:
                        error = f"Le débit et le crédit ne s'équilibrent pas (écart de {fr_number(abs(total),2)} €)."
                    else:
                        from profitos.accounting import _next_lettrage_code
                        code_letter = _next_lettrage_code(c)
                        for r in rows:
                            c.execute(
                                'UPDATE accounting_entry_lines SET lettrage_code=? WHERE id=?',
                                (code_letter, r['id']),
                            )
                        c.commit()
                        flash(f"{len(rows)} lignes lettrées ({code_letter}).")
                        return redirect(url_for('accounting_lettrage', account_code=account_code))

        lines = c.execute(
            """SELECT l.*, e.entry_date, e.piece_number, e.label AS entry_label, e.is_locked
               FROM accounting_entry_lines l JOIN accounting_entries e ON e.id = l.entry_id
               WHERE l.account_code=? ORDER BY COALESCE(l.auxiliary_name,''), e.entry_date""",
            (account_code,),
        ).fetchall()
        by_tiers = {}
        for l in lines:
            by_tiers.setdefault(l['auxiliary_name'] or '—', []).append(l)
        return render_template('accounting_lettrage.html', account=account,
                                by_tiers=by_tiers, error=error)

    @app.route('/comptabilite/export-fec', methods=['GET', 'POST'])
    @login_required
    def accounting_fec_export():
        c = cx()
        error = None
        if request.method == 'POST':
            date_from = (request.form.get('date_from') or '').strip()
            date_to = (request.form.get('date_to') or '').strip()
            if not date_from or not date_to:
                error = "Les deux dates (début et fin) sont obligatoires."
            elif date_from > date_to:
                error = "La date de début doit précéder la date de fin."
            else:
                company = c.execute('SELECT siret FROM company WHERE id=1').fetchone()
                try:
                    filename = fec_filename(company['siret'] if company else None, date_to)
                except ValueError as e:
                    error = str(e)
                if not error:
                    rows = generate_fec(c, date_from, date_to)
                    c.close()
                    content = '\r\n'.join('|'.join(str(cell) for cell in row) for row in rows)
                    body = content.encode('utf-8')
                    log_activity('FEC_EXPORT', f"Export FEC {date_from} → {date_to} ({len(rows) - 1} ligne(s))")
                    return Response(
                        body, mimetype='text/plain',
                        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
                    )
        n_entries = c.execute('SELECT COUNT(*) n FROM accounting_entries').fetchone()['n']
        settings = c.execute('SELECT accountant_email FROM app_settings WHERE id=1').fetchone()
        c.close()
        return render_template('accounting_fec_export.html', error=error, n_entries=n_entries,
                                accountant_email=settings['accountant_email'] if settings else None)

    @app.route('/comptabilite/export-fec/envoyer-comptable', methods=['POST'])
    @login_required
    def accounting_fec_send_accountant():
        c = cx()
        settings = c.execute('SELECT accountant_email FROM app_settings WHERE id=1').fetchone()
        c.close()
        if not settings or not settings['accountant_email']:
            flash("Aucun email de comptable renseigné — configure-le dans Paramètres.")
            return redirect(url_for('accounting_fec_export'))

        date_from = (request.form.get('date_from') or '').strip()
        date_to = (request.form.get('date_to') or '').strip()
        if not date_from or not date_to or date_from > date_to:
            flash("Dates invalides.")
            return redirect(url_for('accounting_fec_export'))

        org = current_org()
        token = secrets.token_urlsafe(20)
        ac = auth_cx()
        ac.execute(
            'INSERT INTO accounting_fec_tokens(token,organization_id,date_from,date_to,created_at) VALUES(?,?,?,?,?)',
            (token, org['id'], date_from, date_to, now()),
        )
        ac.commit(); ac.close()

        base = os.environ.get('APP_BASE_URL', request.host_url.rstrip('/'))
        link = f"{base}{url_for('accounting_fec_download', token=token)}"
        html = render_template(
            'email_transactional.html', title=f"Export FEC — {org['name']}",
            intro=(f"Voici le lien pour télécharger le Fichier des Écritures Comptables (FEC) de "
                   f"{org['name']} pour la période du {date_from} au {date_to}. Le lien régénère "
                   f"l'export à jour à chaque clic."),
            cta_label='Télécharger le FEC', cta_url=link,
            footer="Ce lien reste valable — contacte l'organisation si tu n'es pas concerné(e).",
        )
        result = send_email(settings['accountant_email'], f"Export FEC — {org['name']}", html)
        if result.get('dry_run'):
            flash(f"Service email non configuré — lien non envoyé réellement (mode simulation) à {settings['accountant_email']}.")
        else:
            log_activity('FEC_SENT_TO_ACCOUNTANT', f"Export FEC {date_from} → {date_to} envoyé à {settings['accountant_email']}")
            flash(f"Lien d'export FEC envoyé à {settings['accountant_email']}.")
        return redirect(url_for('accounting_fec_export'))

    @app.route('/comptabilite/export-fec/telecharger/<token>')
    def accounting_fec_download(token):
        """Téléchargement public authentifié par token — utilisé par le lien envoyé à
        l'expert-comptable. Aucune connexion ProfitOS requise. Régénère le FEC à la
        demande (jamais de fichier stocké)."""
        ac = auth_cx()
        mapping = ac.execute(
            'SELECT * FROM accounting_fec_tokens WHERE token=?', (token,)
        ).fetchone()
        ac.close()
        if not mapping:
            abort(404)
        tc = tenant_cx_direct(mapping['organization_id'])
        company = tc.execute('SELECT siret FROM company WHERE id=1').fetchone()
        try:
            filename = fec_filename(company['siret'] if company else None, mapping['date_to'])
        except ValueError:
            tc.close()
            abort(404)
        rows = generate_fec(tc, mapping['date_from'], mapping['date_to'])
        tc.close()
        content = '\r\n'.join('|'.join(str(cell) for cell in row) for row in rows)
        return Response(
            content.encode('utf-8'), mimetype='text/plain',
            headers={'Content-Disposition': f'attachment; filename="{filename}"'},
        )

    @app.route('/comptabilite/cloture', methods=['GET', 'POST'])
    @login_required
    def accounting_closure():
        c = cx()
        error = None
        if request.method == 'POST':
            closed_until = (request.form.get('closed_until') or '').strip()
            current = c.execute('SELECT closed_until FROM accounting_closure WHERE id=1').fetchone()
            already_closed = current['closed_until'] if current else None
            unbalanced = c.execute(
                """SELECT COUNT(*) n FROM (
                     SELECT entry_id FROM accounting_entry_lines GROUP BY entry_id
                     HAVING ROUND(SUM(debit) - SUM(credit), 2) != 0
                   )"""
            ).fetchone()['n']
            if not closed_until:
                error = "La date de clôture est obligatoire."
            elif already_closed and closed_until <= already_closed:
                error = f"La comptabilité est déjà clôturée jusqu'au {already_closed} — impossible de reculer la clôture."
            elif unbalanced:
                error = "Impossible de clôturer : certaines écritures ne sont pas équilibrées (anomalie à corriger d'abord)."
            else:
                c.execute(
                    'INSERT INTO accounting_closure(id,closed_until,closed_at,closed_by) VALUES(1,?,?,?) '
                    'ON CONFLICT(id) DO UPDATE SET closed_until=excluded.closed_until,'
                    'closed_at=excluded.closed_at,closed_by=excluded.closed_by',
                    (closed_until, now(), session.get('user_id')),
                )
                c.execute(
                    "UPDATE accounting_entries SET is_locked=1 WHERE entry_date<=?", (closed_until,)
                )
                c.commit()
                log_activity('ACCOUNTING_CLOSED', f"Comptabilité clôturée jusqu'au {closed_until}")
                flash(f"Comptabilité clôturée jusqu'au {closed_until}. Les écritures antérieures ou à cette date sont désormais verrouillées.")
                return redirect(url_for('accounting_closure'))

        closure = c.execute('SELECT * FROM accounting_closure WHERE id=1').fetchone()
        locked_count = c.execute('SELECT COUNT(*) n FROM accounting_entries WHERE is_locked=1').fetchone()['n']
        c.close()
        return render_template('accounting_closure.html', closure=closure, locked_count=locked_count, error=error)
