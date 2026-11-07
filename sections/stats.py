"""Sezione «Statistiche» del pannello: analisi approfondita per decidere.

Periodi: 7d | 30d | 90d (default 30d). Il periodo precedente ha la stessa lunghezza e
finisce dove inizia quello corrente. Gli scontrini annullati non contano mai.
Raggruppamenti per giorno/ora fatti in Python (SQL portabile SQLite/Postgres).
"""
import csv
import io
from datetime import datetime, timedelta, timezone

from flask import Response, g, render_template, request, url_for

from .common import local_date, local_midnight

PERIOD_KEYS = ("7d", "30d", "90d")
PERIOD_DAYS = {"7d": 7, "30d": 30, "90d": 90}
PERIOD_TABS = [("7d", "7 giorni"), ("30d", "30 giorni"), ("90d", "90 giorni")]
PERIOD_NOUN = {"7d": "negli ultimi 7 giorni", "30d": "negli ultimi 30 giorni", "90d": "negli ultimi 90 giorni"}
VS_TEXT = {"7d": "i 7 giorni precedenti", "30d": "i 30 giorni precedenti", "90d": "i 90 giorni precedenti"}
GIORNI = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
GIORNI_BREVI = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]
MESI = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto",
        "settembre", "ottobre", "novembre", "dicembre"]
MESI_BREVI = ["gen", "feb", "mar", "apr", "mag", "giu", "lug", "ago", "set", "ott", "nov", "dic"]
NO_PAYMENT = "Non indicato"
TOP_N = 10


