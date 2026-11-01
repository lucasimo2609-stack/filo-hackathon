# Verifiche - lacune del 22/10 (documento di Luca, 65 punti, `test_lacune.py`)

Base: commit `a567548`, cioè lo zip del team delle 22:46 (con agente vocale e mail CUP) estratto in `~/Scaricati/filo`.
Lavoro fatto nel worktree `~/filo-lacune` (ramo locale `fix/lacune-lavoro`), pubblicato su GitHub come `fix/lacune`.

## Misure

| Prova | Risultato |
|---|---|
| `pytest -q` sul ramo | 103 verdi: i 43 del team, i 53 di `tests/test_lacune_chiuse.py` e i 7 di `tests/test_trasporti_servizi.py` (23/10) |
| `test_lacune.py` di Luca copiato in `tests/` | 20 rossi su 23, ognuno per il motivo giusto (ho letto i messaggi: nessun crash). Restano verdi 02, 15 e 21, spiegati sotto |
| `test_lacune_chiuse.py` sulla base `a567548` (controprova) | 51 rossi, 2 verdi: il #4 era già chiuso dal team; il #48 era cieco ed è stato riscritto (ora è rosso sulla base) |
| Elaborazione dal vivo (porta 8001, `.env` del team, agenti sulla piattaforma FastwebAI) | 10 pacchetti, tutti passati dalla piattaforma, 0 errori, 0 risposte scartate, 11 casi. I flussi pubblicati, che hanno ancora i prompt vecchi, leggono i campi nuovi (`condizioni`, `visita` a fasce, `caldo_quel_giorno`) |
| Seconda elaborazione dal vivo, stessi dati | 1 pacchetto mandato (posto liberato, con codici nuovi), 9 risposte riusate, 11 casi aggiornati con lo stesso identificativo, 0 nuovi, 0 persi |
| Pagina "Ospedale e cloud" vista dall'accettazione, prima elaborazione | solo righe "trasporti" nel controllo di anonimato, "amiloidosi" assente, pacchetti cure e aderenza assenti, scritto "5 pacchetti clinici non sono mostrati". Taratura: il testo "tipo": "trasporto" la ricerca lo trova |

## Agente trasporti, 23/10: servizi nel pacchetto

Le esecuzioni del 22/10 sulla piattaforma mostravano due cose che l'agente non poteva fare: contare i giorni lavorativi per STACCO (riceveva solo `giorni_alla_visita`) e proporre una partenza (l'orario della visita non è nel pacchetto: `partenza` era sempre vuota). In più l'elenco dei servizi stava anche dentro il prompt della piattaforma.

