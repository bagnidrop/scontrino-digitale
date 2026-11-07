/* Anteprima live del messaggio ai clienti (stessa logica di messaging.render_template_text). */
(function () {
  var ta = document.getElementById("se-template");
  var out = document.getElementById("se-preview");
  if (!ta || !out) return;
  function render() {
    var t = ta.value.split("{negozio}").join(ta.dataset.shop).split("{link}").join(ta.dataset.link);
    out.textContent = t.slice(0, 600);
  }
  ta.addEventListener("input", render);
  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-insert],[data-reset]");
    if (!b) return;
    if (b.hasAttribute("data-reset")) {
      ta.value = b.getAttribute("data-reset");
    } else {
      var s = ta.selectionStart == null ? ta.value.length : ta.selectionStart;
      var en = ta.selectionEnd == null ? s : ta.selectionEnd;
      var ins = b.getAttribute("data-insert");
      ta.value = ta.value.slice(0, s) + ins + ta.value.slice(en);
      ta.selectionStart = ta.selectionEnd = s + ins.length;
    }
    ta.focus();
    render();
  });
})();
