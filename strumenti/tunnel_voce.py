"""Tiene acceso il tunnel pubblico verso il cervello della voce (porta 8010) e lo ricrea da solo se Cloudflare lo perde.

    .venv/bin/python -m strumenti.tunnel_voce

Perché serve: i tunnel rapidi di cloudflared (trycloudflare.com) spariscono dopo un'interruzione di rete. Il processo resta
acceso ma ripete "Tunnel not found" all'infinito, e il nome pubblico non esiste più: la voce di ElevenLabs e i flussi
sulla piattaforma non raggiungono più Filo. È successo due volte il 23/10.

Cosa fa, in giro continuo:
1. avvia cloudflared con le metriche su 127.0.0.1:20242 (da lì Filo legge l'indirizzo per i flussi, app/flussi.py);
2. aspetta che https://<nome>/salute risponda da internet;
3. aggiorna l'agente ElevenLabs con il nuovo indirizzo (python -m app.voce.configura --url ...);
4. ogni 20 secondi riprova /salute; dopo 3 errori di fila, o se cloudflared esce, lo chiude e riparte dal punto 1.
Scrive cosa fa in data/tunnel_voce.log e l'indirizzo attuale in data/.tunnel_voce.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
METRICHE = os.getenv("FILO_TUNNEL_METRICS", "127.0.0.1:20242")
PORTA = os.getenv("VOCE_PORT", "8010")
LOG = ROOT / "data" / "tunnel_voce.log"
INDIRIZZO = ROOT / "data" / ".tunnel_voce"
OGNI, ERRORI_MAX = 20, 3


def scrivi(msg: str):
    riga = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(riga, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(riga + "\n")


def nome() -> str | None:
    try:
        with urllib.request.urlopen(f"http://{METRICHE}/quicktunnel", timeout=2) as r:
            return json.load(r).get("hostname") or None
    except Exception:  # noqa: BLE001
        return None


def risponde(host: str) -> bool:
    try:
        with urllib.request.urlopen(f"https://{host}/salute", timeout=10) as r:
            return r.status == 200 and b'"ok":true' in r.read()
    except Exception:  # noqa: BLE001
        return False


def avvia() -> tuple[subprocess.Popen, str] | None:
    p = subprocess.Popen(["cloudflared", "tunnel", "--url", f"http://127.0.0.1:{PORTA}", "--metrics", METRICHE],
                         stdout=subprocess.DEVNULL, stderr=(ROOT / "data" / "cloudflared.log").open("a"))
    for _ in range(60):  # il nome arriva in pochi secondi, la raggiungibilità da internet in qualche secondo in più
        time.sleep(2)
        if p.poll() is not None:
            scrivi(f"cloudflared è uscito subito (codice {p.returncode})")
            return None
        h = nome()
        if h and risponde(h):
            return p, h
    scrivi("il tunnel non è diventato raggiungibile in 2 minuti: si riprova")
    p.terminate()
    return None


def configura(host: str) -> bool:
    r = subprocess.run([sys.executable, "-m", "app.voce.configura", "--url", f"https://{host}"], cwd=ROOT,
                       capture_output=True, text=True, timeout=120)
    ok = r.returncode == 0 and "aggiornato" in r.stdout
    scrivi("agente ElevenLabs aggiornato" if ok else f"aggiornamento di ElevenLabs NON riuscito: {(r.stdout + r.stderr)[-200:]}")
    return ok


def main():
    LOG.parent.mkdir(exist_ok=True)
    scrivi(f"guardiano avviato (porta {PORTA}, metriche {METRICHE})")
    while True:
        avviato = avvia()
        if not avviato:
            time.sleep(10)
            continue
        p, host = avviato
        INDIRIZZO.write_text(host)
        scrivi(f"tunnel attivo: https://{host}")
        configura(host)
        errori = 0
        while p.poll() is None:
            time.sleep(OGNI)
            errori = 0 if risponde(host) else errori + 1
            if errori:
                scrivi(f"/salute non risponde ({errori}/{ERRORI_MAX})")
            if errori >= ERRORI_MAX:
                break
        scrivi("tunnel perso: lo chiudo e ne apro uno nuovo")
        p.terminate()
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()


if __name__ == "__main__":
    main()
