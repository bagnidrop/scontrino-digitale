"""Sezione «Panoramica» del pannello (/dashboard).

Le route stanno dentro register(): gli helper condivisi arrivano da `ctx` (vedi sections/__init__.py).
Questo modulo NON deve importare `app` (importazione circolare).

Tutti i calcoli per giorno/ora sono fatti in Python (SQL portabile SQLite/Postgres).
Gli scontrini annullati non contano mai.
"""
from datetime import datetime, timedelta, timezone

from flask import g, redirect, render_template, request, url_for

from .common import local_date

# chiave -> (etichetta scheda, giorni coperti)
PERIOD_TABS = [("today", "Oggi", 1), ("7d", "7 giorni", 7), ("30d", "30 giorni", 30), ("90d", "90 giorni", 90)]
PERIOD_DAYS = {k: d for k, _, d in PERIOD_TABS}
VS_TEXT = {"today": "ieri alla stessa ora", "7d": "i 7 giorni prima", "30d": "i 30 giorni prima",
           "90d": "i 90 giorni prima"}
PERIOD_NOUN = {"today": "oggi", "7d": "negli ultimi 7 giorni", "30d": "negli ultimi 30 giorni",
               "90d": "negli ultimi 90 giorni"}
# La pagina Statistiche accetta solo 7d|30d|90d
STATS_PERIOD = {"today": "7d", "7d": "7d", "30d": "30d", "90d": "90d"}
DEFAULT_TEMPLATES = ("Grazie da {negozio}! Ecco il tuo scontrino digitale: {link}",
                     "Grazie da {{negozio}}! Ecco il tuo scontrino digitale: {{link}}")

GIORNI = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
GIORNI_BREVI = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]
MESI = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto",
        "settembre", "ottobre", "novembre", "dicembre"]


