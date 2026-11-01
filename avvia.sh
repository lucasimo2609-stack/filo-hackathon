#!/usr/bin/env bash
# Primo avvio: crea l'ambiente, i dati sintetici e gli utenti di prova, poi accende Filo.
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi
.venv/bin/python -c "import pypdf, elevenlabs" 2>/dev/null || .venv/bin/pip install -q -r requirements.txt
command -v pdftoppm >/dev/null || echo "Nota: manca pdftoppm (anteprime delle pagine). Su Ubuntu: sudo apt install poppler-utils"
command -v tesseract >/dev/null || echo "Nota: manca tesseract (lettura delle scansioni). Su Ubuntu: sudo apt install tesseract-ocr tesseract-ocr-ita"
mkdir -p data/import
if [ ! -f .env ] && [ -f ../fastweb-kit/.env ]; then
  cp ../fastweb-kit/.env .env && chmod 600 .env
  echo "Chiavi copiate da ../fastweb-kit/.env"
fi
VERSIONE=7  # cambia quando cambia la struttura dei dati (7: modifiche alle prenotazioni)
if [ ! -f data/filo.db ] || [ "$(cat data/.versione 2>/dev/null)" != "$VERSIONE" ]; then
  .venv/bin/python -m app.seed --reset && echo "$VERSIONE" > data/.versione
fi
echo "Filo su http://127.0.0.1:8000  (credenziali in data/credenziali_demo.txt)"
echo "Cervello della voce su http://127.0.0.1:8010 (per ElevenLabs serve un tunnel: vedi README_IT.md, sezione Agente vocale)"
.venv/bin/uvicorn app.voce.server:app --host 127.0.0.1 --port 8010 --log-level warning &
VOCE_PID=$!
GUARDIANO_PID=""
# tunnel pubblico verso la voce, ricreato da solo se Cloudflare lo perde (ElevenLabs e i flussi si aggiornano): strumenti/tunnel_voce.py
if command -v cloudflared >/dev/null && ! pgrep -f "strumenti.tunnel_voce" >/dev/null; then
  .venv/bin/python -m strumenti.tunnel_voce >/dev/null 2>&1 &
  GUARDIANO_PID=$!
  echo "Tunnel della voce: guardiano acceso (indirizzo in data/.tunnel_voce, log in data/tunnel_voce.log)"
fi
trap 'kill $VOCE_PID $GUARDIANO_PID 2>/dev/null' EXIT
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
