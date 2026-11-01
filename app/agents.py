"""Agenti nel cloud Fastweb. Ricevono SOLO pacchetti senza nome, con un codice usa e getta.

Ogni agente ha un interruttore (config.AGENTS): "api" chiama direttamente il modello sulla
AI Factory, "sim" esegue un flusso pubblicato su FastwebAI Agents. Se "sim" fallisce si
ripiega su "api" e lo si dice nel risultato.
"""
from __future__ import annotations

import json
import re
import time
import urllib.request

from openai import OpenAI

from .config import AGENTS, FW_BASE_URL, FW_KEYS, SIM_API_KEY, SIM_BASE_URL

_clients: dict[str, OpenAI] = {}

# Liste d'attesa: una persona ha disdetto, l'agente sceglie a chi proporre il posto. Lo stesso testo sta nel workflow
# "Filo - agente agende" sulla piattaforma; Filo controlla la scelta con le stesse regole (app/posti.py, _ammessi).
POSTO_LIBERATO = """
Puoi ricevere anche un pacchetto di tipo "posto_liberato": una persona ha disdetto e si è liberato un posto per "esame" tra "giorni_al_posto" giorni.
"candidati" sono le persone in lista d'attesa per quell'esame, ognuna con un codice anonimo e questi campi:
- "priorita_ricetta": U (urgente), B (breve, entro 10 giorni), D (differibile), P (programmata), scritta dal medico che ha prescritto l'esame;
- "urgenza_medico": alta, media, bassa o nessuna, la valutazione del medico ospedaliero a fine visita;
- "in_lista_da": meno di 30 giorni, 30-90 giorni, oltre 90 giorni;
- "puo_arrivare": false se è senza auto e nessun servizio di trasporto può portarla quel giorno;
- "data_gia_fissata": se ha già una data per lo stesso esame (entro 7 giorni, entro 30 giorni, oltre 30 giorni, nessuna).
Scegli a chi proporre il posto per primo in base all'urgenza: prima la priorità della ricetta, poi l'urgenza del medico, poi da quanto aspetta.
Un'urgenza del medico "alta" può passare davanti a una priorità di ricetta migliore solo se nessuno con quella priorità ha urgenza alta.
Non scegliere chi non può arrivare. A parità di urgenza preferisci chi non ha già una data entro 7 giorni.
Usa solo i dati del pacchetto. Non fare diagnosi.
Per "posto_liberato" rispondi SOLO con JSON: {"codice_scelto": "<codice di uno dei candidati>", "motivo": "<una frase>"}"""

PROMPTS = {
    "cure": """Sei un agente di farmacovigilanza per una centrale di coordinamento sanitario.
Ricevi un pacchetto ANONIMO: "farmaci" (classi di farmaco e reparti che le hanno prescritte) e, se ci sono, "condizioni" (malattie o loro categorie).
Dì se la combinazione merita una verifica da parte del medico. Non fare diagnosi, non suggerire dosaggi.
Gravità: 1 = bassa, 2 = media, 3 = alta (rischio serio, da vedere subito).
Rispondi SOLO con JSON: {"da_verificare": true|false, "gravita": 1|2|3, "spiegazione": "<una frase in italiano semplice>"}""",
    "aderenza": """Sei un agente che segue l'aderenza alle terapie per una centrale di coordinamento sanitario.
Ricevi un pacchetto ANONIMO: classe di farmaco sospesa ("terapia"), da quanti giorni, motivo, e "visita" (quando è la prossima visita: entro 2 giorni, entro 7 giorni, entro 30 giorni, oltre 30 giorni, nessuna).
Valuta quanto è urgente avvisare il medico. Non fare diagnosi.
Urgenza: 1 = bassa, 2 = media, 3 = alta (avvisare il medico oggi).
Rispondi SOLO con JSON: {"rilevante": true|false, "urgenza": 1|2|3, "spiegazione": "<una frase>"}""",
    "trasporti": """Sei un agente che organizza trasporti sanitari per persone fragili.
Ricevi un pacchetto ANONIMO con fascia d'età, distretto, situazione (vive solo, senza auto), giorni alla visita, se in quell'orario c'è sciopero dei mezzi, se quel giorno è prevista un'ondata di calore e "servizi_possibili".
"servizi_possibili" sono i servizi di trasporto che l'ospedale ha già controllato per questa visita: coprono il distretto, rispettano il preavviso (anche in giorni lavorativi) e le loro regole. Sono dati pubblici.
Dì se serve un trasporto. Se serve, scegli il servizio SOLO tra "servizi_possibili", di norma il primo (l'ospedale li mette in ordine: prima i servizi sociali gratuiti, poi i più rapidi); scegline un altro solo se la situazione lo chiede e dillo nel motivo. Se la lista è vuota o manca, metti "servizio": null: lo organizza l'ospedale.
L'orario della visita non è nel pacchetto: metti "partenza": null, la calcola l'ospedale.
Rispondi SOLO con JSON: {"serve_trasporto": true|false, "servizio": "<id di servizi_possibili o null>", "partenza": null, "motivo": "<una frase>"}""",
    "agende": """Sei un agente che gestisce agende e liste d'attesa sanitarie.
Ricevi un pacchetto ANONIMO di tipo "richieste_doppie": stesso esame chiesto da reparti diversi ("esame", "richiesto_da", "intervallo_giorni").
Il campo "calendario" è letto dal calendario dell'ospedale al momento dell'invio: "prenotazione_esistente" dice se per questa persona l'esame è già prenotato (entro 7 giorni, entro 30 giorni, oltre 30 giorni, già passata, nessuna); "posti_liberi_30_giorni" quanti posti liberi ci sono per quell'esame nel prossimo mese (nessuno, pochi, molti).
Dì se è probabilmente un doppione. Se l'esame è già prenotato, la seconda richiesta è quasi sempre un doppione: dillo nella spiegazione. Usa solo i dati del pacchetto, non inventarne altri. Non fare diagnosi.
Rispondi SOLO con JSON: {"doppione": true|false, "spiegazione": "<una frase>"}
""" + POSTO_LIBERATO,
}


