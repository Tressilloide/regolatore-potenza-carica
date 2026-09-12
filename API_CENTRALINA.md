# API della centralina EVR 3 W

Ricavata per reverse engineering dalla pagina web del dispositivo
(`http://192.168.1.22/`, firmware 1.15). Non è documentazione ufficiale:
è quanto si deduce dal JavaScript della centralina e dalle risposte osservate.

## Endpoint

Uno solo. Sia la lettura che i comandi passano da qui:

```
GET http://192.168.1.22/index.json            → stato completo (JSON)
GET http://192.168.1.22/index.json?btn=<cmd>  → esegue un comando e ritorna lo stato
```

Pagine HTML disponibili: `/` (pannello), `/info.html` (seriale e firmware),
`/setup.html` (configurazione, include MQTT e Push Notification API).
Nessun altro endpoint JSON esiste: `status.json`, `data.json`, `api.json`,
`car.json`, `ev.json`, `state.json`, `values.json` rispondono tutti 404.

## Campi dello stato

Esempio di risposta reale (auto non collegata, impianto trifase):

```json
{"status":"0","desc":"Non collegata","time":"0","energy":"0","power":"4140",
 "delay":"0","limit":"0","lock":"1","notify":"0","alg":"2","heat":"0",
 "tplug":"21","tboard":"30.4","pwmin":"4140","pwmax":"22000","wifi":"3",
 "phome":"852","psun":"0","pcar":"0","pnet":"852","fv":"1","intrnt":"1","tfase":"1"}
```

Tutti i valori sono **stringhe**, anche i numerici.

| Campo | Significato |
|---|---|
| `status` | Stato connessione: `0` non collegata, `1` attesa (delay), `2` pausa, `6` errore. Altri valori = in carica |
| `desc` | Descrizione testuale già in italiano ("Non collegata", "In carica", ...) |
| `time` | Durata della sessione corrente (secondi) |
| `energy` | Energia erogata nella sessione corrente (kWh) |
| `power` | Potenza **impostata** (W) |
| `pcar` | Potenza **realmente assorbita dall'auto** (W) — misura, non stima |
| `phome` | Consumo della casa (W) |
| `psun` | Produzione fotovoltaica (W) |
| `pnet` | Scambio con la rete (W) |
| `pwmin` / `pwmax` | Limiti di potenza per la configurazione attuale (W) |
| `tfase` | `1` = trifase, altro = monofase |
| `limit` | Energia da caricare impostata (kWh); `0` = nessun limite |
| `delay` | Avvio ritardato |
| `alg` | Algoritmo attivo: `0` Sole, `1` Eco, `2` Man, `3` Fast, `4` Alone |
| `lock` | Blocco presa |
| `notify` | Notifiche |
| `heat` | Riscaldamento |
| `tplug` / `tboard` | Temperatura presa / scheda (°C) |
| `wifi` | Qualità del segnale |
| `fv` / `intrnt` | Presenza fotovoltaico / connettività |

## Comandi (`?btn=`)

| Comando | Effetto |
|---|---|
| `i` | Accende (in) |
| `o` | Spegne (out) |
| `P<watt>` | Imposta la potenza, es. `P7360` |
| `L<kWh>` | Imposta l'energia da caricare, es. `L50` (1–100) |
| `D<...>` | Avvio ritardato |
| `l` | Attiva/disattiva il limite di energia |
| `s` | Algoritmo **Sole** |
| `e` | Algoritmo **Eco** |
| `m` | Algoritmo **Man** (manuale) |
| `f` | Algoritmo **Fast** |
| `n` | Blocco / notifiche |
| `k` | Tastierino per inserire il limite |
| `X` | Aggiornamento stato (usato dal polling interno) |

**Attenzione:** `P<watt>` ha effetto **solo con `alg` = `2` (Man)**. Nel sorgente
della centralina lo slider della potenza viene disabilitato negli altri
algoritmi. Se qualcuno passa il dispositivo a Sole o Eco dal suo pannello, i
comandi di potenza del regolatore vengono ignorati in silenzio — per questo il
codice controlla `alg` e avvisa su Telegram.

Come è stato ricavato il comando del limite:

```javascript
function chglimit() {
    var p = document.getElementById("limits").value;
    modlimit(1);
    loadDoc("L" + p);      // <-- il comando
}
function loadDoc(cmd) {
    ...
    this.open('GET', 'index.json?btn=' + cmd, true);
}
```

## Percentuale di carica della batteria (SoC)

**Non è disponibile.** Verificato in tre modi:

1. Nessuno dei 22 campi del JSON riporta uno stato di carica.
2. Il JavaScript della centralina non legge mai un campo simile: i soli campi
   usati sono quelli elencati sopra.
3. Nessun endpoint alternativo esiste (tutti 404).

Il motivo è strutturale, non una mancanza del firmware. Per conoscere il SoC
serve la comunicazione ad alto livello **ISO 15118** (PLC sul pilot, quella del
"Plug & Charge"), che richiede hardware dedicato sia sulla colonnina sia
sull'auto. Una wallbox che dialoga in **IEC 61851** — il PWM sul contatto pilot,
che è quello che fa questa — può solo *dire all'auto quanti ampere può
assorbire*: è un canale unidirezionale, l'auto non risponde nulla sul proprio
stato. Nessun aggiornamento firmware può aggirarlo.

### Cosa si può fare invece

- **`energy`** dà i kWh erogati nella sessione: conoscendo la capacità della
  batteria si stima il progresso (`energy / capacità * 100`), ignoto però il
  punto di partenza.
- **`pcar` che cala a batteria quasi piena** è un indizio affidabile di fine
  carica: l'auto riduce l'assorbimento anche se la colonnina concede di più.
  È già usato dal regolatore.
- **API del costruttore dell'auto** (Tesla, VW/Cupra, Renault...): è l'unica via
  realistica per il SoC vero, e passa da internet, non dalla colonnina.
- **MQTT**: `setup.html` espone una configurazione MQTT. Non è stata esplorata;
  pubblicherebbe comunque gli stessi campi, quindi nessun SoC.

## Identificazione

EVR 3 W — firmware 1.15. Seriale e PUK sono leggibili su `/info.html`
(non riportati qui: il PUK è un segreto).
