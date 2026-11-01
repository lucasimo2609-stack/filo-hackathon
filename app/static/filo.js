// Trascina e rilascia: i file lasciati sulla zona vanno nel campo del modulo e il modulo parte.
document.querySelectorAll(".dropzone").forEach(function (z) {
  var input = z.querySelector("input[type=file]");
  var form = z.closest("form");
  ["dragenter", "dragover"].forEach(function (ev) {
    z.addEventListener(ev, function (e) { e.preventDefault(); z.classList.add("over"); });
  });
  ["dragleave", "drop"].forEach(function (ev) {
    z.addEventListener(ev, function (e) { e.preventDefault(); z.classList.remove("over"); });
  });
  z.addEventListener("drop", function (e) {
    if (!e.dataTransfer.files.length) return;
    input.files = e.dataTransfer.files;
    z.querySelector(".dz-msg").textContent = "Caricamento di " + input.files.length + " file...";
    form.submit();
  });
  input.addEventListener("change", function () {
    if (input.files.length) { z.querySelector(".dz-msg").textContent = "Caricamento di " + input.files.length + " file..."; form.submit(); }
  });
});

// conferma prima delle azioni che cancellano (es. Ripristina demo)
document.querySelectorAll("form[data-conferma]").forEach(f => f.addEventListener("submit", e => {
  if (!window.confirm(f.dataset.conferma)) e.preventDefault();
  else f.querySelectorAll("button").forEach(b => { b.disabled = true; b.textContent = f.dataset.attesa || "Ripristino..."; });
}));

// un modulo parte una volta sola: un secondo clic non rimanda la richiesta (es. due elaborazioni di fila)
document.addEventListener("submit", e => {
  const f = e.target;
  if (e.defaultPrevented || f.method.toLowerCase() !== "post") return;
  if (f.dataset.inviato) { e.preventDefault(); return; }
  f.dataset.inviato = "1";
  if (!e.submitter) return;
  e.submitter.setAttribute("aria-busy", "true");
  if ((f.getAttribute("action") || "").endsWith("/elabora")) e.submitter.textContent = "Gli agenti stanno lavorando…";
});
// tornando indietro con il browser i moduli ripartono
window.addEventListener("pageshow", e => {
  if (!e.persisted) return;
  document.querySelectorAll("form[data-inviato]").forEach(f => { delete f.dataset.inviato; });
  document.querySelectorAll("[aria-busy=true]").forEach(b => b.removeAttribute("aria-busy"));
});

// accesso: la riga in cima al modulo dice a chi è riservato il ruolo scelto e i colori diventano quelli del ruolo
// (body.medico porta il blu dell'ambulatorio); al passo 2 il ruolo arriva dal passo 1
(function () {
  if (!document.body.classList.contains("out")) return;
  var nota = document.getElementById("nota-ruolo");
  var radio = document.querySelectorAll("input[name=role][data-nota]");
  var colora = ruolo => document.body.classList.toggle("medico", ruolo === "medico");
  var ricorda = ruolo => { try { sessionStorage.setItem("filo-ruolo", ruolo); } catch (e) { /* senza memoria resta verde */ } };
  if (!radio.length) {
    try { colora(sessionStorage.getItem("filo-ruolo")); } catch (e) { /* idem */ }
    return;
  }
  radio.forEach(r => {
    if (r.checked) { colora(r.value); ricorda(r.value); }
    r.addEventListener("change", () => {
      if (!r.checked) return;
      if (nota) nota.textContent = r.dataset.nota;
      colora(r.value);
      ricorda(r.value);
    });
  });
})();

// pagine che seguono un flusso in corso: si aggiornano da sole
(function () {
  var a = document.querySelector("[data-aggiorna]");
  if (a) setTimeout(function () { location.reload(); }, (+a.dataset.aggiorna || 10) * 1000);
})();

// l'agente ha preparato una chiamata da solo: "squilla" nell'accettazione
(function () {
  if (!document.body.classList.contains("segreteria") || location.pathname.indexOf("/chiamate/CH-") === 0) return;
  var box = null;
  function guarda() {
    fetch("/chiamate/in-arrivo", { credentials: "same-origin" }).then(function (r) { return r.ok ? r.json() : { chiamate: [] }; })
      .then(function (d) {
        var c = d.chiamate[0];
        if (!c) { if (box) { box.remove(); box = null; } return; }
        if (!box) { box = document.createElement("a"); box.className = "squillo"; document.body.appendChild(box); }
        box.href = "/chiamate/" + c.id;
        box.textContent = "L'agente sta chiamando " + c.patient + " - rispondi tu come " + c.patient.split(" ")[0];
      }).catch(function () {});
  }
  guarda(); setInterval(guarda, 5000);
})();

