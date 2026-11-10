# Cassa (POS) — contratto per gli agenti

Leggi prima `DESIGN.md` (regole del pannello, CSP, SQL portabile, CSRF, componenti) e poi questo file.

## Cosa stiamo costruendo
Una cassa vera per ristoranti, che sostituisce la cassa fisica per il lavoro di sala:
**tavolo → comande → PRECONTO (non fiscale) → il cliente lo verifica sul telefono (QR) e conferma →
incasso (anche conto alla romana) → chiusura: nasce lo scontrino digitale e il conto va in «da emettere»**.
Il **documento commerciale fiscale vero** lo emette un RT o il portale Agenzia delle Entrate: qui se ne registra
il numero (`fiscal.py`). **Mai** chiamare «scontrino fiscale» o «documento commerciale» ciò che non lo è:
il preconto e lo scontrino digitale portano sempre la dicitura «NON FISCALE / copia di cortesia» finché
`fiscal_status != 'emesso'`.

## Già fatto (NON modificare, solo usare)
- `db.py` → tabelle `pos_*` (in LATER_SCHEMA). `pos_service.py` → TUTTA la logica (importa e chiama, non duplicare
  SQL di scrittura). `fiscal.py` → adattatore di emissione. `testutil.py`. `test_pos_service.py` (esempio di test).
- `sections/__init__.py` → menu «Cassa» già registrato con 4 moduli segnaposto (`sections/pos_*.py`):
  ogni agente **sostituisce il proprio** modulo. Gli endpoint e i percorsi sotto sono fissi.
- Dall'app arriva `ctx.insert_receipt` (da passare a `pos.close_order`), `ctx.get_db`, `ctx.login_required`,
  `ctx.euro`, `ctx.fmt_dt`, `ctx.public_url`, `ctx.now_iso`, ecc. (vedi `app.create_app`).
- `pos_service`: vedi i docstring. Errori attesi = `pos.PosError(message, status)`: mostrali all'utente
  (flash nelle pagine, `{"error": msg}` nelle API JSON).

## Chi fa cosa (un file = un solo proprietario)
| Agente | Possiede | Endpoint (fissi) |
|---|---|---|
| **MENU** (menu e sala) | `sections/pos_menu.py`, `templates/panel/pos_menu.html`, `public/static/panel-pos-menu.{css,js}`, `test_pos_menu.py` | `pos_menu` GET `/dashboard/menu` (tab `?tab=menu|sala`); POST `pos_category_add/…`, `pos_product_add/edit/toggle`, `pos_table_add/edit/toggle`, `pos_menu_seed` (carica il menu d'esempio) |
| **CASSA** (terminale) | `sections/pos_terminal.py`, `templates/pos/*.html`, `public/static/pos.{css,js}`, `test_pos_terminal.py` | `pos_terminal` GET `/cassa` (mappa sala + conto); API JSON sotto `/cassa/api/...` |
| **CLIENTE** (preconto pubblico + fiscale) | `sections/pos_fiscal.py`, `pos_public.py` (nuovo, da registrare in `sections/__init__.py` — unico file condiviso che può toccare, solo per aggiungere la sua riga), `templates/pos_bill.html`, `templates/panel/pos_fiscal.html`, `public/static/pos-bill.{css,js}`, `public/static/panel-pos-fiscal.css`, ritocchi a `templates/receipt.html` (solo dicitura fiscale), `test_pos_public.py` | pubblico: `pos_bill` GET `/p/<token>`, POST `pos_bill_reply` `/p/<token>/reply` (conferma o segnala errore); pannello: `pos_fiscal` GET `/dashboard/fiscal`, POST `pos_fiscal_issue` |
| **TURNI** (apertura/chiusura giornata) | `sections/pos_shifts.py`, `templates/panel/pos_shifts.html`, `public/static/panel-pos-shifts.{css,js}`, `test_pos_shifts.py` | `pos_shifts` GET `/dashboard/turni`; POST `pos_shift_open`, `pos_shift_close` |

Non toccare i file degli altri. Se ti serve una funzione nuova in `pos_service.py`, **chiedila nel tuo report finale**
oppure scrivila in un tuo modulo; non modificare `pos_service.py`/`db.py` (il coordinatore li integra).

## API JSON della cassa (la implementa CASSA, la usa solo il suo JS)
Sessione = quella del pannello (login azienda). Ogni POST/PUT richiede l'header `X-CSRF: <token>` (il token è
in `<meta name="csrf" content="…">` nella pagina; verificalo lato server con `secrets.compare_digest` contro
`session["csrf"]`). Risposte `{"error": "..."}` con lo status giusto in caso di `PosError`.
- `GET  /cassa/api/floor` → tavoli con stato (`pos.floor_overview`) + `unread` conti disputati
- `POST /cassa/api/orders` `{table_id, covers}` → apre/restituisce il conto
- `GET  /cassa/api/orders/<id>` → conto completo: righe, totali, pagamenti, stato, `bill_url` (`ctx.public_url` stile `/p/<token>`) e QR (`/p/<token>/qr.svg`)
- `GET  /cassa/api/menu` → categorie e prodotti attivi
- `POST /cassa/api/orders/<id>/items` `{product_id, qty, note}` · `PATCH …/items/<item_id>` `{qty}` · `POST …/custom` `{name, price_cents, vat_rate}` · `POST …/discount` `{discount_cents}`
- `POST /cassa/api/orders/<id>/bill` (preconto) · `…/reopen` · `…/confirm` (conferma del personale) · `…/move` `{table_id}` · `…/cancel`
- `POST /cassa/api/orders/<id>/pay` `{method, amount_cents, tendered_cents}` → `{change_cents}` · `DELETE …/payments/<pid>`
- `POST /cassa/api/orders/<id>/close` → chiude (`pos.close_order(db, sid, oid, ctx.insert_receipt)`) e restituisce `{receipt_url, receipt_id}`
Il terminale ricarica lo stato ogni ~4 s (polling) per vedere la conferma del cliente.

