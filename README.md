# Pinnacle Drop Monitor (Tennis)

Monitora i **cali di quota su Pinnacle** (sharp) per i mercati **Match Winner (H2H)** e
**Total Games** dei match tennis in partenza nei prossimi 60 minuti. Quando una quota cala
di più della soglia configurata (default 5%) tra due scansioni, invia un alert **Telegram**:
è un segnale di *steam* (denaro sharp entrato), e i soft book — specie quelli italiani —
seguono in ritardo, lasciandoti il tempo di agire.

Interroga **solo Pinnacle**, quindi ogni scan costa pochissime chiamate. Il tracciamento si
accende/spegne da un pulsante in dashboard (o via `POST /api/tracking`).

**Provider selezionabile** dalla dashboard (o via `PUT /api/settings {"provider": ...}`):
- **The Odds API** — solo tornei principali (Slam, Masters), dati puliti, quota a crediti.
- **OddsPapi** — calendario completo incl. Challenger/ITF.

Ognuno usa la sua key (`THE_ODDS_API_KEY` / `ODDSPAPI_KEY`); imposta quella dei provider che usi.

**Multi-sport**: oltre al tennis, due toggle in dashboard abilitano rispettivamente:
- **basket** — sempre via **OddsPapi**, limitato a **NBA (incl. Summer League), WNBA ed
  EuroBasket** (whitelist in `BASKETBALL_WHITELIST`, modificabile);
- **calcio** — provider **switchabile indipendentemente dal tennis** (pulsante dedicato in
  dashboard, o via `PUT /api/settings {"football_provider": ...}`), limitato ai **top-5
  campionati europei + Champions/Europa/Conference League**:
  - **OddsPapi** — calendario completo, include anche i **turni preliminari** delle coppe
    UEFA (whitelist `FOOTBALL_WHITELIST_ODDSPAPI` in `monitor.py`: match esatto sul nome
    torneo per i campionati domestici — così non prende le serie B/2. Bundesliga — e match
    a sottostringa per le coppe UEFA, per includere anche le varianti "Qualification");
  - **The Odds API** — solo fase a gironi/eliminazione in poi in genere, dati puliti
    (whitelist `FOOTBALL_LEAGUE_KEYS` in `theoddsapi_client.py`).

  Utile passare da OddsPapi (preliminari, luglio-agosto) a The Odds API una volta partita
  la fase a gironi.

I provider funzionano **in parallelo, nella stessa scansione**: tennis, basket e calcio
usano ciascuno il proprio provider selezionato, indipendentemente l'uno dall'altro. Ti
servono entrambe le key (`THE_ODDS_API_KEY` e `ODDSPAPI_KEY`) se vuoi usarli entrambi.

Gli alert indicano sport (🎾/🏀/⚽) e torneo. Ogni sport in più aumenta le chiamate: il basket
di ~`1 + ⌈tornei_in_finestra/5⌉` per scan (OddsPapi); il calcio, se su The Odds API, di
~`2 crediti × leghe attive` per scan (markets=h2h,totals × regions=eu — leghe fuori
stagione non contano), se su OddsPapi segue lo stesso schema del basket.

Questo repository è la versione **standalone**, estratta da Emergent e pronta al deploy
indipendente:

- **Backend** → FastAPI + APScheduler + MongoDB, deploy su **Google Cloud Run**
- **Frontend** → React (Vite) + Tailwind + shadcn/ui, deploy su **Vercel**

```
tennis-monitor/
├── backend/          # FastAPI app (deploy su Cloud Run)
│   ├── server.py             # entrypoint FastAPI (uvicorn server:app)
│   ├── monitor.py            # logica di scan / rilevamento cali di quota / alert
│   ├── theoddsapi_client.py  # client The Odds API (+ modalità mock)
│   ├── telegram_client.py    # invio messaggi Telegram
│   ├── mock_data.py          # dati demo quando non c'è una key valida
│   ├── requirements.txt
│   ├── Procfile
│   └── .env.example
├── frontend/         # React + Vite (deploy su Vercel)
│   ├── src/
│   ├── package.json
│   ├── vite.config.js
│   ├── vercel.json
│   └── .env.example
└── render.yaml       # blueprint Render per il backend
```

