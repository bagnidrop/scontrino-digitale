#!/usr/bin/env python3
"""Ponte per il registratore telematico (RT) — Scontrino Digitale.

Si installa su UN computer sempre acceso nella rete del locale (Windows, Mac o Linux, serve solo Python 3.8+,
nessun pacchetto da installare). Fa solo connessioni IN USCITA verso il server: non apre porte su internet.

  python rt_bridge.py --server https://TUO-DOMINIO --token rt_XXXX --rt http://192.168.1.50/cgi-bin/fpmate.cgi

Driver incluso: Epson (FP-81 II RT, FP-90 III e compatibili col protocollo «ePOS-Print fiscale» / fpmate.cgi).
I reparti dell'RT (IVA) si impostano dal pannello e DEVONO coincidere con quelli programmati nel registratore:
fallo con il tecnico che ha installato l'RT. Prima dell'uso vero prova con --dry-run e con un documento di prova.

Sicurezza fiscale: se non si è sicuri che l'RT abbia emesso il documento (es. nessuna risposta), il ponte NON
lo rimanda: lo segnala come «da controllare» e lo decide una persona.
"""
import argparse
import http.client
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from xml.sax.saxutils import quoteattr

VERSION = "1.0"


# ------------------------------------------------------------------ protocollo Epson
def money(cents):
    return f"{cents // 100}.{cents % 100:02d}"


def epson_xml(payload, operator=1):
    """Documento commerciale: una riga per prodotto (importo totale esatto), poi gli incassi."""
    op = f'operator="{operator}"'
    rows = ['<beginFiscalReceipt %s />' % op]
    for ln in payload["lines"]:
        rows.append('<printRecItem %s description=%s quantity="1" unitPrice="%s" department="%d" justification="1" />'
                    % (op, quoteattr(ln["description"]), money(ln["amount_cents"]), ln["department"]))
    for p in payload["payments"]:
        rows.append('<printRecTotal %s description=%s payment="%s" paymentType="%d" justification="1" />'
                    % (op, quoteattr(p["method"].capitalize()), money(p["amount_cents"]), p["type"]))
    rows.append('<endFiscalReceipt %s />' % op)
    return ('<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><printerFiscalReceipt>'
            + "".join(rows) + '</printerFiscalReceipt></s:Body></s:Envelope>')


def parse_epson(text):
    """Legge la risposta dell'RT. Ritorna (successo, codice, numero_documento_o_vuoto)."""
    m = re.search(r'<response[^>]*\bsuccess="(true|false)"', text)
    if not m:
        return None, "", ""
    code = (re.search(r'<response[^>]*\bcode="([^"]*)"', text) or [None, ""])[1]
    num = re.search(r"<fiscalReceiptNumber>\s*(\d+)\s*</fiscalReceiptNumber>", text)
    z = re.search(r"<zRepNumber>\s*(\d+)\s*</zRepNumber>", text)
    number = ""
    if num:
        number = f"{int(z.group(1)):04d}-{int(num.group(1)):04d}" if z else f"{int(num.group(1)):04d}"
    return m.group(1) == "true", code, number


def send_epson(rt_url, xml, insecure=False, connect_timeout=5, read_timeout=40):
    """Manda il documento all'RT. Esiti: ('issued', numero) | ('rejected', errore) |
    ('not_sent', errore: non è partito nulla, si può riprovare) | ('unknown', errore: forse è stato emesso)."""
    u = urllib.parse.urlparse(rt_url)
    if u.scheme == "https":
        ctx = ssl.create_default_context()
        if insecure:  # l'RT usa un certificato autofirmato
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        conn = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=connect_timeout, context=ctx)
    else:
        conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=connect_timeout)
    try:
        conn.connect()
    except (OSError, ssl.SSLError) as e:
        return "not_sent", f"Registratore non raggiungibile ({e})"
    try:
        conn.sock.settimeout(read_timeout)
        path = (u.path or "/cgi-bin/fpmate.cgi") + (f"?{u.query}" if u.query else "")
        conn.request("POST", path, body=xml.encode("utf-8"), headers={"Content-Type": "text/xml; charset=utf-8"})
        resp = conn.getresponse()
        text = resp.read().decode("utf-8", "replace")
    except Exception as e:  # inviato (o forse no) ma nessuna risposta: NON si sa se è uscito
        return "unknown", f"Nessuna risposta dal registratore ({e})"
    finally:
        conn.close()
    ok, code, number = parse_epson(text)
    if ok is True:
        return "issued", number or "n.d."
    if ok is False:
        return "rejected", f"Il registratore ha risposto con errore (codice {code or 'sconosciuto'})"
    return "unknown", "Risposta del registratore non riconoscibile"


