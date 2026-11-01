"""Clicca ogni pulsante e ogni link di Filo per i due ruoli e segnala errori."""
import re, sqlite3, sys, time
import pyotp
from playwright.sync_api import sync_playwright
S = sys.argv[1] if len(sys.argv) > 1 else "."; B = "http://localhost:8000"
con = sqlite3.connect("data/filo.db"); sec = dict(con.execute("select username,totp_secret from users"))
pw = {m[0]: m[1] for m in re.findall(r"utente: (\S+)\n\s+password: (\S+)", open("data/credenziali_demo.txt").read())}
problems, clicked = [], []
last_code = {}

def fresh_code(u):
    while True:
        c = pyotp.TOTP(sec[u]).now()
        if c != last_code.get(u):
            last_code[u] = c; return c
        time.sleep(2)

def watch(pg):
    pg.on("pageerror", lambda e: problems.append(f"errore JS: {e}"))
    pg.on("console", lambda m: m.type == "error" and problems.append(f"console: {m.text} @ {pg.url}"))
    pg.on("response", lambda r: r.status >= 400 and r.request.resource_type in ("document", "stylesheet", "script") and problems.append(f"{r.status} {r.url}"))

def click(pg, sel, what, nth=0):
    loc = pg.locator(sel).nth(nth)
    loc.click(); pg.wait_for_load_state("networkidle"); clicked.append(what)
    if "Internal Server Error" in pg.content(): problems.append(f"500 dopo '{what}'")

def login(pg, u, role):
    pg.goto(B + "/login"); pg.check(f"input[value={role}]"); pg.fill("#u", u); pg.fill("#pw", pw[u])
    click(pg, "button:has-text('Avanti')", f"{u}: Avanti"); pg.fill("#c", fresh_code(u))
    click(pg, "button:has-text('Entra')", f"{u}: Entra")
    assert pg.url.endswith("/cruscotto"), pg.url

def all_links(pg, role, skip=("/logout",)):
    seen = set(); bad = []
    for _ in range(2):
        hrefs = set()
        for path in ["/cruscotto", "/elaborazione", "/posta", "/registro"] + (["/pazienti"] if role == "medico" else ["/trasporti"]) + sorted(h for h in seen if not h.endswith(".ics")):
            pg.goto(B + path)
            for h in pg.eval_on_selector_all("a[href]", "els=>els.map(e=>e.getAttribute('href'))"):
                if h.startswith("/") and not h.startswith("/static") and h not in seen and not any(h.startswith(s) for s in skip):
                    hrefs.add(h)
        for h in hrefs - seen:
            r = pg.request.get(B + h)
            seen.add(h)
            if r.status >= 400: bad.append(f"{r.status} {h}")
    return seen, bad

