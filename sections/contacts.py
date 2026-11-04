"""Sezione «Clienti» del pannello.

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

    @app.get("/dashboard/contacts")
    @login_required
    def contacts_list():
        rows = get_db().execute("SELECT * FROM contacts WHERE shop_id = ? ORDER BY id DESC",
                                (g.panel_shop["id"],)).fetchall()
        return render_template("contacts.html", rows=rows)

    @app.post("/dashboard/contacts/<int:cid>/delete")
    @login_required
    def contact_delete(cid):
        db = get_db()
        db.execute("DELETE FROM contacts WHERE id = ? AND shop_id = ?", (cid, g.panel_shop["id"]))
        db.commit()
        flash("Contatto eliminato.", "ok")
        return redirect(url_for("contacts_list"))
