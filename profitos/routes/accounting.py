from profitos.runtime import *
from profitos.accounting import AccountingError


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
