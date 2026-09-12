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
import io
import datetime
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
from flask import Flask, jsonify, request, render_template_string

# -----------------------------------------------------------
# CONFIGURAZIONE WEB & GLOBALE
# -----------------------------------------------------------
app = Flask(__name__)

# Usiamo un dizionario per i parametri modificabili così sono condivisi tra Thread
CONFIG = {
    'MONOFASE_MIN_POWER': 1380,
    'MONOFASE_MAX_POWER': 7360,
    'TRIFASE_MIN_POWER': 4140,
    'TRIFASE_MAX_POWER': 22000,
    'POTENZA_PROTEZIONE': 300,      # Modificabile da Web e Telegram (persistito)
    'POTENZA_PRELEVABILE': 0,       # Modificabile da Web e Telegram (persistito)
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
    'ALG_MANUALE': '2',             # alg=2 (Man): l'unico in cui btn=P<watt> ha effetto     # Ogni quanti secondi rileggere 'tfase' dalla centralina

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
    'ORA_RIEPILOGO': 23,            # Ora del riepilogo giornaliero Telegram
}

WALLBOX_URL = f"http://{CONFIG['WALLBOX_IP']}/index.json"

# File di stato, accanto allo script (il service imposta WorkingDirectory)
DIR_BASE = os.path.dirname(os.path.abspath(__file__))
FILE_CONFIG = os.path.join(DIR_BASE, 'config_utente.json')
FILE_STORICO = os.path.join(DIR_BASE, 'storico.jsonl')
FILE_STORICO_GIORNALIERO = os.path.join(DIR_BASE, 'storico_giornaliero.json')
FILE_SESSIONI = os.path.join(DIR_BASE, 'sessioni.json')

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
    'WB_ENERGIA_SESSIONE': 0,
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
contatori_instance = None

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

def log_msg(msg):
    """Salva il log sia su terminale che nel buffer per la Web UI"""
    t_str = time.strftime("%H:%M:%S")
    full_msg = f"[{t_str}] {msg}"
    print(full_msg, flush=True)
    # deque(maxlen) scarta da sola i messaggi vecchi: niente pop(0) su list
    SYSTEM_STATE['LOGS'].append(full_msg)

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
    'LIMITE_KWH':          (1, 100, int),
    'PREZZO_ACQUISTO':     (0, 5, float),
    'PREZZO_VENDITA':      (0, 5, float),
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

    log_msg(f"[CONFIG] Caricati da disco: {', '.join(caricati) if caricati else 'nessun valore valido'}")

def salva_config():
    """Salva le chiavi persistenti su disco in modo ATOMICO.

    Scrive su .tmp e poi os.replace(): se il Pi perde corrente a meta' scrittura
    il file originale resta intatto, mai troncato.
    """
    with STATO_LOCK:
        dati = {chiave: CONFIG[chiave] for chiave in CHIAVI_PERSISTENTI}

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
        "/sessioni - Ultime sessioni di ricarica\n"
        "/grafici [15m|1h|6h|24h] - Grafico dell'andamento\n"
        "/fase - Rileva subito monofase/trifase\n\n"
        "*Controllo*\n"
        "/accendi - Forza l'accensione della Wallbox\n"
        "/spegni - Forza lo spegnimento della Wallbox\n"
        "/reset - Re-inizializza la centralina\n\n"
        "*Impostazioni*\n"
        "/setPotenzaPrelevabile <W> - Potenza prelevabile dalla rete\n"
        "/setPotenzaProtezione <W> - Soglia di protezione\n"
        "/limite <kWh> - Energia da caricare (1-100)\n"
    )
    await update.message.reply_text(msg, parse_mode='Markdown')

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
        limite = CONFIG['LIMITE_KWH']

    tot_grid = sum(fasi[0:3])
    tot_solar = sum(fasi[3:6])
    wb_status = "🟢 ON" if wb_on else "🔴 OFF"
    modalita = "Trifase" if fase_mode == 1 else "Monofase"

    msg = (
        "📊 *Stato Sistema*\n\n"
        f"☀️ *Solare:* {tot_solar:.0f} W\n"
        f"🔌 *Rete:* {tot_grid:.0f} W\n"
        f"🚗 *Wallbox:* {wb_status} ({wb_power:.0f} W)\n"
        f"⚙️ *Modalità:* {modalita}\n"
        f"🛠️ *Prelevabile:* {prelevabile} W\n"
        f"🛡️ *Protezione:* {protezione} W\n"
        f"🔋 *Limite carica:* {limite} kWh\n"
    )
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

