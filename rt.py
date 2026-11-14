"""Collegamento al Registratore Telematico (RT) tramite il «ponte» installato nel locale.

Perché un ponte: l'RT sta nella rete del locale (es. 192.168.1.50), il server è su internet e non può
raggiungerlo. Il ponte (public/static/rt_bridge.py) gira su un PC del locale, chiama il server (solo
connessioni in uscita), prende i documenti da emettere e li manda all'RT.

Regola d'oro: un documento fiscale emesso non si «annulla» da qui. Quindi un documento che potrebbe essere
già uscito dall'RT (stato `uncertain`) non viene MAI rimandato in automatico.
"""
import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone

import pos_service as pos

DEFAULT_DEPTS = {"22": 1, "10": 2, "5": 3, "4": 4, "0": 5}
# Epson: 0 contanti, 2 carta di credito/debito, 3 buono pasto. «altro» non ha un equivalente sicuro.
PAYMENT_TYPES = {"contanti": 0, "carta": 2, "buoni": 3}
STALE_PRINTING_SECONDS = 120
MAX_AUTO_RETRIES = 5
RETRY_DELAY_SECONDS = 10
STATUS_LABELS = {"queued": "In coda", "printing": "In stampa", "done": "Emesso", "failed": "Rifiutato dal registratore",
                 "uncertain": "Da controllare"}


class RtError(pos.PosError):
    pass


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


# ------------------------------------------------------------------ impostazioni
def get_settings(db, shop_id):
    return db.execute("SELECT * FROM rt_settings WHERE shop_id = ?", (shop_id,)).fetchone()


def is_enabled(db, shop_id):
    s = get_settings(db, shop_id)
    return bool(s and s["enabled"])


def dept_map(settings):
    try:
        m = json.loads(settings["dept_map"])
    except (TypeError, ValueError):
        m = {}
    return {**DEFAULT_DEPTS, **{str(k): int(v) for k, v in m.items() if str(v).isdigit()}}


def enable(db, shop_id):
    """Attiva il collegamento (o rigenera il token). Restituisce il token in chiaro: si vede UNA volta."""
    token = "rt_" + secrets.token_urlsafe(24)
    now = pos.now_iso()
    if get_settings(db, shop_id):
        db.execute("UPDATE rt_settings SET enabled = 1, token_hash = ?, token_prefix = ?, updated_at = ? WHERE shop_id = ?",
                   (_hash(token), token[:8], now, shop_id))
    else:
        db.execute("INSERT INTO rt_settings (shop_id, enabled, token_hash, token_prefix, updated_at) VALUES (?,?,?,?,?)",
                   (shop_id, 1, _hash(token), token[:8], now))
    db.commit()
    return token


def disable(db, shop_id):
    db.execute("UPDATE rt_settings SET enabled = 0, token_hash = NULL, updated_at = ? WHERE shop_id = ?",
               (pos.now_iso(), shop_id))
    db.commit()


def set_departments(db, shop_id, mapping):
    clean = {}
    for rate in DEFAULT_DEPTS:
        try:
            d = int(mapping.get(rate))
        except (TypeError, ValueError):
            raise RtError("Il reparto va scritto come numero (1-99).")
        if not 1 <= d <= 99:
            raise RtError("Il reparto va da 1 a 99.")
        clean[rate] = d
    if not get_settings(db, shop_id):
        raise RtError("Attiva prima il collegamento.")
    db.execute("UPDATE rt_settings SET dept_map = ?, updated_at = ? WHERE shop_id = ?",
               (json.dumps(clean), pos.now_iso(), shop_id))
    db.commit()


def shop_by_token(db, token):
    if not token or len(token) > 100:
        return None
    return db.execute("SELECT s.*, r.dept_map FROM rt_settings r JOIN shops s ON s.id = r.shop_id"
                      " WHERE r.token_hash = ? AND r.enabled = 1 AND s.status = 'active'", (_hash(token),)).fetchone()


def touch(db, shop_id):
    db.execute("UPDATE rt_settings SET last_seen = ? WHERE shop_id = ?", (pos.now_iso(), shop_id))
    db.commit()


