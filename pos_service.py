"""Cassa (POS): logica del conto di sala. Nessuna dipendenza da Flask.

Flusso:  open (si ordina) -> preconto (conto al cliente, NON fiscale) -> il cliente verifica e
conferma -> pagamento -> chiusura: nasce lo scontrino digitale e il conto passa a «da emettere»
(documento commerciale fiscale vero, vedi fiscal.py).

Tutte le funzioni ricevono `db` (db.Database) e `shop_id`, filtrano SEMPRE per shop_id e
fanno commit da sole. Gli errori attesi sono PosError (messaggio in italiano, pronto da mostrare).
"""
import secrets
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

METHODS = ("contanti", "carta", "buoni", "altro")
VAT_RATES = (0, 4, 5, 10, 22)
MAX_QTY = 99


class PosError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cents(x):
    return int(Decimal(x).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def line_total(qty, unit):
    return qty * unit


def vat_of(gross, rate):
    """IVA contenuta in un importo lordo."""
    return _cents(Decimal(gross) - Decimal(gross) / (1 + Decimal(str(rate)) / 100))


# ------------------------------------------------------------------ menu e sala
def _clean_name(name, what="nome", limit=80):
    name = str(name or "").strip()[:limit]
    if not name:
        raise PosError(f"Manca il {what}.")
    return name


def _price(price_cents):
    try:
        p = int(price_cents)
    except (TypeError, ValueError):
        raise PosError("Prezzo non valido.")
    if not 0 <= p <= 1_000_000:
        raise PosError("Prezzo fuori intervallo.")
    return p


def add_category(db, shop_id, name):
    cid = db.insert("INSERT INTO pos_categories (shop_id, name, sort) VALUES (?,?,"
                    "(SELECT COALESCE(MAX(sort),0)+1 FROM pos_categories WHERE shop_id = ?))",
                    (shop_id, _clean_name(name), shop_id))
    db.commit()
    return cid


def add_product(db, shop_id, category_id, name, price_cents, vat_rate=10):
    if float(vat_rate) not in VAT_RATES:
        raise PosError("Aliquota IVA non ammessa (0, 4, 5, 10, 22).")
    if category_id is not None:
        ok = db.execute("SELECT 1 FROM pos_categories WHERE id = ? AND shop_id = ?",
                        (category_id, shop_id)).fetchone()
        if not ok:
            raise PosError("Categoria non trovata.", 404)
    pid = db.insert("INSERT INTO pos_products (shop_id, category_id, name, price_cents, vat_rate, sort)"
                    " VALUES (?,?,?,?,?,(SELECT COALESCE(MAX(sort),0)+1 FROM pos_products WHERE shop_id = ?))",
                    (shop_id, category_id, _clean_name(name), _price(price_cents), float(vat_rate), shop_id))
    db.commit()
    return pid


def add_table(db, shop_id, label, area="", seats=2):
    label = _clean_name(label, "nome del tavolo", 30)
    try:
        seats = max(1, min(int(seats), 50))
    except (TypeError, ValueError):
        seats = 2
    try:
        tid = db.insert("INSERT INTO pos_tables (shop_id, label, area, seats, sort) VALUES (?,?,?,?,"
                        "(SELECT COALESCE(MAX(sort),0)+1 FROM pos_tables WHERE shop_id = ?))",
                        (shop_id, label, str(area or "").strip()[:30], seats, shop_id))
    except Exception as e:  # tavolo già esistente (UNIQUE)
        db.rollback()
        if "unique" in str(e).lower():
            raise PosError(f"Esiste già un tavolo «{label}».")
        raise
    db.commit()
    return tid


def seed_default_menu(db, shop_id):
    """Menu e sala di partenza per un ristorante nuovo (si modificano poi dal pannello)."""
    if db.execute("SELECT 1 FROM pos_products WHERE shop_id = ?", (shop_id,)).fetchone():
        return False
    menu = {
        "Antipasti": [("Bruschetta", 600, 10), ("Tagliere misto", 1400, 10)],
        "Primi": [("Spaghetti al pomodoro", 900, 10), ("Lasagna", 1100, 10)],
        "Secondi": [("Cotoletta", 1500, 10), ("Tagliata", 1800, 10)],
        "Pizze": [("Margherita", 800, 10), ("Diavola", 950, 10)],
        "Dolci": [("Tiramisù", 600, 10), ("Panna cotta", 550, 10)],
        "Bevande": [("Acqua 0,75", 250, 10), ("Birra media", 500, 22), ("Calice di vino", 500, 22),
                    ("Caffè", 150, 10)],
    }
    for cat, prods in menu.items():
        cid = add_category(db, shop_id, cat)
        for name, price, vat in prods:
            add_product(db, shop_id, cid, name, price, vat)
    for n in range(1, 9):
        add_table(db, shop_id, f"Tavolo {n}", "Sala", 4)
    add_table(db, shop_id, "Banco", "", 1)
    return True


# ------------------------------------------------------------------ lettura
def get_order(db, shop_id, order_id):
    o = db.execute("SELECT * FROM pos_orders WHERE id = ? AND shop_id = ?", (order_id, shop_id)).fetchone()
    if not o:
        raise PosError("Conto non trovato.", 404)
    return o


def order_items(db, order_id):
    return db.execute("SELECT * FROM pos_order_items WHERE order_id = ? ORDER BY id", (order_id,)).fetchall()


def paid_cents(db, order_id):
    return db.execute("SELECT COALESCE(SUM(amount_cents),0) FROM pos_payments WHERE order_id = ?",
                      (order_id,)).fetchone()[0]


def totals(db, order):
    items = order_items(db, order["id"])
    gross = sum(i["qty"] * i["unit_price_cents"] for i in items)
    discount = min(order["discount_cents"], gross)
    total = gross - discount
    paid = paid_cents(db, order["id"])
    return {"gross": gross, "discount": discount, "total": total, "paid": paid,
            "due": max(0, total - paid), "items": len(items), "qty": sum(i["qty"] for i in items)}


def open_order_for_table(db, shop_id, table_id):
    return db.execute("SELECT * FROM pos_orders WHERE shop_id = ? AND table_id = ? AND status IN ('open','preconto')"
                      " ORDER BY id DESC", (shop_id, table_id)).fetchone()


# ------------------------------------------------------------------ conto
def open_order(db, shop_id, table_id, covers=0, opened_by=""):
    """Apre il conto di un tavolo (o restituisce quello già aperto)."""
    t = db.execute("SELECT * FROM pos_tables WHERE id = ? AND shop_id = ? AND active = 1",
                   (table_id, shop_id)).fetchone()
    if not t:
        raise PosError("Tavolo non trovato.", 404)
    existing = open_order_for_table(db, shop_id, table_id)
    if existing:
        return existing["id"]
    try:
        covers = max(0, min(int(covers or 0), 99))
    except (TypeError, ValueError):
        covers = 0
    shift = db.execute("SELECT id FROM pos_shifts WHERE shop_id = ? AND closed_at IS NULL ORDER BY id DESC",
                       (shop_id,)).fetchone()
    oid = db.insert("INSERT INTO pos_orders (shop_id, table_id, table_label, covers, opened_by, shift_id, created_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (shop_id, table_id, t["label"], covers, str(opened_by)[:40], shift[0] if shift else None, now_iso()))
    db.commit()
    return oid


def _editable(order):
    if order["status"] != "open":
        raise PosError("Il conto è già stato presentato al cliente: riaprilo per modificarlo.", 409)


def add_item(db, shop_id, order_id, product_id, qty=1, note=""):
    o = get_order(db, shop_id, order_id)
    _editable(o)
    p = db.execute("SELECT * FROM pos_products WHERE id = ? AND shop_id = ? AND active = 1",
                   (product_id, shop_id)).fetchone()
    if not p:
        raise PosError("Prodotto non trovato.", 404)
    qty = _qty(qty)
    note = str(note or "").strip()[:80]
    # stesso prodotto senza nota: si somma alla riga esistente
    same = db.execute("SELECT id, qty FROM pos_order_items WHERE order_id = ? AND product_id = ? AND note = ?"
                      " AND unit_price_cents = ? AND sent_at IS NULL", (order_id, product_id, note, p["price_cents"])).fetchone()
    if same:
        if same["qty"] + qty > MAX_QTY:
            raise PosError(f"Massimo {MAX_QTY} per riga.")
        db.execute("UPDATE pos_order_items SET qty = qty + ? WHERE id = ?", (qty, same["id"]))
        item_id = same["id"]
    else:
        item_id = db.insert("INSERT INTO pos_order_items (order_id, product_id, name, qty, unit_price_cents,"
                            " vat_rate, note, created_at) VALUES (?,?,?,?,?,?,?,?)",
                            (order_id, product_id, p["name"], qty, p["price_cents"], p["vat_rate"], note, now_iso()))
    db.commit()
    return item_id


def add_custom_item(db, shop_id, order_id, name, price_cents, vat_rate=10, qty=1):
    """Riga libera (fuori menu)."""
    o = get_order(db, shop_id, order_id)
    _editable(o)
    if float(vat_rate) not in VAT_RATES:
        raise PosError("Aliquota IVA non ammessa.")
    item_id = db.insert("INSERT INTO pos_order_items (order_id, name, qty, unit_price_cents, vat_rate, note, created_at)"
                        " VALUES (?,?,?,?,?,?,?)",
                        (order_id, _clean_name(name), _qty(qty), _price(price_cents), float(vat_rate), "", now_iso()))
    db.commit()
    return item_id


def _qty(q):
    try:
        q = int(q)
    except (TypeError, ValueError):
        raise PosError("Quantità non valida.")
    if not 1 <= q <= MAX_QTY:
        raise PosError(f"La quantità deve essere tra 1 e {MAX_QTY}.")
    return q


def set_item_qty(db, shop_id, order_id, item_id, qty):
    """qty 0 = toglie la riga."""
    o = get_order(db, shop_id, order_id)
    _editable(o)
    it = db.execute("SELECT id FROM pos_order_items WHERE id = ? AND order_id = ?", (item_id, order_id)).fetchone()
    if not it:
        raise PosError("Riga non trovata.", 404)
    try:
        qty = int(qty)
    except (TypeError, ValueError):
        raise PosError("Quantità non valida.")
    if qty <= 0:
        db.execute("DELETE FROM pos_order_items WHERE id = ?", (item_id,))
    else:
        db.execute("UPDATE pos_order_items SET qty = ? WHERE id = ?", (_qty(qty), item_id))
    db.commit()


def set_discount(db, shop_id, order_id, discount_cents):
    o = get_order(db, shop_id, order_id)
    _editable(o)
    try:
        d = int(discount_cents)
    except (TypeError, ValueError):
        raise PosError("Sconto non valido.")
    gross = totals(db, o)["gross"]
    if not 0 <= d <= gross:
        raise PosError("Lo sconto non può superare il totale del conto.")
    db.execute("UPDATE pos_orders SET discount_cents = ? WHERE id = ?", (d, order_id))
    db.commit()


def set_covers(db, shop_id, order_id, covers):
    get_order(db, shop_id, order_id)
    try:
        c = max(0, min(int(covers), 99))
    except (TypeError, ValueError):
        raise PosError("Numero coperti non valido.")
    db.execute("UPDATE pos_orders SET covers = ? WHERE id = ?", (c, order_id))
    db.commit()


def cancel_order(db, shop_id, order_id):
    o = get_order(db, shop_id, order_id)
    if o["status"] in ("paid", "void"):
        raise PosError("Questo conto è già chiuso.", 409)
    if paid_cents(db, order_id):
        raise PosError("Ci sono già pagamenti registrati: non si può annullare il conto.", 409)
    db.execute("UPDATE pos_orders SET status = 'void', closed_at = ? WHERE id = ?", (now_iso(), order_id))
    db.commit()


def move_order(db, shop_id, order_id, table_id):
    """Sposta il conto su un altro tavolo libero."""
    o = get_order(db, shop_id, order_id)
    if o["status"] in ("paid", "void"):
        raise PosError("Questo conto è già chiuso.", 409)
    t = db.execute("SELECT * FROM pos_tables WHERE id = ? AND shop_id = ? AND active = 1",
                   (table_id, shop_id)).fetchone()
    if not t:
        raise PosError("Tavolo non trovato.", 404)
    if open_order_for_table(db, shop_id, table_id):
        raise PosError("Quel tavolo ha già un conto aperto.", 409)
    db.execute("UPDATE pos_orders SET table_id = ?, table_label = ? WHERE id = ?", (table_id, t["label"], order_id))
    db.commit()


# ------------------------------------------------------------------ comanda (cucina)
def send_to_kitchen(db, shop_id, order_id):
    """Segna come inviate le righe non ancora in cucina. Ritorna il codice della comanda (per stamparla)."""
    o = get_order(db, shop_id, order_id)
    _editable(o)
    n = db.execute("SELECT COUNT(*) FROM pos_order_items WHERE order_id = ? AND sent_at IS NULL", (order_id,)).fetchone()[0]
    if not n:
        raise PosError("Non ci sono righe nuove da mandare in cucina.")
    batch = now_iso()
    db.execute("UPDATE pos_order_items SET sent_at = ? WHERE order_id = ? AND sent_at IS NULL", (batch, order_id))
    db.commit()
    return batch


def comanda_items(db, shop_id, order_id, batch):
    get_order(db, shop_id, order_id)
    return db.execute("SELECT * FROM pos_order_items WHERE order_id = ? AND sent_at = ? ORDER BY id",
                      (order_id, batch)).fetchall()


# ------------------------------------------------------------------ personale con PIN
ROLES = {"cameriere": "Cameriere", "cassiere": "Cassiere", "titolare": "Titolare"}


def _valid_pin(pin):
    pin = str(pin or "").strip()
    if not (pin.isdigit() and 4 <= len(pin) <= 6):
        raise PosError("Il PIN deve essere di 4-6 cifre.")
    return pin


def add_staff(db, shop_id, name, pin, role="cameriere"):
    from werkzeug.security import generate_password_hash
    if role not in ROLES:
        raise PosError("Ruolo non valido.")
    sid = db.insert("INSERT INTO pos_staff (shop_id, name, pin_hash, role, created_at) VALUES (?,?,?,?,?)",
                    (shop_id, _clean_name(name, "nome", 40), generate_password_hash(_valid_pin(pin)), role, now_iso()))
    db.commit()
    return sid


def set_staff_pin(db, shop_id, staff_id, pin):
    from werkzeug.security import generate_password_hash
    db.execute("UPDATE pos_staff SET pin_hash = ? WHERE id = ? AND shop_id = ?",
               (generate_password_hash(_valid_pin(pin)), staff_id, shop_id))
    db.commit()


def staff_mode(db, shop_id):
    """True se l'azienda ha almeno una persona attiva: da quel momento la cassa chiede il PIN."""
    return bool(db.execute("SELECT 1 FROM pos_staff WHERE shop_id = ? AND active = 1", (shop_id,)).fetchone())


def check_pin(db, shop_id, staff_id, pin):
    from werkzeug.security import check_password_hash
    st = db.execute("SELECT * FROM pos_staff WHERE id = ? AND shop_id = ? AND active = 1", (staff_id, shop_id)).fetchone()
    if st and check_password_hash(st["pin_hash"], str(pin or "")):
        return st
    return None


# ------------------------------------------------------------------ preconto e verifica del cliente
def request_bill(db, shop_id, order_id):
    """Presenta il preconto: da qui il conto non si modifica e il cliente può verificarlo."""
    o = get_order(db, shop_id, order_id)
    if o["status"] == "preconto":
        return o["bill_token"]
    if o["status"] != "open":
        raise PosError("Questo conto è già chiuso.", 409)
    if not order_items(db, order_id):
        raise PosError("Il conto è vuoto.")
    token = o["bill_token"] or secrets.token_urlsafe(12)
    db.execute("UPDATE pos_orders SET status = 'preconto', bill_token = ?, bill_requested_at = ?,"
               " customer_status = 'pending', customer_note = '', customer_at = NULL, confirmed_by = ''"
               " WHERE id = ?", (token, now_iso(), order_id))
    db.commit()
    return token


def reopen_order(db, shop_id, order_id):
    """Torna in modifica (es. il cliente ha segnalato un errore). Il link del preconto non vale più."""
    o = get_order(db, shop_id, order_id)
    if o["status"] != "preconto":
        raise PosError("Il conto non è in fase di verifica.", 409)
    if paid_cents(db, order_id):
        raise PosError("Ci sono già pagamenti registrati: non si può modificare il conto.", 409)
    db.execute("UPDATE pos_orders SET status = 'open', customer_status = 'none', customer_note = '',"
               " customer_at = NULL, confirmed_by = '' WHERE id = ?", (order_id,))
    db.commit()


def order_by_bill_token(db, token):
    if not token or len(token) > 64:
        return None
    return db.execute("SELECT * FROM pos_orders WHERE bill_token = ?", (token,)).fetchone()


def customer_reply(db, token, ok, note=""):
    """Il cliente, dalla pagina pubblica del preconto, conferma oppure segnala un errore."""
    o = order_by_bill_token(db, token)
    if not o:
        raise PosError("Preconto non trovato.", 404)
    if o["status"] != "preconto":
        raise PosError("Questo conto non è più in attesa di verifica.", 409)
    status = "confirmed" if ok else "disputed"
    db.execute("UPDATE pos_orders SET customer_status = ?, customer_note = ?, customer_at = ?, confirmed_by = ?"
               " WHERE id = ?", (status, str(note or "").strip()[:300], now_iso(), "cliente" if ok else "", o["id"]))
    db.commit()
    return status


def staff_confirm(db, shop_id, order_id, who="staff"):
    """Il cliente ha verificato a voce: lo conferma il personale."""
    o = get_order(db, shop_id, order_id)
    if o["status"] != "preconto":
        raise PosError("Presenta prima il preconto.", 409)
    db.execute("UPDATE pos_orders SET customer_status = 'confirmed', customer_at = ?, confirmed_by = ? WHERE id = ?",
               (now_iso(), str(who)[:40] or "staff", order_id))
    db.commit()


def set_delivery(db, shop_id, order_id, pref, by="staff"):
    """Come il cliente vuole il documento: «digital» = accordo a riceverlo solo in forma elettronica
    (DM 7/12/2016 art. 1 c. 4: «previo accordo con il destinatario»), «paper» = vuole la carta."""
    o = get_order(db, shop_id, order_id)
    if o["status"] not in ("preconto", "paid"):
        raise PosError("Presenta prima il preconto.", 409)
    _set_delivery(db, o["id"], pref, by)


def customer_set_delivery(db, token, pref):
    o = order_by_bill_token(db, token)
    if not o or o["status"] not in ("preconto", "paid"):
        raise PosError("Preconto non trovato.", 404)
    _set_delivery(db, o["id"], pref, "cliente")


def _set_delivery(db, order_id, pref, by):
    if pref not in ("digital", "paper"):
        raise PosError("Scelta non valida.")
    db.execute("UPDATE pos_orders SET delivery_pref = ?, delivery_by = ?, delivery_at = ? WHERE id = ?",
               (pref, str(by)[:40], now_iso(), order_id))
    db.commit()


# ------------------------------------------------------------------ pagamento e chiusura
def add_payment(db, shop_id, order_id, method, amount_cents, tendered_cents=None):
    """Registra un incasso (anche parziale: conto alla romana). Restituisce il resto da dare."""
    o = get_order(db, shop_id, order_id)
    if o["status"] != "preconto":
        raise PosError("Presenta il preconto prima di incassare.", 409)
    if o["customer_status"] != "confirmed":
        raise PosError("Il cliente non ha ancora confermato il conto.", 409)
    if method not in METHODS:
        raise PosError("Metodo di pagamento non valido.")
    try:
        amount = int(amount_cents)
    except (TypeError, ValueError):
        raise PosError("Importo non valido.")
    t = totals(db, o)
    if amount <= 0:
        raise PosError("Importo non valido.")
    if amount > t["due"]:
        raise PosError("L'importo supera quanto resta da pagare.")
    change = 0
    tendered = amount
    if method == "contanti" and tendered_cents not in (None, ""):
        try:
            tendered = int(tendered_cents)
        except (TypeError, ValueError):
            raise PosError("Contante ricevuto non valido.")
        if tendered < amount:
            raise PosError("Il contante ricevuto è meno dell'importo.")
        change = tendered - amount
    db.insert("INSERT INTO pos_payments (order_id, method, amount_cents, tendered_cents, created_at) VALUES (?,?,?,?,?)",
              (order_id, method, amount, tendered, now_iso()))
    db.commit()
    return change


def remove_payment(db, shop_id, order_id, payment_id):
    o = get_order(db, shop_id, order_id)
    if o["status"] != "preconto":
        raise PosError("Il conto è chiuso.", 409)
    db.execute("DELETE FROM pos_payments WHERE id = ? AND order_id = ?", (payment_id, order_id))
    db.commit()


def _receipt_items(db, order):
    """Righe dello scontrino. Lo sconto è ripartito sulle righe in proporzione (resti all'ultima)."""
    items = order_items(db, order["id"])
    gross = sum(i["qty"] * i["unit_price_cents"] for i in items)
    discount = min(order["discount_cents"], gross)
    lines, left = [], discount
    for n, i in enumerate(items):
        line = i["qty"] * i["unit_price_cents"]
        share = left if n == len(items) - 1 else min(left, _cents(Decimal(discount) * line / gross)) if gross else 0
        left -= share
        net = line - share
        name = i["name"] + (f" ({i['note']})" if i["note"] else "")
        lines.append(dict(name=name[:120], qty=float(i["qty"]),
                          unit_price_cents=i["unit_price_cents"], vat_rate=float(i["vat_rate"]),
                          line_total_cents=net, vat_cents=vat_of(net, i["vat_rate"])))
    return lines


def close_order(db, shop_id, order_id, insert_receipt):
    """Chiude un conto interamente pagato: crea lo scontrino digitale e mette il conto «da emettere».

    `insert_receipt(shop_id, items, meta)` è quello dell'app (numerazione per negozio).
    Si può richiamare in sicurezza: se lo scontrino esiste già lo restituisce.
    Ritorna la riga dello scontrino.
    """
    o = get_order(db, shop_id, order_id)
    if o["receipt_id"]:
        return db.execute("SELECT * FROM receipts WHERE id = ?", (o["receipt_id"],)).fetchone()
    if o["status"] not in ("preconto", "paid"):
        raise PosError("Presenta il preconto e incassa prima di chiudere.", 409)
    t = totals(db, o)
    if t["total"] <= 0:
        raise PosError("Il totale del conto è zero.")
    if t["due"] > 0:
        raise PosError("Resta ancora da pagare.", 409)
    if o["customer_status"] != "confirmed":
        raise PosError("Il cliente non ha ancora confermato il conto.", 409)
    pays = db.execute("SELECT method, SUM(amount_cents) a FROM pos_payments WHERE order_id = ?"
                      " GROUP BY method ORDER BY a DESC", (order_id,)).fetchall()
    method = pays[0]["method"].capitalize() if len(pays) == 1 else "Misto"
    # prima si segna pagato (e si libera il tavolo), poi si crea lo scontrino: se qualcosa si
    # interrompe in mezzo, basta richiamare close_order.
    db.execute("UPDATE pos_orders SET status = 'paid', closed_at = COALESCE(closed_at, ?) WHERE id = ?",
               (now_iso(), order_id))
    db.commit()
    external = f"POS-{order_id}"
    try:
        receipt = insert_receipt(shop_id, _receipt_items(db, o),
                                 {"external_id": external, "payment_method": method, "table_label": o["table_label"]})
    except Exception:
        db.rollback()
        receipt = db.execute("SELECT * FROM receipts WHERE shop_id = ? AND external_id = ?",
                             (shop_id, external)).fetchone()
        if not receipt:
            raise
    db.execute("UPDATE pos_orders SET receipt_id = ?, fiscal_status = 'da_emettere' WHERE id = ?",
               (receipt["id"], order_id))
    db.commit()
    return receipt


# ------------------------------------------------------------------ fiscalizzazione (vedi fiscal.py)
def mark_fiscal_issued(db, shop_id, order_id, number, when=None):
    o = get_order(db, shop_id, order_id)
    if o["status"] != "paid":
        raise PosError("Il conto non è ancora chiuso.", 409)
    number = str(number or "").strip()[:40]
    if not number:
        raise PosError("Scrivi il numero del documento commerciale emesso.")
    db.execute("UPDATE pos_orders SET fiscal_status = 'emesso', fiscal_number = ?, fiscal_at = ? WHERE id = ?",
               (number, when or now_iso(), order_id))
    db.commit()


def fiscal_queue(db, shop_id):
    return db.execute("SELECT o.*, r.number AS receipt_number, r.total_cents AS receipt_total, r.token AS receipt_token"
                      " FROM pos_orders o JOIN receipts r ON r.id = o.receipt_id"
                      " WHERE o.shop_id = ? AND o.fiscal_status = 'da_emettere' ORDER BY o.id", (shop_id,)).fetchall()


# ------------------------------------------------------------------ vista sala
def floor_overview(db, shop_id):
    """Tavoli con lo stato del conto: per la mappa della sala."""
    tables = db.execute("SELECT * FROM pos_tables WHERE shop_id = ? AND active = 1 ORDER BY sort, id",
                        (shop_id,)).fetchall()
    out = []
    for t in tables:
        o = open_order_for_table(db, shop_id, t["id"])
        tot = totals(db, o)["total"] if o else 0
        out.append({"id": t["id"], "label": t["label"], "area": t["area"], "seats": t["seats"],
                    "order_id": o["id"] if o else None, "status": o["status"] if o else "free",
                    "customer_status": o["customer_status"] if o else "none", "total_cents": tot})
    return out
