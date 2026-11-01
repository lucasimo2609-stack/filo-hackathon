"""Chiamate al telefono: se la persona riattacca prima dei saluti, nessuno chiude la chiamata in Filo.
Ogni 10 secondi Filo chiede a ElevenLabs lo stato delle conversazioni al telefono ancora aperte e, quando sono finite, le conclude."""
from __future__ import annotations

import threading
import time

from .. import db
from . import chiamata as ch, eleven


def giro():
    with db.tx() as con:
        aperte = [dict(r) for r in con.execute(
            "SELECT id, conversation_id FROM calls WHERE channel='telefono' AND ended_at IS NULL AND conversation_id IS NOT NULL")]
    for c in aperte:
        try:
            stato = eleven._client().conversational_ai.conversations.get(c["conversation_id"]).status
        except Exception:  # noqa: BLE001 conversazione non ancora visibile o rete: si riprova al giro dopo
            continue
        if str(stato).lower() in ("done", "failed"):
            ch.concludi(c["id"], c["conversation_id"])


def avvia(ogni: int = 10):
    if not eleven.telefono_configurato():
        return

    def loop():
        while True:
            try:
                giro()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(ogni)
    threading.Thread(target=loop, daemon=True, name="filo-telefono").start()
