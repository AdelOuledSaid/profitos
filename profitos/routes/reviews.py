from profitos.runtime import *
from profitos.reviews import create_review, toggle_review_item, review_progress, complete_review, run_review_diagnostics, latest_review_diagnostics


def register(app):
    @app.route('/comptabilite/revision')
    @login_required
    def reviews_list():
        from profitos.entities import current_entity_id
        eid = current_entity_id()
        ef = 'entity_id=?' if eid else 'entity_id IS NULL'
        ep = (eid,) if eid else ()
        c = cx()
        reviews = c.execute(f"SELECT * FROM reviews WHERE {ef} ORDER BY created_at DESC", ep).fetchall()
        progress = {r['id']: review_progress(c, r['id']) for r in reviews}
        c.close()
        return render_template('reviews_list.html', reviews=reviews, progress=progress,
                                today_year=date.today().year, today_month=date.today().strftime('%Y-%m'))

    @app.route('/comptabilite/revision/nouvelle', methods=['POST'])
    @login_required
    def review_new():
        from profitos.entities import current_entity_id
        review_type = request.form.get('review_type')
        period_label = (request.form.get('period_label') or '').strip()
        if not period_label:
            flash("La période est obligatoire.")
            return redirect(url_for('reviews_list'))
        c = cx()
        try:
            review_id = create_review(c, review_type, period_label,
                                       entity_id=current_entity_id(), created_by=current_user()['email'])
        except ValueError as e:
            c.close()
            flash(str(e))
            return redirect(url_for('reviews_list'))
        c.close()
        log_activity('REVIEW_CREATED', f"Révision {review_type} créée : {period_label}")
        return redirect(url_for('review_detail', review_id=review_id))

    @app.route('/comptabilite/revision/<int:review_id>')
    @login_required
    def review_detail(review_id):
        from profitos.entities import current_entity_id
        eid = current_entity_id()
        ef = 'entity_id=?' if eid else 'entity_id IS NULL'
        ep = (review_id, eid) if eid else (review_id,)
        c = cx()
        review = c.execute(f'SELECT * FROM reviews WHERE id=? AND {ef}', ep).fetchone()
        if not review:
            c.close(); abort(404)
        items = c.execute(
            'SELECT * FROM review_items WHERE review_id=? ORDER BY item_order', (review_id,)
        ).fetchall()
        diag_run, diag_issues = latest_review_diagnostics(c, review_id)
        c.close()
        done, total = review_progress_from_items(items)
        return render_template('review_detail.html', review=review, items=items, done=done, total=total,
                               diag_run=diag_run, diag_issues=diag_issues)

    @app.route('/comptabilite/revision/<int:review_id>/diagnostic', methods=['POST'])
    @login_required
    def review_diagnostic_run(review_id):
        from profitos.entities import current_entity_id
        eid = current_entity_id()
        ef = 'entity_id=?' if eid else 'entity_id IS NULL'
        ep = (review_id, eid) if eid else (review_id,)
        c = cx()
        review = c.execute(f'SELECT id FROM reviews WHERE id=? AND {ef}', ep).fetchone()
        if not review:
            c.close(); abort(404)
        run, issues = run_review_diagnostics(c, review_id, entity_id=eid, run_by=current_user()['email'])
        c.close()
        log_activity('REVIEW_DIAGNOSTIC_RUN', f"Révision #{review_id}: {run['blocker_count']} blocage(s), {run['warning_count']} alerte(s)")
        flash(f"Diagnostic actualisé : {run['blocker_count']} blocage(s), {run['warning_count']} alerte(s).")
        return redirect(url_for('review_detail', review_id=review_id))

    @app.route('/comptabilite/revision/item/<int:item_id>/toggle', methods=['POST'])
    @login_required
    def review_item_toggle(item_id):
        from profitos.entities import current_entity_id
        eid = current_entity_id()
        ef = 'r.entity_id=?' if eid else 'r.entity_id IS NULL'
        ep = (item_id, eid) if eid else (item_id,)
        c = cx()
        item = c.execute(
            f'''SELECT i.review_id,i.checked FROM review_items i
                JOIN reviews r ON r.id=i.review_id WHERE i.id=? AND {ef}''', ep
        ).fetchone()
        if not item:
            c.close(); abort(404)
        new_checked = not item['checked']
        note = (request.form.get('note') or '').strip() or None
        toggle_review_item(c, item_id, new_checked, note=note,
                            checked_by=current_user()['email'] if new_checked else None)
        c.close()
        return redirect(url_for('review_detail', review_id=item['review_id']))

    @app.route('/comptabilite/revision/<int:review_id>/terminer', methods=['POST'])
    @login_required
    def review_complete(review_id):
        from profitos.entities import current_entity_id
        eid = current_entity_id()
        ef = 'entity_id=?' if eid else 'entity_id IS NULL'
        ep = (review_id, eid) if eid else (review_id,)
        c = cx()
        review = c.execute(f'SELECT id FROM reviews WHERE id=? AND {ef}', ep).fetchone()
        if not review:
            c.close(); abort(404)
        diag_run, diag_issues = run_review_diagnostics(c, review_id, entity_id=eid, run_by=current_user()['email'])
        try:
            complete_review(c, review_id, blocker_count=diag_run['blocker_count'])
        except ValueError as e:
            c.close()
            flash(f"Révision non terminée : {e}")
            return redirect(url_for('review_detail', review_id=review_id))
        c.close()
        log_activity('REVIEW_COMPLETED', f"Révision #{review_id} terminée")
        flash("Révision marquée comme terminée.")
        return redirect(url_for('reviews_list'))


    def _collab_entity_clause(alias=''):
        from profitos.entities import current_entity_id
        eid = current_entity_id()
        col = f"{alias}.entity_id" if alias else "entity_id"
        return eid, (f"{col}=?" if eid else f"{col} IS NULL")

    def _collab_event(c, collaboration_id, eid, event_type, detail=None):
        c.execute("""INSERT INTO accountant_activity
                     (collaboration_id,entity_id,event_type,detail,created_at,created_by)
                     VALUES(?,?,?,?,?,?)""",
                  (collaboration_id,eid,event_type,detail,now_iso(),current_user()['email']))

    @app.route('/comptabilite/revision/<int:review_id>/cabinet')
    @login_required
    def accountant_collaboration(review_id):
        eid, ef = _collab_entity_clause('r')
        params = (review_id,eid) if eid else (review_id,)
        c=cx()
        review=c.execute(f"SELECT r.* FROM reviews r WHERE r.id=? AND {ef}",params).fetchone()
        if not review:
            c.close(); abort(404)
        ce = "entity_id=?" if eid else "entity_id IS NULL"
        cp = (review_id,eid) if eid else (review_id,)
        collab=c.execute(f"""SELECT * FROM accountant_collaborations
                             WHERE review_id=? AND {ce}
                             ORDER BY id DESC LIMIT 1""",cp).fetchone()
        requests=[]; comments={}; activity=[]
        if collab:
            rp=(collab['id'],eid) if eid else (collab['id'],)
            requests=c.execute(f"""SELECT * FROM accountant_requests
                                   WHERE collaboration_id=? AND {ce}
                                   ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END,id DESC""",rp).fetchall()
            for req in requests:
                qp=(req['id'],eid) if eid else (req['id'],)
                comments[req['id']]=c.execute(
                    f"SELECT * FROM accountant_request_comments WHERE request_id=? AND {ce} ORDER BY id",qp
                ).fetchall()
            activity=c.execute(f"""SELECT * FROM accountant_activity
                                   WHERE collaboration_id=? AND {ce} ORDER BY id DESC LIMIT 100""",rp).fetchall()
        c.close()
        return render_template('accountant_collaboration.html',review=review,collab=collab,
                               requests=requests,comments=comments,activity=activity)

    @app.route('/comptabilite/revision/<int:review_id>/cabinet/activer',methods=['POST'])
    @login_required
    def accountant_collaboration_create(review_id):
        eid, ef = _collab_entity_clause('r')
        params=(review_id,eid) if eid else (review_id,)
        c=cx()
        review=c.execute(f"SELECT r.id FROM reviews r WHERE r.id=? AND {ef}",params).fetchone()
        if not review:
            c.close(); abort(404)
        accountant_email=(request.form.get('accountant_email') or '').strip().lower() or None
        firm_name=(request.form.get('firm_name') or '').strip() or None
        ce="entity_id=?" if eid else "entity_id IS NULL"
        cp=(review_id,eid) if eid else (review_id,)
        existing=c.execute(f"""SELECT * FROM accountant_collaborations
                               WHERE review_id=? AND {ce} AND status='active'
                               ORDER BY id DESC LIMIT 1""",cp).fetchone()
        if existing:
            c.close(); flash("Une collaboration cabinet est déjà active pour cette révision.")
            return redirect(url_for('accountant_collaboration',review_id=review_id))
        cur=c.execute("""INSERT INTO accountant_collaborations
                         (entity_id,review_id,accountant_email,firm_name,status,created_at,created_by)
                         VALUES(?,?,?,?, 'active',?,?)""",
                      (eid,review_id,accountant_email,firm_name,now_iso(),current_user()['email']))
        collab_id=cur.lastrowid
        _collab_event(c,collab_id,eid,'COLLABORATION_CREATED',
                      f"{firm_name or 'Cabinet'} · {accountant_email or 'contact non renseigné'}")
        c.commit(); c.close()
        flash("Collaboration cabinet activée.")
        return redirect(url_for('accountant_collaboration',review_id=review_id))

    @app.route('/comptabilite/revision/<int:review_id>/cabinet/demande',methods=['POST'])
    @login_required
    def accountant_request_create(review_id):
        eid, ef = _collab_entity_clause('c')
        c=cx()
        params=(review_id,eid) if eid else (review_id,)
        collab=c.execute(f"""SELECT c.* FROM accountant_collaborations c
                             WHERE c.review_id=? AND {ef} AND c.status='active'
                             ORDER BY c.id DESC LIMIT 1""",params).fetchone()
        if not collab:
            c.close(); abort(404)
        title=(request.form.get('title') or '').strip()
        if not title:
            c.close(); flash("Le titre de la demande est obligatoire.")
            return redirect(url_for('accountant_collaboration',review_id=review_id))
        description=(request.form.get('description') or '').strip() or None
        due_date=(request.form.get('due_date') or '').strip() or None
        cur=c.execute("""INSERT INTO accountant_requests
                         (collaboration_id,entity_id,title,description,due_date,status,created_at,created_by)
                         VALUES(?,?,?,?,?,'open',?,?)""",
                      (collab['id'],eid,title,description,due_date,now_iso(),current_user()['email']))
        _collab_event(c,collab['id'],eid,'REQUEST_CREATED',f"Demande #{cur.lastrowid}: {title}")
        c.commit(); c.close()
        flash("Demande créée.")
        return redirect(url_for('accountant_collaboration',review_id=review_id))

    @app.route('/comptabilite/revision/<int:review_id>/cabinet/demande/<int:request_id>/commenter',methods=['POST'])
    @login_required
    def accountant_request_comment(review_id,request_id):
        eid, ef = _collab_entity_clause('q')
        c=cx()
        params=(request_id,review_id,eid) if eid else (request_id,review_id)
        req=c.execute(f"""SELECT q.*,c.status AS collaboration_status FROM accountant_requests q
                          JOIN accountant_collaborations c ON c.id=q.collaboration_id
                          WHERE q.id=? AND c.review_id=? AND {ef}""",params).fetchone()
        if not req:
            c.close(); abort(404)
        if req['collaboration_status']!='active':
            c.close(); flash("Cette collaboration est clôturée.")
            return redirect(url_for('accountant_collaboration',review_id=review_id))
        if req['status']=='resolved':
            c.close(); flash("Rouvrez la demande avant d'ajouter un nouveau commentaire.")
            return redirect(url_for('accountant_collaboration',review_id=review_id))
        body=(request.form.get('body') or '').strip()
        if not body:
            c.close(); flash("Le commentaire est vide.")
            return redirect(url_for('accountant_collaboration',review_id=review_id))
        c.execute("""INSERT INTO accountant_request_comments
                     (request_id,entity_id,body,created_at,created_by) VALUES(?,?,?,?,?)""",
                  (request_id,eid,body,now_iso(),current_user()['email']))
        _collab_event(c,req['collaboration_id'],eid,'COMMENT_ADDED',f"Demande #{request_id}")
        c.commit(); c.close()
        return redirect(url_for('accountant_collaboration',review_id=review_id))

    @app.route('/comptabilite/revision/<int:review_id>/cabinet/demande/<int:request_id>/statut',methods=['POST'])
    @login_required
    def accountant_request_status(review_id,request_id):
        eid, ef = _collab_entity_clause('q')
        c=cx()
        params=(request_id,review_id,eid) if eid else (request_id,review_id)
        req=c.execute(f"""SELECT q.*,c.status AS collaboration_status FROM accountant_requests q
                          JOIN accountant_collaborations c ON c.id=q.collaboration_id
                          WHERE q.id=? AND c.review_id=? AND {ef}""",params).fetchone()
        if not req:
            c.close(); abort(404)
        if req['collaboration_status']!='active':
            c.close(); flash("Cette collaboration est clôturée.")
            return redirect(url_for('accountant_collaboration',review_id=review_id))
        target=(request.form.get('status') or '').strip()
        if target not in ('open','resolved'):
            c.close(); abort(400)
        if target=='resolved':
            c.execute("""UPDATE accountant_requests SET status='resolved',resolved_at=?,resolved_by=?
                         WHERE id=?""",(now_iso(),current_user()['email'],request_id))
            event='REQUEST_RESOLVED'
        else:
            c.execute("""UPDATE accountant_requests SET status='open',resolved_at=NULL,resolved_by=NULL
                         WHERE id=?""",(request_id,))
            event='REQUEST_REOPENED'
        _collab_event(c,req['collaboration_id'],eid,event,f"Demande #{request_id}: {req['title']}")
        c.commit(); c.close()
        return redirect(url_for('accountant_collaboration',review_id=review_id))

    @app.route('/comptabilite/revision/<int:review_id>/cabinet/cloturer',methods=['POST'])
    @login_required
    def accountant_collaboration_close(review_id):
        eid, ef = _collab_entity_clause('c')
        c=cx()
        params=(review_id,eid) if eid else (review_id,)
        collab=c.execute(f"""SELECT c.* FROM accountant_collaborations c
                             WHERE c.review_id=? AND {ef} AND c.status='active'
                             ORDER BY c.id DESC LIMIT 1""",params).fetchone()
        if not collab:
            c.close(); abort(404)
        _collab_event(c,collab['id'],eid,'COLLABORATION_CLOSED','Collaboration clôturée')
        c.execute("UPDATE accountant_collaborations SET status='closed',closed_at=? WHERE id=?",
                  (now_iso(),collab['id']))
        c.commit(); c.close()
        flash("Collaboration cabinet clôturée.")
        return redirect(url_for('accountant_collaboration',review_id=review_id))



def review_progress_from_items(items):
    total = len(items)
    done = sum(1 for i in items if i['checked'])
    return done, total
