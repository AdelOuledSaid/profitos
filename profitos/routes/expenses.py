import base64
import uuid
from datetime import date
from pathlib import Path

from pypdf.errors import PyPdfError

from profitos.runtime import *
from profitos.feature_access import requires_paid_plan
from profitos.accounting import AccountingError
from profitos.expenses import (
    DEFAULT_EXPENSE_REPORT_CATEGORIES, EXPENSE_REPORT_CATEGORY_LABELS,
    MILEAGE_FISCAL_POWERS, MILEAGE_BRACKETS, MILEAGE_BRACKET_LABELS,
    compute_mileage_allowance, MileageRateNotConfigured,
    generate_expense_report_entry, generate_expense_reimbursement_entry,
)

_RECEIPT_MAX_BYTES = 5 * 1024 * 1024


def _receipt_dir():
    org_id = session.get('org_id')
    if not org_id:
        raise RuntimeError("Organisation non sélectionnée")
    root = UP / "expense_receipts" / str(int(org_id))
    root.mkdir(parents=True, exist_ok=True)
    return root


def _receipt_pdf_extract(path):
    """Extraction texte native d'un reçu/ticket : plus permissive qu'une
    facture formelle (les reçus n'ont pas de structure standardisée).
    Cherche surtout un montant total et une date."""
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    text = '\n'.join((p.extract_text() or '') for p in reader.pages[:3]).strip()
    if len(text) < 10:
        raise ValueError("PDF sans texte exploitable (probablement scanné).")
    amount_match = re.search(
        r'(?:total\s*ttc|montant\s*total|total\s*\u00e0\s*payer|total)\s*[:\-]?\s*([0-9][0-9\s.,]*\s*\u20ac?)',
        text, re.I,
    )
    date_match = re.search(r'(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})', text)
    if not amount_match:
        raise ValueError("Montant introuvable dans le texte du reçu.")
    raw = re.sub(r'[^\d,.\-]', '', amount_match.group(1))
    # Gère les deux conventions (1 234,56 ou 1,234.56) en repérant le dernier séparateur comme décimal.
    if ',' in raw and '.' in raw:
        raw = raw.replace(',', '') if raw.rfind('.') > raw.rfind(',') else raw.replace('.', '').replace(',', '.')
    elif ',' in raw:
        raw = raw.replace(',', '.')
    try:
        amount = float(raw)
    except ValueError:
        raise ValueError("Montant illisible dans le texte du reçu.")
    expense_date = ''
    if date_match:
        d, m, y = date_match.groups()
        y = ('20' + y) if len(y) == 2 else y
        try:
            expense_date = date(int(y), int(m), int(d)).isoformat()
        except ValueError:
            expense_date = ''
    return {'amount': amount, 'expense_date': expense_date, 'description': text.splitlines()[0][:80] if text else ''}


