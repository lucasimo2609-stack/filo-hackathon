// Pagina della chiamata: voce dal browser con ElevenLabs, simulatore a testo, aggiornamento degli eventi.
(function () {
  var root = document.getElementById("call");
  if (!root) return;
  var cid = root.dataset.cid, csrf = root.dataset.csrf;
  var stato = document.getElementById("voce-stato");
  var conv = null, convId = null, chiusuraProgrammata = false;

  function post(url, data) {
    var fd = new FormData(); fd.append("csrf_token", csrf);
    Object.keys(data || {}).forEach(function (k) { fd.append(k, data[k]); });
    return fetch(url, { method: "POST", body: fd, credentials: "same-origin" }).then(function (r) {
      if (!r.ok) return r.json().catch(function () { return {}; }).then(function (j) { throw new Error(j.detail || ("errore " + r.status)); });
      return r.json();
    });
  }
  function esc(s) { var d = document.createElement("div"); d.textContent = s; return d.innerHTML; }

  function aggiorna() {
    fetch("/chiamate/" + cid + "/eventi", { credentials: "same-origin" }).then(function (r) { return r.json(); }).then(function (j) {
      var t = document.getElementById("turni");
      t.innerHTML = j.turns.length ? j.turns.map(function (x) {
        return '<div class="turno ' + x.speaker + '"><span class="meta">' + x.ts.slice(11, 19) + " · " + x.speaker + "</span><br>" + esc(x.text) + "</div>";
      }).join("") : '<p class="meta">Ancora nessuna battuta.</p>';
      t.scrollTop = t.scrollHeight;
      document.getElementById("eventi").innerHTML = j.events.map(function (e) {
        return '<div class="ev ' + (e.kind === "allarme" ? "al" : "") + '"><span class="meta">' + e.ts.slice(11, 19) + " · " + e.kind + "</span><br>" + esc(e.text) + "</div>";
      }).join("");
      document.getElementById("esito").textContent = j.status + (j.ended ? " · chiamata chiusa" : "");
      // l'agente ha salutato: chiudo anche la sessione vocale, lasciando il tempo di finire la frase
      if (j.closing && conv && !chiusuraProgrammata) { chiusuraProgrammata = true; setTimeout(function () { try { conv.endSession(); } catch (e) {} }, 6000); }
    }).catch(function () {});
  }
  aggiorna(); setInterval(aggiorna, 2000);

  var sim = document.getElementById("sim");
  sim.addEventListener("submit", function (e) {
    e.preventDefault();
    var inp = document.getElementById("sim-frase"), frase = inp.value.trim();
    if (!frase) return;
    inp.value = ""; inp.disabled = true;
    post("/chiamate/" + cid + "/testo", { frase: frase }).then(function (j) {
      aggiorna();
      if (j.chiusa) { inp.placeholder = "Chiamata chiusa dall'agente"; return; }
      inp.disabled = false; inp.focus();
    }).catch(function (err) { inp.disabled = false; alertBox(err.message); });
  });
  function alertBox(msg) { stato.textContent = msg; stato.style.color = "var(--warn)"; }

  var bv = document.getElementById("btn-voce"), bs = document.getElementById("btn-stop");
  if (bv) bv.addEventListener("click", function () {
    bv.disabled = true; stato.textContent = "Avvio...";
    navigator.mediaDevices.getUserMedia({ audio: true }).then(function () {
      return post("/chiamate/" + cid + "/avvio");
    }).then(function (cfg) {
      return ElevenLabsClient.Conversation.startSession({
        signedUrl: cfg.signedUrl, connectionType: "websocket",
        dynamicVariables: cfg.dynamicVariables, customLlmExtraBody: cfg.customLlmExtraBody,
        onConnect: function (x) { convId = x && x.conversationId; stato.textContent = "In linea. Parla pure."; bs.hidden = false; },
        onModeChange: function (m) { stato.textContent = m.mode === "speaking" ? "Sta parlando l'agente..." : "Ti ascolta..."; },
        onDisconnect: function () {
          stato.textContent = "Chiamata terminata."; bs.hidden = true;
          post("/chiamate/" + cid + "/fine", { conversation_id: convId || "" }).then(aggiorna);
        },
        onError: function (m) { alertBox("Errore: " + m); }
      });
    }).then(function (c) { conv = c; if (!convId && c.getId) convId = c.getId(); })
      .catch(function (err) {
        bv.disabled = false;
        var mic = err && /NotAllowed|NotFound|NotReadable|Security/.test(err.name || "");
        alertBox(mic ? "Microfono non disponibile in questo browser: la chiamata non è partita. Apri Filo in Chrome o Firefox e consenti il microfono, oppure usa il simulatore a testo qui sotto."
                     : (err.message || String(err)));
      });
  });
  if (bs) bs.addEventListener("click", function () { if (conv) conv.endSession(); });
})();
