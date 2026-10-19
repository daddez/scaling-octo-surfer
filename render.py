#!/usr/bin/env python3
"""
Esegue un ELENCO DI AZIONI con Safari vero (safaridriver su macOS, non emulato) e salva:
  render-results/rendered.html   codice della pagina alla fine delle azioni
  render-results/screenshot.png  foto della pagina
  render-results/meta.json       browser usato, esito, captcha, tempi
  render-results/log.txt         log granulare, una riga per evento

Input (variabili d'ambiente):
  JOB_ID                 ID del lavoro (lettere, numeri, - e _)
  AZIONI                 lista JSON di azioni, es.
                         [{"tag": "view", "url": "https://example.com"},
                          {"tag": "scroll_fino_a_fine", "attesa_s": 6},
                          {"tag": "click", "selettore": "#accetta"}]
  TWOCAPTCHA_API_KEY     opzionale: chiave 2Captcha per reCAPTCHA v2 e Turnstile

Azioni ammesse (tag): view, wait, captcha, scroll, scroll_fino_a_fine, click, click_testo, click_id.
Le variabili di ogni azione sono descritte in ESEGUIBILI, piu' sotto.
"""
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

OUT = Path("render-results")
API_2CAPTCHA = "https://api.2captcha.com"
MAX_AZIONI = 15

CAPTCHA_DETECT_JS = """
const recaptcha = document.querySelector('.g-recaptcha[data-sitekey]');
if (recaptcha) { return {tipo: 'recaptcha_v2', key: recaptcha.getAttribute('data-sitekey')}; }
const turnstile = document.querySelector('.cf-turnstile[data-sitekey]');
if (turnstile) { return {tipo: 'turnstile', key: turnstile.getAttribute('data-sitekey')}; }
return {tipo: null, key: null};
"""

CAPTCHA_INJECT_JS = {
    "recaptcha_v2": """
        const token = arguments[0];
        const ta = document.getElementById('g-recaptcha-response')
                || document.querySelector('textarea[name="g-recaptcha-response"]');
        if (ta) { ta.style.display = 'block'; ta.value = token; ta.innerHTML = token; }
        if (window.___grecaptchaCallback) { window.___grecaptchaCallback(token); }
    """,
    "turnstile": """
        const token = arguments[0];
        const inp = document.querySelector('input[name="cf-turnstile-response"]');
        if (inp) { inp.value = token; }
    """,
}
CAPTCHA_TASK = {"recaptcha_v2": "RecaptchaV2TaskProxyless", "turnstile": "TurnstileTaskProxyless"}

# Trova l'elemento cliccabile VISIBILE il cui testo coincide (o, in mancanza, contiene) quello cercato.
JS_TROVA_PER_TESTO = """
const t = arguments[0].trim().toLowerCase();
const cand = Array.from(document.querySelectorAll(
  'a, button, input[type=submit], input[type=button], [role=button], summary, label'));
const visibile = e => {
  const r = e.getBoundingClientRect(), cs = getComputedStyle(e);
  return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none';
};
const testo = e => (e.innerText || e.value || e.getAttribute('aria-label') || e.title || '').trim().toLowerCase();
const esatti = cand.filter(e => visibile(e) && testo(e) === t);
if (esatti.length) { return esatti[0]; }
return cand.filter(e => visibile(e) && testo(e).includes(t))[0] || null;
"""

# Numera link e pulsanti nell'ordine in cui compaiono (b1, b2, ...) scrivendo l'id nell'attributo
# data-btn-id: chi riceve la pagina lo legge nel DOM; click_id ritrova l'elemento da qui.
JS_NUMERA = """
const cand = Array.from(document.querySelectorAll(
  'a[href], button, input[type=submit], input[type=button], [role=button], summary, '
  + 'input:not([type]), input[type=text], input[type=search]'));
let n = 0;
for (const e of cand) { e.setAttribute('data-btn-id', 'b' + (++n)); }
return n;
"""

JS_SCRIVI = """
const e = arguments[0], t = arguments[1];
e.focus();
Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(e, t);
e.dispatchEvent(new Event('input', {bubbles: true}));
e.dispatchEvent(new Event('change', {bubbles: true}));
if (e.form) { if (e.form.requestSubmit) { e.form.requestSubmit(); } else { e.form.submit(); } return 'modulo'; }
for (const tipo of ['keydown', 'keypress', 'keyup']) {
  e.dispatchEvent(new KeyboardEvent(tipo, {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}));
}
return 'invio';
"""

LOG_RIGHE = []


