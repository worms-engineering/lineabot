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
- **calcio** — sempre via **The Odds API**, limitato ai **top-5 campionati europei
  (Premier League, La Liga, Serie A, Bundesliga, Ligue 1) + Champions/Europa/Conference
  League** (whitelist `FOOTBALL_LEAGUE_KEYS` in `theoddsapi_client.py`, modificabile).

I due provider funzionano **in parallelo, nella stessa scansione**: puoi tenere il tennis su
uno qualsiasi dei due (toggle "provider") mentre basket e calcio restano fissi ciascuno sul
proprio, indipendentemente da quale provider è selezionato per il tennis. Ti servono entrambe
le key (`THE_ODDS_API_KEY` e `ODDSPAPI_KEY`) se vuoi tutti e tre gli sport attivi.

Gli alert indicano sport (🎾/🏀/⚽) e torneo. Ogni sport in più aumenta le chiamate: il basket
di ~`1 + ⌈tornei_in_finestra/5⌉` per scan (OddsPapi), il calcio di ~`2 crediti × leghe attive`
per scan (The Odds API, markets=h2h,totals × regions=eu — leghe fuori stagione non contano).

Questo repository è la versione **standalone**, estratta da Emergent e pronta al deploy
indipendente:

- **Backend** → FastAPI + APScheduler + MongoDB, deploy su **Render**
- **Frontend** → React (Vite) + Tailwind + shadcn/ui, deploy su **Vercel**

```
tennis-monitor/
├── backend/          # FastAPI app (deploy su Render)
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
> d'ambiente segrete su Render — non committarle mai su un repo pubblico.

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

## 3. Deploy del backend su Render

Puoi usare il blueprint incluso (`render.yaml`) oppure configurare a mano.

### Opzione A — Blueprint (consigliata)
1. Fai push del repo su GitHub.
2. Su Render: **New → Blueprint**, seleziona il repo. Render legge `render.yaml`.
3. Imposta le variabili marcate `sync: false` come **secret** nella dashboard:
   - `MONGO_URL` — la connection string di MongoDB Atlas
   - `THE_ODDS_API_KEY` (la legacy `ODDSPAPI_KEY` è accettata come fallback)
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - `CORS_ORIGINS` — l'URL Vercel del frontend (es. `https://your-app.vercel.app`)
4. Deploy.

### Opzione B — Manuale
- **New → Web Service**, connetti il repo.
- Root Directory: `backend`
- Build Command: `pip install -r requirements.txt`
- Start Command: `uvicorn server:app --host 0.0.0.0 --port $PORT`
- Health Check Path: `/`
- Aggiungi le stesse variabili d'ambiente elencate sopra + `DB_NAME=tennis_monitor`.

### ⚠️ Scheduler, toggle di tracciamento e consumo call
Lo scheduler (APScheduler) gira **dentro il processo web** e scansiona ogni `REFRESH_MINUTES`
(default 10). Ogni scansione è gated dal **toggle di tracciamento**:

- **Tracking ON** → scansiona Pinnacle e rileva i cali di quota.
- **Tracking OFF** → lo scan viene saltato, **zero crediti API** (utile es. di notte).

Puoi accendere/spegnere dal pulsante in dashboard o con `POST /api/tracking {"enabled": true|false}`.
`REFRESH_MINUTES = 0` disattiva del tutto lo scheduler (scan solo on-demand via `/api/refresh`).

Il tracciamento richiede osservazioni ravvicinate (confronta la quota con quella dello scan
precedente), quindi conviene tenerlo attivo di continuo:

- **Piano Starter** ($7/mese) — nel `render.yaml` è già `plan: starter`: il servizio non
  dorme e lo scheduler gira 24/7.
- **Piano Free** — il servizio va in sleep dopo ~15 min: per tenerlo sveglio usa un ping
  periodico su `GET /` (health, **0 crediti API**) da un servizio esterno tipo
  cron-job.org ogni ~5-10 min. Lo scanning lo fa lo scheduler interno; il ping serve solo a
  non far addormentare il servizio.

Dopo il deploy, copia l'URL pubblico del backend (es.
`https://tennis-monitor-backend.onrender.com`): ti serve per il frontend.

---

## 4. Deploy del frontend su Vercel

1. Su Vercel: **Add New → Project**, importa lo stesso repo GitHub.
2. **Root Directory**: `frontend`
3. Framework Preset: **Vite** (auto-rilevato). Build `npm run build`, output `dist`.
4. **Environment Variables** → aggiungi:
   - `VITE_BACKEND_URL` = l'URL del backend Render (senza slash finale)
5. Deploy.

Dopo il deploy, torna su Render e imposta `CORS_ORIGINS` con l'URL definitivo di Vercel
(es. `https://tennis-monitor.vercel.app`), così il browser non blocca le chiamate.
Se hai anche un dominio custom, puoi mettere più origin separati da virgola.

---

## 5. Endpoint API principali

| Metodo | Path                 | Descrizione                                   |
|--------|----------------------|-----------------------------------------------|
| GET    | `/`                  | Health check (usato da Render)                |
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

- [ ] MongoDB Atlas creato e connection string in `MONGO_URL` su Render
- [ ] Credenziali The Odds API e Telegram **rigenerate** e messe come secret su Render
- [ ] Backend Render risponde su `/` e `/api/status`
- [ ] `VITE_BACKEND_URL` impostato su Vercel = URL del backend
- [ ] `CORS_ORIGINS` su Render = URL del frontend Vercel
- [ ] `REFRESH_MINUTES` impostato (es. 3-5) e, sul piano Free, cron esterno su `GET /` per
      tenere sveglio il servizio (0 crediti API)
- [ ] Tracciamento acceso/spento dal pulsante in dashboard (spegnilo per non consumare call)
