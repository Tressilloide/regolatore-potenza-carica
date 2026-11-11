import socket
import struct
import xml.etree.ElementTree as ET
import time
import requests
import logging
import json
import os
import asyncio
import threading
import queue
import signal
import sys
import io
import datetime
import itertools
import gzip
import copy
from collections import deque
import matplotlib

matplotlib.use('Agg') # Backend non interattivo per thread-safety
# API a oggetti (niente pyplot): nessun registro globale di figure, quindi
# nessuna figura orfana in RAM se qualcosa fallisce a meta' generazione.
from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.ticker import MaxNLocator
from matplotlib.dates import DateFormatter

from dotenv import load_dotenv
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from flask import Flask, jsonify, request, make_response

from regolatore_pid import RegolatorePID
from regolatore_classico import RegolatoreClassico
from protezione_contatore import ProtezioneContatore, limite_regolazione
from pianificatore import (Pianificatore, MODALITA, NOMI_MODALITA, prossima_scadenza,
                           interpreta_orario, PRESET_BASE, valida_preset, preset_attivo,
                           descrivi_preset)
from previsione import Previsione
from ponte_mqtt import PonteMQTT
import statistiche

# -----------------------------------------------------------
# CONFIGURAZIONE WEB & GLOBALE
# -----------------------------------------------------------
DIR_WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web')
app = Flask(__name__, static_folder=DIR_WEB, static_url_path='/static')

# Usiamo un dizionario per i parametri modificabili così sono condivisi tra Thread
CONFIG = {
    'MONOFASE_MIN_POWER': 1380,
    'MONOFASE_MAX_POWER': 7360,
    'TRIFASE_MIN_POWER': 4140,
    'TRIFASE_MAX_POWER': 22000,
    'POTENZA_PROTEZIONE': 300,      # Modificabile da Web e Telegram (persistito)
    'POTENZA_PRELEVABILE': 0,       # Quanto l'AUTO puo' prendere dalla rete, oltre alla casa (Web e Telegram, persistito)
    # Modalita' di carica (pianificatore.py, persistita): 'sole' = solo il
    # sole in piu', 'misto' = sole + POTENZA_PRELEVABILE, 'veloce' = quanto il
    # contatore permette. L'obiettivo kWh sta in FILE_PIANO.
    'MODALITA': 'misto',
    # Preset: "tanti W dalla rete, fermati a tanti kWh" (pianificatore.py).
    # Si scelgono e si modificano dalla pagina; PRESET_ATTIVO e' l'indice.
    'PRESET': copy.deepcopy(PRESET_BASE),
    'PRESET_ATTIVO': 0,
    # Regolazione calma: limita i cambi di potenza (regolatore_classico.py)
    'REGOLAZIONE_CALMA': False,
    'CALMA_SOGLIA_W': 500,
    'CALMA_INTERVALLO_S': 30,
    'RAPPORTO_SETTIMANALE': True,   # la domenica all'ora del riepilogo, su Telegram
    # --- Contatore di casa (protezione_contatore.py) ---
    # Oltre LIMITE_CONTATORE di prelievo (sole escluso, auto compresa) il
    # contatore stacca: 6 kW di contratto + 10%. I regolatori non puntano mai
    # oltre LIMITE - MARGINE (il margine copre l'errore del sensore e il
    # ritardo dell'auto); se il prelievo lo supera comunque, l'auto viene
    # ridotta o spenta subito. 0 = protezione disattivata.
    'LIMITE_CONTATORE': 6600,
    'MARGINE_CONTATORE': 300,
    'LIMITE_KWH': 100,              # Energia da caricare, 1-100 kWh (persistito)
    'COOLDOWN_ACCENSIONE': 60,
    'UPDATE_INTERVAL_S': 5,
    'TIMER_SPEGNIMENTO': 60,
    'MCAST_GRP': '224.192.32.19',
    'MCAST_PORT': 22600,
    'IFACE': '192.168.1.23',
    'WALLBOX_IP': '192.168.1.22',
    'PORT' :5000,
    'SMOOTHING_ALPHA': 0.9,
    'MAX_DELTA_PER_SEC': 1500,
    'MAX_FAILED_OFF_ATTEMPTS': 2,   # Tentativi falliti prima di considerare wallbox offline

    # --- Controllo fase automatico ---
    # La lettura di index.json e' un GET su LAN (~5 ms): si puo' fare spesso.
    # Va fatta spesso: pcar (potenza misurata) entra nell'anello di controllo,
    # e una misura vecchia di 30s farebbe oscillare la regolazione perche' il
    # regolatore non vedrebbe l'effetto dei comandi appena inviati.
    'INTERVALLO_SYNC_FASE': 5,
    'MAX_ETA_PCAR': 20,             # Oltre questa eta' (s) pcar non e' affidabile

    # --- Prezzi per la stima del risparmio (persistiti) ---
    # Il risparmio di caricare col proprio sole invece che dalla rete e' la
    # differenza tra quanto NON compri e quanto rinunci a vendere.
    'PREZZO_ACQUISTO': 0.25,        # EUR/kWh comprati dalla rete
    'PREZZO_VENDITA': 0.10,         # EUR/kWh ceduti alla rete

    # --- Sicurezza ---
    'TEMP_PRESA_ALLARME': 70,       # Oltre questa temperatura (C) scatta l'allarme
    'TEMP_PRESA_ATTENZIONE': 60,
    'USA_PCAR': True,               # Usa la potenza auto misurata invece del setpoint
    'ALG_MANUALE': '2',
    # Al riavvio: se l'auto sta gia' caricando, adotta lo stato invece di
    # spegnerla. Con Restart=always ogni crash costerebbe ~60s di ricarica
    # (spegnimento + cooldown). Mettere a False per tornare al comportamento
    # precedente, che forzava sempre uno stato noto spegnendo.
    'ADOTTA_CARICA_IN_CORSO': True,             # alg=2 (Man): l'unico in cui btn=P<watt> ha effetto     # Ogni quanti secondi rileggere 'tfase' dalla centralina

    # --- Algoritmo di regolazione (persistito, /algoritmo da Telegram) ---
    # 'classico': la logica storica di run_logic.
    # 'pid': regolatore_pid.py, con la stima dello scambio ogni 6 s. Tarato
    #   con simulatore_regolatore.py sullo storico reale.
    'ALGORITMO': 'classico',
    'PID_KP': 0.1,
    'PID_KI': 0.15,                 # 1/s: a ogni pacchetto (6 s) recupera ~il 90% dell'errore
    'PID_KD': 0.0,                  # con misure ogni 6 s il derivativo aggiunge solo comandi
    'PID_MARGINE': 300,             # W di export da tenere come riserva (i carichi su una fase che importa si vedono solo ogni 60 s)
    'PID_BANDA_DISCESA': 100,       # si scende appena il PID chiede 100 W in meno (in salita vale POTENZA_PROTEZIONE)
    'PID_IMPORT_SPEGNIMENTO': 200,  # import medio al minimo oltre cui, dopo TIMER_SPEGNIMENTO, si spegne
    'PID_FASE_MONOFASE': 0,         # fase della wallbox in monofase: L1 (misurato sul campo)

    # --- Limite di ricarica (kWh) --------------------------------------
    # Confermato sul campo leggendo chglimit()/pushed_limit() della centralina
    # e verificato inviando btn=L45 con la wallbox spenta: la centralina
    # risponde con "limit": "45" e lo mantiene alla rilettura successiva.
    'CMD_LIMITE_TEMPLATE': 'L{valore}',   # -> index.json?btn=L50
    'CMD_LIMITE_ATTIVA': 'l',       # btn=l: pulsante "Limite" (mostra/nasconde riga, non serve per impostare il valore)
    'CHIAVE_LIMITE_JSON': 'limit',  # chiave di index.json che riporta il limite attuale

    # --- Storico e watchdog ---
    'STORICO_INTERVALLO_S': 10,     # Un campione ogni N secondi (downsampling)
    'STORICO_RETENTION_GIORNI': 7,
    'STORICO_FLUSH_S': 60,          # Scrittura su disco a blocchi (salva-SD)
    'WATCHDOG_SENSORE_S': 300,      # Nessun pacchetto da >5 min => allarme
    'SESSIONE_OFFLINE_S': 600,      # Centralina muta da 10 min mentre caricava => sessione chiusa
    'ORA_RIEPILOGO': 23,            # Ora del riepilogo giornaliero Telegram
}

WALLBOX_URL = f"http://{CONFIG['WALLBOX_IP']}/index.json"

# File di stato, accanto allo script (il service imposta WorkingDirectory)
DIR_BASE = os.path.dirname(os.path.abspath(__file__))
FILE_CONFIG = os.path.join(DIR_BASE, 'config_utente.json')
FILE_STORICO = os.path.join(DIR_BASE, 'storico.jsonl')
FILE_STORICO_GIORNALIERO = os.path.join(DIR_BASE, 'storico_giornaliero.json')
FILE_SESSIONI = os.path.join(DIR_BASE, 'sessioni.json')
FILE_STATO_ENERGIA = os.path.join(DIR_BASE, 'stato_energia.json')
FILE_PIANO = os.path.join(DIR_BASE, 'pianificazione.json')
FILE_STATISTICHE = os.path.join(DIR_BASE, 'statistiche_giorni.jsonl')
FILE_PREVISIONI = os.path.join(DIR_BASE, 'previsioni.jsonl')

# Stato condiviso per la Web UI e Telegram
SYSTEM_STATE = {
    'ULTIMA_LETTURA_FASI': None,
    'ULTIMA_LETTURA_SOLARE': None,
    'ULTIME_LETTURE_FASI': deque(maxlen=10000),  # Buffer per i grafici (~28h a 10s)
    'MONITOR_FASI': [0,0,0,0,0,0],
    'WALLBOX_POWER': 0,
    'WALLBOX_STATUS': False,
    'IMPIANTO_FASE': 0, # 0=Mono, 1=Tri
    'WB_PCAR': None,                # Potenza auto MISURATA dalla centralina (W)
    'WB_PCAR_LETTO_IL': 0,          # Quando e' stata letta (per scartarla se stantia)
    'WB_ENERGIA_SESSIONE': 0,      # kWh della sessione (gia' convertiti da Wh)
    'WB_ENERGIA_MISURATA': False,  # True se la centralina ha un contatore reale
    'WB_TEMPO_SESSIONE': 0,
    'WB_TEMP_PRESA': None,
    'WB_TEMP_SCHEDA': None,
    'WB_STATUS': '',
    'WB_DESC': '',
    'WB_ALG': '',
    'LIMITE_KWH_CENTRALINA': None,  # Ultimo limite letto dalla centralina
    'CENTRALINA_ONLINE': True,
    'SENSORE_ONLINE': True,
    'ERRORI_PARSING': 0,
    'LOG_SEQ': 0,
    'LOGS': deque(maxlen=200) # Buffer per la console Web
}

# Lock -------------------------------------------------------------------
# STATO_LOCK: protegge le letture/scritture COMPOSITE di CONFIG e SYSTEM_STATE.
#   Le singole assegnazioni sono gia' atomiche grazie al GIL: si lockano solo
#   le sequenze che devono risultare coerenti (es. lo snapshot per /api/data).
# WALLBOX_LOCK: serializza le SEQUENZE di comandi verso la centralina. Senza,
#   il thread Flask e quello Telegram possono interlacciarsi con il thread
#   principale nel bel mezzo di un turn_off() (send_command -> sleep -> set_power).
STATO_LOCK = threading.RLock()
WALLBOX_LOCK = threading.RLock()

# Variabile globale per accedere al controller dalla UI Web e da Telegram
wallbox_instance = None
archivio_statistiche = statistiche.Archivio(FILE_STATISTICHE, log=lambda testo: log_msg(testo))
contatori_instance = None
regolatore_pid_instance = None
classico_instance = None
pianificatore_instance = None
protezione_instance = None
ponte_mqtt_instance = None

load_dotenv()
API_KEY = os.getenv('API_KEY')
CHAT_ID = os.getenv('CHAT_ID')

# Configurazione logging base
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(message)s', datefmt='%H:%M:%S')
# Silenzia il rumore di fondo delle librerie
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("werkzeug").setLevel(logging.ERROR)
logging.getLogger("telegram").setLevel(logging.WARNING)
# -----------------------

_log_seq = itertools.count(1)

def log_msg(msg):
    """Salva il log sia su terminale che nel buffer per la Web UI"""
    t_str = time.strftime("%H:%M:%S")
    full_msg = f"[{t_str}] {msg}"
    print(full_msg, flush=True)
    # deque(maxlen) scarta da sola i messaggi vecchi: niente pop(0) su list
    SYSTEM_STATE['LOGS'].append(full_msg)
    # Numero progressivo dell'ultima riga: la pagina chiede solo le nuove
    SYSTEM_STATE['LOG_SEQ'] = next(_log_seq)

_ultimi_log = {}

def log_throttled(chiave, msg, intervallo=60):
    """Logga al massimo una volta ogni `intervallo` secondi per la stessa chiave.

    Serve per i messaggi che verrebbero emessi a ogni pacchetto (ogni pochi
    secondi), riempiendo journalctl e il buffer della console web.
    """
    adesso = time.time()
    if adesso - _ultimi_log.get(chiave, 0) < intervallo:
        return
    _ultimi_log[chiave] = adesso
    log_msg(msg)

# -----------------------------------------------------------
# PERSISTENZA CONFIGURAZIONE UTENTE
# -----------------------------------------------------------
# Solo le chiavi realmente modificabili dall'utente vengono salvate su disco:
# le costanti di impianto restano nel codice, cosi' un file vecchio non le congela.
CHIAVI_PERSISTENTI = {
    # chiave: (minimo, massimo, tipo)
    'POTENZA_PRELEVABILE': (0, 20000, int),
    'POTENZA_PROTEZIONE':  (50, 5000, int),
    'MODALITA':            (None, None, MODALITA),
    'LIMITE_KWH':          (1, 100, int),
    'PRESET_ATTIVO':       (0, 7, int),
    'REGOLAZIONE_CALMA':   (None, None, bool),
    'CALMA_SOGLIA_W':      (100, 3000, int),
    'CALMA_INTERVALLO_S':  (5, 600, int),
    'RAPPORTO_SETTIMANALE': (None, None, bool),
    'PREZZO_ACQUISTO':     (0, 5, float),
    'PREZZO_VENDITA':      (0, 5, float),
    # tupla di stringhe = scelta fra valori ammessi
    'ALGORITMO':           (None, None, ('classico', 'pid')),
    'PID_KP':              (0, 5, float),
    'PID_KI':              (0, 2, float),
    'PID_KD':              (0, 20, float),
    'PID_MARGINE':         (0, 3000, int),
}

def valida_valore(chiave, valore):
    """Valida un valore per una chiave persistente.

    Ritorna (ok, valore_pulito, errore). Gestisce None, NaN, stringhe vuote e
    fuori range: sono tutti i modi in cui la vecchia /api/settings andava in 500.
    """
    if chiave not in CHIAVI_PERSISTENTI:
        return False, None, f"Parametro sconosciuto: {chiave}"

    minimo, massimo, tipo = CHIAVI_PERSISTENTI[chiave]

    if valore is None:
        return False, None, f"{chiave}: valore mancante"

    if isinstance(tipo, tuple):
        scelta = str(valore).strip().lower()
        if scelta in tipo:
            return True, scelta, None
        return False, None, f"{chiave}: valori ammessi {', '.join(tipo)} (ricevuto {valore!r})"

    if tipo is bool:
        if isinstance(valore, bool):
            return True, valore, None
        if isinstance(valore, str):
            if valore.lower() in ('true', '1', 'on', 'si'):  return True, True, None
            if valore.lower() in ('false', '0', 'off', 'no'): return True, False, None
        if isinstance(valore, (int, float)):
            return True, bool(valore), None
        return False, None, f"{chiave}: valore booleano non valido ({valore!r})"

    try:
        # float() intercetta NaN/inf, che int() accetterebbe male o farebbe esplodere
        temp = float(valore)
    except (TypeError, ValueError):
        return False, None, f"{chiave}: valore non numerico ({valore!r})"

    if temp != temp or temp in (float('inf'), float('-inf')):
        return False, None, f"{chiave}: valore non finito"

    pulito = round(temp, 4) if tipo is float else int(temp)
    if minimo is not None and pulito < minimo:
        return False, None, f"{chiave}: minimo consentito {minimo} (ricevuto {pulito})"
    if massimo is not None and pulito > massimo:
        return False, None, f"{chiave}: massimo consentito {massimo} (ricevuto {pulito})"

    return True, pulito, None

def carica_config():
    """Carica config_utente.json in CONFIG. Su file assente/corrotto usa i default.

    In caso di errore NON sovrascrive il file: resta ispezionabile dall'utente.
    """
    if not os.path.exists(FILE_CONFIG):
        log_msg(f"[CONFIG] {os.path.basename(FILE_CONFIG)} non presente: uso i valori di default.")
        return

    try:
        with open(FILE_CONFIG, 'r', encoding='utf-8') as f:
            dati = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log_msg(f"[CONFIG] ERRORE lettura {os.path.basename(FILE_CONFIG)}: {e}. Uso i default (file NON modificato).")
        return

    if not isinstance(dati, dict):
        log_msg("[CONFIG] ERRORE: il file non contiene un oggetto JSON. Uso i default.")
        return

    caricati = []
    with STATO_LOCK:
        for chiave in CHIAVI_PERSISTENTI:
            if chiave not in dati:
                continue
            ok, pulito, errore = valida_valore(chiave, dati[chiave])
            if ok:
                CONFIG[chiave] = pulito
                caricati.append(f"{chiave}={pulito}")
            else:
                log_msg(f"[CONFIG] Valore ignorato: {errore}")
        # I preset sono un elenco: validazione a parte
        if 'PRESET' in dati:
            ok, esito = valida_preset(dati['PRESET'])
            if ok:
                CONFIG['PRESET'] = esito
                caricati.append(f"PRESET={len(esito)}")
            else:
                log_msg(f"[CONFIG] Preset ignorati: {esito}")
        if CONFIG['PRESET_ATTIVO'] >= len(CONFIG['PRESET']):
            CONFIG['PRESET_ATTIVO'] = 0
        # "veloce" non esiste piu' (11/11/2026): vale il preset con quel nome
        if str(dati.get('MODALITA', '')).lower() == 'veloce':
            nomi = [p['nome'].lower() for p in CONFIG['PRESET']]
            CONFIG['MODALITA'] = 'preset'
            CONFIG['PRESET_ATTIVO'] = nomi.index('veloce') if 'veloce' in nomi else 0

    log_msg(f"[CONFIG] Caricati da disco: {', '.join(caricati) if caricati else 'nessun valore valido'}")

def salva_config():
    """Salva le chiavi persistenti su disco in modo ATOMICO.

    Scrive su .tmp e poi os.replace(): se il Pi perde corrente a meta' scrittura
    il file originale resta intatto, mai troncato.
    """
    with STATO_LOCK:
        dati = {chiave: CONFIG[chiave] for chiave in CHIAVI_PERSISTENTI}
        dati['PRESET'] = CONFIG['PRESET']

    tmp = FILE_CONFIG + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(dati, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, FILE_CONFIG)
        return True
    except OSError as e:
        log_msg(f"[CONFIG] ERRORE salvataggio: {e}")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False

# -----------------------------------------------------------
# NOTIFICHE TELEGRAM (INVIO ASINCRONO, NON BLOCCANTE)
# -----------------------------------------------------------
# Prima ogni notifica faceva asyncio.run(...) dal thread principale: creava un
# event loop e un oggetto Bot NUOVI ogni volta e bloccava la ricezione dei
# pacchetti UDP per tutta la durata della chiamata HTTP verso Telegram.
# Ora: coda limitata + un solo worker con un solo loop e un solo Bot.
CODA_NOTIFICHE = queue.Queue(maxsize=100)
_ultime_notifiche = {}   # dedup_key -> timestamp ultimo invio
_dedup_lock = threading.Lock()

def notifica(messaggio, dedup_key=None, min_intervallo=300):
    """Accoda una notifica Telegram. NON blocca mai il chiamante.

    dedup_key: se valorizzata, ripetizioni dello stesso evento entro
    min_intervallo secondi vengono scartate (anti-spam).
    """
    if not API_KEY or not CHAT_ID:
        return

    if dedup_key:
        adesso = time.time()
        with _dedup_lock:
            ultimo = _ultime_notifiche.get(dedup_key, 0)
            if adesso - ultimo < min_intervallo:
                return
            _ultime_notifiche[dedup_key] = adesso

    try:
        CODA_NOTIFICHE.put_nowait(messaggio)
    except queue.Full:
        # Telegram irraggiungibile: scarto il piu' vecchio invece di crescere
        # senza limite (niente memory leak) e inserisco il nuovo.
        try:
            CODA_NOTIFICHE.get_nowait()
            CODA_NOTIFICHE.put_nowait(messaggio)
        except (queue.Empty, queue.Full):
            pass

def reset_dedup(dedup_key):
    """Azzera il dedup di un evento, cosi' il prossimo invio passa subito."""
    with _dedup_lock:
        _ultime_notifiche.pop(dedup_key, None)

def _worker_notifiche():
    """Thread daemon: un solo event loop asyncio, un solo Bot, consuma la coda."""
    if not API_KEY or not CHAT_ID:
        log_msg("[TELEGRAM] Credenziali mancanti: notifiche disabilitate.")
        return

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    bot = Bot(token=API_KEY)

    async def _invia(testo):
        # Prima con Markdown (i messaggi usano *grassetto*); se il testo
        # contiene caratteri che Telegram non riesce a interpretare, si
        # ripiega sul testo semplice invece di perdere la notifica.
        try:
            await bot.send_message(chat_id=CHAT_ID, text=testo, parse_mode='Markdown')
        except Exception:
            await bot.send_message(chat_id=CHAT_ID, text=testo)

    while True:
        messaggio = CODA_NOTIFICHE.get()
        try:
            loop.run_until_complete(_invia(messaggio))
        except Exception as e:
            log_msg(f"[ERRORE TELEGRAM] Invio fallito: {e}")
        finally:
            CODA_NOTIFICHE.task_done()

