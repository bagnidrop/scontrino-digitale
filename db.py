"""Database: SQLite in locale e nei test, Postgres in produzione (Vercel, Neon, ecc.).

Si sceglie con la variabile d'ambiente DATABASE_URL (o POSTGRES_URL):
  - assente  -> file SQLite (instance/scontrini.db)
  - postgres://...  -> Postgres

Il resto dell'app usa sempre `?` come segnaposto e legge le righe per nome o per
posizione: questo modulo traduce le differenze tra i due database.
"""
import re
import sqlite3

from flask import current_app, g

try:  # necessario solo con Postgres
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None

IntegrityError = (sqlite3.IntegrityError,) + ((psycopg.IntegrityError,) if psycopg else ())

SCHEMA = """
CREATE TABLE IF NOT EXISTS shops (
    id {PK},
    name TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    api_key_hash TEXT UNIQUE NOT NULL,
    api_key_prefix TEXT NOT NULL,
    address TEXT NOT NULL DEFAULT '',
    vat_number TEXT NOT NULL DEFAULT '',
    review_url TEXT NOT NULL DEFAULT '',
    message_template TEXT NOT NULL DEFAULT 'Grazie da {{negozio}}! Ecco il tuo scontrino digitale: {{link}}',
    footer_note TEXT NOT NULL DEFAULT '',
    invoice_window_days INTEGER NOT NULL DEFAULT 3,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    invite_hash TEXT,
    invite_expires_at TEXT,
    last_login_at TEXT,
    owner_notes TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS receipts (
    id {PK},
    shop_id {FK} NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    token TEXT UNIQUE NOT NULL,
    number INTEGER NOT NULL,
    external_id TEXT,
    total_cents INTEGER NOT NULL,
    vat_cents INTEGER NOT NULL,
    payment_method TEXT NOT NULL DEFAULT '',
    table_label TEXT NOT NULL DEFAULT '',
    lottery_code TEXT NOT NULL DEFAULT '',
    views INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    voided_at TEXT,
    UNIQUE (shop_id, number),
    UNIQUE (shop_id, external_id)
);
CREATE INDEX IF NOT EXISTS idx_receipts_shop_date ON receipts (shop_id, created_at);

CREATE TABLE IF NOT EXISTS receipt_items (
    id {PK},
    receipt_id {FK} NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    qty {REAL} NOT NULL,
    unit_price_cents INTEGER NOT NULL,
    vat_rate {REAL} NOT NULL,
    line_total_cents INTEGER NOT NULL,
    vat_cents INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS invoice_requests (
    id {PK},
    receipt_id {FK} NOT NULL UNIQUE REFERENCES receipts(id) ON DELETE CASCADE,
    company_name TEXT NOT NULL,
    vat_number TEXT NOT NULL DEFAULT '',
    tax_code TEXT NOT NULL DEFAULT '',
    sdi_code TEXT NOT NULL DEFAULT '0000000',
    pec TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL,
    email TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'nuova',
    created_at TEXT NOT NULL
);

-- Log degli invii. Il numero completo NON viene salvato, solo la versione mascherata.
CREATE TABLE IF NOT EXISTS messages (
    id {PK},
    receipt_id {FK} NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    phone_masked TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL
);

-- Numeri salvati SOLO se il cliente ha dato il consenso marketing esplicito.
CREATE TABLE IF NOT EXISTS contacts (
    id {PK},
    shop_id {FK} NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    phone TEXT NOT NULL,
    consent_at TEXT NOT NULL,
    UNIQUE (shop_id, phone)
);

-- Contatore dei tentativi (login, richieste fattura): condiviso tra tutte le istanze,
-- necessario in ambienti serverless dove la memoria non è condivisa.
CREATE TABLE IF NOT EXISTS rate_hits (
    key TEXT NOT NULL,
    ts {REAL} NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rate_hits ON rate_hits (key, ts);
"""

