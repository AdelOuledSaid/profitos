"""Copilote (v488) : analyse du mois fondée sur les données réelles, chiffres vérifiés."""
from profitos.runtime import *
from profitos.feature_access import requires_paid_plan
from profitos import copilot as CP
from profitos.accounting import paris_today

PRESET = "Analyse ma situation financière et fiscale du mois et indique-moi les problèmes à résoudre."


def register(app):

    @app.route('/copilote', methods=['GET', 'POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('pilotage')
    def copilot():
        from profitos.routes.mission_control import _accounting_enabled, _cash, _decorate, _findings
        from profitos.entities import current_entity_id
        question = (request.form.get('question') or '').strip()[:1000] if request.method == 'POST' else ''
        c = cx()
        try:
            findings = [_decorate(f) for f in _findings(c)]
            dossier = CP.build(c, current_entity_id(), paris_today(), cash=_cash(), accounting=_accounting_enabled(),
                               findings=findings)
        finally:
            c.close()
        answer, note = None, None
        if question:
            try:
                from profitos.plan_usage import consume_ai_extraction_quota
                consume_ai_extraction_quota()
                text = CP.ask_ai(question, dossier)
                ok, reasons = CP.check_answer(text, dossier)
                if ok:
                    answer = text
                else:
                    note = ("Réponse de l'IA écartée par le contrôle des chiffres (" + ' ; '.join(reasons) +
                            ") : voici le rapport calculé.")
                    log_ops_event('COPILOT_ANSWER_REJECTED', outcome='WARNING', detail='; '.join(reasons)[:300])
            except ValueError as exc:
                note = f"Rédaction par l'IA indisponible ({exc}) : voici le rapport calculé à partir de vos données."
            log_activity('COPILOT_QUESTION', question[:200])
        links = {f['key']: f for f in findings}
        return render_template('copilot.html', dossier=dossier, report=CP.report(dossier), answer=answer, note=note,
                               question=question, preset=PRESET, links=links,
                               ai_configured=bool(ANTHROPIC_API_KEY))