- Il pacchetto porta `servizi_possibili`: i servizi già controllati in ospedale per distretto, preavviso (lavorativo per chi prenota lun-ven) e regole, con id, nome, regole e costo. Sono dati pubblici e il filtro usa solo campi già nel pacchetto (se il distretto è tolto per l'anonimato valgono solo i servizi di tutto il territorio), quindi non aggiunge nulla di riconoscibile.
- Il prompt non contiene più l'elenco: l'agente sceglie solo da `servizi_possibili`, mette `servizio` null se la lista è vuota e `partenza` sempre null (la calcola l'ospedale, un'ora prima).
- Il controllo in ospedale resta: un servizio fuori regola viene sostituito con il primo valido, "null" scritto come testo vale nessuno.
- Flusso "Filo - agente trasporti" aggiornato con il nuovo prompt e ripubblicato (vedi sotto, versione 3).

## Agente agende, 23/10: posti liberati in ospedale, una chiamata per ogni approvazione

Il compito A (richieste doppie) non cambia. Il compito B esce dal cloud e diventa una procedura locale (`app/posti.py`, pagina `/posti`):

- ordine fisso: classe di priorità della ricetta U, B, D, P (letta dalla lista d'attesa, mai modificata), poi cognome, poi nome. Con i dati di prova: De Marchi (B), Furlan (B, in lista da prima ma dopo per cognome), Boscolo (D);
- ogni chiamata nasce dall'approvazione di un caso "Posto liberato" dell'accettazione; `/chiamate/nuova` non prepara più chiamate per i posti;
- tabelle nuove `slot_offers` (una riga per tentativo) e `slot_events` (ogni passo); due indici unici parziali: un posto e una persona hanno al massimo una proposta aperta;
- accettazione: aggiornamenti con controllo del numero di righe cambiate, tutto in una transazione; poi rilettura (`posti.verifica`) e solo dopo la mail CUP. Errore o verifica non superata: niente mail, posto tenuto, caso `posto_errore`;
- prompt agende in `app/agents.py` ridotto alle sole richieste doppie.

| Prova | Risultato |
|---|---|
| `pytest -q` | 135 verdi: i 108 di prima e 27 in `tests/test_posti.py` |
| Controprova sui test nuovi (codice rotto apposta, poi ripristinato) | ordine per attesa invece che per cognome: 14 rossi; mail senza rilettura del database: 1 rosso; senza indici unici: 1 rosso |
| Pagine con un caso di errore aperto (cruscotto, caso, Chiamate, Posti liberati, chiamata) | tutte 200; "Riprova a salvare" presente. Viste dal server, non a occhio |

Casi provati: parità di priorità (cognome, poi nome), priorità prima dell'alfabeto, flag invariati, nessun pacchetto al cloud, nessuna chiamata senza approvazione, rifiuto e proposta successiva da riapprovare, accettazione con mail solo dopo la verifica, doppia fine chiamata, data precedente annullata e posto liberato, passaggio con pratica di trasporto, tutti rifiutano (lista esaurita, una riga sola), lista vuota, mancata risposta (richiamo solo con nuova approvazione, al secondo silenzio si passa avanti), identità non confermata, proposta scartata, esito a mano, errore di salvataggio a metà (niente resta salvato), verifica non superata e posto liberato a mano, mail guasta, lista non leggibile, telefono che non parte, due posti a due persone, doppia approvazione, posto passato.

Test del team modificati: `test_attaccante.py::test_codici_usa_e_getta_non_si_collegano` e `test_lacune_chiuse.py::test_08_11_...` si basavano sul pacchetto del posto liberato, l'unico che ripartiva a ogni elaborazione. Ora con gli stessi dati la seconda elaborazione non manda niente: il primo test simula il giorno dopo (risposte di ieri cancellate), il secondo controlla che la seconda elaborazione non mandi pacchetti.

Da fare fuori dal codice: il flusso "Filo - agente agende" sulla piattaforma ha ancora la parte 2 del prompt. Non fa danni (nessun pacchetto di quel tipo parte più), ma va ripubblicato con il testo di `app/agents.py` per tenerli allineati.

## Calendario, 23/10: lo strumento dell'agente Agende

`app/calendario.py`, tabelle `cal_slots` e `cal_log`, pagina `/calendario`. In ospedale, non sulla piattaforma: le prenotazioni hanno nomi e date, e la scheda vuole che queste parti non vadano al cloud. La knowledge base della piattaforma (cure_interazioni_validate, Filo - kit audio) e le Tables non hanno prenotazioni: le agende sono costruite sui dati sintetici del repository.

| Prova | Risultato |
|---|---|
| `pytest -q` | 160 verdi: i 141 di prima e 19 in `tests/test_calendario.py` |
| Popolamento | circa 2.500 posti in 45 giorni, tutti `test=1` e con la fonte; le 6 visite del dataset nei loro posti; 0 incoerenze; un secondo popolamento non aggiunge niente |
| Controprova (codice rotto apposta, poi ripristinato) | senza controllo di versione: 1 rosso; senza controllo doppioni: 1 rosso; senza rilettura dopo la modifica: 1 rosso. Togliere la rilettura del calendario nell'approvazione non basta a far passare un posto occupato (lo ferma anche `prenota`): con tutte e due le barriere tolte, 1 rosso |

Test del team modificati: `test_lacune_chiuse.py::test_24` e `::test_26`, `test_posti.py::test_niente_doppia_...` e `::test_due_posti_...` creavano posti liberati e prenotazioni direttamente nelle tabelle, senza il posto nel calendario. Ora usano `conftest.libera_posto` (come una disdetta vera) e `calendario.prenota`.

Piattaforma, 23/10 alle 2:57: pubblicata la v4 di "Filo - agente agende", con il prompt di `app/agents.py` che spiega il campo `calendario` (riassunto a fasce: `prenotazione_esistente`, `posti_liberi_30_giorni`). Start ha sempre il solo campo `input` e il messaggio User resta `<start.input>`: il riassunto viaggia dentro il pacchetto. Prova sulla piattaforma (23/10, 3:02, "Sandbox 1" del flusso, pacchetto come testo nel campo `input`, cioè come lo manda Filo):

| Pacchetto | Risposta della v4 | Letta da Filo (`extract_json` + `check_output`) |
|---|---|---|
| TAC torace, 2 reparti, 19 giorni, `prenotazione_esistente: entro 7 giorni`, posti pochi | `doppione: true`, "L'esame è già prenotato entro 7 giorni, quindi la seconda richiesta è probabilmente un doppione." (359 ms) | accettata |
| TAC torace, 2 reparti, 120 giorni, `prenotazione_esistente: nessuna`, posti molti | `doppione: false`, "Non è presente una prenotazione esistente per l'esame richiesto." (243 ms) | accettata |

Le risposte arrivano dentro un blocco ```json: `extract_json` lo toglie. Resta da fare la stessa prova con la chiamata vera da Filo (`agents._call_sim`, serve il `.env`).

Piattaforma: la bozza lasciata dal Copilot nel flusso agende (Start con 5 campi al posto di `input`) è stata scartata con "Load deployment" della v3. Live e bozza ora coincidono. Con quella bozza pubblicata Filo non avrebbe più passato il pacchetto, perché lo manda sempre come testo nel campo `input`.

## Modifiche alle prenotazioni, 23/10

`app/modifiche.py`, tabelle `cal_changes` e `cal_change_events`. Lo strumento Calendario non applica più da solo `sposta`, `cancella` e `blocca` su una prenotazione di una persona: apre la modifica (posto tenuto, mail all'ambulatorio, caso da approvare), e cambia solo con la conferma del paziente al telefono. Con `richiesta_dal_paziente` si applica subito, con avviso all'ambulatorio.

| Prova | Risultato |
|---|---|
| `pytest -q` | 179 verdi: i 162 di prima e 17 in `tests/test_modifiche.py` |
| Controprova | modifica applicata senza paziente: 15 rossi; niente mail all'ambulatorio alla proposta: 3 rossi; mail senza rilettura del calendario: 1 rosso |

Test del team modificati: in `test_calendario.py` le cancellazioni e gli spostamenti diretti ora passano `richiesta_dal_paziente=True` (sono disdette o spostamenti chiesti dal paziente); in `test_posti.py` l'accettazione di un posto manda due mail (paziente e ambulatorio) e i test contano le due separatamente.

## I tre test di Luca che restano verdi

- **02** ("false" tra virgolette diventa vero): ora "false" è letto come falso. Il test resta verde perché P01 prende anticoagulante + antinfiammatorio e la regola locale fissa lo manda comunque al medico. Prova: `test_04_false_tra_virgolette_e_falso_tranne_la_combinazione_pericolosa` (solo P01, con la scritta "Regola locale fissa").
- **15** ricopia nel test la logica della pagina invece di aprire la pagina: resta verde qualunque cosa faccia il codice. Prova vera: `test_06_44_pagina_cloud_dell_accettazione_senza_parte_clinica`, con login.
- **21** fa da solo l'`UPDATE` che faceva la rotta: idem. Prova vera: `test_13_non_si_scarta_un_caso_gia_eseguito`, dalla rotta.

## Test del team modificati (e perché)

- `test_cancello.py::test_solo_campi_ammessi`: elenco dei campi aggiornato. Esce `condizioni` (tutte, non solo la prima) al posto di `condizione`, `visita` (a fasce) al posto dei giorni esatti nell'aderenza, `caldo_quel_giorno` nei trasporti.
- `test_web.py::test_medico_firma_con_codice`: controllava `?error=` e `?flash=` nell'indirizzo, che era proprio la lacuna 49. Ora controlla il messaggio sulla pagina.

## Quattro classi

1. **Stato positivo**: le misure sopra, dall'interfaccia vera, con gli agenti veri.
2. **Stato negativo**: le lacune non si raggiungono più, e lo provano i test di Luca rossi e i test rovesciati. Il messaggio nell'indirizzo, i caratteri da Google e la cornice SAMEORIGIN non compaiono più nelle intestazioni (`test_49`, `test_50`).
3. **Esclusività**: c'era una seconda scelta del servizio di trasporto nell'agente vocale (`voce/chiamata.py`), con le regole vecchie. Ora usa `trasporti.services_for` come il cancello. Il prompt di ogni agente vive in due posti, `app/agents.py` e il flusso sulla piattaforma: il 22/10 alle 23:27 i quattro flussi sono stati aggiornati con lo stesso testo e ripubblicati, e ognuno ha risposto correttamente a un pacchetto di prova chiamato con `agents._call_sim`. A ogni modifica futura vanno toccati tutti e due. Dal 23/10 l'elenco dei servizi di trasporto non è più nel prompt: arriva nel pacchetto.
4. **Contratti preservati**: i 43 test del team verdi, agente vocale compreso. I casi nati dalle chiamate (`run_id='voce'`) non vengono mai marcati "superati".

## Limiti: cosa non è chiuso

- **29**: famiglia e medico di famiglia mancanti si vedono come "(contatto da registrare)" o "(non registrato: da completare)". Nessun indirizzo inventato, ma neanche recuperato.
- **32**: chi prescrive è ancora il reparto che scrive la nota. C'è la finestra di 180 giorni e lo stop per i farmaci sospesi dal medico.
- **43**: un documento senza data prende la data di importazione, e lo dice accanto.
- **51**: `cup_mail.invia_vera` (codice del team) fa l'invio SMTP dentro la transazione. Senza SMTP configurato non succede niente.
- **54**: `FILO_TODAY` resta 22/10/2026 di base, perché i dati sintetici sono costruiti su quel giorno. I "documenti nuovi" e l'anno di nascita dal codice fiscale ora derivano da lì.
- **55**: l'elaborazione automatica (`FILO_AUTO_RUN_AT`) gira dentro il processo web: se Filo è spento non parte.
- **57**: l'attestazione ora blocca, ma la firma del report della AI Factory non è verificata.
- **60-65**: promesse del progetto, da dichiarare (vedi README_IT.md, "Cosa NON è pronto").
- Il blocco password per postazione è in memoria: un riavvio lo azzera (il blocco del codice a tempo resta nel database).
- **Posti liberati**: le chiamate vere vanno solo a `VOCE_NUMERO_PROVA` (i numeri dei pazienti sono finti); senza telefono configurato si usano la voce dal browser o il simulatore a testo. Al telefono la fine chiamata arriva solo con il webhook firmato di ElevenLabs su `/fine-chiamata` del servizio voce: se si perde, il posto resta tenuto e l'esito si registra a mano in Posti liberati.
- **Posti liberati**: la prenotazione "salvata" è quella nel database di Filo, non nel CUP vero; la mail resta in Posta in uscita (copia vera solo a `MAIL_PROVA`, e l'invio SMTP avviene dentro la transazione come nel punto 51). "Vuole un'altra data" conta come rifiuto: Filo non cerca altre date. Un posto di oggi resta proponibile fino a fine giornata (il controllo è sul giorno, come nel resto di Filo).


## 23/10 mattina: Cure con principio attivo, liste d'attesa con l'agente Agende, unione di agende-posti

- **Cure**: prova dal vivo sulla piattaforma con i pacchetti veri di Filo (database di prova appena generato, estrazione a regole):
  P01 warfarin + ibuprofene -> gravità 3, 3 fonti (DDInter Major, DailyMed); P05 furosemide + ketoprofene -> gravità 2, 1 fonte (Moderate).
  Prima della correzione lo stesso workflow rispondeva "Dati del pacchetto non utilizzabili: Manca principio_attivo" a ogni pacchetto.
- **Liste d'attesa**: con più candidati l'agente Agende sceglie per urgenza, Filo controlla (`posti._ammessi`). Controprova: con la regola
  allentata il test `test_scavalcare_la_ricetta_senza_urgenza_alta_non_passa` fallisce. Prova dal vivo: il workflow sulla piattaforma
  NON ha ancora le istruzioni del posto liberato (risponde "non ci sono richieste doppie" e Filo lo scarta); la riserva con l'API diretta
  e lo stesso prompt sceglie un candidato dentro le regole (B + urgenza media su D + urgenza alta, entrambi ammessi).
- **Limite**: per il controllo di anonimato sui principi attivi non ci sono dati di popolazione sui farmaci: il pacchetto cure resta senza età
  e distretto, ma non c'è un conteggio k sulla coppia di farmaci.
- **Difetto trovato e chiuso**: /chiamate dava 500 con ElevenLabs configurato (avviso sulla voce nella route sbagliata, da 0ebfbc8).
  Test nuovo con ElevenLabs acceso, verificato che fallisce sul codice di prima.
- **Test che chiamavano gli agenti veri**: due test usavano `monkeypatch.undo()`, che toglieva anche gli agenti finti. Ora la rete degli
  agenti è bloccata a livello di modulo in conftest (taratura: `test_nei_test_nessuna_chiamata_vera_agli_agenti`).
- Suite: 220 test verdi.


## Verifiche - kit in ambulatorio (visita accessibile), 23/10

Base: `origin/main` a `e600709`. Ramo locale `kit-visita` nel worktree `~/filo-kit-visita`.
Prova dal vivo su una copia del database (`data/` copiata da `~/filo-lacune`), porta 8003, con SMTP e IMAP
svuotati all'avvio: nessuna mail vera è partita. 8000, 8001, 8002 e 8010 non sono stati toccati.

### Cosa c'era già e cosa manca ancora

- Già su main: prenotazione del kit e mail al coordinamento (`app/kit.py`). Non l'ho toccata.
- Non su main: il ramo `origin/kit-audio-dataset` (regole H1/T1-T5 su chi ha bisogno di cuffie o
  trascrizione, dataset di 100 pazienti, pagina "Kit udito"). È costruito sulla radice vecchia (`deed2a2`),
  non su main. Fuori da questo lavoro.

### Misure

| Prova | Risultato |
|---|---|
| `pytest -q` sul ramo | 226 verdi (220 di prima + 6 in `tests/test_kit_visita.py`) |
| Mutazioni sui test nuovi, una per volta, poi ripristino | 8 su 8 fanno diventare rosso almeno un test: invio prima della chiusura, nessun filtro sulle frasi inventate, microfono non aperto sulla pagina, schermo del tablet vivo dopo la chiusura, "salva in anagrafica" ignorato, audio accettato a visita chiusa, accettazione che legge la visita, seconda visita aperta per lo stesso kit. Le prime 7 le ho girate prima di due ritocchi a `kit_visita.py` (pezzo vuoto trattato come silenzio, un solo avviso nella mail), che non toccano le righe mutate |
| Controprova sulla base | non significativa: su `e600709` il modulo non esiste e i test falliscono all'import |
| Trascrizione vera, ElevenLabs Scribe, file WAV in italiano spezzato in pezzi da 7 s, mandato a `/kit/visita/.../audio` da un client autenticato come medico | 2 pezzi trascritti in 2,8 s e 2,5 s, testo corretto. Il terzo pezzo era vuoto (il file dura 13,5 s, non 21): ElevenLabs risponde 400 `invalid_audio`. Ora conta come silenzio, test `test_pezzo_vuoto_e_silenzio` |
| Whisper sulla AI Factory Fastweb | **non provato**: `FW_KEY_WHISPER` è vuota in tutti e tre i `.env` (filo-lacune, Scaricati/filo, fastweb-kit). Il modello `openai/whisper-large-v3-turbo` viene dal `fastweb.py` del kit Fastweb |
| Sintesi vocale ElevenLabs (per curiosità, per fare un audio di prova) | 402: la chiave non ha credito o permesso per la sintesi con la voce configurata. Riguarda anche l'agente vocale delle chiamate: da controllare prima della demo |

### Prova dal vivo (porta 8003)

1. Accettazione: "Riserva il kit" sulla pratica di Bruno Tonin -> `KB-5945a`, Kit 1, 23/10 10:20. Mail al
   coordinamento in posta in uscita, `sent_to` vuoto (non partita).
2. Medico: sul cruscotto, sotto "Visite accessibili", c'è "Avvia la visita col kit" -> pagina `KV-eded28`.
   Header della pagina: `Permissions-Policy: microphone=(self)`; sul cruscotto resta `microphone=()`.
3. Schermo del tablet, aperto senza login all'indirizzo a codice: le due frasi compaiono da sole (polling 1 s).
4. "Termina la visita" -> documento `D-KV-eded28` nella cartella, tipo "Trascrizione della visita", con la
   fonte della trascrizione nel campo "letto con". L'indirizzo del tablet risponde 404.
5. "Manda la trascrizione": il campo era già compilato con l'indirizzo in anagrafica (`famigliapaziente@...`,
   che nel `.env` è la casella di prova della famiglia: con SMTP acceso la mail partirebbe davvero). Mail in
   posta in uscita; registro: "agente kit: trascrizione della visita mandata a ...".

### Quattro classi

1. **Stato positivo**: il giro sopra, dai tasti per kit, chiusura e invio. Lo schermo del tablet l'ho
   guardato a schermo. L'audio no (punto 1 dei limiti).
2. **Stato negativo**: a visita chiusa l'audio risponde 409, il link del tablet 404, la mail parte solo a
   visita chiusa. Tutto coperto da test e mutazioni.
3. **Esclusività**: una sola visita aperta per kit (il secondo clic riprende la stessa). Il microfono è aperto
   solo su `/kit/visita/` e `/chiamate/`.
4. **Contratti preservati**: suite intera verde. `kit.py`, la prenotazione e la mail al coordinamento non sono
   cambiati. Lo stato della prenotazione resta `riservato` anche dopo la visita.

### Limiti

1. **Il percorso microfono -> filtro -> cuffie e il taglio in frasi nel browser non li ho provati con un
   microfono vero**: il pannello browser di questa sessione non ha microfono. Ho provato server, trascrizione
   e schermo; `kit.js` è controllato solo con `node --check`. Va provato su Chrome (Android o desktop), con le
   cuffie collegate, prima della demo.
2. Il microfono si apre solo su https o su localhost: per la demo dal telefono serve un tunnel https verso Filo.
3. Con ElevenLabs l'audio della visita esce verso ElevenLabs (la pagina lo scrive). Con `FW_KEY_WHISPER`
   resta sulla AI Factory Fastweb (UE).
4. Il riconoscimento delle frasi usa una soglia sul volume rispetto al rumore di fondo: in un ambulatorio
   rumoroso va tarata a orecchio.
5. Nel pannello browser la sessione è caduta più volte: una richiesta `/novita` partita da un documento vecchio
   arrivava senza utente, e `current_user` svuota la sessione. È successo anche con pagine che c'erano già
   prima (`/chiamate/in-arrivo` dopo "Riserva il kit"). Per la stessa via, una scheda di Filo rimasta aperta
   fuori sessione può azzerare il passo intermedio del login ("Modulo scaduto"). Non l'ho corretto: è fuori
   da questo lavoro.

### Agente kit sulla piattaforma FastwebAI (23/10 mattina)

Coordinato con la sessione "Chat hackathon Fastweb": il kit è mio, i suoi flussi e `flussi.py` non li tocco. Ramo `kit-visita`
riallineato sopra il suo `6a92017`. Codice in `app/flussi_kit.py` (router `/filo/kit/*` montato sul servizio voce).

Workflow creati e pubblicati nel workspace "Hackathon - TEAM10" (Gemma 4, UE; id come default in `flussi_kit.py`):
- "Filo - flusso kit prepara" `1c67419e-f535-440b-a598-cb1300c5cc5a`: Start(base, permesso, ref) -> GET visita anonima ->
  agente_kit -> componi -> POST riserva -> Response;
- "Filo - flusso kit trascrizione" `dc809c20-ea66-4f13-8439-aa0d78d5b839`: stesso schema, GET trascrizione anonima -> POST invia.
"Filo - agente kit" (`7a55dae1`, di un altro del team) non è stato toccato.

| Prova | Risultato |
|---|---|
| `pytest -q` | 247 verdi (12 in `tests/test_flussi_kit.py`, workflow finto con le stesse richieste del vero) |
| Mutazioni su `flussi_kit.py` | 7 su 7 rosse: kit inventato accettato, numeri non controllati, segnaposto non contati (sopravviveva: aggiunto `test_ricomponi_...`), nome al cloud, parole non controllate, consiglio con telefono, lunghezza non controllata |
| Agente trascrizione sulla piattaforma (workflow di prova Start -> agente_kit -> componi -> Response, stessi blocchi, poi cancellato) | 2 trascrizioni di prova, 0,9-1,7 s: punteggiatura e maiuscole, "la la" tolto, frasi spezzate unite, `[persona]`, dosi e numeri intatti, virgolette nel testo passate bene dal blocco Function. Controllo fisso di Filo: passa tutte e due |
| Agente scelta del kit (stesso modo) | cardiologia con K1 e K3 liberi: sceglie K1 (tablet) con consiglio sulla terapia, 1,0 s. Nessun kit libero: kit null, Filo scrive "nessun kit libero" |
| Whisper sulla piattaforma | **no**: un blocco Agent con `fastweb/openai/whisper-large-v3-turbo` risponde 404 (lo chiama come chat); il blocco STT accetta solo provider esterni con chiave propria. Workflow di prova cancellato |

Errori miei trovati e corretti sulla piattaforma: Start con i campi come stringa (la piattaforma non li vedeva) e messaggi
dell'agente come stringa ("empty conversation"); ora liste, riletti dopo il salvataggio.

**Non provato end-to-end** (Filo -> workflow -> richiamo di Filo dal tunnel): avviare il mio servizio voce dietro un tunnel
è stato negato dal controllo dei permessi della sessione. Il tunnel che avevo aperto l'ho chiuso. Le due metà sono provate
(Filo con il workflow finto nei test, agente e blocchi sulla piattaforma), il giro intero no.

### Interfaccia granulare, avvisi e sessioni (23/10, 08:00-10:00)

| Prova | Risultato |
|---|---|
| `pytest -q` sul ramo, poi in `~/filo-lacune` su `unione` 75d945f | 257 verdi |
| Mutazione del login a metà (current_user che svuota tutto) | test rosso; poi riscritto dall'altra sessione con l'accesso a un clic |
| Calendario di Bruno (`/persona/P05/calendario`) | solo A-05; né "Anna Zanella" né A-02; disdetta e modifica solo sulle sue visite |
| Disdetta di Bruno dalla sua scheda | torna alla scheda; avviso "Prestazione cancellata" subito, con "2 persone in lista"; la proposta porta a `/caso/<id>` |
| Risposta di Croce Verde simulata con `posta.gestisci_risposta` sulla copia 8003 | la pratica aperta si aggiorna da sola: avvisi "Mail del servizio arrivata", "Risposta ricevuta", "L'agente chiama la persona" (visto a schermo) |
| Dashboard su 8003 a 1440x900 | widget persone (con ricerca), novità, calendario, posta, Da fare a righe; clic su Bruno apre la scheda nella stessa pagina e il menu mostra le sue sezioni (visto a schermo) |
| Cookie di sessione | nome per database (`config.SESSION_COOKIE`): due copie su localhost non si buttano più fuori. Causa vera dei "Modulo scaduto" e delle sessioni cadute di stamattina |

Limiti: la risposta vera dalla casella di Croce Verde sulla 8001 non l'ho seguita io (simulata sulla 8003 con la stessa
funzione chiamata dal lettore IMAP). La dashboard del medico usa il nuovo menu ma non è stata ridisegnata a widget.
