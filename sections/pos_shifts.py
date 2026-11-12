"""«Turni e chiusura»: apertura/chiusura della giornata di cassa con il riepilogo degli incassi.

Endpoint: pos_shifts (GET, ?id=<turno> per il dettaglio), pos_shift_open, pos_shift_close.
I contanti attesi = fondo cassa + contanti incassati (gli importi registrati sono già al netto del resto).
"""
from decimal import Decimal, InvalidOperation

from flask import flash, g, redirect, render_template, request, url_for

import pos_service as pos
from db import IntegrityError


def _euro_to_cents(text):
    try:
        v = int((Decimal(str(text or "0").replace(",", ".").strip()) * 100).to_integral_value())
    except (InvalidOperation, ValueError):
        raise pos.PosError("Importo non valido: scrivilo come 150,00.")
    if not 0 <= v <= 100_000_000:
        raise pos.PosError("Importo fuori intervallo.")
    return v


def summary(db, sid, shift):
    """Numeri di un turno: incassi per metodo, conti, coperti, sconti, contanti attesi."""
    by_method = db.execute(
        "SELECT p.method, SUM(p.amount_cents) AS tot FROM pos_payments p JOIN pos_orders o ON o.id = p.order_id"
        " WHERE o.shop_id = ? AND o.shift_id = ? AND o.status = 'paid' GROUP BY p.method ORDER BY tot DESC",
        (sid, shift["id"])).fetchall()
    agg = db.execute("SELECT COUNT(*), COALESCE(SUM(covers),0), COALESCE(SUM(discount_cents),0) FROM pos_orders"
                     " WHERE shop_id = ? AND shift_id = ? AND status = 'paid'", (sid, shift["id"])).fetchone()
    cash = sum(r["tot"] for r in by_method if r["method"] == "contanti")
    return {"methods": [{"method": r["method"], "total": r["tot"]} for r in by_method],
            "total": sum(r["tot"] for r in by_method), "orders": agg[0], "covers": agg[1], "discounts": agg[2],
            "cash_in": cash, "expected_cash": shift["opening_cash_cents"] + cash}


def open_orders(db, sid, shift_id):
    return db.execute("SELECT id, table_label, status FROM pos_orders WHERE shop_id = ? AND shift_id = ?"
                      " AND status IN ('open','preconto') ORDER BY id", (sid, shift_id)).fetchall()


def register(app, ctx):
    get_db = ctx.get_db

    @app.get("/dashboard/turni", endpoint="pos_shifts")
    @ctx.login_required
    def pos_shifts():
        db, sid = get_db(), g.panel_shop["id"]
        current = db.execute("SELECT * FROM pos_shifts WHERE shop_id = ? AND closed_at IS NULL", (sid,)).fetchone()
        history = db.execute("SELECT * FROM pos_shifts WHERE shop_id = ? AND closed_at IS NOT NULL"
                             " ORDER BY id DESC LIMIT 30", (sid,)).fetchall()
        detail = None
        if request.args.get("id", "").isdigit():
            detail = db.execute("SELECT * FROM pos_shifts WHERE id = ? AND shop_id = ?",
                                (int(request.args["id"]), sid)).fetchone()
        rows = [(h, summary(db, sid, h)) for h in history]
        return render_template("panel/pos_shifts.html", current=current,
                               cur_sum=summary(db, sid, current) if current else None,
                               blockers=open_orders(db, sid, current["id"]) if current else [],
                               history=rows, detail=detail,
                               det_sum=summary(db, sid, detail) if detail else None)

    @app.post("/dashboard/turni/apri", endpoint="pos_shift_open")
    @ctx.login_required
    def pos_shift_open():
        db, sid = get_db(), g.panel_shop["id"]
        try:
            cash = _euro_to_cents(request.form.get("opening_cash"))
            db.insert("INSERT INTO pos_shifts (shop_id, opened_at, opened_by, opening_cash_cents) VALUES (?,?,?,?)",
                      (sid, ctx.now_iso(), g.panel_shop["name"][:40], cash))
            db.commit()
            flash("Turno aperto. Buon servizio!", "ok")
        except pos.PosError as e:
            flash(e.message, "err")
        except IntegrityError:
            db.rollback()
            flash("C'è già un turno aperto.", "err")
        return redirect(url_for("pos_shifts"))

    @app.post("/dashboard/turni/chiudi", endpoint="pos_shift_close")
    @ctx.login_required
    def pos_shift_close():
        db, sid = get_db(), g.panel_shop["id"]
        cur = db.execute("SELECT * FROM pos_shifts WHERE shop_id = ? AND closed_at IS NULL", (sid,)).fetchone()
        if not cur:
            flash("Non c'è nessun turno aperto.", "err")
            return redirect(url_for("pos_shifts"))
        if open_orders(db, sid, cur["id"]):
            flash("Ci sono ancora conti aperti: chiudili o annullali prima di chiudere il turno.", "err")
            return redirect(url_for("pos_shifts"))
        try:
            counted = _euro_to_cents(request.form.get("closing_cash"))
        except pos.PosError as e:
            flash(e.message, "err")
            return redirect(url_for("pos_shifts"))
        db.execute("UPDATE pos_shifts SET closed_at = ?, closing_cash_cents = ?, note = ? WHERE id = ?"
                   " AND closed_at IS NULL", (ctx.now_iso(), counted, request.form.get("note", "").strip()[:300], cur["id"]))
        db.commit()
        flash("Turno chiuso. Ecco il riepilogo.", "ok")
        return redirect(url_for("pos_shifts", id=cur["id"]))
