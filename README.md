# Scontrino Digitale

MVP multi-negozio per **bar e ristoranti**: la cassa invia la vendita via API e il cliente riceve lo scontrino come **link/QR** (oppure su **WhatsApp/SMS**). Dalla stessa pagina può **richiedere la fattura**. Il ristoratore gestisce tutto da un pannello web.

> Lo scontrino digitale è una **copia di cortesia**. Il documento commerciale fiscale resta quello emesso dal registratore telematico: questo sistema lo affianca, non lo sostituisce.

## Avvio rapido

```bash
pip install -r requirements.txt
python app.py            # http://127.0.0.1:5000
pytest                   # test automatici (SQLite)
# su Postgres (cancella e ricrea le tabelle del database indicato):
# TEST_DATABASE_URL=postgresql://utente:pw@localhost/db_di_prova pytest
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

## Logo dell'azienda

Ogni azienda carica il proprio logo da **Impostazioni → Locale → Logo** (PNG, JPG o WebP, fino a 2 MB; la trasparenza è supportata). Compare in cima allo scontrino che vede il cliente, su una piastra bianca così resta leggibile anche in modalità scura.

- Il file non viene mai servito com'è: viene ricodificato in PNG (max 480 px), eliminando metadati e contenuti nascosti. L'SVG è rifiutato perché può contenere script.
- Il logo è salvato nel database (tabella `shop_logos`, creata da sola all'avvio) e servito a un indirizzo pubblico con cache lunga; cambiando logo cambia l'indirizzo.
- Si può sostituire o rimuovere in qualsiasi momento; eliminando l'azienda sparisce anche il logo.

## Configurazione (variabili d'ambiente)

| Variabile | Significato |
|---|---|
| `DATABASE_URL` | Postgres (`postgresql://...`). Se assente usa il file SQLite in `instance/` (solo locale/Docker) |
| `SECRET_KEY` | Chiave delle sessioni. **Obbligatoria su Vercel**; altrove, se assente, ne viene generata una in `instance/` |
| `PUBLIC_BASE_URL` | Dominio pubblico usato nei link, es. `https://scontrini.miodominio.it` |
| `COOKIE_SECURE=1` | Cookie solo HTTPS (da attivare in produzione) |
| `BEHIND_PROXY=1` | Se sta dietro a nginx/Caddy/un load balancer |
| `OWNER_EMAIL` | Email del **proprietario del servizio** (più email separate da virgola). Attiva l'area `/admin` e chiude la registrazione libera |
| `ALLOW_REGISTRATION` | `1` apre / `0` chiude la registrazione libera. Con `OWNER_EMAIL` impostata il valore predefinito è `0` |
| `MESSAGING_PROVIDER` | `console` (default, prova) oppure `twilio` |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` | Credenziali Twilio |
| `TWILIO_WHATSAPP_FROM`, `TWILIO_SMS_FROM` | Numeri mittente |
| `TWILIO_WHATSAPP_CONTENT_SID` | Template WhatsApp approvato (2 variabili: negozio, link) |

**WhatsApp:** per scrivere a un cliente che non ti ha contattato nelle ultime 24 ore servono template approvati da Meta e l'opt-in del cliente. Per un test iniziale usa la sandbox Twilio.

## Proprietario del servizio e aziende clienti

Tu sei il **proprietario**; ogni locale è un'**azienda** con il suo account e i suoi dati separati.

1. Imposta `OWNER_EMAIL` con l'email di un account **già registrato** (se non ne hai uno: metti `ALLOW_REGISTRATION=1`, registrati, poi rimetti `0`). Non si diventa proprietari registrandosi.
2. Entra con quell'account: nel menu compare il gruppo **Amministrazione** (Panoramica servizio, Aziende, Registro attività). Per tutti gli altri `/admin` risponde 404.
3. **Aziende → Nuova azienda**: inserisci nome ed email del titolare. Ottieni un **link d'invito** (monouso, scade in 7 giorni) e un messaggio pronto da copiare. Il titolare sceglie la password da solo e riceve la sua chiave API: tu non vedi mai le password.
4. Dal dettaglio di un'azienda puoi **sospenderla** (accesso e API bloccati, i dati restano), riattivarla, mandare un nuovo link (password dimenticata), annotare note, **entrare come lei** per assistenza (con avviso a schermo e registro) o **eliminarla** con tutti i suoi dati.
5. Ogni azione resta nel **Registro attività**.

## Pubblicazione su Vercel (con database Postgres)

Su Vercel il disco non è persistente, quindi i dati stanno in un database Postgres esterno. L'app crea da sola le tabelle al primo avvio.

1. **Database.** Crea un progetto su [Neon](https://neon.tech) scegliendo la regione **Frankfurt (eu-central-1)**. Copia la stringa di connessione **pooled** (contiene `-pooler`). In alternativa, da Vercel → *Storage / Marketplace* aggiungi Neon: imposta `DATABASE_URL` da solo.
2. **Progetto.** Su [vercel.com](https://vercel.com) → *Add New → Project* → importa il repository GitHub. Vercel riconosce Flask da `app.py`.
3. **Variabili d'ambiente** (*Settings → Environment Variables*):

   | Nome | Valore |
   |---|---|
   | `DATABASE_URL` | la stringa Postgres (già presente se hai usato il Marketplace) |
   | `SECRET_KEY` | `python3 -c "import secrets;print(secrets.token_hex(32))"` |
   | `ALLOW_REGISTRATION` | `1` per ora; poi `0` |
   | `MESSAGING_PROVIDER` | `console` per provare, `twilio` per inviare davvero (+ variabili Twilio) |
   | `PUBLIC_BASE_URL` | il tuo dominio finale, es. `https://scontrini.tuodominio.it` (facoltativa) |