// un servizio risponde, un posto si libera o viene preso: chi lavora lo sa subito, su qualunque pagina sia.
// Le pagine interessate (indicate dal server in `ricarica`) si aggiornano da sole dopo l'avviso.
(function () {
  if (!document.body.classList.contains("segreteria") && !document.body.classList.contains("medico")) return;
  var dopo = document.body.dataset.ora || "", visti = {}, zona = null;
  function chiave(n) { return n.ts + n.step + n.patient + n.text; }
  function avvisa(n) {
    if (!zona) { zona = document.createElement("div"); zona.className = "avvisi"; zona.setAttribute("aria-live", "polite"); document.body.appendChild(zona); }
    var a = document.createElement("a");
    a.className = "avviso"; a.href = n.link;
    a.innerHTML = "<b></b><span></span><small></small>";
    a.querySelector("b").textContent = n.titolo + " · " + n.patient;
    a.querySelector("span").textContent = n.text;
    a.querySelector("small").textContent = n.ts.slice(11, 16) + " · apri";
    zona.appendChild(a);
    setTimeout(function () { a.classList.add("via"); setTimeout(function () { a.remove(); }, 400); }, 12000);
  }
  function guarda() {
    fetch("/novita?dopo=" + encodeURIComponent(dopo), { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) return;
        var ricarica = false, nuove = [];
        d.novita.forEach(function (n) {
          if (visti[chiave(n)]) return;
          visti[chiave(n)] = 1; nuove.push(n); avvisa(n);
          if ((n.ricarica || []).indexOf(location.pathname) >= 0) ricarica = true;
        });
        dopo = d.ora;
        if (ricarica && !document.querySelector("form :focus")) {  // non si ricarica mentre qualcuno sta scrivendo
          try { sessionStorage.setItem("filo-avvisi", JSON.stringify(nuove)); } catch (e) {}
          setTimeout(function () { location.reload(); }, 1800);
        }
      }).catch(function () {});
  }
  // dopo la ricarica l'avviso resta a vista ancora qualche secondo
  try {
    var prima = JSON.parse(sessionStorage.getItem("filo-avvisi") || "[]");
    sessionStorage.removeItem("filo-avvisi");
    prima.forEach(function (n) { visti[chiave(n)] = 1; avvisa(n); });
  } catch (e) {}
  setInterval(guarda, 4000);
})();

// Dashboard: filtro della lista delle persone mentre si scrive il nome
document.addEventListener("input", function (e) {
  var q = e.target.closest && e.target.closest("[data-filtra]");
  if (!q) return;
  var box = document.querySelector(q.dataset.filtra), t = q.value.trim().toLowerCase(), n = 0;
  box.querySelectorAll("[data-nome]").forEach(function (r) { var ok = !t || r.dataset.nome.indexOf(t) >= 0; r.hidden = !ok; n += ok; });
  var v = box.querySelector(".w-vuoto"); if (v) v.hidden = n > 0;
});

// Dentro la stessa pagina: la persona e le sue sezioni si aprono senza ricaricare (menu laterale e contenuto si aggiornano insieme)
(function () {
  function apri(url, storia) {
    return fetch(url, { credentials: "same-origin" }).then(function (r) {
      if (!r.ok || r.redirected) { location.href = url; return; }
      return r.text().then(function (h) {
        var d = new DOMParser().parseFromString(h, "text/html");
        var main = d.querySelector("main"), side = d.querySelector("nav.side");
        if (!main || !side) { location.href = url; return; }
        document.querySelector("main").replaceWith(main);
        document.querySelector("nav.side").replaceWith(side);
        document.title = d.title;
        if (storia) history.pushState({ dentro: 1 }, "", url);
        window.scrollTo(0, 0);
      });
    }).catch(function () { location.href = url; });
  }
  document.addEventListener("click", function (e) {
    var a = e.target.closest && e.target.closest("a[data-dentro]");
    if (!a || e.metaKey || e.ctrlKey || e.shiftKey || e.button) return;
    e.preventDefault();
    apri(a.getAttribute("href"), true);
  });
  window.addEventListener("popstate", function () { apri(location.pathname + location.search, false); });
})();