# -----------------------------------------------------------
# GESTIONE TELEGRAM BOT (RICEZIONE COMANDI)
# -----------------------------------------------------------
def check_auth(update: Update) -> bool:
    """Verifica che il comando provenga dall'utente autorizzato."""
    if str(update.effective_chat.id) != str(CHAT_ID):
        log_msg(f"[TELEGRAM] Tentativo di accesso non autorizzato da {update.effective_chat.id}")
        return False
    return True

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update): return
    msg = (
        "🤖 *Comandi Solar Controller*\n\n"
        "*Stato*\n"
        "/info - Stato attuale del sistema\n"
        "/energia - Riepilogo energetico di oggi\n"
        "/storico [giorni] - Riepilogo degli ultimi giorni\n"
        "/settimana - La settimana contro quella prima\n"
        "/previsione - Sole previsto oggi e domani\n"
        "/sessioni - Ultime sessioni di ricarica\n"
        "/grafici [15m|1h|6h|24h|7g] - Grafico dell'andamento\n"
        "/fase - Rileva subito monofase/trifase\n\n"
        "*Controllo*\n"
        "/accendi - Forza l'accensione della Wallbox\n"
        "/spegni - Forza lo spegnimento della Wallbox\n"
        "/reset - Re-inizializza la centralina\n\n"
        "*Carica*\n"
        "/modalita [sole|misto|preset] - Solo sole, sole + rete, preset\n"
        "/preset [nome] - Preset: tanti kW dalla rete, fino a tanti kWh\n"
        "/obiettivo <kWh> <HH:MM> - Pronta entro un orario (off per annullare)\n"
        "/programma - Obiettivi che si ripetono nella settimana\n\n"
        "*Impostazioni*\n"
        "/prelevabile <W> - Quanto l'auto può prendere dalla rete, oltre alla casa\n"
        "/protezione <W> - Soglia di protezione\n"
        "/limite <kWh> - Energia da caricare (1-100)\n"
        "/algoritmo [classico|pid] - Algoritmo di regolazione\n"
        "/calma [on|off] - Limita i cambi di potenza\n"
        "/pid [kp|ki|kd|margine <valore>] - Stato e taratura del PID\n"
    )
    await update.message.reply_text(msg, parse_mode='Markdown')

def descrizione_limite():
    """Il limite kWh com'e' davvero sulla centralina (letto ogni 5 s)."""
    letto = SYSTEM_STATE.get('LIMITE_KWH_CENTRALINA')
    if letto is None:
        return f"sconosciuto, centralina non raggiungibile (salvato: {CONFIG['LIMITE_KWH']} kWh)"
    return f"attivo, {letto} kWh" if letto > 0 else "nessuno"

async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update): return
    with STATO_LOCK:
        fasi = list(SYSTEM_STATE['MONITOR_FASI'])
        wb_on = SYSTEM_STATE['WALLBOX_STATUS']
        wb_power = SYSTEM_STATE['WALLBOX_POWER'] if wb_on else 0
        fase_mode = SYSTEM_STATE['IMPIANTO_FASE']
        centralina_ok = SYSTEM_STATE['CENTRALINA_ONLINE']
        sensore_ok = SYSTEM_STATE['SENSORE_ONLINE']
        prelevabile = CONFIG['POTENZA_PRELEVABILE']
        protezione = CONFIG['POTENZA_PROTEZIONE']
        limite = descrizione_limite()

    tot_grid = sum(fasi[0:3])
    tot_solar = sum(fasi[3:6])
    wb_status = "🟢 ON" if wb_on else "🔴 OFF"
    modalita = "Trifase" if fase_mode == 1 else "Monofase"

    msg = (
        "📊 *Stato Sistema*\n\n"
        f"☀️ *Solare:* {tot_solar:.0f} W\n"
        f"🔌 *Rete:* {tot_grid:.0f} W\n"
        f"🚗 *Wallbox:* {wb_status} ({wb_power:.0f} W)\n"
        f"⚙️ *Impianto:* {modalita}\n"
        f"🚦 *Carica:* {pianificatore_instance.descrizione() if pianificatore_instance else '?'}\n"
        f"🛠️ *Prelevabile per l'auto:* {prelevabile} W\n"
        f"⚡ *Limite contatore:* {CONFIG['LIMITE_CONTATORE']} W\n"
        f"🛡️ *Protezione:* {protezione} W\n"
        f"🔋 *Limite carica:* {limite}\n"
    )
    # Dati letti direttamente dalla centralina
    with STATO_LOCK:
        desc = SYSTEM_STATE['WB_DESC']
        pcar = SYSTEM_STATE['WB_PCAR']
        sessione = SYSTEM_STATE['WB_ENERGIA_SESSIONE']
        sess_misurata = SYSTEM_STATE['WB_ENERGIA_MISURATA']
        tempo_sess = SYSTEM_STATE['WB_TEMPO_SESSIONE']
        temp_presa = SYSTEM_STATE['WB_TEMP_PRESA']
        alg = SYSTEM_STATE['WB_ALG']

    if desc:
        msg += f"\n🔎 *Centralina*\n🚙 Auto: {desc}\n"
        if pcar is not None:
            msg += f"📏 Potenza misurata: {pcar:.0f} W\n"
        if sessione:
            nota = "" if sess_misurata else " (stimata)"
            msg += f"🔋 Sessione: {sessione:.2f} kWh{nota}"
            if tempo_sess:
                msg += f" in {tempo_sess/60:.0f} min"
            msg += "\n"
        if temp_presa is not None:
            msg += f"🌡️ Presa: {temp_presa:.0f} °C\n"

    if alg and alg != CONFIG['ALG_MANUALE']:
        nomi = {'0': 'Sole', '1': 'Eco', '2': 'Man', '3': 'Fast', '4': 'Alone'}
        msg += (f"\n⚠️ Centralina in modalità *{nomi.get(alg, alg)}* invece di *Man*: "
                f"i comandi di potenza non hanno effetto.\n")
    if not centralina_ok:
        msg += "\n⚠️ *Centralina non raggiungibile*\n"
    if not sensore_ok:
        msg += "\n⚠️ *Nessun dato dal sensore*\n"

    await update.message.reply_text(msg, parse_mode='Markdown')

async def cmd_accendi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update): return
    if not wallbox_instance:
        await update.message.reply_text("❌ Controller Wallbox non disponibile.")
        return
    # clear any manual off override so automation can resume
    wallbox_instance.manual_off = False
    # to_thread: turn_on fa HTTP sincrono, non deve bloccare il loop del bot
    await asyncio.to_thread(wallbox_instance.turn_on)
    await update.message.reply_text("✅ *Comando inviato:* Accensione Wallbox (override manuale disattivato)", parse_mode='Markdown')

async def cmd_spegni(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update): return
    if not wallbox_instance:
        await update.message.reply_text("❌ Controller Wallbox non disponibile.")
        return
    # activate manual off override so it stays off until /accendi
    wallbox_instance.manual_off = True
    await asyncio.to_thread(wallbox_instance.turn_off, True)
    await update.message.reply_text("🛑 *Comando inviato:* Spegnimento Wallbox (override manuale attivo)", parse_mode='Markdown')

def applica_impostazione(chiave, valore, origine):
    """Valida, applica e PERSISTE una singola impostazione.

    Ritorna (ok, messaggio). Usata da Web e Telegram cosi' la validazione e il
    salvataggio su disco sono identici da qualunque parte arrivi la modifica.
    """
    ok, pulito, errore = valida_valore(chiave, valore)
    if not ok:
        return False, errore

    with STATO_LOCK:
        CONFIG[chiave] = pulito
    salva_config()
    log_msg(f"[{origine}] {chiave} impostata a {pulito}")
    return True, None

def esegui_comando(nome, valore, origine):
    """Unico punto per i comandi di carica, da Web, Telegram e Home Assistant
    (MQTT): stessa validazione e stesso effetto da qualunque parte arrivino.

    nome: 'modalita' | 'prelevabile' | 'obiettivo' ('<kWh> <HH:MM>' oppure 'off')
          | 'preset' (nome o indice da 0) | 'calma' (on/off)
    Ritorna (ok, messaggio).
    """
    valore = '' if valore is None else str(valore).strip()
    if nome == 'modalita' and valore.lower() == 'veloce':
        esito = applica_preset('Veloce', origine)      # il nome di prima dell'11/11/2026
    elif nome == 'modalita' and valore.lower() == 'preset':
        esito = applica_preset(CONFIG['PRESET_ATTIVO'], origine)
    elif nome == 'modalita':
        ok, errore = applica_impostazione('MODALITA', valore, origine)
        esito = (ok, f"Modalità: {NOMI_MODALITA[CONFIG['MODALITA']]}" if ok else errore)
    elif nome == 'preset':
        esito = applica_preset(valore, origine)
    elif nome == 'calma':
        ok, errore = applica_impostazione('REGOLAZIONE_CALMA', valore, origine)
        esito = (ok, ("Regolazione calma attiva: meno cambi di potenza" if CONFIG['REGOLAZIONE_CALMA']
                      else "Regolazione calma spenta") if ok else errore)
    elif nome == 'prelevabile':
        ok, errore = applica_impostazione('POTENZA_PRELEVABILE', valore, origine)
        esito = (ok, f"Prelevabile per l'auto: {CONFIG['POTENZA_PRELEVABILE']} W" if ok else errore)
    elif pianificatore_instance is None:
        esito = (False, "Pianificatore non pronto")
    elif nome == 'obiettivo':
        esito = imposta_obiettivo_da_testo(valore)
    else:
        esito = (False, f"Comando sconosciuto: {nome}")
    if esito[0] and nome == 'obiettivo':
        log_msg(f"[{origine}] {esito[1]}")
    if esito[0] and ponte_mqtt_instance and pianificatore_instance:
        ponte_mqtt_instance.pubblica_controlli(stato_controlli())
    return esito

def applica_limite(kwh):
    """Manda il limite di energia alla colonnina (0 = nessuno). Se non
    risponde lo tiene in attesa: lo applica il thread periodico appena torna.
    Ritorna una frase per l'utente."""
    testo = f"limite {kwh} kWh" if kwh else "nessun limite"
    colonnina = wallbox_instance
    if colonnina is None or not CONFIG.get('CMD_LIMITE_TEMPLATE'):
        return testo
    if kwh > 0:
        with STATO_LOCK:
            CONFIG['LIMITE_KWH'] = kwh
        salva_config()
    if colonnina.sync_falliti < 2:
        inviato, _ = colonnina.set_limite_kwh(kwh)
        if inviato:
            colonnina.limite_in_attesa = None
            return testo
    colonnina.limite_in_attesa = kwh
    return testo + " appena la colonnina risponde"

def applica_preset(scelta, origine):
    """Sceglie un preset (per nome, o per indice da 0): modalita' 'preset',
    il suo prelevabile come quota di rete e il suo limite sulla colonnina."""
    elenco = CONFIG['PRESET']
    testo = str(scelta).strip()
    nomi = [p['nome'].lower() for p in elenco]
    if testo.lower() in nomi:
        indice = nomi.index(testo.lower())
    elif testo.isdigit() and int(testo) < len(elenco):
        indice = int(testo)
    else:
        return False, f"Preset sconosciuto: {testo}. Ci sono: {', '.join(p['nome'] for p in elenco)}"
    with STATO_LOCK:
        CONFIG['PRESET_ATTIVO'] = indice
        CONFIG['MODALITA'] = 'preset'
    salva_config()
    p = elenco[indice]
    log_msg(f"[{origine}] Preset {p['nome']}: {descrivi_preset(p)}")
    limite = applica_limite(p['limite_kwh'])
    kw = f"{p['prelevabile'] / 1000:.1f}".replace('.', ',')
    return True, f"Preset {p['nome']}: {kw} kW dalla rete, {limite}"

def salva_preset(elenco, origine):
    """Sostituisce l'elenco dei preset. Se e' attivo un preset e il suo
    limite e' cambiato, lo aggiorna anche sulla colonnina."""
    ok, esito = valida_preset(elenco)
    if not ok:
        return False, esito
    prima = preset_attivo(CONFIG)
    with STATO_LOCK:
        CONFIG['PRESET'] = esito
        if CONFIG['PRESET_ATTIVO'] >= len(esito):
            CONFIG['PRESET_ATTIVO'] = 0
    salva_config()
    log_msg(f"[{origine}] Preset salvati: " + "; ".join(f"{p['nome']} ({descrivi_preset(p)})" for p in esito))
    adesso = preset_attivo(CONFIG)
    if CONFIG['MODALITA'] == 'preset' and adesso['limite_kwh'] != prima['limite_kwh']:
        applica_limite(adesso['limite_kwh'])
    if ponte_mqtt_instance:
        ponte_mqtt_instance.imposta_preset([p['nome'] for p in esito])
        if pianificatore_instance:
            ponte_mqtt_instance.pubblica_controlli(stato_controlli())
    return True, "Preset salvati"

def imposta_obiettivo_da_testo(testo):
    """'15 7:30' -> 15 kWh entro le 7:30 (oggi, o domani se gia' passate).
    'off' -> annulla."""
    parti = testo.replace(',', '.').split()
    if not parti or parti[0].lower() in ('off', 'no', 'annulla', '0'):
        return pianificatore_instance.annulla_obiettivo()
    if len(parti) != 2:
        return False, "Formato: <kWh> <HH:MM>, per esempio 15 7:30"
    orario = interpreta_orario(parti[1])
    if orario is None:
        return False, f"Orario non valido: {parti[1]}"
    try:
        kwh = float(parti[0].lower().replace('kwh', ''))
    except ValueError:
        return False, f"Energia non valida: {parti[0]}"
    return pianificatore_instance.imposta_obiettivo(kwh, prossima_scadenza(*orario))

def stato_controlli():
    """Modalita', prelevabile e obiettivo: per pagina web e Home Assistant."""
    stato = pianificatore_instance.stato() if pianificatore_instance else {}
    stato['prelevabile_w'] = CONFIG['POTENZA_PRELEVABILE']
    attivo = preset_attivo(CONFIG)
    stato['preset'] = CONFIG['PRESET']
    stato['preset_attivo'] = CONFIG['PRESET'].index(attivo) if attivo in CONFIG['PRESET'] else 0
    stato['preset_nome'] = attivo['nome']
    stato['calma'] = CONFIG['REGOLAZIONE_CALMA']
    stato['limite_in_attesa'] = wallbox_instance.limite_in_attesa if wallbox_instance else None
    return stato

# --- Previsione del sole (previsione.py) --------------------------------------
previsione_instance = Previsione()
_previsione = {'t': 0.0, 'riepilogo': {'fonte': None}}

def aggiorna_previsione(forza=False):
    """Passa alla previsione la storia (giorni chiusi e giornata in corso),
    al massimo ogni 5 minuti. Ritorna il riepilogo."""
    adesso = time.time()
    if forza or adesso - _previsione['t'] >= 300:
        _previsione['t'] = adesso
        giorni, oggi = giorni_statistiche()
        if wallbox_instance:
            # l'auto non prende piu' di 11 kW nemmeno in trifase (colonnina da 22)
            previsione_instance.potenza_massima_auto = min(11000, wallbox_instance.limiti_potenza()[1])
        previsione_instance.aggiorna_storia(giorni, oggi)
        _previsione['riepilogo'] = previsione_instance.riepilogo()
    return _previsione['riepilogo']

def sole_atteso(da, a):
    """Per il pianificatore: kWh di sole attesi per l'auto fra due istanti."""
    aggiorna_previsione()
    return previsione_instance.sole_per_auto(da, a)

def meteo_ricevuto(dati):
    """Previsione meteo arrivata da Home Assistant (thread MQTT)."""
    ore = dati.get('ore') if isinstance(dati, dict) else None
    capite = previsione_instance.imposta_meteo(ore)
    if capite:
        _previsione['t'] = 0.0      # al prossimo giro si ricalcola
        log_throttled('meteo_ricevuto', f"[PREVISIONE] Meteo da Home Assistant: {capite} ore", 6 * 3600)

def testo_previsione():
    p = aggiorna_previsione()
    if not p.get('fonte'):
        return "🌤️ Nessuna previsione: serve almeno un giorno di storico."
    fonte = ("dal meteo di Home Assistant" if p['fonte'] == 'meteo'
             else "dalla media degli ultimi giorni (senza meteo)")
    return (f"🌤️ *Sole previsto* ({fonte})\n\n"
            f"Oggi, da adesso: {p['oggi_restante_kwh']} kWh, di cui {p['oggi_restante_auto_kwh']} per l'auto\n"
            f"Domani: {p['domani_kwh']} kWh, di cui {p['domani_auto_kwh']} per l'auto\n"
            f"_Una giornata serena ne dà circa {p['sereno_kwh']}._")

# --- Statistiche per Home Assistant e rapporto settimanale ---------------------
_fondo = {'t': 0.0, 'w': None}

def prezzi_attuali():
    return {'acquisto': CONFIG['PREZZO_ACQUISTO'], 'vendita': CONFIG['PREZZO_VENDITA']}

def statistiche_per_ha(monitor):
    """I numeri pubblicati ogni minuto su solare/pi/statistiche."""
    giorni, oggi = giorni_statistiche()
    dati = {'autosufficienza_oggi': None, 'autoconsumo_oggi': None}
    if oggi in giorni:
        g = statistiche.giornaliero(giorni[oggi])
        dati.update(autosufficienza_oggi=g['autosufficienza'], autoconsumo_oggi=g['autoconsumo'])
    # quanto si puo' accendere adesso senza prelevare dalla rete
    dati['potenza_libera_w'] = max(0, round(monitor.solar_now - monitor.total_grid_load))
    adesso = time.time()
    if adesso - _fondo['t'] > 3600:
        _fondo['t'] = adesso
        da = (statistiche.data(oggi) - datetime.timedelta(days=7)).isoformat()
        s = statistiche.calcola(giorni, da, oggi, prezzi_attuali())
        _fondo['w'] = (s.get('casa') or {}).get('fondo_w')
    dati['carico_fondo_w'] = _fondo['w']
    p = aggiorna_previsione()
    dati['previsione_oggi_kwh'] = p.get('oggi_restante_kwh')
    dati['previsione_domani_kwh'] = p.get('domani_kwh')
    dati['previsione_fonte'] = p.get('fonte')
    return dati

def rapporto_settimanale():
    """La settimana da lunedi' a oggi, contro i giorni subito prima. Testo
    Markdown per Telegram, oppure None se non c'e' niente da dire."""
    giorni, oggi = giorni_statistiche()
    lunedi = statistiche.data(oggi) - datetime.timedelta(days=statistiche.data(oggi).weekday())
    s = statistiche.calcola(giorni, lunedi.isoformat(), oggi, prezzi_attuali(), CONFIG['LIMITE_CONTATORE'])
    t = s.get('totali')
    if not t:
        return None
    prima = s.get('precedente')
    kwh = lambda v: f"{v:.0f}" if v >= 100 else f"{v:.1f}".replace('.', ',')

    def confronto(chiave, punti=False):
        if not prima or prima.get(chiave) is None or t.get(chiave) is None:
            return ""
        if punti:
            diff = t[chiave] - prima[chiave]
            return f" ({'+' if diff >= 0 else '−'}{abs(diff):.0f} punti)"
        if abs(prima[chiave]) < 0.05:
            return ""
        diff = (t[chiave] - prima[chiave]) / prima[chiave] * 100
        return f" ({'+' if diff >= 0 else '−'}{abs(diff):.0f}%)"

    p = s['periodo']
    giorno = lambda iso: f"{int(iso[8:])}/{int(iso[5:7])}"
    righe = [f"📊 *La settimana* ({giorno(p['da'])} – {giorno(p['a'])})", "",
             f"☀️ Prodotti: {kwh(t['sole'])} kWh{confronto('sole')}",
             f"🏠 Casa: {kwh(t['casa'])} kWh{confronto('casa')}"]
    if t['auto'] > 0.05:
        righe.append(f"🚗 Auto: {kwh(t['auto'])} kWh{confronto('auto')}, "
                     f"{t['auto_sole_pct']:.0f}% dal sole")
    righe += [f"🔌 Prelevati: {kwh(t['prelievo'])} kWh{confronto('prelievo')}",
              f"↗️ Immessi: {kwh(t['immessa'])} kWh{confronto('immessa')}"]
    if t['autosufficienza_pct'] is not None:
        righe.append(f"🌱 Autosufficienza: {t['autosufficienza_pct']:.0f}%{confronto('autosufficienza_pct', True)}")
    ric = s['ricariche']
    if ric.get('n'):
        righe.append(f"🔋 Ricariche: {ric['n']}, in media {kwh(ric['media_kwh'])} kWh")
    e = t['euro']
    righe.append(f"💶 Resa dell'impianto: {e['beneficio']:.2f} €".replace('.', ','))
    primati = {r['chiave']: r for r in s['record']}
    if 'giorno_sole_max' in primati:
        r = primati['giorno_sole_max']
        righe.append(f"🏆 Giorno migliore: {giorno(r['giorno'])} con {kwh(r['valore'])} kWh")
    if 'picco_prelievo' in primati:
        righe.append(f"⚡ Prelievo più alto: {primati['picco_prelievo']['valore'] / 1000:.1f} kW".replace('.', ','))
    if prima:
        righe += ["", "_Fra parentesi il confronto con i giorni subito prima._"]
    return "\n".join(righe)

