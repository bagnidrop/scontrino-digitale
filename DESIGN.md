# DESIGN.md — guida per costruire il pannello

Pannello di gestione per **ristoratori italiani** (non tecnici, spesso da telefono, in fretta, tra un servizio e l'altro).
Lingua dell'interfaccia: **italiano**, dando del **tu**, frasi brevi, niente gergo informatico.
Tono: caldo e professionale, come un buon gestionale di sala. Estetica: carta calda, verde profondo, un tocco terracotta.

## 1. Regole d'oro

1. **Ogni card è cliccabile.** Una card di riepilogo, scorciatoia o elemento (statistica, cliente, scontrino, azione) è sempre un `<a class="card is-link">` (o `<button class="card is-link">` se esegue un POST). Va a una pagina utile, con i filtri già applicati. Le sole card non cliccabili sono quelle che *contengono* un form o una tabella: lì sono cliccabili le righe e i controlli.
2. **Zero ostacoli:** l'azione più probabile è la più evidente. Ogni pagina vuota ha uno stato vuoto con invito all'azione (`empty_state`).
3. **Mobile prima:** tutto deve funzionare a **360 px** senza scroll orizzontale della pagina (le tabelle larghe stanno in `.table-wrap`).
4. **Tema chiaro e scuro** automatici: **mai colori fissi** (`#fff`, `#000`, ecc.), solo variabili `var(--…)`.
5. **Accessibilità:** HTML semantico, `<label>` per ogni campo, focus visibile (già nel CSS), contrasto sufficiente, tutto usabile da tastiera, `aria-current` sulle voci attive, niente informazione affidata solo al colore (usa testo o icona insieme al colore).
6. **Coerenza:** usa i componenti qui sotto. Non inventare un secondo stile di bottoni, tabelle o badge.

## 2. Vincoli tecnici (CSP e non solo)

La pagina ha una Content-Security-Policy severa: `default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:`.

- **Vietati** gli attributi `style="…"` e i tag `<style>` e `<script>` inline. Gli stili stanno in file `.css`, gli script in file `.js` caricati con `<script src>`.
- **Vietati** font, immagini, script e CDN esterni. Solo risorse locali (`/static/…`).
- Nei grafici SVG usa **attributi di presentazione** (`x`, `y`, `width`, `height`, `fill`, `viewBox`) e **classi** CSS (`class="bar"`), mai `style=`. Per le barre di avanzamento usa `<meter>`/`<progress>` o un `<rect>` SVG.
- I colori nei grafici vengono da classi che usano `var(--brand)` ecc. (vedi `.chart` in `panel.css`).
- Ogni POST di un form deve contenere `<input type="hidden" name="csrf" value="{{ csrf_token }}">`.
- Per conferme prima di azioni distruttive: attributo `data-confirm="Testo?"` sul `<form>` (gestito da `app.js`).

## 3. Struttura dei file e proprietà

Ogni agente possiede **solo** i propri file. **Non modificare file di altri**, neanche per una riga.

| Comune (NON toccare) | |
|---|---|
| `app.py`, `db.py`, `messaging.py`, `sections/__init__.py`, `sections/common.py` | logica base e registrazione |
| `templates/panel/base.html`, `_ui.html`, `_icons.html` | layout, componenti, icone |
| `public/static/panel.css`, `panel.js`, `app.js` | design system e comportamenti comuni |
| `testutil.py`, `test_app.py`, `seed_demo.py`, `DESIGN.md` | test e dati demo |

Ogni sezione: `sections/<nome>.py` (con `register(app, ctx)`), `templates/panel/<nome>*.html` e, se serve, `public/static/panel-<nome>.css` / `panel-<nome>.js`, caricati con `{% block head %}` / `{% block scripts %}`. I CSS di sezione usano **solo** variabili e componenti esistenti e un prefisso di classe proprio (es. `.ov-…`, `.rc-…`). Se manca un'icona, definisci un `<symbol>` locale nel tuo template (non modificare `_icons.html`).

## 4. Template: come si scrive una pagina

```jinja
{% extends "panel/base.html" %}
{% from "panel/_ui.html" import icon, page_head, stat_card, link_card, badge, empty_state, tabs, pager with context %}
{% block title %}Scontrini{% endblock %}
{% block head %}<link rel="stylesheet" href="{{ url_for('static', filename='panel-receipts.css') }}">{% endblock %}
{% block content %}
  {% call page_head("Scontrini", "Tutti gli scontrini emessi dal locale.") %}
    <a class="btn btn-secondary" href="…">{{ icon('download', 16) }} Esporta</a>
  {% endcall %}
  …
{% endblock %}
```

### Macro (in `panel/_ui.html`)
- `icon(nome, dimensione=20)`: home, receipt, file, users, user, message, chart, trend, plug, settings, logout, plus, search, download, check, x, alert, info, qr, external, chevron, menu, calendar, wallet, eye, send, trash, undo, copy, key, phone, star, clock, filter, printer, sparkle.
- `page_head(titolo, sottotitolo)` con `{% call %}` per i pulsanti a destra.
- `stat_card(href, etichetta, valore, sub, icon_name, tone, hint)` — card statistica interamente cliccabile. `tone`: brand | warn | danger | info | neutral.
- `link_card(href, titolo, testo, icon_name, tone, external)` — card-scorciatoia interamente cliccabile.
- `badge(testo, tone)` — ok | warn | danger | info | neutral.
- `empty_state(titolo, testo, href, cta, icon_name)`.
- `tabs([(etichetta, url, attiva, conteggio), …])` — filtri a schede.
- `pager(page, pages, 'endpoint')` — mantiene i filtri nell'URL.

### Classi principali (in `panel.css`)
Layout: `.stack`, `.stack-sm`, `.row`, `.row-between`, `.grid .cols-2|3|4`, `.grid.split` (2/3 + 1/3), `.section-head`.
Card: `.card`, `.card.flush`, `.card-head`, `.card.is-link`, `.stat-card`, `.action-card`, `.card-go`.
Bottoni: `.btn .btn-primary|secondary|ghost|danger`, `.btn-sm`, `.btn-block`, `.btn-icon`, `button.link`.
Tabelle: `.table-wrap > table.table`, `tr[data-href="…"]` (riga cliccabile; metti comunque un vero `<a>` nella prima cella), `.num`, `.cell-sub`, `tr.is-void`, `.row-actions`.
Form: `.input`, `.select`, `.textarea`, `.field` (+ `.hint`), `.form-grid`, `.form-actions`, `.toolbar`.
Varie: `.badge`, `.tabs/.tab`, `.callout (.warn)`, `.empty`, `.pager`, `.kv` (dl), `pre.code`, `.muted`, `.small`, `.mono`, `.nowrap`, `.chart` (SVG), `.divider`, `.list-plain`.
Comportamenti JS (`panel.js`): `tr[data-href]` cliccabile; `data-autosubmit` su `<select>` invia il form; `data-confirm` su `<form>`; `data-copy="#selettore"` su un bottone copia il testo dell'elemento.

## 5. Dati: regole per far girare tutto anche su Postgres

- Database: `ctx.get_db()`. Segnaposto `?`. `db.execute(sql, params).fetchall()/fetchone()`. Le righe si leggono per nome (`r["total_cents"]`) o per posizione (`r[0]`). Per inserire e avere l'id: `db.insert(sql, params)`. Dopo ogni scrittura `db.commit()`.
- SQL **portabile** (SQLite e Postgres): niente `strftime`, `date()`, `SUM(booleano)`, `INSERT OR IGNORE`, `GROUP_CONCAT`. `LIKE` in Postgres distingue maiuscole: usa `LOWER(col) LIKE ?` con il parametro già in minuscolo. In `GROUP BY` elenca tutte le colonne non aggregate. Raggruppamenti per giorno/ora: **fallo in Python**, dopo aver letto le righe.
- Le date sono stringhe ISO in **UTC** (`2026-11-03T08:10:00+00:00`): ordinarle e confrontarle come testo funziona. Per mostrarle in ora locale: filtro `|dt` nei template, oppure `ctx.LOCAL_TZ` in Python.
- I soldi sono **interi in centesimi**. Nei template: `{{ cents|euro }}` (es. `1.234,50 €`). Quantità: `{{ q|qty }}`.
- **Scontrini annullati** (`voided_at IS NOT NULL`): non contano in incassi, conteggi, medie, grafici e fatture da evadere; restano visibili nell'elenco con il badge «annullato».
- Ogni query deve essere filtrata per `shop_id = g.panel_shop["id"]`: **un locale non deve mai vedere i dati di un altro** (c'è un test).
- Helper in `ctx`: `get_db, login_required, public_url(token), euro, fmt_dt, now_iso, now_utc, parse_iso, LOCAL_TZ, csv_safe, valid_piva, valid_email, new_api_key, parse_items, insert_receipt, vat_breakdown, messaging`. Periodi: `from .common import period_since, day_bounds, local_date, PERIOD_LABELS`.

## 6. Contratto di collegamento tra sezioni

Gli endpoint sotto **esistono già**: collegati a loro con `url_for(...)` anche se la pagina di destinazione non è ancora stata rifinita. Parametri URL standard (tutti facoltativi):

| Endpoint | URL | Parametri |
|---|---|---|
| `dashboard` | `/dashboard` | |
| `receipts_list` | `/dashboard/receipts` | `period`, `day`, `status`=all\|valid\|void, `payment` (es. Carta), `q`, `page` |
| `receipt_detail` | `/dashboard/receipts/<rid>` | rid = id interno |
| `receipt_void` / `receipt_restore` | POST `/dashboard/receipts/<rid>/void\|restore` | campo `page` per tornare alla pagina giusta |
| `invoices_list` | `/dashboard/invoices` | `status`=all\|nuova\|evasa, `q` |
| `contacts_list` | `/dashboard/contacts` | `q`, `page` |
| `messages_list` | `/dashboard/messages` | `period`, `status`=all\|sent\|failed, `channel`=all\|whatsapp\|sms, `page` |
| `stats_page` | `/dashboard/stats` | `period`=7d\|30d\|90d |
| `integration` | `/dashboard/integration` | |
| `settings` | `/dashboard/settings` | |
| `test_receipt` | POST `/dashboard/test-receipt` | crea uno scontrino di prova e apre la pagina cliente |
| `receipt_view` / `receipt_qr` | pubblico `/r/<token>` e `/r/<token>/qr.svg` | |

**Chi implementa cosa:** i parametri di `receipts_list` li implementa l'agente Scontrini; quelli di `invoices_list`/`contacts_list`/`messages_list` l'agente Fatture-Clienti-Messaggi; `stats_page` l'agente Statistiche. Gli altri si limitano a **generare link** con quei parametri.

## 7. Vincoli che i test esistenti impongono (`test_app.py`)

Devono continuare a passare. In particolare:
- Dopo la registrazione e dopo `POST /dashboard/settings/regenerate-key` la pagina raggiunta (con redirect) deve contenere l'elemento esatto `id="apikey">LA_CHIAVE<` (senza spazi). La registrazione rimanda a `settings`.
- La panoramica mostra «5,00 €» come incasso di oggi e **non** «15,00 €» quando c'è uno scontrino da 5 € valido e uno annullato da 10 €.
- L'elenco scontrini dopo l'annullo contiene «annullato» e «Ripristina»; dopo il ripristino il messaggio contiene «ripristinato».
- L'export `/dashboard/invoices.csv` contiene la ragione sociale; un altro locale non vede le fatture altrui; annullo/ripristino di un altro locale → 404; POST senza csrf → 400; senza login → 302 verso il login.
- Pagina cliente `/r/<token>`: nome del locale, totale («7,90 €»), «SCONTRINO ANNULLATO», niente `name="company_name"` se annullato, «Richiesta di fattura inviata».

## 8. Come lavorare e verificare (obbligatorio)

Ogni agente usa **una porta e un database propri** (indicati nel suo incarico) per non interferire con gli altri:

```bash
cd /home/claude/scontrino-digitale
export DATABASE_PATH=<scratchpad>/<agente>/demo.db SECRET_KEY=dev MESSAGING_PROVIDER=console OUTBOX_PATH=<scratchpad>/<agente>/outbox.log
mkdir -p <scratchpad>/<agente>
python seed_demo.py --reset                       # ~1200 scontrini, annullati, fatture, messaggi, clienti
PORT=<porta> nohup python app.py > <scratchpad>/<agente>/server.log 2>&1 &
# accesso:  demo@scontrino.test / demo12345
```
I template Flask si ricaricano solo riavviando il server: riavvialo dopo ogni modifica ai `.py` (e ai template, se non vedi il cambiamento). **Per fermare il tuo server usa `kill $(lsof -ti :<porta>)` o `fuser -k <porta>/tcp`: mai `pkill -f python`**, perché fermeresti i server degli altri.

**Screenshot con Playwright (obbligatorio, e guardali davvero con lo strumento Read):** da `/home/claude` esegui uno script Node con `require('playwright')` (già installato, `chromium.launch()` senza opzioni). Controlla ogni tua pagina a **1366×800**, **820×1000**, **390×800**, in **chiaro e scuro** (`colorScheme: 'dark'` nel contesto). Verifica anche: nessun overflow orizzontale (`document.documentElement.scrollWidth <= window.innerWidth`), nessun errore in console (`page.on('console')` / `pageerror`: le violazioni CSP compaiono lì), tutti i link delle card rispondono 200. **Correggi e ricontrolla almeno una volta** prima di dichiarare finito.

**Test:** aggiungi `test_panel_<sezione>.py` (usa `testutil.py`: `appmod, make_config, csrf_of, register, seed_receipts`). Esegui **solo su SQLite**: `python -m pytest -q test_app.py test_panel_<sezione>.py`. **Non** impostare `TEST_DATABASE_URL`: il Postgres di prova è condiviso e i test lo cancellano. A fine lavoro rilancia anche tutti i test esistenti.

## 9. Come consegnare

Rispondi con un resoconto **breve**: file creati/modificati, cosa hai costruito, i test aggiunti e il loro esito, eventuali deviazioni dal contratto o richieste per l'integrazione (es. un'icona che manca), e cosa NON sei riuscito a fare. Non fare commit né push: li fa il coordinatore.

---

## 9. Amministrazione (proprietario del servizio)

Il **proprietario** è l'account la cui email compare in `OWNER_EMAIL` (variabile d'ambiente, separate da virgola;
in test: `cfg["OWNER_EMAILS"] = {"owner@x.it"}` dopo `make_config`). Non c'è modo di diventarlo registrandosi.
Le **aziende** (clienti del servizio) hanno ognuna il proprio account: le crea il proprietario, che riceve un
**link d'invito monouso** (valido `INVITE_DAYS` = 7 giorni) da girare all'azienda. L'azienda lo apre su
`/invito/<token>`, sceglie la password ed entra; a quel punto nasce la sua chiave API (si vede una volta sola).
Stati di un'azienda (`shops.status`): `invited` (non ha ancora accettato) · `active` · `suspended`.

