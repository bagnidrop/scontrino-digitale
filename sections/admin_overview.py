"""Amministrazione · Panoramica del servizio e Registro attività (solo proprietario).

  admin_overview  GET /admin                                 stato del servizio a colpo d'occhio
  admin_activity  GET /admin/activity?action=&q=&page=       registro delle azioni del proprietario

Regole: ogni route è `@ctx.owner_required`; gli aggregati sono SQL portabile (SQLite/Postgres) con
GROUP BY, mai una query per azienda; gli scontrini annullati non contano mai; il locale del
proprietario stesso non conta nelle statistiche del servizio; le date si raggruppano in Python
nel fuso locale.
"""
import re
from datetime import datetime, timedelta, timezone

from flask import abort, render_template, request, url_for

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

INACTIVE_DAYS = 14        # «a rischio»: attiva ma senza scontrini da tanti giorni
INVITE_WAIT_DAYS = 3      # invito non accettato da più di tanti giorni
INVITE_SOON_HOURS = 48    # invito in scadenza entro tante ore
FAILED_MIN = 3            # «molti messaggi falliti»: almeno 3 e almeno il 20% degli invii
FAILED_PCT = 20
FOLLOW_MAX = 8
TOP_N = 5
PAGE_SIZE = 25

# Azioni del registro: codice -> (testo, gruppo, tono, icona)
ACTIONS = {
    "company.create": ("Ha creato l'azienda", "companies", "ok", "plus"),
    "company.suspend": ("Ha sospeso l'azienda", "companies", "warn", "alert"),
    "company.activate": ("Ha riattivato l'azienda", "companies", "ok", "check"),
    "company.notes": ("Ha aggiornato le note interne di", "companies", "neutral", "file"),
    "company.delete": ("Ha eliminato l'azienda", "deletions", "danger", "trash"),
    "company.invite_reissued": ("Ha generato un nuovo link d'invito per", "invites", "info", "send"),
    "company.invite_accepted": ("Ha accettato l'invito e creato l'account", "invites", "ok", "check"),
    "company.password_reset": ("Ha scelto una nuova password", "invites", "info", "key"),
    "impersonation.start": ("Ha aperto il pannello di", "impersonation", "info", "eye"),
    "impersonation.stop": ("Ha chiuso il pannello di", "impersonation", "neutral", "logout"),
    "impersonation.action": ("Ha fatto una modifica nel pannello di", "impersonation", "warn", "eye"),
}
GROUPS = [  # (chiave, etichetta, descrizione)
    ("companies", "Aziende"),
    ("impersonation", "Accesso come cliente"),
    ("invites", "Inviti e password"),
    ("deletions", "Eliminazioni"),
]
GROUP_KEYS = {k for k, _ in GROUPS}