4. **Deploy.** Controlla `https://TUO-PROGETTO.vercel.app/healthz`, crea il tuo account da `/register`, poi imposta `ALLOW_REGISTRATION=0` e rilancia il deploy (su Vercel le variabili si applicano al deploy successivo).
5. **Dominio.** *Settings → Domains* per usare il tuo dominio.

Ogni `git push` su `main` pubblica automaticamente la nuova versione.

Cose da sapere:
- `vercel.json` fissa la regione delle funzioni a Francoforte (`fra1`), vicina al database.
- Con `MESSAGING_PROVIDER=console` i messaggi **non vengono spediti**: compaiono nei *Runtime Logs* di Vercel con il numero mascherato.
- Il limite di richieste dell'API è per singola istanza (best effort); quelli su login e richieste fattura sono condivisi tra le istanze perché salvati nel database.
- Il piano gratuito *Hobby* di Vercel è pensato per uso personale e non commerciale: per vendere il servizio ai locali serve un piano a pagamento (controlla i termini aggiornati).

## Pubblicazione su un tuo server (Docker)

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

Per più di poche decine di locali imposta `DATABASE_URL` su un Postgres (stesso codice, nessuna modifica). Firewall consigliato: aperte solo le porte 22, 80 e 443.

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

## Cassa (POS) per ristoranti

Dal pannello: **Apri la cassa** (`/cassa`, pensata per tablet). Flusso: tavolo → ordine → **preconto** (NON fiscale, con QR) → il cliente lo verifica dal telefono e **conferma** o **segnala un errore** → incasso (contanti con resto, carta, buoni, conto alla romana) → chiusura: scontrino digitale. Menu e tavoli si gestiscono da **Menu e sala**.

**Fiscalità:** questo software non emette documenti fiscali. Ogni conto pagato va in **Da fiscalizzare**: il documento commerciale vero si emette dal registratore telematico o dal portale «Documento commerciale online» dell'Agenzia delle Entrate e se ne registra qui il numero. Verifica la tua situazione con un commercialista. Vedi `POS-DESIGN.md` e `fiscal.py` (punto di aggancio per un futuro software certificato).

**Turni, comande e personale:** *Turni e chiusura* (fondo cassa, riepilogo incassi, differenza di cassa); dalla cassa **Invia in cucina** crea un foglio comanda stampabile con le sole righe nuove; in *Menu e sala → Personale e PIN* aggiungi camerieri, cassieri e titolari: da quel momento la cassa chiede il PIN, il tablet resta bloccato sulla cassa (il pannello si riapre solo con la password dell'azienda o col PIN di un titolare) e i camerieri non possono incassare, fare sconti né annullare conti.
