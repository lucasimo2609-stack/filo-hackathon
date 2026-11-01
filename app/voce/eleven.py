"""Collegamento con ElevenLabs (SDK ufficiale). Solo telefono, ascolto e voce: il ragionamento resta su Fastweb."""
from __future__ import annotations

from ..config import ELEVEN_AGENT_ID, ELEVEN_PHONE_NUMBER_ID, ELEVENLABS_API_KEY, VOCE_NUMERO_PROVA

PRIMA_FRASE = ("Buongiorno, sono l'assistente automatico della Centrale Operativa dell'ospedale, "
               "non sono una persona. Parlo con {{saluto_nome}}?")
PROMPT_ELEVEN = ("Chiamata {{call_id}}. Il comportamento dell'agente è deciso dal server della Centrale "
                 "(modello personalizzato): questo testo non contiene dati.")


def configurato() -> bool:
    return bool(ELEVENLABS_API_KEY and ELEVEN_AGENT_ID)


def telefono_configurato() -> bool:
    return configurato() and bool(ELEVEN_PHONE_NUMBER_ID and VOCE_NUMERO_PROVA)


def _client():
    from elevenlabs.client import ElevenLabs
    if not ELEVENLABS_API_KEY:
        raise RuntimeError("Manca ELEVENLABS_API_KEY nel file .env")
    return ElevenLabs(api_key=ELEVENLABS_API_KEY)


def signed_url() -> str:
    """URL firmato per la conversazione dal browser: la chiave API resta sul server."""
    return _client().conversational_ai.conversations.get_signed_url(agent_id=ELEVEN_AGENT_ID).signed_url


def chiama_telefono(dati: dict) -> str:
    """Chiamata vera. Va SOLO al numero di prova: i numeri dei pazienti del prototipo sono inventati."""
    if not telefono_configurato():
        raise RuntimeError("Telefono non configurato (ELEVEN_PHONE_NUMBER_ID e VOCE_NUMERO_PROVA nel .env)")
    from elevenlabs.types import ConversationInitiationClientDataRequestInput
    r = _client().conversational_ai.twilio.outbound_call(
        agent_id=ELEVEN_AGENT_ID, agent_phone_number_id=ELEVEN_PHONE_NUMBER_ID, to_number=VOCE_NUMERO_PROVA,
        conversation_initiation_client_data=ConversationInitiationClientDataRequestInput(**dati))
    return getattr(r, "conversation_id", None) or getattr(r, "callSid", None) or ""