# ------------------------------------------------------------------ documenti
def build_payload(db, shop_id, order):
    """Cosa deve emettere l'RT: righe (sconto già incluso) e incassi. Importi in centesimi."""
    s = get_settings(db, shop_id)
    depts = dept_map(s) if s else dict(DEFAULT_DEPTS)
    lines = []
    for ln in pos._receipt_items(db, order):
        key = str(int(ln["vat_rate"]))
        if key not in depts:
            raise RtError(f"Aliquota IVA {key}% non gestita dal collegamento.")
        qty = int(ln["qty"])
        # una riga per prodotto, importo totale esatto (niente arrotondamenti su prezzi unitari)
        lines.append({"description": f"{qty}x {ln['name']}"[:38], "amount_cents": ln["line_total_cents"],
                      "vat_rate": ln["vat_rate"], "department": depts[key]})
    pays = db.execute("SELECT method, SUM(amount_cents) a FROM pos_payments WHERE order_id = ? GROUP BY method"
                      " ORDER BY MIN(id)", (order["id"],)).fetchall()
    payments = []
    for p in pays:
        if p["method"] not in PAYMENT_TYPES:
            raise RtError("Il pagamento «altro» non si può inviare al registratore: emetti il documento a mano.")
        payments.append({"method": p["method"], "type": PAYMENT_TYPES[p["method"]], "amount_cents": p["a"]})
    total = sum(ln["amount_cents"] for ln in lines)
    if not lines or total <= 0 or total != sum(p["amount_cents"] for p in payments):
        raise RtError("Righe e incassi non coincidono: documento non inviabile.")
    return {"order_id": order["id"], "table": order["table_label"], "lines": lines, "payments": payments,
            "total_cents": total}


def enqueue(db, shop_id, order_id):
    """Mette il conto pagato in coda per l'RT. Idempotente: se c'è già un documento attivo/emesso non ne crea un altro."""
    if not is_enabled(db, shop_id):
        raise RtError("Il collegamento al registratore non è attivo.")
    o = pos.get_order(db, shop_id, order_id)
    if o["status"] != "paid" or o["fiscal_status"] != "da_emettere":
        raise RtError("Il conto non è in attesa di documento fiscale.", 409)
    live = db.execute("SELECT id FROM rt_jobs WHERE order_id = ? AND status <> 'failed'", (order_id,)).fetchone()
    if live:
        return live["id"]
    now = pos.now_iso()
    try:
        jid = db.insert("INSERT INTO rt_jobs (shop_id, order_id, payload, created_at, updated_at) VALUES (?,?,?,?,?)",
                        (shop_id, order_id, json.dumps(build_payload(db, shop_id, o)), now, now))
    except Exception as e:  # corsa: un altro processo l'ha appena creato
        db.rollback()
        if "unique" in str(e).lower():
            return db.execute("SELECT id FROM rt_jobs WHERE order_id = ? AND status <> 'failed'", (order_id,)).fetchone()["id"]
        raise
    db.commit()
    return jid


def enqueue_if_enabled(db, shop_id, order_id):
    """Per la cassa alla chiusura del conto: mai bloccare la vendita se il collegamento non va."""
    try:
        return enqueue(db, shop_id, order_id) if is_enabled(db, shop_id) else None
    except pos.PosError:
        db.rollback()
        return None


def sweep(db, shop_id):
    """Un documento «in stampa» da troppo senza risposta diventa «da controllare» (non si rimanda da solo)."""
    limit = _iso(_now() - timedelta(seconds=STALE_PRINTING_SECONDS))
    db.execute("UPDATE rt_jobs SET status = 'uncertain', error = ?, updated_at = ? WHERE shop_id = ?"
               " AND status = 'printing' AND updated_at < ?",
               ("Il ponte non ha risposto: controlla sul registratore se il documento è uscito.", pos.now_iso(), shop_id, limit))
    db.commit()