Nel menu il gruppo **Amministrazione** (Panoramica servizio · Aziende · Registro attività) compare solo al
proprietario. Le pagine usano lo stesso layout (`panel/base.html`, componenti di `_ui.html`, regole §1-§8).

### 9.1 Funzioni in `ctx` (già implementate in app.py: NON modificarle, usarle)
| Funzione | Cosa fa |
|---|---|
| `@ctx.owner_required` | Decoratore di OGNI route di amministrazione. Non proprietario → 404; anonimo → login. |
| `ctx.create_invited_shop(name, email, notes="")` | Crea l'azienda `invited` → `(id, token)`. Prima controlla tu che l'email non esista. Il token è IN CHIARO e va mostrato **una sola volta**. |
| `ctx.issue_invite(shop_id)` | Nuovo link (annulla il precedente) → token. Serve a re-invitare un'azienda `invited` o a far **reimpostare la password** a una `active`. |
| `ctx.invite_url(token)` | URL completo da copiare. |
| `ctx.set_shop_status(shop_id, "suspended"\|"active")` | Sospende (solo `active`) / riattiva (solo `suspended`). Registra nel registro. Alza `ValueError` se non consentito (es. account del proprietario). |
| `ctx.delete_shop(shop_id)` | Elimina azienda e TUTTI i suoi dati. Irreversibile. `ValueError` se non consentito. |
| `ctx.begin_impersonation(shop_id)` | Il proprietario entra nel pannello dell'azienda (solo `active`). Poi tu fai `redirect(url_for("dashboard"))`. Un banner e la voce «Torna all'amministrazione» sono già nel layout. |
| `ctx.audit(action, target_shop_row=None, detail="")` | Scrive nel registro (`admin_audit`). `detail` ≤ 300 caratteri, **mai** token, password o chiavi API. |
| `ctx.is_owner(shop_row)` | Booleano. |