def _receipt_ai_extract(file_bytes, mime_type):
    """Repli IA (API Claude, vision) pour un reçu scanné sans texte
    exploitable. Retourne un dict {amount, expense_date, description,
    category}. Lève ValueError si la clé API n'est pas configurée, si
    l'appel échoue, ou si le montant est manquant — jamais de valeur
    inventée."""
    if not ANTHROPIC_API_KEY:
        raise ValueError(
            "Ce reçu n'a pas de texte exploitable et l'extraction par IA n'est "
            "pas configurée côté serveur."
        )
    b64 = base64.b64encode(file_bytes).decode('utf-8')
    prompt = (
        "Tu analyses un ticket de caisse ou reçu de dépense professionnelle "
        "fourni en pièce jointe. Réponds UNIQUEMENT avec un objet JSON, sans "
        "texte avant ou après, exactement sous cette forme :\n"
        '{"amount": nombre_TTC, "expense_date": "AAAA-MM-JJ ou vide", '
        '"description": "nom du commerce ou description courte", '
        '"category": "repas, transport, hebergement, fournitures ou autre"}\n'
        "Si un champ est illisible ou absent, mets une chaîne vide (sauf amount, "
        "obligatoire). N'invente jamais une valeur que tu ne peux pas lire "
        "réellement sur le document."
    )
    headers = {
        'x-api-key': ANTHROPIC_API_KEY,
        'anthropic-version': '2023-06-01',
        'content-type': 'application/json',
    }
    payload = {
        'model': ANTHROPIC_MODEL,
        'max_tokens': 512,
        'messages': [{
            'role': 'user',
            'content': [
                {'type': 'document' if mime_type == 'application/pdf' else 'image',
                 'source': {'type': 'base64', 'media_type': mime_type, 'data': b64}},
                {'type': 'text', 'text': prompt},
            ],
        }],
    }
    try:
        resp = requests.post('https://api.anthropic.com/v1/messages', json=payload, headers=headers, timeout=45)
    except requests.RequestException as e:
        raise ValueError(f"Connexion à l'API d'extraction impossible : {e}") from e
    if resp.status_code != 200:
        raise ValueError(f"L'extraction par IA a échoué ({resp.status_code}).")
    try:
        data = resp.json()
        text_out = ''.join(b.get('text', '') for b in data.get('content', []) if b.get('type') == 'text').strip()
        text_out = re.sub(r'^```(?:json)?\s*|\s*```$', '', text_out)
        parsed = json.loads(text_out)
    except (ValueError, KeyError, AttributeError) as e:
        raise ValueError("Réponse d'extraction IA illisible.") from e
    if not parsed.get('amount'):
        raise ValueError("Montant non détecté par l'IA sur ce reçu.")
    return parsed


