"""Sezione «Scontrini» del pannello.

Le route stanno dentro register(): gli helper condivisi arrivano da `ctx` (vedi sections/__init__.py).
Questo modulo NON deve importare `app` (importazione circolare).
"""
import csv
import io
import re
from datetime import datetime, timedelta, timezone

from flask import Response, abort, flash, g, jsonify, redirect, render_template, request, session, url_for


def register(app, ctx):
    get_db = ctx.get_db
    login_required = ctx.login_required
    LOCAL_TZ = ctx.LOCAL_TZ
    fmt_dt = ctx.fmt_dt
    csv_safe = ctx.csv_safe
    now_iso = ctx.now_iso
    valid_piva = ctx.valid_piva
    new_api_key = ctx.new_api_key
    parse_items = ctx.parse_items
    insert_receipt = ctx.insert_receipt

    @app.get("/dashboard/receipts/<int:rid>")
    @login_required
    def receipt_detail(rid):
        """Dettaglio di uno scontrino del locale (segnaposto: da completare)."""
        db = get_db()
        r = db.execute("SELECT * FROM receipts WHERE id = ? AND shop_id = ?", (rid, g.panel_shop["id"])).fetchone()
        if r is None:
            abort(404)
        items = db.execute("SELECT * FROM receipt_items WHERE receipt_id = ? ORDER BY id", (rid,)).fetchall()
        return render_template("panel/receipt_detail.html", r=r, items=items)

    @app.get("/dashboard/receipts")
    @login_required
    def receipts_list():
        db = get_db()
        sid = g.panel_shop["id"]
        page = max(1, request.args.get("page", 1, type=int))
        per = 30
        total = db.execute("SELECT COUNT(*) FROM receipts WHERE shop_id = ?", (sid,)).fetchone()[0]
        rows = db.execute(
            "SELECT r.*,"
            " (SELECT COUNT(*) FROM invoice_requests i WHERE i.receipt_id = r.id) AS has_invoice,"
            " (SELECT COUNT(*) FROM messages m WHERE m.receipt_id = r.id AND m.status = 'sent') AS sent_count"
            " FROM receipts r WHERE r.shop_id = ? ORDER BY r.id DESC LIMIT ? OFFSET ?",
            (sid, per, (page - 1) * per)).fetchall()
        return render_template("receipts.html", rows=rows, page=page, pages=max(1, -(-total // per)))

    def _set_voided(rid, voided):
        db = get_db()
        row = db.execute("SELECT id, number FROM receipts WHERE id = ? AND shop_id = ?",
                         (rid, g.panel_shop["id"])).fetchone()
        if row is None:
            abort(404)
        db.execute("UPDATE receipts SET voided_at = ? WHERE id = ?",
                   (now_iso() if voided else None, rid))
        db.commit()
        flash(f"Scontrino n. {row['number']} {'annullato' if voided else 'ripristinato'}.", "ok")
        page = request.form.get("page", 1, type=int)
        return redirect(url_for("receipts_list", page=max(1, page)))

    @app.post("/dashboard/receipts/<int:rid>/void")
    @login_required
    def receipt_void(rid):
        return _set_voided(rid, True)

    @app.post("/dashboard/receipts/<int:rid>/restore")
    @login_required
    def receipt_restore(rid):
        return _set_voided(rid, False)