with sync_playwright() as p:
    b = p.chromium.launch(); ctx = b.new_context(viewport={"width": 1280, "height": 900}); pg = ctx.new_page(); watch(pg)
    # ---------------- accettazione
    login(pg, "m.bianchi", "segreteria")
    for n in ["Trasporti", "Ospedale e cloud", "Posta in uscita", "Registro", "Cruscotto"]:
        click(pg, f"header nav a:has-text('{n}')", f"menu {n}")
    click(pg, "button:has-text('Elabora le novità')", "Elabora le novità"); pg.wait_for_url("**/elaborazione**")
    click(pg, "header nav a:has-text('Cruscotto')", "torna al cruscotto")
    n_cases = pg.locator("a.case").count(); print("casi accettazione:", n_cases)
    # nota + scarta sull'ultimo caso
    click(pg, "a.case", "apri ultimo caso", nth=n_cases - 1)
    pg.fill("input[name=testo]", "Chiamata fatta, richiama domani"); click(pg, "button:has-text('Aggiungi')", "Aggiungi nota")
    assert "Chiamata fatta" in pg.content()
    pg.click("summary:has-text('Scarta')"); pg.fill("input[name=motivo]", "ha già un passaggio dal figlio"); click(pg, "button:has-text('Scarta')", "Scarta")
    assert "scartata" in pg.content()
    # approva tutti i casi rimasti, gestendo le pratiche di trasporto con esiti diversi
    esiti = ["serve", "non_disponibile", "non_serve", "non_risponde"]; k = 0
    while pg.locator("a.case").count():
        click(pg, "a.case", "apri caso")
        click(pg, "form[action$='/approva'] button", "via libera / approva")
        if "/trasporti/" in pg.url:
            e = esiti[k % 4]; k += 1
            if e == "non_disponibile":
                click(pg, "button:has-text('Non disponibile')", "servizio non disponibile")
                if pg.locator("button:has-text('Disponibile')").count():
                    click(pg, "button:has-text('Disponibile') >> nth=0", "secondo servizio disponibile")
                    click(pg, "button:has-text('Serve: conferma')", "chiamata: serve")
            else:
                click(pg, "button:has-text('Disponibile') >> nth=0", "servizio disponibile")
                if e == "serve": click(pg, "button:has-text('Serve: conferma')", "chiamata: serve")
                if e == "non_serve": click(pg, "button:has-text('Non serve')", "chiamata: non serve")
                if e == "non_risponde":
                    for i in range(3):
                        if pg.locator("button:has-text('Non risponde')").count(): click(pg, "button:has-text('Non risponde')", f"chiamata: non risponde {i+1}")
            for i in range(pg.locator("details summary").count()): pg.locator("details summary").nth(i).click()
            pg.screenshot(path=f"{S}/tr_{k}.png", full_page=True)
            click(pg, "header nav a:has-text('Cruscotto')", "torna al cruscotto")
        else:
            pg.wait_for_url("**/cruscotto**")
    pg.screenshot(path=f"{S}/c_segr_fine.png", full_page=True)
    click(pg, "header nav a:has-text('Trasporti')", "menu Trasporti"); pg.screenshot(path=f"{S}/trasporti.png", full_page=True)
    for i in range(pg.locator("a:has-text('Apri')").count()):
        click(pg, "a:has-text('Apri')", f"apri pratica {i+1}", nth=i); pg.go_back()
    click(pg, "header nav a:has-text('Posta')", "menu Posta")
    n_ics = pg.locator("a:has-text('Scarica')").count()
    for i in range(n_ics):
        with pg.expect_download() as d: pg.locator("a:has-text('Scarica')").nth(i).click()
        if "BEGIN:VEVENT" not in open(d.value.path()).read(): problems.append("ics vuoto")
    clicked.append(f"{n_ics} eventi calendario scaricati")
    seen, bad = all_links(pg, "segreteria"); problems += bad; clicked.append(f"{len(seen)} link verificati (accettazione)")
    for h in ["/pazienti", "/pazienti/P01"]:
        if pg.request.get(B + h).status != 403: problems.append(f"accettazione entra in {h}")
    click(pg, "header button:has-text('Esci')", "Esci"); assert "/login" in pg.url
    # ---------------- medico
    login(pg, "e.gatti", "medico"); pg.screenshot(path=f"{S}/m_crusc.png", full_page=True)
    for n in ["Pazienti e documenti", "Ospedale e cloud", "Posta in uscita", "Registro", "Da firmare"]:
        click(pg, f"header nav a:has-text('{n}')", f"menu {n}")
    for i in range(pg.locator(".chips a.chip").count()):
        click(pg, ".chips a.chip", f"documento collegato {i+1}", nth=i); pg.go_back()
    click(pg, "header nav a:has-text('Pazienti')", "menu Pazienti")
    pids = pg.eval_on_selector_all("table a[href^='/pazienti/P']", "els=>els.map(e=>e.getAttribute('href'))")
    for h in pids:
        pg.goto(B + h)
        for i in range(pg.locator(".filters a.chip").count()):
            click(pg, ".filters a.chip", f"filtro {h} {i}", nth=i)
        click(pg, ".filters a.chip", "filtro Tutti")
        for i in range(pg.locator("a.doc").count()):
            click(pg, "a.doc", f"documento {h} {i}", nth=i)
            if not pg.locator(".doctext").inner_text().strip(): problems.append(f"documento vuoto {pg.url}")
    pg.goto(B + "/pazienti/P01?doc=E-061"); pg.screenshot(path=f"{S}/m_cartella.png", full_page=True)
    # firma: prima codice sbagliato, poi giusto; scarta l'ultimo
    click(pg, "header nav a:has-text('Da firmare')", "menu Da firmare")
    click(pg, "a:has-text('Verifica e firma')", "Verifica e firma")
    pg.fill("#code", "000000"); click(pg, "button:has-text('Firma')", "firma con codice sbagliato")
    if "Codice non valido" not in pg.content(): problems.append("codice sbagliato accettato")
    while True:
        pg.goto(B + "/cruscotto")
        n = pg.locator("a:has-text('Verifica e firma')").count()
        if not n: break
        click(pg, "a:has-text('Verifica e firma')", "apri segnalazione")
        if n == 1:
            pg.click("summary:has-text('Scarta')"); pg.fill("input[name=motivo]", "già gestito dal medico di famiglia"); click(pg, "button:has-text('Scarta')", "Scarta (medico)")
        else:
            pg.fill("#code", fresh_code("e.gatti")); click(pg, "button:has-text('Firma')", "Firma")
            if "flash=" not in pg.url and "Fatto" not in pg.content(): problems.append(f"firma fallita {pg.url}")
    pg.screenshot(path=f"{S}/m_fine.png", full_page=True)
    seen, bad = all_links(pg, "medico"); problems += bad; clicked.append(f"{len(seen)} link verificati (medico)")
    if pg.request.get(B + "/trasporti").status != 403: problems.append("medico entra in /trasporti")
    click(pg, "header button:has-text('Esci')", "Esci medico")
    pg.set_viewport_size({"width": 390, "height": 844}); login(pg, "e.gatti", "medico"); pg.goto(B + "/pazienti/P05")
    pg.screenshot(path=f"{S}/m_mobile.png", full_page=True)
    w = pg.evaluate("document.documentElement.scrollWidth"); 
    if w > 392: problems.append(f"scroll orizzontale su telefono: {w}px")
    b.close()
print("azioni:", len(clicked)); print("\n".join(clicked[-6:]))
print("PROBLEMI:", len(problems)); print("\n".join(dict.fromkeys(problems)))
