#!/usr/bin/env python3
"""
Apre UNA pagina con Safari vero (safaridriver su macOS, non emulato) e salva:
  render-results/rendered.html   codice della pagina dopo il caricamento
  render-results/screenshot.png  foto della pagina
  render-results/meta.json       browser usato, esito, captcha, tempi

Input (variabili d'ambiente):
  URL                    indirizzo http/https da aprire
  JOB_ID                 ID del lavoro (lettere, numeri, - e _)
  AZIONI                 lista JSON di selettori CSS da cliccare dopo il caricamento
  TWOCAPTCHA_API_KEY     opzionale: chiave 2Captcha per reCAPTCHA v2 e Turnstile
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
MAX_AZIONI = 5

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


def log(msg):
    print(f"[render] {msg}", flush=True)


def valida_input():
    url, job_id = os.environ.get("URL", ""), os.environ.get("JOB_ID", "")
    if urlparse(url).scheme not in ("http", "https") or not urlparse(url).netloc:
        raise ValueError("URL deve essere un indirizzo http o https")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", job_id):
        raise ValueError("JOB_ID non valido")
    azioni = json.loads(os.environ.get("AZIONI") or "[]")
    if not isinstance(azioni, list) or len(azioni) > MAX_AZIONI or not all(isinstance(a, str) for a in azioni):
        raise ValueError(f"AZIONI deve essere una lista di al massimo {MAX_AZIONI} selettori CSS")
    return url, job_id, azioni


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


def main():
    OUT.mkdir(exist_ok=True)
    meta = {"avvio": datetime.now(timezone.utc).isoformat(), "esito": "errore", "captcha": None}
    d = None
    try:
        url, job_id, azioni = valida_input()
        meta.update({"url": url, "job_id": job_id, "azioni": azioni})

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

        log("apro la pagina")
        try:
            d.get(url)
        except Exception as e:  # tempo scaduto di caricamento: leggo quello che c'e'
            log(f"caricamento non completo: {type(e).__name__}")
            meta["caricamento_incompleto"] = True
        time.sleep(random.uniform(3, 6))
        gestisci_captcha(d, url, meta)
        scroll_umano(d)

        for selettore in azioni:
            log(f"click su {selettore}")
            d.find_element(By.CSS_SELECTOR, selettore).click()
            time.sleep(random.uniform(1.5, 3))
        if azioni:
            gestisci_captcha(d, url, meta)
            scroll_umano(d)

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
        if d is not None:
            d.quit()
    sys.exit(0 if meta["esito"] == "ok" else 1)


if __name__ == "__main__":
    main()
