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
from types import SimpleNamespace

import segno
from flask import (Flask, Response, abort, flash, g, jsonify, redirect,
                   render_template, request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import messaging
import sections
import logo
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


INVITE_DAYS = 7


def hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def issue_invite(shop_id):
    """Genera un nuovo link d'invito (annulla il precedente). Ritorna il token IN CHIARO, da mostrare
    una sola volta: nel database resta solo l'hash."""
    token = secrets.token_urlsafe(32)
    db = get_db()
    db.execute("UPDATE shops SET invite_hash = ?, invite_expires_at = ? WHERE id = ?",
               (hash_token(token), (now_utc() + timedelta(days=INVITE_DAYS)).isoformat(timespec="seconds"),
                shop_id))
    db.commit()
    return token


def create_invited_shop(name, email, notes=""):
    """Crea un'azienda in stato 'invited': nessuna password utilizzabile finché non apre il link.
    Ritorna (id, token_invito). L'email deve essere già stata verificata come libera."""
    _, key_hash, prefix = new_api_key()  # chiave segnaposto: quella vera nasce quando accetta l'invito
    db = get_db()
    shop_id = db.insert(
        "INSERT INTO shops (name, email, password_hash, api_key_hash, api_key_prefix, created_at,"
        " status, owner_notes) VALUES (?,?,?,?,?,?,?,?)",
        (name, email.lower(), generate_password_hash(secrets.token_urlsafe(32)), key_hash, prefix,
         now_iso(), "invited", notes[:500]))
    db.commit()
    return shop_id, issue_invite(shop_id)


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
        DATABASE=os.environ.get("DATABASE_PATH") or os.path.join(instance, "scontrini.db"),
        DATABASE_URL=database_url,
        SECRET_KEY=_secret_key(instance, on_vercel),
        PUBLIC_BASE_URL=os.environ.get("PUBLIC_BASE_URL", "").rstrip("/"),
        # Chi è il proprietario del servizio (email separate da virgola). Con un proprietario
        # impostato la registrazione libera si chiude da sola: le aziende entrano su invito.
        OWNER_EMAILS={e.strip().lower() for e in os.environ.get("OWNER_EMAIL", "").split(",") if e.strip()},
        ALLOW_REGISTRATION=os.environ.get(
            "ALLOW_REGISTRATION", "0" if os.environ.get("OWNER_EMAIL", "").strip() else "1") == "1",
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
    def is_owner_account(shop):
        """L'account è quello del proprietario (a prescindere dallo stato): non si sospende né si elimina."""
        return bool(shop) and shop["email"].lower() in app.config["OWNER_EMAILS"]

    def is_owner(shop=None):
        shop = g.get("panel_shop") if shop is None else shop
        return is_owner_account(shop) and shop["status"] == "active"

    @app.before_request
    def load_user():
        g.panel_shop = None
        g.impersonator = None
        sid = session.get("shop_id")
        if not sid:
            return
        db = get_db()
        shop = db.execute("SELECT * FROM shops WHERE id = ?", (sid,)).fetchone()
        # Account sospeso, eliminato o ancora solo invitato: la sessione cade subito.
        if shop is None or shop["status"] != "active":
            session.clear()
            return
        imp = session.get("impersonator_id")
        if imp:  # il proprietario sta guardando il pannello di un'azienda
            owner = db.execute("SELECT * FROM shops WHERE id = ?", (imp,)).fetchone()
            if owner is None or not is_owner(owner) or is_owner_account(shop):
                session.clear()
                return
            g.impersonator = owner
        g.panel_shop = shop

    # Durante l'accesso come cliente non si toccano credenziali né si creano dati di prova.
    IMPERSONATION_BLOCKED = {"settings_password", "regenerate_key", "test_receipt"}

    @app.before_request
    def impersonation_guard():
        if g.get("impersonator") is not None and request.method == "POST" \
                and request.endpoint in IMPERSONATION_BLOCKED:
            return render_template("error.html", code=403,
                                   text="Questa azione non è consentita mentre guardi il pannello come "
                                        "amministratore."), 403
        return None

    @app.before_request
    def csrf_protect():
        if request.method == "POST" and request.path == "/dashboard/settings/logo":
            request.max_content_length = logo.BODY_LIMIT  # il resto dell'app accetta solo richieste piccole
        if request.method == "POST" and not request.path.startswith("/api/"):
            expected = session.get("csrf", "")
            if not expected or not secrets.compare_digest(expected, request.form.get("csrf", "")):
                abort(400)

    @app.context_processor
    def inject():
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(16)
        return dict(csrf_token=session["csrf"], me=g.get("panel_shop"),
                    allow_registration=app.config["ALLOW_REGISTRATION"],
                    impersonating=g.get("impersonator") is not None)

    @app.after_request
    def security_headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; form-action 'self'; frame-ancestors 'none'")
        if request.path.startswith(("/r/", "/dashboard", "/api/", "/admin", "/invito/")):
            resp.headers["Cache-Control"] = "no-store" if not request.path.endswith(".svg") else "private, max-age=3600"
        return resp

    @app.after_request
    def audit_impersonation(resp):
        """Ogni modifica fatta dal proprietario dentro il pannello di un cliente finisce nel registro."""
        if g.get("impersonator") is not None and request.method == "POST" and resp.status_code < 400 \
                and request.endpoint != "stop_impersonation":
            audit("impersonation.action", g.panel_shop, f"{request.method} {request.path}")
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
        if request.path.startswith("/api/"):
            return jsonify(error="richiesta troppo grande"), 413
        if request.path == "/dashboard/settings/logo":  # come ogni altro errore del modulo: messaggio e ritorno
            flash(f"Il file è troppo pesante: il massimo è {logo.MAX_UPLOAD // (1024 * 1024)} MB. "
                  "Prova con un'immagine più leggera.", "err")
            return redirect(url_for("settings", tab="locale"))
        return render_template("error.html", code=413, text="La richiesta è troppo grande."), 413

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
            if row["status"] != "active":
                raise ApiError("account sospeso: contatta il fornitore del servizio", 403)
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
            " s.review_url, s.footer_note, s.invoice_window_days,"
            " l.etag AS logo_etag, l.width AS logo_w, l.height AS logo_h"
            " FROM receipts r JOIN shops s ON s.id = r.shop_id"
            " LEFT JOIN shop_logos l ON l.shop_id = s.id WHERE r.token = ?", (token,)).fetchone()

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
                if row["status"] != "active":  # si dice solo dopo aver verificato la password
                    flash("Questo account è sospeso. Contatta il fornitore del servizio.", "err")
                    return render_template("login.html"), 403
                db = get_db()
                db.execute("UPDATE shops SET last_login_at = ? WHERE id = ?", (now_iso(), row["id"]))
                db.commit()
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

    # ------------------------------------------------------------ proprietario del servizio
    def audit(action, target=None, detail="", actor=None):
        """Scrive nel registro delle attività. actor: di norma il proprietario (anche se sta guardando un cliente)."""
        actor = actor or g.get("impersonator") or g.get("panel_shop")
        db = get_db()
        db.execute(
            "INSERT INTO admin_audit (actor_shop_id, actor_email, action, target_shop_id, target_name,"
            " detail, created_at) VALUES (?,?,?,?,?,?,?)",
            (actor["id"] if actor else None, actor["email"] if actor else "", action,
             target["id"] if target else None, target["name"] if target else "", detail[:300], now_iso()))
        db.commit()

    def owner_required(view):
        """Solo il proprietario. Per tutti gli altri la pagina non esiste (404): non si rivela che c'è."""
        @wraps(view)
        def wrapped(*a, **kw):
            if g.panel_shop is None:
                return redirect(url_for("login"))
            if g.get("impersonator") is not None or not is_owner(g.panel_shop):
                abort(404)
            return view(*a, **kw)
        return wrapped

    def _managed_shop(shop_id):
        """Azienda gestibile dal proprietario: esiste e non è il suo stesso account."""
        shop = get_db().execute("SELECT * FROM shops WHERE id = ?", (shop_id,)).fetchone()
        if shop is None or is_owner_account(shop):
            raise ValueError("azienda non gestibile")
        return shop

    def set_shop_status(shop_id, status):
        """Sospende (active -> suspended) o riattiva (suspended -> active). Chiamare dentro owner_required."""
        shop = _managed_shop(shop_id)
        if status == "suspended" and shop["status"] == "active":
            action = "company.suspend"
        elif status == "active" and shop["status"] == "suspended":
            action = "company.activate"
        else:
            raise ValueError("cambio di stato non consentito")
        db = get_db()
        db.execute("UPDATE shops SET status = ? WHERE id = ?", (status, shop_id))
        db.commit()
        audit(action, shop)

    def delete_shop(shop_id):
        """Elimina l'azienda e TUTTI i suoi dati (scontrini, fatture, contatti, messaggi). Irreversibile."""
        shop = _managed_shop(shop_id)
        audit("company.delete", shop)
        db = get_db()
        db.execute("DELETE FROM shops WHERE id = ?", (shop_id,))
        db.commit()

    def begin_impersonation(shop_id):
        """Il proprietario entra nel pannello di un'azienda attiva. Registrato nel registro attività."""
        if not is_owner(g.panel_shop) or g.get("impersonator") is not None:
            abort(404)
        try:
            target = _managed_shop(shop_id)
        except ValueError:
            abort(404)
        if target["status"] != "active":
            abort(404)
        audit("impersonation.start", target)
        session["impersonator_id"] = g.panel_shop["id"]
        session["shop_id"] = target["id"]

    @app.post("/admin/stop-impersonation")
    def stop_impersonation():
        if g.get("impersonator") is None:
            abort(404)
        target = g.panel_shop
        audit("impersonation.stop", target)
        session["shop_id"] = g.impersonator["id"]
        session.pop("impersonator_id", None)
        return redirect(f"/admin/companies/{target['id']}")

    def invite_url(token):
        base = app.config["PUBLIC_BASE_URL"] or request.host_url.rstrip("/")
        return f"{base}/invito/{token}"

    @app.route("/invito/<token>", methods=["GET", "POST"])
    def invite(token):
        """Pagina pubblica: l'azienda sceglie la propria password con il link ricevuto (monouso, scade)."""
        if rate_limited(f"invite:{request.remote_addr}", 30, 3600, shared=True):
            return render_template("error.html", code=429, text="Troppi tentativi. Riprova più tardi."), 429
        db = get_db()
        shop = db.execute("SELECT * FROM shops WHERE invite_hash = ?", (hash_token(token),)).fetchone()
        if shop is None or shop["status"] == "suspended" or not shop["invite_expires_at"] \
                or parse_iso(shop["invite_expires_at"]) < now_utc():
            return render_template("error.html", code=404, text="Questo invito non è valido o è scaduto. "
                                   "Chiedi un nuovo link a chi te l'ha inviato."), 404
        if request.method == "POST":
            password, again = request.form.get("password", ""), request.form.get("password2", "")
            if len(password) < 8:
                flash("La password deve avere almeno 8 caratteri.", "err")
            elif password != again:
                flash("Le due password non coincidono.", "err")
            else:
                first_access = shop["status"] == "invited"
                db.execute("UPDATE shops SET password_hash = ?, status = 'active', invite_hash = NULL,"
                           " invite_expires_at = NULL, last_login_at = ? WHERE id = ?",
                           (generate_password_hash(password), now_iso(), shop["id"]))
                key = None
                if first_access:  # la chiave API vera nasce ora e si vede una volta sola
                    key, key_hash, prefix = new_api_key()
                    db.execute("UPDATE shops SET api_key_hash = ?, api_key_prefix = ? WHERE id = ?",
                               (key_hash, prefix, shop["id"]))
                db.commit()
                audit("company.invite_accepted" if first_access else "company.password_reset",
                      shop, actor=shop)
                session.clear()
                session["shop_id"] = shop["id"]
                if key:
                    session["new_api_key"] = key
                session.permanent = True
                return redirect(url_for("settings"))
        return render_template("invite.html", shop=shop, first_access=shop["status"] == "invited")

    # ------------------------------------------------------------ pannello (sezioni)
    # Le pagine del pannello vivono in sections/: una cartella di moduli, uno per sezione.
    sections.register_all(app, SimpleNamespace(
        get_db=get_db, login_required=login_required, public_url=public_url, euro=euro,
        fmt_dt=fmt_dt, fmt_qty=fmt_qty, now_iso=now_iso, now_utc=now_utc, parse_iso=parse_iso,
        LOCAL_TZ=LOCAL_TZ, csv_safe=csv_safe, valid_piva=valid_piva, valid_email=valid_email,
        new_api_key=new_api_key, parse_items=parse_items, insert_receipt=insert_receipt,
        vat_breakdown=vat_breakdown, messaging=messaging,
        # proprietario del servizio (vedi DESIGN.md, «Amministrazione»)
        is_owner=is_owner, owner_required=owner_required, audit=audit, invite_url=invite_url,
        create_invited_shop=create_invited_shop, issue_invite=issue_invite, set_shop_status=set_shop_status,
        delete_shop=delete_shop, begin_impersonation=begin_impersonation, INVITE_DAYS=INVITE_DAYS))

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)),
            debug=os.environ.get("FLASK_DEBUG") == "1")
