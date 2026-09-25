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
| Dati dell'evento (programma, mappa, navetta, laureandi, orari) | `GET /api/event` |
| Calendario (seduta + festa) da aggiungere al telefono | `GET /api/event/calendar.ics` |
| Invitati & RSVP | `GET/POST /api/guests`, `PUT/DELETE /api/guests/{id}` |
| Riepilogo per il catering (coperti, categorie, menu speciali) | `GET /api/guests/summary` |
| Navetta (con controllo dei 54 posti) | `GET /api/bus/summary`, `GET/POST /api/bus/bookings`, `DELETE /api/bus/bookings/{id}` |
| Bacheca auguri | `GET/POST /api/wishes`, `POST /api/wishes/{id}/heart` |
| Galleria foto (upload, ridimensionamento a 1600 px) | `GET/POST /api/photos`, `POST /api/photos/{id}/like` |
| Regali e quote | `GET /api/gifts/targets`, `GET/POST /api/gifts/contributions` |
| Notifiche (cronologia + **Web Push** a tutti i dispositivi) | `GET /api/notifications`, `POST /api/notifications` (solo organizzatori) |
| Notifiche programmate (`sendAt` nel POST), elenco e annullamento | `GET /api/notifications/scheduled`, `DELETE /api/notifications/{id}` (solo organizzatori) |
| Export CSV per ristorante e autista | `GET /api/export/guests.csv`, `GET /api/export/bus.csv` (solo organizzatori) |
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
| `GIFT_SYNC_UPDATE_TEXTS` | `true` per aggiornare testi/IBAN/obiettivo dei regali già in database dal JSON al riavvio (quote raccolte intatte); di default `false` |
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

### Backup automatico

`scripts/backup.sh` crea `backups/backup-neuroparty-<data>.tgz` con una copia consistente del
database (API di backup di SQLite, anche a server acceso), le foto e le chiavi VAPID, e
cancella i backup più vecchi di 7 giorni (`KEEP_DAYS`). Per farlo girare ogni notte alle 03:15:

```bash
./scripts/install-backup-cron.sh        # aggiunge la riga al crontab (idempotente)
./scripts/install-backup-cron.sh --remove
```

Copia periodicamente la cartella `backups/` fuori dal server (es. `scp` o rclone).

### Esportare invitati e navetta (CSV)

Dalla PWA, con il token organizzatore, in ⚙️ Impostazioni ci sono i pulsanti **Esporta
invitati** e **Esporta navetta**. Da terminale:

```bash
curl -H "X-Admin-Token: $ADMIN_TOKEN" https://neurospec.peukeia.eu/api/export/guests.csv -o invitati.csv
curl -H "X-Admin-Token: $ADMIN_TOKEN" https://neurospec.peukeia.eu/api/export/bus.csv -o navetta.csv
```

Separatore `;` e BOM UTF-8: Excel italiano li apre con un doppio clic. `navetta.csv`
termina con la riga del totale posti. `GET /api/guests/summary` restituisce gli stessi numeri
per il catering in JSON (coperti confermati, per categoria, esigenze alimentari con i nomi).

### Notifiche programmate

`POST /api/notifications` accetta `sendAt` (timestamp in millisecondi): se è nel futuro la
notifica resta in attesa, invisibile agli invitati, e viene pubblicata e inviata in push
all'ora indicata da un controllo in background (ogni `SCHEDULER_INTERVAL_S` secondi, default
20). Gli organizzatori la vedono in `GET /api/notifications/scheduled` e possono annullarla con
`DELETE /api/notifications/{id}`. Nella PWA: campo **Programma l'invio** nel dialogo di invio.

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

### Orari ancora da decidere

Il blocco `schedule` del JSON contiene le date (`ceremonyDate`, `partyDate`) e gli orari
(`ceremonyTime`, `partyTime`, `busDepartureTime`, `busReturnTime`, formato `HH:MM`, vuoto =
da definire). Nei testi si usano segnaposto risolti dal server (e dalla PWA/app in locale):

| Sintassi | Risultato |
|---|---|
| `{partyTime}` | l'orario, oppure `da definire` |
| `{partyTime\|ora da definire}` | l'orario, oppure il testo dopo la barra |
| `{partyTime\|Inizio ore $.\|Orario da confermare.}` | con orario: il 2° pezzo con `$` sostituito; senza: il 3° pezzo |

Quando decidi l'orario basta compilare `schedule.partyTime` (es. `"20:30"`) e riavviare:
timeline, mappa, navetta e il file calendario si aggiornano da soli.

### Regali

I regali (`giftTargets`) vivono invece nel database: al riavvio il server aggiunge quelli
presenti nel JSON ma non ancora nel database (per esempio un neo-specialista aggiunto dopo il
primo avvio) senza toccare le quote già raccolte. Per cambiare titolo, descrizione, IBAN, link
o obiettivo di un regalo già esistente modifica il JSON e riavvia con
`GIFT_SYNC_UPDATE_TEXTS=true` nel `.env` (le quote raccolte non vengono mai modificate); i
client ricaricano i dati da soli perché la versione viene incrementata.

> Attenzione: gli IBAN e i link di pagamento nel file di esempio sono segnaposto. All'avvio in
> produzione (`SEED_DEMO_DATA=false`) il server scrive un avviso nel log per ogni IBAN che non
> supera il controllo di validità; nel repo dell'app `python3 tools/check-event-data.py --strict`
> fa lo stesso controllo e blocca la release finché restano IBAN finti.

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
  schedule.py      orari (blocco "schedule"), segnaposto nei testi, calendario .ics
  scheduler.py     pubblicazione delle notifiche programmate (ciclo in background)
  validation.py    controllo IBAN (segnalazione dei segnaposto)
  routers/         un file per area: guests, bus, wishes, photos, gifts, notifications, push, event, export
seed/event-data.json
tests/             pytest (22 test)
Dockerfile, docker-compose.yml, Caddyfile
scripts/           install-ubuntu.sh, update.sh, send-notification.sh, backup.sh, install-backup-cron.sh
```

## Limiti noti

- Nessuna autenticazione per gli invitati: chiunque conosca l'URL può inserire dati.
  L'app è pensata per una cerchia ristretta; non pubblicare il link in chiaro.
- Le notifiche push Web richiedono, su iPhone, iOS 16.4+ e la PWA installata sulla Home.
- L'app Android (senza Google Play Services, per F-Droid) riceve le notifiche con un
  controllo periodico, non in tempo reale a app chiusa.

## Licenza

MIT — Fedele Luisi, 2026.
