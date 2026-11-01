// Visita col kit: microfono del medico -> filtro -> cuffie della persona, e frasi a trascrivere.
// Lo stesso file fa anche lo schermo del tablet (solo lettura): mostra le frasi man mano che arrivano.
(function () {
  var root = document.getElementById("kitvisita");
  if (!root) return;
  var box = document.getElementById("kit-testo");

  // ---- dimensione del testo, ricordata su questo dispositivo
  var dim = 1.6;
  try { dim = parseFloat(localStorage.getItem("kit-dim")) || dim; } catch (e) {}
  function applicaDim() { box.style.fontSize = dim + "rem"; }
  applicaDim();
  root.querySelectorAll("[data-font]").forEach(function (b) {
    b.addEventListener("click", function () {
      dim = Math.min(4, Math.max(1, dim + 0.25 * +b.dataset.font));
      applicaDim();
      try { localStorage.setItem("kit-dim", dim); } catch (e) {}
    });
  });

  // ---- le frasi: si chiedono al server, così medico e tablet vedono lo stesso testo nello stesso ordine.
  // Ogni volta tutte: una frase lenta a trascrivere può arrivare dopo una successiva e non deve perdersi.
  function mostra(segs) {
    if (!segs.length) return;
    var vuoto = box.querySelector(".vuoto");
    if (vuoto) vuoto.remove();
    segs.forEach(function (s) {
      if (box.querySelector('p[data-seq="' + s.seq + '"]')) return;
      var p = document.createElement("p");
      p.dataset.seq = s.seq;
      p.textContent = s.text;
      var dopo = Array.prototype.find.call(box.querySelectorAll("p[data-seq]"), function (x) { return +x.dataset.seq > s.seq; });
      box.insertBefore(p, dopo || null);
    });
    box.scrollTop = box.scrollHeight;
  }
  function scaduta() {
    var st = document.getElementById("kit-stato");
    if (st) st.textContent = "Sessione scaduta: ricarica la pagina e rientra. La visita resta aperta.";
  }
  function aggiorna() {
    fetch(root.dataset.testo, { credentials: "same-origin" })
      .then(function (r) {
        if (r.status === 404 && root.dataset.schermo) { box.innerHTML = '<p class="meta">Visita conclusa.</p>'; throw "fine"; }
        if (r.redirected) { scaduta(); throw "fine"; }
        return r.json();
      })
      .then(function (d) { mostra(d.segmenti || []); setTimeout(aggiorna, 1000); })
      .catch(function (e) { if (e !== "fine") setTimeout(aggiorna, 3000); });
  }
  if (root.dataset.schermo || root.dataset.stato === "in_corso") aggiorna();
  if (root.dataset.schermo || root.dataset.stato !== "in_corso") return;

  // ---- microfono, filtro, cuffie
  var btnAvvia = document.getElementById("kit-avvia"), btnPausa = document.getElementById("kit-pausa");
  var vol = document.getElementById("kit-volume"), cuffie = document.getElementById("kit-cuffie");
  var stato = document.getElementById("kit-stato"), livello = document.getElementById("kit-livello");
  var ctx, stream, guadagno, analisi, registratore, attivo = false, inviati = 0;
  var tipo = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg"].find(function (t) {
    return window.MediaRecorder && MediaRecorder.isTypeSupported(t);
  }) || "";
  var seq = Math.floor(Date.now() / 1000) * 100;  // crescente anche se la pagina si ricarica a metà visita

  function dici(t) { stato.textContent = t; }

  function invia(blob, n) {
    var fd = new FormData();
    fd.append("csrf_token", root.dataset.csrf);
    fd.append("seq", n);
    fd.append("audio", blob, "frase");
    inviati++;
    fetch(root.dataset.audio, { method: "POST", body: fd, credentials: "same-origin" })
      .then(function (r) {
        if (r.redirected) { inviati--; scaduta(); throw "fine"; }
        return r.json().then(function (d) { return { ok: r.ok, d: d }; });
      })
      .then(function (x) {
        inviati--;
        if (!x.ok) { dici(x.d.errore || x.d.detail || "Trascrizione non riuscita."); return; }
        if (x.d.testo) mostra([{ seq: x.d.seq, text: x.d.testo }]);
        if (attivo) dici("In ascolto.");
      })
      .catch(function (e) { if (e === "fine") return; inviati--; dici("Rete assente: la frase non è stata trascritta."); });
  }

  // Un registratore per frase: si chiude dopo una pausa di silenzio (o dopo 12 secondi) e ne riparte subito un altro,
  // così ogni pezzo è un file audio completo e le parole non si tagliano a metà.
  var parlato = false, silenzioDa = 0, inizio = 0, fondo = 0.01;
  function nuovoRegistratore(dest) {
    var pezzi = [], n = ++seq;
    var r = new MediaRecorder(dest.stream, tipo ? { mimeType: tipo } : undefined);
    r.ondataavailable = function (e) { if (e.data.size) pezzi.push(e.data); };
    r.onstop = function () { if (r._conParola && pezzi.length) invia(new Blob(pezzi, { type: r.mimeType || tipo }), n); };
    r._conParola = false;
    r.start();
    inizio = performance.now();
    return r;
  }

  function ciclo(dest) {
    var buf = new Float32Array(analisi.fftSize);
    function passo() {
      if (!attivo) return;
      analisi.getFloatTimeDomainData(buf);
      var s = 0;
      for (var i = 0; i < buf.length; i++) s += buf[i] * buf[i];
      var rms = Math.sqrt(s / buf.length);
      if (!parlato) fondo = fondo * 0.97 + rms * 0.03;  // rumore di fondo, aggiornato solo nelle pause
      var voce = rms > Math.max(0.012, fondo * 2.5);
      livello.style.width = Math.min(100, rms * 600) + "%";
      var ora = performance.now();
      if (voce) { parlato = true; silenzioDa = 0; registratore._conParola = true; }
      else if (parlato && !silenzioDa) silenzioDa = ora;
      var finita = parlato && silenzioDa && ora - silenzioDa > 700;
      if (finita || ora - inizio > 12000) {
        var vecchio = registratore;
        registratore = nuovoRegistratore(dest);
        vecchio.stop();
        parlato = false; silenzioDa = 0;
      }
      setTimeout(passo, 50);
    }
    passo();
  }

  function avvia() {
    if (!navigator.mediaDevices || !window.MediaRecorder) { dici("Questo browser non può usare il microfono (serve https o localhost)."); return; }
    dici("Chiedo il microfono…");
    navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } })
      .then(function (s) {
        stream = s;
        ctx = new (window.AudioContext || window.webkitAudioContext)();
        var src = ctx.createMediaStreamSource(s);
        var passaAlto = ctx.createBiquadFilter(); passaAlto.type = "highpass"; passaAlto.frequency.value = 120;   // ronzii e rumori bassi
        var passaBasso = ctx.createBiquadFilter(); passaBasso.type = "lowpass"; passaBasso.frequency.value = 7000; // fruscio
        var comp = ctx.createDynamicsCompressor();  // voce più uniforme: le parole piano si sentono, i picchi non fanno male
        comp.threshold.value = -40; comp.knee.value = 20; comp.ratio.value = 6; comp.attack.value = 0.005; comp.release.value = 0.2;
        guadagno = ctx.createGain(); guadagno.gain.value = cuffie.checked ? +vol.value : 0;
        var limite = ctx.createDynamicsCompressor(); limite.threshold.value = -3; limite.ratio.value = 20;  // tetto al volume in cuffia
        src.connect(passaAlto); passaAlto.connect(passaBasso); passaBasso.connect(comp);
        comp.connect(guadagno); guadagno.connect(limite); limite.connect(ctx.destination);
        analisi = ctx.createAnalyser(); analisi.fftSize = 1024; comp.connect(analisi);
        var dest = ctx.createMediaStreamDestination(); comp.connect(dest);  // alla trascrizione va la voce già pulita
        attivo = true;
        registratore = nuovoRegistratore(dest);
        ciclo(dest);
        btnAvvia.hidden = true; btnPausa.hidden = false;
        dici(tipo ? "In ascolto." : "In ascolto (formato audio predefinito del browser).");
      })
      .catch(function (e) { dici("Microfono non disponibile: " + (e && e.message || e)); });
  }

  function pausa() {
    attivo = false;
    try { registratore && registratore.state !== "inactive" && registratore.stop(); } catch (e) {}
    stream && stream.getTracks().forEach(function (t) { t.stop(); });
    ctx && ctx.close();
    livello.style.width = "0";
    btnAvvia.hidden = false; btnAvvia.textContent = "Riprendi"; btnPausa.hidden = true;
    dici(inviati ? "In pausa: sto finendo di trascrivere l'ultima frase." : "In pausa.");
  }

  function regola() { if (guadagno) guadagno.gain.value = cuffie.checked ? +vol.value : 0; }
  vol.addEventListener("input", regola);
  cuffie.addEventListener("change", regola);
  btnAvvia.addEventListener("click", avvia);
  btnPausa.addEventListener("click", pausa);
  window.addEventListener("beforeunload", function () { if (attivo) pausa(); });
})();
