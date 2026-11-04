"""Dati demo realistici per provare il pannello (solo uso locale / prove).

    DATABASE_PATH=/percorso/demo.db python seed_demo.py [--days 45] [--reset]

Crea il locale «Trattoria Da Mario» con:
  - scontrini degli ultimi N giorni (più pieni venerdì/sabato, chiuso il lunedì, pranzo e cena),
  - qualche scontrino annullato, richieste di fattura (nuove ed evase), messaggi (anche falliti),
  - clienti con consenso marketing.
Accesso:  demo@scontrino.test  /  demo12345
"""
import argparse
import random
import secrets
import string
import sys
from datetime import datetime, timedelta, timezone

import app as appmod
from db import get_db

EMAIL, PASSWORD = "demo@scontrino.test", "demo12345"

# (nome, prezzo in centesimi, aliquota IVA)
STARTERS = [("Bruschetta al pomodoro", 600, 10), ("Tagliere di salumi", 1400, 10), ("Burrata e pomodorini", 950, 10),
            ("Fritto misto di verdure", 800, 10)]
FIRSTS = [("Spaghetti alle vongole", 1400, 10), ("Carbonara", 1100, 10), ("Risotto ai funghi", 1250, 10),
          ("Lasagna della nonna", 1050, 10), ("Tagliatelle al ragù", 1000, 10)]
SECONDS = [("Tagliata di manzo", 1800, 10), ("Frittura di paranza", 1600, 10), ("Cotoletta alla milanese", 1450, 10),
           ("Grigliata mista", 1700, 10)]
PIZZAS = [("Margherita", 800, 10), ("Diavola", 950, 10), ("Quattro formaggi", 1000, 10), ("Capricciosa", 1050, 10),
          ("Bufala e pachino", 1150, 10)]
DESSERTS = [("Tiramisù", 650, 10), ("Panna cotta", 550, 10), ("Sorbetto al limone", 450, 10)]
WATER = ("Acqua naturale 0,75 l", 300, 10)
SOFT = [("Coca-Cola 33 cl", 400, 10), ("Aranciata", 400, 10)]
ALCOHOL = [("Birra media", 500, 22), ("Calice di vino della casa", 550, 22), ("Calice di Vermentino", 650, 22),
           ("Amaro", 400, 22), ("Limoncello", 400, 22)]
COFFEE = ("Caffè", 150, 10)
COVER = ("Coperto", 200, 10)

PAYMENTS = [("Carta", 55), ("Contanti", 28), ("Satispay", 12), ("Buono pasto", 5)]
COMPANIES = [("Rossi Costruzioni Srl", "Via dei Mille 12, 20100 Milano"), ("Studio Bianchi & Associati", "Corso Italia 45, 10100 Torino"),
             ("Verdi Logistica Spa", "Via Appia 210, 00100 Roma"), ("Marino Consulting Srl", "Via Toledo 88, 80100 Napoli"),
             ("Ferrari Impianti Snc", "Via Emilia 7, 40100 Bologna"), ("Gallo Immobiliare Srl", "Piazza Duomo 3, 50100 Firenze"),
             ("Costa Digital Srl", "Via Garibaldi 21, 16100 Genova"), ("Esposito Trasporti Srl", "Via Roma 150, 90100 Palermo")]
FAIL_ERRORS = ["Twilio HTTP 400: numero non raggiungibile su WhatsApp", "Twilio HTTP 21211: numero di destinazione non valido",
               "errore di rete: timed out"]


def weighted(rng, pairs):
    return rng.choices([p[0] for p in pairs], weights=[p[1] for p in pairs])[0]


def piva_from(n):
    """Partita IVA italiana valida (11 cifre con cifra di controllo)."""
    digits = f"{n:010d}"[-10:]
    total = 0
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return digits + str((10 - total % 10) % 10)