def log(msg):
    riga = f"{datetime.now(timezone.utc).isoformat(timespec='milliseconds')} {msg}"
    LOG_RIGHE.append(riga)
    print(riga, flush=True)


class Ctx:
    """Stato condiviso dalle azioni di UN lavoro."""

    def __init__(self, d, meta):
        self.d = d
        self.meta = meta
        self.url = None


# ----------------------------------------------------------------- captcha

def risolvi_captcha(tipo, key, url):
    """Chiede il token a 2Captcha. Ritorna il token o None."""
    chiave = os.environ.get("TWOCAPTCHA_API_KEY")
    if not chiave:
        log("captcha rilevato ma TWOCAPTCHA_API_KEY non e' impostata")
        return None
    r = requests.post(f"{API_2CAPTCHA}/createTask", timeout=30, json={
        "clientKey": chiave,
        "task": {"type": CAPTCHA_TASK[tipo], "websiteURL": url, "websiteKey": key},
    }).json()
    if r.get("errorId"):
        log(f"2Captcha createTask: errore {r.get('errorCode')}")
        return None
    inizio = time.time()
    while time.time() - inizio < 180:
        time.sleep(5)
        r2 = requests.post(f"{API_2CAPTCHA}/getTaskResult", timeout=30,
                           json={"clientKey": chiave, "taskId": r["taskId"]}).json()
        if r2.get("errorId"):
            log(f"2Captcha getTaskResult: errore {r2.get('errorCode')}")
            return None
        if r2.get("status") == "ready":
            sol = r2["solution"]
            return sol.get("gRecaptchaResponse") or sol.get("token")
    log("2Captcha: tempo scaduto")
    return None


def gestisci_captcha(d, url, meta):
    esito = d.execute_script(CAPTCHA_DETECT_JS)
    if not esito or not esito.get("tipo"):
        return
    tipo = esito["tipo"]
    log(f"captcha rilevato: {tipo}")
    meta["captcha"] = {"tipo": tipo, "risolto": False}
    token = risolvi_captcha(tipo, esito["key"], url)
    if token:
        d.execute_script(CAPTCHA_INJECT_JS[tipo], token)
        meta["captcha"]["risolto"] = True
        log("captcha risolto e token iniettato")
        time.sleep(3)


# ----------------------------------------------------------------- scroll

def scroll_umano(d):
    """Scorre fino in fondo con passi e pause irregolari (carica i contenuti pigri)."""
    ultima, fermi = d.execute_script("return document.body.scrollHeight"), 0
    for _ in range(120):
        d.execute_script(f"window.scrollBy(0, {random.randint(200, 600)});")
        time.sleep(random.uniform(0.3, 1.0))
        altezza = d.execute_script("return document.body.scrollHeight")
        in_fondo = d.execute_script("return (window.innerHeight + window.scrollY) >= document.body.scrollHeight - 5")
        if in_fondo:
            fermi = fermi + 1 if altezza == ultima else 0
            if fermi >= 2:
                return
        ultima = altezza


def stato_pagina(d):
    """(numero di elementi, altezza della pagina): cambia quando la pagina carica altro contenuto."""
    return d.execute_script("return [document.getElementsByTagName('*').length, document.body.scrollHeight]")


# ----------------------------------------------------------------- azioni
# Ogni azione riceve il contesto e il suo dizionario {"tag": ..., ...variabili}.

def az_view(ctx, a):
    ctx.url = a["url"]
    log(f"apro {ctx.url}")
    try:
        ctx.d.get(ctx.url)
    except Exception as e:  # tempo scaduto di caricamento: leggo quello che c'e'
        log(f"caricamento non completo: {type(e).__name__}")
        ctx.meta["caricamento_incompleto"] = True
        try:
            elementi = stato_pagina(ctx.d)[0]
        except Exception:
            elementi = 0
        if elementi < 50:  # pagina praticamente vuota: riprovo una volta con piu' tempo
            log(f"la pagina ha solo {elementi} elementi: riprovo una volta con 120 secondi")
            ctx.meta["caricamento_riprovato"] = True
            ctx.d.set_page_load_timeout(120)
            try:
                ctx.d.get(ctx.url)
                ctx.meta["caricamento_incompleto"] = False
            except Exception as e2:
                log(f"anche il secondo caricamento e' incompleto: {type(e2).__name__}")
            finally:
                ctx.d.set_page_load_timeout(60)
            log(f"dopo il secondo tentativo la pagina ha {stato_pagina(ctx.d)[0]} elementi")
    gestisci_captcha(ctx.d, ctx.url, ctx.meta)


def az_wait(ctx, a):
    log(f"attendo {a['secondi']} secondi")
    time.sleep(a["secondi"])


