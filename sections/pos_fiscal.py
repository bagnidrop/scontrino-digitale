"""«Da fiscalizzare»: conti pagati per cui va ancora emesso il documento commerciale vero.

Endpoint: pos_fiscal, pos_fiscal_issue. Questo software non emette documenti fiscali (vedi fiscal.py).
"""
from flask import flash, g, redirect, render_template, request, url_for

import pos_service as pos


def register(app, ctx):
    get_db = ctx.get_db

    @app.get("/dashboard/fiscal", endpoint="pos_fiscal")
    @ctx.login_required
    def pos_fiscal():
        db, sid = get_db(), g.panel_shop["id"]
        done = db.execute("SELECT o.*, r.number AS receipt_number, r.total_cents AS receipt_total, r.token AS receipt_token"
                          " FROM pos_orders o JOIN receipts r ON r.id = o.receipt_id"
                          " WHERE o.shop_id = ? AND o.fiscal_status = 'emesso' ORDER BY o.fiscal_at DESC LIMIT 20",
                          (sid,)).fetchall()
        return render_template("panel/pos_fiscal.html", queue=pos.fiscal_queue(db, sid), done=done)

    @app.post("/dashboard/fiscal/<int:oid>", endpoint="pos_fiscal_issue")
    @ctx.login_required
    def pos_fiscal_issue(oid):
        try:
            pos.mark_fiscal_issued(get_db(), g.panel_shop["id"], oid, request.form.get("number", ""))
            flash("Documento commerciale registrato.", "ok")
        except pos.PosError as e:
            flash(e.message, "err")
        return redirect(url_for("pos_fiscal"))
