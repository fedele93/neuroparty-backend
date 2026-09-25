# NeuroParty Backend

Backend condiviso per **NeuroParty**, l'app della festa di specializzazione in Neurologia
(repo dell'app Android + PWA: [fedele93/spec2026app](https://github.com/fedele93/spec2026app)).

Senza backend ogni telefono aveva i propri dati: un RSVP o una prenotazione della navetta
restavano sul dispositivo di chi li inseriva e le "notifiche push" arrivavano solo a chi le
inviava. Con questo server tutti gli invitati vedono gli stessi dati e le notifiche
arrivano davvero a tutti.

## Cosa fa

| Funzione | Endpoint |
|---|---|
| Dati dell'evento (programma, mappa, navetta, laureandi) | `GET /api/event` |
| Invitati & RSVP | `GET/POST /api/guests`, `PUT/DELETE /api/guests/{id}` |
| Navetta (con controllo dei 54 posti) | `GET /api/bus/summary`, `GET/POST /api/bus/bookings`, `DELETE /api/bus/bookings/{id}` |
| Bacheca auguri | `GET/POST /api/wishes`, `POST /api/wishes/{id}/heart` |
| Galleria foto (upload, ridimensionamento a 1600 px) | `GET/POST /api/photos`, `POST /api/photos/{id}/like` |
| Regali e quote | `GET /api/gifts/targets`, `GET/POST /api/gifts/contributions` |
| Notifiche (cronologia + **Web Push** a tutti i dispositivi) | `GET /api/notifications`, `POST /api/notifications` (solo organizzatori) |
| Sottoscrizione push del browser | `GET /api/push/vapid-public-key`, `POST /api/push/subscribe` |
| Sincronizzazione app Android in una chiamata | `GET /api/snapshot`, `GET /api/state` |

Documentazione interattiva (Swagger) su `https://<tuo-dominio>/docs`.

**Permessi** (senza account, per semplicità):
- ogni dispositivo si presenta con un `X-Client-Id` casuale: chi inserisce un invitato o una
  prenotazione può modificarlo o cancellarlo;
- gli organizzatori usano `X-Admin-Token` (valore di `ADMIN_TOKEN` nel `.env`): possono inviare
  notifiche push e cancellare qualsiasi record;
- cambiare lo stato RSVP di un invitato è permesso a tutti.

## Stack

- Python 3.12, [FastAPI](https://fastapi.tiangolo.com/), SQLAlchemy, SQLite (file su volume)
- [pywebpush](https://github.com/web-push-libs/pywebpush) per le notifiche Web Push (chiavi VAPID generate al primo avvio)
- Pillow per ridimensionare le foto
- Docker Compose + [Caddy](https://caddyserver.com/) (HTTPS automatico con Let's Encrypt)

## Deploy su Ubuntu con Docker e sottodominio

Prerequisiti: un server Ubuntu 22.04/24.04 raggiungibile da Internet (porte 80 e 443 aperte)
e un sottodominio del tuo dominio, es. `neurospec.peukeia.eu`.

### 1. DNS

Nel pannello del tuo dominio crea un record **A** (e **AAAA** se hai IPv6):

```
neurospec.peukeia.eu  ->  <IP pubblico del server>
```

### 2. Installazione (script automatico)

```bash
curl -fsSL https://raw.githubusercontent.com/fedele93/neuroparty-backend/main/scripts/install-ubuntu.sh | bash
```

Lo script installa Docker, clona i due repository in `~/neuroparty/` e crea un `.env` con un
`ADMIN_TOKEN` casuale. Se hai appena installato Docker esci e rientra nella shell
(o esegui `newgrp docker`).

Oppure manualmente:

```bash
curl -fsSL https://get.docker.com | sh && sudo usermod -aG docker $USER
mkdir -p ~/neuroparty && cd ~/neuroparty
git clone https://github.com/fedele93/spec2026app.git
git clone https://github.com/fedele93/neuroparty-backend.git
cd neuroparty-backend && cp .env.example .env
```

### 3. Configura `.env`

```bash
nano .env
```

| Variabile | Valore |
|---|---|
| `DOMAIN` | `neurospec.peukeia.eu` |
| `ACME_EMAIL` | la tua email (avvisi Let's Encrypt) |
| `PUBLIC_URL` | `https://neurospec.peukeia.eu` |
| `ADMIN_TOKEN` | stringa segreta lunga (`openssl rand -hex 24`) |
| `VAPID_SUBJECT` | `mailto:fedeleluisi@gmail.com` |
| `SEED_DEMO_DATA` | `false` in produzione (`true` solo per provare con dati finti) |
| `PWA_DIR` | `../spec2026app/pwa` (cartella della PWA da servire) |

### 4. Avvio

```bash
docker compose up -d --build
docker compose logs -f        # Ctrl+C per uscire
```

Caddy richiede il certificato HTTPS in automatico. Dopo circa un minuto:

- `https://neurospec.peukeia.eu/` -> la PWA (installabile su iPhone da Safari con
  "Aggiungi alla schermata Home", su Android da Chrome con "Installa app")
- `https://neurospec.peukeia.eu/api/health` -> `{"status":"ok"}`
- `https://neurospec.peukeia.eu/docs` -> documentazione API

### 5. Collega le app

- **PWA**: nessuna configurazione, usa lo stesso dominio.
- **App Android**: nel repo `spec2026app` imposta `API_BASE_URL=https://neurospec.peukeia.eu`
  nel file `.env` prima di compilare (vedi il README dell'app).
- **Organizzatori**: nella PWA apri ⚙️ Impostazioni e inserisci l'`ADMIN_TOKEN`; nell'app
  Android mettilo in `.env` (`ADMIN_TOKEN=...`) prima della build.

### Aggiornare

```bash
cd ~/neuroparty/neuroparty-backend && ./scripts/update.sh
```

### Backup

Tutto lo stato è nella cartella `data/` (database SQLite, foto in `data/uploads/`, chiavi
VAPID). Basta copiarla:

```bash
tar czf backup-neuroparty-$(date +%F).tgz data/
```

### Inviare una notifica dal terminale

```bash
./scripts/send-notification.sh "🚌 Partenza navetta" "Partiamo tra 15 minuti dal Piazzale" Navetta
```

## Dati dell'evento

`seed/event-data.json` contiene programma, mappa, orari navetta, laureandi e la lista dei
regali (con IBAN, link Satispay/PayPal). Modificalo e riavvia (`docker compose restart api`):
la PWA legge questi dati dal server, quindi date e luoghi si aggiornano senza ricompilare
nulla. Tieni allineata la copia in `spec2026app/pwa/shared/event-data.json`, usata come
fallback offline e per generare i dati dell'app Android.

I regali (`giftTargets`) vivono invece nel database: al riavvio il server aggiunge quelli
presenti nel JSON ma non ancora nel database (per esempio un neo-specialista aggiunto dopo il
primo avvio) senza toccare le quote già raccolte; per cambiare testi o IBAN di un regalo già
esistente va modificato il record nel database (o si riparte da una cartella `data/` vuota).

> Attenzione: gli IBAN e i link di pagamento nel file di esempio sono segnaposto.

## Sviluppo locale

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest                                   # test API
ADMIN_TOKEN=test SEED_DEMO_DATA=true PWA_DIR=../spec2026app/pwa uvicorn app.main:app --reload
# API + PWA su http://127.0.0.1:8000
```

Oppure con Docker senza HTTPS: `docker compose -f docker-compose.dev.yml up --build`.

## Struttura

```
app/
  main.py          crea l'app FastAPI, seed iniziale, file statici
  config.py        variabili d'ambiente
  models.py        tabelle (stesse entità dell'app Android)
  schemas.py       validazione input
  auth.py          X-Admin-Token / X-Client-Id
  push.py          Web Push (VAPID)
  routers/         un file per area: guests, bus, wishes, photos, gifts, notifications, push, event
seed/event-data.json
tests/             pytest (14 test)
Dockerfile, docker-compose.yml, Caddyfile
scripts/           install-ubuntu.sh, update.sh, send-notification.sh
```

## Limiti noti

- Nessuna autenticazione per gli invitati: chiunque conosca l'URL può inserire dati.
  L'app è pensata per una cerchia ristretta; non pubblicare il link in chiaro.
- Le notifiche push Web richiedono, su iPhone, iOS 16.4+ e la PWA installata sulla Home.
- L'app Android (senza Google Play Services, per F-Droid) riceve le notifiche con un
  controllo periodico, non in tempo reale a app chiusa.

## Licenza

MIT — Fedele Luisi, 2026.