---

## 1. Prerequisiti

- Un database **MongoDB**. In locale va bene `mongodb://localhost:27017`; in produzione
  usa **MongoDB Atlas** (free tier M0) e prendi la connection string `mongodb+srv://...`.
- Una **key The Odds API** valida (the-odds-api.com; altrimenti usa la modalità demo/mock).
- Un **bot Telegram** (token da @BotFather) e il tuo **chat id**.

> ⚠️ **Sicurezza**: nel file originale erano presenti la key API e i token Telegram in
> chiaro. Sono stati riportati in `backend/.env` solo per lo sviluppo locale. **Rigenera /
> ruota queste credenziali** prima di andare in produzione e impostale come variabili
> d'ambiente segrete su Cloud Run (o Secret Manager) — non committarle mai su un repo pubblico.

---

## 2. Sviluppo locale

### Backend
```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# assicurati che MongoDB sia in esecuzione, poi:
uvicorn server:app --reload --port 8000
```
API su `http://localhost:8000`, docs su `http://localhost:8000/docs`.

Per provare senza key, imposta `USE_MOCK_DATA="true"` in `backend/.env`
(oppure usa il bottone **Enable demo data** nella UI).

### Frontend
```bash
cd frontend
npm install
# frontend/.env deve contenere: VITE_BACKEND_URL=http://localhost:8000
npm run dev
```
UI su `http://localhost:3000`.

---

## 3. Deploy del backend su Google Cloud Run

Il backend gira come container: l'immagine è costruita dal `Dockerfile` in root, che ascolta
su `$PORT` (Cloud Run lo inietta a runtime). Puoi deployare direttamente da sorgente con un
solo comando `gcloud`, che builda e pubblica in automatico.

### Deploy

```bash
gcloud run deploy lineabot-backend \
  --source . \
  --region europe-west1 \
  --allow-unauthenticated \
  --min-instances 1 \
  --no-cpu-throttling \
  --set-env-vars "DB_NAME=tennis_monitor,REFRESH_MINUTES=10,USE_MOCK_DATA=false" \
  --set-env-vars "MONGO_URL=...,THE_ODDS_API_KEY=...,ODDSPAPI_KEY=..." \
  --set-env-vars "TELEGRAM_BOT_TOKEN=...,TELEGRAM_CHAT_ID=...,CORS_ORIGINS=https://your-app.vercel.app"
```

> Per i valori sensibili conviene usare **Secret Manager** (`--set-secrets` invece di
> `--set-env-vars`), così le key non restano in chiaro nella config del servizio.

Variabili d'ambiente (le stesse di `backend/.env.example`):
- `MONGO_URL` — connection string MongoDB Atlas (`mongodb+srv://...`)
- `DB_NAME` — es. `tennis_monitor`
- `THE_ODDS_API_KEY` (la legacy `ODDSPAPI_KEY` è accettata come fallback)
- `ODDSPAPI_KEY`
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- `CORS_ORIGINS` — l'URL Vercel del frontend (es. `https://your-app.vercel.app`)
- `REFRESH_MINUTES` — minuti tra gli scan automatici (default 10)

### ⚠️ CPU sempre allocata e min-instances (fondamentale su Cloud Run)
Lo scheduler (APScheduler) gira **dentro il processo web**, non come job separato. Cloud Run
di default **alloca la CPU solo durante le richieste** e scala a **zero** quando non ne
arrivano: in quel caso lo scheduler interno viene congelato e **gli scan si fermano** senza
alcun errore. Perché il monitoraggio giri 24/7 servono entrambe le opzioni:

- **`--min-instances 1`** — tiene almeno un'istanza sempre viva (niente scale-to-zero);
- **`--no-cpu-throttling`** (CPU always allocated) — la CPU resta assegnata anche fuori
  dalle richieste, così i timer di APScheduler scattano davvero.

