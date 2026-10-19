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

Azioni ammesse (tag): view, wait, captcha, scroll, scroll_fino_a_fine, click.
Il catalogo con codice, variabili ed esempi sta nella tabella azioni_worker_browser del server.
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
from selenium.webdriver.common.by import By

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


# tag -> (funzione, {variabile: (tipo, obbligatoria)})
ESEGUIBILI = {
    "view": (az_view, {"url": (str, True)}),
    "wait": (az_wait, {"secondi": ((int, float), True)}),
    "captcha": (az_captcha, {}),
    "scroll": (az_scroll, {}),
    "scroll_fino_a_fine": (az_scroll_fino_a_fine, {"attesa_s": ((int, float), False), "max_cicli": (int, False)}),
    "click": (az_click, {"selettore": (str, True)}),
}


def valida_input():
    job_id = os.environ.get("JOB_ID", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", job_id):
        raise ValueError("JOB_ID non valido")
    azioni = json.loads(os.environ.get("AZIONI") or "[]")
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
    if azioni[0]["tag"] != "view":
        raise ValueError("la prima azione deve essere 'view'")
    return job_id, azioni


def main():
    OUT.mkdir(exist_ok=True)
    meta = {"avvio": datetime.now(timezone.utc).isoformat(), "esito": "errore", "captcha": None}
    d = None
    try:
        job_id, azioni = valida_input()
        meta.update({"job_id": job_id, "azioni": azioni})

        d = webdriver.Safari()  # Safari vero, comandato da safaridriver
        d.set_page_load_timeout(60)
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

        html = d.execute_script("return document.documentElement.outerHTML")
        (OUT / "rendered.html").write_text(html, encoding="utf-8")
        (OUT / "screenshot.png").write_bytes(d.get_screenshot_as_png())
        meta.update({"esito": "ok", "url_finale": d.current_url, "byte_html": len(html.encode("utf-8"))})
        log(f"fatto: {meta['byte_html']} byte di pagina")
    except Exception as e:
        meta["errore"] = f"{type(e).__name__}: {e}"
        log(f"ERRORE {meta['errore']}")
    finally:
        meta["fine"] = datetime.now(timezone.utc).isoformat()
        (OUT / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        (OUT / "log.txt").write_text("\n".join(LOG_RIGHE) + "\n", encoding="utf-8")
        if d is not None:
            d.quit()
    sys.exit(0 if meta["esito"] == "ok" else 1)


if __name__ == "__main__":
    main()
