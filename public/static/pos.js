// Terminale di cassa. Nessun HTML da stringhe: i nodi si costruiscono con h() (testo sempre escapato).
(function () {
  "use strict";
  var CSRF = document.querySelector('meta[name="csrf"]').content;
  var app = document.getElementById("pos-app");
  var toastEl = document.getElementById("pos-toast");
  var S = { view: "floor", tables: [], order: null, menu: null, cat: null, staff: null, lock: null, comanda: null };
  var timer = null, toastT = null;

  function h(tag, attrs, kids) {
    var el = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      var v = attrs[k];
      if (v === null || v === undefined || v === false) return;
      if (k === "class") el.className = v;
      else if (k.slice(0, 2) === "on") el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : v);
    });
    [].concat(kids === undefined ? [] : kids).forEach(function (c) {
      if (c === null || c === undefined || c === false) return;
      el.appendChild(typeof c === "object" ? c : document.createTextNode(String(c)));
    });
    return el;
  }
  function canCash() { return !S.staff || S.staff.role !== "cameriere"; }
  function eur(c) { return (c / 100).toFixed(2).replace(".", ",") + " €"; }
  function cents(txt) {
    var n = parseFloat(String(txt).replace(",", "."));
    return isFinite(n) ? Math.round(n * 100) : NaN;
  }
  function toast(msg, err) {
    toastEl.textContent = msg; toastEl.className = "pos-toast" + (err ? " err" : ""); toastEl.hidden = false;
    clearTimeout(toastT); toastT = setTimeout(function () { toastEl.hidden = true; }, err ? 5000 : 3000);
  }
  function api(method, path, body) {
    return fetch("/cassa/api" + path, {
      method: method, credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF": CSRF },
      body: body === undefined ? undefined : JSON.stringify(body)
    }).then(function (r) {
      return r.json().then(function (j) {
        if (!r.ok) { var e = new Error(j.error || "Errore"); e.status = r.status; e.locked = !!j.locked; throw e; }
        return j;
      });
    });
  }
  function fail(e) {
    if (e.status === 401) { location.href = "/login"; return; }
    if (e.locked) { startLock(); return; }
    toast(e.message, true);
    refresh();
  }
  function act(method, path, body, after) {
    return api(method, path, body).then(function (j) { if (j.id) S.order = j; if (after) after(j); render(); }).catch(fail);
  }

  // ---------------------------------------------------------------- sala
  function tableClass(t) {
    if (t.status === "free") return "";
    if (t.status === "open") return "s-open";
    if (t.customer_status === "confirmed") return "s-ok";
    if (t.customer_status === "disputed") return "s-err";
    return "s-wait";
  }
  function tableText(t) {
    if (t.status === "free") return "Libero";
    if (t.status === "open") return "Si sta ordinando";
    return { pending: "Preconto: attende il cliente", confirmed: "Cliente ha confermato", disputed: "Il cliente segnala un errore" }[t.customer_status] || "Preconto";
  }
  function viewFloor() {
    return h("div", {}, [
      h("h2", {}, "Sala"),
      h("p", { class: "pos-sub" }, "Tocca un tavolo per aprire o riprendere il conto."),
      h("div", { class: "floor" }, S.tables.map(function (t) {
        return h("button", { class: "tbl " + tableClass(t), onclick: function () { openTable(t); } }, [
          h("b", {}, t.label), h("span", {}, tableText(t)),
          h("span", { class: "amt" }, t.status === "free" ? " " : eur(t.total_cents))
        ]);
      })),
      h("p", { class: "legend" }, "Giallo scuro: attende il cliente · Verde: cliente ha confermato · Rosso: errore segnalato")
    ]);
  }
  function openTable(t) {
    api("POST", "/orders", { table_id: t.id, covers: 0 }).then(function (o) {
      S.order = o; S.view = "order"; return loadMenu();
    }).then(render).catch(fail);
  }
  function loadMenu() {
    if (S.menu) return Promise.resolve();
    return api("GET", "/menu").then(function (m) { S.menu = m; S.cat = m.categories.length ? m.categories[0].id : null; });
  }

  // ---------------------------------------------------------------- conto
  function viewOrder() {
    var o = S.order, editable = o.status === "open";
    var left = h("div", {}, [editable ? viewMenu() : viewStatus(o)]);
    var right = h("div", { class: "panel" }, viewBill(o));
    return h("div", {}, [
      h("div", { class: "row head" }, [
        h("button", { onclick: toFloor }, "← Sala"),
        h("h2", {}, o.table + (o.covers ? " · " + o.covers + " coperti" : ""))
      ]),
      h("div", { class: "order" }, [left, right])
    ]);
  }
  function toFloor() { S.view = "floor"; S.order = null; refresh(); }
  function viewMenu() {
    var m = S.menu;
    if (!m || !m.products.length) return h("div", { class: "panel" }, [h("p", {}, "Il menu è vuoto."),
      h("a", { href: "/dashboard/menu" }, "Aggiungi i prodotti dal pannello")]);
    var prods = m.products.filter(function (p) { return p.category_id === S.cat; });
    return h("div", { class: "panel" }, [
      h("div", { class: "cats" }, m.categories.map(function (c) {
        return h("button", { class: c.id === S.cat ? "on" : "", onclick: function () { S.cat = c.id; render(); } }, c.name);
      })),
      h("div", { class: "prods" }, prods.map(function (p) {
        return h("button", { class: "prod", onclick: function () { act("POST", "/orders/" + S.order.id + "/items", { product_id: p.id, qty: 1 }); } },
          [h("b", {}, p.name), h("span", {}, eur(p.price))]);
      }))
    ]);
  }
  function deliveryRow(o) {
    var who = o.delivery_by === "cliente" ? "scelto dal cliente" : (o.delivery_by ? "registrato da " + o.delivery_by : "");
    function pick(pref, label) {
      return h("button", { class: o.delivery === pref ? "primary" : "", onclick: function () { act("POST", "/orders/" + o.id + "/delivery", { pref: pref }); } }, label);
    }
    return h("div", {}, [
      h("p", { class: "pos-sub" }, o.delivery === "digital" ? "Documento fiscale: SOLO DIGITALE, nessuna carta (" + who + ")."
        : o.delivery === "paper" ? "Documento fiscale: il cliente vuole la CARTA (" + who + ")."
        : "Il cliente non ha ancora scelto come vuole il documento fiscale. Se dice a voce che va bene solo digitale, registralo qui:"),
      h("div", { class: "row" }, [pick("digital", "Solo digitale"), pick("paper", "Vuole la carta")])
    ]);
  }
  function viewStatus(o) {
    var kids = [];
    if (o.status === "preconto") {
      if (o.customer_status === "disputed") {
        kids.push(h("div", { class: "banner err" }, ["Il cliente segnala un errore: ", h("em", {}, o.customer_note || "(nessuna nota)")]));
        kids.push(h("button", { class: "primary", onclick: function () { act("POST", "/orders/" + o.id + "/reopen", {}); } }, "Riapri il conto per correggerlo"));
      } else if (o.customer_status === "confirmed") {
        kids.push(deliveryRow(o));
        kids.push(h("div", { class: "banner ok" }, o.confirmed_by === "cliente" ? "Il cliente ha verificato e confermato il conto. Puoi incassare." : "Conto confermato a voce da " + o.confirmed_by + ". Puoi incassare."));
      } else {
        kids.push(h("div", { class: "banner wait" }, "PRECONTO (non fiscale): in attesa che il cliente lo verifichi"));
        if (o.bill) {
          kids.push(h("img", { class: "qr", src: o.bill.qr, alt: "QR del preconto" }));
          kids.push(h("span", { class: "link" }, o.bill.url));
        }
        kids.push(h("div", { class: "row" }, [
          h("button", { class: "primary", onclick: function () { act("POST", "/orders/" + o.id + "/confirm", {}); } }, "Il cliente ha verificato a voce"),
          h("button", { onclick: function () { act("POST", "/orders/" + o.id + "/reopen", {}); } }, "Riapri per modificare")
        ]));
      }
    } else if (o.status === "paid") {
      kids.push(h("div", { class: "banner ok" }, "Conto chiuso e pagato."));
      if (o.delivery === "paper") kids.push(h("div", { class: "banner err" }, "Il cliente vuole la copia CARTACEA: stampala dal registratore."));
      else if (o.delivery === "digital") kids.push(h("div", { class: "banner ok" }, "Il cliente ha scelto il documento solo digitale: niente carta."));
      if (o.receipt) {
        kids.push(h("img", { class: "qr", src: o.receipt.qr, alt: "QR dello scontrino digitale" }));
        kids.push(h("a", { class: "link", href: o.receipt.url, target: "_blank", rel: "noopener" }, "Scontrino digitale n. " + o.receipt.number + " — " + o.receipt.url));
      }
      if (o.fiscal_status === "emesso") kids.push(h("div", { class: "banner ok" }, "Documento commerciale emesso n. " + o.fiscal_number + "."));
      else if (o.rt === "queued" || o.rt === "printing") kids.push(h("div", { class: "banner wait" }, "Documento fiscale in stampa sul registratore…"));
      else if (o.rt === "failed" || o.rt === "uncertain") kids.push(h("div", { class: "banner err" }, "Il registratore non ha confermato il documento: controlla in «Da fiscalizzare». " + (o.rt_error || "")));
      else kids.push(h("div", { class: "banner fisc" }, "Scontrino digitale = copia di cortesia, NON fiscale. Resta da emettere il documento commerciale (registratore telematico o portale Agenzia delle Entrate): lo registri in «Da fiscalizzare»."));
    } else if (o.status === "void") {
      kids.push(h("div", { class: "banner" }, "Conto annullato."));
    }
    return h("div", { class: "panel" }, kids);
  }
  function viewBill(o) {
    var t = o.totals, editable = o.status === "open", kids = [h("h2", {}, "Conto")];
    if (!o.items.length) kids.push(h("p", { class: "pos-sub" }, "Nessun prodotto: tocca il menu per aggiungere."));
    o.items.forEach(function (i) {
      kids.push(h("div", { class: "line" }, [
        editable ? h("div", { class: "q" }, [
          h("button", { "aria-label": "Meno", onclick: function () { act("PATCH", "/orders/" + o.id + "/items/" + i.id, { qty: i.qty - 1 }); } }, "−"),
          h("b", {}, i.qty),
          h("button", { "aria-label": "Più", onclick: function () { act("PATCH", "/orders/" + o.id + "/items/" + i.id, { qty: i.qty + 1 }); } }, "+")
        ]) : h("b", {}, i.qty + "×"),
        h("div", {}, [i.name, i.note ? h("small", {}, i.note) : null, editable ? h("small", {}, i.sent ? "✓ in cucina" : "da inviare") : null]),
        h("b", {}, eur(i.total))
      ]));
    });
    if (t.discount) kids.push(h("div", { class: "pay-row" }, [h("span", {}, "Sconto"), h("span", {}, "− " + eur(t.discount))]));
    kids.push(h("div", { class: "tot" }, [h("span", {}, "Totale"), h("span", {}, eur(t.total))]));
    o.payments.forEach(function (p) {
      kids.push(h("div", { class: "pay-row" }, [h("span", {}, "Incassato " + p.method),
        h("span", {}, eur(p.amount) + " "), o.status === "preconto" && canCash() ? h("button", { "aria-label": "Annulla pagamento", onclick: function () { act("DELETE", "/orders/" + o.id + "/payments/" + p.id); } }, "✕") : null]));
    });
    if (editable) {
      kids.push(h("div", { class: "row" }, [
        h("button", { onclick: function () { askCover(o); } }, "Coperti"),
        canCash() ? h("button", { onclick: function () { askDiscount(o); } }, "Sconto") : null,
        canCash() ? h("button", { onclick: function () { askCustom(o); } }, "Riga libera") : null
      ]));
      if (o.unsent) kids.push(h("div", { class: "row" }, [
        h("button", { class: "kitchen", onclick: sendKitchen }, "Invia in cucina (" + o.unsent + " " + (o.unsent === 1 ? "riga" : "righe") + ")")
      ]));
      if (S.comanda && S.comanda.oid === o.id) kids.push(h("a", { class: "banner ok comanda-link", href: S.comanda.url, target: "_blank", rel: "noopener" }, "Comanda inviata: stampa il foglio per la cucina →"));
      kids.push(h("div", { class: "row" }, [
        canCash() ? h("button", { class: "danger", onclick: function () { if (confirm("Annullare il conto?")) act("POST", "/orders/" + o.id + "/cancel", {}, toFloorSoon); } }, "Annulla conto") : null,
        h("button", { class: "primary", disabled: !o.items.length, onclick: function () { act("POST", "/orders/" + o.id + "/bill", {}); } }, "Preconto")
      ]));
    } else if (o.status === "preconto" && o.customer_status === "confirmed") {
      kids.push(canCash() ? payBox(o) : h("div", { class: "banner wait" }, "Conto confermato dal cliente: l'incasso lo fa un cassiere (serve il suo PIN)."));
    }
    return kids;
  }
  function sendKitchen() {
    api("POST", "/orders/" + S.order.id + "/comanda", {}).then(function (j) {
      S.order = j; S.comanda = { oid: j.id, url: j.comanda_url }; toast("Comanda pronta: stampala per la cucina"); render();
    }).catch(fail);
  }
  function toFloorSoon() { setTimeout(toFloor, 0); }
  function askCover(o) {
    var v = prompt("Quanti coperti?", o.covers || ""); if (v === null) return;
    act("POST", "/orders/" + o.id + "/covers", { covers: parseInt(v, 10) || 0 });
  }
  function askDiscount(o) {
    var v = prompt("Sconto in euro (0 per toglierlo)", o.discount_cents ? (o.discount_cents / 100) : ""); if (v === null) return;
    var c = cents(v || "0"); if (isNaN(c)) return toast("Importo non valido", true);
    act("POST", "/orders/" + o.id + "/discount", { discount_cents: c });
  }
  function askCustom(o) {
    var name = prompt("Nome della riga (es. Coperto)"); if (!name) return;
    var price = cents(prompt("Prezzo in euro, IVA inclusa") || ""); if (isNaN(price)) return toast("Prezzo non valido", true);
    var vat = parseInt(prompt("IVA % (4, 5, 10, 22)", "10") || "10", 10);
    act("POST", "/orders/" + o.id + "/custom", { name: name, price_cents: price, vat_rate: vat });
  }
  function payBox(o) {
    var due = o.totals.due;
    if (due <= 0) {
      return h("div", {}, [h("button", { class: "primary", onclick: function () {
        act("POST", "/orders/" + o.id + "/close", {}, function () { toast("Conto chiuso: scontrino digitale creato"); });
      } }, "Chiudi conto e crea lo scontrino")]);
    }
    var amount = h("input", { id: "pay-amount", inputmode: "decimal", value: (due / 100).toFixed(2).replace(".", ",") });
    var given = h("input", { id: "pay-given", inputmode: "decimal", placeholder: "Contante ricevuto (facoltativo)" });
    function go(method) {
      var a = cents(amount.value); if (isNaN(a) || a <= 0) return toast("Importo non valido", true);
      var body = { method: method, amount_cents: a };
      if (method === "contanti" && given.value.trim()) { var g = cents(given.value); if (isNaN(g)) return toast("Contante non valido", true); body.tendered_cents = g; }
      api("POST", "/orders/" + o.id + "/pay", body).then(function (j) {
        S.order = j; if (j.change_cents) toast("Resto da dare: " + eur(j.change_cents)); render();
      }).catch(fail);
    }
    return h("div", {}, [
      h("p", { class: "pos-sub" }, "Resta da pagare " + eur(due) + ". Per il conto alla romana incassa una parte alla volta."),
      h("label", { for: "pay-amount" }, "Importo di questo incasso (€)"), amount, given,
      h("div", { class: "methods" }, [["contanti", "Contanti"], ["carta", "Carta"], ["buoni", "Buoni pasto"], ["altro", "Altro"]].map(function (m) {
        return h("button", { class: "primary", onclick: function () { go(m[0]); } }, m[1]);
      }))
    ]);
  }

  // ---------------------------------------------------------------- PIN del personale
  function showWho() {
    var who = document.getElementById("pos-who");
    if (!who) return;
    who.textContent = "";
    if (S.staff) {
      who.appendChild(h("span", {}, S.staff.name + " · " + ({ cameriere: "cameriere", cassiere: "cassiere", titolare: "titolare" }[S.staff.role] || "")));
      who.appendChild(h("button", { onclick: function () { api("POST", "/lock", {}).then(function () { S.staff = null; S.order = null; S.view = "floor"; startLock(); }); } }, "Blocca"));
    }
  }
  function startLock() {
    S.lock = { pick: null, pin: "" };
    S.order = null; S.view = "floor";
    api("GET", "/staff").then(function (j) {
      if (!j.locked) { S.lock = null; return refresh(); }
      S.lock.staff = j.staff; renderLock();
    }).catch(fail);
  }
  function renderLock() {
    var L = S.lock; app.textContent = "";
    var box = h("div", { class: "panel lock" }, []);
    if (!L.pick) {
      box.appendChild(h("h2", {}, "Chi sei?"));
      box.appendChild(h("div", { class: "floor" }, L.staff.map(function (m) {
        return h("button", { class: "tbl", onclick: function () { L.pick = m; L.pin = ""; renderLock(); } }, [h("b", {}, m.name), h("span", {}, m.role)]);
      })));
    } else {
      box.appendChild(h("h2", {}, "Ciao " + L.pick.name + ", inserisci il PIN"));
      box.appendChild(h("div", { class: "pin-dots", "aria-live": "polite" }, L.pin.length ? "●".repeat(L.pin.length) : "—"));
      var pad = h("div", { class: "pinpad" }, []);
      "123456789".split("").concat(["←", "0", "OK"]).forEach(function (k) {
        pad.appendChild(h("button", { class: k === "OK" ? "primary" : "", onclick: function () {
          if (k === "←") L.pin = L.pin.slice(0, -1);
          else if (k === "OK") return submitPin();
          else if (L.pin.length < 6) L.pin += k;
          renderLock();
        } }, k));
      });
      box.appendChild(pad);
      box.appendChild(h("button", { onclick: function () { L.pick = null; renderLock(); } }, "← Cambia persona"));
    }
    app.appendChild(box);
  }
  function submitPin() {
    var L = S.lock;
    api("POST", "/unlock", { staff_id: L.pick.id, pin: L.pin }).then(function (j) {
      S.staff = j.staff; S.lock = null; showWho(); refresh().then(render);
    }).catch(function (e) { L.pin = ""; toast(e.message, true); renderLock(); });
  }

  // ---------------------------------------------------------------- ciclo
  function render() {
    if (S.lock) { return renderLock(); }
    app.textContent = "";
    app.appendChild(S.view === "order" && S.order ? viewOrder() : viewFloor());
  }
  function refresh() {
    var p = S.view === "order" && S.order
      ? api("GET", "/orders/" + S.order.id).then(function (o) { S.order = o; })
      : api("GET", "/floor").then(function (j) { S.tables = j.tables; S.staff = j.staff; showWho(); });
    return p.then(function () { if (!document.activeElement || document.activeElement.tagName !== "INPUT") render(); }).catch(function (e) { if (e.status === 401) location.href = "/login"; });
  }
  if (document.querySelector('meta[name="pos-staff-mode"]')) { startLock(); }
  else { refresh().then(function () { if (S.view === "floor") render(); }); }
  timer = setInterval(function () { if (!document.hidden && !S.lock) refresh(); }, 4000);
})();
