// Piccoli comportamenti della pagina (niente script inline: la CSP lo vieta).
document.addEventListener("click", function (e) {
  var printBtn = e.target.closest("[data-print]");
  if (printBtn) { window.print(); return; }

  var copyBtn = e.target.closest("[data-copy]");
  if (copyBtn) {
    var target = document.querySelector(copyBtn.getAttribute("data-copy"));
    if (target && navigator.clipboard) {
      navigator.clipboard.writeText(target.textContent.trim()).then(function () {
        copyBtn.textContent = "Copiata ✓";
      });
    }
  }
});

document.addEventListener("submit", function (e) {
  var msg = e.target.getAttribute("data-confirm");
  if (msg && !window.confirm(msg)) { e.preventDefault(); }
});

// Preconto del cliente: si aggiorna da solo quando il personale cambia il conto.
(function () {
  var m = document.querySelector('meta[name="pos-bill-poll"]');
  if (!m) return;
  var last = null;
  setInterval(function () {
    if (document.hidden || document.querySelector("details[open], textarea:focus")) return;
    fetch(m.content, { credentials: "same-origin" }).then(function (r) { return r.text(); }).then(function (t) {
      var sig = t.replace(/name="csrf" value="[^"]*"/g, "").length + ":" + (t.match(/Totale da pagare[\s\S]{0,80}/) || [""])[0];
      if (last !== null && sig !== last) { window.location.reload(); }
      last = sig;
    }).catch(function () {});
  }, 5000);
})();