def _utc(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def euro_short(cents):
    return f"{round(cents / 100):,}".replace(",", ".") + " €"


def nice_max(cents):
    euros = max(cents / 100, 1)
    for step in (20, 40, 80, 100, 200, 400, 800, 1000, 2000, 4000, 8000, 10000, 20000, 40000, 80000, 100000, 200000, 500000):
        if euros <= step:
            return step * 100
    return int(euros // 100000 + 1) * 100000 * 100


def rate_text(rate):
    return f"{rate:g}".replace(".", ",")


def delta(cur, prev, vs, points=False):
    """Variazione rispetto al periodo precedente: testo con freccia (mai solo colore) e direzione."""
    unit = " punti" if points else "%"
    if points:
        diff = round(cur - prev)
        if diff == 0:
            return {"text": f"＝ Uguale a {vs}", "dir": "flat"}
        arrow, sign = ("▲", "+") if diff > 0 else ("▼", "−")
        return {"text": f"{arrow} {sign}{abs(diff)}{unit} rispetto a {vs}", "dir": "up" if diff > 0 else "down"}
    if prev == 0 and cur == 0:
        return {"text": f"Nessun dato né per {vs}", "dir": "flat"}
    if prev == 0:
        return {"text": f"▲ In crescita rispetto a {vs} (allora zero)", "dir": "up"}
    pct = round((cur - prev) * 100 / prev)
    if pct == 0:
        return {"text": f"＝ Uguale a {vs}", "dir": "flat"}
    arrow, sign = ("▲", "+") if pct > 0 else ("▼", "−")
    return {"text": f"{arrow} {sign}{abs(pct)}% rispetto a {vs}", "dir": "up" if pct > 0 else "down"}


def long_day(d):
    return f"{GIORNI[d.weekday()]} {d.day} {MESI[d.month - 1]}"


def register(app, ctx):
    get_db = ctx.get_db
    login_required = ctx.login_required
    LOCAL_TZ = ctx.LOCAL_TZ
    csv_safe = ctx.csv_safe
    euro = ctx.euro

    # ------------------------------------------------------------------ dati
    def _period():
        key = request.args.get("period", "30d")
        return key if key in PERIOD_KEYS else "30d"

    def _windows(key):
        n = PERIOD_DAYS[key]
        cur_start = local_midnight(LOCAL_TZ, n - 1)
        prev_start = cur_start - timedelta(days=n)
        end = datetime.now(LOCAL_TZ) + timedelta(seconds=1)
        return n, cur_start, prev_start, end

    def _vat_rows(db, sid, start, end):
        rows = db.execute(
            "SELECT i.vat_rate AS rate, SUM(i.line_total_cents) AS gross, SUM(i.vat_cents) AS vat"
            " FROM receipt_items i JOIN receipts r ON r.id = i.receipt_id"
            " WHERE r.shop_id = ? AND r.voided_at IS NULL AND r.created_at >= ? AND r.created_at < ?"
            " GROUP BY i.vat_rate ORDER BY i.vat_rate", (sid, start, end)).fetchall()
        out = []
        for r in rows:
            gross, vat = int(r["gross"] or 0), int(r["vat"] or 0)
            out.append({"rate": float(r["rate"]), "rate_text": rate_text(float(r["rate"])),
                        "gross": gross, "vat": vat, "net": gross - vat})
        return out

    def _top_items(db, sid, start, end):
        rows = db.execute(
            "SELECT i.name AS name, SUM(i.qty) AS q, SUM(i.line_total_cents) AS t"
            " FROM receipt_items i JOIN receipts r ON r.id = i.receipt_id"
            " WHERE r.shop_id = ? AND r.voided_at IS NULL AND r.created_at >= ? AND r.created_at < ?"
            " GROUP BY i.name", (sid, start, end)).fetchall()
        return [{"name": r["name"], "qty": float(r["q"] or 0), "total": int(r["t"] or 0)} for r in rows]

    def _rank(items, key, n=TOP_N):
        top = sorted(items, key=lambda x: (-x[key], x["name"].lower()))[:n]
        vmax = max((x[key] for x in top), default=0) or 1
        for x in top:
            x["pct"] = max(2, round(x[key] * 100 / vmax)) if x[key] else 0
        return top

    def _daily_chart(days, prev_days, key):
        n = len(days)
        W, H, left, right, top, bottom = 640, 260, 56, 8, 16, 46
        plot_w, plot_h = W - left - right, H - top - bottom
        vmax = nice_max(max([d["total"] for d in days] + [d["total"] for d in prev_days] + [1]))
        slot = plot_w / n
        bw = round(max(slot * 0.66, 2), 1)
        step = 1 if n <= 7 else (3 if n <= 30 else 10)
        for i, d in enumerate(days):
            h = round(plot_h * d["total"] / vmax, 1) if d["total"] else 0
            d.update(i=i, x=round(left + i * slot + (slot - bw) / 2, 1), w=bw, h=h, y=round(top + plot_h - h, 1),
                     col_x=round(left + i * slot, 1), col_w=round(slot, 1), cx=round(left + i * slot + slot / 2, 1),
                     show=((n - 1 - i) % step == 0), is_today=(i == n - 1),
                     lbl=str(d["date"].day), wd=GIORNI_BREVI[d["date"].weekday()],
                     long=long_day(d["date"]),
                     url=url_for("receipts_list", day=d["iso"], status="valid") if d["count"] else "")
        pts = " ".join(f"{round(left + i * slot + slot / 2, 1)},{round(top + plot_h - plot_h * p['total'] / vmax, 1)}"
                       for i, p in enumerate(prev_days))
        grid = [{"y": round(top + plot_h - plot_h * f, 1), "label": euro_short(vmax * f)} for f in (0, 0.25, 0.5, 0.75, 1)]
        return {"W": W, "H": H, "left": left, "right": W - right, "label_y": top + plot_h + 16,
                "label_y2": top + plot_h + 31, "days": days, "grid": grid, "prev_points": pts, "step": step}

    def _heatmap(counts, best_day, hours_lo, hours_hi):
        cw, ch, left, top = 44, 38, 46, 26
        hours = list(range(hours_lo, hours_hi + 1))
        W, H = left + cw * len(hours), top + ch * 7 + 4
        vmax = max(counts.values(), default=0) or 1
        cells = []
        for wd in range(7):
            for j, h in enumerate(hours):
                c = counts.get((wd, h), 0)
                level = 0 if not c else min(5, max(1, -(-c * 5 // vmax)))
                day = best_day.get((wd, h))
                label = f"{GIORNI[wd].capitalize()} {h:02d}:00–{(h + 1) % 24:02d}:00: {c} scontrini"
                cells.append({"x": left + j * cw + 2, "y": top + wd * ch + 2, "w": cw - 4, "h": ch - 4,
                              "tx": left + j * cw + cw / 2, "ty": top + wd * ch + ch / 2 + 5, "count": c, "level": level,
                              "label": label,
                              "url": url_for("receipts_list", day=day.isoformat(), status="valid") if (c and day) else "",
                              "day_text": long_day(day) if (c and day) else ""})
        every = 1 if len(hours) <= 14 else 2
        return {"W": W, "H": H, "left": left, "cells": cells, "hours": [
            {"x": left + j * cw + cw / 2, "label": f"{h:02d}", "show": j % every == 0} for j, h in enumerate(hours)],
            "rows": [{"y": top + wd * ch + ch / 2 + 4, "label": GIORNI_BREVI[wd]} for wd in range(7)],
            "table_hours": hours, "table": [[counts.get((wd, h), 0) for h in hours] for wd in range(7)]}

    def _weekday_chart(wd_counts, wd_totals, best_day_wd):
        W, H, left, right, top, bottom = 380, 250, 8, 8, 24, 30
        plot_h = H - top - bottom
        slot = (W - left - right) / 7
        bw = round(slot * 0.62, 1)
        vmax = max(wd_counts) or 1
        cols = []
        for wd in range(7):
            h = round(plot_h * wd_counts[wd] / vmax, 1) if wd_counts[wd] else 0
            day = best_day_wd.get(wd)
            cols.append({"wd": GIORNI_BREVI[wd], "long": GIORNI[wd].capitalize(), "count": wd_counts[wd],
                         "total": wd_totals[wd], "h": h, "y": round(top + plot_h - h, 1), "w": bw,
                         "x": round(left + wd * slot + (slot - bw) / 2, 1), "cx": round(left + wd * slot + slot / 2, 1),
                         "col_x": round(left + wd * slot, 1), "col_w": round(slot, 1),
                         "is_best": wd_counts[wd] == vmax and vmax > 0,
                         "url": url_for("receipts_list", day=day.isoformat(), status="valid") if (day and wd_counts[wd]) else "",
                         "day_text": long_day(day) if day else ""})
        return {"W": W, "H": H, "cols": cols, "base_y": top + plot_h, "label_y": top + plot_h + 18, "top": top}

    @app.get("/dashboard/stats")
    @login_required
    def stats_page():
        db = get_db()
        sid = g.panel_shop["id"]
        key = _period()
        n, cur_start, prev_start, end = _windows(key)
        vs = VS_TEXT[key]
        rows = db.execute(
            "SELECT id, number, table_label, total_cents, payment_method, views, created_at FROM receipts"
            " WHERE shop_id = ? AND voided_at IS NULL AND created_at >= ? AND created_at < ?",
            (sid, _utc(prev_start), _utc(end))).fetchall()
        cur_iso = _utc(cur_start)
        cur, prev = [], []
        for r in rows:
            item = {"id": r["id"], "number": r["number"], "table": r["table_label"] or "", "total": r["total_cents"],
                    "payment": (r["payment_method"] or "").strip(), "views": r["views"] or 0,
                    "dt": local_date(r["created_at"], LOCAL_TZ)}
            (cur if r["created_at"] >= cur_iso else prev).append(item)

        def summary(lst):
            total = sum(x["total"] for x in lst)
            days_open = len({x["dt"].date() for x in lst})
            opened = sum(1 for x in lst if x["views"] > 0)
            return {"count": len(lst), "total": total, "avg": round(total / len(lst)) if lst else 0,
                    "days_open": days_open, "per_day": round(total / days_open) if days_open else 0,
                    "opened": opened, "open_pct": round(opened * 100 / len(lst)) if lst else 0}

        s, p = summary(cur), summary(prev)
        first = cur_start.date()
        per_day, per_day_prev = {}, {}
        for x in cur:
            t, c = per_day.get(x["dt"].date(), (0, 0))
            per_day[x["dt"].date()] = (t + x["total"], c + 1)
        for x in prev:
            t, c = per_day_prev.get(x["dt"].date(), (0, 0))
            per_day_prev[x["dt"].date()] = (t + x["total"], c + 1)
        days = []
        for i in range(n):
            d = first + timedelta(days=i)
            t, c = per_day.get(d, (0, 0))
            days.append({"date": d, "iso": d.isoformat(), "total": t, "count": c})
        prev_days = []
        for i in range(n):
            d = prev_start.date() + timedelta(days=i)
            t, c = per_day_prev.get(d, (0, 0))
            prev_days.append({"date": d, "total": t, "count": c})

        base = {"period": key, "status": "valid"}
        receipts_url = url_for("receipts_list", **base)
        inv_rows = db.execute(
            "SELECT r.created_at AS c, q.status AS st FROM invoice_requests q JOIN receipts r ON r.id = q.receipt_id"
            " WHERE r.shop_id = ? AND r.voided_at IS NULL AND r.created_at >= ? AND r.created_at < ?",
            (sid, _utc(prev_start), _utc(end))).fetchall()
        inv_cur = [r for r in inv_rows if r["c"] >= cur_iso]
        inv_prev = len(inv_rows) - len(inv_cur)
        inv_new = sum(1 for r in inv_cur if r["st"] == "nuova")
        best = max(cur, key=lambda x: (x["total"], x["dt"]), default=None)
        d_tot, d_cnt = delta(s["total"], p["total"], vs), delta(s["count"], p["count"], vs)
        d_avg = delta(s["avg"], p["avg"], vs)
        d_open = delta(s["open_pct"], p["open_pct"], vs, points=True) if p["count"] else \
            {"text": f"Nessun dato per {vs}", "dir": "flat"}
        d_inv = delta(len(inv_cur), inv_prev, vs)
        tone = {"up": "brand", "down": "danger", "flat": "neutral"}
        kpis = [
            {"href": receipts_url, "label": "Incasso", "value": euro(s["total"]), "sub": d_tot["text"], "icon": "wallet",
             "tone": tone[d_tot["dir"]], "hint": "Vedi gli scontrini"},
            {"href": receipts_url, "label": "Scontrini emessi", "value": str(s["count"]), "sub": d_cnt["text"],
             "icon": "receipt", "tone": tone[d_cnt["dir"]], "hint": "Vedi l'elenco"},
            {"href": receipts_url, "label": "Scontrino medio", "value": euro(s["avg"]), "sub": d_avg["text"],
             "icon": "trend", "tone": tone[d_avg["dir"]], "hint": "Vedi gli scontrini"},
            {"href": receipts_url, "label": "Aperti dal cliente", "value": f"{s['open_pct']}%",
             "sub": (f"{s['opened']} su {s['count']} · " + d_open["text"]) if s["count"] else "Nessuno scontrino nel periodo",
             "icon": "eye", "tone": "info", "hint": "Vedi gli scontrini"},
            {"href": url_for("invoices_list", status="nuova" if inv_new else "all"), "label": "Richieste di fattura",
             "value": str(len(inv_cur)),
             "sub": (f"{inv_new} da evadere · " if inv_new else "") + d_inv["text"],
             "icon": "file", "tone": "warn" if inv_new else "neutral", "hint": "Vedi le fatture"},
        ]
        extras = {"per_day": s["per_day"], "days_open": s["days_open"]}

        # orari di punta
        counts, totals_slot, slot_days = {}, {}, {}
        wd_counts, wd_totals, wd_days = [0] * 7, [0] * 7, [{} for _ in range(7)]
        for x in cur:
            wd, h, d = x["dt"].weekday(), x["dt"].hour, x["dt"].date()
            counts[(wd, h)] = counts.get((wd, h), 0) + 1
            slot_days.setdefault((wd, h), {})
            slot_days[(wd, h)][d] = slot_days[(wd, h)].get(d, 0) + 1
            wd_counts[wd] += 1
            wd_totals[wd] += x["total"]
            wd_days[wd][d] = wd_days[wd].get(d, 0) + 1
        best_day = {k: max(v.items(), key=lambda kv: (kv[1], kv[0]))[0] for k, v in slot_days.items()}
        best_day_wd = {wd: max(v.items(), key=lambda kv: (kv[1], kv[0]))[0] for wd, v in enumerate(wd_days) if v}
        heat = weekday = None
        peak = None
        if cur:
            hrs = [h for (_wd, h) in counts]
            lo, hi = min(hrs), max(hrs)
            while hi - lo < 5:
                if lo > 0:
                    lo -= 1
                if hi - lo < 5 and hi < 23:
                    hi += 1
            heat = _heatmap(counts, best_day, lo, hi)
            weekday = _weekday_chart(wd_counts, wd_totals, best_day_wd)
            (pwd, ph), pc = max(counts.items(), key=lambda kv: (kv[1], -kv[0][0], -kv[0][1]))
            peak = {"text": f"{GIORNI[pwd].capitalize()} {ph:02d}:00–{(ph + 1) % 24:02d}:00",
                    "sub": f"{pc} scontrini in quella fascia {PERIOD_NOUN[key]}",
                    "url": url_for("receipts_list", day=best_day[(pwd, ph)].isoformat(), status="valid")}

        # metodi di pagamento
        pay = {}
        for x in cur:
            name = x["payment"] or NO_PAYMENT
            e = pay.setdefault(name, {"name": name, "count": 0, "total": 0})
            e["count"] += 1
            e["total"] += x["total"]
        payments = sorted(pay.values(), key=lambda e: (-e["total"], e["name"]))
        for e in payments:
            e["pct"] = round(e["total"] * 100 / s["total"]) if s["total"] else 0
            e["bar"] = max(2, e["pct"]) if e["total"] else 0
            e["url"] = (url_for("receipts_list", period=key, status="valid", payment=e["name"])
                        if e["name"] != NO_PAYMENT else receipts_url)

        # giorno più ricco
        rich = max((d for d in days if d["count"]), key=lambda d: (d["total"], d["iso"]), default=None)
        insights = []
        if cur:
            insights.append({"href": peak["url"], "title": "Momento più affollato", "text": f"{peak['text']} · {peak['sub']}",
                             "icon": "clock", "tone": "info"})
            insights.append({"href": url_for("receipts_list", day=rich["iso"], status="valid"), "title": "Giorno più ricco",
                             "text": f"{long_day(rich['date']).capitalize()} · {euro(rich['total'])} in {rich['count']} scontrini",
                             "icon": "calendar", "tone": "brand"})
            top_pay = payments[0]
            insights.append({"href": top_pay["url"], "title": "Metodo di pagamento principale",
                             "text": f"{top_pay['name']} · {top_pay['pct']}% degli incassi", "icon": "wallet", "tone": "warn"})
            insights.append({"href": url_for("receipt_detail", rid=best["id"]), "title": "Scontrino più alto",
                             "text": f"N. {best['number']} · {euro(best['total'])} · {long_day(best['dt'].date())}",
                             "icon": "star", "tone": "info"})

        items = _top_items(db, sid, cur_iso, _utc(end))
        top_amount, top_qty = _rank(items, "total"), _rank(items, "qty")
        vat = _vat_rows(db, sid, cur_iso, _utc(end))
        vat_tot = {"net": sum(v["net"] for v in vat), "vat": sum(v["vat"] for v in vat), "gross": sum(v["gross"] for v in vat)}

        has_prev_any = bool(prev)
        return render_template(
            "panel/stats.html", period=key, n=n, noun=PERIOD_NOUN[key], vs=vs, s=s, p=p, kpis=kpis,
            period_tabs=[(label, url_for("stats_page", period=k), k == key, None) for k, label in PERIOD_TABS],
            chart=_daily_chart(days, prev_days, key), days=days, heat=heat, weekday=weekday, insights=insights,
            extras=extras, payments=payments, top_amount=top_amount, top_qty=top_qty, vat=vat, vat_tot=vat_tot,
            receipts_url=receipts_url, csv_url=url_for("stats_csv", period=key), empty=not cur,
            has_prev=has_prev_any, has_older=(key != "90d"), start_text=long_day(first), end_text=long_day(first + timedelta(days=n - 1)),
            prev_start_text=long_day(prev_start.date()), prev_end_text=long_day(first - timedelta(days=1)),
            url_90=url_for("stats_page", period="90d"))

    @app.get("/dashboard/stats.csv")
    @login_required
    def stats_csv():
        db = get_db()
        sid = g.panel_shop["id"]
        key = _period()
        n, cur_start, _prev, end = _windows(key)
        vat = _vat_rows(db, sid, _utc(cur_start), _utc(end))
        fmt = lambda c: f"{c / 100:.2f}".replace(".", ",")  # noqa: E731
        out = io.StringIO()
        out.write("﻿")  # BOM: Excel legge bene gli accenti
        w = csv.writer(out, delimiter=";")
        w.writerow([csv_safe("Riepilogo IVA per aliquota")])
        w.writerow([csv_safe("Periodo"), csv_safe(cur_start.strftime("%d/%m/%Y")),
                    csv_safe((end - timedelta(seconds=1)).strftime("%d/%m/%Y"))])
        w.writerow([])
        w.writerow(["Aliquota IVA %", "Imponibile €", "IVA €", "Totale lordo €"])
        for v in vat:
            w.writerow([csv_safe(v["rate_text"]), csv_safe(fmt(v["net"])), csv_safe(fmt(v["vat"])), csv_safe(fmt(v["gross"]))])
        w.writerow([csv_safe("Totale"), csv_safe(fmt(sum(v["net"] for v in vat))), csv_safe(fmt(sum(v["vat"] for v in vat))),
                    csv_safe(fmt(sum(v["gross"] for v in vat)))])
        return Response(out.getvalue(), mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename=riepilogo-iva-{key}.csv"})
