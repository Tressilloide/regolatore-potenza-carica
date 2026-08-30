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
    'ECO_MODE': False,              # Modalita' Eco: carica solo con surplus reale (persistito)
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
    'INTERVALLO_SYNC_FASE': 30,     # Ogni quanti secondi rileggere 'tfase' dalla centralina

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

# Stato condiviso per la Web UI e Telegram
SYSTEM_STATE = {
    'ULTIMA_LETTURA_FASI': None,
    'ULTIMA_LETTURA_SOLARE': None,
    'ULTIME_LETTURE_FASI': deque(maxlen=10000),  # Buffer per i grafici (~28h a 10s)
    'MONITOR_FASI': [0,0,0,0,0,0],
    'WALLBOX_POWER': 0,
    'WALLBOX_STATUS': False,
    'IMPIANTO_FASE': 0, # 0=Mono, 1=Tri
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
    'ECO_MODE':            (None, None, bool),
    'LIMITE_KWH':          (1, 100, int),
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

    pulito = int(temp)
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
        "/grafici - Grafico real-time delle potenze\n"
        "/fase - Rileva subito monofase/trifase\n\n"
        "*Controllo*\n"
        "/accendi - Forza l'accensione della Wallbox\n"
        "/spegni - Forza lo spegnimento della Wallbox\n"
        "/reset - Re-inizializza la centralina\n\n"
        "*Impostazioni*\n"
        "/setPotenzaPrelevabile <W> - Potenza prelevabile dalla rete\n"
        "/setPotenzaProtezione <W> - Soglia di protezione\n"
        "/limite <kWh> - Energia da caricare (1-100)\n"
        "/eco on|off - Ricarica solo con surplus fotovoltaico\n"
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
        eco = CONFIG['ECO_MODE']
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
        f"🌱 *Eco:* {'ATTIVA' if eco else 'disattiva'}\n"
        f"🛠️ *Prelevabile:* {prelevabile} W{' (ignorata in Eco)' if eco else ''}\n"
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
        nota = "\n_(ignorata: modalità Eco attiva)_" if CONFIG['ECO_MODE'] else ""
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

async def cmd_eco(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Attiva/disattiva la modalita' Eco (ricarica solo con surplus reale)."""
    if not check_auth(update): return
    if not context.args:
        stato = "🟢 ATTIVA" if CONFIG['ECO_MODE'] else "⚪ disattiva"
        await update.message.reply_text(
            f"🌱 *Modalità Eco:* {stato}\nUsa: `/eco on` oppure `/eco off`", parse_mode='Markdown')
        return

    ok, errore = applica_impostazione('ECO_MODE', context.args[0], 'TELEGRAM')
    if not ok:
        await update.message.reply_text("⚠️ Usa: `/eco on` oppure `/eco off`", parse_mode='Markdown')
        return

    if CONFIG['ECO_MODE']:
        await update.message.reply_text(
            "🌱 *Modalità Eco ATTIVA*\nLa wallbox carica solo con surplus fotovoltaico reale.\n"
            f"_La potenza prelevabile ({CONFIG['POTENZA_PRELEVABILE']} W) viene ignorata._", parse_mode='Markdown')
    else:
        await update.message.reply_text(
            f"⚪ *Modalità Eco disattivata*\nTorna attiva la potenza prelevabile: {CONFIG['POTENZA_PRELEVABILE']} W",
            parse_mode='Markdown')

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
    
    history = SYSTEM_STATE['ULTIME_LETTURE_FASI']
    if not history or len(history) < 2:
        await update.message.reply_text("⏳ Non ci sono ancora abbastanza dati per generare il grafico. Riprova tra poco.")
        return

    await update.message.reply_text("📊 Generazione grafico in corso...")

    # Prepara i dati per matplotlib
    times = [time.strftime("%H:%M:%S", time.localtime(h[3])) for h in history]
    grid = [h[0] for h in history]
    solar = [h[1] for h in history]
    wb = [h[4] if len(h) > 4 else 0 for h in history]

    # API a oggetti invece di pyplot: la figura non entra nel registro globale
    # di pyplot, quindi non serve plt.close() e un'eccezione non lascia figure
    # orfane in RAM a ogni /grafici (era un memory leak).
    fig = Figure(figsize=(10, 5))
    ax = fig.subplots()
    ax.plot(times, grid, label='Consumo Rete (W)', color='#ff6384', linewidth=2)
    ax.fill_between(times, solar, color='#4bc0c0', alpha=0.2)
    ax.plot(times, solar, label='Produzione Solare (W)', color='#4bc0c0', linewidth=2)
    ax.fill_between(times, wb, color='#36a2eb', alpha=0.1)
    ax.plot(times, wb, label='Potenza Wallbox (W)', color='#36a2eb', linewidth=2)

    ax.set_title("Andamento Energetico Real-Time")
    ax.set_xlabel("Orario")
    ax.set_ylabel("Watt (W)")
    ax.legend(loc="upper left")
    ax.grid(True, linestyle='--', alpha=0.6)
    ax.xaxis.set_major_locator(MaxNLocator(8))
    ax.tick_params(axis='x', rotation=45)
    fig.tight_layout()

    buf = io.BytesIO()
    FigureCanvasAgg(fig).print_png(buf)
    buf.seek(0)

    await update.message.reply_photo(photo=buf)

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
        (["eco"], cmd_eco),
        (["limite"], cmd_limite),
        (["fase"], cmd_fase),
        (["energia"], cmd_energia),
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
    <title>Solar Monitor - by Eric and Gemini</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        body { font-family: 'Segoe UI', sans-serif; background: #f4f4f9; padding: 20px; color: #333; }
        .container { max-width: 1000px; margin: 0 auto; }
        .card { background: white; padding: 20px; border-radius: 10px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); margin-bottom: 20px; }
        h2 { margin-top: 0; color: #444; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 20px; }
        .stat { font-size: 1.2em; margin: 10px 0; }
        .stat span { font-weight: bold; color: #007bff; }
        .input-group { margin-bottom: 15px; }
        label { display: block; margin-bottom: 5px; font-weight: bold; }
        input[type="number"] { width: 100%; padding: 8px; border: 1px solid #ddd; border-radius: 4px; }
        button { background: #28a745; color: white; border: none; padding: 10px 20px; border-radius: 4px; cursor: pointer; width: 100%; font-size: 1em; }
        button:hover { background: #218838; }
        .btn-warning { background: #ffc107; color: #333; margin-top: 15px; }
        .btn-warning:hover { background: #e0a800; }
        .phase-box { display: flex; justify-content: space-between; border-bottom: 1px solid #eee; padding: 5px 0; }
        .tot-box { display: flex; justify-content: space-between; background-color: #e9ecef; padding: 8px 5px; margin-top: 10px; border-radius: 4px; font-weight: bold; }
        .status-on { color: green; font-weight: bold; }
        .status-off { color: red; font-weight: bold; }
        .time-ago { font-weight: normal !important; font-style: italic; color: #888 !important; font-size: 0.9em; margin-left: 5px; }
        
        /* Stile per la Console */
        .console-box {
            background: #1e1e1e;
            color: #00ff00;
            font-family: 'Courier New', Courier, monospace;
            height: 250px;
            overflow-y: scroll;
            padding: 15px;
            border-radius: 5px;
            font-size: 0.9em;
            line-height: 1.4;
            white-space: pre-wrap;   /* i log arrivano come testo, non HTML */
        }

        /* --- Elementi aggiunti --- */
        .nota { font-size: 0.82em; color: #777; margin-top: 6px; font-style: italic; }
        .toggle-row { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
        .toggle-row label { margin-bottom: 0; }
        .toggle-row input[type="checkbox"] { width: 20px; height: 20px; cursor: pointer; }
        input[type="range"] { width: 100%; cursor: pointer; }

        .esito { margin-top: 12px; padding: 10px; border-radius: 4px; font-size: 0.9em; white-space: pre-line; }
        .esito.ok { background: #d4edda; color: #155724; }
        .esito.ko { background: #f8d7da; color: #721c24; }

        .grid-energia { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; }
        .tile { background: #f8f9fa; border-radius: 8px; padding: 14px; text-align: center; }
        .tile-val { font-size: 1.5em; font-weight: bold; color: #007bff; }
        .tile-lab { font-size: 0.8em; color: #666; margin-top: 4px; }

        .eff-wrap { margin-top: 18px; }
        .eff-head { display: flex; justify-content: space-between; margin-bottom: 6px; font-size: 0.95em; }
        .eff-bar { background: #e9ecef; border-radius: 10px; height: 20px; overflow: hidden; }
        .eff-fill { background: linear-gradient(90deg, #ffc107, #28a745); height: 100%; width: 0%; transition: width .4s; }

        .range-bar { display: flex; gap: 8px; margin-bottom: 12px; }
        .range-btn { width: auto; padding: 6px 16px; background: #e9ecef; color: #555; font-size: 0.9em; }
        .range-btn:hover { background: #dde1e5; }
        .range-btn.attivo { background: #007bff; color: white; }
        .range-btn.attivo:hover { background: #0069d9; }

        .allarme { background: #fff3cd; color: #856404; padding: 8px; border-radius: 4px; margin: 8px 0; font-size: 0.9em; }
    </style>
</head>
<body>
    <div class="container">
        <h1>☀️ Solar Controller</h1>
        
        <div class="grid">
            <div class="card">
                <h2>⚙️ Impostazioni</h2>
                <!-- Nessun attributo value= hard-coded: i valori arrivano dal
                     server in fetchData(), altrimenti la pagina mostrerebbe
                     sempre i default anche dopo un salvataggio. -->
                <div class="input-group">
                    <label>Potenza Prelevabile (W)</label>
                    <input type="number" id="prelevabile" oninput="marcaSporco(event)">
                    <div class="nota" id="nota_eco" hidden>Ignorata: modalità Eco attiva</div>
                </div>
                <div class="input-group">
                    <label>Potenza Protezione (W)</label>
                    <input type="number" id="protezione" oninput="marcaSporco(event)">
                </div>
                <div class="input-group">
                    <label>🔋 Limite di carica: <span id="limite_val">--</span> kWh</label>
                    <input type="range" id="limite" min="1" max="100" step="1"
                           oninput="marcaSporco(event); document.getElementById('limite_val').innerText = this.value;">
                    <div class="nota" id="nota_limite" hidden>
                        Comando centralina non ancora configurato: il valore viene salvato ma non inviato.
                    </div>
                </div>
                <div class="input-group toggle-row">
                    <label for="eco">🌱 Modalità Eco (solo surplus)</label>
                    <input type="checkbox" id="eco" onchange="marcaSporco(event)">
                </div>
                <button onclick="updateSettings()">Salva Impostazioni</button>
                <button class="btn-warning" onclick="reinitWallbox()">🔄 Re-Inizializza Wallbox</button>
                <div id="esito" class="esito" hidden></div>
            </div>

            <div class="card">
                <h2>🔌 Stato Sistema</h2>
                <div class="stat">Wallbox: <span id="wb_status">--</span></div>
                <div class="stat">Potenza WB: <span id="wb_power">0</span> W</div>
                <div class="stat">Modalità: <span id="wb_mode">--</span></div>
                <div class="stat">Eco: <span id="eco_stato">--</span></div>
                <div id="allarmi"></div>
                <div class="stat" style="font-size: 0.9em; color: #666;">Ultimo Agg. Fasi: <span id="last_fasi">--</span> <span id="sec_fasi" class="time-ago"></span></div>
                <div class="stat" style="font-size: 0.9em; color: #666;">Ultimo Agg. Solare: <span id="last_solar">--</span> <span id="sec_solar" class="time-ago"></span></div>
            </div>
        </div>

        <div class="card">
            <h2>⚡ Energia di Oggi</h2>
            <div class="grid-energia">
                <div class="tile"><div class="tile-val" id="e_solare">--</div><div class="tile-lab">☀️ Prodotta (kWh)</div></div>
                <div class="tile"><div class="tile-val" id="e_import">--</div><div class="tile-lab">🔌 Importata (kWh)</div></div>
                <div class="tile"><div class="tile-val" id="e_export">--</div><div class="tile-lab">↗️ Esportata (kWh)</div></div>
                <div class="tile"><div class="tile-val" id="e_wb">--</div><div class="tile-lab">🚗 In auto (kWh)*</div></div>
                <div class="tile"><div class="tile-val" id="e_wb_fv">--</div><div class="tile-lab">🌱 Da fotovoltaico (kWh)</div></div>
                <div class="tile"><div class="tile-val" id="e_tempo">--</div><div class="tile-lab">⏱️ Carica (min)</div></div>
            </div>
            <div class="eff-wrap">
                <div class="eff-head">Efficienza di carica <strong id="e_eff">--</strong></div>
                <div class="eff-bar"><div class="eff-fill" id="e_eff_bar"></div></div>
            </div>
            <div class="nota">* La potenza della wallbox è il valore <em>comandato</em> alla centralina,
               non una misura: i kWh in auto e l'efficienza sono una stima.</div>
        </div>

        <div class="card">
            <h2>⚡ Dettaglio Fasi</h2>
            <div class="grid">
                <div>
                    <h3>Consumo Rete (Grid)</h3>
                    <div class="phase-box"><span>L1:</span> <span><span id="l1">0</span> W</span></div>
                    <div class="phase-box"><span>L2:</span> <span><span id="l2">0</span> W</span></div>
                    <div class="phase-box"><span>L3:</span> <span><span id="l3">0</span> W</span></div>
                    <div class="tot-box"><span>TOTALE RETE:</span> <span><span id="tot_grid">0</span> W</span></div>
                </div>
                <div>
                    <h3>Produzione (Solar)</h3>
                    <div class="phase-box"><span>L4:</span> <span><span id="l4">0</span> W</span></div>
                    <div class="phase-box"><span>L5:</span> <span><span id="l5">0</span> W</span></div>
                    <div class="phase-box"><span>L6:</span> <span><span id="l6">0</span> W</span></div>
                    <div class="tot-box"><span>TOTALE SOLARE:</span> <span><span id="tot_solar">0</span> W</span></div>
                </div>
            </div>
        </div>

        <div class="card">
            <h2>📈 Grafico Andamento</h2>
            <div class="range-bar">
                <button class="range-btn attivo" data-range="live" onclick="cambiaRange('live')">Live</button>
                <button class="range-btn" data-range="1h"  onclick="cambiaRange('1h')">1h</button>
                <button class="range-btn" data-range="6h"  onclick="cambiaRange('6h')">6h</button>
                <button class="range-btn" data-range="24h" onclick="cambiaRange('24h')">24h</button>
            </div>
            <canvas id="energyChart"></canvas>
        </div>

        <div class="card">
            <h2>🖥️ Console Live</h2>
            <div id="console" class="console-box"></div>
        </div>
    </div>

    <script>
        const ctx = document.getElementById('energyChart').getContext('2d');
        const chart = new Chart(ctx, {
            type: 'line',
            data: {
                labels: [],
                datasets: [{
                    label: 'Consumo Rete (W)',
                    borderColor: 'rgb(255, 99, 132)',
                    data: [],
                    fill: false,
                    tension: 0.1
                }, {
                    label: 'Produzione Solare (W)',
                    borderColor: 'rgb(75, 192, 192)',
                    data: [],
                    fill: true,
                    backgroundColor: 'rgba(75, 192, 192, 0.2)',
                    tension: 0.1
                }, {
                    label: 'Potenza Wallbox (W)',
                    borderColor: 'rgb(54, 162, 235)',
                    data: [],
                    fill: true,
                    backgroundColor: 'rgba(54, 162, 235, 0.1)',
                    tension: 0.1
                }]
            },
            options: {
                responsive: true,
                scales: { 
                    x: { display: false },
                    y: { beginAtZero: true }
                },
                animation: { duration: 0 }
            }
        });

        function formatTime(timestamp) {
            if (!timestamp) return "Mai";
            const date = new Date(timestamp * 1000);
            return date.toLocaleTimeString();
        }

        // --- Ciclo di vita dei campi ------------------------------------
        // Un campo non va sovrascritto dal polling se l'utente ci sta
        // scrivendo (activeElement) o se ha gia' scritto e non ha ancora
        // salvato (campiSporchi).
        const campiSporchi = new Set();
        function marcaSporco(e) { campiSporchi.add(e.target.id); }

        function aggiornaCampo(id, valore) {
            const el = document.getElementById(id);
            if (!el) return;
            if (document.activeElement === el) return;
            if (campiSporchi.has(id)) return;
            if (el.type === 'checkbox') el.checked = !!valore;
            else el.value = valore;
        }

        function mostraEsito(testo, ok) {
            const box = document.getElementById('esito');
            box.textContent = testo;
            box.className = 'esito ' + (ok ? 'ok' : 'ko');
            box.hidden = false;
            if (ok) setTimeout(() => { box.hidden = true; }, 4000);
        }

        async function fetchData() {
            try {
                const response = await fetch('/api/data');
                const data = await response.json();

                // I valori REALI del server finiscono in .value (non nel
                // placeholder, che resta invisibile se il campo ha un valore).
                aggiornaCampo('prelevabile', data.config.prelevabile);
                aggiornaCampo('protezione', data.config.protezione);
                aggiornaCampo('eco', data.config.eco);
                aggiornaCampo('limite', data.config.limite);
                document.getElementById('limite_val').innerText =
                    document.getElementById('limite').value || data.config.limite;

                document.getElementById('nota_eco').hidden = !data.config.eco;
                document.getElementById('nota_limite').hidden = !!data.status.limite_supportato;
                document.getElementById('eco_stato').innerText = data.config.eco ? '🟢 Attiva' : '⚪ Disattiva';

                // Allarmi di salute del sistema
                const allarmi = [];
                if (!data.status.centralina_online) allarmi.push('⚠️ Centralina non raggiungibile');
                if (!data.status.sensore_online) allarmi.push('📡 Nessun dato dal sensore');
                if (data.status.manual_off) allarmi.push('✋ Override manuale attivo (/accendi per riprendere)');
                const boxAll = document.getElementById('allarmi');
                boxAll.textContent = '';
                allarmi.forEach(a => {
                    const d = document.createElement('div');
                    d.className = 'allarme';
                    d.textContent = a;
                    boxAll.appendChild(d);
                });

                // Card energia
                const en = data.energia || {};
                const set = (id, v) => { document.getElementById(id).innerText = (v === undefined || v === null) ? '--' : v; };
                set('e_solare', en.solare_kwh);
                set('e_import', en.rete_importata_kwh);
                set('e_export', en.rete_esportata_kwh);
                set('e_wb', en.wallbox_kwh);
                set('e_wb_fv', en.wallbox_da_fv_kwh);
                set('e_tempo', en.minuti_carica);
                const eff = en.efficienza;
                document.getElementById('e_eff').innerText = (eff === undefined || eff === null) ? 'n/d' : eff + '%';
                document.getElementById('e_eff_bar').style.width = ((eff === undefined || eff === null) ? 0 : eff) + '%';

                const wbSpan = document.getElementById('wb_status');
                wbSpan.innerText = data.status.wb_on ? "ON" : "OFF";
                wbSpan.className = data.status.wb_on ? "status-on" : "status-off";
                
                document.getElementById('wb_power').innerText = data.status.wb_power;
                document.getElementById('wb_mode').innerText = data.status.fase_mode === 1 ? "Trifase" : "Monofase";
                
                const serverTime = data.status.server_time;
                const lastFasi = data.status.last_fasi;
                const lastSolar = data.status.last_solar;

                document.getElementById('last_fasi').innerText = formatTime(lastFasi);
                document.getElementById('sec_fasi').innerText = lastFasi ? `(${Math.max(0, Math.round(serverTime - lastFasi))}s fa)` : '';
                
                document.getElementById('last_solar').innerText = formatTime(lastSolar);
                document.getElementById('sec_solar').innerText = lastSolar ? `(${Math.max(0, Math.round(serverTime - lastSolar))}s fa)` : '';

                const f = data.status.fasi;
                for(let i=0; i<6; i++) {
                    document.getElementById('l'+(i+1)).innerText = Math.round(f[i]);
                }
                document.getElementById('tot_grid').innerText = Math.round(data.status.grid_total);
                document.getElementById('tot_solar').innerText = Math.round(data.status.solar_total);

                if (rangeAttivo === 'live') disegnaGrafico(data.history);

                const consoleDiv = document.getElementById('console');
                const isScrolledToBottom = consoleDiv.scrollHeight - consoleDiv.clientHeight <= consoleDiv.scrollTop + 5;

                // textContent, non innerHTML: i log non vengono interpretati
                // come markup (il CSS white-space: pre-wrap rende gli a capo).
                consoleDiv.textContent = data.logs.join('\\n');

                if (isScrolledToBottom) {
                    consoleDiv.scrollTop = consoleDiv.scrollHeight;
                }

            } catch (e) { console.error("Errore fetch:", e); }
        }

        // --- Grafico con selettore di intervallo -------------------------
        let rangeAttivo = 'live';

        function disegnaGrafico(punti) {
            chart.data.labels = punti.map(h => formatTime(h.time));
            chart.data.datasets[0].data = punti.map(h => h.grid);
            chart.data.datasets[1].data = punti.map(h => h.solar);
            chart.data.datasets[2].data = punti.map(h => h.wb);
            chart.update();
        }

        async function cambiaRange(r) {
            rangeAttivo = r;
            document.querySelectorAll('.range-btn').forEach(b =>
                b.classList.toggle('attivo', b.dataset.range === r));
            if (r === 'live') { fetchData(); return; }
            try {
                const resp = await fetch('/api/storico?range=' + r);
                const dati = await resp.json();
                if (dati.success) disegnaGrafico(dati.punti);
            } catch (e) { console.error("Errore storico:", e); }
        }

        async function updateSettings() {
            // Validazione lato client: cosi' NaN/null non arrivano nemmeno al
            // server (che comunque li rifiuta con 400 - difesa in profondita').
            const payload = {};
            const errori = [];
            const num = (id, etichetta) => {
                const raw = document.getElementById(id).value.trim();
                if (raw === '') { errori.push(etichetta + ': campo vuoto'); return; }
                const v = Number(raw);
                if (!Number.isFinite(v)) { errori.push(etichetta + ': valore non numerico'); return; }
                payload[id] = Math.round(v);
            };
            num('prelevabile', 'Potenza prelevabile');
            num('protezione', 'Potenza protezione');
            payload['limite'] = Number(document.getElementById('limite').value);
            payload['eco'] = document.getElementById('eco').checked;

            if (errori.length) { mostraEsito('Correggi:\\n' + errori.join('\\n'), false); return; }

            try {
                const resp = await fetch('/api/settings', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                const res = await resp.json().catch(() => ({}));
                // Prima si mostrava "salvato!" anche su HTTP 500.
                if (!resp.ok || !res.success) {
                    mostraEsito('Errore nel salvataggio:\\n' + (res.errori || ['errore sconosciuto']).join('\\n'), false);
                    return;
                }
                campiSporchi.clear();
                mostraEsito('Impostazioni salvate.', true);
                fetchData();
            } catch (e) {
                mostraEsito('Errore di rete: ' + e, false);
            }
        }

        async function reinitWallbox() {
            if (!confirm("Sei sicuro di voler forzare la re-inizializzazione della Wallbox?")) return;
            try {
                const response = await fetch('/api/init_wallbox', { method: 'POST' });
                const result = await response.json();
                if (response.ok && result.success) {
                    mostraEsito("Re-inizializzazione avviata. Segui l'esito nella console.", true);
                    fetchData();
                } else {
                    mostraEsito('Errore: ' + (result.error || 'invio comando fallito'), false);
                }
            } catch (e) { mostraEsito('Errore di rete: ' + e, false); }
        }

        fetchData();
        setInterval(fetchData, 2000);
        // Lo storico lungo si aggiorna con calma: non serve ogni 2s
        setInterval(() => { if (rangeAttivo !== 'live') cambiaRange(rangeAttivo); }, 60000);
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
    'eco':         'ECO_MODE',
    'limite':      'LIMITE_KWH',
}

@app.route('/api/data')
def get_data():
    # /api/data e' chiamata ogni 2s: restituisce solo la finestra recente.
    # Lo storico lungo ha il suo endpoint (/api/storico) e la sua cadenza,
    # altrimenti si trasferirebbero megabyte 30 volte al minuto.
    limite_t = time.time() - 3600
    history = [
        {'grid': i[0], 'solar': i[1], 'time': i[3], 'wb': i[4] if len(i) > 4 else 0}
        for i in list(SYSTEM_STATE['ULTIME_LETTURE_FASI']) if i[3] >= limite_t
    ]

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
        }
        configurazione = {
            'prelevabile': CONFIG['POTENZA_PRELEVABILE'],
            'protezione': CONFIG['POTENZA_PROTEZIONE'],
            'eco': CONFIG['ECO_MODE'],
            'limite': CONFIG['LIMITE_KWH'],
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
    """Storico esteso, sottocampionato lato server (max ~300 punti)."""
    intervalli = {'1h': 3600, '6h': 21600, '24h': 86400}
    scelta = request.args.get('range', '1h')
    if scelta not in intervalli:
        return jsonify({'success': False, 'error': f"range non valido: {scelta}"}), 400

    da = time.time() - intervalli[scelta]
    punti = [i for i in list(SYSTEM_STATE['ULTIME_LETTURE_FASI']) if i[3] >= da]

    # Sottocampionamento a bucket: media per bucket, non "uno ogni N"
    massimo = 300
    if len(punti) > massimo:
        passo = len(punti) / massimo
        aggregati = []
        for n in range(massimo):
            blocco = punti[int(n * passo):max(int((n + 1) * passo), int(n * passo) + 1)]
            if not blocco:
                continue
            aggregati.append({
                'grid': sum(b[0] for b in blocco) / len(blocco),
                'solar': sum(b[1] for b in blocco) / len(blocco),
                'wb': sum((b[4] if len(b) > 4 else 0) for b in blocco) / len(blocco),
                'time': blocco[len(blocco) // 2][3],
            })
        punti_out = aggregati
    else:
        punti_out = [{'grid': i[0], 'solar': i[1], 'wb': i[4] if len(i) > 4 else 0, 'time': i[3]}
                     for i in punti]

    return jsonify({'success': True, 'range': scelta, 'punti': punti_out})

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
            chiave_limite = CONFIG.get('CHIAVE_LIMITE_JSON')
            if chiave_limite and chiave_limite in dati:
                SYSTEM_STATE['LIMITE_KWH_CENTRALINA'] = dati[chiave_limite]
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
        """Imposta l'energia da caricare (1-100 kWh) sulla centralina.

        Finche' CONFIG['CMD_LIMITE_TEMPLATE'] e' None NON invia nulla: il valore
        viene solo salvato. Questo permette di deployare la funzione prima di
        aver confermato il comando esatto, senza mandare btn a caso all'hardware.
        Ritorna (inviato, messaggio).
        """
        template = CONFIG.get('CMD_LIMITE_TEMPLATE')
        kwh = max(1, min(100, int(kwh)))

        if not template:
            msg = "Valore salvato, ma il comando della centralina non è ancora configurato (vedi CMD_LIMITE_TEMPLATE)."
            log_msg(f"[LIMITE] {msg}")
            return False, msg

        with WALLBOX_LOCK:
            ok = self.send_command({'btn': template.format(valore=kwh)})

        if not ok:
            return False, "Comando non accettato dalla centralina."

        log_msg(f"[LIMITE] Impostato a {kwh} kWh")

        # Verifica: rileggo e confronto. E' il modo piu' rapido per capire se
        # l'ipotesi sul formato del comando e' corretta.
        chiave = CONFIG.get('CHIAVE_LIMITE_JSON')
        if chiave:
            dati = self.leggi_stato_centralina(timeout=4)
            if dati is not None and str(dati.get(chiave)) != str(kwh):
                return False, (f"Comando inviato ma la centralina riporta "
                               f"{chiave}={dati.get(chiave)}: verificare CMD_LIMITE_TEMPLATE.")
        return True, f"Limite impostato a {kwh} kWh."

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

            # NB: qui non si reinvia il limite kWh salvato alla centralina.
            # Il comando resta disponibile da UI/Telegram (set_limite_kwh), ma
            # l'init non deve forzare nulla sull'hardware: il valore reale e'
            # quello che la centralina ricorda per conto suo.

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
            disponibile_fv = max(0.0, solare_w - casa_w)
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

    def fine_sessione(self):
        if self.inizio_carica is None:
            return
        durata = time.time() - self.inizio_carica
        kwh = (self.wallbox_wh - self.wh_inizio_sessione) / 1000.0
        self.inizio_carica = None
        if durata < 60:
            return   # sessioni lampo: non vale la pena notificarle
        notifica(f"🔋 Sessione di carica terminata\n"
                 f"Durata: {durata/60:.0f} min\n"
                 f"Energia: {kwh:.2f} kWh (stimata)")

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

                    wb_status = SYSTEM_STATE.get('WALLBOX_STATUS', False)
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

    # Modalita' Eco: si ignora la potenza prelevabile dalla rete, senza
    # sovrascriverla in CONFIG. Disattivando Eco l'utente ritrova il suo valore.
    with STATO_LOCK:
        eco = CONFIG['ECO_MODE']
        POTENZA_PRELEVABILE = 0 if eco else CONFIG['POTENZA_PRELEVABILE']

    potenza_generata = monitor.solar_now
    potenza_consumata = monitor.total_grid_load
    potenza_carica = wallbox.display_power if wallbox.is_on else 0
    potenza_casa = monitor.house_load
    potenza_generata += POTENZA_PRELEVABILE
    potenza_esportata = potenza_generata - potenza_consumata

    log_msg(f"[INFO] Gen: {potenza_generata:.0f}W  | Casa: {potenza_casa:.0f}W | Esp: {potenza_esportata:.0f}W | "
            f"WB: {'ON' if wallbox.is_on else 'OFF'} ({potenza_carica:.0f}W){' | ECO' if eco else ''}")

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
    log_msg(f">>> CONTROLLO FASE AUTOMATICO ATTIVO (ogni {CONFIG['INTERVALLO_SYNC_FASE']}s) <<<")

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