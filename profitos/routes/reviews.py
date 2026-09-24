from profitos.runtime import *
from profitos.reviews import create_review, toggle_review_item, review_progress, complete_review


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
        c = cx()
        review = c.execute('SELECT * FROM reviews WHERE id=?', (review_id,)).fetchone()
        if not review:
            c.close(); abort(404)
        items = c.execute(
            'SELECT * FROM review_items WHERE review_id=? ORDER BY item_order', (review_id,)
        ).fetchall()
        c.close()
        done, total = review_progress_from_items(items)
        return render_template('review_detail.html', review=review, items=items, done=done, total=total)

    @app.route('/comptabilite/revision/item/<int:item_id>/toggle', methods=['POST'])
    @login_required
    def review_item_toggle(item_id):
        c = cx()
        item = c.execute('SELECT review_id,checked FROM review_items WHERE id=?', (item_id,)).fetchone()
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
        c = cx()
        try:
            complete_review(c, review_id)
        except ValueError as e:
            c.close()
            flash(f"Révision non terminée : {e}")
            return redirect(url_for('review_detail', review_id=review_id))
        c.close()
        log_activity('REVIEW_COMPLETED', f"Révision #{review_id} terminée")
        flash("Révision marquée comme terminée.")
        return redirect(url_for('reviews_list'))


def review_progress_from_items(items):
    total = len(items)
    done = sum(1 for i in items if i['checked'])
    return done, total
