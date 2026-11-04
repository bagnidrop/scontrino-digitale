"""Sezione «Fatture» del pannello.

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

    @app.get("/dashboard/invoices")
    @login_required
    def invoices_list():
        return render_template("invoices.html", rows=_invoice_rows(g.panel_shop["id"]))

    def _invoice_rows(sid):
        return get_db().execute(
            "SELECT i.*, r.number, r.total_cents, r.vat_cents, r.created_at AS receipt_date, r.token,"
            " r.voided_at AS receipt_voided"
            " FROM invoice_requests i JOIN receipts r ON r.id = i.receipt_id"
            " WHERE r.shop_id = ? ORDER BY i.id DESC", (sid,)).fetchall()

    @app.get("/dashboard/invoices.csv")
    @login_required
    def invoices_csv():
        out = io.StringIO()
        out.write("﻿")  # BOM: Excel legge correttamente gli accenti
        w = csv.writer(out, delimiter=";")
        w.writerow(["Data richiesta", "Scontrino n.", "Data scontrino", "Totale €", "Ragione sociale",
                    "P.IVA", "Codice fiscale", "Codice SDI", "PEC", "Indirizzo", "Email", "Stato"])
        for i in _invoice_rows(g.panel_shop["id"]):
            w.writerow([csv_safe(x) for x in (
                fmt_dt(i["created_at"]), i["number"], fmt_dt(i["receipt_date"]),
                f"{i['total_cents'] / 100:.2f}".replace(".", ","), i["company_name"], i["vat_number"],
                i["tax_code"], i["sdi_code"], i["pec"], i["address"], i["email"], i["status"])])
        return Response(out.getvalue(), mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": "attachment; filename=richieste-fattura.csv"})

    @app.post("/dashboard/invoices/<int:inv_id>/status")
    @login_required
    def invoice_status(inv_id):
        db = get_db()
        row = db.execute("SELECT i.* FROM invoice_requests i JOIN receipts r ON r.id = i.receipt_id"
                         " WHERE i.id = ? AND r.shop_id = ?", (inv_id, g.panel_shop["id"])).fetchone()
        if row is None:
            abort(404)
        new = "evasa" if row["status"] == "nuova" else "nuova"
        db.execute("UPDATE invoice_requests SET status = ? WHERE id = ?", (new, inv_id))
        db.commit()
        return redirect(url_for("invoices_list"))
