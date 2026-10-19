# selezionatore-bandi-worker

Apre **una pagina per volta con Safari vero** (safaridriver su un Mac di GitHub Actions, non emulato)
e restituisce pagina, screenshot e metadati. Non contiene chiavi, prompt né modelli di AI.

- Il server del Selezionatore Bandi lancia `browser-agent` (`workflow_dispatch`: `url`, `job_id`, `azioni`).
- Il risultato resta 7 giorni come artifact `render-<job_id>`: `rendered.html`, `screenshot.png`, `meta.json`.
- `meta.json` riporta nome, versione e sistema del browser usato.
- L'unico segreto del repo e' `TWOCAPTCHA_API_KEY` (opzionale, per reCAPTCHA v2 e Turnstile).
- Permessi del workflow: solo `contents: read`. Gli input passano come variabili d'ambiente.
