"""Sezione «Panoramica» del pannello.

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

    @app.get("/dashboard")
    @login_required
    def dashboard():
        db = get_db()
        sid = g.panel_shop["id"]
        midnight = datetime.now(LOCAL_TZ).replace(hour=0, minute=0, second=0, microsecond=0)

        def since(days):
            return (midnight - timedelta(days=days)).astimezone(timezone.utc).isoformat(timespec="seconds")

        def agg(days):
            row = db.execute("SELECT COUNT(*) AS c, COALESCE(SUM(total_cents),0) AS t, COALESCE(SUM(CASE WHEN views > 0 THEN 1 ELSE 0 END),0) AS v"
                             " FROM receipts WHERE shop_id = ? AND voided_at IS NULL AND created_at >= ?",
                             (sid, since(days))).fetchone()
            return dict(count=row["c"], total=row["t"], opened=row["v"])

        stats = {"today": agg(0), "week": agg(6), "month": agg(29)}
        stats["pending_invoices"] = db.execute(
            "SELECT COUNT(*) FROM invoice_requests i JOIN receipts r ON r.id = i.receipt_id"
            " WHERE r.shop_id = ? AND i.status = 'nuova' AND r.voided_at IS NULL", (sid,)).fetchone()[0]
        stats["messages_month"] = db.execute(
            "SELECT COUNT(*) FROM messages m JOIN receipts r ON r.id = m.receipt_id"
            " WHERE r.shop_id = ? AND m.status = 'sent' AND m.created_at >= ?", (sid, since(29))).fetchone()[0]
        recent = db.execute("SELECT * FROM receipts WHERE shop_id = ? ORDER BY id DESC LIMIT 5", (sid,)).fetchall()
        base = app.config["PUBLIC_BASE_URL"] or request.host_url.rstrip("/")
        return render_template("dashboard.html", stats=stats, recent=recent, base_url=base)

    @app.post("/dashboard/test-receipt")
    @login_required
    def test_receipt():
        items = parse_items([
            {"name": "Spaghetti alle vongole", "qty": 2, "unit_price": "14.00", "vat_rate": 10},
            {"name": "Acqua naturale 0,75 l", "qty": 1, "unit_price": "3.00", "vat_rate": 10},
            {"name": "Calice di vino", "qty": 2, "unit_price": "5.50", "vat_rate": 22},
            {"name": "Coperto", "qty": 2, "unit_price": "2.00", "vat_rate": 10},
        ])
        row = insert_receipt(g.panel_shop["id"], items,
                             {"payment_method": "Carta", "table_label": "Tavolo 7"})
        return redirect(url_for("receipt_view", token=row["token"]))
