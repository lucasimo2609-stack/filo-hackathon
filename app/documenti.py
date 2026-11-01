"""Cartelle SINTETICHE dei 10 pazienti: lettere di dimissione, referti, esami, prescrizioni, telefonate, note sociali.
Nessun dato reale. I documenti con nomi, vie e telefoni servono da "spie" per il test del cancello.

Campi: id, paziente, tipo, reparto, data, titolo, autore, testo
"""

DOCS = [
    # ---------------------------------------------------------------- P01 Giuseppe Rossato
    ("E-061", "P01", "lettera_dimissione", "medicina", "2026-08-11", "Lettera di dimissione", "dott.ssa Carla Venturini, Medicina generale",
     """Ricovero dal 04/08/2026 al 11/08/2026.
Motivo del ricovero: cardiopalmo e dispnea da sforzo insorti da 3 giorni.
Diagnosi: fibrillazione atriale ad alta risposta ventricolare, di nuova insorgenza.
Decorso: controllo della frequenza ottenuto con bisoprololo. Ecocardiogramma: frazione di eiezione 52%, atrio sinistro dilatato.
CHA2DS2-VASc 3. Avviata terapia anticoagulante orale con warfarin, target INR 2-3.
Terapia alla dimissione: warfarin secondo schema INR, bisoprololo 2,5 mg al mattino.
Controlli: INR entro 5 giorni presso il centro TAO; visita cardiologica a 4 settimane."""),
    ("E-087", "P01", "referto", "cardiologia", "2026-09-02", "Visita cardiologica di controllo", "dott. Paolo Zennaro, Cardiologia",
     "Fibrillazione atriale permanente. Prosegue terapia anticoagulante orale con warfarin, controllo INR mensile."),
    ("E-088", "P01", "esami_laboratorio", "laboratorio", "2026-10-06", "Esami del sangue", "Laboratorio analisi",
     """INR 2,6 (intervallo terapeutico 2,0-3,0)
Emoglobina 13,1 g/dl (13,5-17,5) leggermente bassa
Piastrine 210.000/mm3
Creatinina 1,1 mg/dl
Potassio 4,3 mmol/l"""),
    ("E-093", "P01", "referto", "ortopedia", "2026-10-15", "Visita ortopedica", "dott. Luca Bortolato, Ortopedia",
     "Gonartrosi bilaterale. Si prescrive ibuprofene 600 mg due volte al giorno per 10 giorni."),
    ("E-112", "P01", "nota_reparto", "medicina", "2026-10-16", "Nota del reparto",  "inf. Silvia Pagan",
     "Il sig. Rossato vive solo da quando è mancata la moglie, il figlio Marco abita a Torino (tel. 347 1234567). Non guida più."),

    # ---------------------------------------------------------------- P02 Anna Zanella
    ("E-140", "P02", "referto", "cardiologia", "2026-06-18", "Visita cardiologica", "dott. Paolo Zennaro, Cardiologia",
     """Ipertensione arteriosa in scarso controllo: PA 165/95 in ambulatorio, 158/92 al domicilio (media di 7 giorni).
ECG: ritmo sinusale, segni di ipertrofia ventricolare sinistra.
Si introduce ramipril 5 mg la sera. Controllo di creatinina e potassio a 2 settimane. Visita di controllo a ottobre."""),
    ("E-141", "P02", "esami_laboratorio", "laboratorio", "2026-07-03", "Esami del sangue", "Laboratorio analisi",
     """Creatinina 0,9 mg/dl
Potassio 4,6 mmol/l
Sodio 139 mmol/l
Colesterolo LDL 128 mg/dl"""),
    ("E-201", "P02", "telefonata", "infermiere di famiglia", "2026-10-20", "Telefonata dell'infermiere di famiglia", "inf. Davide Marchiori",
     "La signora Zanella riferisce: le pillole per la pressione (ramipril) mi fanno girare la testa, da una settimana non le prendo. Il marito non guida."),

    # ---------------------------------------------------------------- P03 Teresa Furlan
    ("E-170", "P03", "lettera_dimissione", "medicina", "2026-08-29", "Lettera di dimissione", "dott.ssa Carla Venturini, Medicina generale",
     """Ricovero dal 22/08/2026 al 29/08/2026.
Diagnosi: scompenso cardiaco a frazione di eiezione ridotta (FE 35%), riacutizzato.
Decorso: buona risposta alla terapia endovenosa, calo ponderale di 3 kg.
Terapia alla dimissione: furosemide 25 mg al mattino, ramipril 2,5 mg la sera, bisoprololo 1,25 mg.
Indicazioni: pesarsi ogni mattina, contattare il medico se aumento di peso superiore a 2 kg in 3 giorni.
Da programmare ecocardiogramma di controllo."""),
    ("E-171", "P03", "richiesta", "cardiologia", "2026-09-12", "Richiesta di esame", "dott. Paolo Zennaro, Cardiologia",
     "Si richiede ecocardiogramma di controllo dopo riacutizzazione di scompenso. Priorità B."),
    ("E-172", "P03", "esami_laboratorio", "laboratorio", "2026-10-09", "Esami del sangue", "Laboratorio analisi",
     """NT-proBNP 1.850 pg/ml (valore alto)
Creatinina 1,3 mg/dl
Potassio 4,9 mmol/l
Sodio 134 mmol/l (leggermente basso)"""),
    ("E-173", "P03", "nota_sociale", "servizi sociali", "2026-10-13", "Scheda dei servizi sociali", "A.S. Giulia Rampazzo",
     "La signora Furlan vive sola al secondo piano senza ascensore in via Torre Belfredo. Esce di casa con fatica. Una vicina la aiuta con la spesa."),

    # ---------------------------------------------------------------- P04 Lucia Pavan
    ("E-149", "P04", "referto", "pneumologia", "2026-09-25", "Visita pneumologica", "dott.ssa Elisa Cagnin, Pneumologia",
     """BPCO di grado moderato (GOLD 2), ex fumatrice (30 pacchi/anno, ha smesso nel 2019).
Spirometria: VEMS 62% del teorico.
TAC del 2025: due micronoduli polmonari di 4 e 5 mm al lobo superiore destro, da controllare.
Terapia: broncodilatatore a lunga durata d'azione, un'inalazione al giorno."""),
    ("E-150", "P04", "richiesta", "pneumologia", "2026-10-02", "Richiesta di esame", "dott.ssa Elisa Cagnin, Pneumologia",
     "Si richiede TAC torace senza mezzo di contrasto per controllo noduli."),
    ("E-232", "P04", "verbale_ps", "pronto soccorso", "2026-10-20", "Verbale di pronto soccorso", "dott. Marco Furlanetto, Pronto soccorso",
     """Accesso per dispnea da sforzo ingravescente da 10 giorni. Saturazione 94% in aria ambiente. Non febbre.
Rx torace: non addensamenti. Esami nella norma.
Dimessa con indicazione a rivalutazione internistica a breve."""),
    ("E-233", "P04", "richiesta", "medicina interna", "2026-10-21", "Richiesta di esame", "dott. Andrea Semenzato, Medicina interna",
     "Richiesta TAC torace senza mdc, dispnea da sforzo."),

    # ---------------------------------------------------------------- P05 Bruno Tonin
    ("E-090", "P05", "referto", "cardiologia", "2026-09-28", "Visita cardiologica", "dott. Paolo Zennaro, Cardiologia",
     "Amiloidosi cardiaca da transtiretina. In terapia con furosemide 25 mg."),
    ("E-091", "P05", "nota_reparto", "medicina", "2026-09-30", "Nota del reparto", "inf. Silvia Pagan",
     "Ex falegname, abita sopra la ferramenta in piazza Ferretto, porta l'apparecchio acustico."),
    ("E-092", "P05", "esami_laboratorio", "laboratorio", "2026-10-07", "Esami del sangue", "Laboratorio analisi",
     """Creatinina 1,5 mg/dl (0,7-1,2) alta
Filtrato glomerulare stimato 41 ml/min
Potassio 4,1 mmol/l
NT-proBNP 2.400 pg/ml"""),
    ("E-094", "P05", "referto", "geriatria", "2026-10-12", "Visita geriatrica", "dott.ssa Francesca Barbieri, Geriatria",
     "Dolori articolari alle mani: si consiglia ketoprofene 50 mg al bisogno, massimo due volte al giorno."),
    ("E-205", "P05", "prenotazione", "CUP", "2026-10-14", "Nota di prenotazione CUP", "sportello CUP",
     "Il paziente chiede un supporto per l'udito durante la visita."),

    # ---------------------------------------------------------------- P06 Sergio Vianello
    ("E-301", "P06", "referto", "diabetologia", "2026-10-01", "Visita diabetologica", "dott.ssa Laura Scarpa, Diabetologia",
     "Diabete tipo 2 in buon compenso. Prosegue metformina 1000 mg."),
    ("E-306", "P06", "esami_laboratorio", "laboratorio", "2026-09-24", "Esami del sangue", "Laboratorio analisi",
     """Emoglobina glicata 6,8% (obiettivo sotto il 7%)
Glicemia a digiuno 128 mg/dl
Creatinina 0,9 mg/dl
Microalbuminuria assente"""),
    ("E-307", "P06", "referto", "oculistica", "2026-05-15", "Fondo oculare", "dott. Giorgio Tessari, Oculistica",
     "Fondo oculare: non segni di retinopatia diabetica. Controllo tra 12 mesi."),

    # ---------------------------------------------------------------- P07 Giovanna Boscolo
    ("E-302", "P07", "nota_reparto", "geriatria", "2026-10-10", "Nota del reparto", "inf. Martina Baldan",
     "La signora Boscolo vive sola, non ha l'auto. Ipertensione in terapia con amlodipina, assunta regolarmente."),
    ("E-308", "P07", "referto", "geriatria", "2026-10-10", "Valutazione geriatrica", "dott.ssa Francesca Barbieri, Geriatria",
     """Valutazione multidimensionale: autonoma nelle attività di base, parzialmente dipendente per spesa e uscite.
Test del cammino: andatura rallentata, una caduta in casa a luglio senza conseguenze.
Soffio sistolico all'auscultazione: richiesto ecocardiogramma (priorità D)."""),
    ("E-309", "P07", "esami_laboratorio", "laboratorio", "2026-09-30", "Esami del sangue", "Laboratorio analisi",
     """Emoglobina 12,4 g/dl
Vitamina D 14 ng/ml (bassa)
Creatinina 0,8 mg/dl"""),

    # ---------------------------------------------------------------- P08 Franco De Marchi
    ("E-303", "P08", "referto", "endocrinologia", "2026-10-05", "Visita endocrinologica", "dott. Stefano Mazzon, Endocrinologia",
     "Ipotiroidismo. Levotiroxina 75 mcg al mattino."),
    ("E-310", "P08", "esami_laboratorio", "laboratorio", "2026-09-28", "Esami del sangue", "Laboratorio analisi",
     """TSH 3,1 mUI/l (0,4-4,0)
FT4 1,2 ng/dl
Colesterolo totale 205 mg/dl"""),
    ("E-311", "P08", "richiesta", "cardiologia", "2026-09-30", "Richiesta di esame", "dott. Paolo Zennaro, Cardiologia",
     "Si richiede ecocardiogramma per dispnea lieve e soffio aortico. Priorità B."),

    # ---------------------------------------------------------------- P09 Elisa Carraro
    ("E-304", "P09", "referto", "pneumologia", "2026-09-20", "Visita pneumologica", "dott.ssa Elisa Cagnin, Pneumologia",
     "BPCO stabile. Prosegue terapia inalatoria."),
    ("E-312", "P09", "telefonata", "infermiere di famiglia", "2026-10-19", "Telefonata dell'infermiere di famiglia", "inf. Davide Marchiori",
     "La signora Carraro dice che da due settimane non prende più il broncodilatatore perché la confezione nuova costa troppo e aspetta di parlarne con il medico. Tosse al mattino un po' aumentata."),
    ("E-313", "P09", "esami_laboratorio", "laboratorio", "2026-09-18", "Emogasanalisi", "Laboratorio analisi",
     """pH 7,41
pO2 74 mmHg
pCO2 42 mmHg
Saturazione 95%"""),

    # ---------------------------------------------------------------- P10 Renato Scarpa
    ("E-305", "P10", "telefonata", "infermiere di famiglia", "2026-10-19", "Telefonata dell'infermiere di famiglia", "inf. Davide Marchiori",
     "Il signor Scarpa vive solo, figlia a Padova. Dice che prende tutte le medicine. Ha prenotato la visita oculistica."),
    ("E-314", "P10", "referto", "medicina generale", "2026-09-10", "Bilancio di salute", "dott.ssa Rizzo, Medicina generale",
     """Uomo di 86 anni in buone condizioni generali. Calo della vista negli ultimi mesi, soprattutto alla guida serale: ha smesso di guidare di sera.
Pressione 135/80. Non terapie croniche.
Inviato a visita oculistica per sospetta cataratta."""),
]
