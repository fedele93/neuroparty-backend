# Workflow n8n di esempio

Due workflow pronti da importare nel tuo n8n self-hosted (menu **⋯ → Import from File**).
Dopo l'import vanno solo compilati i valori segnati come segnaposto e scelte le credenziali
SMTP (o il canale che preferisci: Telegram, Signal, Google Sheets...).

Come funziona il collegamento, in due direzioni:

```
n8n  --GET /api/automation/report.html (X-Automation-Token)-->  backend     (n8n "tira" i dati)
backend  --POST evento JSON (X-Automation-Secret)-->  n8n nodo Webhook      (il backend "spinge" gli eventi)
```

## 1. `report-giornaliero-mail.json` — report ogni mattina

`Schedule Trigger → Impostazioni → HTTP Request → Send Email`

Ogni giorno alle 8:00 (ora italiana) scarica `GET /api/automation/report.html` e lo spedisce
via mail: coperti confermati e menu speciali, posti navetta per fermata, quote regalo,
novità delle ultime 24 ore, notifiche programmate.

Da compilare nel nodo **Impostazioni**:

| Campo | Valore |
|---|---|
| `baseUrl` | `https://neurospec.peukeia.eu` — oppure `http://neuroparty-api:8000` se n8n e il backend sono sulla stessa rete Docker (`proxy_network`): la chiamata resta interna al server |
| `automationToken` | il valore di `AUTOMATION_TOKEN` nel `.env` del backend (o, in mancanza, `ADMIN_TOKEN`) |
| `destinatari` | uno o più indirizzi separati da virgola |
| `oreNovita` | ampiezza della sezione "Novità" (ore); 24 per un report giornaliero, 168 per uno settimanale |

Nel nodo **Invia mail** imposta il mittente e seleziona le credenziali SMTP. Per cambiare
orario modifica la cron `0 8 * * *` nel trigger (es. `0 20 * * 0` = domenica alle 20).

Varianti veloci:
- **testo semplice** (Telegram, WhatsApp via API, Signal): usa `/api/automation/report.txt`;
- **dati grezzi** per costruire la tua mail o un foglio Google: `/api/automation/report`
  (JSON, senza `responseFormat: text`);
- **CSV in allegato**: aggiungi un secondo HTTP Request verso `/api/export/guests.csv` con
  `Response Format = File` e passa il binario al nodo Send Email (campo *Attachments*).

## 2. `avvisi-in-tempo-reale.json` — una mail per ogni evento importante

`Webhook → Code → Send Email`

Il backend chiama il nodo **Webhook** ogni volta che succede qualcosa; il nodo **Code**
trasforma l'evento in oggetto + testo e lascia passare solo quelli interessanti (nuovo RSVP,
cambio di stato RSVP, prenotazione navetta, quota regalo, evento di test). Auguri, foto e
notifiche vengono ignorati: basta aggiungere un `case` nello switch per riceverli.

Collegamento:

1. importa il workflow e **attivalo** (interruttore in alto a destra): l'URL di produzione è
   `https://<tuo-n8n>/webhook/neuroparty` (quello con `/webhook-test/` funziona solo mentre
   premi *Listen for test event* nell'editor);
2. nel `.env` del backend imposta `N8N_WEBHOOK_URL` a quell'indirizzo (o all'indirizzo
   interno `http://<container-n8n>:5678/webhook/neuroparty` se i due container condividono
   la rete Docker) e riavvia: `docker compose -f docker-compose.prod.yml up -d`;
3. collauda con `./scripts/n8n-test-webhook.sh`: se `delivered` è `true` ti arriverà la mail
   "Test collegamento n8n riuscito".

Sicurezza consigliata: genera un segreto (`openssl rand -hex 24`), mettilo in
`N8N_WEBHOOK_SECRET` e nel nodo Webhook scegli **Authentication → Header Auth** creando una
credenziale con nome `X-Automation-Secret` e quel valore. Le chiamate senza il segreto giusto
vengono rifiutate da n8n. Il backend invia anche `X-NeuroParty-Signature: sha256=<HMAC>` del
corpo, se vuoi verificare la firma in un nodo Code.

## Formato degli eventi

```json
{
  "event": "bus.booked",
  "timestamp": 1760000000000,
  "source": "neuroparty",
  "data": {
    "booking": { "id": 12, "passengerName": "Anna", "seatsCount": 3, "pickupStop": "...", "returnTripWanted": true, ... },
    "bus": { "maxSeats": 54, "bookedSeats": 13, "availableSeats": 41 }
  }
}
```

| Evento | Contenuto di `data` |
|---|---|
| `guest.created` / `guest.deleted` | `guest`, `summary` (coperti confermati, per categoria, esigenze alimentari) |
| `guest.updated` | `guest`, `changes` (i soli campi modificati, es. `{"rsvpStatus": "DECLINED"}`), `summary` |
| `bus.booked` / `bus.cancelled` | `booking`, `bus` (posti totali/prenotati/liberi) |
| `wish.created` | `wish` |
| `photo.uploaded` | `photo` (con `imageUri` assoluto) |
| `gift.contributed` | `contribution`, `target` (con l'importo raccolto aggiornato) |
| `notification.scheduled` | `notification` (programmata dagli organizzatori) |
| `notification.published` | `notification`, `scheduled` (true se pubblicata dallo scheduler) |
| `test` | `message`, `app` |

L'elenco completo con le descrizioni è anche in `GET /api/automation/status`.
