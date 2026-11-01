# Filo · AI agents for territorial care coordination

**Team project built in 24 hours at the [FastwebAI Hackathon – Agent Forge h24](https://www.fastweb.it/fastwebai-hackathon/) (Milan, 22–23 October 2026).**

> All data is synthetic. This is a prototype, not a medical device.

Every morning, Filo reads a hospital's clinical notes, reports and appointment schedules and turns them into two separate work queues:

- **Front desk**: transport for patients whose appointment is at risk (public transport strike, living alone, no car), freed-up slots to reassign (one proposal at a time, every call approved by a human) and an accessibility kit for the visit. This view sees only logistics, never clinical documents.
- **Doctor**: overlapping treatments, suspended therapies and duplicated tests. Every action is signed with a time-based one-time code.

## Privacy by design

The core idea is that **no personal data ever leaves the hospital**, while four AI agents run in the cloud on Fastweb's AI Factory.

```
hospital (local)                                     Fastweb cloud
notes -> closed-list extractor -> gateway -> anonymous packets, one-time codes -> 4 agents
         key table  <-  re-identification + local rule checks  <-  answers
```

1. **Local extractor**: free text is reduced to entries from a closed list (drug class, specialty, condition). Names, addresses and phone numbers never pass. Negations ("denies diabetes") are not turned into data, and every extracted fact links back to the exact sentence of the source document.
2. **Gateway**: one packet per agent and per patient, carrying only the fields that agent needs. Transport packets enforce **k-anonymity (k ≥ 10) and l-diversity (l ≥ 3)**: if a packet cannot be generalized enough, it is not sent and a local rule decides.
3. **One-time codes**, new at every run and different per agent, plus deduplication so packets cannot be linked across agents.
4. **Attestation** before sending: blocks any agent running a model outside the approved EU list.
5. **Local validation** of every answer. An invalid answer becomes a case for manual review; no case disappears silently.
6. **Fallback rules** that always apply regardless of the model (e.g. anticoagulant + NSAID always goes to the doctor).

Other features: document import with tax-code matching and local OCR, SHA-256 integrity checks on original documents, an autonomous transport-booking workflow with deadlines and fallbacks, a voice agent (ElevenLabs) that calls patients, and a live-captioning page for hearing-impaired patients during visits.

## Tech stack

Python · FastAPI · Jinja2 · SQLite · OpenAI-compatible LLM APIs (Fastweb AI Factory: Gemma, Qwen) · ElevenLabs · pytest (**259 tests**)

## Team

- **Ambra Danesin**
- **Vincenzo Rienzo**
- **Luca Simonelli**

Original repository: [ambradan/filo-fastweb-hackathon](https://github.com/ambradan/filo-fastweb-hackathon) (private). This is a snapshot of the code at the end of the hackathon, published with the team's agreement.

### My contribution (Luca)

I reworked how the **transport agent** chooses a service. Instead of putting the full list of transport services in the cloud agent's prompt, the hospital side now pre-filters the services that are valid for each anonymous packet (district coverage, working-day notice, eligibility rules such as "frail patients only") and sends only those. The agent must pick from that list or return `null`, so it can no longer suggest a service that breaks the rules, and the packet still adds nothing that identifies the patient. I also hardened the parsing of the agent's answer and added tests for the new behaviour (`tests/test_trasporti_servizi.py`).

## Run it

```bash
cp .env.example .env    # add your API keys
./avvia.sh
```

The first run creates a virtual environment, generates the synthetic data and two demo users, then serves the app at http://127.0.0.1:8000. Tests: `python -m pytest`.

## Documentation

The full documentation is in Italian: [README_IT.md](README_IT.md) (architecture, workflows, voice agent, what is not production-ready) and [VERIFICHE.md](VERIFICHE.md) (checks and known limits).
