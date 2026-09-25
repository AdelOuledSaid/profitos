from profitos.runtime import *
import uuid


def _cabinet_docs_dir(org_id):
    root = UP / "cabinet_documents" / str(int(org_id))
    root.mkdir(parents=True, exist_ok=True)
    return root


def register(app):
    @app.route('/cabinet/portefeuille')
    @login_required
    def cabinet_portfolio():
        """Vue d'ensemble multi-clients — un comptable/gérant qui appartient
        à plusieurs organisations (memberships) voit ici un résumé de
        chacune : révisions en cours, temps passé ce mois-ci. Ouvre
        brièvement chaque base tenant l'une après l'autre (chaque
        organisation a son propre fichier SQLite séparé — impossible de
        tout lire en une seule requête), toujours refermée immédiatement
        après lecture."""
        orgs = user_organizations()
        uid = session.get('user_id')
        month_start = date.today().replace(day=1).isoformat()

        ac = auth_cx()
        rows = []
        for org in orgs:
            reviews_pending = 0
            reviews_total = 0
            try:
                tc = tenant_cx_direct(org['id'])
                reviews_pending = tc.execute(
                    "SELECT COUNT(*) n FROM reviews WHERE status='in_progress'"
                ).fetchone()['n']
                reviews_total = tc.execute("SELECT COUNT(*) n FROM reviews").fetchone()['n']
                tc.close()
            except Exception:
                # Une organisation dont la base tenant n'est pas encore
                # initialisée (jamais connectée) ne doit jamais faire
                # planter tout le portefeuille — elle apparaît juste sans
                # données de révision.
                pass
            minutes_month = ac.execute(
                "SELECT COALESCE(SUM(duration_minutes),0) t FROM cabinet_time_entries "
                "WHERE organization_id=? AND entry_date>=?",
                (org['id'], month_start),
            ).fetchone()['t']
            rows.append({
                'org': org, 'reviews_pending': reviews_pending, 'reviews_total': reviews_total,
                'minutes_month': minutes_month,
            })
        ac.close()
        return render_template('cabinet_portfolio.html', rows=rows, month_label=date.today().strftime('%B %Y'))

    @app.route('/cabinet/temps', methods=['GET', 'POST'])
    @login_required
    def cabinet_time_new():
        orgs = user_organizations()
        if request.method == 'POST':
            org_id_raw = request.form.get('organization_id')
            try:
                org_id = int(org_id_raw)
            except (TypeError, ValueError):
                flash("Client invalide.")
                return redirect(url_for('cabinet_time_new'))
            if org_id not in {o['id'] for o in orgs}:
                flash("Tu n'as pas accès à ce client.")
                return redirect(url_for('cabinet_time_new'))
            entry_date = request.form.get('entry_date') or date.today().isoformat()
            try:
                duration_minutes = int(request.form.get('duration_minutes') or 0)
            except ValueError:
                duration_minutes = 0
            if duration_minutes <= 0:
                flash("La durée doit être positive (en minutes).")
                return redirect(url_for('cabinet_time_new'))
            description = (request.form.get('description') or '').strip()
            billable = 1 if request.form.get('billable') == 'on' else 0

            ac = auth_cx()
            ac.execute(
                """INSERT INTO cabinet_time_entries
                   (user_id,organization_id,entry_date,duration_minutes,description,billable,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (session.get('user_id'), org_id, entry_date, duration_minutes, description, billable, now()),
            )
            ac.commit(); ac.close()
            flash("Temps enregistré.")
            return redirect(url_for('cabinet_portfolio'))

        return render_template('cabinet_time_new.html', orgs=orgs, today=date.today().isoformat())

    @app.route('/cabinet/temps/<int:organization_id>')
    @login_required
    def cabinet_time_list(organization_id):
        orgs = user_organizations()
        if organization_id not in {o['id'] for o in orgs}:
            abort(404)
        org = next(o for o in orgs if o['id'] == organization_id)
        ac = auth_cx()
        entries = ac.execute(
            """SELECT cte.*, u.full_name, u.email FROM cabinet_time_entries cte
               JOIN users u ON u.id=cte.user_id
               WHERE cte.organization_id=? ORDER BY cte.entry_date DESC, cte.id DESC""",
            (organization_id,),
        ).fetchall()
        total_minutes = sum(e['duration_minutes'] for e in entries)
        ac.close()
        return render_template('cabinet_time_list.html', org=org, entries=entries, total_minutes=total_minutes)

    @app.route('/cabinet/documents/<int:organization_id>', methods=['GET', 'POST'])
    @login_required
    def cabinet_documents(organization_id):
        orgs = user_organizations()
        if organization_id not in {o['id'] for o in orgs}:
            abort(404)
        org = next(o for o in orgs if o['id'] == organization_id)

        if request.method == 'POST':
            uploaded = request.files.get('file')
            category = (request.form.get('category') or 'Autre').strip()
            if not uploaded or not uploaded.filename:
                flash("Aucun fichier sélectionné.")
                return redirect(url_for('cabinet_documents', organization_id=organization_id))
            ext = os.path.splitext(uploaded.filename)[1][:10]
            stored_name = uuid.uuid4().hex + ext
            dest = _cabinet_docs_dir(organization_id) / stored_name
            uploaded.save(str(dest))
            ac = auth_cx()
            ac.execute(
                """INSERT INTO cabinet_documents(organization_id,uploaded_by,filename,category,stored_name,uploaded_at)
                   VALUES(?,?,?,?,?,?)""",
                (organization_id, session.get('user_id'), uploaded.filename, category, stored_name, now()),
            )
            ac.commit(); ac.close()
            flash("Document ajouté.")
            return redirect(url_for('cabinet_documents', organization_id=organization_id))

        ac = auth_cx()
        docs = ac.execute(
            """SELECT cd.*, u.full_name, u.email FROM cabinet_documents cd
               LEFT JOIN users u ON u.id=cd.uploaded_by
               WHERE cd.organization_id=? ORDER BY cd.uploaded_at DESC""",
            (organization_id,),
        ).fetchall()
        ac.close()
        return render_template('cabinet_documents.html', org=org, docs=docs)

    @app.route('/cabinet/documents/<int:organization_id>/<int:doc_id>/telecharger')
    @login_required
    def cabinet_document_download(organization_id, doc_id):
        orgs = user_organizations()
        if organization_id not in {o['id'] for o in orgs}:
            abort(404)
        ac = auth_cx()
        doc = ac.execute(
            'SELECT * FROM cabinet_documents WHERE id=? AND organization_id=?', (doc_id, organization_id)
        ).fetchone()
        ac.close()
        if not doc:
            abort(404)
        path = _cabinet_docs_dir(organization_id) / doc['stored_name']
        if not path.is_file():
            abort(404)
        return Response(
            path.read_bytes(), mimetype='application/octet-stream',
            headers={'Content-Disposition': f'attachment; filename="{doc["filename"]}"'},
        )
