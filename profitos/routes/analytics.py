from profitos.runtime import *


def register(app):
    @app.route('/comptabilite/analytique', methods=['GET', 'POST'])
    @login_required
    def analytical_axes_list():
        c = cx()
        error = None
        if request.method == 'POST':
            action = request.form.get('action')
            if action == 'new_axis':
                name = (request.form.get('axis_name') or '').strip()
                if not name:
                    error = "Le nom de l'axe est obligatoire."
                else:
                    existing = c.execute('SELECT id FROM analytical_axes WHERE name=?', (name,)).fetchone()
                    if existing:
                        error = f"Un axe « {name} » existe déjà."
                    else:
                        c.execute('INSERT INTO analytical_axes(name,created_at) VALUES(?,?)', (name, now()))
                        c.commit()
                        flash(f"Axe « {name} » créé.")
                        return redirect(url_for('analytical_axes_list'))
            elif action == 'new_tag':
                axis_id = request.form.get('axis_id')
                tag_name = (request.form.get('tag_name') or '').strip()
                if not axis_id or not tag_name:
                    error = "L'axe et le nom du tag sont obligatoires."
                else:
                    existing = c.execute(
                        'SELECT id FROM analytical_tags WHERE axis_id=? AND name=?', (axis_id, tag_name)
                    ).fetchone()
                    if existing:
                        error = f"Le tag « {tag_name} » existe déjà sur cet axe."
                    else:
                        c.execute(
                            'INSERT INTO analytical_tags(axis_id,name,created_at) VALUES(?,?,?)',
                            (axis_id, tag_name, now()),
                        )
                        c.commit()
                        flash(f"Tag « {tag_name} » créé.")
                        return redirect(url_for('analytical_axes_list'))

        axes = c.execute('SELECT * FROM analytical_axes ORDER BY name').fetchall()
        tags_by_axis = {}
        for a in axes:
            tags_by_axis[a['id']] = c.execute(
                'SELECT * FROM analytical_tags WHERE axis_id=? ORDER BY name', (a['id'],)
            ).fetchall()
        c.close()
        return render_template('analytical_axes_list.html', axes=axes, tags_by_axis=tags_by_axis, error=error)

    @app.route('/comptabilite/analytique/tag/<int:tag_id>/supprimer', methods=['POST'])
    @login_required
    def analytical_tag_delete(tag_id):
        c = cx()
        c.execute('UPDATE accounting_entry_lines SET analytical_tag_id=NULL WHERE analytical_tag_id=?', (tag_id,))
        c.execute('DELETE FROM analytical_tags WHERE id=?', (tag_id,))
        c.commit(); c.close()
        flash("Tag supprimé — les lignes déjà taguées ont été détaguées, jamais supprimées.")
        return redirect(url_for('analytical_axes_list'))

    @app.route('/comptabilite/journaux/ligne/<int:line_id>/tag', methods=['POST'])
    @login_required
    def analytical_tag_line(line_id):
        c = cx()
        line = c.execute('SELECT entry_id FROM accounting_entry_lines WHERE id=?', (line_id,)).fetchone()
        if not line:
            c.close(); abort(404)
        tag_id_raw = request.form.get('analytical_tag_id')
        tag_id = int(tag_id_raw) if tag_id_raw and tag_id_raw.isdigit() else None
        c.execute('UPDATE accounting_entry_lines SET analytical_tag_id=? WHERE id=?', (tag_id, line_id))
        c.commit()
        entry_row = c.execute('SELECT journal_code FROM accounting_entries WHERE id=?', (line['entry_id'],)).fetchone()
        c.close()
        return redirect(url_for('accounting_journal_detail', code=entry_row['journal_code']))

    @app.route('/comptabilite/analytique/rentabilite')
    @login_required
    def analytical_profitability():
        c = cx()
        axes = c.execute('SELECT * FROM analytical_axes ORDER BY name').fetchall()
        axis_id_raw = request.args.get('axis_id')
        axis_id = int(axis_id_raw) if axis_id_raw and axis_id_raw.isdigit() else (axes[0]['id'] if axes else None)
        date_from = request.args.get('date_from') or f"{date.today().year}-01-01"
        date_to = request.args.get('date_to') or date.today().isoformat()

        rows = []
        if axis_id:
            tags = c.execute('SELECT * FROM analytical_tags WHERE axis_id=? ORDER BY name', (axis_id,)).fetchall()
            for tg in tags:
                revenue = c.execute(
                    """SELECT COALESCE(SUM(l.credit),0) t FROM accounting_entry_lines l
                       JOIN accounting_entries e ON e.id=l.entry_id
                       JOIN accounting_chart_of_accounts a ON a.code=l.account_code
                       WHERE l.analytical_tag_id=? AND a.account_class=7 AND e.entry_date BETWEEN ? AND ?""",
                    (tg['id'], date_from, date_to),
                ).fetchone()['t']
                expense = c.execute(
                    """SELECT COALESCE(SUM(l.debit),0) t FROM accounting_entry_lines l
                       JOIN accounting_entries e ON e.id=l.entry_id
                       JOIN accounting_chart_of_accounts a ON a.code=l.account_code
                       WHERE l.analytical_tag_id=? AND a.account_class=6 AND e.entry_date BETWEEN ? AND ?""",
                    (tg['id'], date_from, date_to),
                ).fetchone()['t']
                rows.append({'tag': tg, 'revenue': revenue, 'expense': expense, 'margin': revenue - expense})
            untagged_revenue = c.execute(
                """SELECT COALESCE(SUM(l.credit),0) t FROM accounting_entry_lines l
                   JOIN accounting_entries e ON e.id=l.entry_id
                   JOIN accounting_chart_of_accounts a ON a.code=l.account_code
                   WHERE l.analytical_tag_id IS NULL AND a.account_class=7 AND e.entry_date BETWEEN ? AND ?""",
                (date_from, date_to),
            ).fetchone()['t']
            untagged_expense = c.execute(
                """SELECT COALESCE(SUM(l.debit),0) t FROM accounting_entry_lines l
                   JOIN accounting_entries e ON e.id=l.entry_id
                   JOIN accounting_chart_of_accounts a ON a.code=l.account_code
                   WHERE l.analytical_tag_id IS NULL AND a.account_class=6 AND e.entry_date BETWEEN ? AND ?""",
                (date_from, date_to),
            ).fetchone()['t']
        else:
            untagged_revenue = untagged_expense = 0
        c.close()
        return render_template('analytical_profitability.html', axes=axes, axis_id=axis_id, rows=rows,
                                date_from=date_from, date_to=date_to,
                                untagged_revenue=untagged_revenue, untagged_expense=untagged_expense)
