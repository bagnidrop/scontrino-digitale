"""Sezione «Impostazioni» del pannello.

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

    @app.route("/dashboard/settings", methods=["GET", "POST"])
    @login_required
    def settings():
        db = get_db()
        sid = g.panel_shop["id"]
        if request.method == "POST":
            f = request.form
            name = f.get("name", "").strip()[:80]
            review = f.get("review_url", "").strip()[:300]
            template = f.get("message_template", "").strip()[:300]
            vat = re.sub(r"\s", "", f.get("vat_number", "")).upper().removeprefix("IT")
            try:
                window = min(30, max(0, int(f.get("invoice_window_days", 3))))
            except ValueError:
                window = 3
            if not name:
                flash("Il nome del locale è obbligatorio.", "err")
            elif review and not review.startswith("https://"):
                flash("Il link recensioni deve iniziare con https://", "err")
            elif vat and not valid_piva(vat):
                flash("La partita IVA non è valida.", "err")
            else:
                if "{link}" not in template:
                    template = (template + " {link}").strip()
                db.execute("UPDATE shops SET name=?, address=?, vat_number=?, review_url=?,"
                           " message_template=?, footer_note=?, invoice_window_days=? WHERE id=?",
                           (name, f.get("address", "").strip()[:200], vat, review, template,
                            f.get("footer_note", "").strip()[:300], window, sid))
                db.commit()
                flash("Impostazioni salvate.", "ok")
                return redirect(url_for("settings"))
        new_key = session.pop("new_api_key", None)
        base = app.config["PUBLIC_BASE_URL"] or request.host_url.rstrip("/")
        return render_template("settings.html", new_key=new_key, base_url=base,
                               s=db.execute("SELECT * FROM shops WHERE id = ?", (sid,)).fetchone())

    @app.post("/dashboard/settings/regenerate-key")
    @login_required
    def regenerate_key():
        key, key_hash, prefix = new_api_key()
        db = get_db()
        db.execute("UPDATE shops SET api_key_hash = ?, api_key_prefix = ? WHERE id = ?",
                   (key_hash, prefix, g.panel_shop["id"]))
        db.commit()
        session["new_api_key"] = key
        flash("Nuova API key generata: quella precedente non funziona più.", "ok")
        return redirect(url_for("settings"))