Tabella `admin_audit(id, actor_shop_id, actor_email, action, target_shop_id, target_name, detail, created_at)`
(nessuna FK: resta dopo l'eliminazione dell'azienda). Azioni già scritte dal core: `company.suspend`,
`company.activate`, `company.delete`, `company.invite_accepted`, `company.password_reset`,
`impersonation.start`, `impersonation.stop`, `impersonation.action` (ogni POST fatto mentre si guarda un cliente).
Azioni che scrive la sezione Aziende: `company.create`, `company.invite_reissued`, `company.notes`.
Colonne utili di `shops`: `status, invite_expires_at, last_login_at, owner_notes, created_at, name, email`.

### 9.2 Chi possiede cosa (file disgiunti, come nel resto del progetto)
- **Agente «Aziende»**: `sections/admin_companies.py`, `templates/panel/admin_companies.html`,
  `admin_company_new.html`, `admin_company_detail.html`, `public/static/panel-admin-companies.css` (classi `.ac-`),
  `test_admin_companies.py`.
- **Agente «Panoramica servizio»**: `sections/admin_overview.py`, `templates/panel/admin_overview.html`,
  `admin_activity.html`, `public/static/panel-admin-overview.css` (classi `.ao-`), `test_admin_overview.py`.
- Il core (`app.py`, `db.py`, `sections/__init__.py`, `panel/base.html`, `_ui.html`, `panel.css`, `panel.js`,
  `invite.html`) NON si tocca: i difetti si segnalano nel resoconto.

