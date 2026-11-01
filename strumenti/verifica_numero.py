"""Aggiunge il tuo cellulare tra i Verified Caller ID di Twilio (gratis anche con l'account di prova).

    .venv/bin/python -m strumenti.verifica_numero

Twilio ti chiama sul cellulare: quando te lo chiede, digita sulla tastiera del telefono il codice che compare qui.
Usa TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN e TWILIO_NUMERO dal .env, senza stamparli.
"""
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config  # noqa: E402,F401  (carica il .env)

SID, TOKEN = os.getenv("TWILIO_ACCOUNT_SID", ""), os.getenv("TWILIO_AUTH_TOKEN", "")
NUMERO = os.getenv("TWILIO_NUMERO", "").replace(" ", "")
BASE = f"https://api.twilio.com/2010-04-01/Accounts/{SID}"
AUTH = "Basic " + base64.b64encode(f"{SID}:{TOKEN}".encode()).decode()


def chiama(metodo, percorso, dati=None):
    req = urllib.request.Request(BASE + percorso, data=urllib.parse.urlencode(dati).encode() if dati else None,
                                 method=metodo, headers={"Authorization": AUTH})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def gia_verificato():
    q = urllib.parse.urlencode({"PhoneNumber": NUMERO})
    return bool(chiama("GET", f"/OutgoingCallerIds.json?{q}").get("outgoing_caller_ids"))


def main():
    if not (SID and TOKEN and NUMERO):
        sys.exit("Mancano nel .env TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN o TWILIO_NUMERO.")
    if gia_verificato():
        print(f"{NUMERO} è già un Verified Caller ID. Puoi rilanciare il controllo.")
        return
    try:
        r = chiama("POST", "/OutgoingCallerIds.json", {"PhoneNumber": NUMERO, "FriendlyName": "Filo prova"})
    except urllib.error.HTTPError as e:
        sys.exit(f"Twilio ha rifiutato la richiesta ({e.code}): {e.read().decode()[:300]}")
    print(f"\n   CODICE DA DIGITARE SUL TELEFONO:  {r['validation_code']}\n")
    print("Twilio ti sta chiamando: rispondi e, quando te lo chiede, digita il codice sulla tastiera.")
    for _ in range(36):
        time.sleep(5)
        if gia_verificato():
            print("Verificato. Ora rilancia: .venv/bin/python -m strumenti.controlla")
            return
    print("Non risulta ancora verificato dopo 3 minuti: rilancia questo script per riprovare.")


if __name__ == "__main__":
    main()