# Colonne aggiunte dopo la prima versione: (tabella, colonna, definizione).
# Vengono applicate ai database esistenti all'avvio, senza perdere dati.
MIGRATIONS = [
    ("receipts", "voided_at", "TEXT"),  # scontrino annullato
    # account azienda gestiti dal proprietario del servizio
    ("shops", "status", "TEXT NOT NULL DEFAULT 'active'"),  # active | invited | suspended
    ("shops", "invite_hash", "TEXT"),                       # sha256 del link d'invito (mai in chiaro)
    ("shops", "invite_expires_at", "TEXT"),
    ("shops", "last_login_at", "TEXT"),
    ("shops", "owner_notes", "TEXT NOT NULL DEFAULT ''"),
    ("pos_order_items", "sent_at", "TEXT"),  # inviata in cucina (comanda)
]

# Tabelle aggiunte dopo la prima versione: create (se mancano) a ogni avvio.
LATER_SCHEMA = """
-- Registro delle azioni del proprietario del servizio. Nessuna chiave esterna: resta dopo
-- l'eliminazione di un'azienda.
CREATE TABLE IF NOT EXISTS admin_audit (
    id {PK},
    actor_shop_id {FK},
    actor_email TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    target_shop_id {FK},
    target_name TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_admin_audit_date ON admin_audit (created_at);

-- Logo dell'azienda: immagine già controllata e ricodificata in PNG (vedi logo.py). Uno per azienda.
-- Si legge dall'indirizzo pubblico /logo/<etag>.png: l'etag dipende dal contenuto, non dall'azienda.
CREATE TABLE IF NOT EXISTS shop_logos (
    shop_id {FK} PRIMARY KEY REFERENCES shops(id) ON DELETE CASCADE,
    data {BLOB} NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    etag TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shop_logos_etag ON shop_logos (etag);

-- ---------------------------------------------------------------- CASSA (POS)
-- Menu: categorie e prodotti. Il prezzo è IVA inclusa, in centesimi.
CREATE TABLE IF NOT EXISTS pos_categories (
    id {PK},
    shop_id {FK} NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    sort INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_pos_categories_shop ON pos_categories (shop_id);

CREATE TABLE IF NOT EXISTS pos_products (
    id {PK},
    shop_id {FK} NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    category_id {FK} REFERENCES pos_categories(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    price_cents INTEGER NOT NULL,
    vat_rate {REAL} NOT NULL DEFAULT 10,
    sort INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_pos_products_shop ON pos_products (shop_id, category_id);

-- Sala: tavoli (anche "Banco", "Asporto").
CREATE TABLE IF NOT EXISTS pos_tables (
    id {PK},
    shop_id {FK} NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    area TEXT NOT NULL DEFAULT '',
    seats INTEGER NOT NULL DEFAULT 2,
    sort INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    UNIQUE (shop_id, label)
);

-- Personale con PIN (il titolare entra con l'account dell'azienda).
CREATE TABLE IF NOT EXISTS pos_staff (
    id {PK},
    shop_id {FK} NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    pin_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'cameriere',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

-- Turni di cassa (apertura/chiusura giornata).
CREATE TABLE IF NOT EXISTS pos_shifts (
    id {PK},
    shop_id {FK} NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    opened_at TEXT NOT NULL,
    opened_by TEXT NOT NULL DEFAULT '',
    opening_cash_cents INTEGER NOT NULL DEFAULT 0,
    closed_at TEXT,
    closing_cash_cents INTEGER,
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_pos_shifts_shop ON pos_shifts (shop_id, opened_at);
-- un solo turno aperto per azienda (garantito dal database)
CREATE UNIQUE INDEX IF NOT EXISTS idx_pos_shifts_one_open ON pos_shifts (shop_id) WHERE closed_at IS NULL;

-- Conto di un tavolo.
--   status:          open (si ordina) -> preconto (conto mostrato al cliente) -> paid (pagato) ; void (annullato)
--   customer_status: none | pending (in attesa del cliente) | confirmed | disputed (il cliente segnala un errore)
--   fiscal_status:   non_fiscale (finché non pagato) -> da_emettere -> emesso (documento commerciale vero)
CREATE TABLE IF NOT EXISTS pos_orders (
    id {PK},
    shop_id {FK} NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    table_id {FK} REFERENCES pos_tables(id) ON DELETE SET NULL,
    table_label TEXT NOT NULL DEFAULT '',
    covers INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'open',
    opened_by TEXT NOT NULL DEFAULT '',
    discount_cents INTEGER NOT NULL DEFAULT 0,
    bill_token TEXT UNIQUE,
    bill_requested_at TEXT,
    customer_status TEXT NOT NULL DEFAULT 'none',
    customer_note TEXT NOT NULL DEFAULT '',
    customer_at TEXT,
    confirmed_by TEXT NOT NULL DEFAULT '',
    receipt_id {FK} REFERENCES receipts(id) ON DELETE SET NULL,
    fiscal_status TEXT NOT NULL DEFAULT 'non_fiscale',
    fiscal_number TEXT NOT NULL DEFAULT '',
    fiscal_at TEXT,
    shift_id {FK},
    created_at TEXT NOT NULL,
    closed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_pos_orders_shop ON pos_orders (shop_id, status);

CREATE TABLE IF NOT EXISTS pos_order_items (
    id {PK},
    order_id {FK} NOT NULL REFERENCES pos_orders(id) ON DELETE CASCADE,
    product_id {FK},
    name TEXT NOT NULL,
    qty INTEGER NOT NULL DEFAULT 1,
    unit_price_cents INTEGER NOT NULL,
    vat_rate {REAL} NOT NULL DEFAULT 10,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pos_order_items_order ON pos_order_items (order_id);

CREATE TABLE IF NOT EXISTS pos_payments (
    id {PK},
    order_id {FK} NOT NULL REFERENCES pos_orders(id) ON DELETE CASCADE,
    method TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    tendered_cents INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pos_payments_order ON pos_payments (order_id);

-- ------------------------------------------- Registratore telematico (RT) tramite «ponte» nel locale
-- Il ponte (rt_bridge.py) gira su un PC del locale, chiama il server e parla con l'RT in rete locale.
CREATE TABLE IF NOT EXISTS rt_settings (
    shop_id {FK} PRIMARY KEY REFERENCES shops(id) ON DELETE CASCADE,
    enabled INTEGER NOT NULL DEFAULT 0,
    token_hash TEXT,
    token_prefix TEXT NOT NULL DEFAULT '',
    last_seen TEXT,
    dept_map TEXT NOT NULL DEFAULT '{{"22": 1, "10": 2, "5": 3, "4": 4, "0": 5}}',
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_rt_settings_token ON rt_settings (token_hash);

-- Coda dei documenti da far emettere all'RT.
--   queued -> printing (preso dal ponte) -> done | failed (l'RT ha rifiutato: si può riprovare)
--   | uncertain (non sappiamo se l'RT l'ha emesso: va controllato a mano, MAI rimandato in automatico)
CREATE TABLE IF NOT EXISTS rt_jobs (
    id {PK},
    shop_id {FK} NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    order_id {FK} NOT NULL REFERENCES pos_orders(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'queued',
    payload TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    number TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rt_jobs_shop ON rt_jobs (shop_id, status);
-- un solo documento attivo (o emesso) per conto: impedisce doppie emissioni fiscali
CREATE UNIQUE INDEX IF NOT EXISTS idx_rt_jobs_one_per_order ON rt_jobs (order_id) WHERE status <> 'failed';
"""

