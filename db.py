"""Database SQLite (multi-negozio). Per la produzione si può passare a PostgreSQL
mantenendo lo stesso schema."""
import sqlite3

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS shops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    api_key_hash TEXT UNIQUE NOT NULL,
    api_key_prefix TEXT NOT NULL,
    address TEXT NOT NULL DEFAULT '',
    vat_number TEXT NOT NULL DEFAULT '',
    review_url TEXT NOT NULL DEFAULT '',
    message_template TEXT NOT NULL DEFAULT 'Grazie da {negozio}! Ecco il tuo scontrino digitale: {link}',
    footer_note TEXT NOT NULL DEFAULT '',
    invoice_window_days INTEGER NOT NULL DEFAULT 3,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
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
    UNIQUE (shop_id, number),
    UNIQUE (shop_id, external_id)
);
CREATE INDEX IF NOT EXISTS idx_receipts_shop_date ON receipts (shop_id, created_at);

CREATE TABLE IF NOT EXISTS receipt_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_id INTEGER NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    qty REAL NOT NULL,
    unit_price_cents INTEGER NOT NULL,
    vat_rate REAL NOT NULL,
    line_total_cents INTEGER NOT NULL,
    vat_cents INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS invoice_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_id INTEGER NOT NULL UNIQUE REFERENCES receipts(id) ON DELETE CASCADE,
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
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_id INTEGER NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    phone_masked TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL
);

-- Numeri salvati SOLO se il cliente ha dato il consenso marketing esplicito.
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    phone TEXT NOT NULL,
    consent_at TEXT NOT NULL,
    UNIQUE (shop_id, phone)
);
"""


def get_db():
    if "db" not in g:
        con = sqlite3.connect(current_app.config["DATABASE"], timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        g.db = con
    return g.db


def close_db(_exc=None):
    con = g.pop("db", None)
    if con is not None:
        con.close()


def init_db(path):
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode = WAL")
    con.executescript(SCHEMA)
    con.commit()
    con.close()