def _utc(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def nice_max(n):
    for step in (4, 8, 10, 20, 40, 50, 100, 200, 400, 500, 1000, 2000, 4000, 5000, 10000, 20000, 50000):
        if n <= step:
            return step
    return (n // 100000 + 1) * 100000


def delta(cur, prev, vs, bad_if_up=False):
    """Confronto col periodo precedente: testo con freccia (mai solo colore) e direzione."""
    if prev == 0 and cur == 0:
        return {"text": f"Nessun dato né per {vs}", "dir": "flat"}
    if prev == 0:
        return {"text": f"▲ In crescita rispetto a {vs} (allora zero)", "dir": "down" if bad_if_up else "up"}
    pct = round((cur - prev) * 100 / prev)
    if pct == 0:
        return {"text": f"＝ Uguale a {vs}", "dir": "flat"}
    arrow, sign = ("▲", "+") if pct > 0 else ("▼", "−")
    good = (pct > 0) != bad_if_up
    return {"text": f"{arrow} {sign}{abs(pct)}% rispetto a {vs}", "dir": "up" if good else "down"}


def long_day(d):
    return f"{GIORNI[d.weekday()]} {d.day} {MESI[d.month - 1]}"


def ago_text(days):
    if days <= 0:
        return "oggi"
    if days == 1:
        return "ieri"
    return f"{days} giorni fa"


def plural(n, one, many):
    return f"{n} {one if n == 1 else many}"


def register(app, ctx):
    get_db = ctx.get_db
    LOCAL_TZ = ctx.LOCAL_TZ
    parse_iso = ctx.parse_iso

    def _owner_emails():
        return {e.lower() for e in app.config.get("OWNER_EMAILS", set())}

    def _period():
        key = request.args.get("period", "30d")
        return key if key in PERIOD_KEYS else "30d"

    def _new_company_url():
        # endpoint dell'agente «Aziende»: se manca ancora, il percorso del contratto
        return url_for("admin_company_new") if "admin_company_new" in app.view_functions else "/admin/companies/new"

    # ---------------------------------------------------------------- panoramica
    def _by_shop(db, sql, params, ids):
        """Esegue un aggregato (shop_id, n) e scarta le aziende non gestite (es. il proprietario)."""
        return {r[0]: int(r[1]) for r in db.execute(sql, params).fetchall() if r[0] in ids}

    def _messages(db, start, end, ids):
        out = {}
        rows = db.execute(
            "SELECT r.shop_id, m.status, COUNT(*) FROM messages m JOIN receipts r ON r.id = m.receipt_id"
            " WHERE m.created_at >= ? AND m.created_at < ? GROUP BY r.shop_id, m.status", (start, end)).fetchall()
        for sid, status, n in rows:
            if sid in ids:
                e = out.setdefault(sid, {"sent": 0, "failed": 0})
                e["sent" if status == "sent" else "failed"] += int(n)
        return out

    def _chart(days, n):
        W, H, left, right, top, bottom = 640, 240, 40, 8, 14, 44
        plot_w, plot_h = W - left - right, H - top - bottom
        vmax = nice_max(max([d["count"] for d in days] + [1]))
        slot = plot_w / n
        bw = round(max(slot * 0.66, 2), 1)
        step = 1 if n <= 7 else (3 if n <= 30 else 10)
        for i, d in enumerate(days):
            h = round(plot_h * d["count"] / vmax, 1) if d["count"] else 0
            d.update(x=round(left + i * slot + (slot - bw) / 2, 1), w=bw, h=h, y=round(top + plot_h - h, 1),
                     cx=round(left + i * slot + slot / 2, 1), show=((n - 1 - i) % step == 0),
                     is_today=(i == n - 1), lbl=str(d["date"].day), wd=GIORNI_BREVI[d["date"].weekday()],
                     long=long_day(d["date"]))
        grid = [{"y": round(top + plot_h - plot_h * f, 1), "label": f"{round(vmax * f):,}".replace(",", ".")}
                for f in (0, 0.25, 0.5, 0.75, 1)]
        return {"W": W, "H": H, "left": left, "right": W - right, "label_y": top + plot_h + 16,
                "label_y2": top + plot_h + 30, "days": days, "grid": grid}

    @app.get("/admin")
    @ctx.owner_required
    def admin_overview():
        db = get_db()
        key = _period()
        n = PERIOD_DAYS[key]
        vs = VS_TEXT[key]
        now = datetime.now(timezone.utc)
        cur_start = local_midnight(LOCAL_TZ, n - 1)
        prev_start = cur_start - timedelta(days=n)
        end_iso = _utc(datetime.now(LOCAL_TZ) + timedelta(seconds=1))
        cur_iso, prev_iso = _utc(cur_start), _utc(prev_start)
        owners = _owner_emails()

        shops = [dict(r) if not isinstance(r, dict) else r for r in db.execute(
            "SELECT id, name, email, status, created_at, invite_expires_at, last_login_at FROM shops").fetchall()]
        shops = [s for s in shops if s["email"].lower() not in owners]
        ids = {s["id"] for s in shops}
        period_tabs = [(label, url_for("admin_overview", period=k), k == key, None) for k, label in PERIOD_TABS]

        ctx_base = {"invite_days": ctx.INVITE_DAYS, "period": key, "period_tabs": period_tabs, "noun": PERIOD_NOUN[key], "vs": vs, "n": n,
                    "new_company_url": _new_company_url()}
        if not shops:
            return render_template("panel/admin_overview.html", empty=True, **ctx_base)

        # --- aggregati (una query ciascuno, mai una per azienda)
        last_receipt = {}
        for r in db.execute("SELECT shop_id, MAX(created_at) FROM receipts WHERE voided_at IS NULL"
                            " GROUP BY shop_id").fetchall():
            if r[0] in ids:
                last_receipt[r[0]] = r[1]
        rows = db.execute("SELECT shop_id, created_at, total_cents FROM receipts WHERE voided_at IS NULL"
                          " AND created_at >= ? AND created_at < ?", (cur_iso, end_iso)).fetchall()
        per_shop, per_day = {}, {}
        total_cur = 0
        money_cur = 0
        for r in rows:
            if r[0] not in ids:
                continue
            total_cur += 1
            money_cur += int(r[2] or 0)
            per_shop[r[0]] = per_shop.get(r[0], 0) + 1
            d = local_date(r[1], LOCAL_TZ).date()
            per_day[d] = per_day.get(d, 0) + 1
        prev_rows = _by_shop(db, "SELECT shop_id, COUNT(*) FROM receipts WHERE voided_at IS NULL"
                             " AND created_at >= ? AND created_at < ? GROUP BY shop_id", (prev_iso, cur_iso), ids)
        total_prev = sum(prev_rows.values())
        money_prev = sum(_by_shop(db, "SELECT shop_id, SUM(total_cents) FROM receipts WHERE voided_at IS NULL"
                                  " AND created_at >= ? AND created_at < ? GROUP BY shop_id",
                                  (prev_iso, cur_iso), ids).values())

        msg_cur = _messages(db, cur_iso, end_iso, ids)
        msg_prev = _messages(db, prev_iso, cur_iso, ids)
        sent_cur = sum(e["sent"] for e in msg_cur.values())
        failed_cur = sum(e["failed"] for e in msg_cur.values())
        sent_prev = sum(e["sent"] for e in msg_prev.values())

        inv_sql = ("SELECT r.shop_id, COUNT(*) FROM invoice_requests q JOIN receipts r ON r.id = q.receipt_id"
                   " WHERE r.voided_at IS NULL AND q.created_at >= ? AND q.created_at < ? GROUP BY r.shop_id")
        inv_cur = sum(_by_shop(db, inv_sql, (cur_iso, end_iso), ids).values())
        inv_prev = sum(_by_shop(db, inv_sql, (prev_iso, cur_iso), ids).values())

        new_cur = sum(1 for s in shops if cur_iso <= s["created_at"] < end_iso)
        new_prev = sum(1 for s in shops if prev_iso <= s["created_at"] < cur_iso)

        # --- stati delle aziende
        cutoff = _utc(now - timedelta(days=INACTIVE_DAYS))
        active = [s for s in shops if s["status"] == "active"]
        invited = [s for s in shops if s["status"] == "invited"]
        suspended = [s for s in shops if s["status"] == "suspended"]
        # a rischio solo se l'account esiste da almeno INACTIVE_DAYS giorni (i nuovi non hanno ancora emesso nulla)
        at_risk = [s for s in active if s["created_at"] < cutoff and (last_receipt.get(s["id"]) or "") < cutoff]
        now_iso = _utc(now)
        expired = [s for s in invited if s["invite_expires_at"] and s["invite_expires_at"] < now_iso]

        tone = {"up": "brand", "down": "danger", "flat": "neutral"}
        companies_url = lambda **kw: url_for("admin_companies", **kw)  # noqa: E731
        status_cards = [
            {"href": companies_url(status="all"), "label": "Aziende", "value": str(len(shops)),
             "sub": "Tutte quelle che hai creato", "icon": "building", "tone": "brand", "hint": "Vedi tutte"},
            {"href": companies_url(status="active"), "label": "Attive", "value": str(len(active)),
             "sub": (f"{len(active) * 100 // len(shops)}% del totale"), "icon": "check", "tone": "brand",
             "hint": "Vedi le attive"},
            {"href": companies_url(status="invited"), "label": "Invitate, non ancora entrate", "value": str(len(invited)),
             "sub": (f"{plural(len(expired), 'invito scaduto', 'inviti scaduti')}" if expired
                     else "Non hanno ancora accettato"),
             "icon": "send", "tone": "warn" if invited else "neutral", "hint": "Vedi gli inviti"},
            {"href": companies_url(status="suspended"), "label": "Sospese", "value": str(len(suspended)),
             "sub": "Accesso bloccato", "icon": "alert", "tone": "danger" if suspended else "neutral",
             "hint": "Vedi le sospese"},
            {"href": companies_url(status="inactive"), "label": "A rischio", "value": str(len(at_risk)),
             "sub": f"Attive senza scontrini da {INACTIVE_DAYS} giorni", "icon": "clock",
             "tone": "danger" if at_risk else "neutral", "hint": "Vedi chi non emette"},
        ]
        d_rec = delta(total_cur, total_prev, vs)
        d_money = delta(money_cur, money_prev, vs)
        d_sent = delta(sent_cur, sent_prev, vs)
        d_inv = delta(inv_cur, inv_prev, vs)
        d_new = delta(new_cur, new_prev, vs)
        fmt = lambda v: f"{v:,}".replace(",", ".")  # noqa: E731
        usage_cards = [
            {"href": companies_url(status="active", sort="activity"), "label": "Nuove aziende", "value": str(new_cur),
             "sub": d_new["text"], "icon": "plus", "tone": tone[d_new["dir"]], "hint": "Le più recenti",
             "href_override": companies_url(sort="recent")},
            {"href": companies_url(sort="activity"), "label": "Scontrini gestiti", "value": fmt(total_cur),
             "sub": d_rec["text"], "icon": "receipt", "tone": tone[d_rec["dir"]], "hint": "Aziende per attività"},
            {"href": companies_url(sort="activity"), "label": "Incassi gestiti", "value": money_cur,
             "money": True, "sub": d_money["text"], "icon": "wallet", "tone": tone[d_money["dir"]],
             "hint": "Aziende per attività"},
            {"href": companies_url(sort="activity"), "label": "Messaggi inviati", "value": fmt(sent_cur),
             "sub": (f"{plural(failed_cur, 'fallito', 'falliti')} · " if failed_cur else "Nessuno fallito · ") + d_sent["text"],
             "icon": "message", "tone": "warn" if failed_cur else tone[d_sent["dir"]], "hint": "Aziende per attività"},
            {"href": companies_url(sort="activity"), "label": "Richieste di fattura", "value": str(inv_cur),
             "sub": d_inv["text"], "icon": "file", "tone": tone[d_inv["dir"]], "hint": "Aziende per attività"},
        ]
        usage_cards[0]["href"] = usage_cards[0].pop("href_override")

        # --- grafico
        first = cur_start.date()
        days = [{"date": first + timedelta(days=i), "count": per_day.get(first + timedelta(days=i), 0)}
                for i in range(n)]
        chart = _chart(days, n)

        # --- più attive
        by_id = {s["id"]: s for s in shops}
        ranked = sorted(per_shop.items(), key=lambda kv: (-kv[1], by_id[kv[0]]["name"].lower()))[:TOP_N]
        vmax = ranked[0][1] if ranked else 1
        top = []
        for sid, c in ranked:
            s = by_id[sid]
            lr = last_receipt.get(sid)
            top.append({"id": sid, "name": s["name"], "count": c, "pct": max(3, round(c * 100 / vmax)),
                        "status": s["status"], "url": url_for("admin_company_detail", cid=sid),
                        "last": ago_text((now - parse_iso(lr)).days) if lr else ""})

        # --- da seguire
        def issued(s):
            exp = s["invite_expires_at"]
            return parse_iso(exp) - timedelta(days=ctx.INVITE_DAYS) if exp else parse_iso(s["created_at"])

        follow = []
        for s in invited:
            exp = parse_iso(s["invite_expires_at"]) if s["invite_expires_at"] else None
            sent_days = (now - issued(s)).days
            if exp and exp <= now:
                gone = max((now - exp).days, 0)
                follow.append((0, s, "Invito scaduto", "danger", "send",
                               f"Il link è scaduto {ago_text(gone)}: serve un nuovo invito."))
            elif exp and exp - now <= timedelta(hours=INVITE_SOON_HOURS):
                hours = max(int((exp - now).total_seconds() // 3600), 1)
                follow.append((1, s, "Invito in scadenza", "warn", "clock",
                               f"Il link scade fra {plural(hours, 'ora', 'ore')} e non è ancora stato aperto."))
            elif sent_days > INVITE_WAIT_DAYS:
                follow.append((3, s, "Non ha accettato", "info", "send",
                               f"Invitata da {sent_days} giorni, non ha ancora scelto la password."))
        for s in at_risk:
            lr = last_receipt.get(s["id"])
            text = (f"Nessuno scontrino da {(now - parse_iso(lr)).days} giorni." if lr
                    else "Attiva ma non ha mai emesso uno scontrino.")
            follow.append((2, s, "Non emette", "danger", "clock", text))
        for sid, e in msg_cur.items():
            tot = e["sent"] + e["failed"]
            if e["failed"] >= FAILED_MIN and e["failed"] * 100 >= FAILED_PCT * tot and by_id[sid]["status"] == "active":
                follow.append((2, by_id[sid], "Messaggi falliti", "warn", "message",
                               f"{e['failed']} messaggi su {tot} non sono partiti {PERIOD_NOUN[key]}."))
        follow.sort(key=lambda f: (f[0], f[1]["name"].lower()))
        follow_items = [{"id": s["id"], "name": s["name"], "label": lbl, "tone": tn, "icon": ic, "text": tx,
                         "url": url_for("admin_company_detail", cid=s["id"])}
                        for _p, s, lbl, tn, ic, tx in follow[:FOLLOW_MAX]]

        recent = _audit_rows(db, "", "", 5, 0, owners)

        return render_template(
            "panel/admin_overview.html", empty=False, status_cards=status_cards, usage_cards=usage_cards,
            chart=chart, total_cur=total_cur, top=top, follow=follow_items, follow_total=len(follow),
            follow_more=max(len(follow) - FOLLOW_MAX, 0), recent=recent,
            activity_url=url_for("admin_activity"), activity_sort_url=url_for("admin_companies", sort="activity"),
            at_risk_url=url_for("admin_companies", status="inactive"), **ctx_base)

    # ---------------------------------------------------------------- registro
    def _group_of(code):
        return ACTIONS[code][1] if code in ACTIONS else ""

    def _audit_where(action, q):
        where, params = [], []
        if action in GROUP_KEYS:
            codes = [c for c, v in ACTIONS.items() if v[1] == action]
            where.append("a.action IN (" + ",".join("?" * len(codes)) + ")")
            params += codes
        if q:
            like = "%" + q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where.append("(LOWER(a.target_name) LIKE ? ESCAPE '\\' OR LOWER(a.detail) LIKE ? ESCAPE '\\'"
                         " OR LOWER(a.actor_email) LIKE ? ESCAPE '\\')")
            params += [like, like, like]
        return (" WHERE " + " AND ".join(where)) if where else "", params

    def _clean(text):
        """Rete di sicurezza: nel registro non deve mai comparire nulla che somigli a un token o a una chiave."""
        return re.sub(r"[A-Za-z0-9_\-]{24,}", "[nascosto]", text or "")

    def _audit_rows(db, action, q, limit, offset, owners):
        where, params = _audit_where(action, q)
        rows = db.execute(
            "SELECT a.id, a.actor_email, a.action, a.target_shop_id, a.target_name, a.detail, a.created_at,"
            " s.id AS live_id, s.email AS live_email FROM admin_audit a"
            " LEFT JOIN shops s ON s.id = a.target_shop_id" + where +
            " ORDER BY a.created_at DESC, a.id DESC LIMIT ? OFFSET ?", params + [limit, offset]).fetchall()
        out = []
        for r in rows:
            code = r["action"]
            label, group, tone, icon = ACTIONS.get(code, ("Azione non riconosciuta", "", "neutral", "info"))
            live = r["live_id"] is not None and (r["live_email"] or "").lower() not in owners
            out.append({"id": r["id"], "actor": r["actor_email"] or "—", "code": code, "known": code in ACTIONS,
                        "label": label, "tone": tone, "icon": icon, "name": r["target_name"] or "",
                        "has_target": bool(r["target_shop_id"] or r["target_name"]),
                        "url": url_for("admin_company_detail", cid=r["live_id"]) if live else "",
                        "deleted": bool(r["target_shop_id"]) and r["live_id"] is None,
                        "detail": _clean(r["detail"]), "at": r["created_at"]})
        return out

    @app.get("/admin/activity")
    @ctx.owner_required
    def admin_activity():
        db = get_db()
        action = request.args.get("action", "all")
        if action not in GROUP_KEYS:
            action = "all"
        q = (request.args.get("q") or "").strip()[:100]
        owners = _owner_emails()
        counts = {}
        for code, c in db.execute("SELECT action, COUNT(*) FROM admin_audit GROUP BY action").fetchall():
            g_ = _group_of(code) or "other"
            counts[g_] = counts.get(g_, 0) + int(c)
        total_all = sum(counts.values())
        where, params = _audit_where(action, q)
        total = db.execute("SELECT COUNT(*) FROM admin_audit a" + where, params).fetchone()[0]
        pages = max(1, -(-total // PAGE_SIZE))
        try:
            page = int(request.args.get("page", "1"))
        except ValueError:
            page = 1
        page = min(max(page, 1), pages)
        entries = _audit_rows(db, action, q, PAGE_SIZE, (page - 1) * PAGE_SIZE, owners)

        def tab_url(key):
            kw = {}
            if key != "all":
                kw["action"] = key
            if q:
                kw["q"] = q
            return url_for("admin_activity", **kw)

        tabs_ = [("Tutte", tab_url("all"), action == "all", total_all)]
        tabs_ += [(label, tab_url(k), action == k, counts.get(k, 0)) for k, label in GROUPS]
        return render_template("panel/admin_activity.html", entries=entries, filter_tabs=tabs_, action=action, q=q,
                               total=total, total_all=total_all, page=page, pages=pages,
                               filtered=bool(q or action != "all"),
                               reset_url=url_for("admin_activity"))
