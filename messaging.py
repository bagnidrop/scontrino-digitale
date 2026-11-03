"""Invio messaggi (WhatsApp / SMS).

Provider scelto con la variabile d'ambiente MESSAGING_PROVIDER:
  - "console" (default): non invia nulla, scrive il messaggio in instance/outbox.log
  - "twilio": invia davvero via Twilio (WhatsApp e SMS)

Variabili per Twilio:
  TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN
  TWILIO_WHATSAPP_FROM   es. +14155238886   (numero mittente WhatsApp)
  TWILIO_SMS_FROM        es. +39XXXXXXXXXX  (o un sender alfanumerico)
  TWILIO_WHATSAPP_CONTENT_SID (opzionale) SID di un template WhatsApp approvato.
      Fuori dalla finestra di 24 ore WhatsApp consente solo template approvati:
      il template deve avere 2 variabili: {{1}} = nome negozio, {{2}} = link.
"""
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def normalize_phone(raw, default_cc="39"):
    """Restituisce il numero in formato E.164 (+39333...) oppure None se non valido."""
    if not raw:
        return None
    s = str(raw).strip()
    plus = s.startswith("+")
    digits = re.sub(r"\D", "", s)
    if not plus and digits.startswith("00"):
        plus, digits = True, digits[2:]
    if not plus:
        digits = default_cc + digits
    if not re.fullmatch(r"\d{8,15}", digits):
        return None
    return "+" + digits


def mask_phone(phone):
    if len(phone) <= 6:
        return "•" * len(phone)
    return phone[:3] + "•" * (len(phone) - 6) + phone[-3:]


def render_template_text(template, shop_name, link):
    """Sostituzione semplice (niente str.format: il testo è scritto dal negoziante)."""
    text = template.replace("{negozio}", shop_name).replace("{link}", link)
    return text[:600]


def send(channel, to, body, template_vars=None):
    """Ritorna (status, errore): status è 'sent' oppure 'failed'."""
    provider = os.environ.get("MESSAGING_PROVIDER", "console").lower()
    if provider == "twilio":
        return _send_twilio(channel, to, body, template_vars)
    return _send_console(channel, to, body)


def _send_console(channel, to, body):
    """Modalità prova: non spedisce nulla. Scrive su file, o nei log se il disco è in sola lettura."""
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    line = f"[{stamp}] ({channel}) a {mask_phone(to)}: {body}"
    path = os.environ.get("OUTBOX_PATH") or os.path.join(BASE_DIR, "instance", "outbox.log")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:  # es. Vercel: i log della funzione fanno da "outbox"
        print("[outbox] " + line, file=sys.stderr, flush=True)
    return "sent", None


def _send_twilio(channel, to, body, template_vars):
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    token = os.environ.get("TWILIO_AUTH_TOKEN")
    if not sid or not token:
        return "failed", "credenziali Twilio mancanti"

    if channel == "whatsapp":
        sender = os.environ.get("TWILIO_WHATSAPP_FROM")
        if not sender:
            return "failed", "TWILIO_WHATSAPP_FROM mancante"
        data = {"From": "whatsapp:" + sender, "To": "whatsapp:" + to}
        content_sid = os.environ.get("TWILIO_WHATSAPP_CONTENT_SID")
        if content_sid and template_vars:
            data["ContentSid"] = content_sid
            data["ContentVariables"] = json.dumps(template_vars)
        else:
            data["Body"] = body
    else:
        sender = os.environ.get("TWILIO_SMS_FROM")
        if not sender:
            return "failed", "TWILIO_SMS_FROM mancante"
        data = {"From": sender, "To": to, "Body": body}

    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), method="POST")
    auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
    req.add_header("Authorization", "Basic " + auth)
    try:
        with urllib.request.urlopen(req, timeout=10):
            return "sent", None
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode()).get("message", "")
        except Exception:
            detail = ""
        return "failed", f"Twilio HTTP {exc.code}: {detail}"[:200]
    except Exception as exc:  # rete, timeout...
        return "failed", f"errore di rete: {exc}"[:200]
