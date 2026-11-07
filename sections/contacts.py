"""Sezione «Clienti» del pannello: numeri salvati SOLO con il consenso marketing del cliente.

Endpoint: contacts_list, contacts_csv, contact_delete.
Questo modulo NON deve importare `app` (importazione circolare).
"""
import csv
import io
import re
from datetime import timedelta

from flask import Response, abort, flash, g, redirect, render_template, request, url_for

PER_PAGE = 25
RECENT_DAYS = 30


# Numeri normalizzati (+39…): sono sicuri anche in Excel, quindi si esportano così come sono.
PHONE_RE = re.compile(r"\+\d{6,15}")


def register(app, ctx):
    get_db = ctx.get_db
    login_required = ctx.login_required
    fmt_dt = ctx.fmt_dt
    csv_safe = ctx.csv_safe

    def _since_iso(days):
        return (ctx.now_utc() - timedelta(days=days)).isoformat(timespec="seconds")

    def _where(sid, q, recent):
        where, params = "shop_id = ?", [sid]
        digits = re.sub(r"[^\d]", "", q or "")
        if digits:
            where += " AND phone LIKE ?"
            params.append("%" + digits + "%")
        elif q:  # testo senza cifre: nessun numero può corrispondere
            where += " AND 1 = 0"
        if recent:
            where += " AND consent_at >= ?"
            params.append(_since_iso(RECENT_DAYS))
        return where, params

    def _args():
        q = request.args.get("q", "").strip()[:40]
        recent = request.args.get("new") == "30"
        return q, recent

    @app.get("/dashboard/contacts")
    @login_required
    def contacts_list():
        db = get_db()
        sid = g.panel_shop["id"]
        q, recent = _args()
        page = max(1, request.args.get("page", 1, type=int) or 1)

        total = db.execute("SELECT COUNT(*) FROM contacts WHERE shop_id = ?", (sid,)).fetchone()[0]
        new30 = db.execute("SELECT COUNT(*) FROM contacts WHERE shop_id = ? AND consent_at >= ?",
                           (sid, _since_iso(RECENT_DAYS))).fetchone()[0]
        sent = db.execute("SELECT COUNT(*) FROM messages m JOIN receipts r ON r.id = m.receipt_id"
                          " WHERE r.shop_id = ? AND m.status = 'sent'", (sid,)).fetchone()[0]

        where, params = _where(sid, q, recent)
        count = db.execute("SELECT COUNT(*) FROM contacts WHERE " + where, params).fetchone()[0]
        pages = max(1, -(-count // PER_PAGE))
        page = min(page, pages)
        rows = db.execute("SELECT id, phone, consent_at FROM contacts WHERE " + where +
                          " ORDER BY consent_at DESC, id DESC LIMIT ? OFFSET ?",
                          params + [PER_PAGE, (page - 1) * PER_PAGE]).fetchall()

        export_args = {k: v for k, v in (("q", q), ("new", "30" if recent else "")) if v}
        return render_template("panel/contacts.html", rows=rows, q=q, recent=recent, page=page, pages=pages,
                               count=count, total=total, new30=new30, sent=sent,
                               export_url=url_for("contacts_csv", **export_args))

    @app.get("/dashboard/contacts.csv")
    @login_required
    def contacts_csv():
        q, recent = _args()
        where, params = _where(g.panel_shop["id"], q, recent)
        out = io.StringIO()
        out.write("﻿")  # BOM: Excel legge correttamente gli accenti
        w = csv.writer(out, delimiter=";")
        w.writerow(["Numero", "Consenso marketing del"])
        for c in get_db().execute("SELECT phone, consent_at FROM contacts WHERE " + where +
                                  " ORDER BY consent_at DESC, id DESC", params).fetchall():
            w.writerow([c["phone"] if PHONE_RE.fullmatch(c["phone"]) else csv_safe(c["phone"]),
                        csv_safe(fmt_dt(c["consent_at"]))])
        return Response(out.getvalue(), mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": "attachment; filename=clienti-consenso-marketing.csv"})

    @app.post("/dashboard/contacts/<int:cid>/delete")
    @login_required
    def contact_delete(cid):
        db = get_db()
        sid = g.panel_shop["id"]
        row = db.execute("SELECT id FROM contacts WHERE id = ? AND shop_id = ?", (cid, sid)).fetchone()
        if not row:
            abort(404)
        db.execute("DELETE FROM contacts WHERE id = ? AND shop_id = ?", (cid, sid))
        db.commit()
        flash("Numero eliminato: non lo trovi più nel tuo elenco.", "ok")
        back = request.form.get("next", "")
        if not back.startswith("/dashboard/contacts") or back.startswith("//") or "\\" in back:
            back = url_for("contacts_list")
        return redirect(back)
