"""Sezione «Fatture» del pannello: richieste di fattura dei clienti.

Endpoint: invoices_list, invoice_detail (nuovo), invoices_csv, invoice_status.
Questo modulo NON deve importare `app` (importazione circolare).
"""
import csv
import io

from flask import Response, abort, flash, g, redirect, render_template, request, url_for

STATUS_KEYS = ("all", "nuova", "evasa", "void")


def register(app, ctx):
    get_db = ctx.get_db
    login_required = ctx.login_required
    fmt_dt = ctx.fmt_dt
    csv_safe = ctx.csv_safe

    def _rows(sid, q="", inv_id=None):
        sql = ("SELECT i.*, r.id AS rid, r.number, r.total_cents, r.created_at AS receipt_date,"
               " r.voided_at AS receipt_voided"
               " FROM invoice_requests i JOIN receipts r ON r.id = i.receipt_id WHERE r.shop_id = ?")
        params = [sid]
        if inv_id is not None:
            sql += " AND i.id = ?"
            params.append(inv_id)
        q = (q or "").strip().lower()[:80]
        if q:
            like = "%" + q + "%"
            sql += (" AND (LOWER(i.company_name) LIKE ? OR LOWER(i.vat_number) LIKE ? OR LOWER(i.tax_code) LIKE ?"
                    " OR LOWER(i.email) LIKE ?)")
            params += [like] * 4
        sql += " ORDER BY i.id DESC"
        out = []
        for i in get_db().execute(sql, params).fetchall():
            out.append({
                "id": i["id"], "rid": i["rid"], "number": i["number"], "total_cents": i["total_cents"],
                "created_at": i["created_at"], "receipt_date": i["receipt_date"],
                "voided": bool(i["receipt_voided"]), "status": i["status"],
                "company_name": i["company_name"], "vat_number": i["vat_number"], "tax_code": i["tax_code"],
                "sdi_code": i["sdi_code"], "pec": i["pec"], "address": i["address"], "email": i["email"],
                "copy_text": _copy_text(i),
            })
        return out

    def _copy_text(i):
        lines = [f"Ragione sociale: {i['company_name']}"]
        if i["vat_number"]:
            lines.append(f"P.IVA: {i['vat_number']}")
        if i["tax_code"]:
            lines.append(f"Codice fiscale: {i['tax_code']}")
        lines.append(f"Codice SDI: {i['sdi_code']}")
        if i["pec"]:
            lines.append(f"PEC: {i['pec']}")
        lines.append(f"Indirizzo: {i['address']}")
        lines.append(f"Email: {i['email']}")
        lines.append(f"Scontrino n. {i['number']} del {fmt_dt(i['receipt_date'], '%d/%m/%Y')}")
        return "\n".join(lines)

    @app.get("/dashboard/invoices")
    @login_required
    def invoices_list():
        status = request.args.get("status", "all")
        if status not in STATUS_KEYS:
            status = "all"
        q = request.args.get("q", "").strip()[:80]
        allrows = _rows(g.panel_shop["id"], q)
        todo = [r for r in allrows if r["status"] == "nuova" and not r["voided"]]
        done = [r for r in allrows if r["status"] == "evasa" and not r["voided"]]
        voided = [r for r in allrows if r["voided"]]
        counts = {"all": len(allrows), "nuova": len(todo), "evasa": len(done), "void": len(voided)}
        rows = {"all": allrows, "nuova": todo, "evasa": done, "void": voided}[status]
        todo_total = sum(r["total_cents"] for r in todo)
        return render_template("panel/invoices.html", rows=rows, status=status, q=q, counts=counts,
                               todo_total=todo_total)

    @app.get("/dashboard/invoices/<int:inv_id>")
    @login_required
    def invoice_detail(inv_id):
        rows = _rows(g.panel_shop["id"], inv_id=inv_id)
        if not rows:
            abort(404)
        return render_template("panel/invoice_detail.html", i=rows[0])

    @app.get("/dashboard/invoices.csv")
    @login_required
    def invoices_csv():
        out = io.StringIO()
        out.write("﻿")  # BOM: Excel legge correttamente gli accenti
        w = csv.writer(out, delimiter=";")
        w.writerow(["Data richiesta", "Scontrino n.", "Data scontrino", "Totale €", "Ragione sociale",
                    "P.IVA", "Codice fiscale", "Codice SDI", "PEC", "Indirizzo", "Email", "Stato"])
        for i in _rows(g.panel_shop["id"]):
            w.writerow([csv_safe(x) for x in (
                fmt_dt(i["created_at"]), i["number"], fmt_dt(i["receipt_date"]),
                f"{i['total_cents'] / 100:.2f}".replace(".", ","), i["company_name"], i["vat_number"],
                i["tax_code"], i["sdi_code"], i["pec"], i["address"], i["email"],
                i["status"] + (" (scontrino annullato)" if i["voided"] else ""))])
        return Response(out.getvalue(), mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": "attachment; filename=richieste-fattura.csv"})

    @app.post("/dashboard/invoices/<int:inv_id>/status")
    @login_required
    def invoice_status(inv_id):
        db = get_db()
        rows = _rows(g.panel_shop["id"], inv_id=inv_id)
        if not rows:
            abort(404)
        row = rows[0]
        back = request.form.get("next", "")
        if not back.startswith("/dashboard/invoices") or back.startswith("//") or "\\" in back:
            back = url_for("invoices_list")
        if row["status"] == "nuova":
            if row["voided"]:
                flash("Lo scontrino è annullato: la fattura non va evasa.", "err")
                return redirect(back)
            new = "evasa"
            flash(f"Fattura per {row['company_name']} segnata come evasa.", "ok")
        else:
            new = "nuova"
            flash(f"Richiesta di {row['company_name']} riaperta.", "ok")
        db.execute("UPDATE invoice_requests SET status = ? WHERE id = ?", (new, inv_id))
        db.commit()
        return redirect(back)
