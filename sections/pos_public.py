"""Pagina PUBBLICA del preconto: il cliente verifica il conto dal telefono (QR) e conferma o segnala un errore.

Endpoint: pos_bill, pos_bill_reply, pos_bill_qr. Il preconto NON è un documento fiscale.
"""
import io

import segno
from flask import Response, abort, flash, redirect, render_template, request, url_for

import pos_service as pos


def register(app, ctx):
    get_db = ctx.get_db

    def load(token):
        db = get_db()
        o = pos.order_by_bill_token(db, token)
        if o is None:
            return None, None
        shop = db.execute("SELECT s.name, s.address, s.vat_number, l.etag AS logo_etag, l.width AS logo_w,"
                          " l.height AS logo_h FROM shops s LEFT JOIN shop_logos l ON l.shop_id = s.id"
                          " WHERE s.id = ?", (o["shop_id"],)).fetchone()
        return o, shop

    @app.get("/p/<token>", endpoint="pos_bill")
    def pos_bill(token):
        o, shop = load(token)
        if o is None or o["status"] == "void":
            abort(404)
        db = get_db()
        receipt_token = None
        if o["receipt_id"]:
            receipt_token = db.execute("SELECT token FROM receipts WHERE id = ?", (o["receipt_id"],)).fetchone()[0]
        return render_template("pos_bill.html", o=o, shop=shop, items=pos.order_items(db, o["id"]),
                               t=pos.totals(db, o), receipt_token=receipt_token, token=token)

    @app.post("/p/<token>/reply", endpoint="pos_bill_reply")
    def pos_bill_reply(token):
        o, _ = load(token)
        if o is None or o["status"] == "void":
            abort(404)
        if ctx.rate_limited(f"bill:{request.remote_addr}", 30, 3600, shared=True):
            abort(400)
        ok = request.form.get("answer") == "ok"
        try:
            pos.customer_reply(get_db(), token, ok, request.form.get("note", ""))
        except pos.PosError as e:
            flash(e.message, "err")
        return redirect(url_for("pos_bill", token=token))

    @app.get("/p/<token>/qr.svg", endpoint="pos_bill_qr")
    def pos_bill_qr(token):
        if pos.order_by_bill_token(get_db(), token) is None:
            abort(404)
        base = app.config["PUBLIC_BASE_URL"] or request.host_url.rstrip("/")
        buf = io.BytesIO()
        segno.make(f"{base}/p/{token}", error="m").save(buf, kind="svg", scale=10, border=2)
        return Response(buf.getvalue(), mimetype="image/svg+xml")
