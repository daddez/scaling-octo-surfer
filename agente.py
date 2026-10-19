#!/usr/bin/env python3
"""
Worker SEMPRE ACCESO: un solo Safari, aperto una volta, che esegue i lavori uno dopo l'altro.

Il Mac chiama il server (mai il contrario) all'indirizzo PONTE_URL:
  GET  /agente/prossimo   aspetta fino a 25 s un lavoro; ogni richiesta e' anche un battito
  POST /agente/battito    battito ogni 20 s mentre un lavoro e' in corso
  POST /agente/esito/<id> consegna lo zip del risultato (lo stesso contenuto di render-results/)
  POST /agente/addio      "esco": il server sa che deve rilanciarmi se ci sono altri lavori

Variabili d'ambiente:
  PONTE_URL, PONTE_TOKEN   indirizzo del server e chiave condivisa (segreti del repo)
  MAX_ORE                  durata massima prima di uscire (default 5.75, GitHub chiude i lavori a 6 ore)
  INATTIVO_MIN             minuti senza lavori dopo i quali esce (default 45)
  TWOCAPTCHA_API_KEY       come in render.py
"""
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import requests
from selenium import webdriver
from selenium.common.exceptions import InvalidSessionIdException, WebDriverException

import render

URL = os.environ["PONTE_URL"].rstrip("/")
HEADERS = {"Authorization": f"Bearer {os.environ['PONTE_TOKEN']}"}
MAX_S = float(os.environ.get("MAX_ORE", "5.75")) * 3600
INATTIVO_S = float(os.environ.get("INATTIVO_MIN", "45")) * 60
ATTESA_S = 25
BATTITO_S = 20
SENZA_SERVER_MAX_S = 600   # se il server non risponde per 10 minuti, esce (verra' rilanciato quando serve)

fermo = threading.Event()


def dice(msg):
    print(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {msg}", flush=True)


def chiama(metodo, percorso, **kw):
    intestazioni = {**HEADERS, **kw.pop("headers", {})}
    return requests.request(metodo, URL + percorso, headers=intestazioni, timeout=kw.pop("timeout", 30), **kw)


def battiti():
    while not fermo.wait(BATTITO_S):
        try:
            chiama("POST", "/agente/battito")
        except requests.RequestException:
            pass


def apri_safari():
    d = webdriver.Safari()
    d.set_page_load_timeout(60)
    return d


def pulisci(d):
    """Tra un lavoro e l'altro: nessun cookie e pagina vuota, cosi' i lavori non si influenzano."""
    d.delete_all_cookies()
    d.get("about:blank")


def esegui(d, job_id, azioni, cartella):
    """Esegue UN lavoro e ritorna i byte dello zip. Non alza mai: l'errore finisce in meta.json."""
    render.OUT = Path(cartella)
    render.OUT.mkdir(parents=True, exist_ok=True)
    render.LOG_RIGHE.clear()
    meta = {"avvio": datetime.now(timezone.utc).isoformat(), "esito": "errore", "captcha": None, "job_id": job_id,
            "azioni": azioni, "modo": "agente"}
    sessione_persa = False
    try:
        render.valida_lavoro(job_id, azioni)
        render.esegui_su_driver(d, azioni, meta)
    except Exception as e:
        meta["errore"] = f"{type(e).__name__}: {e}"
        render.log(f"ERRORE {meta['errore']}")
        sessione_persa = isinstance(e, InvalidSessionIdException)
    render.scrivi_esito(meta)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(Path(cartella).iterdir()):
            z.write(f, f.name)
    return buf.getvalue(), meta["esito"], sessione_persa


def consegna(job_id, dati):
    for tentativo in range(1, 4):
        try:
            r = chiama("POST", f"/agente/esito/{job_id}", data=dati, timeout=120,
                       headers={"Content-Type": "application/zip"})
            if r.status_code == 200:
                return True
            dice(f"consegna {job_id}: risposta {r.status_code}")
        except requests.RequestException as e:
            dice(f"consegna {job_id}: tentativo {tentativo} fallito ({type(e).__name__})")
        time.sleep(3 * tentativo)
    return False


def main():
    inizio = time.time()
    ultimo_lavoro = time.time()
    senza_server_da = None
    d = apri_safari()
    dice(f"Safari aperto ({d.capabilities.get('browserVersion')}); server {URL}")
    threading.Thread(target=battiti, daemon=True).start()
    fatti = 0
    motivo = "fine"
    try:
        while True:
            if time.time() - inizio > MAX_S:
                motivo = f"{MAX_S / 3600:.2f} ore di vita"
                break
            if time.time() - ultimo_lavoro > INATTIVO_S:
                motivo = f"{INATTIVO_S / 60:.0f} minuti senza lavori"
                break
            try:
                r = chiama("GET", "/agente/prossimo", params={"attesa": ATTESA_S}, timeout=ATTESA_S + 15)
                senza_server_da = None
            except requests.RequestException as e:
                senza_server_da = senza_server_da or time.time()
                if time.time() - senza_server_da > SENZA_SERVER_MAX_S:
                    motivo = "server irraggiungibile"
                    break
                dice(f"server non raggiungibile ({type(e).__name__}), riprovo")
                time.sleep(10)
                continue
            if r.status_code == 401:
                motivo = "chiave rifiutata dal server"
                break
            if r.status_code != 200:
                if r.status_code != 204:
                    dice(f"risposta inattesa {r.status_code}")
                    time.sleep(5)
                continue
            lavoro = r.json()
            job_id, azioni = lavoro["job_id"], lavoro["azioni"]
            dice(f"lavoro {job_id}: {len(azioni)} azioni")
            cartella = tempfile.mkdtemp(prefix="lavoro-")
            t0 = time.time()
            dati, esito, persa = esegui(d, job_id, azioni, cartella)
            ok = consegna(job_id, dati)
            dice(f"lavoro {job_id}: esito {esito}, {time.time() - t0:.1f} s, consegnato={ok}")
            shutil.rmtree(cartella, ignore_errors=True)
            fatti += 1
            ultimo_lavoro = time.time()
            try:
                if persa:
                    raise InvalidSessionIdException("sessione persa")
                pulisci(d)
            except WebDriverException:
                dice("Safari non risponde: lo riapro")
                try:
                    d.quit()
                except Exception:
                    pass
                d = apri_safari()
    finally:
        fermo.set()
        try:
            chiama("POST", "/agente/addio", json={"motivo": motivo, "lavori": fatti}, timeout=15)
        except requests.RequestException:
            pass
        try:
            d.quit()
        except Exception:
            pass
        dice(f"Esco: {motivo} ({fatti} lavori eseguiti)")


if __name__ == "__main__":
    main()