async def cmd_set_prelevabile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update): return
    if not context.args:
        await update.message.reply_text(
            f"🛠️ *Potenza Prelevabile* attuale: {CONFIG['POTENZA_PRELEVABILE']} W\n"
            "Usa: `/prelevabile 1000`", parse_mode='Markdown')
        return
    ok, errore = applica_impostazione('POTENZA_PRELEVABILE', context.args[0], 'TELEGRAM')
    if ok:
        await update.message.reply_text(
            f"✅ *Potenza Prelevabile* impostata a {CONFIG['POTENZA_PRELEVABILE']} W", parse_mode='Markdown')
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
        letto = SYSTEM_STATE.get('LIMITE_KWH_CENTRALINA')
        msg = f"🔋 *Limite di carica:* {CONFIG['LIMITE_KWH']} kWh\n"
        if letto is not None:
            msg += f"_Letto dalla centralina: {letto}_\n"
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
        kwh = SYSTEM_STATE.get('WB_ENERGIA_SESSIONE') or 0.0
        corso = f"▶️ *In corso:* {minuti:.0f} min, {kwh:.2f} kWh\n\n"
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
            "⚠️ Intervallo non valido. Usa: `/grafici 15m`, `1h`, `6h` o `24h`", parse_mode='Markdown')
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
# INTERFACCIA WEB (HTML/JS)
# -----------------------------------------------------------
# (Il template HTML rimane invariato, l'ho tenuto per completezza dello script)
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="it">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="color-scheme" content="light dark">
    <title>Solar Controller</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        :root {
            --sfondo:#f1f5f9; --superficie:#fff; --superficie2:#f8fafc; --bordo:#e2e8f0;
            --testo:#0f172a; --testo2:#64748b;
            --sole:#10b981; --auto:#3b82f6; --rete:#ef4444; --casa:#f59e0b;
            --ok:#10b981; --avviso:#f59e0b; --errore:#ef4444;
            --ombra:0 1px 2px rgba(15,23,42,.06),0 4px 12px rgba(15,23,42,.04);
            --raggio:14px;
        }
        @media (prefers-color-scheme: dark) {
            :root {
                --sfondo:#0b1120; --superficie:#131c31; --superficie2:#1b2540; --bordo:#263149;
                --testo:#e8edf7; --testo2:#94a3b8;
                --ombra:0 1px 2px rgba(0,0,0,.4),0 4px 16px rgba(0,0,0,.3);
            }
        }
        * { box-sizing:border-box; }
        body {
            margin:0; padding:20px 16px 48px; background:var(--sfondo); color:var(--testo);
            font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Inter,sans-serif;
            font-size:15px; line-height:1.5; -webkit-font-smoothing:antialiased;
        }
        .wrap { max-width:1120px; margin:0 auto; }

        .top { display:flex; align-items:center; justify-content:space-between;
               gap:16px; flex-wrap:wrap; margin-bottom:22px; }
        .top h1 { margin:0; font-size:1.45rem; font-weight:650; letter-spacing:-.02em; }
        .top .sub { color:var(--testo2); font-size:.85rem; margin-top:2px; }
        .pillole { display:flex; gap:8px; flex-wrap:wrap; }
        .pillola { display:inline-flex; align-items:center; gap:6px; background:var(--superficie);
                   border:1px solid var(--bordo); padding:6px 13px; border-radius:999px;
                   font-size:.82rem; font-weight:500; }
        .punto { width:8px; height:8px; border-radius:50%; background:var(--testo2); flex:none; }
        .punto.on   { background:var(--ok);     box-shadow:0 0 0 3px color-mix(in srgb,var(--ok) 22%,transparent); }
        .punto.off  { background:var(--testo2); }
        .punto.warn { background:var(--avviso); box-shadow:0 0 0 3px color-mix(in srgb,var(--avviso) 22%,transparent); }
        .punto.err  { background:var(--errore); box-shadow:0 0 0 3px color-mix(in srgb,var(--errore) 22%,transparent); }

        .card { background:var(--superficie); border:1px solid var(--bordo); border-radius:var(--raggio);
                padding:20px; box-shadow:var(--ombra); margin-bottom:16px; }
        .card h2 { margin:0 0 16px; font-size:.78rem; font-weight:650; text-transform:uppercase;
                   letter-spacing:.07em; color:var(--testo2); }
        .colonne { display:grid; grid-template-columns:1.35fr 1fr; gap:16px; align-items:start; }
        @media (max-width:860px) { .colonne { grid-template-columns:1fr; } }

        .flusso { display:grid; grid-template-columns:repeat(4,1fr); gap:10px; }
        @media (max-width:620px) { .flusso { grid-template-columns:repeat(2,1fr); } }
        .nodo { background:var(--superficie2); border:1px solid var(--bordo); border-radius:12px;
                padding:14px 12px; text-align:center; }
        .nodo .ico { font-size:1.3rem; line-height:1; }
        .nodo .val { font-size:1.45rem; font-weight:680; margin-top:7px; letter-spacing:-.02em;
                     font-variant-numeric:tabular-nums; }
        .nodo .um { font-size:.74rem; font-weight:500; color:var(--testo2); margin-left:2px; }
        .nodo .lab { font-size:.76rem; color:var(--testo2); margin-top:3px; }
        .nodo.sole .val { color:var(--sole); }
        .nodo.auto .val { color:var(--auto); }
        .nodo.rete .val { color:var(--rete); }
        .nodo.casa .val { color:var(--casa); }

        .tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(120px,1fr)); gap:10px; }
        .tile { background:var(--superficie2); border:1px solid var(--bordo); border-radius:11px; padding:13px; }
        .tile .v { font-size:1.25rem; font-weight:660; letter-spacing:-.02em; font-variant-numeric:tabular-nums; }
        .tile .l { font-size:.74rem; color:var(--testo2); margin-top:3px; }

        .eff { margin-top:18px; }
        .eff-top { display:flex; justify-content:space-between; align-items:baseline;
                   font-size:.84rem; margin-bottom:7px; }
        .eff-top b { font-size:1.3rem; font-weight:680; color:var(--sole); font-variant-numeric:tabular-nums; }
        .barra { height:9px; background:var(--superficie2); border:1px solid var(--bordo);
                 border-radius:999px; overflow:hidden; }
        .barra > i { display:block; height:100%; width:0;
                     background:linear-gradient(90deg,var(--casa),var(--sole)); transition:width .5s ease; }

        .campo { margin-bottom:15px; }
        .campo label { display:block; font-size:.82rem; font-weight:550; color:var(--testo2); margin-bottom:6px; }
        input[type=number], input[type=range] { width:100%; font-family:inherit; font-size:.95rem; }
        input[type=number] { padding:9px 11px; border:1px solid var(--bordo); border-radius:9px;
                             background:var(--superficie2); color:var(--testo); font-variant-numeric:tabular-nums; }
        input[type=number]:focus { outline:none; border-color:var(--auto);
                                   box-shadow:0 0 0 3px color-mix(in srgb,var(--auto) 18%,transparent); }
        input[type=range] { accent-color:var(--auto); cursor:pointer; margin:4px 0; }

        button { font-family:inherit; font-size:.9rem; font-weight:560; cursor:pointer;
                 border:1px solid transparent; border-radius:10px; padding:10px 16px;
                 transition:filter .15s,background .15s; }
        button:hover:not(:disabled) { filter:brightness(1.06); }
        button:disabled { opacity:.5; cursor:not-allowed; }
        .b-primario { background:var(--auto); color:#fff; width:100%; }
        .b-secondario { background:var(--superficie2); color:var(--testo);
                        border-color:var(--bordo); width:100%; margin-top:9px; }
        .b-limite { width:100%; margin-top:8px; background:var(--superficie2);
                    color:var(--testo); border-color:var(--bordo); }
        .b-limite[data-attivo="1"] { background:var(--sole); color:#fff; border-color:var(--sole); }

        .nota { font-size:.76rem; color:var(--testo2); margin-top:6px; }
        .esito { margin-top:13px; padding:10px 13px; border-radius:9px; font-size:.84rem; white-space:pre-line; }
        .esito.ok { background:color-mix(in srgb,var(--ok) 14%,transparent); color:var(--ok); }
        .esito.ko { background:color-mix(in srgb,var(--errore) 14%,transparent); color:var(--errore); }

        .avviso-box { display:flex; gap:9px; align-items:flex-start;
                      background:color-mix(in srgb,var(--avviso) 13%,transparent);
                      border:1px solid color-mix(in srgb,var(--avviso) 32%,transparent);
                      color:var(--testo); padding:11px 13px; border-radius:10px;
                      font-size:.84rem; margin-bottom:10px; }

        .riga { display:flex; justify-content:space-between; align-items:center; padding:8px 0;
                border-bottom:1px solid var(--bordo); font-size:.88rem; }
        .riga:last-child { border-bottom:none; }
        .riga .n { color:var(--testo2); font-weight:500; }
        .riga .w { font-variant-numeric:tabular-nums; font-weight:560; }
        .tot { display:flex; justify-content:space-between; margin-top:9px; padding:10px 12px;
               background:var(--superficie2); border-radius:9px; font-weight:620; font-size:.9rem; }
        .tot .w { font-variant-numeric:tabular-nums; }

        .intervalli { display:flex; gap:6px; margin-bottom:14px; flex-wrap:wrap; }
        .i-btn { background:var(--superficie2); color:var(--testo2); border:1px solid var(--bordo);
                 padding:6px 14px; border-radius:999px; font-size:.82rem; width:auto; }
        .i-btn.attivo { background:var(--auto); color:#fff; border-color:var(--auto); }
        .grafico-box { position:relative; height:320px; }

        .console { background:#0a0f1c; color:#4ade80; border:1px solid var(--bordo);
                   font-family:ui-monospace,"SF Mono",Menlo,Consolas,monospace; height:240px;
                   overflow-y:auto; padding:14px; border-radius:11px; font-size:.78rem;
                   line-height:1.65; white-space:pre-wrap; word-break:break-word; }
        .meta { display:flex; justify-content:space-between; font-size:.76rem; color:var(--testo2);
                margin-top:12px; flex-wrap:wrap; gap:8px; }
    </style>
</head>
<body>
<div class="wrap">

    <div class="top">
        <div>
            <h1>Solar Controller</h1>
            <div class="sub">Regolazione della ricarica da surplus fotovoltaico</div>
        </div>
        <div class="pillole">
            <span class="pillola"><span class="punto" id="p_wb"></span><span id="t_wb">--</span></span>
            <span class="pillola"><span class="punto" id="p_auto"></span><span id="t_auto">--</span></span>
            <span class="pillola"><span class="punto" id="p_fase"></span><span id="t_fase">--</span></span>
        </div>
    </div>

    <div id="avvisi"></div>

    <div class="card">
        <h2>Flusso di potenza</h2>
        <div class="flusso">
            <div class="nodo sole"><div class="ico">&#9728;&#65039;</div><div class="val"><span id="f_sole">0</span><span class="um">W</span></div><div class="lab">Produzione</div></div>
            <div class="nodo casa"><div class="ico">&#127968;</div><div class="val"><span id="f_casa">0</span><span class="um">W</span></div><div class="lab">Casa</div></div>
            <div class="nodo auto"><div class="ico">&#128663;</div><div class="val"><span id="f_auto">0</span><span class="um">W</span></div><div class="lab" id="f_auto_lab">Wallbox</div></div>
            <div class="nodo rete"><div class="ico">&#9889;</div><div class="val"><span id="f_rete">0</span><span class="um">W</span></div><div class="lab">Rete</div></div>
        </div>
        <div class="meta">
            <span>Fasi: <span id="last_fasi">--</span> <span id="sec_fasi"></span></span>
            <span>Solare: <span id="last_solar">--</span> <span id="sec_solar"></span></span>
        </div>
    </div>

    <div class="colonne">
        <div>
            <div class="card">
                <h2>Energia di oggi</h2>
                <div class="tiles">
                    <div class="tile"><div class="v" id="e_solare">--</div><div class="l">Prodotta (kWh)</div></div>
                    <div class="tile"><div class="v" id="e_import">--</div><div class="l">Importata (kWh)</div></div>
                    <div class="tile"><div class="v" id="e_export">--</div><div class="l">Esportata (kWh)</div></div>
                    <div class="tile"><div class="v" id="e_wb">--</div><div class="l">In auto (kWh)</div></div>
                    <div class="tile"><div class="v" id="e_wb_fv">--</div><div class="l">Da solare (kWh)</div></div>
                    <div class="tile"><div class="v" id="e_tempo">--</div><div class="l">Carica (min)</div></div>
                </div>
                <div class="eff">
                    <div class="eff-top"><span>Efficienza di carica</span><b id="e_eff">--</b></div>
                    <div class="barra"><i id="e_eff_bar"></i></div>
                    <div class="nota" id="nota_stima">Quota di ricarica coperta dal fotovoltaico.</div>
                </div>
            </div>

            <div class="card">
                <h2>Andamento</h2>
                <div class="intervalli">
                    <button class="i-btn attivo" data-r="live" onclick="cambiaRange('live')">Live</button>
                    <button class="i-btn" data-r="1h" onclick="cambiaRange('1h')">1 ora</button>
                    <button class="i-btn" data-r="6h" onclick="cambiaRange('6h')">6 ore</button>
                    <button class="i-btn" data-r="24h" onclick="cambiaRange('24h')">24 ore</button>
                </div>
                <div class="grafico-box"><canvas id="grafico"></canvas></div>
            </div>

            <div class="card">
                <h2>Ultimi giorni</h2>
                <div class="grafico-box" style="height:260px"><canvas id="grafico_giorni"></canvas></div>
                <div class="tiles" style="margin-top:16px">
                    <div class="tile"><div class="v" id="g_solare">--</div><div class="l">Prodotti (kWh)</div></div>
                    <div class="tile"><div class="v" id="g_auto">--</div><div class="l">In auto (kWh)</div></div>
                    <div class="tile"><div class="v" id="g_eff">--</div><div class="l">Da solare</div></div>
                    <div class="tile"><div class="v" id="g_eur">--</div><div class="l">Risparmio stimato</div></div>
                </div>
            </div>

            <div class="card">
                <h2>Dettaglio fasi</h2>
                <div class="colonne" style="gap:22px">
                    <div>
                        <div class="riga"><span class="n">L1 rete</span><span class="w"><span id="l1">0</span> W</span></div>
                        <div class="riga"><span class="n">L2 rete</span><span class="w"><span id="l2">0</span> W</span></div>
                        <div class="riga"><span class="n">L3 rete</span><span class="w"><span id="l3">0</span> W</span></div>
                        <div class="tot"><span>Totale rete</span><span class="w"><span id="tot_grid">0</span> W</span></div>
                    </div>
                    <div>
                        <div class="riga"><span class="n">L4 solare</span><span class="w"><span id="l4">0</span> W</span></div>
                        <div class="riga"><span class="n">L5 solare</span><span class="w"><span id="l5">0</span> W</span></div>
                        <div class="riga"><span class="n">L6 solare</span><span class="w"><span id="l6">0</span> W</span></div>
                        <div class="tot"><span>Totale solare</span><span class="w"><span id="tot_solar">0</span> W</span></div>
                    </div>
                </div>
            </div>
        </div>

        <div>
            <div class="card">
                <h2>Impostazioni</h2>

                <div class="campo">
                    <label for="prelevabile">Potenza prelevabile dalla rete (W)</label>
                    <input type="number" id="prelevabile" min="0" max="20000" oninput="marcaSporco(event)">
                </div>

                <div class="campo">
                    <label for="protezione">Soglia di protezione (W)</label>
                    <input type="number" id="protezione" min="50" max="5000" oninput="marcaSporco(event)">
                    <div class="nota">Variazioni piu' piccole non vengono inviate alla centralina.</div>
                </div>

                <div class="campo">
                    <label for="limite">Energia da caricare &mdash; <b id="limite_val">--</b> kWh
                        (<span id="limite_stato">--</span>)</label>
                    <input type="range" id="limite" min="1" max="100" step="1"
                           oninput="marcaSporco(event); document.getElementById('limite_val').textContent = this.value;">
                    <button type="button" class="b-limite" id="btn_limite_toggle" onclick="toggleLimite()">--</button>
                    <div class="nota" id="nota_limite" hidden>
                        Comando centralina non configurato: il valore viene salvato ma non inviato.
                    </div>
                    <div class="nota">
                        Se la centralina usa l'energia stimata (nessun contatore reale), l'energia
                        effettivamente erogata puo' essere inferiore al valore impostato.
                    </div>
                </div>

                <div class="colonne" style="gap:10px">
                    <div class="campo" style="margin-bottom:0">
                        <label for="prezzo_acquisto">Prezzo acquisto (&euro;/kWh)</label>
                        <input type="number" id="prezzo_acquisto" min="0" max="5" step="0.01"
                               oninput="marcaSporco(event)">
                    </div>
                    <div class="campo" style="margin-bottom:0">
                        <label for="prezzo_vendita">Prezzo vendita (&euro;/kWh)</label>
                        <input type="number" id="prezzo_vendita" min="0" max="5" step="0.01"
                               oninput="marcaSporco(event)">
                    </div>
                </div>
                <div class="nota" style="margin-bottom:15px">
                    Servono solo a stimare il risparmio: caricare col proprio sole evita
                    l'acquisto ma rinuncia alla vendita.
                </div>

                <button class="b-primario" onclick="salva()">Salva impostazioni</button>
                <button class="b-secondario" onclick="reinit()">Re-inizializza wallbox</button>
                <div id="esito" class="esito" hidden></div>
            </div>

            <div class="card">
                <h2>Centralina</h2>
                <div class="riga"><span class="n">Stato</span><span class="w" id="c_desc">--</span></div>
                <div class="riga"><span class="n">Potenza impostata</span><span class="w"><span id="c_set">0</span> W</span></div>
                <div class="riga"><span class="n">Potenza misurata</span><span class="w"><span id="c_pcar">--</span> W</span></div>
                <div class="riga"><span class="n">Sessione</span><span class="w"><span id="c_sess">--</span> kWh</span></div>
                <div class="riga"><span class="n">Temperatura presa</span><span class="w"><span id="c_temp">--</span> &deg;C</span></div>
                <div class="riga"><span class="n">Limite centralina</span><span class="w" id="c_limite">--</span></div>
            </div>

            <div class="card">
                <h2>Sessioni di ricarica</h2>
                <div id="sessione_corso" hidden></div>
                <div id="elenco_sessioni"></div>
            </div>

            <div class="card">
                <h2>Console</h2>
                <div id="console" class="console"></div>
            </div>
        </div>
    </div>
</div>

<script>
const $ = id => document.getElementById(id);
const num = v => (v === null || v === undefined) ? '--' : Math.round(v).toLocaleString('it-IT');

/* I campi non vanno sovrascritti dal polling se l'utente ci sta scrivendo
   (activeElement) o ha gia' modificato senza salvare (sporchi). */
const sporchi = new Set();
function marcaSporco(e) { sporchi.add(e.target.id); }

function setCampo(id, valore) {
    const el = $(id);
    if (!el || document.activeElement === el || sporchi.has(id)) return;
    if (el.type === 'checkbox') el.checked = !!valore; else el.value = valore;
}

function esito(testo, ok) {
    const b = $('esito');
    b.textContent = testo;
    b.className = 'esito ' + (ok ? 'ok' : 'ko');
    b.hidden = false;
    if (ok) setTimeout(() => { b.hidden = true; }, 4000);
}

function stile() {
    const c = getComputedStyle(document.documentElement);
    return { sole: c.getPropertyValue('--sole').trim(), auto: c.getPropertyValue('--auto').trim(),
             rete: c.getPropertyValue('--rete').trim(), testo2: c.getPropertyValue('--testo2').trim(),
             bordo: c.getPropertyValue('--bordo').trim() };
}

const col = stile();
const grafico = new Chart($('grafico').getContext('2d'), {
    type: 'line',
    data: { labels: [], datasets: [
        { label: 'Solare', borderColor: col.sole, backgroundColor: col.sole + '26',
          data: [], fill: true, tension: .3, pointRadius: 0, borderWidth: 2 },
        { label: 'Wallbox', borderColor: col.auto, backgroundColor: col.auto + '1f',
          data: [], fill: true, tension: .3, pointRadius: 0, borderWidth: 2 },
        { label: 'Rete', borderColor: col.rete, backgroundColor: 'transparent',
          data: [], fill: false, tension: .3, pointRadius: 0, borderWidth: 1.8 }
    ]},
    options: {
        responsive: true, maintainAspectRatio: false, animation: { duration: 0 },
        interaction: { mode: 'index', intersect: false },
        plugins: {
            legend: { labels: { color: col.testo2, usePointStyle: true, pointStyle: 'line',
                                boxWidth: 22, font: { size: 12 } } },
            tooltip: { callbacks: { label: c => c.dataset.label + ': ' + num(c.parsed.y) + ' W' } }
        },
        scales: {
            x: { ticks: { color: col.testo2, maxTicksLimit: 8, font: { size: 11 } },
                 grid: { color: col.bordo, drawTicks: false } },
            y: { beginAtZero: true, ticks: { color: col.testo2, font: { size: 11 }, callback: v => num(v) },
                 grid: { color: col.bordo, drawTicks: false } }
        }
    }
});

function oraDi(ts) { return ts ? new Date(ts * 1000).toLocaleTimeString('it-IT') : '--'; }

/* ---------- storico giornaliero (barre impilate) ---------- */
const grafGiorni = new Chart($('grafico_giorni').getContext('2d'), {
    type: 'bar',
    data: { labels: [], datasets: [
        { label: 'In auto da solare', data: [], backgroundColor: col.sole, stack: 'a' },
        { label: 'In auto da rete', data: [], backgroundColor: col.casa, stack: 'a' },
        { label: 'Prodotti dal FV', data: [], type: 'line', borderColor: col.auto,
          backgroundColor: 'transparent', borderWidth: 2, pointRadius: 2, tension: .3 }
    ]},
    options: {
        responsive: true, maintainAspectRatio: false, animation: { duration: 0 },
        interaction: { mode: 'index', intersect: false },
        plugins: {
            legend: { labels: { color: col.testo2, usePointStyle: true, boxWidth: 12,
                                font: { size: 11 } } },
            tooltip: { callbacks: { label: c => c.dataset.label + ': ' + c.parsed.y.toFixed(1) + ' kWh' } }
        },
        scales: {
            x: { stacked: true, ticks: { color: col.testo2, font: { size: 10 } },
                 grid: { display: false } },
            y: { stacked: true, beginAtZero: true,
                 ticks: { color: col.testo2, font: { size: 10 } },
                 grid: { color: col.bordo, drawTicks: false } }
        }
    }
});

async function caricaGiorni() {
    try {
        const d = await (await fetch('/api/giorni?quanti=14')).json();
        if (!d.success) return;
        const g = d.giorni;
        grafGiorni.data.labels = g.map(x => (x.giorno || '').slice(5).replace('-', '/'));
        const daFv = g.map(x => x.wallbox_da_fv_kwh || 0);
        grafGiorni.data.datasets[0].data = daFv;
        grafGiorni.data.datasets[1].data = g.map((x, i) => Math.max(0, (x.wallbox_kwh || 0) - daFv[i]));
        grafGiorni.data.datasets[2].data = g.map(x => x.solare_kwh || 0);
        grafGiorni.update();

        const somma = (k) => g.reduce((a, x) => a + (x[k] || 0), 0);
        const totWb = somma('wallbox_kwh'), totFv = somma('wallbox_da_fv_kwh');
        $('g_solare').textContent = somma('solare_kwh').toFixed(0);
        $('g_auto').textContent = totWb.toFixed(0);
        $('g_eff').textContent = totWb > 0 ? Math.round(totFv / totWb * 100) + '%' : '--';
        $('g_eur').textContent = somma('risparmio_eur').toFixed(2) + ' €';
    } catch (e) { console.error(e); }
}

/* ---------- sessioni di ricarica ---------- */
async function caricaSessioni() {
    try {
        const d = await (await fetch('/api/sessioni?quante=8')).json();
        if (!d.success) return;

        const corso = $('sessione_corso');
        if (d.in_corso) {
            corso.hidden = false;
            corso.className = 'tot';
            corso.textContent = 'In corso: ' + d.in_corso.minuti + ' min · '
                              + d.in_corso.kwh.toFixed(2) + ' kWh';
        } else {
            corso.hidden = true;
        }

        const box = $('elenco_sessioni');
        box.textContent = '';
        if (!d.sessioni.length) {
            const v = document.createElement('div');
            v.className = 'nota';
            v.textContent = 'Nessuna sessione registrata finora.';
            box.appendChild(v);
            return;
        }
        d.sessioni.forEach(s => {
            const r = document.createElement('div');
            r.className = 'riga';
            const quando = new Date(s.inizio * 1000).toLocaleString('it-IT',
                { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
            const sx = document.createElement('span');
            sx.className = 'n';
            sx.textContent = quando + ' · ' + s.minuti + ' min';
            const dx = document.createElement('span');
            dx.className = 'w';
            dx.textContent = s.kwh.toFixed(2) + ' kWh'
                           + (s.quota_fv !== null && s.quota_fv !== undefined
                              ? ' · ' + Math.round(s.quota_fv) + '% sole' : '');
            r.appendChild(sx); r.appendChild(dx);
            box.appendChild(r);
        });
    } catch (e) { console.error(e); }
}

function disegna(punti) {
    const lungo = punti.length > 1 && (punti[punti.length - 1].time - punti[0].time) > 86400;
    grafico.data.labels = punti.map(p => {
        const d = new Date(p.time * 1000);
        return lungo ? d.toLocaleString('it-IT', {day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'})
                     : d.toLocaleTimeString('it-IT', {hour:'2-digit',minute:'2-digit'});
    });
    grafico.data.datasets[0].data = punti.map(p => p.solar);
    grafico.data.datasets[1].data = punti.map(p => p.wb);
    grafico.data.datasets[2].data = punti.map(p => p.grid);
    grafico.update();
}

let range = 'live';
async function cambiaRange(r) {
    range = r;
    document.querySelectorAll('.i-btn').forEach(b => b.classList.toggle('attivo', b.dataset.r === r));
    if (r === 'live') { aggiorna(); return; }
    try {
        const res = await fetch('/api/storico?range=' + r);
        const d = await res.json();
        if (d.success) disegna(d.punti);
    } catch (e) { console.error(e); }
}

function pillola(punto, testo, stato, etichetta) {
    $(punto).className = 'punto ' + stato;
    $(testo).textContent = etichetta;
}

async function aggiorna() {
    let d;
    try { d = await (await fetch('/api/data')).json(); } catch (e) { return; }
    const s = d.status, c = d.config, en = d.energia || {};

    pillola('p_wb', 't_wb', s.wb_on ? 'on' : 'off', s.wb_on ? 'Wallbox attiva' : 'Wallbox ferma');
    const collegata = s.wb_status && s.wb_status !== '0';
    pillola('p_auto', 't_auto', collegata ? 'on' : 'off', s.wb_desc || 'Auto: --');
    pillola('p_fase', 't_fase', 'on', s.fase_mode === 1 ? 'Trifase' : 'Monofase');

    const av = [];
    if (!s.centralina_online) av.push('Centralina non raggiungibile: i comandi non arrivano.');
    if (!s.sensore_online) av.push('Nessun dato dal sensore: la regolazione resta sull ultimo valore.');
    if (s.manual_off) av.push('Override manuale attivo. Usa /accendi su Telegram per riprendere.');
    if (s.alg_manuale === false) av.push('La centralina non e in modalita Man: i comandi di potenza vengono ignorati.');
    const boxAv = $('avvisi');
    boxAv.textContent = '';
    av.forEach(t => {
        const el = document.createElement('div');
        el.className = 'avviso-box';
        el.textContent = t;
        boxAv.appendChild(el);
    });

    const solare = s.solar_total || 0;
    const misurata = (s.wb_pcar !== null && s.wb_pcar !== undefined);
    const wbW = misurata ? s.wb_pcar : (s.wb_on ? s.wb_power : 0);
    $('f_sole').textContent = num(solare);
    $('f_casa').textContent = num(Math.max(0, (s.grid_total || 0) - wbW));
    $('f_auto').textContent = num(wbW);
    $('f_rete').textContent = num(Math.abs((s.grid_total || 0) - solare));
    $('f_auto_lab').textContent = misurata ? 'Wallbox (misurata)' : 'Wallbox (stimata)';

    $('last_fasi').textContent = oraDi(s.last_fasi);
    $('sec_fasi').textContent = s.last_fasi ? '(' + Math.max(0, Math.round(s.server_time - s.last_fasi)) + 's fa)' : '';
    $('last_solar').textContent = oraDi(s.last_solar);
    $('sec_solar').textContent = s.last_solar ? '(' + Math.max(0, Math.round(s.server_time - s.last_solar)) + 's fa)' : '';

    (s.fasi || []).forEach((v, i) => { const el = $('l' + (i + 1)); if (el) el.textContent = num(v); });
    $('tot_grid').textContent = num(s.grid_total);
    $('tot_solar').textContent = num(s.solar_total);

    $('c_desc').textContent = s.wb_desc || '--';
    $('c_set').textContent = num(s.wb_power);
    $('c_pcar').textContent = num(s.wb_pcar);
    $('c_sess').textContent = (s.wb_energia_sessione === null || s.wb_energia_sessione === undefined) ? '--' : s.wb_energia_sessione;
    $('c_temp').textContent = (s.wb_temp_presa === null || s.wb_temp_presa === undefined) ? '--' : Math.round(s.wb_temp_presa);

    const set = (id, v) => { $(id).textContent = (v === null || v === undefined) ? '--' : v; };
    set('e_solare', en.solare_kwh); set('e_import', en.rete_importata_kwh);
    set('e_export', en.rete_esportata_kwh); set('e_wb', en.wallbox_kwh);
    set('e_wb_fv', en.wallbox_da_fv_kwh); set('e_tempo', en.minuti_carica);
    const eff = en.efficienza;
    $('e_eff').textContent = (eff === null || eff === undefined) ? '--' : eff + '%';
    $('e_eff_bar').style.width = ((eff === null || eff === undefined) ? 0 : eff) + '%';
    $('nota_stima').textContent = misurata
        ? 'Quota di ricarica coperta dal fotovoltaico (potenza misurata dalla centralina).'
        : 'Quota di ricarica coperta dal fotovoltaico. Valori stimati: la centralina non riporta la potenza misurata.';

    setCampo('prelevabile', c.prelevabile);
    setCampo('protezione', c.protezione);
    setCampo('limite', c.limite);
    setCampo('prezzo_acquisto', c.prezzo_acquisto);
    setCampo('prezzo_vendita', c.prezzo_vendita);
    $('limite_val').textContent = $('limite').value || c.limite;
    $('nota_limite').hidden = !!s.limite_supportato;

    /* Stato reale del limite: sempre quello letto dalla centralina
       (campo 'limit' > 0 = attivo), mai un flag locale che potrebbe
       disallinearsi da quanto l'hardware ricorda per conto suo. */
    const limiteAttivo = Number(s.limite_kwh_centralina) > 0;
    const btn = $('btn_limite_toggle');
    $('limite_stato').textContent = limiteAttivo ? 'attivo' : 'disattivo';
    $('c_limite').textContent = limiteAttivo ? s.limite_kwh_centralina + ' kWh' : 'nessuno';
    btn.textContent = limiteAttivo ? 'Disattiva limite' : 'Attiva limite';
    btn.dataset.attivo = limiteAttivo ? '1' : '0';
    btn.disabled = !s.limite_supportato;

    const cons = $('console');
    const giu = cons.scrollHeight - cons.clientHeight <= cons.scrollTop + 6;
    cons.textContent = (d.logs || []).join('\\n');   /* textContent: niente markup interpretato */
    if (giu) cons.scrollTop = cons.scrollHeight;

    if (range === 'live') disegna(d.history || []);
}

async function salva() {
    const payload = {}, errori = [];
    const leggi = (id, etichetta) => {
        const raw = $(id).value.trim();
        if (raw === '') { errori.push(etichetta + ': campo vuoto'); return; }
        const v = Number(raw);
        if (!Number.isFinite(v)) { errori.push(etichetta + ': valore non numerico'); return; }
        payload[id] = Math.round(v);
    };
    leggi('prelevabile', 'Potenza prelevabile');
    leggi('protezione', 'Soglia di protezione');
    payload.limite = Number($('limite').value);
    const prezzo = (id, etichetta) => {
        const raw = $(id).value.trim();
        if (raw === '') { errori.push(etichetta + ': campo vuoto'); return; }
        const v = Number(raw);
        if (!Number.isFinite(v) || v < 0) { errori.push(etichetta + ': valore non valido'); return; }
        payload[id] = v;
    };
    prezzo('prezzo_acquisto', 'Prezzo acquisto');
    prezzo('prezzo_vendita', 'Prezzo vendita');
    if (errori.length) { esito('Correggi:\\n' + errori.join('\\n'), false); return; }

    try {
        const r = await fetch('/api/settings', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const res = await r.json().catch(() => ({}));
        if (!r.ok || !res.success) {
            esito('Salvataggio non riuscito:\\n' + (res.errori || ['errore sconosciuto']).join('\\n'), false);
            return;
        }
        sporchi.clear();
        esito('Impostazioni salvate.', true);
        aggiorna();
    } catch (e) { esito('Errore di rete: ' + e, false); }
}

async function toggleLimite() {
    const btn = $('btn_limite_toggle');
    const nuovoStato = btn.dataset.attivo !== '1';
    btn.disabled = true;
    /* Attivando si manda il valore ATTUALE dello slider (anche se non ancora
       salvato): altrimenti si riattiverebbe silenziosamente un valore vecchio. */
    const payload = { attivo: nuovoStato };
    if (nuovoStato) payload.kwh = Number($('limite').value);
    try {
        const r = await fetch('/api/limite_toggle', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const res = await r.json().catch(() => ({}));
        if (!r.ok || !res.success) {
            esito('Errore: ' + (res.error || res.messaggio || 'sconosciuto'), false);
            return;
        }
        sporchi.delete('limite');
        esito(res.messaggio || 'Fatto.', true);
    } catch (e) {
        esito('Errore di rete: ' + e, false);
    } finally {
        aggiorna();   /* rilegge lo stato reale e riabilita il bottone */
    }
}

async function reinit() {
    if (!confirm('Forzare la re-inizializzazione della wallbox?')) return;
    try {
        const r = await fetch('/api/init_wallbox', { method: 'POST' });
        const res = await r.json();
        if (r.ok && res.success) { esito('Re-inizializzazione avviata. Segui la console.', true); aggiorna(); }
        else esito('Errore: ' + (res.error || 'comando non riuscito'), false);
    } catch (e) { esito('Errore di rete: ' + e, false); }
}

aggiorna();
caricaGiorni();
caricaSessioni();
setInterval(aggiorna, 2000);
/* Giorni e sessioni cambiano di rado: bastano 60s, non serve il polling da 2s */
setInterval(caricaGiorni, 60000);
setInterval(caricaSessioni, 30000);
setInterval(() => { if (range !== 'live') cambiaRange(range); }, 60000);
</script>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

# Mappa nomi campo web -> chiavi CONFIG, cosi' la UI non conosce i nomi interni
CAMPI_WEB = {
    'prelevabile': 'POTENZA_PRELEVABILE',
    'protezione':  'POTENZA_PROTEZIONE',
    'limite':      'LIMITE_KWH',
    'prezzo_acquisto': 'PREZZO_ACQUISTO',
    'prezzo_vendita':  'PREZZO_VENDITA',
}

@app.route('/api/data')
def get_data():
    # /api/data e' chiamata ogni 2s: restituisce solo la finestra recente.
    # Lo storico lungo ha il suo endpoint (/api/storico) e la sua cadenza,
    # altrimenti si trasferirebbero megabyte 30 volte al minuto.
    # serie_storico ordina e sottocampiona: senza ordinamento anche il grafico
    # live disegnerebbe linee che rimbalzano tra i campioni ricaricati da disco
    # e quelli nuovi.
    history = serie_storico(3600, max_punti=240)

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
            'limite_supportato': bool(CONFIG['CMD_LIMITE_TEMPLATE']),
            # Dati letti direttamente dalla centralina
            'wb_desc': SYSTEM_STATE['WB_DESC'],
            'wb_status': SYSTEM_STATE['WB_STATUS'],
            'wb_pcar': SYSTEM_STATE['WB_PCAR'],
            'wb_energia_sessione': SYSTEM_STATE['WB_ENERGIA_SESSIONE'],
            'wb_tempo_sessione': SYSTEM_STATE['WB_TEMPO_SESSIONE'],
            'wb_temp_presa': SYSTEM_STATE['WB_TEMP_PRESA'],
            'wb_alg': SYSTEM_STATE['WB_ALG'],
            'alg_manuale': SYSTEM_STATE['WB_ALG'] in ('', CONFIG['ALG_MANUALE']),
            'limite_kwh_centralina': SYSTEM_STATE.get('LIMITE_KWH_CENTRALINA'),
        }
        configurazione = {
            'prelevabile': CONFIG['POTENZA_PRELEVABILE'],
            'protezione': CONFIG['POTENZA_PROTEZIONE'],
            'limite': CONFIG['LIMITE_KWH'],
            'prezzo_acquisto': CONFIG['PREZZO_ACQUISTO'],
            'prezzo_vendita': CONFIG['PREZZO_VENDITA'],
        }

    return jsonify({
        'config': configurazione,
        'status': stato,
        'energia': contatori_instance.riepilogo() if contatori_instance else {},
        'history': history,
        'logs': list(SYSTEM_STATE['LOGS'])
    })

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

    # Il limite kWh va anche inoltrato alla centralina
    if 'LIMITE_KWH' in puliti and wallbox_instance:
        wallbox_instance.set_limite_kwh(puliti['LIMITE_KWH'])

    return jsonify({'success': True, 'applicati': puliti})

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
        in_corso = {
            'minuti': round((adesso - contatori_instance.inizio_carica) / 60),
            'kwh': round(SYSTEM_STATE.get('WB_ENERGIA_SESSIONE') or 0.0, 2),
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
        # anti-spam del log di override manuale
        self.ultimo_log_manual_off = 0
        # l'auto e' fisicamente collegata? (da status/desc della centralina)
        self.auto_collegata = False

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
                SYSTEM_STATE['CENTRALINA_ONLINE'] = False
                self.sync_falliti += 1
                return None
            dati = risposta.json()
            SYSTEM_STATE['CENTRALINA_ONLINE'] = True
            if self.sync_falliti >= 3:
                log_msg("[CENTRALINA] Comunicazione ripristinata.")
                reset_dedup('centralina_offline')
                notifica("🔌 Centralina di nuovo raggiungibile.")
            self.sync_falliti = 0
            self._assorbi_stato(dati)
            return dati
        except requests.exceptions.RequestException as e:
            log_throttled('centralina_connessione', f"[CENTRALINA] Errore di connessione: {e}", 300)
            SYSTEM_STATE['CENTRALINA_ONLINE'] = False
            self.sync_falliti += 1
            return None
        except ValueError:
            log_throttled('centralina_json', "[CENTRALINA] La risposta non e' un JSON valido.", 300)
            SYSTEM_STATE['CENTRALINA_ONLINE'] = False
            self.sync_falliti += 1
            return None

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
            SYSTEM_STATE['WB_ENERGIA_SESSIONE'] = num('energy')
            SYSTEM_STATE['WB_TEMPO_SESSIONE'] = num('time')
            SYSTEM_STATE['WB_TEMP_PRESA'] = num('tplug')
            SYSTEM_STATE['WB_TEMP_SCHEDA'] = num('tboard')
            SYSTEM_STATE['WB_STATUS'] = str(dati.get('status', ''))
            SYSTEM_STATE['WB_DESC'] = str(dati.get('desc', ''))
            SYSTEM_STATE['WB_ALG'] = str(dati.get('alg', ''))
            chiave_limite = CONFIG.get('CHIAVE_LIMITE_JSON')
            if chiave_limite and chiave_limite in dati:
                SYSTEM_STATE['LIMITE_KWH_CENTRALINA'] = dati[chiave_limite]

        # L'auto e' collegata? status "0" = Non collegata.
        collegata = str(dati.get('status', '0')) != '0'
        if collegata != self.auto_collegata:
            self.auto_collegata = collegata
            desc = dati.get('desc', '?')
            log_msg(f"[CENTRALINA] Stato connessione: {desc}")
            notifica(f"🔌 Wallbox: {desc}", dedup_key='stato_connessione', min_intervallo=120)

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
        requested = int(max(min_p, min(max_p, int(watts))))

        now = time.time()

        if not bypass:#bypasso sia il filtro che la sogli a di protezione
            if abs(requested - self.current_set_power) < CONFIG['POTENZA_PROTEZIONE'] and self.is_on:
                log_msg(f"[INFO] Variazione potenza ({requested}W) inferiore alla soglia di protezione ({CONFIG['POTENZA_PROTEZIONE']}W). Nessun cambiamento.")
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

            log_msg(f"[AZIONE] CAMBIO POTENZA -> richiesta={requested}W limited={limited}W invio={send_value}W")
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
        with WALLBOX_LOCK:
            if self.is_on:
                return
            if self.time_turned_off > 0:
                tempo_trascorso = time.time() - self.time_turned_off
                if tempo_trascorso < CONFIG['COOLDOWN_ACCENSIONE']:
                    log_msg(f"[INFO] Attesa cooldown: {CONFIG['COOLDOWN_ACCENSIONE'] - tempo_trascorso:.1f}s prima di accendere")
                    return

            log_msg("[AZIONE] ACCENSIONE (ON)")
            min_p, _ = self.limiti_potenza()
            self.set_power(min_p, bypass=True)

            if self.send_command({'btn': 'i'}):
                self.is_on = True
                self.failed_off_attempts = 0  # Reset contatore quando si accende
                self.last_update_time = time.time()
                self.update_shared_state()
                if contatori_instance:
                    contatori_instance.inizio_sessione()

    def turn_off(self, force=False):
        with WALLBOX_LOCK:
            now = time.time()
            if force and self.last_update_time != 0 and (now - self.last_update_time < CONFIG['UPDATE_INTERVAL_S']):
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
                if era_acceso and contatori_instance:
                    contatori_instance.fine_sessione()
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

    def initialize(self):
        """Reset completo: rilegge la fase E forza lo spegnimento.

        Usata al boot, dal bottone web e da /reset. NON va chiamata
        periodicamente: per il controllo ciclico della fase esiste sync_fase(),
        che non spegne nulla.
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

            log_msg("1. Metto in OFF (Attesa dati)...")
            self.last_update_time = 0
            self.turn_off(force=True)

            min_p, _ = self.limiti_potenza()
            log_msg(f"2. Imposto potenza minima ({min_p}W)...")
            self.set_power(min_p, bypass=True)

            # NB: qui non si reinvia il limite kWh salvato alla centralina (il
            # comando resta disponibile da UI/Telegram via set_limite_kwh). Per
            # sicurezza pero' si forza SEMPRE lo spegnimento del limite se la
            # centralina ne ricorda uno ancora attivo da una sessione precedente:
            # cosi' un riavvio/reset non eredita mai silenziosamente un tetto di
            # carica dimenticato acceso.
            if CONFIG.get('CMD_LIMITE_TEMPLATE'):
                limite_letto = SYSTEM_STATE.get('LIMITE_KWH_CENTRALINA')
                if limite_letto not in (None, 0, '0'):
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
        self.inizio_carica = None
        self.wh_inizio_sessione = 0.0
        self.fv_inizio_sessione = 0.0
        self.energy_inizio = 0.0

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
        # dt <= 0: orologio all'indietro (sync NTP). dt > 60: buco nei pacchetti.
        # In entrambi i casi non si inventa energia.
        if dt <= 0 or dt > 60:
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
            self.wallbox_da_fv_wh += min(wallbox_w, disponibile_fv) * ore

    def _controlla_rollover(self):
        oggi = time.strftime('%Y-%m-%d')
        if oggi == self.giorno:
            return
        self.archivia_giorno()
        self.giorno = oggi
        self.azzera()
        log_msg(f"[ENERGIA] Nuovo giorno: {oggi}. Contatori azzerati.")

    def efficienza_carica(self):
        """% dei kWh della wallbox coperti dal fotovoltaico."""
        if self.wallbox_wh <= 0:
            return None
        return min(100.0, self.wallbox_da_fv_wh / self.wallbox_wh * 100.0)

    def inizio_sessione(self):
        self.inizio_carica = time.time()
        self.wh_inizio_sessione = self.wallbox_wh
        self.fv_inizio_sessione = self.wallbox_da_fv_wh
        # Contatore della centralina a inizio sessione: e' una misura reale,
        # a differenza dei nostri Wh integrati dal setpoint.
        self.energy_inizio = SYSTEM_STATE.get('WB_ENERGIA_SESSIONE') or 0.0

    def fine_sessione(self):
        """Chiude la sessione, la salva su disco e notifica."""
        if self.inizio_carica is None:
            return
        fine = time.time()
        durata = fine - self.inizio_carica
        kwh_stimati = (self.wallbox_wh - self.wh_inizio_sessione) / 1000.0
        kwh_fv = (self.wallbox_da_fv_wh - self.fv_inizio_sessione) / 1000.0
        inizio = self.inizio_carica
        self.inizio_carica = None

        if durata < 60:
            return   # sessioni lampo: non vale la pena registrarle

        # La centralina azzera 'energy' a fine sessione, quindi il valore letto
        # per ultimo e' quello buono; se manca si ripiega sulla stima.
        kwh_reali = SYSTEM_STATE.get('WB_ENERGIA_SESSIONE') or 0.0
        misurata = kwh_reali > 0
        kwh = kwh_reali if misurata else kwh_stimati
        quota_fv = min(100.0, kwh_fv / kwh_stimati * 100.0) if kwh_stimati > 0 else None

        sessione = {
            'inizio': inizio,
            'fine': fine,
            'minuti': round(durata / 60),
            'kwh': round(kwh, 2),
            'kwh_misurati': misurata,
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

        log_msg(f"[SESSIONE] Terminata: {durata/60:.0f} min, {kwh:.2f} kWh"
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
    _coda_storico.append([int(ts), int(rete), int(solare), int(wb)])

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
                    ts, rete, solare, wb = json.loads(riga)
                except (ValueError, TypeError):
                    continue   # riga troncata da un crash: si salta
                if ts < limite or ts < TIMESTAMP_MINIMO:
                    continue
                SYSTEM_STATE['ULTIME_LETTURE_FASI'].append(
                    (rete, solare, [0, 0, 0, 0, 0, 0], ts, wb))
                caricati += 1
    except OSError as e:
        log_msg(f"[STORICO] Lettura fallita: {e}")
        return
    log_msg(f"[STORICO] Ricaricati {caricati} campioni da disco.")

INTERVALLI = {'15m': 900, '1h': 3600, '6h': 21600, '24h': 86400}

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
    """Ultimi N giorni chiusi + la giornata in corso, con risparmio stimato."""
    giorni = leggi_json(FILE_STORICO_GIORNALIERO, [])[-quanti:]
    if contatori_instance:
        oggi = contatori_instance.riepilogo()
        # il giorno in corso non e' ancora nel file: lo aggiungo in coda
        giorni = [g for g in giorni if g.get('giorno') != oggi['giorno']] + [oggi]
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


class EnergyMonitor:
    def __init__(self):
        self.solar_now = 0.0
        self.total_grid_load = 0.0
        self.house_load = 0.0
        self.fases = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        self.ctrletturefasi = 0
        self.time = None

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

                    self.total_grid_load = l1 + l2 + l3
                    self.solar_now = l4 + l5 + l6
                    self.fases = [l1, l2, l3, l4, l5, l6]

                    self.ctrletturefasi += 1
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

                    registra_campione(self.total_grid_load, self.solar_now,
                                      wb_power, self.fases, self.time)
                    if contatori_instance:
                        contatori_instance.aggiorna(self.solar_now, self.total_grid_load,
                                                    wb_power, self.house_load, self.time)

                    return "TRIGGER"

            elif root.tag == 'solar':
                curr = root.find('current')
                if curr is not None:
                    nodo = curr.find('generating')
                    if nodo is None:
                        return None
                    self.solar_now = float(nodo.text)
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

def run_logic(monitor, wallbox):
    # if user has manually requested the wallbox to remain off, skip all automatic decisions
    if getattr(wallbox, 'manual_off', False):
        # Prima questo log usciva a OGNI pacchetto (ogni pochi secondi) e
        # riempiva journalctl. Ora: una volta ogni 10 minuti.
        log_throttled('manual_off',
                      "[INFO] Override manuale attivo, wallbox rimane spento fino a comando /accendi",
                      600)
        return

    with STATO_LOCK:
        POTENZA_PRELEVABILE = CONFIG['POTENZA_PRELEVABILE']

    potenza_generata = monitor.solar_now
    potenza_consumata = monitor.total_grid_load
    # Potenza in carica: si preferisce la misura reale della centralina (pcar).
    # Con il solo setpoint, se l'auto assorbe meno di quanto concesso il
    # regolatore crede di erogare piu' di quanto faccia e continua ad alzare.
    _misurata = wallbox.potenza_reale()
    if _misurata is not None and wallbox.is_on:
        potenza_carica = _misurata
    else:
        potenza_carica = wallbox.display_power if wallbox.is_on else 0
    potenza_casa = monitor.house_load
    potenza_generata += POTENZA_PRELEVABILE
    potenza_esportata = potenza_generata - potenza_consumata

    log_msg(f"[INFO] Gen: {potenza_generata:.0f}W  | Casa: {potenza_casa:.0f}W | Esp: {potenza_esportata:.0f}W | "
            f"WB: {'ON' if wallbox.is_on else 'OFF'} ({potenza_carica:.0f}W)")

    potenza_minima, potenza_massima = wallbox.limiti_potenza()

    # ------------------------------------------------------------------
    # notifica potenza massima solo se mantenuta per almeno 60s
    now = time.time()
    if wallbox.is_on:
        # verifica se siamo al massimo o sopra
        if potenza_carica >= potenza_massima:
            if wallbox.max_reached_start is None:
                wallbox.max_reached_start = now
            elif not wallbox.max_notified and now - wallbox.max_reached_start >= 60:
                if wallbox.fase == 1:
                    notifica(f"⚠️ Potenza massima raggiunta ({potenza_massima:.0f}W).",
                             dedup_key='max_potenza', min_intervallo=1800)
                else:
                    notifica(f"⚠️ Potenza massima raggiunta ({potenza_massima:.0f}W). Consiglio: mettere "
                             f"l'impianto in modalità trifase per sfruttare meglio la potenza disponibile.",
                             dedup_key='max_potenza', min_intervallo=1800)
                wallbox.max_notified = True
        else:
            # siamo scesi sotto, resettiamo contatori
            wallbox.max_reached_start = None
            wallbox.max_notified = False
    # ------------------------------------------------------------------

    if potenza_consumata == 0:
        return

    if not wallbox.is_on:
        if potenza_esportata > potenza_minima:
            log_msg(f"[DECISIONE] Export sufficiente. Accendo a {potenza_minima}W.")
            wallbox.turn_on()
        return

    if wallbox.is_on:
        now = time.time()
        if wallbox.pending_off_until > 0:
            if now < wallbox.pending_off_until:
                restante = wallbox.pending_off_until - now
                log_msg(f"[INFO] Timer minimo attivo: {restante:.0f}s restanti (attendo la scadenza)...")
                return
            else:
                wallbox.pending_off_until = 0
                if potenza_generata < potenza_minima or potenza_esportata < -200:#spengo se continuo ad importare piu di 200w
                    log_msg(f"[DECISIONE] Sole insufficiente. Spengo.")
                    consiglio = ("Consiglio: mettere l'impianto in modalità monofase per sfruttare "
                                 "meglio la potenza disponibile." if wallbox.fase == 1
                                 else "Consiglio: staccare la macchina.")
                    notifica(f"⚠️ Potenza insufficiente ({potenza_generata:.0f}W), consumo casa "
                             f"({potenza_casa:.0f}W). Spengo wallbox.\n{consiglio}",
                             dedup_key='sole_insufficiente', min_intervallo=900)
                    wallbox.turn_off(force=True)
                    return
                else:
                    log_msg(f"[DECISIONE] Generazione sufficiente. Continuo.")
                    wallbox.set_power(potenza_minima, bypass=True)
                    return

        if potenza_consumata > potenza_generata:
            nuova_potenza = potenza_generata - potenza_casa - 200#200W evito on/off
            log_msg(f"[DECISIONE]2 Diminuisco a {nuova_potenza:.0f}W")
            wallbox.set_power(nuova_potenza, bypass=False)
        if potenza_carica > (potenza_generata - potenza_casa) or potenza_esportata < 0:
            nuova_potenza = potenza_carica - abs(potenza_esportata)
            if nuova_potenza < potenza_minima or potenza_generata < potenza_minima:
                log_msg(f"[DECISIONE] Sole insufficiente. Minimo per {CONFIG['TIMER_SPEGNIMENTO']}s.")
                wallbox.set_power(potenza_minima, bypass=True)
                wallbox.pending_off_until = now + CONFIG['TIMER_SPEGNIMENTO']
            else:
                log_msg(f"[DECISIONE] Diminuisco a {nuova_potenza:.0f}W")
                wallbox.set_power(nuova_potenza, bypass=False)

        else: 
            nuova_potenza = potenza_carica + abs(potenza_generata-potenza_consumata)- 100
            if nuova_potenza > potenza_generata:
                return
            delta_potenza = nuova_potenza - potenza_carica
            if potenza_casa + delta_potenza >potenza_generata or nuova_potenza + potenza_casa > potenza_generata:
                return
            
            if nuova_potenza > potenza_massima:
                # limito alla potenza massima disponibile, la notifica viene gestita
                # dal blocco di controllo sopra per evitare messaggi ripetuti.
                nuova_potenza = potenza_massima
                wallbox.set_power(nuova_potenza, bypass=True)
                log_msg(f"[DECISIONE] Aumento a {nuova_potenza:.0f}W")
                return
            log_msg(f"[DECISIONE] Aumento a {nuova_potenza:.0f}W")
            wallbox.set_power(nuova_potenza, bypass=False)

# -----------------------------------------------------------
# THREAD PERIODICO (attivita' cicliche)
# -----------------------------------------------------------
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

        # --- Flush storico su disco ogni 60s (salva-SD) ---
        if adesso >= prossimo_flush:
            try:
                flush_storico()
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
        except Exception as e:
            log_msg(f"[ERRORE] riepilogo giornaliero: {e}")

# -----------------------------------------------------------
# MAIN
# -----------------------------------------------------------
def main():
    global wallbox_instance, contatori_instance

    # La configurazione va caricata PRIMA di costruire il controller, cosi'
    # initialize() lavora gia' con i valori salvati dall'utente.
    carica_config()
    carica_storico()

    monitor = EnergyMonitor()
    contatori_instance = ContatoriEnergia()
    wallbox_instance = WallboxController()
    wallbox = wallbox_instance

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

    wallbox.initialize()

    # 3. AVVIO THREAD PERIODICO (dopo initialize, per non sovrapporre un
    #    sync_fase alla inizializzazione di avvio)
    threading.Thread(target=thread_periodico, args=(wallbox,), daemon=True).start()
    log_msg(f">>> LETTURA CENTRALINA E CONTROLLO FASE ATTIVI (ogni {CONFIG['INTERVALLO_SYNC_FASE']}s) <<<")

    errori_consecutivi = 0
    while True:
        try:
            data, _ = sock.recvfrom(65535)
            evt = monitor.parse_packet(data)
            errori_consecutivi = 0

            if evt == "TRIGGER":
                run_logic(monitor, wallbox)

        except socket.timeout:
            log_throttled('nessun_pacchetto',
                          "[AVVISO] Nessun pacchetto multicast negli ultimi 30s.", 300)
        except KeyboardInterrupt:
            log_msg("Interruzione richiesta: salvo lo stato...")
            flush_storico()
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