> Il ping periodico su `GET /` (stile cron-job.org) **non basta** su Cloud Run: sveglia
> l'istanza a ogni ping, ma tra un ping e l'altro la CPU resta strozzata e lo scheduler non
> avanza. Usalo al più come rete di sicurezza, non al posto di `--no-cpu-throttling`.

### Scheduler, toggle di tracciamento e consumo call
Ogni scansione è gated dal **toggle di tracciamento**:

- **Tracking ON** → scansiona Pinnacle e rileva i cali di quota.
- **Tracking OFF** → lo scan viene saltato, **zero crediti API** (utile es. di notte).

Puoi accendere/spegnere dal pulsante in dashboard o con `POST /api/tracking {"enabled": true|false}`.
`REFRESH_MINUTES = 0` disattiva lo scheduler dello scan principale (resta on-demand via
`/api/refresh`); il fast-loop dei prediction market (F1 e MLB, keyless e gratuito) continua
comunque a girare ogni `F1_REFRESH_SECONDS`.

Il tracciamento richiede osservazioni ravvicinate (confronta la quota con quella dello scan
precedente), quindi conviene tenerlo attivo di continuo.

Dopo il deploy, copia l'URL pubblico del servizio (es.
`https://lineabot-backend-xxxxxxxx-ew.a.run.app`): ti serve per il frontend.

### Alternativa: Render
Il repo include anche `render.yaml` (blueprint Render) e un `Procfile`: se preferisci Render,
**New → Blueprint** sul repo e imposta gli stessi secret. Nota che sul piano Free il servizio
dorme dopo ~15 min, quindi lì il ping esterno su `GET /` serve davvero a tenerlo sveglio.

---

## 4. Deploy del frontend su Vercel

1. Su Vercel: **Add New → Project**, importa lo stesso repo GitHub.
2. **Root Directory**: `frontend`
3. Framework Preset: **Vite** (auto-rilevato). Build `npm run build`, output `dist`.
4. **Environment Variables** → aggiungi:
   - `VITE_BACKEND_URL` = l'URL del backend Cloud Run (senza slash finale)
5. Deploy.

Dopo il deploy, torna su Cloud Run e imposta `CORS_ORIGINS` con l'URL definitivo di Vercel
(es. `https://tennis-monitor.vercel.app`), così il browser non blocca le chiamate.
Se hai anche un dominio custom, puoi mettere più origin separati da virgola.

---

## 5. Endpoint API principali

| Metodo | Path                 | Descrizione                                   |
|--------|----------------------|-----------------------------------------------|
| GET    | `/`                  | Health check (usato da Cloud Run)             |
| GET    | `/api/status`        | Stato scan, prossimo scan, soglia, tracking on/off |
| GET    | `/api/snapshot`      | Ultimo snapshot: match e quote tracciate      |
| GET    | `/api/alerts`        | Storico alert (cali di quota)                 |
| POST   | `/api/refresh`       | Forza uno scan immediato (anche se tracking off) |
| POST   | `/api/tracking`      | Attiva/disattiva il tracciamento `{"enabled": bool}` |
| GET/PUT| `/api/settings`      | Legge/aggiorna soglia drop, tracking, Telegram |
| POST   | `/api/telegram/test` | Invia un messaggio Telegram di test           |
| POST   | `/api/mock/{bool}`   | Attiva/disattiva la modalità demo             |

---

## 6. Checklist finale

- [ ] MongoDB Atlas creato e connection string in `MONGO_URL` su Cloud Run
- [ ] Credenziali The Odds API e Telegram **rigenerate** e messe come secret su Cloud Run (Secret Manager)
- [ ] Backend Cloud Run risponde su `/` e `/api/status`
- [ ] `VITE_BACKEND_URL` impostato su Vercel = URL del backend
- [ ] `CORS_ORIGINS` su Cloud Run = URL del frontend Vercel
- [ ] Servizio Cloud Run con `--min-instances 1` **e** `--no-cpu-throttling`, così lo
      scheduler interno gira 24/7 (senza, gli scan si fermano quando l'istanza è idle)
- [ ] `REFRESH_MINUTES` impostato (es. 3-5)
- [ ] Tracciamento acceso/spento dal pulsante in dashboard (spegnilo per non consumare call)
