"""Opportunity Radar (v483) : opportunités tirées de faits vérifiés."""
from profitos.runtime import *
from profitos.feature_access import requires_paid_plan
from profitos import opportunity_radar as OR
from profitos import verification as V
from profitos.accounting import paris_today


def _eid():
    from profitos.entities import current_entity_id
    return current_entity_id()


def register(app):

    @app.route('/opportunites')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('pilotage')
    def radar():
        from profitos.routes.mission_control import _decorate
        today = paris_today()
        c = cx()
        try:
            findings, evaluated = OR.detect(c, _eid(), today)
            visible = [_decorate(f) for f in V.sync(c, _eid(), findings, today, None, evaluated, owner=OR.MODULE)]
        finally:
            c.close()
        groups = [(kind, OR.KIND_LABELS[kind], [f for f in visible if f['kind'] == kind and f['track']['status'] == V.OPEN])
                  for kind in OR.KINDS]
        other = [f for f in visible if f['kind'] not in OR.KINDS and f['track']['status'] == V.OPEN]
        followed = [f for f in visible if f['track']['status'] == V.IN_CONTROL]
        return render_template('opportunity_radar.html', groups=groups, other=other, followed=followed,
                               severity_labels=V.SEVERITY_LABELS, kind_labels=OR.KIND_LABELS)
