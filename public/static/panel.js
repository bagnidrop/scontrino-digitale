// Comportamenti del pannello. Niente script inline (la CSP lo vieta): tutto passa da attributi data-*.
//   data-nav-toggle / data-nav-close : apre/chiude il menu su telefono
//   tr[data-href]                    : riga di tabella cliccabile (la prima cella contiene comunque un link vero)
//   data-autosubmit                  : un <select> o <input> con questo attributo invia il form quando cambia
(function () {
  var body = document.body;

  document.addEventListener("click", function (e) {
    if (e.target.closest("[data-nav-toggle]")) { body.classList.toggle("nav-open"); return; }
    if (e.target.closest("[data-nav-close]")) { body.classList.remove("nav-open"); return; }

    var row = e.target.closest("tr[data-href]");
    if (row && !e.target.closest("a, button, input, select, textarea, label, form")) {
      var href = row.getAttribute("data-href");
      if (e.metaKey || e.ctrlKey) { window.open(href, "_blank", "noopener"); } else { window.location.href = href; }
    }
  });

  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") { body.classList.remove("nav-open"); }
  });

  document.addEventListener("change", function (e) {
    var el = e.target.closest("[data-autosubmit]");
    if (el && el.form) { el.form.submit(); }
  });

  // Dopo aver scelto una voce di menu su telefono, il cassetto si chiude.
  document.querySelectorAll(".sidebar .nav-link").forEach(function (a) {
    a.addEventListener("click", function () { body.classList.remove("nav-open"); });
  });
})();
