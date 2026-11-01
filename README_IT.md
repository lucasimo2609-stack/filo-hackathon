# Filo · Centrale Operativa Territoriale

Prototipo per l'hackathon FastwebAI Agent Forge. **Dati interamente sintetici. Non è un dispositivo medico.**

Filo legge ogni mattina note, referti e agende dell'ospedale e prepara due liste di lavoro separate:

- **Accettazione**: trasporti per chi ha una visita a rischio (sciopero, vive solo, senza auto), posti liberati da riassegnare (una proposta alla volta, ogni chiamata approvata da una persona), kit per la visita accessibile. Vede solo la parte logistica.
- **Medico**: cure che si incrociano, terapie sospese, esami chiesti due volte. Ogni azione si firma con il codice a tempo.

## Come gira

```
ospedale (locale)                          cloud Fastweb (AI Factory / FastwebAI Agents)
note -> lettore a lista chiusa -> cancello -> pacchetti anonimi, codici usa e getta -> 4 agenti
        tabella delle chiavi  <- ricomposizione + controlli locali <- risposte
```

1. **Lettore locale** (`app/extract.py`): dal testo libero escono solo voci di una lista chiusa (classe di farmaco, specialità, condizione...). Nomi, vie, telefoni non passano mai. Le negazioni ("nega diabete", "escluso scompenso", "non assume") non diventano dati; una terapia sospesa da un medico non diventa "terapia abbandonata" e quel farmaco smette di contare. Contano solo i farmaci citati negli ultimi 180 giorni. Se c'è un modello locale acceso (llama.cpp su `LOCAL_LLM_URL`, `LOCAL_LLM_ENABLED=auto|on|off`) lo usa, ma un suo dato vale solo se porta la frase esatta del documento da cui viene.
2. **Cancello** (`app/gateway.py`): un pacchetto per agente e per persona, solo i campi che servono a quell'agente. Malattia rara (meno di 10 persone nel distretto): parte solo la categoria. Pacchetto trasporti: almeno 10 persone simili (k) e almeno 3 condizioni diverse tra loro (l); se non basta si allarga la fascia d'età, poi si toglie il distretto, e se ancora non basta **il pacchetto non parte**: decide il controllo locale in ospedale. I pacchetti di cure e aderenza non portano età né distretto. Il pacchetto cure porta il **principio attivo** di ogni farmaco riconosciuto per nome (warfarin, ibuprofene...), perché l'agente Cure cerca le coppie in DDInter e DailyMed: senza età, distretto né nome della persona la coppia di farmaci da sola non la individua. Con meno di due principi attivi riconosciuti il pacchetto non parte e decide il controllo locale. Le righe delle fonti che l'agente cita (DDInter, DailyMed) finiscono nelle Prove della scheda del medico.
3. **Codici usa e getta**: nuovi a ogni elaborazione e diversi per agente. Da soli non bastano, perché il contenuto può somigliarsi: un pacchetto uguale a uno già mandato oggi non si rimanda (si riusa la risposta), e nel pacchetto dell'aderenza la visita va per fasce, non in giorni esatti, così non si aggancia al pacchetto trasporti dello stesso giro.
4. **Attestazione**: prima di inviare. **Blocca** l'invio se un agente usa un modello fuori dall'elenco UE (`FILO_EU_MODELS`) o se gli indirizzi non sono quelli Fastweb; con `FILO_ATTESTATION=report` serve anche il file del report (`FILO_ATTESTATION_REPORT`) con l'impronta giusta. La verifica della firma del report della AI Factory è ancora **simulata**.
5. **Invio a gruppi mescolati** verso gli agenti, senza tenere bloccato il database durante le chiamate. Poi **ricomposizione** in ospedale e **controlli fissi** su ogni risposta: vero/falso veri, livelli solo 1-3, orari validi, codice scelto solo tra i candidati di quel posto (e mai a scavalcare una priorità della ricetta più alta senza un'urgenza alta del medico). Una risposta fuori regola non diventa un caso "inventato" ma **un caso da controllare a mano**: nessun caso sparisce in silenzio.
6. **Regole locali di riserva**, che valgono qualunque cosa risponda l'agente: anticoagulante + antinfiammatorio (o antinfiammatorio + cortisonico) va sempre al medico; anticoagulante, antidiabetico o antipertensivo sospesi da 3 giorni o più vanno sempre al medico; chi è senza auto e vive solo (o ha almeno 80 anni) con una visita entro 2 giorni o durante lo sciopero ha sempre il trasporto. Se nessun servizio è in regola nasce "Trasporto da organizzare a mano".
7. **Punteggio** calcolato in locale con regole fisse, non dal modello.
8. **Casi che non rinascono**: ogni caso ha una chiave (tipo, persona, visita o farmaci). Un caso già deciso non torna alla elaborazione dopo; uno in attesa si aggiorna con lo stesso identificativo (chi lo sta leggendo non lo perde); uno non più segnalato diventa "superato", non sparisce. Due elaborazioni insieme non partono; una che si interrompe resta scritta con il suo errore. Con `FILO_AUTO_RUN_AT=07:00` l'elaborazione parte da sola ogni mattina.

## Avvio

```bash
./avvia.sh
```

Al primo avvio crea `.venv`, copia le chiavi da `../fastweb-kit/.env` se ci sono, genera i dati sintetici e due utenti di prova. Password e QR per l'app di autenticazione finiscono in `data/credenziali_demo.txt` e `data/qr_*.png` (solo sul tuo computer, non vanno nel repo).

Inquadra i QR con Google Authenticator / Microsoft Authenticator, poi apri http://127.0.0.1:8000.

Ricominciare da zero con i dati: `.venv/bin/python -m app.seed --reset` (utenti, password e QR restano). Per rigenerare anche gli utenti: aggiungi `--nuovi-utenti`.

## Le due schermate

- **Accettazione** (verde): cruscotto con trasporti, posto liberato e kit; pagina **Trasporti** con le pratiche aperte. Non vede documenti clinici, né cartelle, né la ricomposizione codice -> persona.
- **Medico** (blu): **Da firmare** con le segnalazioni e i documenti collegati; **Pazienti e documenti** con la cartella di ogni paziente (lettere di dimissione, referti, esami, richieste, telefonate, schede sociali, verbali di pronto soccorso: 35 documenti sintetici per 10 pazienti, ognuno con il suo originale in PDF), filtri per tipo e lettore. Ogni apertura finisce nel registro.

## Documenti originali e importazione

Ogni documento ha il suo **originale** (PDF o scansione) archiviato in ospedale con l'**impronta SHA-256**: aprendo un documento Filo ricontrolla l'impronta e dice "Fonte verificata" oppure avvisa che il file è cambiato. Ogni dato letto (farmaco, condizione, terapia sospesa...) porta alla **frase esatta** del documento, evidenziata, accanto all'immagine dell'originale. Le segnalazioni hanno "Visualizza la fonte".

Come entrano i documenti, tutto in locale (niente va al cloud):

- **Nella cartella del paziente**: trascina PDF, scansioni o foto sulla zona in alto. Se il codice fiscale nel documento è di un'altra persona il file NON viene agganciato: va tra i "da smistare".
- **Importazione massiva** (pagina Importa documenti): un .zip esportato dall'archivio, file sparsi, oppure la **cartella condivisa** dove l'azienda deposita i documenti (SharePoint, OneDrive, Drive sincronizzati o disco di rete: `FILO_IMPORT_DIR` nel `.env`, di base `data/import`). Filo legge il testo (OCR locale per le scansioni), riconosce il paziente dal **codice fiscale** in testata (con il controllo del carattere finale), crea i pazienti che non ci sono e archivia tutto. Doppioni saltati. Senza codice fiscale valido, o con un nome che non torna, il documento va **da smistare** e decide una persona: l'AI non indovina a chi appartiene un referto.
- **FSE 2.0 e sistemi dell'ospedale**: mostrati come connettori non collegati. Il FSE si consulta solo con il consenso del paziente e con l'accreditamento regionale; Filo legge già i documenti CDA2 in XML.

Pacchetto di prova: `esempi/importazione_esempio_40_pazienti.zip` (128 file: 40 pazienti nuovi, 2 referti per pazienti già presenti, un doppione, una scansione senza codice fiscale). Tempi misurati su una macchina a 2 core e 8 GB: **500 pazienti, 1485 PDF in 8,5 secondi**; una scansione con OCR circa 4 secondi a pagina.

Per anteprime e scansioni servono due programmi: `sudo apt install poppler-utils tesseract-ocr tesseract-ocr-ita`. Senza, Filo funziona lo stesso: mostra il PDF nel browser e manda le scansioni tra i "da smistare".

Rigenerare gli originali o fare la prova con 500 pazienti (serve `pip install reportlab`): `python strumenti/genera_documenti.py [--pazienti 500 --uscita /tmp/prova]`.

## Kit in ambulatorio: la visita accessibile

Il kit arriva in ambulatorio con la mail al coordinamento (vedi sopra). Il giorno della visita, sul cruscotto del medico, **Avvia la visita col kit** apre una pagina per il tablet:

- il microfono a clip del medico entra nel browser, passa da un filtro (riduzione del rumore, filtri passa-alto e passa-basso, compressore, tetto al volume) ed esce nelle cuffie della persona, con il volume regolabile;
- ogni frase, chiusa da una pausa, va a trascrivere e compare in caratteri grandi. L'audio non si salva. Trascrive Whisper sulla AI Factory Fastweb se c'è `FW_KEY_WHISPER`, altrimenti ElevenLabs Scribe (lo dice la pagina);
- se la persona legge da un altro schermo c'è un indirizzo a codice, senza accesso, che smette di funzionare a fine visita;
- **Termina la visita** mette la trascrizione nella cartella come documento ("Trascrizione della visita");
- **Manda la trascrizione** la manda per mail all'indirizzo in anagrafica, oppure a uno scritto lì (si può salvare in anagrafica). Come le altre mail del prototipo, parte davvero solo verso le caselle di prova del `.env`.

Il microfono funziona solo su https o su localhost: per la demo dal telefono serve un tunnel https verso Filo.

## Trasporti: cosa fa l'agente da solo

Abbiamo controllato i servizi di Venezia e Mestre (ottobre 2026): **nessuno pubblica un calendario o una prenotazione online**. STACCO (trasporto sociale) si prenota per telefono, lun-ven 9-13, almeno 3 giorni prima, e conferma entro 48 ore; anche i gestionali delle associazioni (es. TrasportoSolidale) tengono il calendario interno, senza accesso dall'esterno. Quindi il flusso è:

1. L'accettazione dà il via libera.
2. Filo manda **da solo** la richiesta di disponibilità (andata e ritorno, indirizzo con il quartiere) al servizio che rispetta distretto, preavviso e regole del servizio: STACCO conta i giorni lavorativi perché si prenota lun-ven ed è riservato alle persone fragili. Dati della persona solo al servizio, mai al cloud. La richiesta ha una **scadenza** (i tempi del servizio, ma mai oltre mezzogiorno del giorno prima della visita).
3. La risposta del servizio la registra l'accettazione (arriva per mail o telefono). Se non può, **o se non risponde entro la scadenza**, Filo passa da solo al servizio successivo; se non ce ne sono, torna all'accettazione.
4. L'agente vocale **chiama la persona** per sapere se il trasporto serve davvero (vedi "Agente vocale"); in alternativa l'accettazione registra a mano l'esito di una telefonata. Se non risponde, Filo fissa il tentativo dopo un'ora e a quell'ora la pratica torna tra le chiamate da fare. Dopo 3 tentativi avvisa il servizio che per ora il posto non serve e torna all'accettazione.
5. Se serve: conferma al servizio e mail di conferma del CUP alla persona con il trasporto. Se non serve: il servizio viene avvisato che il posto si libera.
6. Se la visita viene spostata o annullata, Filo avvisa il servizio e riporta la pratica all'accettazione.

I controlli a tempo (scadenze, tentativi, visite cambiate) girano a ogni elaborazione e ogni volta che l'accettazione apre il cruscotto o i trasporti: senza un programma che resta acceso di notte.

Prossimo passo realistico: un accordo con le associazioni per ricevere le risposte in un formato fisso (mail con modulo o un collegamento al loro gestionale), così anche il punto 3 diventa automatico.

## Posti liberati: ordine fisso, una chiamata per ogni approvazione

Tutto in ospedale (`app/posti.py`): il pacchetto "posto liberato" non va più al cloud.

1. **Ordine**: prima la classe di priorità della ricetta (U, B, D, P), il flag messo dal medico prescrittore e già scritto nella lista d'attesa (Filo non lo cambia mai); a parità di classe, **cognome e poi nome** in ordine alfabetico. Il cognome si riconosce dal codice fiscale anche quando un documento importato scrive "Cognome Nome".
2. A ogni elaborazione Filo prepara la **proposta per il primo in ordine**: un caso nel cruscotto dell'accettazione. Nessuna chiamata parte da sola.
3. **Approvare = via libera a UNA chiamata** a quella persona: il posto resta tenuto per lei (non viene proposto ad altri) e la chiamata dell'agente vocale è pronta da avviare.
4. **Accetta**: prenotazione confermata, eventuale data precedente per lo stesso esame annullata (quel posto torna libero), richiesta tolta dalla lista d'attesa; poi Filo **rilegge il database** e solo se tutto torna manda la mail di conferma del CUP. Se il salvataggio o la verifica non vanno: nessuna mail, posto ancora tenuto, caso "Da controllare a mano" (riprova o libera dalla pagina Posti liberati).
5. **Rifiuta** (o vuole un'altra data): resta in lista nella sua posizione e Filo prepara subito la proposta per il successivo, **da approvare di nuovo**.
6. **Nessuna risposta** (o identità non confermata): posto ancora tenuto; l'accettazione sceglie se richiamare (nuova approvazione) o passare al successivo. Dopo 2 chiamate senza risposta si passa da soli al successivo.
7. **Lista esaurita**: il posto resta libero e lo si registra, senza assegnarlo d'ufficio.

Un posto ha al massimo una proposta aperta e una persona al massimo una: lo garantiscono due indici unici del database. La pagina **Posti liberati** mostra l'ordine dei candidati, lo stato di ognuno e tutti i passi (proposta, approvazione, chiamata, risposta, aggiornamento, verifica, mail, errori). Se una chiamata si chiude senza che Filo lo sappia, l'esito si registra lì a mano.

## Calendario: la fonte che l'agente Agende legge prima di agire

`app/calendario.py`, pagina **Calendario** (accettazione). Resta in ospedale: nessun dato va al cloud.

- **Agende**: un posto per esame, luogo, giorno e ora, con stato libero, tenuto (proposto, in attesa di risposta), prenotato o bloccato. Due indici unici: un posto per orario, una prenotazione in un posto solo.
- **Dati sintetici**, tutti marcati (`test=1`, colonna `fonte`, scritta "DATI DI PROVA" nella pagina): le agende di 45 giorni degli esami del dataset, le visite di `seed.py` nei loro posti, il posto disdetto, e prenotazioni di prova di altre persone (solo "occupato", nessuna persona inventata). L'ecocardiogramma è pieno, come deve essere con una lista d'attesa. Si può ripopolare: niente doppioni, e un posto liberato da una disdetta non torna occupato.
- **Operazioni** (`calendario.STRUMENTI`, eseguite da `calendario.esegui`): consulta, posto libero, disponibilità, prenota, conferma, sposta, cancella, posti liberati. Ogni scrittura rilegge il posto nella stessa transazione e lo cambia solo se è ancora alla versione letta; rifiuta doppioni (stessa persona, stesso esame) e sovrapposizioni (stessa persona, stessa ora); dopo il salvataggio rilegge con una connessione nuova e lo dice. Un annullamento o uno spostamento di una prenotazione futura crea un posto liberato per la lista d'attesa. Tutto finisce in `cal_log`.
- **Agende usa il calendario**: la proposta di un posto liberato lo rilegge prima di partire e prima dell'approvazione (se nel frattempo è stato preso, si ferma); approvare tiene il posto nel calendario, un rifiuto lo libera, un'accettazione lo conferma e annulla la data precedente. Le richieste doppie guardano se l'esame è già prenotato: se sì, il caso va al medico anche se l'agente nel cloud dice "non doppione" (regola locale fissa). All'agente nel cloud arriva un riassunto del calendario letto al momento dell'invio, nel pacchetto: `prenotazione_esistente` a fasce (entro 7 giorni, entro 30, oltre 30, già passata, nessuna) e `posti_liberi_30_giorni` (nessuno, pochi, molti). Niente date, orari, luoghi, nomi o codici delle prenotazioni.

## Modifiche alle prenotazioni: ambulatorio avvisato, paziente chiamato

`app/modifiche.py`. Una prenotazione già fissata non cambia mai senza il paziente. Quando l'ospedale deve spostarla o annullarla (pagina Calendario, "Proponi una modifica", oppure "Chiudi un posto" con una prenotazione dentro):

1. Filo rilegge il calendario, sceglie il primo posto libero adatto (stesso esame, da domani, stesso luogo se possibile, nessuna sovrapposizione) o quello indicato, e lo **tiene**;
2. **mail all'ambulatorio**: modifica proposta, non ancora valida;
3. caso nel cruscotto: **ogni chiamata al paziente** parte solo con l'approvazione di una persona. Dopo l'identità l'agente vocale spiega il cambio e registra la risposta (`conferma_modifica`);
4. **conferma**: la stessa prenotazione passa alla data nuova (o viene annullata), il posto lasciato va alla lista d'attesa (o resta chiuso, se l'ha chiuso l'ambulatorio); Filo **rilegge** il calendario e solo allora manda la mail all'ambulatorio ("confermata") e al paziente (conferma CUP o annullamento);
5. **rifiuto o altra data**: resta la data di prima, il posto tenuto torna libero, mail all'ambulatorio. **Nessuna risposta**: si richiama con una nuova approvazione; dopo 2 tentativi si chiude così;
6. **errore di salvataggio o rilettura che non torna**: niente mail di conferma, caso "Da controllare a mano" (riprova o ritira dalla pagina Calendario).

Una modifica aperta per prenotazione e un posto tenuto per una modifica sola (indici unici). Se la disdetta la chiede il paziente (sportello, telefono) si applica subito (`richiesta_dal_paziente`) e l'ambulatorio viene avvisato. Anche un posto liberato accettato dal paziente avvisa l'ambulatorio.

## Conferme alla persona (mail in stile CUP)

Quando un trasporto è confermato, o un posto liberato viene accettato al telefono, alla persona (o al familiare indicato) arriva una mail come quella del CUP: codice prenotazione, assistito con codice fiscale in parte nascosto, prestazione, priorità, data e luogo, impegnativa (NRE), medico prescrittore, preparazione, cosa portare, come disdire (almeno 2 giorni lavorativi prima, altrimenti ticket e possibile sanzione) e, se c'è, il riquadro del trasporto organizzato. L'evento di calendario è allegato. Ente, indirizzi e numeri sono inventati e ogni mail dice che è una prova.

Di base le mail restano in "Posta in uscita" (anteprima come la vedrebbe la persona). Per riceverne una copia vera: `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD` e `MAIL_PROVA` nel `.env` (con Gmail serve una password per app). Filo invia SOLO a `MAIL_PROVA`.

## Agente vocale (ElevenLabs per la voce, Fastweb per il ragionamento)

L'agente telefona per tre motivi: confermare un trasporto, proporre un posto liberato (e chiedere se serve un passaggio), chiedere a una persona come sta e se prende le medicine. ElevenLabs fa solo telefono, ascolto e voce. Il ragionamento è Gemma 4 sulla AI Factory Fastweb, chiamato da un piccolo servizio di Filo (`app/voce/server.py`, porta 8010), che ElevenLabs usa come "modello personalizzato".

Regole decise da Filo, non dal modello:

- Prima frase: "sono l'assistente automatico, non sono una persona" (AI Act, art. 50).
- Identità: l'agente chiede l'anno di nascita e lo controlla Filo. Prima della conferma il modello NON riceve visite, terapie o servizi: non può dirli. Due errori: saluta e chiude.
- Segnali d'allarme (dolore al petto, fiato corto, caduta, svenimento, sangue...): regole fisse sulla frase, risposta fissa ("chiami il 112, avviso l'infermiere"), caso in cima alla lista del medico. Il modello non viene interpellato.
- Strumenti (identità, trasporto, appuntamento, stato di salute, operatore) eseguiti da Filo con controlli sui valori (un orario deve essere un orario tra 3 ore e 30 minuti prima della visita, le risposte sanitarie da un elenco chiuso).
- Se il modello prova a chiudere, o dice "ho segnato", senza aver registrato l'esito, Filo lo obbliga a registrarlo prima.
- Segreteria: messaggio neutro, mai informazioni sanitarie.
- Esiti: trasporto e appuntamenti all'accettazione (le pratiche avanzano da sole), "come sta" e trascrizioni solo al medico.

A ElevenLabs vanno solo il nome (per salutare) e il codice della chiamata; il resto del contesto lo costruisce Filo a ogni battuta. La voce però passa da ElevenLabs: in produzione serve il piano con residenza dei dati in UE e conservazione zero, oppure un'alternativa europea per la voce. La AI Factory, in streaming, a volte manda le lettere accentate spezzate: Filo le ripara prima che arrivino alla voce.

**Provare senza ElevenLabs**: pagina Chiamate -> apri una chiamata -> "Simulatore a testo". Stesso agente, stessi controlli.

**Accendere la voce vera** (dal browser del portatile):

1. Nel `.env`: `ELEVENLABS_API_KEY=...` (la chiave la crei su elevenlabs.io, Impostazioni -> API Keys).
2. Un tunnel verso il SOLO servizio della voce (le schermate di Filo restano private). Con cloudflared, senza account:
   `cloudflared tunnel --url http://127.0.0.1:8010` -> stampa un indirizzo `https://xxxx.trycloudflare.com`.
   Installazione su Ubuntu: `curl -L -o /tmp/cf.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb && sudo dpkg -i /tmp/cf.deb`.
3. `.venv/bin/python -m app.voce.configura --url https://xxxx.trycloudflare.com` -> crea l'agente (voce italiana se c'è nella tua libreria, parlata un po' più lenta, conservazione zero se il piano lo permette) e stampa `ELEVEN_AGENT_ID=...` da aggiungere al `.env`. Il codice segreto del servizio della voce viene salvato tra i segreti di ElevenLabs, mai stampato. Se il tunnel cambia indirizzo, rilancia lo stesso comando: aggiorna l'agente.
4. Riavvia Filo (`./avvia.sh`) e in una chiamata premi "Avvia dal browser (voce)".

**Telefono** (facoltativo): importa un numero Twilio o SIP in ElevenLabs e metti nel `.env` `ELEVEN_PHONE_NUMBER_ID` e `VOCE_NUMERO_PROVA` (il telefono di qualcuno del team: Filo chiama SOLO quel numero, perché i numeri dei pazienti sono inventati). Per chiudere le chiamate telefoniche, nel pannello ElevenLabs imposta il webhook di fine chiamata su `https://xxxx.trycloudflare.com/fine-chiamata` e copia il suo segreto in `ELEVEN_WEBHOOK_SECRET`.

## Agenti: API diretta o piattaforma

Nel `.env`:

```
AGENT_CURE_BACKEND=sim          # api (predefinito) | sim
SIM_WF_CURE=<id del flusso pubblicato su FastwebAI Agents>
SIM_API_KEY=<Sim Key da Settings>
```

Con `sim` Filo esegue il flusso sulla piattaforma; se fallisce ripiega da solo sull'API diretta e lo scrive nella pagina "Ospedale e cloud".

## Sicurezza dell'applicazione

- Accesso in due passi: password (scrypt) + codice a tempo (TOTP). Dopo 5 password sbagliate si blocca **quella postazione** per 5 minuti, non l'utente: chi conosce solo il nome del medico non può chiudergli l'accesso. Stesso messaggio che l'utente esista o no.
- Codice a tempo valido una volta sola, anche dopo un riavvio (i codici usati stanno nel database).
- Il medico firma con il codice a tempo sia per approvare sia per **scartare** una segnalazione clinica.
- Un caso si esegue una volta sola anche con due clic o due persone insieme; un caso già eseguito non si può più scartare.
- Ruoli separati lato server: l'accettazione riceve 403 sui casi clinici, non vede la ricomposizione codice -> persona, e nella pagina "Ospedale e cloud" vede solo i pacchetti logistici; nel registro vede solo le azioni del proprio ruolo.
- Ogni apertura di scheda, documento, originale o anteprima va nel registro accessi. Uscita automatica dopo 15 minuti di inattività. L'uscita chiude la sessione anche lato server: una copia del cookie non serve più.
- I messaggi a schermo arrivano dalla sessione, mai dall'indirizzo: un link non può far comparire in Filo un testo scelto da altri.
- Protezione CSRF, cookie SameSite strict (`FILO_HTTPS=1` per il cookie Secure dietro HTTPS), CSP senza risorse esterne (niente caratteri da Google), nessuna pagina incorniciabile tranne l'anteprima della mail dentro Filo.
- Le mail non partono davvero: finiscono in "Posta in uscita"; gli eventi .ics sono veri.

## Interfaccia: scelte e fonti

Le liste di lavoro (cruscotto dell'accettazione, "Da firmare" del medico) seguono pratiche documentate in sanità:

- **Priorità a cinque livelli**: Immediata (85-100), Urgente (65-84), Differibile (40-64), Minore (20-39), Non urgente (sotto 20). Ordine e colori del triage italiano (Accordo Stato-Regioni 2019), nomi propri: Filo non è un triage di pronto soccorso e non promette tempi. **Le soglie sono una scelta del prototipo**, non vengono da una fonte.
- **Il colore non è mai l'unica informazione** (WCAG 2.2, criterio 1.4.1; NHS design system): ogni livello ha parola, colore, simbolo e un testo per lo screen reader.
- **Una regola fissa alza il livello**: come il "red score" del NEWS2, una combinazione pericolosa o un trasporto indispensabile portano il caso almeno a Urgente anche con un totale basso.
- **Ogni segnalazione dice da dove viene** (agente, regola fissa, da controllare a mano, chiamata), come la fonte obbligatoria delle card CDS Hooks di HL7, e **da quanto aspetta**.
- **L'azione proposta si fa dalla riga** (via libera, riserva il kit, approva la chiamata): le scorciatoie d'azione e l'avviso al ruolo giusto sono ciò che migliora l'accettazione degli avvisi (revisione sistematica, JAMIA 2019).
- **Scarto con motivo da elenco** (come gli override reasons di CDS Hooks) più un dettaglio libero.
- **Al massimo due avvisi esterni**, il più grave prima (NHS warning callout, VA.gov).
- Stile: font di sistema, intestazione traslucida, chiaro e scuro, movimento ridotto e contrasto alto rispettati.

## Test

```bash
.venv/bin/python -m pytest -q                       # 160 test, senza rete
.venv/bin/python strumenti/prova_pulsanti.py        # con Filo acceso e dati appena ricreati: clicca ogni pulsante e link dei due ruoli
```

`prova_pulsanti.py` usa Playwright (`pip install playwright && playwright install chromium`) e consuma i casi del giorno: dopo, rigenera i dati con `python -m app.seed --reset`.

Girano con agenti finti, senza rete. `tests/test_lacune_chiuse.py` prende una per una le lacune trovate il 22/10 e prova che sono chiuse, passando dalle rotte vere dove la lacuna era in una rotta: agenti in errore o fuori formato, modello che dice "no" a un caso urgente, nessun servizio, doppio clic, casi che rinascono, pagine e registro per ruolo, sessione revocata, negazioni, omocodie, documenti senza codice fiscale. Controllano poi che nessuna "spia" (nomi, vie, telefoni, frasi delle note) esca nei pacchetti, che passino solo campi ammessi, che la malattia rara venga generalizzata, che l'attaccante con codici fissi ricolleghi i pacchetti e con codici usa e getta no, e che l'accettazione non veda i casi clinici.

## Cosa NON è pronto per un ospedale vero

Attestazione: la firma del report della AI Factory non è ancora verificata; utenti di prova; date dei dati sintetici costruite sul 22/10/2026 (`FILO_TODAY`); un documento senza data prende quella di importazione (è scritto accanto); i farmaci si attribuiscono al reparto che scrive la nota; il kit per l'udito è solo una prenotazione per l'ambulatorio; i controlli prescritti e mai prenotati non vengono ancora cercati; le risposte dei servizi di trasporto le registra una persona; SQLite in chiaro sul disco; nessuna integrazione con FSE/CUP reali; valutazione clinica degli agenti da fare con medici; valutazione d'impatto (DPIA) e classificazione AI Act ad alto rischio da fare.