async def cmd_modalita(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/modalita [sole|misto|preset]"""
    if not check_auth(update): return
    if not context.args:
        await update.message.reply_text(
            f"⚙️ *Modalità:* {pianificatore_instance.descrizione() if pianificatore_instance else '?'}\n\n"
            "`/modalita sole` - solo il sole in più rispetto alla casa\n"
            f"`/modalita misto` - sole + {CONFIG['POTENZA_PRELEVABILE']} W dalla rete\n"
            "`/modalita preset` - il preset scelto (vedi /preset)", parse_mode='Markdown')
        return
    ok, messaggio = await asyncio.to_thread(esegui_comando, 'modalita', context.args[0], 'TELEGRAM')
    await update.message.reply_text(f"{'✅' if ok else '⚠️'} {messaggio}")

async def cmd_preset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/preset [nome | numero]"""
    if not check_auth(update): return
    elenco = CONFIG['PRESET']
    if not context.args:
        attivo = preset_attivo(CONFIG) if CONFIG['MODALITA'] == 'preset' else None
        righe = [f"{'▶️' if p is attivo else '▫️'} {n}. *{p['nome']}*: {descrivi_preset(p)}"
                 for n, p in enumerate(elenco, 1)]
        await update.message.reply_text(
            "🎛️ *Preset*\n\n" + "\n".join(righe) +
            "\n\nPer sceglierne uno: `/preset 2` oppure `/preset " + elenco[0]['nome'] +
            "`.\nSi modificano dalla pagina web.", parse_mode='Markdown')
        return
    scelta = " ".join(context.args)
    if scelta.isdigit() and 1 <= int(scelta) <= len(elenco):
        scelta = str(int(scelta) - 1)           # su Telegram si conta da 1
    ok, messaggio = await asyncio.to_thread(esegui_comando, 'preset', scelta, 'TELEGRAM')
    await update.message.reply_text(f"{'✅' if ok else '⚠️'} {messaggio}")

async def cmd_programma(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/programma: il programma settimanale degli obiettivi."""
    if not check_auth(update): return
    regole = pianificatore_instance.programma if pianificatore_instance else []
    if not regole:
        await update.message.reply_text("🗓️ Nessun programma settimanale. Si imposta dalla pagina web.")
        return
    nomi = ['lun', 'mar', 'mer', 'gio', 'ven', 'sab', 'dom']
    righe = [f"{'✅' if r['attivo'] else '⏸️'} {' '.join(nomi[g] for g in r['giorni'])}: "
             f"{r['kwh']:g} kWh entro le {r['ora']}" for r in regole]
    prossima = pianificatore_instance.prossima_del_programma()
    coda = (f"\n\nProssima: {prossima[1]['kwh']:g} kWh entro "
            f"{time.strftime('%d/%m %H:%M', time.localtime(prossima[0]))}" if prossima else "")
    await update.message.reply_text("🗓️ *Programma settimanale*\n\n" + "\n".join(righe) + coda +
                                    "\n\n_Si modifica dalla pagina web._", parse_mode='Markdown')

async def cmd_calma(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/calma [on|off]"""
    if not check_auth(update): return
    if not context.args:
        await update.message.reply_text(
            f"🌊 *Regolazione calma:* {'attiva' if CONFIG['REGOLAZIONE_CALMA'] else 'spenta'}\n"
            f"Un cambio di potenza solo se vale almeno {CONFIG['CALMA_SOGLIA_W']} W e ogni "
            f"{CONFIG['CALMA_INTERVALLO_S']} s.\n`/calma on` oppure `/calma off`", parse_mode='Markdown')
        return
    ok, messaggio = esegui_comando('calma', context.args[0], 'TELEGRAM')
    await update.message.reply_text(f"{'✅' if ok else '⚠️'} {messaggio}")

async def cmd_previsione(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update): return
    await update.message.reply_text(await asyncio.to_thread(testo_previsione), parse_mode='Markdown')

async def cmd_settimana(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update): return
    testo = await asyncio.to_thread(rapporto_settimanale)
    await update.message.reply_text(testo or "⏳ Non ci sono ancora dati per questa settimana.",
                                    parse_mode='Markdown')

async def cmd_obiettivo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/obiettivo <kWh> <HH:MM> | off"""
    if not check_auth(update): return
    if not context.args:
        await update.message.reply_text(
            f"🎯 {pianificatore_instance.descrizione() if pianificatore_instance else '?'}\n\n"
            "Usa: `/obiettivo 15 7:30` (15 kWh entro le 7:30) oppure `/obiettivo off`.\n"
            "Si carica col sole finché basta; se non basta per finire in tempo, "
            "si integra con la rete quanto serve.", parse_mode='Markdown')
        return
    ok, messaggio = esegui_comando('obiettivo', ' '.join(context.args), 'TELEGRAM')
    await update.message.reply_text(f"{'🎯' if ok else '⚠️'} {messaggio}")

async def cmd_set_prelevabile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update): return
    if not context.args:
        await update.message.reply_text(
            f"🛠️ *Potenza Prelevabile* attuale: {CONFIG['POTENZA_PRELEVABILE']} W\n"
            "Usa: `/prelevabile 1000`", parse_mode='Markdown')
        return
    ok, errore = esegui_comando('prelevabile', context.args[0], 'TELEGRAM')
    if ok:
        nota = ("" if CONFIG['MODALITA'] == 'misto' else
                f"\n_Vale nella modalità Sole + rete (ora: {pianificatore_instance.nome_modalita() if pianificatore_instance else CONFIG['MODALITA']})._")
        await update.message.reply_text(
            f"✅ *Potenza Prelevabile* impostata a {CONFIG['POTENZA_PRELEVABILE']} W{nota}", parse_mode='Markdown')
    else:
        await update.message.reply_text(f"⚠️ {errore}\nEsempio: `/prelevabile 1000`", parse_mode='Markdown')

async def cmd_set_protezione(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update): return
    if not context.args:
        await update.message.reply_text(
            f"🛡️ *Potenza Protezione* attuale: {CONFIG['POTENZA_PROTEZIONE']} W\n"
            "Usa: `/protezione 300`", parse_mode='Markdown')
        return
    ok, errore = applica_impostazione('POTENZA_PROTEZIONE', context.args[0], 'TELEGRAM')
    if ok:
        await update.message.reply_text(
            f"✅ *Potenza Protezione* impostata a {CONFIG['POTENZA_PROTEZIONE']} W", parse_mode='Markdown')
    else:
        await update.message.reply_text(f"⚠️ {errore}\nEsempio: `/protezione 300`", parse_mode='Markdown')

async def cmd_limite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Imposta l'energia da caricare in kWh (slider 'Limite' della centralina)."""
    if not check_auth(update): return
    if not context.args:
        msg = f"🔋 *Limite sulla centralina:* {descrizione_limite()}\n"
        if not CONFIG['CMD_LIMITE_TEMPLATE']:
            msg += "\n⚠️ _Comando non ancora configurato: il valore è salvato ma non inviato alla centralina._\n"
        msg += "\nUsa: `/limite 50`"
        await update.message.reply_text(msg, parse_mode='Markdown')
        return

    ok, errore = applica_impostazione('LIMITE_KWH', context.args[0], 'TELEGRAM')
    if not ok:
        await update.message.reply_text(f"⚠️ {errore}\nEsempio: `/limite 50` (1-100)", parse_mode='Markdown')
        return

    if wallbox_instance:
        inviato, dettaglio = await asyncio.to_thread(wallbox_instance.set_limite_kwh, CONFIG['LIMITE_KWH'])
        await update.message.reply_text(
            f"{'✅' if inviato else '⚠️'} *Limite:* {CONFIG['LIMITE_KWH']} kWh\n{dettaglio}", parse_mode='Markdown')
    else:
        await update.message.reply_text(f"✅ *Limite* salvato: {CONFIG['LIMITE_KWH']} kWh", parse_mode='Markdown')

async def cmd_algoritmo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra o cambia l'algoritmo di regolazione (classico | pid)."""
    if not check_auth(update): return
    if not context.args:
        await update.message.reply_text(
            f"🧠 *Algoritmo:* {CONFIG['ALGORITMO']}\n"
            "Usa: `/algoritmo pid` oppure `/algoritmo classico`", parse_mode='Markdown')
        return
    ok, errore = applica_impostazione('ALGORITMO', context.args[0], 'TELEGRAM')
    if ok:
        await update.message.reply_text(f"✅ *Algoritmo* impostato a {CONFIG['ALGORITMO']}", parse_mode='Markdown')
    else:
        await update.message.reply_text(f"⚠️ {errore}", parse_mode='Markdown')

async def cmd_pid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stato del PID; con argomenti ne cambia un parametro: /pid ki 0.2"""
    if not check_auth(update): return
    parametri = {'kp': 'PID_KP', 'ki': 'PID_KI', 'kd': 'PID_KD', 'margine': 'PID_MARGINE'}
    if len(context.args) == 2:
        chiave = parametri.get(context.args[0].lower())
        if chiave is None:
            await update.message.reply_text("⚠️ Parametri: kp, ki, kd, margine. Esempio: `/pid ki 0.2`",
                                            parse_mode='Markdown')
            return
        ok, errore = applica_impostazione(chiave, context.args[1], 'TELEGRAM')
        if not ok:
            await update.message.reply_text(f"⚠️ {errore}", parse_mode='Markdown')
            return

    msg = (f"🧠 *PID* ({'attivo' if CONFIG['ALGORITMO'] == 'pid' else 'non attivo'})\n"
           f"Kp {CONFIG['PID_KP']}  Ki {CONFIG['PID_KI']}  Kd {CONFIG['PID_KD']}\n"
           f"Margine: {CONFIG['PID_MARGINE']} W\n")
    stima = regolatore_pid_instance.ultima_stima if regolatore_pid_instance else None
    if stima:
        segno = "esporto" if stima['scambio'] >= 0 else "importo"
        msg += (f"\n📐 *Stima* ({time.time() - stima['t']:.0f}s fa)\n"
                f"Scambio: {segno} {abs(stima['scambio'])} W "
                f"(correzione {stima['correzione']:+d} W)\n"
                f"Disponibile per l'auto: {stima['disponibile']} W\n")
        if stima['tetto_auto'] is not None:
            msg += f"L'auto assorbe meno del concesso: al massimo {stima['tetto_auto']} W\n"
    await update.message.reply_text(msg, parse_mode='Markdown')

async def cmd_storico(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Riepilogo degli ultimi giorni + grafico a barre."""
    if not check_auth(update): return
    try:
        quanti = max(2, min(30, int(context.args[0]))) if context.args else 7
    except (IndexError, ValueError):
        quanti = 7

    giorni = giorni_storico(quanti)
    if len(giorni) < 1:
        await update.message.reply_text("⏳ Non c'è ancora storico giornaliero.")
        return

    tot_fv = sum(g.get('wallbox_da_fv_kwh') or 0 for g in giorni)
    tot_wb = sum(g.get('wallbox_kwh') or 0 for g in giorni)
    tot_sol = sum(g.get('solare_kwh') or 0 for g in giorni)
    tot_eur = sum(g.get('risparmio_eur') or 0 for g in giorni)
    eff = (tot_fv / tot_wb * 100) if tot_wb > 0 else None

    righe = []
    for g in giorni[-10:]:
        d = g.get('giorno', '?')[5:]     # MM-GG
        e = g.get('efficienza')
        righe.append(f"`{d}`  ☀️{g.get('solare_kwh', 0):5.1f}  🚗{g.get('wallbox_kwh', 0):5.1f}  "
                     f"{'🌱' + format(e, '.0f') + '%' if e is not None else '  —'}")

    msg = (f"📅 *Ultimi {len(giorni)} giorni*\n\n" + "\n".join(righe) +
           f"\n\n*Totali*\n"
           f"☀️ Prodotti: {tot_sol:.1f} kWh\n"
           f"🚗 In auto: {tot_wb:.1f} kWh\n"
           f"🌱 Da fotovoltaico: {tot_fv:.1f} kWh"
           f"{f' ({eff:.0f}%)' if eff is not None else ''}\n"
           f"💰 Risparmio stimato: {tot_eur:.2f} €\n")
    await update.message.reply_text(msg, parse_mode='Markdown')

    if len(giorni) >= 2:
        immagine = await asyncio.to_thread(genera_grafico_giorni, giorni)
        await update.message.reply_photo(photo=immagine)

def genera_grafico_giorni(giorni):
    """Barre impilate: kWh in auto da fotovoltaico vs da rete, per giorno."""
    etichette = [g.get('giorno', '')[5:] for g in giorni]
    da_fv = [g.get('wallbox_da_fv_kwh') or 0 for g in giorni]
    da_rete = [max(0, (g.get('wallbox_kwh') or 0) - f) for g, f in zip(giorni, da_fv)]
    solare = [g.get('solare_kwh') or 0 for g in giorni]

    fig = Figure(figsize=(11, 5), dpi=110)
    fig.patch.set_facecolor('#ffffff')
    ax = fig.subplots()
    ax.set_facecolor('#fbfbfd')

    ax.bar(etichette, da_fv, label='In auto da fotovoltaico', color='#10b981')
    ax.bar(etichette, da_rete, bottom=da_fv, label='In auto da rete', color='#f59e0b')
    ax.plot(etichette, solare, label='Prodotti dal FV', color='#3b82f6',
            linewidth=2, marker='o', markersize=4)

    ax.set_title(f"Ricarica degli ultimi {len(giorni)} giorni", fontsize=13, fontweight='bold')
    ax.set_ylabel("kWh")
    ax.legend(loc='upper left', framealpha=.9, fontsize=9)
    ax.grid(True, axis='y', linestyle='--', alpha=.35)
    ax.tick_params(axis='x', rotation=45, labelsize=9)
    fig.tight_layout()

    buf = io.BytesIO()
    FigureCanvasAgg(fig).print_png(buf)
    buf.seek(0)
    return buf

async def cmd_sessioni(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ultime sessioni di ricarica registrate."""
    if not check_auth(update): return
    sessioni = list(reversed(leggi_sessioni(10)))

    if contatori_instance and contatori_instance.inizio_carica:
        minuti = (time.time() - contatori_instance.inizio_carica) / 60
        kwh = contatori_instance.kwh_sessione()
        nota = "" if contatori_instance.sessione.get('misurata') else " (stimata)"
        corso = f"▶️ *In corso:* {minuti:.0f} min, {kwh:.2f} kWh{nota}\n\n"
    else:
        corso = ""

    if not sessioni:
        await update.message.reply_text(
            corso + "📭 Nessuna sessione registrata finora.", parse_mode='Markdown')
        return

    righe = []
    for s in sessioni:
        quando = time.strftime('%d/%m %H:%M', time.localtime(s['inizio']))
        fv = f" · 🌱{s['quota_fv']:.0f}%" if s.get('quota_fv') is not None else ""
        righe.append(f"`{quando}`  {s['minuti']}min · {s['kwh']:.2f} kWh{fv}")

    tot = sum(s['kwh'] for s in sessioni)
    eur = sum(s.get('risparmio_eur') or 0 for s in sessioni)
    await update.message.reply_text(
        corso + f"🔌 *Ultime {len(sessioni)} sessioni*\n\n" + "\n".join(righe) +
        f"\n\nTotale: {tot:.2f} kWh · risparmio stimato {eur:.2f} €",
        parse_mode='Markdown')

async def cmd_fase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Forza subito una rilettura di 'tfase' dalla centralina."""
    if not check_auth(update): return
    if not wallbox_instance:
        await update.message.reply_text("❌ Controller Wallbox non disponibile.")
        return

    await update.message.reply_text("🔍 Lettura configurazione centralina...")
    # to_thread: l'I/O sincrono non deve bloccare il loop del bot
    esito = await asyncio.to_thread(wallbox_instance.sync_fase)
    if esito == 'errore':
        await update.message.reply_text("❌ Centralina non raggiungibile.")
        return

    modalita = "Trifase" if wallbox_instance.fase == 1 else "Monofase"
    min_p, max_p = wallbox_instance.limiti_potenza()
    await update.message.reply_text(
        f"⚙️ *Modalità:* {modalita}\n"
        f"📏 *Limiti:* {min_p} - {max_p} W\n"
        f"_({'cambiata ora' if esito == 'cambiata' else 'invariata'})_", parse_mode='Markdown')

async def cmd_energia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Riepilogo energetico della giornata + efficienza di carica."""
    if not check_auth(update): return
    if not contatori_instance:
        await update.message.reply_text("⏳ Contatori non ancora inizializzati.")
        return
    await update.message.reply_text(contatori_instance.riepilogo_markdown(), parse_mode='Markdown')

async def cmd_grafici(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update): return
    
    # Finestra temporale: /grafici [15m|1h|6h|24h], default 1h.
    # Prima veniva passato TUTTO il buffer (fino a 10000 punti, ~28 ore) con
    # etichette "%H:%M:%S" senza data: matplotlib le trattava come CATEGORIE,
    # quindi le 20:01 di ieri e di oggi finivano nella stessa colonna e la
    # linea rimbalzava avanti e indietro, creando una ragnatela illeggibile.
    scelta = (context.args[0].lower() if context.args else '1h')
    if scelta not in INTERVALLI:
        await update.message.reply_text(
            "⚠️ Intervallo non valido. Usa: `/grafici 15m`, `1h`, `6h`, `24h` o `7g`", parse_mode='Markdown')
        return

    punti = serie_storico(INTERVALLI[scelta], max_punti=400)
    if len(punti) < 2:
        await update.message.reply_text(
            f"⏳ Non ci sono ancora abbastanza dati per l'intervallo {scelta}. Riprova tra poco.")
        return

    await update.message.reply_text(f"📊 Generazione grafico ({scelta})...")
    immagine = await asyncio.to_thread(genera_grafico, punti, scelta)
    await update.message.reply_photo(photo=immagine)

def genera_grafico(punti, etichetta):
    """Disegna il grafico e lo restituisce come PNG in memoria.

    API a oggetti invece di pyplot: la figura non entra nel registro globale
    di pyplot, quindi non serve close() e un'eccezione non lascia figure
    orfane in RAM a ogni chiamata (era un memory leak).
    """
    # datetime reali sull'asse X, non stringhe: matplotlib li posiziona in
    # scala temporale e i punti di giorni diversi non collidono piu'.
    x = [datetime.datetime.fromtimestamp(p['time']) for p in punti]
    grid = [p['grid'] for p in punti]
    solar = [p['solar'] for p in punti]
    wb = [p['wb'] for p in punti]

    fig = Figure(figsize=(11, 5.5), dpi=110)
    fig.patch.set_facecolor('#ffffff')
    ax = fig.subplots()
    ax.set_facecolor('#fbfbfd')

    ax.fill_between(x, solar, color='#10b981', alpha=0.18)
    ax.plot(x, solar, label='Produzione solare', color='#10b981', linewidth=2)
    ax.fill_between(x, wb, color='#3b82f6', alpha=0.14)
    ax.plot(x, wb, label='Potenza wallbox', color='#3b82f6', linewidth=2)
    ax.plot(x, grid, label='Consumo rete', color='#ef4444', linewidth=1.8)

    durata = punti[-1]['time'] - punti[0]['time']
    ax.set_title(f"Andamento energetico — ultime {etichetta}", fontsize=13, fontweight='bold')
    ax.set_ylabel("Watt (W)")
    ax.legend(loc="upper left", framealpha=0.9)
    ax.grid(True, linestyle='--', alpha=0.35)
    ax.set_ylim(bottom=0)

    # Formato dell'ora scelto in base alla durata: oltre le 24h serve il giorno
    if durata > 86400:
        formato = '%d/%m %H:%M'
    elif durata > 3600:
        formato = '%H:%M'
    else:
        formato = '%H:%M:%S'
    ax.xaxis.set_major_formatter(DateFormatter(formato))
    ax.xaxis.set_major_locator(MaxNLocator(8))
    ax.tick_params(axis='x', rotation=30, labelsize=9)
    for et in ax.get_xticklabels():
        et.set_horizontalalignment('right')
    fig.tight_layout()

    buf = io.BytesIO()
    FigureCanvasAgg(fig).print_png(buf)
    buf.seek(0)
    return buf

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Re-inizializzazione completa della centralina (ex /restart)."""
    if not check_auth(update): return
    if not wallbox_instance:
        await update.message.reply_text("❌ *Errore:* Controller Wallbox non disponibile.", parse_mode='Markdown')
        return

    log_msg("[TELEGRAM] Richiesta manuale di re-inizializzazione Wallbox!")
    await update.message.reply_text("🔄 *Re-inizializzazione avviata...*", parse_mode='Markdown')
    # to_thread: initialize() fa HTTP sincrono + sleep, non deve bloccare il bot
    await asyncio.to_thread(wallbox_instance.initialize)
    modalita = "Trifase" if wallbox_instance.fase == 1 else "Monofase"
    await update.message.reply_text(
        f"✅ *Re-inizializzazione completata*\n⚙️ Modalità rilevata: {modalita}", parse_mode='Markdown')

async def gestore_errori_telegram(update, context):
    """Gestore errori di python-telegram-bot.

    Senza, PTB scrive 'No error handlers are registered' e riversa nel log un
    traceback completo a ogni singhiozzo di rete (visto: un 502 'Bad Gateway'
    notturno). Il suo retry loop si riprende da solo, quindi basta una riga.
    """
    errore = context.error
    nome = type(errore).__name__
    if nome in ('NetworkError', 'TimedOut', 'RetryAfter'):
        log_throttled(f'telegram_{nome}', f"[TELEGRAM] Problema di rete ({nome}): {errore}. "
                                          f"Riprovo automaticamente.", 600)
    else:
        log_msg(f"[TELEGRAM] Errore non gestito ({nome}): {errore}")

def _registra_comandi(app):
    """Registra gli handler. I nomi con maiuscole sono rifiutati da PTB v20+
    (regex ^[\\da-z_]{1,32}$) e farebbero morire l'intero bot: vengono quindi
    registrati a parte, tollerando il fallimento."""
    handlers = [
        (["start", "help"], cmd_help),
        (["info"], cmd_info),
        (["accendi"], cmd_accendi),
        (["spegni"], cmd_spegni),
        (["prelevabile", "setpotenzaprelevabile"], cmd_set_prelevabile),
        (["protezione", "setpotenzaprotezione"], cmd_set_protezione),
        (["limite"], cmd_limite),
        (["algoritmo"], cmd_algoritmo),
        (["modalita"], cmd_modalita),
        (["obiettivo"], cmd_obiettivo),
        (["preset"], cmd_preset),
        (["programma"], cmd_programma),
        (["calma"], cmd_calma),
        (["previsione"], cmd_previsione),
        (["settimana"], cmd_settimana),
        (["pid"], cmd_pid),
        (["fase"], cmd_fase),
        (["energia"], cmd_energia),
        (["storico"], cmd_storico),
        (["sessioni"], cmd_sessioni),
        (["grafici"], cmd_grafici),
        (["reset", "restart"], cmd_reset),
    ]
    for nomi, funzione in handlers:
        try:
            app.add_handler(CommandHandler(nomi, funzione))
        except Exception as e:
            log_msg(f"[TELEGRAM] Handler {nomi} non registrato: {e}")

    # Alias storici in CamelCase: se la versione di PTB li rifiuta, si perde
    # solo l'alias, non il bot.
    for nome, funzione in (("setPotenzaPrelevabile", cmd_set_prelevabile),
                           ("setPotenzaProtezione", cmd_set_protezione)):
        try:
            app.add_handler(CommandHandler(nome, funzione))
        except Exception:
            log_msg(f"[TELEGRAM] Alias '{nome}' non supportato da questa versione di PTB (usa la forma minuscola).")

def run_telegram_polling():
    """Avvia il polling di Telegram in un thread separato, con retry."""
    if not API_KEY:
        log_msg("[TELEGRAM] API_KEY mancante. Bot disabilitato.")
        return

    # In un thread non principale non esiste un event loop corrente: PTB
    # chiamerebbe get_event_loop() e solleverebbe RuntimeError, uccidendo il
    # thread in silenzio. Lo creiamo esplicitamente.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    attesa = 5
    while True:
        try:
            app = Application.builder().token(API_KEY).build()
            _registra_comandi(app)
            app.add_error_handler(gestore_errori_telegram)
            log_msg(">>> BOT TELEGRAM ATTIVO. In attesa di comandi... <<<")
            attesa = 5
            # stop_signals=None evita conflitti di segnali con il thread principale
            app.run_polling(stop_signals=None, close_loop=False)
            log_msg("[TELEGRAM] Polling terminato.")
            return
        except Exception as e:
            # Senza questo, una caduta di rete all'avvio spegneva il bot per sempre
            log_msg(f"[TELEGRAM] ERRORE nel polling: {e}. Riprovo tra {attesa}s.")
            time.sleep(attesa)
            attesa = min(attesa * 2, 300)

# -----------------------------------------------------------
# INTERFACCIA WEB
# -----------------------------------------------------------
# La pagina sta in web/ (index.html, app.css, app.js, scena3d.js e le
# librerie in web/vendor/): niente piu' HTML dentro questo file, e niente
# CDN, cosi' funziona anche senza internet. Flask la serve da /static.
_pagine = {}      # nome del file -> (versione, html)

def versione_web():
    """Cambia quando cambia un file della pagina: finisce negli indirizzi
    (?v=...) cosi' dopo un deploy il browser non usa i file vecchi."""
    try:
        return str(int(max(os.path.getmtime(os.path.join(DIR_WEB, f))
                           for f in os.listdir(DIR_WEB)
                           if os.path.isfile(os.path.join(DIR_WEB, f)))))
    except (OSError, ValueError):
        return '0'

def servi_pagina(nome):
    """Una pagina di web/, con __V__ sostituito dalla versione dei file."""
    versione = versione_web()
    pagina = _pagine.get(nome)
    if pagina is None or pagina[0] != versione:
        try:
            with open(os.path.join(DIR_WEB, nome), 'r', encoding='utf-8') as f:
                pagina = _pagine[nome] = (versione, f.read().replace('__V__', versione))
        except OSError as e:
            return f"Pagina non disponibile: {e}", 500
    risposta = make_response(pagina[1])
    risposta.headers['Cache-Control'] = 'no-cache'
    return risposta

@app.route('/')
def index():
    return servi_pagina('index.html')

@app.route('/statistiche')
def pagina_statistiche():
    return servi_pagina('statistiche.html')

# Mappa nomi campo web -> chiavi CONFIG, cosi' la UI non conosce i nomi interni
CAMPI_WEB = {
    'prelevabile': 'POTENZA_PRELEVABILE',
    'protezione':  'POTENZA_PROTEZIONE',
    'limite':      'LIMITE_KWH',
    'prezzo_acquisto': 'PREZZO_ACQUISTO',
    'prezzo_vendita':  'PREZZO_VENDITA',
    'algoritmo':       'ALGORITMO',
    'calma':           'REGOLAZIONE_CALMA',
    'calma_soglia':    'CALMA_SOGLIA_W',
    'calma_intervallo': 'CALMA_INTERVALLO_S',
    'rapporto_settimanale': 'RAPPORTO_SETTIMANALE',
    'pid_kp':          'PID_KP',
    'pid_ki':          'PID_KI',
    'pid_kd':          'PID_KD',
    'pid_margine':     'PID_MARGINE',
}

_picco = {'t': 0.0, 'w': 0}

def picco_solare():
    """Produzione massima vista nello storico in memoria (~7 giorni). Serve
    alla scena 3D per capire se e' nuvoloso. Ricalcolata ogni 10 minuti."""
    adesso = time.time()
    if adesso - _picco['t'] > 600:
        _picco['t'] = adesso
        _picco['w'] = int(max((p[1] for p in list(SYSTEM_STATE['ULTIME_LETTURE_FASI'])), default=0))
    return _picco['w']

@app.route('/api/data')
def get_data():
    """Stato attuale. La pagina lo chiede ogni 2 s, quindi con due parametri
    si fa mandare solo il necessario:
      storia=0   senza l'ultima ora di storico (la prende da /api/storico)
      log_da=N   solo le righe di log dopo la numero N (log_seq della
                 risposta precedente)
    Senza parametri risponde con tutto, come ha sempre fatto."""
    con_storia = request.args.get('storia', '1') != '0'
    log_da = request.args.get('log_da', type=int)
    # serie_storico ordina e sottocampiona: il buffer mescola i campioni
    # ricaricati da disco con quelli nuovi.
    history = serie_storico(3600, max_punti=240) if con_storia else None

    with STATO_LOCK:
        fasi = list(SYSTEM_STATE['MONITOR_FASI'])
        stato = {
            'server_time': time.time(),
            'wb_on': SYSTEM_STATE['WALLBOX_STATUS'],
            'wb_power': SYSTEM_STATE['WALLBOX_POWER'] if SYSTEM_STATE['WALLBOX_STATUS'] else 0,
            'fase_mode': SYSTEM_STATE['IMPIANTO_FASE'],
            'last_fasi': SYSTEM_STATE['ULTIMA_LETTURA_FASI'],
            'last_solar': SYSTEM_STATE['ULTIMA_LETTURA_SOLARE'],
            'fasi': fasi,
            'grid_total': sum(fasi[0:3]),
            'solar_total': sum(fasi[3:6]),
            'centralina_online': SYSTEM_STATE['CENTRALINA_ONLINE'],
            'sensore_online': SYSTEM_STATE['SENSORE_ONLINE'],
            'manual_off': bool(wallbox_instance and wallbox_instance.manual_off),
            'auto_collegata': bool(wallbox_instance and wallbox_instance.auto_collegata),
            'in_carica': bool(wallbox_instance and wallbox_instance.in_carica),
            'limite_supportato': bool(CONFIG['CMD_LIMITE_TEMPLATE']),
            # Dati letti direttamente dalla centralina
            'wb_desc': SYSTEM_STATE['WB_DESC'],
            'wb_status': SYSTEM_STATE['WB_STATUS'],
            'wb_pcar': SYSTEM_STATE['WB_PCAR'],
            'wb_energia_sessione': round(SYSTEM_STATE['WB_ENERGIA_SESSIONE'], 2),
            'wb_energia_misurata': SYSTEM_STATE['WB_ENERGIA_MISURATA'],
            'wb_tempo_sessione': SYSTEM_STATE['WB_TEMPO_SESSIONE'],
            'wb_temp_presa': SYSTEM_STATE['WB_TEMP_PRESA'],
            'wb_alg': SYSTEM_STATE['WB_ALG'],
            'alg_manuale': SYSTEM_STATE['WB_ALG'] in ('', CONFIG['ALG_MANUALE']),
            'limite_kwh_centralina': SYSTEM_STATE.get('LIMITE_KWH_CENTRALINA'),
            'picco_solare_w': picco_solare(),
        }
        minimo, massimo = (wallbox_instance.limiti_potenza() if wallbox_instance
                           else (CONFIG['MONOFASE_MIN_POWER'], CONFIG['MONOFASE_MAX_POWER']))
        configurazione = {
            'prelevabile': CONFIG['POTENZA_PRELEVABILE'],
            'modalita': CONFIG['MODALITA'],
            'limite_contatore': CONFIG['LIMITE_CONTATORE'],
            'margine_contatore': CONFIG['MARGINE_CONTATORE'],
            'protezione': CONFIG['POTENZA_PROTEZIONE'],
            'limite': CONFIG['LIMITE_KWH'],
            'prezzo_acquisto': CONFIG['PREZZO_ACQUISTO'],
            'prezzo_vendita': CONFIG['PREZZO_VENDITA'],
            'algoritmo': CONFIG['ALGORITMO'],
            'calma': CONFIG['REGOLAZIONE_CALMA'],
            'calma_soglia': CONFIG['CALMA_SOGLIA_W'],
            'calma_intervallo': CONFIG['CALMA_INTERVALLO_S'],
            'rapporto_settimanale': CONFIG['RAPPORTO_SETTIMANALE'],
            'potenza_minima': minimo,
            'potenza_massima': massimo,
        }
        stato['stima_pid'] = regolatore_pid_instance.ultima_stima if regolatore_pid_instance else None
    piano = stato_controlli() if pianificatore_instance else None
    if piano is not None:
        previsione = dict(_previsione['riepilogo'])
        previsione.pop('ore', None)         # ora per ora solo su /api/previsione
        piano['previsione'] = previsione

    # Log: tutte le righe, oppure solo quelle dopo log_da. Se la pagina e'
    # piu' avanti di noi (servizio riavviato) o troppo indietro, si rimanda
    # tutto e lo si dice (logs_completi).
    righe = list(SYSTEM_STATE['LOGS'])
    seq = SYSTEM_STATE['LOG_SEQ']
    completi = True
    if log_da is not None and 0 <= seq - log_da <= len(righe):
        righe = righe[len(righe) - (seq - log_da):]
        completi = False

    risposta = {
        'config': configurazione,
        'status': stato,
        'energia': contatori_instance.riepilogo() if contatori_instance else {},
        'piano': piano,
        'logs': righe,
        'log_seq': seq,
        'logs_completi': completi,
    }
    if con_storia:
        risposta['history'] = history
    return jsonify(risposta)

@app.route('/api/storico')
def get_storico():
    """Storico esteso, ordinato e sottocampionato lato server (max ~300 punti)."""
    scelta = request.args.get('range', '1h')
    if scelta not in INTERVALLI:
        return jsonify({'success': False, 'error': f"range non valido: {scelta}"}), 400
    return jsonify({'success': True, 'range': scelta,
                    'punti': serie_storico(INTERVALLI[scelta])})

@app.route('/api/settings', methods=['POST'])
def update_settings():
    """Salva le impostazioni. Validazione ALL-OR-NOTHING.

    Prima un campo invalido faceva int(None) -> TypeError -> HTTP 500, e
    l'altro campo NON veniva salvato. Ora si valida tutto prima di applicare
    qualsiasi cosa e si risponde 400 con il dettaglio dell'errore.
    """
    dati = request.get_json(silent=True)
    if not isinstance(dati, dict):
        return jsonify({'success': False, 'errori': ['Corpo della richiesta non valido']}), 400

    puliti, errori = {}, []
    for campo, valore in dati.items():
        chiave = CAMPI_WEB.get(campo)
        if chiave is None:
            errori.append(f"Parametro sconosciuto: {campo}")
            continue
        ok, pulito, errore = valida_valore(chiave, valore)
        if ok:
            puliti[chiave] = pulito
        else:
            errori.append(errore)

    if errori:
        return jsonify({'success': False, 'errori': errori}), 400
    if not puliti:
        return jsonify({'success': False, 'errori': ['Nessun parametro da aggiornare']}), 400

    with STATO_LOCK:
        CONFIG.update(puliti)
    salva_config()
    log_msg("[WEB] Parametri aggiornati: " + ", ".join(f"{k}={v}" for k, v in puliti.items()))

    if ponte_mqtt_instance and pianificatore_instance:
        ponte_mqtt_instance.pubblica_controlli(stato_controlli())

    # Il limite kWh va anche inoltrato alla centralina
    if 'LIMITE_KWH' in puliti and wallbox_instance:
        wallbox_instance.set_limite_kwh(puliti['LIMITE_KWH'])

    return jsonify({'success': True, 'applicati': puliti})

@app.route('/api/comando', methods=['POST'])
def api_comando():
    """Comandi di carica dalla pagina: {nome: modalita|prelevabile|obiettivo,
    valore: ...}. Stessa funzione di Telegram e Home Assistant."""
    dati = request.get_json(silent=True)
    if not isinstance(dati, dict) or not isinstance(dati.get('nome'), str):
        return jsonify({'success': False, 'error': 'Corpo della richiesta non valido'}), 400
    ok, messaggio = esegui_comando(dati['nome'], dati.get('valore'), 'WEB')
    return jsonify({'success': ok, 'messaggio': messaggio,
                    'piano': stato_controlli()}), (200 if ok else 400)

@app.route('/api/preset', methods=['POST'])
def api_preset():
    """Salva l'elenco dei preset: {preset: [{nome, prelevabile, limite_kwh}, ...]}"""
    dati = request.get_json(silent=True)
    if not isinstance(dati, dict):
        return jsonify({'success': False, 'messaggio': 'Corpo della richiesta non valido'}), 400
    ok, messaggio = salva_preset(dati.get('preset'), 'WEB')
    return jsonify({'success': ok, 'messaggio': messaggio,
                    'piano': stato_controlli()}), (200 if ok else 400)

@app.route('/api/programma', methods=['POST'])
def api_programma():
    """Salva il programma settimanale: {programma: [{giorni, ora, kwh, attivo}, ...]}"""
    dati = request.get_json(silent=True)
    if not isinstance(dati, dict) or pianificatore_instance is None:
        return jsonify({'success': False, 'messaggio': 'Richiesta non valida'}), 400
    ok, messaggio = pianificatore_instance.imposta_programma(dati.get('programma'))
    if ok:
        log_msg(f"[WEB] {messaggio}")
        if ponte_mqtt_instance:
            ponte_mqtt_instance.pubblica_controlli(stato_controlli())
    return jsonify({'success': ok, 'messaggio': messaggio,
                    'piano': stato_controlli()}), (200 if ok else 400)

@app.route('/api/previsione')
def api_previsione():
    """Sole previsto per il resto di oggi e per domani, ora per ora."""
    return jsonify(aggiorna_previsione())

@app.route('/api/limite_toggle', methods=['POST'])
def toggle_limite():
    """Attiva/disattiva il limite kWh.

    Il pulsante fisico della centralina (btn=l) e' un toggle stateful: da un
    bottone web stateless e' rischioso (un doppio invio lo rimette nello
    stato sbagliato). Qui si usa invece L<valore>/L0, entrambi deterministici.

    Se il body include 'kwh', e' il valore corrente scelto nello slider (non
    ancora salvato con "Salva Impostazioni"): l'attivazione lo usa e lo rende
    anche il nuovo CONFIG['LIMITE_KWH'] persistito, cosi' la posizione dello
    slider al momento del click e' sempre quella che finisce sulla centralina,
    non un valore vecchio salvato in precedenza.
    """
    if not wallbox_instance:
        return jsonify({'success': False, 'error': 'Controller non disponibile'}), 503
    if not CONFIG.get('CMD_LIMITE_TEMPLATE'):
        return jsonify({'success': False, 'error': 'Comando limite non configurato'}), 400

    dati = request.get_json(silent=True)
    if not isinstance(dati, dict) or 'attivo' not in dati:
        return jsonify({'success': False, 'error': "Parametro 'attivo' mancante"}), 400

    attivo = bool(dati['attivo'])
    if attivo:
        ok, kwh_pulito, errore = valida_valore('LIMITE_KWH', dati.get('kwh', CONFIG['LIMITE_KWH']))
        if not ok:
            return jsonify({'success': False, 'error': errore}), 400
        with STATO_LOCK:
            CONFIG['LIMITE_KWH'] = kwh_pulito
        salva_config()
        valore = kwh_pulito
    else:
        valore = 0

    inviato, messaggio = wallbox_instance.set_limite_kwh(valore)
    return jsonify({'success': inviato, 'messaggio': messaggio, 'attivo': attivo}), (200 if inviato else 502)

@app.route('/api/giorni')
def get_giorni():
    """Storico giornaliero: finora veniva scritto su disco ma mai mostrato."""
    try:
        quanti = max(1, min(90, int(request.args.get('quanti', 14))))
    except (TypeError, ValueError):
        quanti = 14
    return jsonify({'success': True, 'giorni': giorni_storico(quanti)})

def campioni_memoria(da=0):
    """I campioni in RAM nel formato di statistiche.py:
    (ts, consumo, sole, auto, fasi)."""
    return [(p[3], p[0], p[1], p[4] if len(p) > 4 else 0, p[2])
            for p in list(SYSTEM_STATE['ULTIME_LETTURE_FASI']) if p[3] >= da]

def archivia_statistiche():
    """Chiude nell'archivio delle statistiche i giorni passati che mancano
    (di norma: ieri, appena dopo mezzanotte)."""
    aggiunti = archivio_statistiche.archivia(campioni_memoria())
    if aggiunti:
        log_msg(f"[STATISTICHE] Archiviati: {', '.join(aggiunti)}")

_stat_oggi = {'t': 0.0, 'record': None}

def giorni_statistiche():
    """L'archivio dei giorni chiusi piu' la giornata in corso, ricalcolata
    dai campioni in memoria al massimo una volta al minuto."""
    giorni = dict(archivio_statistiche.giorni())
    adesso = time.time()
    oggi = statistiche.giorno_di(adesso)
    if adesso - _stat_oggi['t'] > 60:
        # due ore di contesto prima di mezzanotte: servono a capire se un
        # valore dell'auto fermo da ieri sera e' una carica vera
        mezzanotte = time.mktime(time.localtime(adesso)[:3] + (0, 0, 0, 0, 0, -1))
        recenti = statistiche.aggrega(campioni_memoria(mezzanotte - 7200))
        _stat_oggi.update(t=adesso, record=recenti.get(oggi))
    if _stat_oggi['record'] and _stat_oggi['record']['g'] == oggi:
        giorni[oggi] = _stat_oggi['record']
    return giorni, oggi

def previsioni_salvate():
    """{giorno: {'kwh', 'fonte'}}: la previsione fatta la sera prima."""
    previsioni = {}
    try:
        with open(FILE_PREVISIONI, encoding='utf-8') as f:
            for riga in f:
                try:
                    r = json.loads(riga)
                    previsioni[r['g']] = r
                except (ValueError, KeyError, TypeError):
                    continue
    except OSError:
        pass
    return previsioni

def salva_previsione_di_domani():
    """Alle 20 si mette da parte quanto ci si aspetta per domani: la pagina
    Statistiche lo confronta poi con quello che il tetto ha fatto davvero."""
    aggiorna_previsione(forza=True)
    domani = previsione_instance.domani()
    if domani is None or domani[0] in previsioni_salvate():
        return
    with open(FILE_PREVISIONI, 'a', encoding='utf-8') as f:
        f.write(json.dumps({'g': domani[0], 'kwh': domani[1], 'fonte': previsione_instance.fonte(),
                            'fatta': int(time.time())}, separators=(',', ':')) + '\n')
    log_msg(f"[PREVISIONE] Per domani ({domani[0]}): {domani[1]} kWh, fonte {previsione_instance.fonte()}")

def calcola_statistiche():
    giorni, oggi = giorni_statistiche()
    primo = min(giorni) if giorni else oggi
    da, a = statistiche.intervallo(request.args.get('periodo', '30g'), oggi, primo,
                                   request.args.get('da'), request.args.get('a'))
    stat = statistiche.calcola(
        giorni, da, a,
        {'acquisto': CONFIG['PREZZO_ACQUISTO'], 'vendita': CONFIG['PREZZO_VENDITA']},
        CONFIG['LIMITE_CONTATORE'], previsioni_salvate())
    stat['oggi'] = oggi
    stat['adesso'] = time.time()
    return stat

@app.route('/api/statistiche')
def api_statistiche():
    """Tutti i numeri della pagina Statistiche per un periodo:
    ?periodo=7g|30g|90g|365g|anno|tutto|AAAA-MM  oppure  ?da=AAAA-MM-GG&a=AAAA-MM-GG"""
    try:
        stat = calcola_statistiche()
    except ValueError as e:
        return jsonify({'success': False, 'messaggio': str(e)}), 400
    corpo = json.dumps(stat, separators=(',', ':')).encode('utf-8')
    compresso = 'gzip' in request.headers.get('Accept-Encoding', '')
    risposta = make_response(gzip.compress(corpo, 5) if compresso else corpo)
    risposta.headers['Content-Type'] = 'application/json'
    risposta.headers['Cache-Control'] = 'no-store'
    if compresso:
        risposta.headers['Content-Encoding'] = 'gzip'
        risposta.headers['Vary'] = 'Accept-Encoding'
    return risposta

@app.route('/api/statistiche.csv')
def api_statistiche_csv():
    """I giorni del periodo come tabella per il foglio di calcolo."""
    try:
        stat = calcola_statistiche()
    except ValueError as e:
        return str(e), 400
    periodo = stat.get('periodo') or {}
    risposta = make_response(statistiche.csv_giorni(stat))
    risposta.headers['Content-Type'] = 'text/csv; charset=utf-8'
    risposta.headers['Content-Disposition'] = (
        f"attachment; filename=energia_{periodo.get('da', '')}_{periodo.get('a', '')}.csv")
    return risposta

@app.route('/api/sessioni')
def get_sessioni():
    """Ultime sessioni di ricarica registrate."""
    try:
        quante = max(1, min(200, int(request.args.get('quante', 10))))
    except (TypeError, ValueError):
        quante = 10
    sessioni = list(reversed(leggi_sessioni(quante)))
    in_corso = None
    if contatori_instance and contatori_instance.inizio_carica:
        adesso = time.time()
        # 'kwh' e' l'energia di QUESTA sessione (delta del contatore), coerente
        # con le sessioni registrate; 'kwh_da_collegamento' e' il totale che la
        # centralina conta dall'inserimento del cavo, pause comprese.
        totale = SYSTEM_STATE.get('WB_ENERGIA_SESSIONE') or 0.0
        in_corso = {
            'minuti': round((adesso - contatori_instance.inizio_carica) / 60),
            'kwh': round(contatori_instance.kwh_sessione(), 2),
            'kwh_da_collegamento': round(totale, 2),
            'misurata': bool(SYSTEM_STATE.get('WB_ENERGIA_MISURATA')),
        }
    return jsonify({'success': True, 'sessioni': sessioni, 'in_corso': in_corso})

@app.route('/api/init_wallbox', methods=['POST'])
def force_init_wallbox():
    """Avvia la re-inizializzazione IN BACKGROUND.

    Prima girava dentro l'handler: HTTP sincrono + sleep(1) tenevano appesa la
    richiesta (e un worker Flask) per diversi secondi.
    """
    if not wallbox_instance:
        return jsonify({'success': False, 'error': 'Controller non disponibile'}), 503
    if SYSTEM_STATE.get('INIT_IN_CORSO'):
        return jsonify({'success': False, 'error': 'Inizializzazione già in corso'}), 409

    def _init():
        SYSTEM_STATE['INIT_IN_CORSO'] = True
        try:
            wallbox_instance.initialize()
        except Exception as e:
            log_msg(f"[ERRORE] Inizializzazione fallita: {e}")
        finally:
            SYSTEM_STATE['INIT_IN_CORSO'] = False

    log_msg("[WEB] Richiesta manuale di re-inizializzazione Wallbox!")
    threading.Thread(target=_init, daemon=True).start()
    return jsonify({'success': True, 'stato': 'avviata'}), 202

@app.route('/api/wallbox_raw')
def wallbox_raw():
    """DIAGNOSTICA (sola lettura): JSON grezzo della centralina + chiavi cambiate
    rispetto alla lettura precedente.

    Serve al reverse engineering del pulsante "Limite": aprire questa pagina,
    muovere lo slider dalla pagina della centralina, ricaricare, e leggere
    'chiavi_cambiate' per scoprire il nome esatto del campo. Non invia comandi.
    """
    try:
        risposta = requests.get(WALLBOX_URL, timeout=5)
        grezzo = risposta.json()
    except requests.exceptions.RequestException as e:
        return jsonify({'ok': False, 'errore': f"Centralina non raggiungibile: {e}"}), 502
    except ValueError:
        return jsonify({'ok': False, 'errore': 'Risposta non JSON'}), 502

    precedente = SYSTEM_STATE.get('WALLBOX_RAW_PRECEDENTE') or {}
    cambiate = {k: [precedente.get(k), v] for k, v in grezzo.items() if precedente.get(k) != v}
    SYSTEM_STATE['WALLBOX_RAW_PRECEDENTE'] = grezzo

    return jsonify({
        'ok': True,
        'letto_il': time.strftime('%Y-%m-%d %H:%M:%S'),
        'chiavi_cambiate': cambiate if precedente else {},
        'grezzo': grezzo,
    })

def run_flask():
    app.run(host='0.0.0.0', port=CONFIG['PORT'], debug=False, use_reloader=False)

# -----------------------------------------------------------
# GESTORE WALLBOX E CLASSI SOTTOSTANTI
# -----------------------------------------------------------
class WallboxController:
    def __init__(self):
        self.current_set_power = 0
        self.is_on = False
        self.last_update_time = 0
        self.fase = 0
        self.time_turned_off = 0  
        self.pending_off_until = 0
        self.smoothing_alpha = CONFIG.get('SMOOTHING_ALPHA', 0.25)
        self.max_delta_per_sec = CONFIG.get('MAX_DELTA_PER_SEC', 1500)
        self.last_power_cmd_time = time.time()
        self.display_power = 0
        # manual override flag set when user issues /spegni via Telegram
        # while True the automatic logic will not turn the wallbox back on
        self.manual_off = False
        # tracking for sustained max power notifications
        self.max_reached_start = None   # timestamp when we first hit max
        self.max_notified = False      # whether notification was already sent
        # contatore per tracciare tentativi falliti di spegnimento
        # se il wallbox non risponde N volte, assume che sia offline
        self.failed_off_attempts = 0
        self.last_off_attempt_time = None
        # letture di 'tfase' fallite consecutive (per backoff e allarme)
        self.sync_falliti = 0
        # Limite kWh scelto mentre la colonnina non rispondeva: lo applica il
        # thread periodico quando torna (None = niente in attesa)
        self.limite_in_attesa = None
        # anti-spam del log di override manuale
        self.ultimo_log_manual_off = 0
        # l'auto e' fisicamente collegata? (da status/desc della centralina)
        self.auto_collegata = False
        # sta REALMENTE assorbendo? (pcar > 0) - decide i confini della sessione
        self.in_carica = False
        # Setpoint massimo dopo un intervento per il limite del contatore,
        # fino al campione delle fasi successivo (protezione_contatore.py)
        self.tetto_contatore = None
        # Limite kWh: ultimo valore letto dalla centralina (resta anche a
        # colonnina spenta, per confrontarlo alla riaccensione) e ultimo
        # inviato da noi (la sua rilettura non e' una novita')
        self.limite_noto = None
        self.limite_inviato = None
        # Ultima lettura con pcar > 0, e da quando la centralina non risponde:
        # per chiudere la sessione se la colonnina viene spenta mentre carica
        self.ultima_carica_vista = None
        self.offline_dal = None

    def update_shared_state(self):
        with STATO_LOCK:
            SYSTEM_STATE['WALLBOX_POWER'] = int(round(self.display_power))
            SYSTEM_STATE['WALLBOX_STATUS'] = self.is_on
            SYSTEM_STATE['IMPIANTO_FASE'] = self.fase

    def limiti_potenza(self, fase=None):
        """(min, max) in W per la fase indicata. Centralizza una scelta che
        prima era duplicata in 5 punti diversi."""
        fase = self.fase if fase is None else fase
        if fase == 0:
            return CONFIG['MONOFASE_MIN_POWER'], CONFIG['MONOFASE_MAX_POWER']
        return CONFIG['TRIFASE_MIN_POWER'], CONFIG['TRIFASE_MAX_POWER']

    def send_command(self, params):
        try:
            response = requests.get(WALLBOX_URL, params=params, timeout=3)
            ok = response.status_code == 200
            SYSTEM_STATE['CENTRALINA_ONLINE'] = ok
            return ok
        except requests.exceptions.RequestException:
            SYSTEM_STATE['CENTRALINA_ONLINE'] = False
            return False

    def leggi_stato_centralina(self, timeout=5):
        """GET su index.json senza parametri. Ritorna il dict oppure None.

        NON acquisisce WALLBOX_LOCK: e' I/O puro e non deve tenere fermo il
        regolatore per tutta la durata della richiesta.
        """
        try:
            risposta = requests.get(WALLBOX_URL, timeout=timeout)
            if risposta.status_code != 200:
                log_throttled('centralina_codice',
                              f"[CENTRALINA] Risposta inattesa: codice {risposta.status_code}", 300)
                self._lettura_fallita()
                return None
            dati = risposta.json()
            SYSTEM_STATE['CENTRALINA_ONLINE'] = True
            if self.sync_falliti >= 3:
                minuti = (time.time() - self.offline_dal) / 60 if self.offline_dal else 0
                log_msg(f"[CENTRALINA] Comunicazione ripristinata dopo {minuti:.0f} min.")
                reset_dedup('centralina_offline')
                notifica("🔌 Centralina di nuovo raggiungibile.")
            self.sync_falliti = 0
            self.offline_dal = None
            self._assorbi_stato(dati)
            return dati
        except requests.exceptions.RequestException as e:
            # Una riga all'inizio dell'interruzione, non una ogni 5 minuti per
            # tutta la notte (la colonnina la sera viene spenta: 350 righe in
            # 72 h). Il ritorno lo segnala "Comunicazione ripristinata".
            if self.sync_falliti == 0:
                log_msg(f"[CENTRALINA] Errore di connessione: {type(e).__name__}. "
                        f"Riprovo in silenzio finche' non torna.")
            self._lettura_fallita()
            return None
        except ValueError:
            log_throttled('centralina_json', "[CENTRALINA] La risposta non e' un JSON valido.", 300)
            self._lettura_fallita()
            return None

    def _lettura_fallita(self):
        SYSTEM_STATE['CENTRALINA_ONLINE'] = False
        if self.sync_falliti == 0:
            self.offline_dal = time.time()
        self.sync_falliti += 1
        # Colonnina sparita mentre caricava (la sera la si spegne dal
        # quadro): pcar non torna mai a 0 e la sessione restava aperta fino
        # al giorno dopo (sessioni da 1148 e 1334 minuti). Dopo 10 minuti la
        # si chiude all'ultima volta che la si e' vista caricare.
        if (self.in_carica and self.offline_dal
                and time.time() - self.offline_dal > CONFIG['SESSIONE_OFFLINE_S']):
            self.in_carica = False
            if contatori_instance and contatori_instance.sessione is not None:
                log_msg("[SESSIONE] Centralina irraggiungibile da "
                        f"{(time.time() - self.offline_dal) / 60:.0f} min: chiudo la sessione.")
                contatori_instance.fine_sessione(fine=self.ultima_carica_vista)
        # Sparita da dieci minuti: per noi e' spenta. Restava "accesa" al suo
        # ultimo setpoint finche' non tornava (l'11/11: ON a 1418 W per ore
        # con la colonnina staccata dal quadro), e la casa calcolata usciva
        # piu' bassa di quel tanto. Quando torna e carica, la si adotta.
        if (self.is_on and self.offline_dal
                and time.time() - self.offline_dal > CONFIG['SESSIONE_OFFLINE_S']):
            self.is_on = False
            self.update_shared_state()
            log_msg("[CENTRALINA] Irraggiungibile da piu' di "
                    f"{CONFIG['SESSIONE_OFFLINE_S'] // 60} minuti: la considero spenta.")
        if self.sync_falliti >= 2:
            # Colonnina spenta o irraggiungibile: il limite che ha adesso non
            # lo sappiamo. Prima restava l'ultimo letto, e l'interfaccia lo
            # mostrava "attivo" anche a colonnina spenta. Due errori di fila,
            # per non far lampeggiare la pagina a ogni errore isolato.
            SYSTEM_STATE['LIMITE_KWH_CENTRALINA'] = None

    def _assorbi_stato(self, dati):
        """Estrae dal JSON della centralina tutto cio' che ci serve.

        index.json espone misure REALI che prima venivano ignorate:
          pcar  potenza assorbita dall'auto (misurata, non il setpoint)
          phome/psun/pnet  consumo casa / produzione / scambio rete
          energy/time      energia e durata della sessione corrente
          status/desc      stato connessione ("Non collegata", "In carica", ...)
          pwmin/pwmax      limiti di potenza dichiarati dalla centralina
          alg              algoritmo attivo (2 = Man, l'unico che accetta P<watt>)
        """
        def num(chiave, default=0.0):
            try:
                return float(dati.get(chiave, default))
            except (TypeError, ValueError):
                return default

        with STATO_LOCK:
            SYSTEM_STATE['WB_PCAR'] = num('pcar')
            SYSTEM_STATE['WB_PCAR_LETTO_IL'] = time.time()
            # 'energy' e' in Wh (NON kWh) e il SEGNO e' un flag, non un valore:
            #   energy > 0  -> energia MISURATA da un contatore reale
            #   energy < 0  -> energia STIMATA, il valore e' il modulo
            # (dal JS della centralina: if(data.energy>0){stimato=0} else
            #  {stimato=1; data.energy=-data.energy} ... data.energy+" Wh")
            # Prima veniva preso cosi' com'era e mostrato come kWh: usciva
            # "-370 kWh" invece di "0,37 kWh (stimata)".
            energia_grezza = num('energy')
            SYSTEM_STATE['WB_ENERGIA_SESSIONE'] = abs(energia_grezza) / 1000.0
            SYSTEM_STATE['WB_ENERGIA_MISURATA'] = energia_grezza > 0
            SYSTEM_STATE['WB_TEMPO_SESSIONE'] = num('time')
            SYSTEM_STATE['WB_TEMP_PRESA'] = num('tplug')
            SYSTEM_STATE['WB_TEMP_SCHEDA'] = num('tboard')
            SYSTEM_STATE['WB_STATUS'] = str(dati.get('status', ''))
            SYSTEM_STATE['WB_DESC'] = str(dati.get('desc', ''))
            SYSTEM_STATE['WB_ALG'] = str(dati.get('alg', ''))
            chiave_limite = CONFIG.get('CHIAVE_LIMITE_JSON')
            limite_letto = None
            if chiave_limite and chiave_limite in dati:
                try:
                    limite_letto = int(float(dati[chiave_limite]))
                except (TypeError, ValueError):
                    limite_letto = None
                SYSTEM_STATE['LIMITE_KWH_CENTRALINA'] = limite_letto

        if limite_letto is not None:
            self._sincronizza_limite(limite_letto)

        # Energia caricata dall'ultima lettura: alla sessione e all'obiettivo
        if contatori_instance:
            incremento = contatori_instance.energia_centralina(
                abs(energia_grezza) / 1000.0, energia_grezza > 0)
            if incremento > 0 and pianificatore_instance:
                pianificatore_instance.aggiungi_energia(incremento)

        # L'auto e' collegata? status "0" = Non collegata.
        collegata = str(dati.get('status', '0')) != '0'
        if collegata != self.auto_collegata:
            era_collegata = self.auto_collegata
            self.auto_collegata = collegata
            desc = dati.get('desc', '?')
            log_msg(f"[CENTRALINA] Stato connessione: {desc}")
            notifica(f"🔌 Wallbox: {desc}", dedup_key='stato_connessione', min_intervallo=120)
            # Auto staccata: l'obiettivo era per quella carica. Solo sulla
            # transizione, cosi' lo si puo' impostare anche prima di attaccarla.
            # Uno del programma settimanale invece riparte da zero (pianificatore.py).
            if era_collegata and not collegata and pianificatore_instance \
                    and pianificatore_instance.auto_scollegata():
                notifica("🎯 Auto scollegata: obiettivo di carica annullato.")

        # --- Confini della sessione di ricarica ---------------------------
        # Si decidono QUI, sulla potenza realmente assorbita, non sui nostri
        # comandi. L'auto puo' smettere da sola (batteria piena -> status 2
        # "Fermo") senza che noi spegniamo nulla: prima in quel caso la
        # sessione restava aperta per sempre e non veniva mai registrata,
        # cioe' proprio nel modo in cui una ricarica finisce piu' spesso.
        carica_attiva = num('pcar') > 0
        if carica_attiva:
            self.ultima_carica_vista = time.time()
        if carica_attiva != self.in_carica:
            self.in_carica = carica_attiva
            if contatori_instance:
                if carica_attiva:
                    if contatori_instance.inizio_carica is None:
                        contatori_instance.inizio_sessione()
                        log_msg("[SESSIONE] Avviata.")
                else:
                    # NON si tocca is_on: la wallbox resta abilitata, e' l'auto
                    # che non assorbe. Spegnere e riaccendere qui provocherebbe
                    # un ciclo inutile a ogni batteria piena.
                    if contatori_instance.inizio_carica is not None:
                        log_msg(f"[SESSIONE] L'auto ha smesso di assorbire "
                                f"({dati.get('desc', '?')}): chiudo la sessione.")
                        contatori_instance.fine_sessione()

        # I limiti dichiarati dalla centralina hanno la precedenza sui valori
        # cablati. ATTENZIONE: la fase va presa dal JSON, non da self.fase:
        # questa funzione gira PRIMA che sync_fase aggiorni self.fase, quindi
        # durante un cambio mono<->tri scriveremmo i limiti nella fase sbagliata.
        pwmin, pwmax = num('pwmin'), num('pwmax')
        if pwmin > 0 and pwmax > pwmin:
            fase_json = 1 if str(dati.get('tfase')) == '1' else 0
            chiave_min = 'TRIFASE_MIN_POWER' if fase_json == 1 else 'MONOFASE_MIN_POWER'
            chiave_max = 'TRIFASE_MAX_POWER' if fase_json == 1 else 'MONOFASE_MAX_POWER'
            with STATO_LOCK:
                if CONFIG[chiave_min] != int(pwmin) or CONFIG[chiave_max] != int(pwmax):
                    log_msg(f"[CENTRALINA] Limiti aggiornati dalla centralina: "
                            f"{int(pwmin)}-{int(pwmax)}W (erano {CONFIG[chiave_min]}-{CONFIG[chiave_max]})")
                    CONFIG[chiave_min] = int(pwmin)
                    CONFIG[chiave_max] = int(pwmax)

        # Sicurezza: una presa che scalda troppo e' un rischio reale.
        # tplug e' la temperatura della presa misurata dalla centralina.
        tplug = num('tplug', -1)
        if tplug > 0:
            if tplug >= CONFIG['TEMP_PRESA_ALLARME']:
                log_msg(f"[ALLARME] Temperatura presa {tplug:.0f}C oltre la soglia "
                        f"di {CONFIG['TEMP_PRESA_ALLARME']}C!")
                notifica(f"🔥 *ALLARME: presa a {tplug:.0f} °C*\n"
                         f"Oltre la soglia di sicurezza ({CONFIG['TEMP_PRESA_ALLARME']} °C). "
                         f"Verificare il collegamento.",
                         dedup_key='temp_allarme', min_intervallo=600)
            elif tplug >= CONFIG['TEMP_PRESA_ATTENZIONE']:
                log_throttled('temp_attenzione',
                              f"[AVVISO] Temperatura presa elevata: {tplug:.0f}C", 600)
                notifica(f"🌡️ Presa a {tplug:.0f} °C: temperatura elevata, tenere d'occhio.",
                         dedup_key='temp_attenzione', min_intervallo=3600)
            else:
                reset_dedup('temp_allarme')
                reset_dedup('temp_attenzione')

        # btn=P<watt> ha effetto solo con alg=2 (Man): se la centralina viene
        # messa in Sole/Eco dal suo pannello, i nostri comandi di potenza
        # vengono ignorati in silenzio. Meglio accorgersene.
        alg = str(dati.get('alg', ''))
        if alg and alg != CONFIG['ALG_MANUALE']:
            nomi = {'0': 'Sole', '1': 'Eco', '2': 'Man', '3': 'Fast', '4': 'Alone'}
            log_throttled('alg_non_manuale',
                          f"[AVVISO] Centralina in modalita' '{nomi.get(alg, alg)}': i comandi di "
                          f"potenza vengono ignorati finche' non torna in 'Man'.", 600)
            notifica(f"⚠️ La centralina è in modalità *{nomi.get(alg, alg)}*, non *Man*: "
                     f"la regolazione automatica della potenza non ha effetto.",
                     dedup_key='alg_non_manuale', min_intervallo=3600)

    def _sincronizza_limite(self, letto):
        """Il limite vero e' quello della centralina, letto ogni 5 s.

        Puo' cambiare senza di noi: dal pannello della colonnina, o quando
        viene spenta e riaccesa. Se e' attivo con un valore diverso da quello
        salvato lo si adotta, cosi' lo slider della pagina e "Salva
        impostazioni" non lo riportano al valore vecchio. Se un limite attivo
        sparisce senza un nostro comando, lo si dice su Telegram: l'auto
        caricherebbe senza tetto.
        """
        precedente = self.limite_noto
        self.limite_noto = letto
        if letto == precedente:
            return
        if letto == self.limite_inviato:
            return      # e' la rilettura di un nostro comando, gia' loggato
        self.limite_inviato = None
        if precedente is None:
            log_msg(f"[LIMITE] Sulla centralina: "
                    f"{f'attivo, {letto} kWh' if letto > 0 else 'nessun limite'}")
        else:
            testo = lambda v: f"{v} kWh" if v else "nessuno"
            log_msg(f"[LIMITE] Cambiato sulla centralina, non da qui: "
                    f"{testo(precedente)} -> {testo(letto)}")
            if precedente > 0 and letto == 0:
                notifica(f"🔋 Il limite di carica di {precedente} kWh non c'è più sulla "
                         f"centralina (spenta e riaccesa?): l'auto carica senza limite.",
                         dedup_key='limite_sparito', min_intervallo=600)
        if 1 <= letto <= 100 and letto != CONFIG['LIMITE_KWH']:
            with STATO_LOCK:
                CONFIG['LIMITE_KWH'] = letto
            salva_config()
            log_msg(f"[LIMITE] Adottato il valore della centralina: {letto} kWh")

    def potenza_reale(self):
        """Potenza assorbita dall'auto, misurata dalla centralina.

        Ritorna None se il dato manca o e' troppo vecchio: il chiamante
        ripiega sul setpoint. Una misura stantia e' peggio del setpoint, perche'
        il regolatore non vedrebbe l'effetto dei comandi appena inviati e
        continuerebbe a correggere (oscillazione).
        """
        if not CONFIG.get('USA_PCAR'):
            return None
        valore = SYSTEM_STATE.get('WB_PCAR')
        if valore is None:
            return None
        eta = time.time() - SYSTEM_STATE.get('WB_PCAR_LETTO_IL', 0)
        if eta > CONFIG.get('MAX_ETA_PCAR', 20):
            if not self.is_on:
                return None     # a colonnina spenta non interessa a nessuno
            log_throttled('pcar_stantia',
                          f"[INFO] Potenza misurata vecchia di {eta:.0f}s: uso il setpoint.", 300)
            return None
        return valore

    def sync_fase(self):
        """Controllo leggero della sola chiave 'tfase', ogni 30s.

        NON spegne la wallbox e non interrompe la regolazione: a differenza di
        initialize(), qui non c'e' nessun turn_off. Ritorna
        'invariata' | 'cambiata' | 'errore'.
        """
        dati = self.leggi_stato_centralina(timeout=5)
        if dati is None:
            # Meglio tenere la fase vecchia che ripiegare su un default sbagliato
            if self.sync_falliti == 3:
                notifica("🔌 Centralina non raggiungibile: controllo fase sospeso.",
                         dedup_key='centralina_offline', min_intervallo=1800)
            return 'errore'

        self.adotta_carica_esterna(dati)

        nuova_fase = 1 if str(dati.get("tfase")) == "1" else 0

        if nuova_fase == self.fase:
            return 'invariata'   # nessun log: evita rumore ogni 30 secondi

        with WALLBOX_LOCK:
            vecchia = self.fase
            self.fase = nuova_fase
            min_p, max_p = self.limiti_potenza()
            modalita = "TRIFASE" if nuova_fase == 1 else "MONOFASE"

            log_msg(f"[FASE] Cambio rilevato: {'TRIFASE' if vecchia else 'MONOFASE'} -> {modalita} "
                    f"(limiti {min_p}-{max_p}W)")

            # Riallineamento passivo: si aggiorna solo lo stato in memoria e si
            # annulla il throttle, cosi' il prossimo pacchetto ricalcola e invia
            # il valore corretto. Nessun comando inviato da questo thread: cosi'
            # non si interferisce con un set_power in corso nel loop principale.
            if self.current_set_power:
                clampata = max(min_p, min(max_p, self.current_set_power))
                if clampata != self.current_set_power:
                    log_msg(f"[FASE] Setpoint riallineato: {self.current_set_power}W -> {clampata}W")
                    self.current_set_power = clampata
                    self.display_power = float(clampata)
            self.last_update_time = 0
            self.update_shared_state()

        notifica(f"⚙️ Impianto ora in modalità *{modalita}*.\nNuovi limiti: {min_p}-{max_p} W",
                 dedup_key='cambio_fase', min_intervallo=60)
        return 'cambiata'

    def _adotta_carica(self, dati):
        """Prende per buona la carica in corso sulla centralina invece di
        spegnerla: accesa, al setpoint che la centralina sta usando. Ritorna
        quel setpoint. Va chiamata con WALLBOX_LOCK."""
        # Limiti della fase letta ADESSO dal JSON: self.fase puo' essere
        # ancora quella vecchia se la colonnina e' ripartita in un'altra fase
        fase = 1 if str(dati.get('tfase')) == '1' else 0
        min_p, max_p = self.limiti_potenza(fase)
        try:
            potenza = int(float(dati.get('power', min_p)))
        except (TypeError, ValueError):
            potenza = min_p
        potenza = max(min_p, min(max_p, potenza))
        self.is_on = True
        self.current_set_power = potenza
        self.display_power = float(potenza)
        self.failed_off_attempts = 0
        self.update_shared_state()
        return potenza

    def adotta_carica_esterna(self, dati):
        """La centralina carica ma per noi e' spenta: si adotta la carica.

        Succede quando la colonnina viene accesa (o torna la corrente) con il
        servizio gia' avviato: appena alimentata riparte da sola a caricare
        alla sua potenza minima. Per il regolatore pero' restava spenta, e da
        spenta si riaccende solo se l'export supera il minimo; ma l'export che
        vede e' gia' al netto dell'auto, quindi non lo superava quasi mai e la
        carica restava bloccata al minimo (4140 W in trifase) anche importando
        dalla rete (visto il 25 e il 28/10). Il reset dall'interfaccia la
        sbloccava perche' initialize() adotta la carica in corso: qui si fa lo
        stesso a ogni lettura della centralina, senza reset.
        """
        if self.is_on or not CONFIG.get('ADOTTA_CARICA_IN_CORSO', True):
            return
        try:
            pcar = float(dati.get('pcar', 0))
        except (TypeError, ValueError):
            return
        if pcar <= 0:
            return
        if self.manual_off:
            log_throttled('carica_con_override',
                          f"[AVVISO] La centralina sta caricando ({pcar:.0f}W) ma l'override "
                          f"manuale e' attivo: non la regolo finche' non arriva /accendi.", 600)
            return
        # Dopo un nostro OFF la centralina impiega qualche secondo a fermarsi:
        # in quella finestra pcar e' ancora > 0 e non e' una ripartenza
        if time.time() - self.time_turned_off < CONFIG['COOLDOWN_ACCENSIONE']:
            return
        with WALLBOX_LOCK:
            if self.is_on:
                return
            potenza = self._adotta_carica(dati)
        log_msg(f"[CENTRALINA] Sta caricando ({pcar:.0f}W) ma risultava spenta "
                f"(colonnina riaccesa?): adotto la carica a {potenza}W e riprendo a regolare.")

    def set_limite_kwh(self, kwh):
        """Imposta l'energia da caricare (0-100 kWh) sulla centralina.

        0 e' un valore valido e significa "limite disattivato": la centralina
        usa esattamente questa convenzione (campo 'limit' a 0 = nessun tetto),
        e L0 e' il modo deterministico di disattivare senza dipendere dal
        pulsante fisico btn=l, che e' un toggle stateful (send-and-forget da
        un bottone web rischia di lasciarlo nello stato sbagliato).

        Finche' CONFIG['CMD_LIMITE_TEMPLATE'] e' None NON invia nulla: il valore
        viene solo salvato. Questo permette di deployare la funzione prima di
        aver confermato il comando esatto, senza mandare btn a caso all'hardware.
        Ritorna (inviato, messaggio).
        """
        template = CONFIG.get('CMD_LIMITE_TEMPLATE')
        kwh = max(0, min(100, int(kwh)))

        if not template:
            msg = "Valore salvato, ma il comando della centralina non è ancora configurato (vedi CMD_LIMITE_TEMPLATE)."
            log_msg(f"[LIMITE] {msg}")
            return False, msg

        with WALLBOX_LOCK:
            ok = self.send_command({'btn': template.format(valore=kwh)})

        if not ok:
            return False, "Comando non accettato dalla centralina."

        self.limite_inviato = kwh
        log_msg(f"[LIMITE] {'Disattivato' if kwh == 0 else f'Impostato a {kwh} kWh'}")

        # Verifica: rileggo e confronto. E' il modo piu' rapido per capire se
        # l'ipotesi sul formato del comando e' corretta.
        chiave = CONFIG.get('CHIAVE_LIMITE_JSON')
        if chiave:
            dati = self.leggi_stato_centralina(timeout=4)
            if dati is not None and str(dati.get(chiave)) != str(kwh):
                return False, (f"Comando inviato ma la centralina riporta "
                               f"{chiave}={dati.get(chiave)}: verificare CMD_LIMITE_TEMPLATE.")
        return True, ("Limite disattivato." if kwh == 0 else f"Limite impostato a {kwh} kWh.")

    def set_power(self, watts, bypass):
        """Wrapper con lock. La logica sta in _set_power.

        WALLBOX_LOCK e' un RLock, quindi turn_on()/turn_off(), che lo detengono
        gia' e chiamano set_power, non si auto-bloccano.
        """
        with WALLBOX_LOCK:
            return self._set_power(watts, bypass)

    def _set_power(self, watts, bypass):
        min_p, max_p = self.limiti_potenza()
        if self.tetto_contatore is not None:
            max_p = max(min_p, min(max_p, self.tetto_contatore))
            if self.is_on and int(watts) > max_p and self.current_set_power == max_p:
                return      # gia' al tetto della protezione: niente comandi doppi
        requested = int(max(min_p, min(max_p, int(watts))))

        now = time.time()

        if not bypass:   # bypass salta filtri e soglia di protezione
            # Variazione sotto la soglia: niente comando. Senza log: usciva a
            # quasi ogni pacchetto (10079 righe in 72 h) e copriva il resto.
            if abs(requested - self.current_set_power) < CONFIG['POTENZA_PROTEZIONE'] and self.is_on:
                return
            elapsed = now - (self.last_power_cmd_time or now)
            allowed_delta = self.max_delta_per_sec * max(elapsed, 0.01)
            if requested > self.current_set_power + allowed_delta:
                limited = int(self.current_set_power + allowed_delta)
            elif requested < self.current_set_power - allowed_delta:
                limited = int(self.current_set_power - allowed_delta)
            else:
                limited = requested

            if self.last_update_time > 0 and (now - self.last_update_time < CONFIG['UPDATE_INTERVAL_S']):
                return

            if self.display_power == 0:
                smoothed = float(limited)
            else:
                smoothed = self.smoothing_alpha * float(limited) + (1 - self.smoothing_alpha) * float(self.display_power)

            send_value = int(round(smoothed))
            if send_value == self.current_set_power:
                self.display_power = smoothed
                self.update_shared_state()
                return

            # Il cambio lo logga chi l'ha deciso (regolatore_classico._imposta)
        else:
            send_value = requested
            smoothed = float(send_value)

        # NOTA: qui prima veniva appeso un campione sintetico allo storico, in
        # aggiunta a quello di parse_packet. Risultato: punti duplicati nel
        # grafico a ogni cambio di potenza. Unico produttore ora: parse_packet.
        if self.send_command({'btn': f'P{send_value}'}):
            self.current_set_power = send_value
            self.last_update_time = now
            self.last_power_cmd_time = now
            self.display_power = smoothed
            self.update_shared_state()

    def turn_on(self):
        """Accende la wallbox. Ritorna True solo se ha davvero acceso.

        Il valore di ritorno serve al chiamante per non loggare una decisione
        che in realta' non ha prodotto nulla (durante il cooldown la richiesta
        viene rifiutata e prima si scriveva comunque 'Accendo' a ogni ciclo).
        """
        with WALLBOX_LOCK:
            if self.is_on:
                return False
            if self.time_turned_off > 0:
                tempo_trascorso = time.time() - self.time_turned_off
                if tempo_trascorso < CONFIG['COOLDOWN_ACCENSIONE']:
                    log_throttled('cooldown',
                                  f"[INFO] Attesa cooldown: "
                                  f"{CONFIG['COOLDOWN_ACCENSIONE'] - tempo_trascorso:.0f}s prima di accendere", 30)
                    return False

            # Colonnina che non risponde (spenta dal quadro): niente tentativi.
            # Ogni tentativo sono due richieste da 3 s di timeout dentro il
            # ciclo dei pacchetti, cioe' ~6 s di blocco a OGNI pacchetto per
            # tutto il tempo in cui c'e' sole e la colonnina e' spenta. Che sia
            # tornata lo scopre il thread periodico, che la interroga comunque.
            if self.sync_falliti >= 2:
                log_throttled('accensione_offline',
                              "[INFO] Ci sarebbe potenza per caricare, ma la centralina non "
                              "risponde: aspetto che torni.", 3600)
                return False

            min_p, _ = self.limiti_potenza()
            self.set_power(min_p, bypass=True)

            if self.send_command({'btn': 'i'}):
                # Si logga solo l'accensione riuscita: con la colonnina spenta
                # il tentativo si ripete a ogni pacchetto (~6 s) e prima
                # riempiva il journal (4338 righe "ACCENSIONE" in tre giorni)
                log_msg("[AZIONE] ACCENSIONE (ON)")
                self.is_on = True
                self.failed_off_attempts = 0  # Reset contatore quando si accende
                self.last_update_time = time.time()
                self.update_shared_state()
                # La sessione la apre _assorbi_stato quando vede pcar > 0:
                # un solo produttore, basato sulla potenza reale.
                return True
            log_throttled('accensione_fallita',
                          "[AVVISO] Accensione non riuscita: centralina non raggiungibile?", 300)
            return False

    def turn_off(self, force=False, urgente=False):
        """`urgente`: limite del contatore superato, si spegne anche se un
        comando e' appena partito (di norma si aspettano UPDATE_INTERVAL_S)."""
        with WALLBOX_LOCK:
            now = time.time()
            if (force and not urgente and self.last_update_time != 0
                    and (now - self.last_update_time < CONFIG['UPDATE_INTERVAL_S'])):
                return

            if not (self.is_on or force):
                return

            log_msg("[AZIONE] SPEGNIMENTO (OFF)")
            if self.send_command({'btn': 'o'}):
                era_acceso = self.is_on
                self.is_on = False
                self.failed_off_attempts = 0  # Reset contatore quando lo spegnimento riesce
                self.time_turned_off = time.time()
                self.last_update_time = time.time()
                self.update_shared_state()
                # La sessione la chiude _assorbi_stato quando pcar torna a 0.
                time.sleep(0.5)
                min_p, _ = self.limiti_potenza()
                try:
                    self.set_power(min_p, bypass=True)
                except Exception as e:
                    log_msg(f"[AVVISO] set_power dopo OFF fallito: {e}")
                    self.current_set_power = min_p
                    self.display_power = float(self.current_set_power)
                    self.update_shared_state()
            else:
                # Comando di spegnimento fallito - incrementa contatore
                self.failed_off_attempts += 1
                self.last_off_attempt_time = now
                max_attempts = CONFIG.get('MAX_FAILED_OFF_ATTEMPTS', 3)

                if self.failed_off_attempts >= max_attempts:
                    # Dopo N tentativi falliti, assume che il wallbox sia offline/spento fisicamente
                    log_msg(f"[AVVISO] Wallbox non risponde ai comandi di spegnimento ({self.failed_off_attempts} tentativi falliti). Assumo che sia offline/spento fisicamente.")
                    notifica("🛑 Colonnina spenta manualmente in precedenza",
                             dedup_key='spenta_manualmente', min_intervallo=1800)
                    self.is_on = False  # Considero il wallbox come spento
                    self.failed_off_attempts = 0  # Reset contatore
                    self.update_shared_state()
                else:
                    log_msg(f"[AVVISO] Comando OFF fallito ({self.failed_off_attempts}/{max_attempts} tentativi). Riproverò...")

    def initialize(self, avvio=False):
        """Reset completo: rilegge la fase E forza lo spegnimento.

        Usata al boot (avvio=True), dal bottone web e da /reset. NON va
        chiamata periodicamente: per il controllo ciclico della fase esiste
        sync_fase(), che non spegne nulla.
        """
        with WALLBOX_LOCK:
            log_msg("=== INIZIALIZZAZIONE SISTEMA ===")
            log_msg(f"Richiesta dati a {WALLBOX_URL}...")
            dati = self.leggi_stato_centralina(timeout=5)

            if dati is not None:
                self.fase = 1 if str(dati.get("tfase")) == "1" else 0
                log_msg(f"TIPO IMPIANTO: {'TRIFASE' if self.fase else 'MONOFASE'}")
                self.update_shared_state()
            else:
                log_msg(f"[AVVISO] Centralina non raggiungibile: mantengo la modalita' "
                        f"{'TRIFASE' if self.fase else 'MONOFASE'}.")

            self.last_update_time = 0
            min_p, _ = self.limiti_potenza()

            # Se l'auto sta GIA' caricando, si adotta lo stato invece di
            # spegnere. Con Restart=always ogni crash costerebbe ~60s di
            # ricarica persa (spegnimento + cooldown di riaccensione), e ormai
            # lo stato reale della centralina e' leggibile: non serve piu'
            # forzare uno stato noto spegnendo tutto.
            try:
                pcar_ora = float(dati.get('pcar', 0)) if dati else 0.0
            except (TypeError, ValueError):
                pcar_ora = 0.0

            if CONFIG.get('ADOTTA_CARICA_IN_CORSO', True) and pcar_ora > 0:
                potenza_ora = self._adotta_carica(dati)
                log_msg(f"1. Ricarica gia' in corso ({pcar_ora:.0f}W): adotto lo stato "
                        f"senza interromperla (setpoint {potenza_ora}W).")
            else:
                log_msg("1. Metto in OFF (Attesa dati)...")
                self.turn_off(force=True)
                log_msg(f"2. Imposto potenza minima ({min_p}W)...")
                self.set_power(min_p, bypass=True)

            # Limite kWh. Con il reset esplicito (pagina web, /reset) un limite
            # rimasto attivo si spegne, come sempre: chi resetta vuole uno stato
            # pulito. All'avvio del servizio invece resta: prima ogni deploy o
            # riavvio lo cancellava, e andava rimesso a mano. Non e' piu' un
            # tetto "nascosto": pagina, /info e /limite mostrano sempre quello
            # letto dalla colonnina.
            if CONFIG.get('CMD_LIMITE_TEMPLATE'):
                limite_letto = SYSTEM_STATE.get('LIMITE_KWH_CENTRALINA')
                if limite_letto and avvio:
                    log_msg(f"[LIMITE] Sulla centralina c'e' un limite di {limite_letto} kWh: lo lascio.")
                elif limite_letto:
                    log_msg(f"[LIMITE] Trovato limite attivo ereditato ({limite_letto} kWh): lo disattivo.")
                    self.set_limite_kwh(0)

            time.sleep(1)
            log_msg("=== PRONTO. IN ATTESA PACCHETTI ===")

# -----------------------------------------------------------
# CONTATORI ENERGIA E STORICO SU DISCO
# -----------------------------------------------------------
# Timestamp minimo accettabile (1 gen 2025): il Raspberry non ha RTC, al boot
# l'orologio parte dal 1970 e salta in avanti quando NTP sincronizza. I campioni
# con data assurda vanno scartati, non salvati.
TIMESTAMP_MINIMO = 1735689600

class ContatoriEnergia:
    """Integra le potenze istantanee (W) in energia (Wh) sulla giornata.

    ATTENZIONE: la potenza wallbox e' il SETPOINT inviato alla centralina, non
    una misura reale. I kWh della wallbox sono quindi una STIMA e vanno
    etichettati come tale nell'interfaccia.
    """

    def __init__(self):
        self.giorno = time.strftime('%Y-%m-%d')
        self.ultimo_t = None
        self.azzera()
        # Sessione di ricarica in corso: accumula per conto suo. Prima la si
        # ricavava come differenza dei contatori del giorno, che a mezzanotte
        # si azzerano: le sessioni notturne uscivano con la quota dal sole a
        # None o negativa (-9,6% il 5/11).
        self.sessione = None
        # Ultima lettura del contatore 'energy' della centralina (kWh)
        self._energia_letta = None
        self._energia_letta_t = None

    def azzera(self):
        self.solare_wh = 0.0
        self.rete_importata_wh = 0.0
        self.rete_esportata_wh = 0.0
        self.wallbox_wh = 0.0
        self.wallbox_da_fv_wh = 0.0
        self.secondi_carica = 0.0

    def aggiorna(self, solare_w, rete_w, wallbox_w, casa_w, ora=None):
        """Integrazione rettangolare su dt. dt fuori range viene scartato."""
        ora = ora or time.time()
        if self.ultimo_t is None:
            self.ultimo_t = ora
            return

        dt = ora - self.ultimo_t
        self.ultimo_t = ora
        # dt <= 0: orologio all'indietro (sync NTP). dt > 150: mancano almeno
        # due pacchetti, buco. In entrambi i casi non si inventa energia.
        # Il limite era 60 s, ma 'electricity' arriva proprio ogni 60 s: con un
        # minimo ritardo (il ciclo occupato con la centralina) l'intervallo
        # veniva scartato. Il 28/10 alle 16:40 il Pi contava 34,6 kWh prodotti
        # contro i 40,2 del contatore del sensore OWL (-14%).
        if dt <= 0 or dt > 150:
            return

        self._controlla_rollover()

        ore = dt / 3600.0
        self.solare_wh += max(0.0, solare_w) * ore

        # rete_w e' il carico totale letto dal contatore; l'export e' la parte
        # di produzione che eccede il consumo.
        surplus = solare_w - rete_w
        if surplus >= 0:
            self.rete_esportata_wh += surplus * ore
        else:
            self.rete_importata_wh += (-surplus) * ore

        if wallbox_w > 0:
            self.wallbox_wh += wallbox_w * ore
            self.secondi_carica += dt
            # Quota coperta dal fotovoltaico: il surplus disponibile alla casa
            # non puo' eccedere ne' la produzione al netto della casa ne' il
            # consumo effettivo della wallbox.
            # casa_w = rete_totale - potenza_wallbox, e puo' essere NEGATIVO
            # quando la potenza wallbox sovrastima (setpoint invece della
            # misura). Senza il clamp, solare_w - casa_w diventa maggiore
            # della produzione reale e si attribuiva al fotovoltaico piu'
            # energia di quanta ne fosse stata prodotta: l'efficienza
            # risultava gonfiata (visto nei dati: 171 kWh "da FV" in un
            # giorno da 70 kWh prodotti).
            casa_reale = max(0.0, casa_w)
            disponibile_fv = max(0.0, min(solare_w, solare_w - casa_reale))
            da_fv_wh = min(wallbox_w, disponibile_fv) * ore
            self.wallbox_da_fv_wh += da_fv_wh
            if self.sessione is not None:
                self.sessione['wh_stimati'] += wallbox_w * ore
                self.sessione['wh_fv'] += da_fv_wh

    def _controlla_rollover(self):
        oggi = time.strftime('%Y-%m-%d')
        if oggi == self.giorno:
            return
        self.archivia_giorno()
        self.giorno = oggi
        self.azzera()
        self.salva_stato()
        log_msg(f"[ENERGIA] Nuovo giorno: {oggi}. Contatori azzerati.")

    def efficienza_carica(self):
        """% dei kWh della wallbox coperti dal fotovoltaico."""
        if self.wallbox_wh <= 0:
            return None
        return min(100.0, self.wallbox_da_fv_wh / self.wallbox_wh * 100.0)

    @property
    def inizio_carica(self):
        return self.sessione['inizio'] if self.sessione else None

    def inizio_sessione(self):
        self.sessione = {'inizio': time.time(), 'wh_stimati': 0.0, 'wh_fv': 0.0,
                         'kwh_centralina': 0.0, 'misurata': False}

    def energia_centralina(self, kwh_letti, misurata):
        """A ogni lettura di 'energy' della centralina (kWh dall'inserimento del
        cavo). Ritorna l'incremento dalla lettura precedente, gia' sommato alla
        sessione in corso.

        Il contatore non si azzera fra una pausa e la ripresa (prima ogni
        sessione ereditava il totale delle precedenti: 1 minuto registrato
        come 3,65 kWh) ma si azzera quando si stacca il cavo o la colonnina
        viene spenta: allora la lettura nuova e' gia' tutta energia nuova.
        Sommando gli incrementi, entrambi i casi tornano giusti."""
        adesso = time.time()
        precedente, t_prec = self._energia_letta, self._energia_letta_t
        self._energia_letta, self._energia_letta_t = kwh_letti, adesso
        if precedente is None:
            return 0.0      # prima lettura dopo l'avvio: nessun riferimento
        incremento = kwh_letti - precedente if kwh_letti >= precedente else kwh_letti
        # Piu' di quanto 22 kW possono caricare nel frattempo: lettura
        # sballata o contatore ripartito con un valore vecchio. Si scarta.
        massimo = 22.0 * (adesso - t_prec) / 3600.0 + 0.05
        if incremento <= 0 or incremento > massimo:
            return 0.0
        if self.sessione is not None:
            self.sessione['kwh_centralina'] += incremento
            self.sessione['misurata'] = misurata
        return incremento

    def kwh_sessione(self):
        """kWh della sessione in corso: il contatore della centralina, oppure
        la nostra integrazione se la centralina non ha contato nulla."""
        s = self.sessione
        if s is None:
            return 0.0
        return s['kwh_centralina'] if s['kwh_centralina'] > 0 else s['wh_stimati'] / 1000.0

    def fine_sessione(self, fine=None):
        """Chiude la sessione, la salva su disco e notifica. `fine`: quando
        l'auto ha smesso davvero (se la colonnina e' sparita, l'ultima volta
        che la si e' vista caricare)."""
        s = self.sessione
        if s is None:
            return
        self.sessione = None
        fine = fine or time.time()
        inizio = s['inizio']
        durata = max(0.0, fine - inizio)
        if durata < 60:
            return   # sessioni lampo: non vale la pena registrarle

        kwh_stimati = s['wh_stimati'] / 1000.0
        # Il contatore della centralina ha la precedenza sulla nostra
        # integrazione ANCHE quando e' marcato "stimato": quel flag significa
        # "non omologato", non "inaffidabile", ed e' comunque un conteggio
        # fatto dall'hardware, piu' fine del nostro campionato ogni 60 s.
        if s['kwh_centralina'] > 0:
            kwh = s['kwh_centralina']
            misurata = s['misurata']
            origine = 'contatore' if misurata else 'centralina (stima)'
        else:
            kwh, misurata, origine = kwh_stimati, False, 'integrazione interna'
        quota_fv = min(100.0, s['wh_fv'] / s['wh_stimati'] * 100.0) if s['wh_stimati'] > 0 else None

        sessione = {
            'inizio': inizio,
            'fine': fine,
            'minuti': round(durata / 60),
            'kwh': round(kwh, 2),
            'kwh_misurati': misurata,
            'origine_kwh': origine,
            'quota_fv': round(quota_fv, 1) if quota_fv is not None else None,
            'potenza_media': round(kwh * 1000 / (durata / 3600)) if durata > 0 else 0,
            'risparmio_eur': risparmio_euro(kwh * (quota_fv or 0) / 100.0,
                                            kwh * (1 - (quota_fv or 0) / 100.0)),
        }
        try:
            elenco = leggi_json(FILE_SESSIONI, [])
            elenco.append(sessione)
            scrivi_json(FILE_SESSIONI, elenco[-200:])   # retention: ultime 200
        except Exception as e:
            log_msg(f"[SESSIONE] Salvataggio fallito: {e}")
        if ponte_mqtt_instance:
            ponte_mqtt_instance.pubblica_sessione(sessione)

        log_msg(f"[SESSIONE] Terminata: {durata/60:.0f} min, {kwh:.2f} kWh ({origine})"
                f"{f', {quota_fv:.0f}% da FV' if quota_fv is not None else ''}")
        testo = (f"🔋 *Sessione di carica terminata*\n"
                 f"⏱️ Durata: {durata/60:.0f} min\n"
                 f"⚡ Energia: {kwh:.2f} kWh{'' if misurata else ' (stimata)'}\n")
        if quota_fv is not None:
            testo += f"🌱 Da fotovoltaico: {quota_fv:.0f}%\n"
        if sessione['risparmio_eur'] > 0:
            testo += f"💰 Risparmio stimato: {sessione['risparmio_eur']:.2f} €\n"
        notifica(testo)

    def riepilogo(self):
        eff = self.efficienza_carica()
        return {
            'giorno': self.giorno,
            'solare_kwh': round(self.solare_wh / 1000.0, 2),
            'rete_importata_kwh': round(self.rete_importata_wh / 1000.0, 2),
            'rete_esportata_kwh': round(self.rete_esportata_wh / 1000.0, 2),
            'wallbox_kwh': round(self.wallbox_wh / 1000.0, 2),
            'wallbox_da_fv_kwh': round(self.wallbox_da_fv_wh / 1000.0, 2),
            'efficienza': round(eff, 1) if eff is not None else None,
            'minuti_carica': round(self.secondi_carica / 60.0),
        }

    def riepilogo_markdown(self):
        r = self.riepilogo()
        eff = f"{r['efficienza']:.1f}%" if r['efficienza'] is not None else "n/d"
        return (
            f"⚡ *Energia di oggi* ({r['giorno']})\n\n"
            f"☀️ Prodotta: {r['solare_kwh']} kWh\n"
            f"🔌 Importata: {r['rete_importata_kwh']} kWh\n"
            f"↗️ Esportata: {r['rete_esportata_kwh']} kWh\n\n"
            f"🚗 In auto: {r['wallbox_kwh']} kWh _(stima)_\n"
            f"🌱 Da fotovoltaico: {r['wallbox_da_fv_kwh']} kWh\n"
            f"📊 *Efficienza di carica: {eff}*\n"
            f"⏱️ Tempo di carica: {r['minuti_carica']} min\n"
        )

    def salva_stato(self):
        """Salva i contatori della giornata in corso.

        Senza questo, un riavvio (e con Restart=always capita) azzerava tutti
        i contatori del giorno: l'energia accumulata fin li' andava persa e il
        giorno archiviato a mezzanotte risultava incompleto.
        """
        return scrivi_json(FILE_STATO_ENERGIA, {
            'giorno': self.giorno,
            'solare_wh': self.solare_wh,
            'rete_importata_wh': self.rete_importata_wh,
            'rete_esportata_wh': self.rete_esportata_wh,
            'wallbox_wh': self.wallbox_wh,
            'wallbox_da_fv_wh': self.wallbox_da_fv_wh,
            'secondi_carica': self.secondi_carica,
        })

    def carica_stato(self):
        """Ripristina i contatori se il file e' della giornata corrente."""
        dati = leggi_json(FILE_STATO_ENERGIA, {})
        if not dati or dati.get('giorno') != self.giorno:
            if dati:
                log_msg(f"[ENERGIA] Stato salvato del {dati.get('giorno')}: "
                        f"non e' oggi, riparto da zero.")
            return
        try:
            self.solare_wh = float(dati.get('solare_wh', 0))
            self.rete_importata_wh = float(dati.get('rete_importata_wh', 0))
            self.rete_esportata_wh = float(dati.get('rete_esportata_wh', 0))
            self.wallbox_wh = float(dati.get('wallbox_wh', 0))
            self.wallbox_da_fv_wh = float(dati.get('wallbox_da_fv_wh', 0))
            self.secondi_carica = float(dati.get('secondi_carica', 0))
        except (TypeError, ValueError) as e:
            log_msg(f"[ENERGIA] Stato salvato illeggibile: {e}. Riparto da zero.")
            self.azzera()
            return
        log_msg(f"[ENERGIA] Ripresi i contatori di oggi: "
                f"{self.solare_wh/1000:.2f} kWh solari, {self.wallbox_wh/1000:.2f} kWh in auto.")

    def archivia_giorno(self):
        """Appende il giorno chiuso a storico_giornaliero.json (retention 90gg)."""
        try:
            storico = []
            if os.path.exists(FILE_STORICO_GIORNALIERO):
                with open(FILE_STORICO_GIORNALIERO, 'r', encoding='utf-8') as f:
                    storico = json.load(f)
            if not isinstance(storico, list):
                storico = []
            storico = [g for g in storico if g.get('giorno') != self.giorno]
            storico.append(self.riepilogo())
            storico = storico[-90:]

            tmp = FILE_STORICO_GIORNALIERO + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(storico, f, indent=2)
            os.replace(tmp, FILE_STORICO_GIORNALIERO)
        except (OSError, ValueError) as e:
            log_msg(f"[ENERGIA] Archiviazione giorno fallita: {e}")


# --- Storico campionato su disco -------------------------------------------
_coda_storico = deque()          # righe in attesa di flush
_ultimo_campione_t = 0.0

def registra_campione(rete, solare, wb, fasi, ts=None):
    """UNICO produttore dello storico. Sottocampiona a STORICO_INTERVALLO_S."""
    global _ultimo_campione_t
    ts = ts or time.time()
    if ts < TIMESTAMP_MINIMO:
        return  # orologio non ancora sincronizzato

    SYSTEM_STATE['ULTIME_LETTURE_FASI'].append((rete, solare, list(fasi), ts, wb))

    if ts - _ultimo_campione_t < CONFIG['STORICO_INTERVALLO_S']:
        return
    _ultimo_campione_t = ts
    # le sei fasi in coda alla riga: le statistiche le ritrovano dopo un riavvio
    _coda_storico.append([int(ts), int(rete), int(solare), int(wb)] + [int(x) for x in list(fasi)[:6]])

def flush_storico():
    """Scrive su disco i campioni accodati. Chiamata dal thread periodico ogni
    60s: una manciata di righe per volta, non una scrittura per pacchetto
    (protegge la SD del Raspberry)."""
    if not _coda_storico:
        return
    righe = []
    while _coda_storico:
        righe.append(_coda_storico.popleft())
    try:
        with open(FILE_STORICO, 'a', encoding='utf-8') as f:
            for r in righe:
                f.write(json.dumps(r) + '\n')
    except OSError as e:
        log_msg(f"[STORICO] Scrittura fallita: {e}")

def carica_storico():
    """Ricarica in RAM lo storico recente, cosi' i grafici sopravvivono ai riavvii."""
    if not os.path.exists(FILE_STORICO):
        return
    limite = time.time() - CONFIG['STORICO_RETENTION_GIORNI'] * 86400
    caricati = 0
    try:
        with open(FILE_STORICO, 'r', encoding='utf-8') as f:
            for riga in f:
                riga = riga.strip()
                if not riga:
                    continue
                try:
                    campione = json.loads(riga)
                    ts, rete, solare, wb = campione[:4]
                except (ValueError, TypeError):
                    continue   # riga troncata da un crash: si salta
                if ts < limite or ts < TIMESTAMP_MINIMO:
                    continue
                # le righe scritte prima del 10/11/2026 non hanno le fasi
                fasi = campione[4:10] if len(campione) >= 10 else [0, 0, 0, 0, 0, 0]
                SYSTEM_STATE['ULTIME_LETTURE_FASI'].append((rete, solare, fasi, ts, wb))
                caricati += 1
    except OSError as e:
        log_msg(f"[STORICO] Lettura fallita: {e}")
        return
    log_msg(f"[STORICO] Ricaricati {caricati} campioni da disco.")

INTERVALLI = {'15m': 900, '1h': 3600, '6h': 21600, '24h': 86400, '7g': 604800}

def leggi_json(percorso, default):
    """Legge un file JSON tollerando assenza e corruzione."""
    if not os.path.exists(percorso):
        return default
    try:
        with open(percorso, 'r', encoding='utf-8') as f:
            dati = json.load(f)
        return dati if isinstance(dati, type(default)) else default
    except (json.JSONDecodeError, OSError) as e:
        log_msg(f"[FILE] Lettura {os.path.basename(percorso)} fallita: {e}")
        return default

def scrivi_json(percorso, dati):
    """Scrittura atomica: tmp + os.replace."""
    tmp = percorso + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(dati, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, percorso)
        return True
    except OSError as e:
        log_msg(f"[FILE] Scrittura {os.path.basename(percorso)} fallita: {e}")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False

def risparmio_euro(kwh_da_fv, kwh_da_rete=0.0):
    """Stima del risparmio di aver caricato col proprio sole.

    Caricare dal fotovoltaico evita di comprare (PREZZO_ACQUISTO) ma rinuncia
    a vendere quell'energia (PREZZO_VENDITA): il guadagno netto e' la
    differenza. L'energia presa dalla rete e' invece un costo.
    """
    delta = CONFIG['PREZZO_ACQUISTO'] - CONFIG['PREZZO_VENDITA']
    return round(kwh_da_fv * delta - kwh_da_rete * CONFIG['PREZZO_ACQUISTO'], 2)

def giorni_storico(quanti=14):
    """Ultimi N giorni chiusi + la giornata in corso, con risparmio stimato.

    I giorni chiusi vengono dall'archivio delle statistiche (statistiche.py),
    gli stessi numeri della pagina Statistiche: i vecchi riepiloghi di
    storico_giornaliero.json sottostimavano la produzione fino al 28/10/2026 e
    contavano in auto energia mai caricata. Quel file resta scritto, per
    confronto, ma non lo legge piu' nessuno. La giornata in corso viene dai
    contatori, cosi' coincide con la scheda Oggi."""
    archivio, oggi_iso = giorni_statistiche()
    giorni = []
    for giorno in sorted(g for g in archivio if g != oggi_iso)[-quanti:]:
        record = archivio[giorno]
        x = statistiche.giornaliero(record)
        giorni.append({
            'giorno': giorno, 'solare_kwh': x['sole'],
            'rete_importata_kwh': x['prelievo'], 'rete_esportata_kwh': x['immessa'],
            'wallbox_kwh': x['auto'], 'wallbox_da_fv_kwh': x['auto_sole'],
            'efficienza': round(x['auto_sole'] / x['auto'] * 100, 1) if x['auto'] > 0.05 else None,
            'minuti_carica': round(sum(record['ia'])),
        })
    if contatori_instance:
        giorni = (giorni + [contatori_instance.riepilogo()])[-quanti:]
    for g in giorni:
        da_fv = g.get('wallbox_da_fv_kwh') or 0
        da_rete = max(0, (g.get('wallbox_kwh') or 0) - da_fv)
        g['risparmio_eur'] = risparmio_euro(da_fv, da_rete)
    return giorni

def leggi_sessioni(quante=20):
    return leggi_json(FILE_SESSIONI, [])[-quante:]


def serie_storico(secondi, max_punti=300):
    """Estrae una serie temporale pulita dal buffer: finestra + ORDINAMENTO +
    sottocampionamento a bucket.

    Usata da /api/data, /api/storico e /grafici. L'ordinamento e' essenziale:
    il buffer mescola i campioni ricaricati da disco all'avvio con quelli
    live, e un grafico su dati non ordinati disegna una ragnatela di linee
    che rimbalzano avanti e indietro.
    """
    da = time.time() - secondi
    punti = sorted((p for p in list(SYSTEM_STATE['ULTIME_LETTURE_FASI']) if p[3] >= da),
                   key=lambda p: p[3])
    if not punti:
        return []

    def tupla(p):
        return {'time': p[3], 'grid': p[0], 'solar': p[1], 'wb': p[4] if len(p) > 4 else 0}

    if len(punti) <= max_punti:
        return [tupla(p) for p in punti]

    # Media per bucket: conserva la forma della curva invece di scartare
    # campioni a caso (che farebbe sparire i picchi).
    passo = len(punti) / max_punti
    out = []
    for n in range(max_punti):
        inizio = int(n * passo)
        fine = max(int((n + 1) * passo), inizio + 1)
        blocco = punti[inizio:fine]
        if not blocco:
            continue
        out.append({
            'time': blocco[len(blocco) // 2][3],
            'grid': sum(b[0] for b in blocco) / len(blocco),
            'solar': sum(b[1] for b in blocco) / len(blocco),
            'wb': sum((b[4] if len(b) > 4 else 0) for b in blocco) / len(blocco),
        })
    return out

def ruota_storico():
    """Riscrive il file tenendo solo i campioni entro la retention."""
    if not os.path.exists(FILE_STORICO):
        return
    limite = time.time() - CONFIG['STORICO_RETENTION_GIORNI'] * 86400
    try:
        if os.path.getsize(FILE_STORICO) < 2_000_000:
            return   # niente da fare finche' e' piccolo
        tenute = []
        with open(FILE_STORICO, 'r', encoding='utf-8') as f:
            for riga in f:
                try:
                    if json.loads(riga)[0] >= limite:
                        tenute.append(riga)
                except (ValueError, TypeError, IndexError):
                    continue
        tmp = FILE_STORICO + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            f.writelines(tenute)
        os.replace(tmp, FILE_STORICO)
        log_msg(f"[STORICO] Ruotato: {len(tenute)} campioni mantenuti.")
    except OSError as e:
        log_msg(f"[STORICO] Rotazione fallita: {e}")


def potenza_auto_per_i_conteggi(wb_power):
    """La potenza dell'auto da usare per CONTARE l'energia (non per regolare).

    Se la colonnina non risponde da piu' di due minuti l'auto e' ferma.
    Spenta dal quadro mentre caricava, restava "accesa" al suo ultimo setpoint
    per ore: l'8/11 risultavano 86,9 kWh in auto in un giorno in cui la casa
    ne aveva consumati 54 in tutto.
    """
    colonnina = wallbox_instance
    if (colonnina is not None and colonnina.offline_dal
            and time.time() - colonnina.offline_dal > 120):
        return 0
    return wb_power


def numero(testo):
    """Testo di un nodo XML -> float, None se manca o non e' un numero."""
    try:
        return float(testo)
    except (TypeError, ValueError):
        return None


class EnergyMonitor:
    def __init__(self):
        self.solar_now = 0.0
        self.total_grid_load = 0.0
        self.house_load = 0.0
        self.fases = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        self.ctrletturefasi = 0
        self.time = None
        # 'exporting' del pacchetto solar: media dei 6 s precedenti, somma
        # delle sole fasi che esportano (vedi regolatore_pid.py). Non e' lo
        # scambio netto: lo usa solo lo stimatore del PID.
        self.export_now = None
        self.ultimo_tipo = None     # 'solar' | 'electricity': cosa ha fatto scattare run_logic
        # Contatori del giorno del sensore OWL (Wh), integrati da lui: vanno
        # solo nell'archivio sul server, come riferimento per le analisi.
        self.owl_giorno = {}

    def parse_packet(self, data):
        try:
            xml_str = data.decode('utf-8', errors='ignore')
            root = ET.fromstring(xml_str)
            
            if root.tag == 'electricity':
                channels = root.find('channels')
                # 'is not None': un Element senza figli e' falsy, quindi il
                # vecchio "if channels:" avrebbe scartato pacchetti validi.
                if channels is not None:
                    p = {}
                    for c in channels.findall('chan'):
                        nodo = c.find('curr')
                        try:
                            val = float(nodo.text) if nodo is not None else 0.0
                        except (TypeError, ValueError):
                            val = 0.0
                        p[c.get('id')] = val

                    l1, l2, l3 = p.get('0',0), p.get('1',0), p.get('2',0)
                    l4, l5, l6 = p.get('3',0), p.get('4',0), p.get('5',0)
                    giorno = {c.get('id'): numero(c.findtext('day')) for c in channels.findall('chan')}
                    if all(giorno.get(str(i)) is not None for i in range(6)):
                        self.owl_giorno['owl_consumo_wh'] = round(sum(giorno[str(i)] for i in range(3)), 1)
                        self.owl_giorno['owl_solare_wh'] = round(sum(giorno[str(i)] for i in range(3, 6)), 1)

                    self.total_grid_load = l1 + l2 + l3
                    self.solar_now = l4 + l5 + l6
                    self.fases = [l1, l2, l3, l4, l5, l6]

                    self.ctrletturefasi += 1
                    self.ultimo_tipo = 'electricity'
                    SYSTEM_STATE['ULTIMA_LETTURA_FASI'] = time.time()
                    SYSTEM_STATE['MONITOR_FASI'] = self.fases
                    SYSTEM_STATE['SENSORE_ONLINE'] = True
                    self.time = SYSTEM_STATE['ULTIMA_LETTURA_FASI']

                    # Potenza wallbox: si preferisce SEMPRE pcar, la misura reale
                    # letta dalla centralina. Il setpoint e' solo una stima e si
                    # discosta parecchio quando l'auto assorbe meno di quanto
                    # concesso (batteria quasi carica, derating termico): in quel
                    # caso il consumo casa risultava sottostimato e il regolatore
                    # continuava ad alzare.
                    wb_status = SYSTEM_STATE.get('WALLBOX_STATUS', False)
                    misurata = SYSTEM_STATE.get('WB_PCAR') if CONFIG.get('USA_PCAR') else None
                    eta_ok = (time.time() - SYSTEM_STATE.get('WB_PCAR_LETTO_IL', 0)) <= CONFIG.get('MAX_ETA_PCAR', 20)
                    if misurata is not None and eta_ok:
                        wb_power = misurata
                    else:
                        wb_power = SYSTEM_STATE.get('WALLBOX_POWER', 0) if wb_status else 0
                    self.house_load = self.total_grid_load - wb_power

                    wb_conteggio = potenza_auto_per_i_conteggi(wb_power)

                    registra_campione(self.total_grid_load, self.solar_now,
                                      wb_conteggio, self.fases, self.time)
                    if contatori_instance:
                        contatori_instance.aggiorna(self.solar_now, self.total_grid_load,
                                                    wb_conteggio, self.total_grid_load - wb_conteggio,
                                                    self.time)
                    # Try a parte: un errore qui non deve far scartare il
                    # pacchetto, cioe' saltare la regolazione di questo minuto
                    if ponte_mqtt_instance and ponte_mqtt_instance.attivo:
                        try:
                            ponte_mqtt_instance.pubblica_minuto(dati_minuto(self, wb_conteggio))
                            ponte_mqtt_instance.pubblica_controlli(stato_controlli())
                            ponte_mqtt_instance.pubblica_statistiche(statistiche_per_ha(self))
                        except Exception as e:
                            log_throttled('mqtt_minuto', f"[MQTT] Record del minuto non pubblicato: {e}", 600)

                    return "TRIGGER"

            elif root.tag == 'solar':
                curr = root.find('current')
                if curr is not None:
                    nodo = curr.find('generating')
                    if nodo is None:
                        return None
                    self.solar_now = float(nodo.text)
                    nodo_exp = curr.find('exporting')
                    try:
                        self.export_now = float(nodo_exp.text) if nodo_exp is not None else None
                    except (TypeError, ValueError):
                        self.export_now = None
                    for chiave, percorso in (('owl_generato_wh', 'day/generated'),
                                             ('owl_esportato_wh', 'day/exported')):
                        valore = numero(root.findtext(percorso))
                        if valore is not None:
                            self.owl_giorno[chiave] = valore
                    if ponte_mqtt_instance:
                        ponte_mqtt_instance.campione_solare(self.solar_now)
                    self.ultimo_tipo = 'solar'
                    SYSTEM_STATE['ULTIMA_LETTURA_SOLARE'] = time.time()
                    SYSTEM_STATE['SENSORE_ONLINE'] = True
                    self.time = SYSTEM_STATE['ULTIMA_LETTURA_SOLARE']
                    return "TRIGGER"

        except ET.ParseError as e:
            # Prima era "except Exception: pass": un cambio di formato dei
            # pacchetti sarebbe stato completamente invisibile.
            SYSTEM_STATE['ERRORI_PARSING'] += 1
            log_throttled('parse_xml', f"[ERRORE] Pacchetto XML non interpretabile: {e}", 300)
        except Exception as e:
            SYSTEM_STATE['ERRORI_PARSING'] += 1
            log_throttled('parse_generico', f"[ERRORE] Pacchetto scartato: {e}", 300)
        return None

def dati_minuto(monitor, auto_w):
    """Il record al minuto per Home Assistant e l'archivio sul server (vedi
    ponte_mqtt.py). Valori istantanei del pacchetto 'electricity' appena
    letto, piu' i contatori del giorno."""
    with STATO_LOCK:
        acceso = SYSTEM_STATE['WALLBOX_STATUS']
        dati = {
            'ts': int(monitor.time),
            'produzione_w': round(monitor.solar_now),
            'consumo_w': round(monitor.total_grid_load),
            'prelievo_w': round(monitor.total_grid_load - monitor.solar_now),
            'auto_w': round(auto_w),
            'setpoint_w': SYSTEM_STATE['WALLBOX_POWER'] if acceso else 0,
            'pcar_w': SYSTEM_STATE['WB_PCAR'],
            'colonnina': (SYSTEM_STATE['WB_DESC'] or '').replace('_', ' ') or None,
            'fase': 'trifase' if SYSTEM_STATE['IMPIANTO_FASE'] == 1 else 'monofase',
            'algoritmo': CONFIG['ALGORITMO'],
            'prelevabile_w': CONFIG['POTENZA_PRELEVABILE'],
            'fasi_w': [round(x) for x in monitor.fases],
            'sessione_kwh': SYSTEM_STATE['WB_ENERGIA_SESSIONE'],
        }
    if contatori_instance:
        r = contatori_instance.riepilogo()
        dati.update({
            'oggi_prodotta_kwh': r['solare_kwh'],
            'oggi_prelevata_kwh': r['rete_importata_kwh'],
            'oggi_immessa_kwh': r['rete_esportata_kwh'],
            'oggi_auto_kwh': r['wallbox_kwh'],
            'oggi_auto_sole_kwh': r['wallbox_da_fv_kwh'],
            'oggi_minuti_carica': r['minuti_carica'],
        })
    dati.update(monitor.owl_giorno)
    if pianificatore_instance:
        piano = pianificatore_instance.stato()
        dati['modalita'] = piano['modalita_effettiva']
        dati['quota_rete_auto_w'] = piano['quota_w']
        if piano['obiettivo']:
            dati['obiettivo_kwh'] = piano['obiettivo']['kwh']
            dati['obiettivo_fatti_kwh'] = piano['obiettivo']['fatti']
    return dati

def regola_pid(monitor, wallbox):
    """Un passo del regolatore PID, a ogni pacchetto 'solar'."""
    regolatore = regolatore_pid_instance
    regolatore.aggiorna_guadagni()      # Kp/Ki/Kd cambiati da Telegram valgono subito
    # Lock per tutto il passo: legge lo stato della wallbox e decide, e un
    # comando da Web/Telegram nel mezzo renderebbe la decisione incoerente
    with WALLBOX_LOCK:
        regolatore.passo(wallbox, monitor.solar_now, monitor.export_now, monitor.time)

    _, potenza_massima = wallbox.limiti_potenza()
    classico_instance.notifica_potenza_massima(wallbox, wallbox.current_set_power, potenza_massima)

    stima = regolatore.ultima_stima
    if stima:
        log_throttled('stato_pid',
                      f"[PID] Sole {monitor.solar_now:.0f}W | Scambio stimato {stima['scambio']:+d}W "
                      f"(corr {stima['correzione']:+d}) | Disponibile {stima['disponibile']}W | "
                      f"WB: {'ON' if wallbox.is_on else 'OFF'} ({wallbox.current_set_power}W)", 60)

def run_logic(monitor, wallbox):
    # Il PID osserva sempre, anche quando comanda il classico: al cambio di
    # algoritmo ha gia' la casa per fase e non deve aspettare 60 s.
    pid_attivo = CONFIG['ALGORITMO'] == 'pid' and regolatore_pid_instance is not None
    if regolatore_pid_instance is not None:
        if monitor.ultimo_tipo == 'electricity':
            regolatore_pid_instance.aggiorna_fasi(monitor.fases[0:3], monitor.fases[3:6],
                                                  wallbox.fase == 1, monitor.time)
        elif not pid_attivo or wallbox.manual_off:
            # Quando passo() non gira, il PID osserva comunque (prima di un
            # eventuale comando del classico in questo stesso pacchetto)
            regolatore_pid_instance.osserva(wallbox, monitor.solar_now)

    # Limite del contatore: prima di qualunque algoritmo, e anche durante i
    # loro timer. Se interviene, questo pacchetto e' gia' stato corretto.
    if protezione_instance is not None:
        with WALLBOX_LOCK:
            if monitor.ultimo_tipo == 'electricity':
                protezione_instance.aggiorna_fasi(
                    monitor.fases[0:3], wallbox.current_set_power if wallbox.is_on else 0,
                    wallbox.fase == 1)
            if protezione_instance.controlla(wallbox, monitor.solar_now):
                if regolatore_pid_instance is not None:
                    regolatore_pid_instance.comando_esterno()
                return

    # if user has manually requested the wallbox to remain off, skip all automatic decisions
    if getattr(wallbox, 'manual_off', False):
        # Prima questo log usciva a OGNI pacchetto (ogni pochi secondi) e
        # riempiva journalctl. Ora: una volta ogni 10 minuti.
        log_throttled('manual_off',
                      "[INFO] Override manuale attivo, wallbox rimane spento fino a comando /accendi",
                      600)
        return

    if pid_attivo:
        # Il PID decide solo sui 'solar' (ogni 6 s): gli 'electricity' gli
        # servono per la casa per fase, gia' passata sopra.
        if monitor.ultimo_tipo == 'solar':
            regola_pid(monitor, wallbox)
        return

    if wallbox.tetto_contatore is not None:
        # La protezione del contatore e' appena intervenuta: il consumo che il
        # classico vede e' di prima, con l'auto di prima, e ricorreggerlo a
        # ogni 'solar' portava l'auto al minimo. Si riparte dal campione
        # delle fasi successivo; intanto il sole lo segue la protezione.
        return

    classico_instance.passo(wallbox, monitor.solar_now, monitor.total_grid_load, monitor.house_load)

# -----------------------------------------------------------
# THREAD PERIODICO (attivita' cicliche)
# -----------------------------------------------------------
def salva_tutto(motivo=""):
    """Scrive su disco tutto cio' che e' volatile. Idempotente."""
    try:
        flush_storico()
    except Exception as e:
        log_msg(f"[ARRESTO] Flush storico fallito: {e}")
    try:
        if contatori_instance:
            contatori_instance.salva_stato()
    except Exception as e:
        log_msg(f"[ARRESTO] Salvataggio contatori fallito: {e}")
    if motivo:
        log_msg(f"[ARRESTO] Stato salvato ({motivo}).")

def gestore_arresto(segnale, frame):
    """SIGTERM: e' cosi' che systemctl restart/stop ferma il servizio.

    Senza questo il processo veniva ucciso senza salvare, e si perdeva fino a
    60s di contatori (l'intervallo fra un salvataggio periodico e il
    successivo). NON spegne la wallbox: un riavvio del servizio non deve
    interrompere una ricarica in corso.
    """
    salva_tutto(f"segnale {segnale}")
    if ponte_mqtt_instance:
        ponte_mqtt_instance.chiudi()
    sys.exit(0)

def thread_periodico(wallbox):
    """Unico thread per tutte le attivita' cicliche.

    Ogni attivita' ha il proprio intervallo e il proprio try/except: il
    fallimento di una non ferma le altre e non uccide il thread.
    """
    prossimo_sync = 0
    prossimo_flush = 0
    prossimo_watchdog = 0
    prossima_rotazione = time.time() + 3600
    riepilogo_inviato_il = None
    rapporto_inviato_il = None
    previsione_salvata_il = None
    giorno_statistiche = statistiche.giorno_di(time.time())

    while True:
        time.sleep(5)
        adesso = time.time()

        # --- Controllo fase ogni 30s (non spegne nulla) ---
        if adesso >= prossimo_sync:
            try:
                esito = wallbox.sync_fase()
                if esito == 'errore':
                    # backoff esponenziale: 30s, 60s, 120s... max 5 minuti
                    ritardo = min(CONFIG['INTERVALLO_SYNC_FASE'] * (2 ** min(wallbox.sync_falliti, 4)), 300)
                else:
                    ritardo = CONFIG['INTERVALLO_SYNC_FASE']
                prossimo_sync = adesso + ritardo
            except Exception as e:
                log_msg(f"[ERRORE] sync_fase: {e}")
                prossimo_sync = adesso + 60

        # --- Flush storico + contatori su disco ogni 60s (salva-SD) ---
        if adesso >= prossimo_flush:
            try:
                flush_storico()
                if contatori_instance:
                    contatori_instance.salva_stato()
            except Exception as e:
                log_msg(f"[ERRORE] flush storico: {e}")
            prossimo_flush = adesso + CONFIG['STORICO_FLUSH_S']

        # --- Watchdog sensore ogni 60s ---
        if adesso >= prossimo_watchdog:
            try:
                ultima = SYSTEM_STATE.get('ULTIMA_LETTURA_FASI')
                if ultima and adesso - ultima > CONFIG['WATCHDOG_SENSORE_S']:
                    if SYSTEM_STATE['SENSORE_ONLINE']:
                        SYSTEM_STATE['SENSORE_ONLINE'] = False
                        minuti = (adesso - ultima) / 60
                        log_msg(f"[AVVISO] Nessun pacchetto dal sensore da {minuti:.0f} minuti!")
                        notifica(f"📡 Nessun dato dal sensore da {minuti:.0f} minuti. "
                                 f"Il regolatore non può più adattare la potenza.",
                                 dedup_key='sensore_offline', min_intervallo=1800)
                elif ultima and SYSTEM_STATE['SENSORE_ONLINE'] is False:
                    SYSTEM_STATE['SENSORE_ONLINE'] = True
                    reset_dedup('sensore_offline')
                    notifica("📡 Dati dal sensore ripristinati.")
            except Exception as e:
                log_msg(f"[ERRORE] watchdog: {e}")
            prossimo_watchdog = adesso + 60

        # --- Limite kWh rimasto in attesa: la colonnina e' tornata ---
        try:
            if (wallbox.limite_in_attesa is not None and wallbox.sync_falliti == 0
                    and SYSTEM_STATE['CENTRALINA_ONLINE']):
                kwh, wallbox.limite_in_attesa = wallbox.limite_in_attesa, None
                inviato, messaggio = wallbox.set_limite_kwh(kwh)
                if not inviato and not SYSTEM_STATE['CENTRALINA_ONLINE']:
                    wallbox.limite_in_attesa = kwh      # sparita di nuovo: si riprova
                else:
                    log_msg(f"[LIMITE] In attesa dalla scelta del preset: {messaggio}")
        except Exception as e:
            log_msg(f"[ERRORE] limite in attesa: {e}")

        # --- Scadenza dell'obiettivo (anche senza pacchetti) ---
        try:
            if pianificatore_instance:
                pianificatore_instance.controlla()
        except Exception as e:
            log_msg(f"[ERRORE] pianificatore: {e}")

        # --- Statistiche: due minuti dopo mezzanotte si chiude il giorno prima ---
        if statistiche.giorno_di(adesso - 120) != giorno_statistiche:
            giorno_statistiche = statistiche.giorno_di(adesso - 120)
            try:
                archivia_statistiche()
            except Exception as e:
                log_msg(f"[ERRORE] archivio statistiche: {e}")

        # --- Rotazione storico ogni ora ---
        if adesso >= prossima_rotazione:
            try:
                ruota_storico()
            except Exception as e:
                log_msg(f"[ERRORE] rotazione storico: {e}")
            prossima_rotazione = adesso + 3600

        # --- Riepilogo giornaliero Telegram ---
        try:
            ora_locale = time.localtime(adesso)
            oggi = time.strftime('%Y-%m-%d', ora_locale)
            if (ora_locale.tm_hour == CONFIG['ORA_RIEPILOGO']
                    and riepilogo_inviato_il != oggi and contatori_instance):
                riepilogo_inviato_il = oggi
                if contatori_instance.wallbox_wh > 0 or contatori_instance.solare_wh > 0:
                    notifica("📅 *Riepilogo giornaliero*\n\n" + contatori_instance.riepilogo_markdown())
            # Alle 20 la previsione per domani, da confrontare poi col vero
            if ora_locale.tm_hour == 20 and previsione_salvata_il != oggi:
                previsione_salvata_il = oggi
                salva_previsione_di_domani()
            # La domenica, alla stessa ora, anche la settimana
            if (ora_locale.tm_wday == 6 and ora_locale.tm_hour == CONFIG['ORA_RIEPILOGO']
                    and rapporto_inviato_il != oggi and CONFIG['RAPPORTO_SETTIMANALE']):
                rapporto_inviato_il = oggi
                testo = rapporto_settimanale()
                if testo:
                    notifica(testo)
        except Exception as e:
            log_msg(f"[ERRORE] riepilogo giornaliero: {e}")

# -----------------------------------------------------------
# MAIN
# -----------------------------------------------------------
def main():
    global wallbox_instance, contatori_instance, regolatore_pid_instance, ponte_mqtt_instance, protezione_instance, classico_instance, pianificatore_instance

    # La configurazione va caricata PRIMA di costruire il controller, cosi'
    # initialize() lavora gia' con i valori salvati dall'utente.
    carica_config()
    carica_storico()
    try:
        archivia_statistiche()      # i giorni passati rimasti fuori (servizio fermo a mezzanotte)
    except Exception as e:
        log_msg(f"[ERRORE] archivio statistiche: {e}")
    # Legge CONFIG a ogni passo: le modifiche da Telegram valgono subito
    regolatore_pid_instance = RegolatorePID(CONFIG, log=log_msg)
    classico_instance = RegolatoreClassico(
        CONFIG, log=log_msg, log_raro=log_throttled,
        avvisa=lambda messaggio, chiave=None, intervallo=300: notifica(messaggio, chiave, intervallo))
    log_msg(f"[CONFIG] Algoritmo di regolazione: {CONFIG['ALGORITMO']}")
    protezione_instance = ProtezioneContatore(
        CONFIG, log=log_msg,
        avvisa=lambda messaggio: notifica(messaggio, dedup_key='limite_contatore', min_intervallo=900))
    if limite_regolazione(CONFIG) is None:
        log_msg("[CONFIG] Limite del contatore disattivato (LIMITE_CONTATORE = 0)")
    else:
        log_msg(f"[CONFIG] Limite del contatore {CONFIG['LIMITE_CONTATORE']}W: si regola entro "
                f"{limite_regolazione(CONFIG)}W; prelevabile per l'auto {CONFIG['POTENZA_PRELEVABILE']}W")

    monitor = EnergyMonitor()
    contatori_instance = ContatoriEnergia()
    contatori_instance.carica_stato()   # riprende i contatori di oggi dopo un riavvio
    # Dati per Home Assistant e per l'archivio sul server (vedi ponte_mqtt.py)
    ponte_mqtt_instance = PonteMQTT(os.getenv('MQTT_HOST'), os.getenv('MQTT_PORT', 1883),
                                    os.getenv('MQTT_USER'), os.getenv('MQTT_PASSWORD'), log=log_msg)
    wallbox_instance = WallboxController()
    wallbox = wallbox_instance

    # Modalita' e obiettivo: decidono la quota di rete per l'auto
    # (prelevabile_effettivo la chiede a CONFIG['PIANIFICATORE'])
    pianificatore_instance = Pianificatore(
        CONFIG, minimo_colonnina=lambda: wallbox.limiti_potenza()[0], log=log_msg,
        avvisa=notifica, salva=lambda stato: scrivi_json(FILE_PIANO, stato))
    pianificatore_instance.importa(leggi_json(FILE_PIANO, {}))
    CONFIG['PIANIFICATORE'] = pianificatore_instance
    log_msg(f"[PIANO] Carica: {pianificatore_instance.descrizione()}")
    ponte_mqtt_instance.imposta_comandi(
        lambda nome, valore: esegui_comando(nome, valore, 'HOME ASSISTANT'))
    ponte_mqtt_instance.imposta_preset([p['nome'] for p in CONFIG['PRESET']])
    # Il sole previsto entra nell'obiettivo; il meteo arriva da Home Assistant
    ponte_mqtt_instance.imposta_meteo(meteo_ricevuto)
    pianificatore_instance.sole_atteso = sole_atteso
    try:
        aggiorna_previsione(forza=True)
    except Exception as e:
        log_msg(f"[ERRORE] previsione: {e}")

    # 0. AVVIO WORKER NOTIFICHE (prima di tutto: cosi' anche gli errori
    #    di avvio degli altri thread possono essere notificati)
    threading.Thread(target=_worker_notifiche, daemon=True).start()

    # 1. AVVIO THREAD SERVER WEB
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    log_msg(f">>> INTERFACCIA WEB ATTIVA SULLA PORTA {CONFIG['PORT']} <<<")

    # 2. AVVIO THREAD BOT TELEGRAM
    tg_thread = threading.Thread(target=run_telegram_polling)
    tg_thread.daemon = True
    tg_thread.start()

    # systemd ferma il servizio con SIGTERM: intercettarlo per salvare
    signal.signal(signal.SIGTERM, gestore_arresto)

    notifica("✅ SISTEMA AVVIATO.")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    try:
        sock.bind(('0.0.0.0', CONFIG['MCAST_PORT']))
        mreq = struct.pack("4s4s", socket.inet_aton(CONFIG['MCAST_GRP']), socket.inet_aton(CONFIG['IFACE']))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        # Senza timeout, se il multicast si interrompe recvfrom resta bloccato
        # per sempre e il watchdog non potrebbe nemmeno accorgersene.
        sock.settimeout(30)
        log_msg(f"In ascolto su {CONFIG['IFACE']}:{CONFIG['MCAST_PORT']}...")
    except OSError as e:
        logging.critical(f"Errore Rete (Bind): {e}")
        return

    wallbox.initialize(avvio=True)

    # 3. AVVIO THREAD PERIODICO (dopo initialize, per non sovrapporre un
    #    sync_fase alla inizializzazione di avvio)
    threading.Thread(target=thread_periodico, args=(wallbox,), daemon=True).start()
    log_msg(f">>> LETTURA CENTRALINA E CONTROLLO FASE ATTIVI (ogni {CONFIG['INTERVALLO_SYNC_FASE']}s) <<<")

    errori_consecutivi = 0
    ultimo_pacchetto = time.time()
    while True:
        try:
            data, _ = sock.recvfrom(65535)
            ultimo_pacchetto = time.time()
            evt = monitor.parse_packet(data)
            errori_consecutivi = 0

            if evt == "TRIGGER":
                run_logic(monitor, wallbox)

        except socket.timeout:
            # Di notte i pacchetti 'solar' non arrivano e resta un 'electricity'
            # al minuto: 30 s di silenzio sono normali (prima: 300 righe a notte)
            silenzio = time.time() - ultimo_pacchetto
            if silenzio > 150:
                log_throttled('nessun_pacchetto',
                              f"[AVVISO] Nessun pacchetto dal sensore da {silenzio:.0f}s.", 300)
        except KeyboardInterrupt:
            log_msg("Interruzione richiesta: salvo lo stato...")
            salva_tutto()
            salva_config()
            wallbox.turn_off(force=True)
            break
        except Exception as e:
            # Backoff progressivo: senza, un errore persistente diventa un
            # busy-loop che satura la CPU del Raspberry.
            errori_consecutivi += 1
            log_throttled('errore_loop', f"[ERRORE] {e}", 60)
            time.sleep(min(0.5 * errori_consecutivi, 30))

if __name__ == "__main__":
    main()