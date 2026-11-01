"""Collega il numero Twilio a ElevenLabs e lo scrive nel .env.

    python -m app.voce.telefono

Legge dal .env: ELEVENLABS_API_KEY, ELEVEN_AGENT_ID, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
TWILIO_NUMERO (il numero da cui chiama: numero Twilio o, per sole chiamate in uscita, il tuo cellulare verificato in Twilio).
Le credenziali Twilio vanno solo a ElevenLabs e non vengono mai stampate.
"""
from __future__ import annotations

import os
import re

from ..config import ELEVEN_AGENT_ID, ROOT
from .eleven import _client


def _scrivi_env(chiave: str, valore: str):
    env = ROOT / ".env"
    testo = env.read_text(encoding="utf-8")
    riga = f"{chiave}={valore}"
    testo = re.sub(rf"(?m)^{chiave}=.*$", riga, testo) if re.search(rf"(?m)^{chiave}=", testo) else testo.rstrip("\n") + "\n" + riga + "\n"
    env.write_text(testo, encoding="utf-8")


def main():
    sid, token, numero = (os.getenv(k, "").strip() for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_NUMERO"))
    if not (sid and token and numero):
        raise SystemExit("Mancano nel .env TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN o TWILIO_NUMERO.")
    numero = numero.replace(" ", "")
    pn = _client().conversational_ai.phone_numbers
    esistente = next((p for p in pn.list() if getattr(p, "phone_number", "").replace(" ", "") == numero), None)
    if esistente:
        pid = esistente.phone_number_id
        print(f"Numero già collegato a ElevenLabs ({pid}).")
    else:
        from elevenlabs.conversational_ai.phone_numbers.types.phone_numbers_create_request_body import PhoneNumbersCreateRequestBody_Twilio
        r = pn.create(request=PhoneNumbersCreateRequestBody_Twilio(phone_number=numero, label="Filo prova", sid=sid, token=token))
        pid = r.phone_number_id
        print(f"Numero collegato a ElevenLabs ({pid}).")
    if ELEVEN_AGENT_ID:
        try:
            pn.update(pid, agent_id=ELEVEN_AGENT_ID)
        except Exception as e:  # noqa: BLE001
            print(f"Agente non assegnato al numero ({str(e)[:120]}): per le chiamate in uscita non serve.")
    _scrivi_env("ELEVEN_PHONE_NUMBER_ID", pid)
    print("ELEVEN_PHONE_NUMBER_ID scritto nel .env. Riavvia Filo.")


if __name__ == "__main__":
    main()
