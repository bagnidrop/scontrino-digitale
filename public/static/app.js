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