def az_captcha(ctx, a):
    gestisci_captcha(ctx.d, ctx.url or ctx.d.current_url, ctx.meta)


def az_scroll(ctx, a):
    log("scorro fino in fondo")
    scroll_umano(ctx.d)


def az_scroll_fino_a_fine(ctx, a):
    """Scorre in fondo, aspetta, e se e' comparso altro contenuto ripete; si ferma
    quando dopo l'attesa non compare piu' nulla (o dopo max_cicli)."""
    attesa, max_cicli = a.get("attesa_s", 6), a.get("max_cicli", 30)
    for ciclo in range(1, max_cicli + 1):
        prima = stato_pagina(ctx.d)
        scroll_umano(ctx.d)
        time.sleep(attesa)
        dopo = stato_pagina(ctx.d)
        log(f"ciclo {ciclo}: elementi {prima[0]} -> {dopo[0]}, altezza {prima[1]} -> {dopo[1]}")
        if dopo == prima:
            log("nessun nuovo contenuto dopo l'attesa: fine dello scorrimento")
            return
    log(f"raggiunto il massimo di {max_cicli} cicli")


def az_click(ctx, a):
    log(f"click su {a['selettore']}")
    ctx.d.find_element(By.CSS_SELECTOR, a["selettore"]).click()
    time.sleep(random.uniform(1.5, 3))
    gestisci_captcha(ctx.d, ctx.url or ctx.d.current_url, ctx.meta)


def az_click_testo(ctx, a):
    log(f"click sul testo '{a['testo']}'")
    el = ctx.d.execute_script(JS_TROVA_PER_TESTO, a["testo"])
    if el is None:
        raise ValueError(f"nessun elemento cliccabile visibile con il testo '{a['testo']}'")
    el.click()
    time.sleep(random.uniform(1.5, 3))
    gestisci_captcha(ctx.d, ctx.url or ctx.d.current_url, ctx.meta)


def az_click_id(ctx, a):
    n = ctx.d.execute_script(JS_NUMERA)
    log(f"numerati {n} elementi; click su {a['id']}")
    els = ctx.d.find_elements(By.CSS_SELECTOR, f"[data-btn-id='{a['id']}']")
    if not els:
        raise ValueError(f"nessun elemento con id {a['id']} (la pagina ne ha {n})")
    ctx.d.execute_script("arguments[0].scrollIntoView({block:'center'});", els[0])
    time.sleep(0.5)
    els[0].click()
    time.sleep(random.uniform(1.5, 3))
    gestisci_captcha(ctx.d, ctx.url or ctx.d.current_url, ctx.meta)


def az_scrivi_id(ctx, a):
    """Scrive il testo nel campo con quell'id e preme Invio (barra di ricerca, filtro). La pagina che ne risulta,
    nuova o filtrata sul posto, viene letta al passo dopo."""
    n = ctx.d.execute_script(JS_NUMERA)
    els = ctx.d.find_elements(By.CSS_SELECTOR, f"[data-btn-id='{a['id']}']")
    if not els:
        raise ValueError(f"nessun campo con id {a['id']} (la pagina ha {n} elementi numerati)")
    log(f"scrivo '{a['testo']}' nel campo {a['id']} e premo Invio")
    ctx.d.execute_script("arguments[0].scrollIntoView({block:'center'});", els[0])
    time.sleep(0.5)
    try:
        els[0].click()
        els[0].clear()
        els[0].send_keys(a["testo"])
        time.sleep(0.5)
        els[0].send_keys(Keys.ENTER)
    except WebDriverException as e:
        # campo nascosto (per esempio la ricerca dentro un pannello chiuso): scrivo e invio con JavaScript
        log(f"campo non utilizzabile con la tastiera ({type(e).__name__}): scrivo con JavaScript")
        come = ctx.d.execute_script(JS_SCRIVI, els[0], a["testo"])
        log(f"testo inserito e inviato tramite {come}")
    time.sleep(random.uniform(3, 5))
    gestisci_captcha(ctx.d, ctx.url or ctx.d.current_url, ctx.meta)


# tag -> (funzione, {variabile: (tipo, obbligatoria)})
ESEGUIBILI = {
    "view": (az_view, {"url": (str, True)}),
    "wait": (az_wait, {"secondi": ((int, float), True)}),
    "captcha": (az_captcha, {}),
    "scroll": (az_scroll, {}),
    "scroll_fino_a_fine": (az_scroll_fino_a_fine, {"attesa_s": ((int, float), False), "max_cicli": (int, False)}),
    "click": (az_click, {"selettore": (str, True)}),
    "click_testo": (az_click_testo, {"testo": (str, True)}),
    "click_id": (az_click_id, {"id": (str, True)}),
    "scrivi_id": (az_scrivi_id, {"id": (str, True), "testo": (str, True)}),
}