def _client(model: str) -> OpenAI:
    key = FW_KEYS.get(model)
    if not key:
        raise RuntimeError(f"Manca la chiave per {model} nel file .env")
    if model not in _clients:
        _clients[model] = OpenAI(base_url=FW_BASE_URL, api_key=key, max_retries=3, timeout=90)
    return _clients[model]


def extract_json(text: str) -> dict | None:
    if not text:
        return None
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    s = text.find("{")
    if s < 0:
        return None
    cand = text[s:]
    for attempt in (cand, cand.rstrip().rstrip(",") + "}", cand + "}"):
        try:
            return json.loads(attempt)
        except json.JSONDecodeError:
            pass
    e = cand.rfind("}")
    try:
        return json.loads(cand[: e + 1]) if e > 0 else None
    except json.JSONDecodeError:
        return None


def _system(agent: str) -> str:
    return PROMPTS[agent]


def _call_api(agent: str, payload: dict) -> dict:
    model = AGENTS[agent]["model"]
    msgs = [{"role": "system", "content": _system(agent)},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
    for attempt in range(2):
        r = _client(model).chat.completions.create(model=model, messages=msgs, temperature=0, max_tokens=500)
        out = extract_json(r.choices[0].message.content or "")
        if out is not None:
            return out
        msgs.append({"role": "user", "content": "Rispondi solo con il JSON richiesto."})
    raise RuntimeError("risposta non in JSON")


def _sim_post(wf: str, body: dict) -> dict:
    req = urllib.request.Request(f"{SIM_BASE_URL}/api/workflows/{wf}/execute", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "X-API-Key": SIM_API_KEY})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def _call_sim(agent: str, payload: dict) -> dict:
    """Flusso pubblicato su FastwebAI Agents: blocco Inizio con campo "input" -> blocco Agente -> risposta JSON.

    Un solo formato (quello verificato): niente secondo invio dello stesso pacchetto. Una risposta di errore
    della piattaforma ({"success": false, "error": ...}) è un errore, non un risultato: si passa alla riserva."""
    wf = AGENTS[agent].get("sim_workflow")
    if not (wf and SIM_API_KEY):
        raise RuntimeError("flusso o chiave della piattaforma non configurati")
    data = _sim_post(wf, {"input": {"input": json.dumps(payload, ensure_ascii=False)}})
    if not isinstance(data, dict):
        raise RuntimeError("risposta della piattaforma non leggibile")
    if data.get("success") is False or data.get("error"):
        raise RuntimeError(f"la piattaforma risponde con un errore: {str(data.get('error') or 'success=false')[:120]}")
    out = data.get("output", data)
    if isinstance(out, dict) and "content" in out:
        out = out["content"]
    if isinstance(out, str):
        out = extract_json(out)
    if not isinstance(out, dict) or ("success" in out and "error" in out):
        raise RuntimeError("risposta della piattaforma non leggibile")
    return out


def call(agent: str, payload: dict) -> tuple[dict, str, int]:
    """Ritorna (risultato, backend usato, millisecondi)."""
    t = time.perf_counter()
    backend = AGENTS[agent]["backend"]
    if backend == "sim":
        try:
            out = _call_sim(agent, payload)
            return out, "piattaforma FastwebAI Agents", int((time.perf_counter() - t) * 1000)
        except Exception as e:  # riserva: chiamata diretta
            out = _call_api(agent, payload)
            return out, f"API diretta (riserva: {str(e)[:150]})", int((time.perf_counter() - t) * 1000)
    out = _call_api(agent, payload)
    return out, "API diretta AI Factory", int((time.perf_counter() - t) * 1000)