def _utc(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def euro_short(cents):
    """Euro interi con punto delle migliaia (etichette dell'asse)."""
    return f"{round(cents / 100):,}".replace(",", ".") + " €"


def delta(cur, prev, vs):
    """Confronto col periodo precedente: testo con freccia (mai solo colore)."""
    if prev == 0 and cur == 0:
        return f"Nessun dato per {vs}"
    if prev == 0:
        return f"▲ In aumento: {vs} era zero"
    pct = round((cur - prev) * 100 / prev)
    if pct == 0:
        return f"＝ Come {vs}"
    return f"{'▲ +' if pct > 0 else '▼ −'}{abs(pct)}% su {vs}"


def nice_max(cents):
    """Massimo «tondo» dell'asse verticale, in centesimi."""
    euros = max(cents / 100, 1)
    for step in (10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000, 100000, 200000, 500000):
        if euros <= step:
            return step * 100
    return int(euros // 100000 + 1) * 100000 * 100


def build_chart(per_day, first, n, shift_days):
    """Barre giornaliere degli ultimi n giorni (da `first`, data locale) + tacca del periodo precedente."""
    W, H, left, right, top, bottom = 640, 250, 56, 8, 14, 44
    plot_w, plot_h = W - left - right, H - top - bottom
    days = []
    for i in range(n):
        d = first + timedelta(days=i)
        tot, cnt = per_day.get(d, (0, 0))
        ptot, pcnt = per_day.get(d - timedelta(days=shift_days), (0, 0))
        days.append({"date": d, "iso": d.isoformat(), "total": tot, "count": cnt, "ptotal": ptot, "pcount": pcnt})
    vmax = nice_max(max(max(x["total"], x["ptotal"]) for x in days))
    slot = plot_w / n
    bw = round(slot * (0.62 if n <= 14 else 0.7), 1)
    step = 1 if n <= 7 else 5
    for i, x in enumerate(days):
        h = round(plot_h * x["total"] / vmax, 1)
        ph = round(plot_h * x["ptotal"] / vmax, 1)
        d = x["date"]
        x.update(
            col_x=round(left + i * slot, 1), col_w=round(slot, 1), col_y=top, col_h=plot_h + 8,
            x=round(left + i * slot + (slot - bw) / 2, 1), w=bw, h=h, y=round(top + plot_h - h, 1),
            py=round(top + plot_h - ph - 1, 1), has_prev=x["ptotal"] > 0,
            cx=round(left + i * slot + slot / 2, 1), is_today=(i == n - 1),
            show_label=((n - 1 - i) % step == 0), show_wd=(n <= 7),
            label=str(d.day), wd=GIORNI_BREVI[d.weekday()],
            long=f"{GIORNI[d.weekday()]} {d.day} {MESI[d.month - 1]}",
        )
    grid = [{"y": round(top + plot_h - plot_h * f, 1), "label": euro_short(vmax * f)} for f in (0, 0.5, 1)]
    best = max(days, key=lambda x: x["total"])
    return {"W": W, "H": H, "left": left, "right": W - right, "label_y": top + plot_h + 18,
            "label_y2": top + plot_h + 34, "days": days, "grid": grid, "n": n,
            "total": sum(x["total"] for x in days), "count": sum(x["count"] for x in days),
            "ptotal": sum(x["ptotal"] for x in days),
            "best": best if best["total"] else None}


def build_hours(per_hour):
    """Fasce orarie: conteggio scontrini per ora locale. Ritorna None se non ci sono dati."""
    if not per_hour:
        return None
    lo, hi = min(per_hour), max(per_hour)
    while hi - lo < 5:  # almeno 6 colonne, per non avere barre enormi
        if lo > 0:
            lo -= 1
        if hi - lo < 5 and hi < 23:
            hi += 1
    W, H, top, bottom, left = 360, 150, 12, 26, 6
    plot_h, plot_w = H - top - bottom, W - left * 2
    n = hi - lo + 1
    slot = plot_w / n
    cmax = max(c for c, _ in per_hour.values())
    peak_h = max(per_hour, key=lambda h: (per_hour[h][0], per_hour[h][1]))
    cols = []
    for i, h in enumerate(range(lo, hi + 1)):
        cnt, tot = per_hour.get(h, (0, 0))
        bh = round(plot_h * cnt / cmax, 1) if cnt else 0
        cols.append({"h": h, "count": cnt, "total": tot, "x": round(left + i * slot + slot * 0.12, 1),
                     "w": round(slot * 0.76, 1), "bh": bh, "y": round(top + plot_h - bh, 1),
                     "cx": round(left + i * slot + slot / 2, 1), "peak": h == peak_h,
                     "show": (h - lo) % (1 if n <= 8 else 2) == 0})
    return {"W": W, "H": H, "base": top + plot_h, "label_y": top + plot_h + 16, "left": left,
            "right": W - left, "cols": cols, "peak": peak_h,
            "peak_count": per_hour[peak_h][0], "peak_total": per_hour[peak_h][1]}


def register(app, ctx):
    get_db = ctx.get_db
    login_required = ctx.login_required
    LOCAL_TZ = ctx.LOCAL_TZ
    parse_items = ctx.parse_items
    insert_receipt = ctx.insert_receipt

    def count_messages(db, sid, start, end, status="sent"):
        return db.execute(
            "SELECT COUNT(*) FROM messages m JOIN receipts r ON r.id = m.receipt_id"
            " WHERE r.shop_id = ? AND m.status = ? AND m.created_at >= ? AND m.created_at < ?",
            (sid, status, start, end)).fetchone()[0]

    def top_dishes(db, sid, start, end):
        rows = db.execute(
            "SELECT i.name AS name, SUM(i.qty) AS qty, SUM(i.line_total_cents) AS cents"
            " FROM receipt_items i JOIN receipts r ON r.id = i.receipt_id"
            " WHERE r.shop_id = ? AND r.voided_at IS NULL AND r.created_at >= ? AND r.created_at < ?"
            " GROUP BY i.name ORDER BY cents DESC LIMIT 5", (sid, start, end)).fetchall()
        top = rows[0]["cents"] if rows else 0
        return [{"name": r["name"], "qty": r["qty"], "cents": r["cents"],
                 "pct": max(round(r["cents"] * 100 / top), 4) if top else 0} for r in rows]

    @app.get("/dashboard")
    @login_required
    def dashboard():
        db = get_db()
        shop = g.panel_shop
        sid = shop["id"]
        period = request.args.get("period", "today")
        if period not in PERIOD_DAYS:
            period = "today"
        now = datetime.now(LOCAL_TZ)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        n = PERIOD_DAYS[period]
        start = midnight - timedelta(days=n - 1)
        end = now + timedelta(seconds=1)
        shift = timedelta(days=n)
        vs = VS_TEXT[period]
        stats_period = STATS_PERIOD[period]

        today_name = f"{GIORNI[now.weekday()]} {now.day} {MESI[now.month - 1]} {now.year}"
        greeting = "Buongiorno" if now.hour < 13 else ("Buon pomeriggio" if now.hour < 18 else "Buonasera")
        common = {"period": period, "greeting": greeting, "today_name": today_name, "shop": shop,
                  "period_tabs": [(label, url_for("dashboard", period=k), k == period, None)
                                  for k, label, _ in PERIOD_TABS]}

        total_receipts = db.execute("SELECT COUNT(*) FROM receipts WHERE shop_id = ?", (sid,)).fetchone()[0]
        if total_receipts == 0:
            has_review = bool((shop["review_url"] or "").strip())
            has_template = (shop["message_template"] or "").strip() not in ("",) + DEFAULT_TEMPLATES
            steps = [
                {"done": False, "post": True, "title": "Prova uno scontrino",
                 "text": "Creane uno di esempio e guarda cosa vede il cliente sul telefono."},
                {"done": False, "href": url_for("integration"), "title": "Collega la cassa",
                 "text": "Fai arrivare qui gli scontrini in automatico."},
                {"done": has_template, "href": url_for("settings"), "title": "Personalizza il messaggio",
                 "text": "Scegli le parole con cui saluti i clienti."},
                {"done": has_review, "href": url_for("settings"), "title": "Aggiungi il link alle recensioni",
                 "text": "Invita i clienti contenti a lasciarti una recensione su Google."},
            ]
            return render_template("panel/overview.html", new_shop=True, steps=steps,
                                   done_count=sum(1 for s in steps if s["done"]), **common)

        # Grafico: 7 giorni per Oggi/7g, 30 giorni per 30g/90g; la tacca mostra lo stesso giorno del blocco precedente.
        chart_n = 7 if n <= 7 else 30
        chart_first = (midnight - timedelta(days=chart_n - 1)).date()
        since = min(start - shift, midnight - timedelta(days=2 * chart_n - 1))

        rows = db.execute(
            "SELECT created_at, total_cents, views FROM receipts"
            " WHERE shop_id = ? AND voided_at IS NULL AND created_at >= ?", (sid, _utc(since))).fetchall()

        cur = {"count": 0, "total": 0, "opened": 0}
        prev = {"count": 0, "total": 0, "opened": 0}
        per_day, per_hour = {}, {}
        s_cur, e_cur = _utc(start), _utc(end)
        s_prev, e_prev = _utc(start - shift), _utc(end - shift)
        for r in rows:
            ts = r["created_at"]
            loc = local_date(ts, LOCAL_TZ)
            tot, cnt = per_day.get(loc.date(), (0, 0))
            per_day[loc.date()] = (tot + r["total_cents"], cnt + 1)
            bucket = cur if s_cur <= ts < e_cur else (prev if s_prev <= ts < e_prev else None)
            if bucket is not None:
                bucket["count"] += 1
                bucket["total"] += r["total_cents"]
                bucket["opened"] += 1 if (r["views"] or 0) > 0 else 0
            if bucket is cur:
                c, t = per_hour.get(loc.hour, (0, 0))
                per_hour[loc.hour] = (c + 1, t + r["total_cents"])
        for b in (cur, prev):
            b["avg"] = round(b["total"] / b["count"]) if b["count"] else 0
            b["open_pct"] = round(b["opened"] * 100 / b["count"]) if b["count"] else 0

        msg_cur = count_messages(db, sid, s_cur, e_cur)
        msg_prev = count_messages(db, sid, s_prev, e_prev)
        cmp = {"total": delta(cur["total"], prev["total"], vs), "count": delta(cur["count"], prev["count"], vs),
               "avg": delta(cur["avg"], prev["avg"], vs), "msg": delta(msg_cur, msg_prev, vs)}

        pending = db.execute(
            "SELECT COUNT(*) FROM invoice_requests i JOIN receipts r ON r.id = i.receipt_id"
            " WHERE r.shop_id = ? AND i.status = 'nuova' AND r.voided_at IS NULL", (sid,)).fetchone()[0]
        failed = count_messages(db, sid, _utc(midnight - timedelta(days=29)), e_cur, "failed")
        voided_today = db.execute(
            "SELECT COUNT(*) FROM receipts WHERE shop_id = ? AND voided_at IS NOT NULL AND created_at >= ?",
            (sid, _utc(midnight))).fetchone()[0]

        recent = db.execute("SELECT * FROM receipts WHERE shop_id = ? ORDER BY id DESC LIMIT 6", (sid,)).fetchall()
        chart = build_chart(per_day, chart_first, chart_n, chart_n)
        chart["delta"] = delta(chart["total"], chart["ptotal"], f"i {chart_n} giorni prima")
        return render_template(
            "panel/overview.html", new_shop=False, cur=cur, prev=prev, cmp=cmp, msg_cur=msg_cur,
            pending=pending, failed=failed, voided_today=voided_today, recent=recent, chart=chart,
            hours=build_hours(per_hour), dishes=top_dishes(db, sid, s_cur, e_cur),
            stats_period=stats_period, has_review=bool((shop["review_url"] or "").strip()),
            period_noun=PERIOD_NOUN[period], **common)

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
