// Atalaya — JS de la interfaz. Fichero externo: la CSP bloquea scripts inline.
(function () {
  // Seleccionar todo / ninguno en las cuadrículas de casillas (Mi cuenta)
  document.querySelectorAll(".bulklink").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var grid = document.getElementById(btn.dataset.target);
      if (!grid) return;
      grid.querySelectorAll('input[type="checkbox"]').forEach(function (cb) {
        cb.checked = btn.dataset.bulk === "on";
      });
    });
  });

  // Copiar el enlace de invitación (admin)
  var copyBtn = document.getElementById("copy-invite");
  if (copyBtn) {
    copyBtn.addEventListener("click", function () {
      var url = document.getElementById("invite-url");
      navigator.clipboard.writeText(url.textContent).then(function () {
        var orig = copyBtn.textContent;
        copyBtn.textContent = copyBtn.dataset.copied;
        setTimeout(function () { copyBtn.textContent = orig; }, 1600);
      });
    });
  }

  // Fuseau deducido del navegador durante el onboarding
  var tzSel = document.getElementById("tzselect");
  if (tzSel && tzSel.dataset.autodetect === "1") {
    try {
      var tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
      for (var i = 0; i < tzSel.options.length; i++) {
        if (tzSel.options[i].value === tz) { tzSel.selectedIndex = i; break; }
      }
    } catch (e) { /* se mantiene el valor por defecto */ }
  }
})();