_TYPES = {
    "sqlite": {"PK": "INTEGER PRIMARY KEY AUTOINCREMENT", "FK": "INTEGER", "REAL": "REAL", "BLOB": "BLOB"},
    "postgres": {"PK": "BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY", "FK": "BIGINT",
                 "REAL": "DOUBLE PRECISION", "BLOB": "BYTEA"},
}


def schema_for(dialect):
    # Le graffe dei testi di default sono raddoppiate ({{ }}) per convivere con str.format
    return SCHEMA.format(**_TYPES[dialect])


class Row:
    """Riga leggibile per nome (r["id"]) o per posizione (r[0]), come sqlite3.Row."""
    __slots__ = ("_names", "_values")

    def __init__(self, names, values):
        self._names = names
        self._values = tuple(values)

    def __getitem__(self, key):
        if isinstance(key, str):
            try:
                return self._values[self._names.index(key)]
            except ValueError:
                raise KeyError(key) from None
        return self._values[key]

    def keys(self):
        return list(self._names)

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def __repr__(self):
        return f"Row({dict(zip(self._names, self._values))})"


def _row_factory(cursor):
    names = [c.name for c in cursor.description] if cursor.description else []
    return lambda values: Row(names, values)


def _to_pyformat(sql):
    """'?' -> '%s' fuori dai testi tra apici; i '%' letterali vengono raddoppiati."""
    parts = re.split(r"('(?:[^']|'')*')", sql)
    out = []
    for i, part in enumerate(parts):
        out.append(part if i % 2 else part.replace("%", "%%").replace("?", "%s"))
    return "".join(out)


