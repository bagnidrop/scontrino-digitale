"""Collegamento all'RT: API per il ponte (/api/rt/v1/*, autenticata col token) e azioni dal pannello.

Endpoint: rt_next, rt_result (ponte); pos_rt_enable, pos_rt_disable, pos_rt_depts, pos_rt_retry, pos_rt_send (pannello).
"""
from flask import flash, g, jsonify, redirect, request, session, url_for

import pos_service as pos
import rt


def register(app, ctx):
    get_db = ctx.get_db

    def bridge(view):
        from functools import wraps

        @wraps(view)
        def wrapped(*a, **kw):
            auth = request.headers.get("Authorization", "")
            token = auth[7:] if auth.lower().startswith("bearer ") else ""
            shop = rt.shop_by_token(get_db(), token)
            if shop is None:
                return jsonify(error="token non valido o collegamento disattivato"), 401
            if ctx.rate_limited(f"rt:{shop['id']}", 300, 60):
                return jsonify(error="troppe richieste"), 429
            g.rt_shop = shop
            rt.touch(get_db(), shop["id"])
            try:
                return view(*a, **kw)
            except pos.PosError as e:
                get_db().rollback()
                return jsonify(error=e.message), e.status
        return wrapped

    @app.post("/api/rt/v1/next", endpoint="rt_next")
    @bridge
    def rt_next():
        job = rt.claim_next(get_db(), g.rt_shop["id"])
        if not job:
            return "", 204
        return jsonify(job=job)

    @app.post("/api/rt/v1/jobs/<int:job_id>/result", endpoint="rt_result")
    @bridge
    def rt_result(job_id):
        b = request.get_json(silent=True) or {}
        status = rt.report(get_db(), g.rt_shop["id"], job_id, b.get("outcome"), b.get("number", ""), b.get("error", ""))
        return jsonify(status=status)

    # ---------------------------------------------------------------- pannello
    def back():
        return redirect(url_for("pos_fiscal") + "#registratore")

    @app.post("/dashboard/fiscal/rt/enable", endpoint="pos_rt_enable")
    @ctx.login_required
    def pos_rt_enable():
        session["rt_token_once"] = rt.enable(get_db(), g.panel_shop["id"])
        flash("Collegamento attivato. Copia il token: non lo rivedrai.", "ok")
        return back()

    @app.post("/dashboard/fiscal/rt/disable", endpoint="pos_rt_disable")
    @ctx.login_required
    def pos_rt_disable():
        rt.disable(get_db(), g.panel_shop["id"])
        flash("Collegamento disattivato: i conti restano nella lista «Da fiscalizzare» da registrare a mano.", "ok")
        return back()

    @app.post("/dashboard/fiscal/rt/depts", endpoint="pos_rt_depts")
    @ctx.login_required
    def pos_rt_depts():
        try:
            rt.set_departments(get_db(), g.panel_shop["id"], {r: request.form.get(f"dept_{r}") for r in rt.DEFAULT_DEPTS})
            flash("Reparti salvati.", "ok")
        except pos.PosError as e:
            flash(e.message, "err")
        return back()

    @app.post("/dashboard/fiscal/rt/retry/<int:job_id>", endpoint="pos_rt_retry")
    @ctx.login_required
    def pos_rt_retry(job_id):
        try:
            if request.form.get("checked") != "1":
                raise pos.PosError("Conferma di aver controllato sul registratore che il documento NON sia uscito.")
            rt.retry(get_db(), g.panel_shop["id"], job_id)
            flash("Documento rimesso in coda.", "ok")
        except pos.PosError as e:
            flash(e.message, "err")
        return back()

    @app.post("/dashboard/fiscal/rt/send/<int:order_id>", endpoint="pos_rt_send")
    @ctx.login_required
    def pos_rt_send(order_id):
        try:
            rt.enqueue(get_db(), g.panel_shop["id"], order_id)
            flash("Documento messo in coda per il registratore.", "ok")
        except pos.PosError as e:
            flash(e.message, "err")
        return back()