def valida_input():
    return valida_lavoro(os.environ.get("JOB_ID", ""), json.loads(os.environ.get("AZIONI") or "[]"))


def valida_lavoro(job_id, azioni):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", job_id):
        raise ValueError("JOB_ID non valido")
    if not isinstance(azioni, list) or not 1 <= len(azioni) <= MAX_AZIONI:
        raise ValueError(f"AZIONI deve essere una lista di 1-{MAX_AZIONI} azioni")
    for i, a in enumerate(azioni, 1):
        if not isinstance(a, dict) or a.get("tag") not in ESEGUIBILI:
            raise ValueError(f"azione {i}: tag sconosciuto ({a.get('tag') if isinstance(a, dict) else a!r})")
        _, schema = ESEGUIBILI[a["tag"]]
        extra = set(a) - {"tag"} - set(schema)
        if extra:
            raise ValueError(f"azione {i} ({a['tag']}): variabili non ammesse {sorted(extra)}")
        for nome, (tipo, obbligatoria) in schema.items():
            if nome not in a:
                if obbligatoria:
                    raise ValueError(f"azione {i} ({a['tag']}): manca '{nome}'")
            elif not isinstance(a[nome], tipo) or isinstance(a[nome], bool):
                raise ValueError(f"azione {i} ({a['tag']}): '{nome}' ha un tipo sbagliato")
        if a["tag"] == "view":
            p = urlparse(a["url"])
            if p.scheme not in ("http", "https") or not p.netloc:
                raise ValueError(f"azione {i}: view accetta solo indirizzi http o https")
        if a["tag"] == "wait" and not 0 < a["secondi"] <= 60:
            raise ValueError(f"azione {i}: wait accetta da 0 a 60 secondi")
        if a["tag"] == "scrivi_id" and not 1 <= len(a["testo"]) <= 200:
            raise ValueError(f"azione {i}: scrivi_id accetta da 1 a 200 caratteri")
    if azioni[0]["tag"] != "view":
        raise ValueError("la prima azione deve essere 'view'")
    return job_id, azioni


def esegui_su_driver(d, azioni, meta):
    """Esegue le azioni su un Safari gia' aperto e salva pagina e foto in OUT. Alza l'eccezione se qualcosa non va."""
    caps = d.capabilities
    meta["browser"] = {
        "nome": caps.get("browserName"),
        "versione": caps.get("browserVersion"),
        "sistema": caps.get("platformName"),
        "user_agent": d.execute_script("return navigator.userAgent"),
    }
    log(f"browser: {meta['browser']['nome']} {meta['browser']['versione']} su {meta['browser']['sistema']}")

    ctx = Ctx(d, meta)
    for i, a in enumerate(azioni, 1):
        log(f"azione {i}/{len(azioni)}: {json.dumps(a, ensure_ascii=False)}")
        ESEGUIBILI[a["tag"]][0](ctx, a)

    d.execute_script(JS_NUMERA)
    html = d.execute_script("return document.documentElement.outerHTML")
    (OUT / "rendered.html").write_text(html, encoding="utf-8")
    (OUT / "screenshot.png").write_bytes(d.get_screenshot_as_png())
    meta.update({"esito": "ok", "url_finale": d.current_url, "byte_html": len(html.encode("utf-8"))})
    log(f"fatto: {meta['byte_html']} byte di pagina")


def scrivi_esito(meta):
    meta["fine"] = datetime.now(timezone.utc).isoformat()
    (OUT / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    (OUT / "log.txt").write_text("\n".join(LOG_RIGHE) + "\n", encoding="utf-8")


def main():
    OUT.mkdir(exist_ok=True)
    meta = {"avvio": datetime.now(timezone.utc).isoformat(), "esito": "errore", "captcha": None}
    d = None
    try:
        job_id, azioni = valida_input()
        meta.update({"job_id": job_id, "azioni": azioni})

        d = webdriver.Safari()  # Safari vero, comandato da safaridriver
        d.set_page_load_timeout(60)
        esegui_su_driver(d, azioni, meta)
    except Exception as e:
        meta["errore"] = f"{type(e).__name__}: {e}"
        log(f"ERRORE {meta['errore']}")
    finally:
        scrivi_esito(meta)
        if d is not None:
            d.quit()
    sys.exit(0 if meta["esito"] == "ok" else 1)


if __name__ == "__main__":
    main()