class Database:
    """Piccolo adattatore con la stessa interfaccia per SQLite e Postgres."""

    def __init__(self, conn, dialect):
        self.conn = conn
        self.dialect = dialect
        self.is_pg = dialect == "postgres"

    def execute(self, sql, params=()):
        if self.is_pg:
            return self.conn.execute(_to_pyformat(sql), params)
        return self.conn.execute(sql, params)

    def executemany(self, sql, rows):
        if self.is_pg:
            with self.conn.cursor() as cur:
                cur.executemany(_to_pyformat(sql), rows)
            return None
        return self.conn.executemany(sql, rows)

    def insert(self, sql, params=()):
        """Esegue un INSERT e restituisce l'id della nuova riga."""
        if self.is_pg:
            return self.conn.execute(_to_pyformat(sql) + " RETURNING id", params).fetchone()[0]
        return self.conn.execute(sql, params).lastrowid

    def lock_shop(self, shop_id):
        """Serializza la numerazione degli scontrini di un negozio (da usare prima di MAX(number))."""
        if self.is_pg:
            self.conn.execute("SELECT id FROM shops WHERE id = %s FOR UPDATE", (shop_id,))
        else:
            self.conn.execute("BEGIN IMMEDIATE")

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        self.conn.close()


def connect(config):
    url = config.get("DATABASE_URL")
    if url:
        if psycopg is None:
            raise RuntimeError("Per usare Postgres installa psycopg:  pip install 'psycopg[binary]'")
        # prepare_threshold=None: compatibile con i pooler (PgBouncer) di Neon/Supabase
        conn = psycopg.connect(url, row_factory=_row_factory, prepare_threshold=None,
                               connect_timeout=10)
        return Database(conn, "postgres")
    con = sqlite3.connect(config["DATABASE"], timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return Database(con, "sqlite")


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config)
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _columns(db, table):
    if db.is_pg:
        rows = db.execute("SELECT column_name FROM information_schema.columns"
                          " WHERE table_schema = current_schema() AND table_name = ?",
                          (table,)).fetchall()
        return {r[0] for r in rows}
    return {r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()}


def migrate(db):
    """Aggiunge le colonne e le tabelle mancanti (idempotente)."""
    if db.is_pg:
        db.execute(LATER_SCHEMA.format(**_TYPES["postgres"]))
    else:
        db.conn.executescript(LATER_SCHEMA.format(**_TYPES["sqlite"]))
    for table, column, ddl in MIGRATIONS:
        if column not in _columns(db, table):
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    db.commit()


def init_db(config):
    """Crea le tabelle se mancano (idempotente, sicuro con più istanze avviate insieme)."""
    db = connect(config)
    try:
        if db.is_pg:
            # Più istanze serverless possono partire insieme: il lock evita corse tra CREATE.
            db.execute("SELECT pg_advisory_lock(727274)")
            try:
                exists = db.execute("SELECT to_regclass('rate_hits')").fetchone()[0]
                if not exists:
                    db.execute(schema_for("postgres"))
                db.commit()
                migrate(db)
            finally:
                db.execute("SELECT pg_advisory_unlock(727274)")
                db.commit()
        else:
            db.execute("PRAGMA journal_mode = WAL")
            db.conn.executescript(schema_for("sqlite"))
            db.commit()
            migrate(db)
    finally:
        db.close()