def claim_next(db, shop_id):
    """Il ponte prende il prossimo documento. Lo segna «in stampa» PRIMA di mandarlo all'RT."""
    sweep(db, shop_id)
    ready = _iso(_now() - timedelta(seconds=RETRY_DELAY_SECONDS))
    for j in db.execute("SELECT * FROM rt_jobs WHERE shop_id = ? AND status = 'queued' ORDER BY id", (shop_id,)).fetchall():
        if j["attempts"] and j["updated_at"] > ready:
            continue
        cur = db.execute("UPDATE rt_jobs SET status = 'printing', attempts = attempts + 1, updated_at = ?"
                         " WHERE id = ? AND status = 'queued'", (pos.now_iso(), j["id"]))
        db.commit()
        if getattr(cur, "rowcount", 1) == 1:
            return {"id": j["id"], "payload": json.loads(j["payload"]), "attempt": j["attempts"] + 1}
    return None


def report(db, shop_id, job_id, outcome, number="", error=""):
    """Esito dal ponte: issued | rejected | not_sent | unknown."""
    j = db.execute("SELECT * FROM rt_jobs WHERE id = ? AND shop_id = ?", (job_id, shop_id)).fetchone()
    if not j:
        raise RtError("Documento non trovato.", 404)
    if j["status"] == "done":
        return "done"  # risposta ripetuta: già registrato
    if outcome == "issued":
        number = str(number or "").strip()[:40] or "n.d."
        db.execute("UPDATE rt_jobs SET status = 'done', number = ?, error = '', updated_at = ? WHERE id = ?",
                   (number, pos.now_iso(), job_id))
        db.commit()
        o = pos.get_order(db, shop_id, j["order_id"])
        if o["fiscal_status"] != "emesso":
            pos.mark_fiscal_issued(db, shop_id, j["order_id"], number)
        return "done"
    if outcome == "not_sent":
        status = "queued" if j["attempts"] < MAX_AUTO_RETRIES else "failed"
        err = (str(error)[:300] or "Registratore non raggiungibile.")
    elif outcome == "rejected":
        status, err = "failed", str(error)[:300] or "Il registratore ha rifiutato il documento."
    elif outcome == "unknown":
        status, err = "uncertain", str(error)[:300] or "Nessuna risposta chiara dal registratore."
    else:
        raise RtError("Esito non valido.")
    db.execute("UPDATE rt_jobs SET status = ?, error = ?, updated_at = ? WHERE id = ?",
               (status, err, pos.now_iso(), job_id))
    db.commit()
    return status


def retry(db, shop_id, job_id):
    """Riprova a mano un documento rifiutato o da controllare (l'utente ha verificato che NON sia uscito)."""
    j = db.execute("SELECT * FROM rt_jobs WHERE id = ? AND shop_id = ?", (job_id, shop_id)).fetchone()
    if not j or j["status"] not in ("failed", "uncertain"):
        raise RtError("Questo documento non si può rimandare.", 409)
    o = pos.get_order(db, shop_id, j["order_id"])
    if o["fiscal_status"] == "emesso":
        raise RtError("Il documento risulta già emesso.", 409)
    # il vecchio tentativo resta nello storico come «failed»; ne nasce uno nuovo (indice: un solo attivo per conto)
    db.execute("UPDATE rt_jobs SET status = 'failed', updated_at = ? WHERE id = ?", (pos.now_iso(), job_id))
    now = pos.now_iso()
    db.insert("INSERT INTO rt_jobs (shop_id, order_id, payload, created_at, updated_at) VALUES (?,?,?,?,?)",
              (shop_id, j["order_id"], json.dumps(build_payload(db, shop_id, o)), now, now))
    db.commit()


def jobs_for_panel(db, shop_id, limit=25):
    sweep(db, shop_id)
    return db.execute("SELECT j.*, o.table_label FROM rt_jobs j JOIN pos_orders o ON o.id = j.order_id"
                      " WHERE j.shop_id = ? ORDER BY j.id DESC LIMIT ?", (shop_id, limit)).fetchall()


def job_status_for_order(db, order_id):
    r = db.execute("SELECT status, error FROM rt_jobs WHERE order_id = ? ORDER BY id DESC LIMIT 1", (order_id,)).fetchone()
    return (r["status"], r["error"]) if r else (None, "")
