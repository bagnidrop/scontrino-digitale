# Scontrino Digitale

MVP multi-negozio per **bar e ristoranti**: la cassa invia la vendita via API e il cliente riceve lo scontrino come **link/QR** (oppure su **WhatsApp/SMS**). Dalla stessa pagina può **richiedere la fattura**. Il ristoratore gestisce tutto da un pannello web.

> Lo scontrino digitale è una **copia di cortesia**. Il documento commerciale fiscale resta quello emesso dal registratore telematico: questo sistema lo affianca, non lo sostituisce.

## Avvio rapido

```bash
pip install -r requirements.txt
python app.py            # http://127.0.0.1:5000
pytest                   # test automatici
```

1. Apri `/register` e crea l'account del locale: ricevi la **API key** (si vede una sola volta).
2. Dal pannello premi **Crea scontrino di prova** per vedere cosa riceve il cliente.
3. Collega la cassa con le chiamate qui sotto.

Per impostazione predefinita i messaggi **non vengono spediti**: finiscono in `instance/outbox.log` (numero mascherato). Così puoi provare tutto senza costi.

## API per la cassa

Autenticazione: header `X-API-Key: <chiave>` (oppure `Authorization: Bearer <chiave>`).

**Crea scontrino** — `POST /api/v1/receipts`

```json
{
  "external_id": "CASSA1-0001",
  "table": "Tavolo 4",
  "payment_method": "Carta",
  "lottery_code": "",
  "items": [
    {"name": "Margherita", "qty": 2, "unit_price": "8.50", "vat_rate": 10},
    {"name": "Birra media", "qty": 2, "unit_price": "5.00", "vat_rate": 22}
  ]
}
```

Risposta `201` con `token`, `url` (da stampare come QR) e `qr_svg_url`.
- `external_id` rende la chiamata **idempotente**: se la cassa riprova, riceve lo stesso scontrino (`200`) senza doppioni.
- `total_cents` è facoltativo; se presente deve coincidere con la somma delle righe.
- Aliquote IVA ammesse: 4, 5, 10, 22.

**Invia al cliente** — `POST /api/v1/receipts/<token>/send`

```json
{"phone": "333 1234567", "channel": "whatsapp", "marketing_consent": false}
```

`channel` è `whatsapp` (default) o `sms`. Massimo 3 invii per scontrino.

## Pagine

| Percorso | Cosa fa |
|---|---|
| `/r/<token>` | Scontrino per il cliente, stampa/PDF, richiesta fattura |
| `/r/<token>/qr.svg` | QR code dello scontrino |
| `/dashboard` | Statistiche, scontrino di prova, guida di collegamento |
| `/dashboard/receipts`, `/invoices`, `/contacts`, `/settings` | Storico, richieste fattura (con export CSV), contatti con consenso, personalizzazione e API key |

## Configurazione (variabili d'ambiente)

| Variabile | Significato |
|---|---|
| `SECRET_KEY` | Chiave delle sessioni (se assente ne viene generata una in `instance/`) |
| `PUBLIC_BASE_URL` | Dominio pubblico usato nei link, es. `https://scontrini.miodominio.it` |
| `COOKIE_SECURE=1` | Cookie solo HTTPS (da attivare in produzione) |
| `BEHIND_PROXY=1` | Se sta dietro a nginx/Caddy/un load balancer |
| `ALLOW_REGISTRATION=0` | Chiude la registrazione libera (utile per un lancio su invito) |
| `MESSAGING_PROVIDER` | `console` (default, prova) oppure `twilio` |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` | Credenziali Twilio |
| `TWILIO_WHATSAPP_FROM`, `TWILIO_SMS_FROM` | Numeri mittente |
| `TWILIO_WHATSAPP_CONTENT_SID` | Template WhatsApp approvato (2 variabili: negozio, link) |

**WhatsApp:** per scrivere a un cliente che non ti ha contattato nelle ultime 24 ore servono template approvati da Meta e l'opt-in del cliente. Per un test iniziale usa la sandbox Twilio.

## Pubblicazione su un server (Docker)

Servono un VPS Linux con Docker e un dominio con record DNS **A** verso l'IP del server.

```bash
# una tantum: installa Docker (Ubuntu/Debian)
curl -fsSL https://get.docker.com | sh

# scarica il progetto e configuralo
git clone https://github.com/TUO_UTENTE/scontrino-digitale.git /opt/scontrino
cd /opt/scontrino
cp .env.example .env && nano .env      # DOMAIN, SECRET_KEY, ecc.
docker compose up -d --build
```

Caddy ottiene da solo il certificato HTTPS. Controlla `https://TUO_DOMINIO/healthz`, poi crea il tuo account da `/register` e imposta `ALLOW_REGISTRATION=0` in `.env` (`docker compose up -d` per applicare).

**Aggiornare:** `cd /opt/scontrino && git pull && docker compose up -d --build`

**Backup:** `./backup.sh` salva una copia del database in `backups/` (cron consigliato, vedi il file) ed elimina quelle più vecchie di 14 giorni. Tieni una copia anche fuori dal server.

**Log:** `docker compose logs -f app`

Per più di poche decine di locali passa a PostgreSQL (lo schema è standard). Firewall consigliato: aperte solo le porte 22, 80 e 443.

## Sicurezza e privacy già previste

- API key salvata come hash; password con hash; protezione CSRF sui form; rate limit su login e API.
- Link scontrino con token casuale non indovinabile.
- Il numero di telefono **non viene salvato** dopo l'invio: nel log resta solo la versione mascherata. Si conserva solo con consenso marketing esplicito, e il locale può cancellarlo dalla pagina Contatti.
- Pagina fattura protetta da campo anti-bot e controllo della P.IVA (cifra di controllo).

## Da fare prima di venderlo

- **Informativa privacy e contratto di nomina a responsabile del trattamento (GDPR)** con ogni locale; testi legali da far scrivere a un professionista.
- **Verifica fiscale con un commercialista** sul ruolo di copia di cortesia e sulla lotteria degli scontrini.
- **Connettori** per 1–2 gestionali cloud (Tilby, Scloby, ecc.): oggi l'integrazione avviene via API, quindi serve che il gestionale possa chiamarla.
- Recupero password, fatturazione/abbonamento dei locali, politica di conservazione dei dati.