# ------------------------------------------------------------------ dialogo col server
def call(server, token, path, body=None, timeout=20):
    req = urllib.request.Request(server.rstrip("/") + path, method="POST", data=json.dumps(body or {}).encode(),
                                 headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                                          "User-Agent": f"rt-bridge/{VERSION}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return r.status, (json.loads(raw) if raw else {})


def pending_path(args):
    return args.pending_file


def load_pending(args):
    try:
        with open(pending_path(args)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def save_pending(args, items):
    if items:
        with open(pending_path(args), "w") as f:
            json.dump(items, f)
    elif os.path.exists(pending_path(args)):
        os.remove(pending_path(args))


def flush_pending(args):
    """Esiti già ottenuti dall'RT ma non ancora comunicati al server: si riprova finché non arrivano."""
    items, left = load_pending(args), []
    for it in items:
        try:
            call(args.server, args.token, f"/api/rt/v1/jobs/{it['id']}/result", it["body"])
        except urllib.error.HTTPError as e:
            if e.code >= 500 or e.code == 429:
                left.append(it)
            else:
                log(f"esito del documento {it['id']} rifiutato dal server ({e.code}): scartato")
        except Exception:
            left.append(it)
    if left != items:
        save_pending(args, left)
    return not left


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def process_once(args):
    """Un giro: ritira un documento, lo manda all'RT, comunica l'esito. True se ha lavorato."""
    if not flush_pending(args):
        return False  # prima si comunicano gli esiti vecchi: mai prendere altro lavoro con esiti in sospeso
    status, data = call(args.server, args.token, "/api/rt/v1/next")
    if status == 204 or not data.get("job"):
        return False
    job = data["job"]
    xml = epson_xml(job["payload"], args.operator)
    if args.dry_run:
        log(f"[prova] documento {job['id']}: nessun invio all'RT.\n{xml}")
        outcome, info = "not_sent", "Prova (--dry-run): nulla inviato"
    else:
        log(f"documento {job['id']} (tavolo {job['payload'].get('table', '')}) → registratore")
        outcome, info = send_epson(args.rt, xml, args.rt_insecure)
    body = {"outcome": outcome}
    body["number" if outcome == "issued" else "error"] = info
    log(f"esito: {outcome} {info}")
    items = load_pending(args) + [{"id": job["id"], "body": body}]
    save_pending(args, items)  # prima si salva, poi si comunica: un'interruzione non perde l'esito
    flush_pending(args)
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(description="Ponte tra Scontrino Digitale e il registratore telematico")
    ap.add_argument("--server", default=os.environ.get("RT_SERVER"), help="indirizzo del servizio, es. https://scontrini.miodominio.it")
    ap.add_argument("--token", default=os.environ.get("RT_TOKEN"), help="token del ponte (dal pannello, «Da fiscalizzare»)")
    ap.add_argument("--rt", default=os.environ.get("RT_URL"), help="indirizzo dell'RT, es. http://192.168.1.50/cgi-bin/fpmate.cgi")
    ap.add_argument("--rt-insecure", action="store_true", help="accetta il certificato autofirmato dell'RT (https)")
    ap.add_argument("--operator", type=int, default=1, help="numero operatore dell'RT (default 1)")
    ap.add_argument("--interval", type=float, default=3.0, help="secondi tra un controllo e l'altro")
    ap.add_argument("--pending-file", default="rt_bridge_pending.json")
    ap.add_argument("--dry-run", action="store_true", help="prova: mostra cosa invierebbe, senza parlare con l'RT")
    ap.add_argument("--once", action="store_true", help="un solo giro ed esce")
    args = ap.parse_args(argv)
    if not (args.server and args.token) or not (args.rt or args.dry_run):
        ap.error("servono --server, --token e --rt (oppure --dry-run)")
    log(f"ponte RT {VERSION} avviato → {args.server}")
    while True:
        try:
            worked = process_once(args)
        except urllib.error.HTTPError as e:
            worked = False
            log(f"il server ha risposto {e.code}" + (": token non valido o collegamento disattivato" if e.code == 401 else ""))
        except Exception as e:
            worked = False
            log(f"server non raggiungibile ({e}): riprovo")
        if args.once:
            return 0
        if not worked:
            time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
