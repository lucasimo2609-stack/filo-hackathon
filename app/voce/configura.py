"""Crea (o aggiorna) l'agente vocale su ElevenLabs.

    python -m app.voce.configura --url https://xxxx.trycloudflare.com

--url è l'indirizzo pubblico del cervello della voce (il tunnel verso la porta 8010).
Legge ELEVENLABS_API_KEY dal .env; alla prima esecuzione stampa ELEVEN_AGENT_ID da aggiungere al .env.
Il codice segreto per il cervello della voce viene salvato tra i segreti di ElevenLabs, mai stampato.
"""
from __future__ import annotations

import argparse
import os
import sys

from ..config import ELEVEN_AGENT_ID, VOCE_MODEL, voce_token
from .eleven import PRIMA_FRASE, PROMPT_ELEVEN, _client

SECRET_NAME = "filo_voce_token"


def _voce_italiana(client, scelta: str | None) -> str:
    scelta = scelta or os.getenv("VOCE_ELEVEN_VOICE_ID")  # la voce scelta resta anche quando si ricollega il tunnel
    if scelta:
        return scelta
    try:
        voices = client.voices.search(page_size=100).voices
        it = [v for v in voices if (v.labels or {}).get("language") in ("it", "italian") or "ital" in (v.name or "").lower()]
        if it:
            print(f"Voce scelta: {it[0].name}")
            return it[0].voice_id
    except Exception:
        pass
    print("Nessuna voce italiana nella tua libreria: uso una voce multilingue di base. "
          "Per cambiarla aggiungi una voce italiana dalla Voice Library e rilancia con --voce <voice_id>.")
    return "21m00Tcm4TlvDq8ikWAM"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="indirizzo pubblico del cervello della voce, es. https://xxxx.trycloudflare.com")
    ap.add_argument("--voce", help="voice_id ElevenLabs (facoltativo)")
    a = ap.parse_args()
    url = a.url.rstrip("/") + "/v1"

    from elevenlabs import types as T
    client = _client()
    secret_id = None
    for s in client.conversational_ai.secrets.list().secrets:
        if s.name == SECRET_NAME:
            secret_id = s.secret_id
    if secret_id is None:
        secret_id = client.conversational_ai.secrets.create(name=SECRET_NAME, value=voce_token()).secret_id

    conversation_config = T.ConversationalConfig(
        agent=T.AgentConfig(
            first_message=PRIMA_FRASE,
            language="it",
            dynamic_variables=T.DynamicVariablesConfig(dynamic_variable_placeholders={"nome": "Mario Rossi", "saluto_nome": "il signor Mario Rossi", "call_id": "CH-000000"}),
            prompt=T.PromptAgentApiModelOutput(
                prompt=PROMPT_ELEVEN, llm="custom-llm", temperature=0.2,
                custom_llm=T.CustomLlm(url=url, model_id=VOCE_MODEL, api_key=T.ConvAiSecretLocator(secret_id=secret_id), api_type="chat_completions"),
                built_in_tools=T.BuiltInToolsOutput(
                    end_call=T.SystemToolConfigOutput(name="end_call", description="Chiude la chiamata dopo i saluti.",
                                                      params=T.SystemToolConfigOutputParams_EndCall(system_tool_type="end_call")),
                    voicemail_detection=T.SystemToolConfigOutput(name="voicemail_detection", description="Segreteria telefonica rilevata.",
                                                                 params=T.SystemToolConfigOutputParams_VoicemailDetection(system_tool_type="voicemail_detection")),
                ),
            ),
        ),
        tts=T.TtsConversationalConfigOutput(model_id="eleven_flash_v2_5", voice_id=_voce_italiana(client, a.voce), speed=0.9, stability=0.6),
        turn=T.TurnConfig(turn_eagerness="patient", silence_end_call_timeout=40),
    )
    platform_settings = T.AgentPlatformSettingsRequestModel(
        overrides=T.ConversationInitiationClientDataConfigInput(custom_llm_extra_body=True),
        privacy=T.PrivacyConfigInput(record_voice=False, retention_days=-1, delete_audio=True, zero_retention_mode=True),
    )
    agents = client.conversational_ai.agents
    try:
        if ELEVEN_AGENT_ID:
            agents.update(agent_id=ELEVEN_AGENT_ID, conversation_config=conversation_config, platform_settings=platform_settings)
            print(f"Agente {ELEVEN_AGENT_ID} aggiornato: cervello della voce su {url}")
        else:
            r = agents.create(name="Filo - agente vocale", conversation_config=conversation_config, platform_settings=platform_settings,
                              tags=["filo", "hackathon"])
            print(f"Agente creato. Aggiungi al file .env:\nELEVEN_AGENT_ID={r.agent_id}")
    except Exception as e:  # la conservazione zero può non essere nel piano: si riprova senza e lo si dice
        if "zero" not in str(e).lower() and "retention" not in str(e).lower():
            raise
        print(f"Risposta di ElevenLabs: {str(e)[-300:]}", file=sys.stderr)
        print("ElevenLabs non permette la conservazione zero con un modello esterno (il nostro cervello su Fastweb). "
              "Imposto il massimo compatibile: audio non registrato, conversazione, trascrizione e dati personali cancellati dopo 1 giorno.",
              file=sys.stderr)
        platform_settings = T.AgentPlatformSettingsRequestModel(
            overrides=T.ConversationInitiationClientDataConfigInput(custom_llm_extra_body=True),
            privacy=T.PrivacyConfigInput(record_voice=False, retention_days=1, delete_audio=True, delete_transcript_and_pii=True))
        if ELEVEN_AGENT_ID:
            agents.update(agent_id=ELEVEN_AGENT_ID, conversation_config=conversation_config, platform_settings=platform_settings)
            print(f"Agente {ELEVEN_AGENT_ID} aggiornato")
        else:
            r = agents.create(name="Filo - agente vocale", conversation_config=conversation_config, platform_settings=platform_settings)
            print(f"Agente creato. Aggiungi al file .env:\nELEVEN_AGENT_ID={r.agent_id}")


if __name__ == "__main__":
    main()