def build_order(rng, covers):
    order = {}

    def add(item, qty=1):
        order[item] = order.get(item, 0) + qty

    add(COVER, covers)
    add(WATER, max(1, covers // 2))
    pizza_night = rng.random() < 0.45
    for _ in range(covers):
        if pizza_night:
            add(rng.choice(PIZZAS))
        else:
            if rng.random() < 0.35:
                add(rng.choice(STARTERS))
            add(rng.choice(FIRSTS))
            if rng.random() < 0.55:
                add(rng.choice(SECONDS))
        if rng.random() < 0.55:
            add(rng.choice(ALCOHOL))
        elif rng.random() < 0.5:
            add(rng.choice(SOFT))
        if rng.random() < 0.3:
            add(rng.choice(DESSERTS))
        if rng.random() < 0.45:
            add(COFFEE)
    if covers >= 3 and rng.random() < 0.5:
        add(rng.choice(STARTERS))
    return order


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=45)
    ap.add_argument("--reset", action="store_true", help="elimina e ricrea il locale demo")
    args = ap.parse_args()

    rng = random.Random(42)
    app = appmod.create_app()
    with app.app_context():
        db = get_db()
        exists = db.execute("SELECT id FROM shops WHERE email = ?", (EMAIL,)).fetchone()
        if exists and not args.reset:
            sys.exit(f"Il locale demo esiste già ({EMAIL}). Usa --reset per ricrearlo.")
        if exists:
            db.execute("DELETE FROM shops WHERE id = ?", (exists["id"],))
            db.commit()

        shop_id, api_key = appmod.create_shop("Trattoria Da Mario", EMAIL, PASSWORD)
        db.execute("UPDATE shops SET address = ?, vat_number = ?, review_url = ?, footer_note = ? WHERE id = ?",
                   ("Via Garibaldi 12, 20121 Milano", piva_from(123456789), "https://g.page/r/trattoria-da-mario/review",
                    "Grazie e a presto da tutto lo staff!", shop_id))
        db.commit()

        now = datetime.now(appmod.LOCAL_TZ)
        base_per_weekday = {0: 0, 1: 17, 2: 20, 3: 24, 4: 38, 5: 54, 6: 40}  # lunedì chiuso
        sales = []
        for back in range(args.days, -1, -1):
            day = (now - timedelta(days=back)).replace(hour=0, minute=0, second=0, microsecond=0)
            n = base_per_weekday[day.weekday()]
            n = int(n * rng.uniform(0.8, 1.2))
            for _ in range(n):
                lunch = rng.random() < 0.4
                h, mi = (rng.randint(12, 14), rng.randint(0, 59)) if lunch else (rng.randint(19, 22), rng.randint(0, 59))
                if lunch and h == 12 and mi < 10:
                    mi = 10 + mi
                when = day.replace(hour=h, minute=mi, second=rng.randint(0, 59))
                if when > now:
                    continue
                sales.append(when)
        sales.sort()

        stats = dict(receipts=0, voided=0, invoices=0, invoices_new=0, messages=0, failed=0)
        contacts_pool = [f"+39{rng.choice(['320', '328', '333', '338', '347', '349', '366', '389'])}{rng.randint(1000000, 9999999)}"
                         for _ in range(160)]
        used_contacts = set()

        for number, when in enumerate(sales, 1):
            covers = rng.choices([1, 2, 3, 4, 5, 6], weights=[14, 36, 14, 20, 8, 8])[0]
            order = build_order(rng, covers)
            items = appmod.parse_items([{"name": n[0], "qty": q, "unit_price_cents": n[1], "vat_rate": n[2]}
                                        for n, q in order.items()])
            total = sum(i["line_total_cents"] for i in items)
            vat = sum(i["vat_cents"] for i in items)
            created = when.astimezone(timezone.utc)
            voided = rng.random() < 0.03
            table = "Asporto" if rng.random() < 0.08 else f"Tavolo {rng.randint(1, 20)}"
            lottery = "".join(rng.choices(string.ascii_uppercase + string.digits, k=8)) if rng.random() < 0.08 else ""
            rid = db.insert(
                "INSERT INTO receipts (shop_id, token, number, external_id, total_cents, vat_cents, payment_method,"
                " table_label, lottery_code, views, created_at, voided_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (shop_id, secrets.token_urlsafe(12), number, f"CASSA1-{number:05d}", total, vat,
                 weighted(rng, PAYMENTS), table, lottery, rng.choice([0, 0, 1, 1, 1, 2, 3]),
                 created.isoformat(timespec="seconds"),
                 (created + timedelta(minutes=rng.randint(2, 25))).isoformat(timespec="seconds") if voided else None))
            db.executemany(
                "INSERT INTO receipt_items (receipt_id, name, qty, unit_price_cents, vat_rate, line_total_cents, vat_cents)"
                " VALUES (?,?,?,?,?,?,?)",
                [(rid, i["name"], i["qty"], i["unit_price_cents"], i["vat_rate"], i["line_total_cents"], i["vat_cents"])
                 for i in items])
            stats["receipts"] += 1
            stats["voided"] += voided
            if voided:
                continue

            # messaggi inviati al cliente
            if rng.random() < 0.4:
                phone = rng.choice(contacts_pool)
                failed = rng.random() < 0.06
                status = "failed" if failed else "sent"
                db.execute("INSERT INTO messages (receipt_id, channel, phone_masked, status, error, created_at) VALUES (?,?,?,?,?,?)",
                           (rid, rng.choices(["whatsapp", "sms"], weights=[75, 25])[0], appmod.messaging.mask_phone(phone),
                            status, rng.choice(FAIL_ERRORS) if failed else None,
                            (created + timedelta(minutes=rng.randint(1, 4))).isoformat(timespec="seconds")))
                stats["messages"] += 1
                stats["failed"] += failed
                if not failed and rng.random() < 0.55 and phone not in used_contacts:
                    used_contacts.add(phone)
                    db.execute("INSERT INTO contacts (shop_id, phone, consent_at) VALUES (?,?,?) ON CONFLICT DO NOTHING",
                               (shop_id, phone, (created + timedelta(minutes=2)).isoformat(timespec="seconds")))

            # richieste di fattura
            if rng.random() < 0.03:
                name, address = rng.choice(COMPANIES)
                requested = created + timedelta(hours=rng.randint(1, 40))
                if requested > now.astimezone(timezone.utc):
                    requested = now.astimezone(timezone.utc)
                old = (now.astimezone(timezone.utc) - created) > timedelta(days=4)
                status = "evasa" if old and rng.random() < 0.88 else "nuova"
                with_pec = rng.random() < 0.3
                db.execute(
                    "INSERT INTO invoice_requests (receipt_id, company_name, vat_number, tax_code, sdi_code, pec, address, email,"
                    " status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (rid, name, piva_from(rng.randint(10**8, 10**9)), "",
                     "0000000" if with_pec else "".join(rng.choices(string.ascii_uppercase + string.digits, k=7)),
                     f"{name.split()[0].lower()}@pec.it" if with_pec else "", address,
                     f"amministrazione@{name.split()[0].lower()}.it", status, requested.isoformat(timespec="seconds")))
                stats["invoices"] += 1
                stats["invoices_new"] += status == "nuova"

        # clienti con consenso anche non legati a uno scontrino recente
        for phone in contacts_pool[:25]:
            if phone not in used_contacts:
                used_contacts.add(phone)
                when = (now - timedelta(days=rng.randint(0, args.days))).astimezone(timezone.utc)
                db.execute("INSERT INTO contacts (shop_id, phone, consent_at) VALUES (?,?,?) ON CONFLICT DO NOTHING",
                           (shop_id, phone, when.isoformat(timespec="seconds")))
        db.commit()

    print(f"Locale demo pronto: {EMAIL} / {PASSWORD}")
    print(f"  scontrini {stats['receipts']} (annullati {stats['voided']}) · fatture {stats['invoices']} "
          f"(da evadere {stats['invoices_new']}) · messaggi {stats['messages']} (falliti {stats['failed']}) · clienti {len(used_contacts)}")
    print(f"  API key: {api_key}")


if __name__ == "__main__":
    main()
