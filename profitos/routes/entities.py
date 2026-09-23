from profitos.runtime import *
from profitos.entities import resolve_entity, list_all_entities
from profitos.sepa import validate_iban


def register(app):
    @app.route('/entites/basculer', methods=['POST'])
    @login_required
    def entity_switch():
        from profitos.entities import user_can_access_entity
        c = cx()
        entity_id_raw = request.form.get('entity_id')
        entity_id = int(entity_id_raw) if entity_id_raw and entity_id_raw.isdigit() else None
        try:
            identity = resolve_entity(c, entity_id)
        except ValueError:
            c.close()
            flash("Entité introuvable.")
            return redirect(url_for('home'))
        if not user_can_access_entity(c, session.get('user_id'), entity_id):
            c.close()
            flash("Tu n'as pas accès à cette entité.")
            return redirect(url_for('home'))
        c.close()
        if entity_id:
            session['current_entity_id'] = entity_id
        else:
            session.pop('current_entity_id', None)
        flash(f"Entité active : {identity['name']}.")
        target = None
        if request.referrer:
            parsed = urlsplit(request.referrer)
            if parsed.netloc == request.host:
                target = safe_next_url(parsed.path + (f"?{parsed.query}" if parsed.query else ''))
        return redirect(target or url_for('home'))

    @app.route('/entites')
    @login_required
    @require_area('settings')
    def entities_list():
        c = cx()
        entities = list_all_entities(c)
        c.close()
        return render_template('entities_list.html', entities=entities)

    @app.route('/entites/nouvelle', methods=['GET', 'POST'])
    @login_required
    @require_area('settings')
    def entity_new():
        if request.method == 'POST':
            name = (request.form.get('name') or '').strip()
            if not name:
                flash("Le nom de l'entité est obligatoire.")
                return redirect(url_for('entity_new'))
            iban = (request.form.get('iban') or '').replace(' ', '').upper().strip()
            if iban and not validate_iban(iban):
                flash("IBAN invalide — vérifie la saisie.")
                return redirect(url_for('entity_new'))
            c = cx()
            c.execute(
                """INSERT INTO entities(name,siret,vat_number,address,postal_code,iban,bic,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (name, (request.form.get('siret') or '').strip(), (request.form.get('vat_number') or '').strip(),
                 (request.form.get('address') or '').strip(), (request.form.get('postal_code') or '').strip(),
                 iban, (request.form.get('bic') or '').upper().strip(), now()),
            )
            c.commit()
            new_id = c.execute('SELECT last_insert_rowid()').fetchone()[0]
            c.close()
            log_activity('ENTITY_CREATED', f"Entité « {name} » créée")
            flash(f"Entité « {name} » créée.")
            return redirect(url_for('entity_detail', entity_id=new_id))
        return render_template('entity_new.html')

    @app.route('/entites/<int:entity_id>', methods=['GET', 'POST'])
    @login_required
    @require_area('settings')
    def entity_detail(entity_id):
        c = cx()
        entity_row = c.execute('SELECT * FROM entities WHERE id=?', (entity_id,)).fetchone()
        if not entity_row:
            c.close(); abort(404)
        error = None
        if request.method == 'POST':
            iban = (request.form.get('iban') or '').replace(' ', '').upper().strip()
            if iban and not validate_iban(iban):
                error = "IBAN invalide — vérifie la saisie."
            else:
                c.execute(
                    "UPDATE entities SET name=?,siret=?,vat_number=?,address=?,postal_code=?,iban=?,bic=? WHERE id=?",
                    ((request.form.get('name') or entity_row['name']).strip(),
                     (request.form.get('siret') or '').strip(), (request.form.get('vat_number') or '').strip(),
                     (request.form.get('address') or '').strip(), (request.form.get('postal_code') or '').strip(),
                     iban, (request.form.get('bic') or '').upper().strip(), entity_id),
                )
                c.commit()
                flash("Entité mise à jour.")
                return redirect(url_for('entity_detail', entity_id=entity_id))
        entity_row = c.execute('SELECT * FROM entities WHERE id=?', (entity_id,)).fetchone()

        invoice_count = c.execute('SELECT COUNT(*) n FROM outgoing_invoices WHERE entity_id=?', (entity_id,)).fetchone()['n']
        purchase_count = c.execute('SELECT COUNT(*) n FROM purchase_invoices WHERE entity_id=?', (entity_id,)).fetchone()['n']
        c.close()
        return render_template('entity_detail.html', entity=entity_row, error=error,
                                invoice_count=invoice_count, purchase_count=purchase_count)

    @app.route('/entites/consolide')
    @login_required
    @require_area('settings')
    def entities_consolidated():
        c = cx()
        entities = list_all_entities(c)
        rows = []
        grand_total_sales = 0.0
        grand_total_purchases = 0.0
        for e in entities:
            eid = e['id']
            sales = c.execute(
                "SELECT COALESCE(SUM(total),0) t FROM outgoing_invoices WHERE (entity_id=? OR (? IS NULL AND entity_id IS NULL))",
                (eid, eid),
            ).fetchone()['t']
            purchases = c.execute(
                "SELECT COALESCE(SUM(total),0) t FROM purchase_invoices WHERE (entity_id=? OR (? IS NULL AND entity_id IS NULL))",
                (eid, eid),
            ).fetchone()['t']
            rows.append({'entity': e, 'sales': sales, 'purchases': purchases})
            grand_total_sales += sales
            grand_total_purchases += purchases
        c.close()
        return render_template('entities_consolidated.html', rows=rows,
                                grand_total_sales=grand_total_sales, grand_total_purchases=grand_total_purchases)
