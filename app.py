"""Scontrino Digitale - MVP multi-negozio per bar e ristoranti.

Avvio rapido:  python app.py   (poi apri http://127.0.0.1:5000)
"""
import csv
import hashlib
import io
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from functools import wraps

import segno
from flask import (Flask, Response, abort, flash, g, jsonify, redirect,
                   render_template, request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import messaging
from db import IntegrityError, close_db, get_db, init_db

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("Europe/Rome")
except Exception:  # tzdata non disponibile
    LOCAL_TZ = timezone.utc

D = Decimal
VAT_RATES = ("4", "5", "10", "22")


# ---------------------------------------------------------------- utilità
def now_utc():
    return datetime.now(timezone.utc)


def now_iso():
    return now_utc().isoformat(timespec="seconds")


def parse_iso(value):
    return datetime.fromisoformat(value)


def euro(cents):
    cents = int(cents or 0)
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    whole = f"{cents // 100:,}".replace(",", ".")
    return f"{sign}{whole},{cents % 100:02d} €"


def fmt_dt(iso, fmt="%d/%m/%Y %H:%M"):
    try:
        return parse_iso(iso).astimezone(LOCAL_TZ).strftime(fmt)
    except Exception:
        return iso or ""


def fmt_qty(q):
    q = float(q)
    return str(int(q)) if q == int(q) else str(q).replace(".", ",")


def to_cents(x):
    return int(D(x).quantize(D("1"), rounding=ROUND_HALF_UP))


def valid_piva(v):
    """Partita IVA italiana: 11 cifre + cifra di controllo."""
    if not re.fullmatch(r"\d{11}", v or ""):
        return False
    total = 0
    for i, ch in enumerate(v[:10]):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return (10 - total % 10) % 10 == int(v[10])


def valid_email(v):
    return bool(re.fullmatch(r"[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}", v or ""))


def csv_safe(value):
    """Evita la 'CSV injection' quando il file viene aperto con Excel."""
    s = "" if value is None else str(value)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


_hits = {}


def rate_limited(key, limit, window, shared=False):
    """Limite di tentativi. shared=True usa il database (valido tra tutte le istanze
    serverless); altrimenti la memoria del processo, sufficiente per l'API."""
    now = time.time()
    if shared:
        db = get_db()
        db.execute("DELETE FROM rate_hits WHERE key = ? AND ts < ?", (key, now - window))
        n = db.execute("SELECT COUNT(*) FROM rate_hits WHERE key = ? AND ts >= ?",
                       (key, now - window)).fetchone()[0]
        if n < limit:
            db.execute("INSERT INTO rate_hits (key, ts) VALUES (?, ?)", (key, now))
        if secrets.randbelow(50) == 0:  # pulizia occasionale delle chiavi inattive
            db.execute("DELETE FROM rate_hits WHERE ts < ?", (now - 86400,))
        db.commit()
        return n >= limit
    recent = [t for t in _hits.get(key, []) if now - t < window]
    if len(recent) >= limit:
        _hits[key] = recent
        return True
    recent.append(now)
    _hits[key] = recent
    if len(_hits) > 20000:
        _hits.clear()
    return False


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


# ------------------------------------------------------- logica scontrini
def parse_items(raw):
    if not isinstance(raw, list) or not 1 <= len(raw) <= 200:
        raise ApiError("'items' deve essere una lista da 1 a 200 righe")
    out = []
    for i, it in enumerate(raw, 1):
        if not isinstance(it, dict):
            raise ApiError(f"riga {i}: formato non valido")
        name = str(it.get("name", "")).strip()[:120]
        if not name:
            raise ApiError(f"riga {i}: manca 'name'")
        try:
            qty = D(str(it.get("qty", 1)))
            if "unit_price_cents" in it:
                price = D(str(it["unit_price_cents"]))
            elif "unit_price" in it:
                price = D(str(it["unit_price"])) * 100
            else:
                raise ApiError(f"riga {i}: manca 'unit_price_cents' (o 'unit_price' in euro)")
            rate = D(str(it.get("vat_rate", 10)))
            if not (qty.is_finite() and price.is_finite() and rate.is_finite()):
                raise InvalidOperation
            if not 0 < qty <= 1000:
                raise ApiError(f"riga {i}: 'qty' deve essere tra 0 e 1000")
            if abs(price) > 10_000_000:
                raise ApiError(f"riga {i}: prezzo fuori intervallo")
            if not 0 <= rate <= 30:
                raise ApiError(f"riga {i}: 'vat_rate' deve essere tra 0 e 30")
            line = to_cents(qty * price)
            vat = to_cents(D(line) - D(line) / (1 + rate / 100))
            unit = to_cents(price)
        except (InvalidOperation, ValueError, ArithmeticError):
            raise ApiError(f"riga {i}: numeri non validi")
        out.append(dict(name=name, qty=float(qty), unit_price_cents=unit,
                        vat_rate=float(rate), line_total_cents=line, vat_cents=vat))
    return out


def insert_receipt(shop_id, items, meta):
    """Crea lo scontrino con numerazione progressiva per negozio. Ritorna la riga."""
    db = get_db()
    total = sum(i["line_total_cents"] for i in items)
    vat = sum(i["vat_cents"] for i in items)
    token = secrets.token_urlsafe(12)
    try:
        db.lock_shop(shop_id)
        number = db.execute("SELECT COALESCE(MAX(number), 0) + 1 FROM receipts WHERE shop_id = ?",
                            (shop_id,)).fetchone()[0]
        rid = db.insert(
            "INSERT INTO receipts (shop_id, token, number, external_id, total_cents, vat_cents,"
            " payment_method, table_label, lottery_code, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (shop_id, token, number, meta.get("external_id"), total, vat,
             meta.get("payment_method", ""), meta.get("table_label", ""),
             meta.get("lottery_code", ""), now_iso()))
        db.executemany(
            "INSERT INTO receipt_items (receipt_id, name, qty, unit_price_cents, vat_rate,"
            " line_total_cents, vat_cents) VALUES (?,?,?,?,?,?,?)",
            [(rid, i["name"], i["qty"], i["unit_price_cents"], i["vat_rate"],
              i["line_total_cents"], i["vat_cents"]) for i in items])
        db.commit()
    except Exception:
        db.rollback()
        raise
    return db.execute("SELECT * FROM receipts WHERE id = ?", (rid,)).fetchone()


