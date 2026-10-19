# browser-worker

Esegue **un elenco di azioni su una pagina web con Safari vero** (safaridriver su un Mac di GitHub Actions, non emulato)
e restituisce pagina, screenshot e metadati. Non contiene chiavi.

## Lavoro singolo (`browser-agent`)

- Si lancia con `workflow_dispatch`: `job_id` e `azioni` (lista JSON di azioni, la prima e' `view` con l'indirizzo).
- Il risultato resta 7 giorni come artifact `render-<job_id>`: `rendered.html`, `screenshot.png`, `meta.json`, `log.txt`.
- `meta.json` riporta nome, versione e sistema del browser usato.
- Permessi del workflow: solo `contents: read`. Gli input passano come variabili d'ambiente.
- Azioni ammesse: `view`, `wait`, `captcha`, `scroll`, `scroll_fino_a_fine`, `click` (selettore CSS), `click_testo` (testo visibile del pulsante), `click_id` (id `bN`), `scrivi_id` (scrive nel campo `bN` e preme Invio). Vedi `render.py`.

## Esecuzione continua (`worker-acceso`, `agente.py`)

Un solo Safari che resta acceso e prende i lavori da un indirizzo configurato, uno dopo l'altro.
Esce da solo dopo 5 ore e 45 minuti o dopo 45 minuti senza lavori.

## Segreti del repo

`PONTE_URL`, `PONTE_TOKEN` (esecuzione continua) e `TWOCAPTCHA_API_KEY` (opzionale, per reCAPTCHA v2 e Turnstile).
