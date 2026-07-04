# Tennis Value Monitor

Monitor di value bet sul tennis. Confronta le quote **Pinnacle** (sharp, usato come
riferimento no-vig) con i book soft (**Bet365, Betfair, Snai**) sui mercati Over/Under
games, e invia un alert **Telegram** quando trova un edge positivo superiore alla soglia
configurata.

Questo repository è la versione **standalone**, estratta da Emergent e pronta al deploy
indipendente:

- **Backend** → FastAPI + APScheduler + MongoDB, deploy su **Render**
- **Frontend** → React (Vite) + Tailwind + shadcn/ui, deploy su **Vercel**

```
tennis-monitor/
├── backend/          # FastAPI app (deploy su Render)
│   ├── server.py             # entrypoint FastAPI (uvicorn server:app)
│   ├── monitor.py            # logica di scan / calcolo edge / alert
│   ├── oddspapi_client.py    # client OddsPapi v5 (+ modalità mock)
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
- Una **key OddsPapi v5** valida (altrimenti usa la modalità demo/mock, vedi sotto).
- Un **bot Telegram** (token da @BotFather) e il tuo **chat id**.

> ⚠️ **Sicurezza**: nel file originale erano presenti key OddsPapi e token Telegram in
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

Per provare senza key OddsPapi, imposta `USE_MOCK_DATA="true"` in `backend/.env`
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
   - `ODDSPAPI_KEY`
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

### ⚠️ Nota importante sullo scheduler
Il monitor esegue uno scan automatico ogni `REFRESH_MINUTES` (default 10) tramite un job
in background (APScheduler) **dentro il processo web**. Sul piano **Free** di Render il
servizio va in sleep dopo ~15 minuti di inattività: quando dorme, lo scheduler si ferma e
gli alert automatici non partono finché qualcuno non riapre l'app.

Per uno scan realmente 24/7 hai due opzioni:
1. Usa il piano **Starter** ($7/mese) — nel `render.yaml` è già impostato `plan: starter`.
2. Restando sul free, tieni sveglio il servizio con un ping periodico su `/api/refresh`
   (es. un **Render Cron Job** o un servizio esterno tipo cron-job.org che chiama
   `POST https://<tuo-backend>.onrender.com/api/refresh` ogni 10 minuti).

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
| GET    | `/api/status`        | Stato ultimo scan, prossimo scan, soglia      |
| GET    | `/api/snapshot`      | Ultimo snapshot dei match e value bet         |
| GET    | `/api/alerts`        | Storico alert Telegram                        |
| POST   | `/api/refresh`       | Forza uno scan immediato                       |
| GET/PUT| `/api/settings`      | Legge/aggiorna soglia, book soft, Telegram    |
| POST   | `/api/telegram/test` | Invia un messaggio Telegram di test           |
| POST   | `/api/mock/{bool}`   | Attiva/disattiva la modalità demo             |

---

## 6. Checklist finale

- [ ] MongoDB Atlas creato e connection string in `MONGO_URL` su Render
- [ ] Credenziali OddsPapi e Telegram **rigenerate** e messe come secret su Render
- [ ] Backend Render risponde su `/` e `/api/status`
- [ ] `VITE_BACKEND_URL` impostato su Vercel = URL del backend
- [ ] `CORS_ORIGINS` su Render = URL del frontend Vercel
- [ ] (Se piano free) cron esterno che pinga `/api/refresh` per lo scan 24/7