def vat_breakdown(items):
    rows = {}
    for it in items:
        r = rows.setdefault(it["vat_rate"], {"rate": it["vat_rate"], "gross": 0, "vat": 0})
        r["gross"] += it["line_total_cents"]
        r["vat"] += it["vat_cents"]
    for r in rows.values():
        r["net"] = r["gross"] - r["vat"]
    return sorted(rows.values(), key=lambda r: r["rate"])


def new_api_key():
    key = "sd_" + secrets.token_urlsafe(24)
    return key, hashlib.sha256(key.encode()).hexdigest(), key[:8]


def create_shop(name, email, password):
    """Crea un negozio e ritorna (id, api_key in chiaro). Usata da registrazione e manage.py."""
    key, key_hash, prefix = new_api_key()
    db = get_db()
    shop_id = db.insert(
        "INSERT INTO shops (name, email, password_hash, api_key_hash, api_key_prefix, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (name, email.lower(), generate_password_hash(password), key_hash, prefix, now_iso()))
    db.commit()
    return shop_id, key


def _load_secret(instance_dir):
    path = os.path.join(instance_dir, "secret_key")
    if os.path.exists(path):
        with open(path) as fh:
            return fh.read().strip()
    key = secrets.token_hex(32)
    with open(path, "w") as fh:
        fh.write(key)
    os.chmod(path, 0o600)
    return key


# -------------------------------------------------------------- applicazione
def _secret_key(instance, on_vercel):
    key = os.environ.get("SECRET_KEY")
    if key:
        return key
    if on_vercel:  # il disco è in sola lettura e ogni istanza ne avrebbe una diversa
        raise RuntimeError("Imposta SECRET_KEY tra le Environment Variables del progetto Vercel.")
    return _load_secret(instance)


def create_app(config=None):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    # I file statici stanno in public/: Vercel li serve dalla CDN, in locale li serve Flask.
    app = Flask(__name__, static_folder=os.path.join(base_dir, "public", "static"),
                static_url_path="/static")
    on_vercel = bool(os.environ.get("VERCEL"))
    database_url = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL") or ""
    if on_vercel and not database_url:
        raise RuntimeError("Imposta DATABASE_URL (Postgres) tra le Environment Variables di Vercel: "
                           "su Vercel il disco non è persistente e SQLite perderebbe i dati.")
    instance = os.path.join(base_dir, "instance")
    try:
        os.makedirs(instance, exist_ok=True)
    except OSError:  # disco in sola lettura: con Postgres non serve
        if not database_url:
            raise
    app.config.update(
        DATABASE=os.path.join(instance, "scontrini.db"),
        DATABASE_URL=database_url,
        SECRET_KEY=_secret_key(instance, on_vercel),
        PUBLIC_BASE_URL=os.environ.get("PUBLIC_BASE_URL", "").rstrip("/"),
        ALLOW_REGISTRATION=os.environ.get("ALLOW_REGISTRATION", "1") == "1",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "1" if on_vercel else "0") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(days=14),
        MAX_CONTENT_LENGTH=256 * 1024,
    )
    if config:
        app.config.update(config)
    if os.environ.get("BEHIND_PROXY", "1" if on_vercel else "0") == "1":
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    init_db(app.config)
    app.teardown_appcontext(close_db)
    app.jinja_env.filters.update(euro=euro, dt=fmt_dt, qty=fmt_qty)

    def public_url(token):
        base = app.config["PUBLIC_BASE_URL"] or request.host_url.rstrip("/")
        return f"{base}/r/{token}"

    # ---- sicurezza trasversale
    @app.before_request
    def load_user():
        g.panel_shop = None
        sid = session.get("shop_id")
        if sid:
            g.panel_shop = get_db().execute("SELECT * FROM shops WHERE id = ?", (sid,)).fetchone()
            if g.panel_shop is None:
                session.pop("shop_id", None)

    @app.before_request
    def csrf_protect():
        if request.method == "POST" and not request.path.startswith("/api/"):
            expected = session.get("csrf", "")
            if not expected or not secrets.compare_digest(expected, request.form.get("csrf", "")):
                abort(400)

    @app.context_processor
    def inject():
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(16)
        return dict(csrf_token=session["csrf"], me=g.get("panel_shop"),
                    allow_registration=app.config["ALLOW_REGISTRATION"])

    @app.after_request
    def security_headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; form-action 'self'; frame-ancestors 'none'")
        if request.path.startswith(("/r/", "/dashboard", "/api/")):
            resp.headers["Cache-Control"] = "no-store" if not request.path.endswith(".svg") else "private, max-age=3600"
        return resp

    @app.errorhandler(ApiError)
    def handle_api_error(err):
        return jsonify(error=err.message), err.status

    @app.errorhandler(404)
    def not_found(_e):
        if request.path.startswith("/api/"):
            return jsonify(error="non trovato"), 404
        return render_template("error.html", code=404,
                               text="Pagina non trovata. Il link potrebbe essere errato."), 404

    @app.errorhandler(400)
    def bad_request(_e):
        return render_template("error.html", code=400,
                               text="Richiesta non valida o sessione scaduta. Ricarica la pagina e riprova."), 400

    @app.errorhandler(413)
    def too_large(_e):
        return jsonify(error="richiesta troppo grande"), 413

    def login_required(view):
        @wraps(view)
        def wrapped(*a, **kw):
            if g.panel_shop is None:
                return redirect(url_for("login"))
            return view(*a, **kw)
        return wrapped

    def api_auth(view):
        @wraps(view)
        def wrapped(*a, **kw):
            key = request.headers.get("X-API-Key", "")
            if not key:
                auth = request.headers.get("Authorization", "")
                key = auth[7:] if auth.lower().startswith("bearer ") else ""
            if not key:
                raise ApiError("API key mancante (header X-API-Key)", 401)
            row = get_db().execute("SELECT * FROM shops WHERE api_key_hash = ?",
                                   (hashlib.sha256(key.encode()).hexdigest(),)).fetchone()
            if row is None:
                raise ApiError("API key non valida", 401)
            if rate_limited(f"api:{row['id']}", 600, 60):
                raise ApiError("troppe richieste", 429)
            g.api_shop = row
            return view(*a, **kw)
        return wrapped

    # ------------------------------------------------------------ pagine pubbliche
    @app.get("/")
    def index():
        return render_template("landing.html")

    @app.get("/healthz")
    def healthz():
        return jsonify(status="ok")

    # ------------------------------------------------------------------- API cassa
    def receipt_payload(row):
        return {"id": row["id"], "token": row["token"], "number": row["number"],
                "total_cents": row["total_cents"], "voided": row["voided_at"] is not None,
                "url": public_url(row["token"]),
                "qr_svg_url": public_url(row["token"]) + "/qr.svg"}

    @app.post("/api/v1/receipts")
    @api_auth
    def api_create_receipt():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ApiError("corpo JSON mancante o non valido")
        items = parse_items(data.get("items"))
        total = sum(i["line_total_cents"] for i in items)
        if total < 0:
            raise ApiError("il totale non può essere negativo")
        if "total_cents" in data:
            try:
                declared = int(data["total_cents"])
            except (TypeError, ValueError):
                raise ApiError("'total_cents' non valido")
            if declared != total:
                raise ApiError(f"'total_cents' ({declared}) non coincide con la somma delle righe ({total})")
        ext = str(data["external_id"])[:64] if data.get("external_id") else None
        meta = {
            "external_id": ext,
            "payment_method": str(data.get("payment_method", ""))[:30],
            "table_label": str(data.get("table", ""))[:30],
            "lottery_code": re.sub(r"[^A-Za-z0-9]", "", str(data.get("lottery_code", "")))[:8].upper(),
        }
        db = get_db()
        shop_id = g.api_shop["id"]
        if ext:  # idempotenza: la cassa può ritentare senza creare doppioni
            row = db.execute("SELECT * FROM receipts WHERE shop_id = ? AND external_id = ?",
                             (shop_id, ext)).fetchone()
            if row:
                return jsonify(receipt_payload(row)), 200
        try:
            row = insert_receipt(shop_id, items, meta)
        except IntegrityError:
            row = db.execute("SELECT * FROM receipts WHERE shop_id = ? AND external_id = ?",
                             (shop_id, ext)).fetchone()
            if row is None:
                raise
            return jsonify(receipt_payload(row)), 200
        return jsonify(receipt_payload(row)), 201

    @app.post("/api/v1/receipts/<token>/send")
    @api_auth
    def api_send_receipt(token):
        db = get_db()
        shop = g.api_shop
        receipt = db.execute("SELECT * FROM receipts WHERE token = ? AND shop_id = ?",
                             (token, shop["id"])).fetchone()
        if receipt is None:
            raise ApiError("scontrino non trovato", 404)
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ApiError("corpo JSON mancante o non valido")
        if receipt["voided_at"]:
            raise ApiError("scontrino annullato: non si può inviare", 409)
        phone = messaging.normalize_phone(data.get("phone"))
        if not phone:
            raise ApiError("numero di telefono non valido")
        channel = data.get("channel", "whatsapp")
        if channel not in ("whatsapp", "sms"):
            raise ApiError("'channel' deve essere 'whatsapp' o 'sms'")
        already = db.execute("SELECT COUNT(*) FROM messages WHERE receipt_id = ?",
                             (receipt["id"],)).fetchone()[0]
        if already >= 3:
            raise ApiError("limite di invii per questo scontrino raggiunto", 429)

        link = public_url(token)
        body = messaging.render_template_text(shop["message_template"], shop["name"], link)
        status, error = messaging.send(channel, phone, body,
                                       template_vars={"1": shop["name"], "2": link})
        db.execute("INSERT INTO messages (receipt_id, channel, phone_masked, status, error, created_at)"
                   " VALUES (?,?,?,?,?,?)",
                   (receipt["id"], channel, messaging.mask_phone(phone), status, error, now_iso()))
        # Il numero si conserva SOLO con consenso marketing esplicito.
        if data.get("marketing_consent") is True and status == "sent":
            db.execute("INSERT INTO contacts (shop_id, phone, consent_at) VALUES (?,?,?)"
                       " ON CONFLICT DO NOTHING",
                       (shop["id"], phone, now_iso()))
        db.commit()
        if status != "sent":
            return jsonify(status=status, error=error), 502
        return jsonify(status="sent", url=link)

    @app.post("/api/v1/receipts/<token>/void")
    @api_auth
    def api_void_receipt(token):
        """Annulla uno scontrino (es. vendita stornata in cassa). Idempotente."""
        db = get_db()
        row = db.execute("SELECT * FROM receipts WHERE token = ? AND shop_id = ?",
                         (token, g.api_shop["id"])).fetchone()
        if row is None:
            raise ApiError("scontrino non trovato", 404)
        if row["voided_at"] is None:
            db.execute("UPDATE receipts SET voided_at = ? WHERE id = ?", (now_iso(), row["id"]))
            db.commit()
            row = db.execute("SELECT * FROM receipts WHERE id = ?", (row["id"],)).fetchone()
        return jsonify(receipt_payload(row))

    # --------------------------------------------------------- scontrino per il cliente
    def load_receipt(token):
        return get_db().execute(
            "SELECT r.*, s.name AS shop_name, s.address AS shop_address, s.vat_number AS shop_vat,"
            " s.review_url, s.footer_note, s.invoice_window_days"
            " FROM receipts r JOIN shops s ON s.id = r.shop_id WHERE r.token = ?", (token,)).fetchone()

    @app.get("/r/<token>")
    def receipt_view(token):
        r = load_receipt(token)
        if r is None:
            abort(404)
        db = get_db()
        db.execute("UPDATE receipts SET views = views + 1 WHERE id = ?", (r["id"],))
        db.commit()
        items = db.execute("SELECT * FROM receipt_items WHERE receipt_id = ? ORDER BY id",
                           (r["id"],)).fetchall()
        inv = db.execute("SELECT * FROM invoice_requests WHERE receipt_id = ?", (r["id"],)).fetchone()
        age = now_utc() - parse_iso(r["created_at"])
        can_invoice = (inv is None and r["voided_at"] is None
                       and age <= timedelta(days=r["invoice_window_days"]))
        return render_template("receipt.html", r=r, items=items, vat_rows=vat_breakdown(items),
                               inv=inv, can_invoice=can_invoice, form={}, errors=[])

    @app.get("/r/<token>/qr.svg")
    def receipt_qr(token):
        if load_receipt(token) is None:
            abort(404)
        buf = io.BytesIO()
        segno.make(public_url(token), error="m").save(buf, kind="svg", scale=10, border=2)
        return Response(buf.getvalue(), mimetype="image/svg+xml")

    @app.post("/r/<token>/invoice")
    def receipt_invoice(token):
        r = load_receipt(token)
        if r is None:
            abort(404)
        if rate_limited(f"inv:{request.remote_addr}", 10, 3600, shared=True):
            abort(400)
        db = get_db()
        items = db.execute("SELECT * FROM receipt_items WHERE receipt_id = ? ORDER BY id",
                           (r["id"],)).fetchall()
        existing = db.execute("SELECT * FROM invoice_requests WHERE receipt_id = ?", (r["id"],)).fetchone()
        age = now_utc() - parse_iso(r["created_at"])
        can_invoice = (existing is None and r["voided_at"] is None
                       and age <= timedelta(days=r["invoice_window_days"]))

        f = {k: request.form.get(k, "").strip() for k in
             ("company_name", "vat_number", "tax_code", "sdi_code", "pec", "address", "email")}
        f["vat_number"] = re.sub(r"\s|^IT", "", f["vat_number"].upper())
        f["tax_code"] = f["tax_code"].upper().replace(" ", "")
        f["sdi_code"] = f["sdi_code"].upper().replace(" ", "")
        errors = []
        if request.form.get("website"):  # campo trappola per i bot
            abort(400)
        if not can_invoice:
            errors.append("Non è più possibile richiedere la fattura per questo scontrino.")
        if not f["company_name"]:
            errors.append("Inserisci la ragione sociale o il nome.")
        if not f["vat_number"] and not f["tax_code"]:
            errors.append("Inserisci la partita IVA o il codice fiscale.")
        if f["vat_number"] and not valid_piva(f["vat_number"]):
            errors.append("La partita IVA non è valida (11 cifre).")
        if f["tax_code"] and not re.fullmatch(r"[A-Z0-9]{16}|\d{11}", f["tax_code"]):
            errors.append("Il codice fiscale non è valido.")
        if f["sdi_code"] and not re.fullmatch(r"[A-Z0-9]{7}", f["sdi_code"]):
            errors.append("Il codice destinatario deve avere 7 caratteri.")
        if f["pec"] and not valid_email(f["pec"]):
            errors.append("L'indirizzo PEC non è valido.")
        if not f["address"]:
            errors.append("Inserisci l'indirizzo di fatturazione.")
        if not valid_email(f["email"]):
            errors.append("Inserisci un'email valida per ricevere la fattura.")
        for k, limit in (("company_name", 120), ("address", 200), ("email", 120), ("pec", 120)):
            if len(f[k]) > limit:
                errors.append("Uno dei campi è troppo lungo.")
                break
        if errors:
            return render_template("receipt.html", r=r, items=items, vat_rows=vat_breakdown(items),
                                   inv=existing, can_invoice=can_invoice, form=f, errors=errors), 422
        try:
            db.execute(
                "INSERT INTO invoice_requests (receipt_id, company_name, vat_number, tax_code, sdi_code,"
                " pec, address, email, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (r["id"], f["company_name"], f["vat_number"], f["tax_code"], f["sdi_code"] or "0000000",
                 f["pec"], f["address"], f["email"], now_iso()))
            db.commit()
        except IntegrityError:  # la richiesta esiste già
            db.rollback()
        flash("Richiesta inviata: il locale emetterà la fattura ai dati indicati.", "ok")
        return redirect(url_for("receipt_view", token=token) + "#fattura")

    # ------------------------------------------------------------ account negoziante
    @app.route("/register", methods=["GET", "POST"])
    def register():
        if not app.config["ALLOW_REGISTRATION"]:
            abort(404)
        if request.method == "POST":
            name = request.form.get("name", "").strip()[:80]
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            if not name or not valid_email(email) or len(password) < 8:
                flash("Compila tutti i campi: serve un'email valida e una password di almeno 8 caratteri.", "err")
            elif get_db().execute("SELECT 1 FROM shops WHERE email = ?", (email,)).fetchone():
                flash("Esiste già un account con questa email.", "err")
            else:
                sid, key = create_shop(name, email, password)
                session.clear()
                session["shop_id"] = sid
                session["new_api_key"] = key
                session.permanent = True
                return redirect(url_for("settings"))
        return render_template("register.html")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            if rate_limited(f"login:{request.remote_addr}", 10, 300, shared=True):
                flash("Troppi tentativi. Riprova tra qualche minuto.", "err")
                return render_template("login.html"), 429
            email = request.form.get("email", "").strip().lower()
            row = get_db().execute("SELECT * FROM shops WHERE email = ?", (email,)).fetchone()
            if row and check_password_hash(row["password_hash"], request.form.get("password", "")):
                session.clear()
                session["shop_id"] = row["id"]
                session.permanent = True
                return redirect(url_for("dashboard"))
            flash("Email o password non corrette.", "err")
        return render_template("login.html")

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("index"))

    # --------------------------------------------------------------------- pannello
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

    @app.get("/dashboard/receipts")
    @login_required
    def receipts_list():
        db = get_db()
        sid = g.panel_shop["id"]
        page = max(1, request.args.get("page", 1, type=int))
        per = 30
        total = db.execute("SELECT COUNT(*) FROM receipts WHERE shop_id = ?", (sid,)).fetchone()[0]
        rows = db.execute(
            "SELECT r.*,"
            " (SELECT COUNT(*) FROM invoice_requests i WHERE i.receipt_id = r.id) AS has_invoice,"
            " (SELECT COUNT(*) FROM messages m WHERE m.receipt_id = r.id AND m.status = 'sent') AS sent_count"
            " FROM receipts r WHERE r.shop_id = ? ORDER BY r.id DESC LIMIT ? OFFSET ?",
            (sid, per, (page - 1) * per)).fetchall()
        return render_template("receipts.html", rows=rows, page=page, pages=max(1, -(-total // per)))

    def _set_voided(rid, voided):
        db = get_db()
        row = db.execute("SELECT id, number FROM receipts WHERE id = ? AND shop_id = ?",
                         (rid, g.panel_shop["id"])).fetchone()
        if row is None:
            abort(404)
        db.execute("UPDATE receipts SET voided_at = ? WHERE id = ?",
                   (now_iso() if voided else None, rid))
        db.commit()
        flash(f"Scontrino n. {row['number']} {'annullato' if voided else 'ripristinato'}.", "ok")
        page = request.form.get("page", 1, type=int)
        return redirect(url_for("receipts_list", page=max(1, page)))

    @app.post("/dashboard/receipts/<int:rid>/void")
    @login_required
    def receipt_void(rid):
        return _set_voided(rid, True)

    @app.post("/dashboard/receipts/<int:rid>/restore")
    @login_required
    def receipt_restore(rid):
        return _set_voided(rid, False)

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

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)),
            debug=os.environ.get("FLASK_DEBUG") == "1")