### 9.3 Endpoint e link (contratto)
| Endpoint | Percorso | Proprietario |
|---|---|---|
| `admin_overview` | `GET /admin` | Panoramica servizio |
| `admin_activity` | `GET /admin/activity?action=&q=&page=` | Panoramica servizio |
| `admin_companies` | `GET /admin/companies?status=&q=&sort=&page=` | Aziende |
| `admin_company_new` | `GET/POST /admin/companies/new` | Aziende |
| `admin_company_detail` | `GET /admin/companies/<int:cid>` | Aziende |
| azioni sul dettaglio | `POST /admin/companies/<cid>/suspend`, `/activate`, `/invite` (nuovo link/reset password), `/notes`, `/impersonate`, `/delete` | Aziende |

Filtri di `admin_companies`: `status = all | active | invited | suspended | inactive` (*inactive* = attiva ma senza
scontrini negli ultimi 14 giorni), `sort = recent | name | activity`, `q` cerca in nome ed email.
La Panoramica servizio collega ogni sua card a questi URL.

### 9.4 Regole di sicurezza (vincolanti)
1. Ogni route: `@ctx.owner_required`. Le modifiche solo con POST (il CSRF è già globale). Ogni query sulle
   aziende altrui è ammessa SOLO qui, in sola lettura aggregata; mai mostrare password, hash o chiavi API.
2. Il link d'invito compare una sola volta, subito dopo creazione/reset (passalo alla pagina successiva con
   `session["invite_once"] = {"cid":…, "url":…}` e rimuovilo con `session.pop` alla prima visualizzazione). Mai
   nel database in chiaro, mai nel registro, mai in un log, mai in un URL di redirect.
3. Eliminazione: serve scrivere il **nome esatto** dell'azienda E la **password del proprietario**
   (`check_password_hash(g.panel_shop["password_hash"], …)`); altrimenti non si elimina nulla. Mostra prima un
   riepilogo di cosa verrà perso (n. scontrini, fatture, contatti).
4. Sospendere/eliminare/impersonare l'account del proprietario o un'azienda inesistente: `ValueError`/404, mai 500.
5. Niente `style=` né script inline (CSP, §2); testi in italiano; tutto filtrato e paginato lato server.
6. SQL portabile SQLite/Postgres (§5): conteggi per azienda con sottoselect o `GROUP BY` completo; date in Python.