## Pagina pubblica del preconto `/p/<token>` (CLIENTE)
Mobile-first, senza login. Mostra locale (e logo se c'è: `/logo/<etag>.png`, vedi `receipt.html`), tavolo, righe,
sconto, TOTALE, dicitura ben visibile **«PRECONTO — documento NON fiscale. Verifica che sia tutto corretto»**
e due azioni: **«È tutto corretto»** (→ `pos.customer_reply(db, token, True)`) e **«C'è un errore»** con campo
nota (→ `customer_reply(..., False, nota)`). Dopo la risposta mostra l'esito; la pagina si aggiorna da sola se il conto
cambia (meta refresh o poll con JS). Se il conto è già pagato (`paid`) e c'è `receipt_id`, mostra il link allo scontrino
`/r/<token scontrino>`. Token sconosciuto/assente → 404 uguale a quello degli scontrini. Rate limit come nelle altre pagine
pubbliche (vedi `app.py`, tabella `rate_hits`). Il QR: `/p/<token>/qr.svg` (segno, come in `app.py`).

## Fiscalizzazione (CLIENTE, pannello)
`/dashboard/fiscal`: elenco `pos.fiscal_queue` («da emettere») con totale, tavolo, data, link allo scontrino; per ogni riga un
form per registrare il numero del documento commerciale emesso (→ `pos.mark_fiscal_issued`). Testo guida chiaro:
«Emetti il documento commerciale dal registratore telematico o dal portale “Documento commerciale online” dell’Agenzia delle
Entrate, poi scrivi qui il numero». Mostra anche gli ultimi «emessi».

## Turni (TURNI)
Apri turno (fondo cassa in €), chiudi turno (contante contato): riepilogo per metodo di pagamento (da `pos_payments`
dei conti `paid` con `shift_id` del turno), numero conti, coperti, sconti, differenza di cassa (contanti attesi = fondo +
contanti incassati − resti; il resto = tendered − amount). Storico degli ultimi turni. Un solo turno aperto per azienda.
Non si può chiudere un turno con conti ancora aperti (`open`/`preconto` con `shift_id` del turno): elencali con link a `/cassa`.

## Regole comuni
- UI in italiano, tono da ristoratore, niente gergo. CSP rigida: niente inline style/script; CSRF su ogni POST.
- Importi: interi in centesimi; mostra con `ctx.euro`. SQL portabile (vedi DESIGN.md), ogni query filtrata per `shop_id`.
- Il terminale `/cassa`: tablet-first (touch, bersagli ≥48 px), chiaro/scuro, usabile anche a 390 px.
- Test: `pytest` su SQLite per i tuoi file (`make_config("<nome>.db")`), uso di `testutil`. Il coordinatore li prova anche su Postgres.
- Porta e database propri per l'avvio manuale: MENU 5071, CASSA 5072, CLIENTE 5073, TURNI 5074
  (`export DATABASE_PATH=/tmp/<tuo>.db; python seed_demo.py --reset; PORT=507x python app.py`; il locale demo è `demo@scontrino.test` / `demo12345`; per avere menu e sala nel demo chiama `pos.seed_default_menu`).
  Mai `pkill -f python`: ferma per porta (`kill $(lsof -ti :PORT)`).
- Screenshot Playwright a 1366×800, 820×1000, 390×800, chiaro e scuro, e controlla la console (zero errori/CSP).
- NON fare commit/push: li fa il coordinatore. Lavora a piccoli passi e salva spesso (rate limit).
- Report finale: cosa hai fatto, endpoint, cosa manca, funzioni richieste a `pos_service`.