def register(app):
    @app.route('/notes-de-frais')
    @login_required
    @requires_active_plan
    @require_area('invoicing')
    def expense_reports_list():
        c = cx()
        user = current_user()
        can_see_all = current_role() in ('OWNER', 'ADMIN', 'COMPTABLE')
        if can_see_all:
            reports = c.execute('SELECT * FROM expense_reports ORDER BY id DESC').fetchall()
        else:
            reports = c.execute(
                'SELECT * FROM expense_reports WHERE employee_email=? ORDER BY id DESC', (user['email'],)
            ).fetchall()
        totals = {}
        for r in reports:
            t = c.execute('SELECT SUM(amount) t FROM expense_report_lines WHERE report_id=?', (r['id'],)).fetchone()
            totals[r['id']] = t['t'] or 0
        c.close()
        return render_template('expense_reports_list.html', reports=reports, totals=totals, can_see_all=can_see_all)

    @app.route('/notes-de-frais/nouvelle', methods=['POST'])
    @login_required
    @requires_active_plan
    @require_area('invoicing')
    def expense_report_new():
        user = current_user()
        period_label = (request.form.get('period_label') or '').strip()
        c = cx()
        c.execute(
            "INSERT INTO expense_reports(employee_email,period_label,status,created_at) VALUES(?,?,'draft',?)",
            (user['email'], period_label, now()),
        )
        c.commit()
        new_id = c.execute('SELECT last_insert_rowid()').fetchone()[0]
        c.close()
        return redirect(url_for('expense_report_detail', report_id=new_id))

    @app.route('/notes-de-frais/<int:report_id>')
    @login_required
    @requires_active_plan
    @require_area('invoicing')
    def expense_report_detail(report_id):
        c = cx()
        report = c.execute('SELECT * FROM expense_reports WHERE id=?', (report_id,)).fetchone()
        if not report:
            c.close(); abort(404)
        user = current_user()
        if report['employee_email'] != user['email'] and current_role() not in ('OWNER', 'ADMIN', 'COMPTABLE'):
            c.close(); abort(403)
        lines = c.execute(
            'SELECT * FROM expense_report_lines WHERE report_id=? ORDER BY expense_date', (report_id,)
        ).fetchall()
        total = sum(l['amount'] or 0 for l in lines)
        c.close()
        return render_template('expense_report_detail.html', report=report, lines=lines, total=total,
                                categories=DEFAULT_EXPENSE_REPORT_CATEGORIES,
                                category_labels=EXPENSE_REPORT_CATEGORY_LABELS,
                                fiscal_powers=MILEAGE_FISCAL_POWERS)

    @app.route('/notes-de-frais/<int:report_id>/ligne', methods=['POST'])
    @login_required
    @requires_active_plan
    @require_area('invoicing')
    def expense_report_add_line(report_id):
        c = cx()
        report = c.execute('SELECT * FROM expense_reports WHERE id=?', (report_id,)).fetchone()
        if not report:
            c.close(); abort(404)
        if report['status'] != 'draft':
            c.close()
            flash("On ne peut ajouter une ligne qu'à une note de frais en brouillon.")
            return redirect(url_for('expense_report_detail', report_id=report_id))

        category = request.form.get('category', 'autre')
        if category not in EXPENSE_REPORT_CATEGORY_LABELS:
            category = 'autre'
        expense_date_str = request.form.get('expense_date') or None

        if category == 'kilometrique':
            fiscal_power = request.form.get('vehicle_fiscal_power', '5')
            try:
                km = float(request.form.get('km_driven') or 0)
                amount = compute_mileage_allowance(c, fiscal_power, km)
            except (ValueError, MileageRateNotConfigured) as e:
                c.close()
                flash(str(e))
                return redirect(url_for('expense_report_detail', report_id=report_id))
            c.execute(
                """INSERT INTO expense_report_lines
                   (report_id,expense_date,category,description,amount,vehicle_fiscal_power,km_driven,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (report_id, expense_date_str, category, f"Trajet {km:.0f} km ({fiscal_power} CV)",
                 amount, fiscal_power, km, now()),
            )
            c.commit(); c.close()
            flash(f"Trajet ajouté : {fr_number(amount, 2)} €.")
            return redirect(url_for('expense_report_detail', report_id=report_id))

        uploaded = request.files.get('receipt')
        amount = request.form.get('amount')
        description = (request.form.get('description') or '').strip()
        receipt_path = None
        used_ai = False

        if uploaded and uploaded.filename:
            data = uploaded.read()
            if len(data) > _RECEIPT_MAX_BYTES:
                c.close()
                flash("Le reçu dépasse 5 Mo.")
                return redirect(url_for('expense_report_detail', report_id=report_id))
            if data.startswith(b"%PDF-"):
                mime, ext = 'application/pdf', '.pdf'
            elif data.startswith(b"\xff\xd8\xff"):
                mime, ext = 'image/jpeg', '.jpg'
            elif data.startswith(b"\x89PNG\r\n\x1a\n"):
                mime, ext = 'image/png', '.png'
            else:
                c.close()
                flash("Format non reconnu (PDF, JPEG ou PNG attendu).")
                return redirect(url_for('expense_report_detail', report_id=report_id))

            receipt_dir = _receipt_dir()
            stored = uuid.uuid4().hex + ext
            (receipt_dir / stored).write_bytes(data)
            path = receipt_dir / stored
            try:
                if mime == 'application/pdf':
                    try:
                        detected = _receipt_pdf_extract(path)
                    except (ValueError, PyPdfError) as text_err:
                        if isinstance(text_err, ValueError) and "PDF sans texte exploitable" not in str(text_err):
                            raise
                        detected = _receipt_ai_extract(path.read_bytes(), mime)
                        used_ai = True
                else:
                    detected = _receipt_ai_extract(path.read_bytes(), mime)
                    used_ai = True
            except ValueError as e:
                c.close()
                flash(f"Extraction impossible : {e}")
                return redirect(url_for('expense_report_detail', report_id=report_id))
            receipt_path = stored
            if not amount:
                amount = detected.get('amount')
            if not expense_date_str:
                expense_date_str = detected.get('expense_date') or None
            if not description:
                description = detected.get('description') or ''

        try:
            amount = float(amount)
        except (TypeError, ValueError):
            c.close()
            flash("Montant manquant ou invalide.")
            return redirect(url_for('expense_report_detail', report_id=report_id))

        c.execute(
            """INSERT INTO expense_report_lines
               (report_id,expense_date,category,description,amount,receipt_path,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (report_id, expense_date_str, category, description, amount, receipt_path, now()),
        )
        c.commit(); c.close()
        flash(("Ligne analysée par IA et ajoutée." if used_ai else "Ligne ajoutée.") + f" {fr_number(amount, 2)} €.")
        return redirect(url_for('expense_report_detail', report_id=report_id))

    @app.route('/notes-de-frais/<int:report_id>/ligne/<int:line_id>/supprimer', methods=['POST'])
    @login_required
    @requires_active_plan
    @require_area('invoicing')
    def expense_report_delete_line(report_id, line_id):
        c = cx()
        report = c.execute('SELECT status FROM expense_reports WHERE id=?', (report_id,)).fetchone()
        if report and report['status'] == 'draft':
            c.execute('DELETE FROM expense_report_lines WHERE id=? AND report_id=?', (line_id, report_id))
            c.commit()
        c.close()
        return redirect(url_for('expense_report_detail', report_id=report_id))

    @app.route('/notes-de-frais/<int:report_id>/soumettre', methods=['POST'])
    @login_required
    @requires_active_plan
    @require_area('invoicing')
    def expense_report_submit(report_id):
        c = cx()
        report = c.execute('SELECT * FROM expense_reports WHERE id=?', (report_id,)).fetchone()
        if not report:
            c.close(); abort(404)
        n_lines = c.execute('SELECT COUNT(*) n FROM expense_report_lines WHERE report_id=?', (report_id,)).fetchone()['n']
        if n_lines == 0:
            flash("Ajoute au moins une ligne avant de soumettre.")
        elif report['status'] == 'draft':
            c.execute("UPDATE expense_reports SET status='submitted',submitted_at=? WHERE id=?", (now(), report_id))
            c.commit()
            log_activity('EXPENSE_REPORT_SUBMITTED', f"Note de frais #{report_id} soumise")
            flash("Note de frais soumise pour validation.")
        c.close()
        return redirect(url_for('expense_report_detail', report_id=report_id))

    @app.route('/notes-de-frais/<int:report_id>/valider', methods=['POST'])
    @login_required
    @requires_active_plan
    @require_area('invoicing')
    def expense_report_approve(report_id):
        if current_role() not in ('OWNER', 'ADMIN', 'COMPTABLE'):
            flash("Seul un propriétaire, administrateur ou comptable peut valider une note de frais.")
            return redirect(url_for('expense_report_detail', report_id=report_id))
        c = cx()
        report = c.execute('SELECT * FROM expense_reports WHERE id=?', (report_id,)).fetchone()
        if not report:
            c.close(); abort(404)
        if report['status'] != 'submitted':
            c.close()
            flash("Cette note de frais n'est pas en attente de validation.")
            return redirect(url_for('expense_report_detail', report_id=report_id))
        validator = current_user()
        c.execute(
            "UPDATE expense_reports SET status='approved',approved_by=?,approved_at=?,rejection_reason=NULL WHERE id=?",
            (validator['email'], now(), report_id),
        )
        c.commit()
        try:
            report_updated = c.execute('SELECT * FROM expense_reports WHERE id=?', (report_id,)).fetchone()
            generate_expense_report_entry(c, report_updated)
        except AccountingError as e:
            log_ops_event('ACCOUNTING_ENTRY_FAILED', outcome='ERROR', detail=f"note de frais {report_id}: {e}")
        c.close()
        log_activity('EXPENSE_REPORT_APPROVED', f"Note de frais #{report_id} validée par {validator['email']}")
        flash("Note de frais validée.")
        return redirect(url_for('expense_report_detail', report_id=report_id))

    @app.route('/notes-de-frais/<int:report_id>/rejeter', methods=['POST'])
    @login_required
    @requires_active_plan
    @require_area('invoicing')
    def expense_report_reject(report_id):
        if current_role() not in ('OWNER', 'ADMIN', 'COMPTABLE'):
            flash("Seul un propriétaire, administrateur ou comptable peut rejeter une note de frais.")
            return redirect(url_for('expense_report_detail', report_id=report_id))
        c = cx()
        report = c.execute('SELECT * FROM expense_reports WHERE id=?', (report_id,)).fetchone()
        if not report:
            c.close(); abort(404)
        if report['status'] != 'submitted':
            c.close()
            flash("Cette note de frais n'est pas en attente de validation.")
            return redirect(url_for('expense_report_detail', report_id=report_id))
        validator = current_user()
        reason = (request.form.get('rejection_reason') or '').strip()
        c.execute(
            "UPDATE expense_reports SET status='rejected',approved_by=?,approved_at=?,rejection_reason=? WHERE id=?",
            (validator['email'], now(), reason or None, report_id),
        )
        c.commit(); c.close()
        log_activity('EXPENSE_REPORT_REJECTED', f"Note de frais #{report_id} rejetée par {validator['email']}")
        flash("Note de frais rejetée.")
        return redirect(url_for('expense_report_detail', report_id=report_id))

    @app.route('/notes-de-frais/<int:report_id>/rembourser', methods=['POST'])
    @login_required
    @requires_active_plan
    @require_area('invoicing')
    def expense_report_reimburse(report_id):
        if current_role() not in ('OWNER', 'ADMIN', 'COMPTABLE'):
            flash("Seul un propriétaire, administrateur ou comptable peut marquer un remboursement.")
            return redirect(url_for('expense_report_detail', report_id=report_id))
        c = cx()
        report = c.execute('SELECT * FROM expense_reports WHERE id=?', (report_id,)).fetchone()
        if not report:
            c.close(); abort(404)
        if report['status'] != 'approved':
            c.close()
            flash("Seule une note de frais validée peut être marquée remboursée.")
            return redirect(url_for('expense_report_detail', report_id=report_id))
        c.execute("UPDATE expense_reports SET status='reimbursed',reimbursed_at=? WHERE id=?", (now(), report_id))
        c.commit()
        try:
            report_updated = c.execute('SELECT * FROM expense_reports WHERE id=?', (report_id,)).fetchone()
            generate_expense_reimbursement_entry(c, report_updated)
        except AccountingError as e:
            log_ops_event('ACCOUNTING_ENTRY_FAILED', outcome='ERROR', detail=f"remboursement note {report_id}: {e}")
        c.close()
        flash("Note de frais marquée remboursée.")
        return redirect(url_for('expense_report_detail', report_id=report_id))

    @app.route('/notes-de-frais/bareme', methods=['GET', 'POST'])
    @login_required
    @requires_active_plan
    @require_area('settings')
    def mileage_rate_settings():
        c = cx()
        if request.method == 'POST':
            for fp in MILEAGE_FISCAL_POWERS:
                for bracket in MILEAGE_BRACKETS:
                    rate_key = f'rate_{fp}_{bracket}'
                    fixed_key = f'fixed_{fp}_{bracket}'
                    try:
                        rate = float(request.form.get(rate_key) or 0)
                    except ValueError:
                        rate = 0
                    try:
                        fixed = float(request.form.get(fixed_key) or 0)
                    except ValueError:
                        fixed = 0
                    c.execute(
                        'UPDATE mileage_rate_table SET rate_per_km=?,fixed_amount=? WHERE fiscal_power=? AND bracket=?',
                        (rate, fixed, fp, bracket),
                    )
            c.commit()
            flash("Barème kilométrique mis à jour.")
            return redirect(url_for('mileage_rate_settings'))

        rows = c.execute('SELECT * FROM mileage_rate_table').fetchall()
        c.close()
        rates = {(r['fiscal_power'], r['bracket']): r for r in rows}
        return render_template('mileage_rate_settings.html', rates=rates,
                                fiscal_powers=MILEAGE_FISCAL_POWERS, brackets=MILEAGE_BRACKETS,
                                bracket_labels=MILEAGE_BRACKET_LABELS)
