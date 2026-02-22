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
import io
import matplotlib

from solaar_eric import invia_notifica
matplotlib.use('Agg') # Backend non interattivo per thread-safety
import matplotlib.pyplot as plt

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
    'POTENZA_PROTEZIONE': 300,      # Modificabile da Web e Telegram
    'POTENZA_PRELEVABILE': 0,       # Modificabile da Web e Telegram
    'COOLDOWN_ACCENSIONE': 60,
    'UPDATE_INTERVAL_S': 5,
    'TIMER_SPEGNIMENTO': 60,
    'MCAST_GRP': '224.192.32.19',
    'MCAST_PORT': 22600,
    'IFACE': '192.168.1.193',
    'WALLBOX_IP': '192.168.1.22',
    'SMOOTHING_ALPHA': 0.5, 
    'MAX_DELTA_PER_SEC': 1500
}

WALLBOX_URL = f"http://{CONFIG['WALLBOX_IP']}/index.json"

# Stato condiviso per la Web UI e Telegram
SYSTEM_STATE = {
    'ULTIMA_LETTURA_FASI': None,
    'ULTIMA_LETTURA_SOLARE': None,
    'ULTIME_LETTURE_FASI': [],   # Buffer per il grafico
    'ULTIME_LETTURE_SOLARE': [], # Buffer per il grafico
    'MONITOR_FASI': [0,0,0,0,0,0],
    'WALLBOX_POWER': 0,
    'WALLBOX_STATUS': False,
    'IMPIANTO_FASE': 0, # 0=Mono, 1=Tri
    'LOGS': [] # Buffer per la console Web
}

# Variabile globale per accedere al controller dalla UI Web e da Telegram
wallbox_instance = None 

load_dotenv()
API_KEY = os.getenv('API_KEY')
CHAT_ID = os.getenv('CHAT_ID')
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(message)s', datefmt='%H:%M:%S')

def log_msg(msg):
    """Salva il log sia su terminale che nel buffer per la Web UI"""
    t_str = time.strftime("%H:%M:%S")
    full_msg = f"[{t_str}] {msg}"
    print(full_msg)
    SYSTEM_STATE['LOGS'].append(full_msg)
    # Tieni solo gli ultimi 50 messaggi in memoria per non appesantire
    if len(SYSTEM_STATE['LOGS']) > 50:
        SYSTEM_STATE['LOGS'].pop(0)

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
        "/info - Mostra lo stato attuale del sistema\n"
        "/accendi - Forza l'accensione della Wallbox\n"
        "/spegni - Forza lo spegnimento della Wallbox\n"
        "/setPotenzaPrelevabile <W> - Imposta potenza prelevabile dalla rete\n"
        "/setPotenzaProtezione <W> - Imposta la soglia di protezione\n"
        "/grafici - Invia il grafico real-time delle potenze\n"
    )
    await update.message.reply_text(msg, parse_mode='Markdown')

async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update): return
    fasi = SYSTEM_STATE['MONITOR_FASI']
    tot_grid = sum(fasi[0:3])
    tot_solar = sum(fasi[3:6])
    wb_status = "🟢 ON" if SYSTEM_STATE['WALLBOX_STATUS'] else "🔴 OFF"
    wb_power = SYSTEM_STATE['WALLBOX_POWER'] if SYSTEM_STATE['WALLBOX_STATUS'] else 0
    modalita = "Trifase" if SYSTEM_STATE['IMPIANTO_FASE'] == 1 else "Monofase"
    
    msg = (
        "📊 *Stato Sistema*\n\n"
        f"☀️ *Solare:* {tot_solar:.0f} W\n"
        f"🔌 *Rete:* {tot_grid:.0f} W\n"
        f"🚗 *Wallbox:* {wb_status} ({wb_power:.0f} W)\n"
        f"⚙️ *Modalità:* {modalita}\n"
        f"🛠️ *Prelevabile:* {CONFIG['POTENZA_PRELEVABILE']} W\n"
        f"🛡️ *Protezione:* {CONFIG['POTENZA_PROTEZIONE']} W\n"
    )
    await update.message.reply_text(msg, parse_mode='Markdown')

async def cmd_accendi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update): return
    if wallbox_instance:
        wallbox_instance.turn_on()
        await update.message.reply_text("✅ *Comando inviato:* Accensione Wallbox", parse_mode='Markdown')

async def cmd_spegni(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update): return
    if wallbox_instance:
        wallbox_instance.turn_off(force=True)
        await update.message.reply_text("🛑 *Comando inviato:* Spegnimento Wallbox", parse_mode='Markdown')

async def cmd_set_prelevabile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update): return
    try:
        valore = int(context.args[0])
        CONFIG['POTENZA_PRELEVABILE'] = valore
        log_msg(f"[TELEGRAM] Potenza Prelevabile impostata a {valore}W")
        await update.message.reply_text(f"✅ *Potenza Prelevabile* impostata a {valore} W", parse_mode='Markdown')
    except (IndexError, ValueError):
        await update.message.reply_text("⚠️ Usa il formato: `/setPotenzaPrelevabile 1000`", parse_mode='Markdown')

async def cmd_set_protezione(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update): return
    try:
        valore = int(context.args[0])
        CONFIG['POTENZA_PROTEZIONE'] = valore
        log_msg(f"[TELEGRAM] Potenza Protezione impostata a {valore}W")
        await update.message.reply_text(f"✅ *Potenza Protezione* impostata a {valore} W", parse_mode='Markdown')
    except (IndexError, ValueError):
        await update.message.reply_text("⚠️ Usa il formato: `/setPotenzaProtezione 300`", parse_mode='Markdown')

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

    # Crea il grafico
    plt.figure(figsize=(10, 5))
    plt.plot(times, grid, label='Consumo Rete (W)', color='#ff6384', linewidth=2)
    plt.fill_between(times, solar, color='#4bc0c0', alpha=0.2)
    plt.plot(times, solar, label='Produzione Solare (W)', color='#4bc0c0', linewidth=2)
    plt.fill_between(times, wb, color='#36a2eb', alpha=0.1)
    plt.plot(times, wb, label='Potenza Wallbox (W)', color='#36a2eb', linewidth=2)
    
    plt.title("Andamento Energetico Real-Time")
    plt.xlabel("Orario")
    plt.ylabel("Watt (W)")
    plt.legend(loc="upper left")
    plt.grid(True, linestyle='--', alpha=0.6)
    
    # Mostra solo alcuni tick sull'asse X per evitare sovrapposizioni
    plt.xticks(rotation=45)
    plt.gca().xaxis.set_major_locator(plt.MaxNLocator(8)) 
    plt.tight_layout()

    # Salva in memoria (BytesIO)
    buf = io.BytesIO()
    plt.savefig(buf, format='png')
    buf.seek(0)
    plt.close()

    # Invia l'immagine
    await update.message.reply_photo(photo=buf)

def run_telegram_polling():
    """Inizializza e avvia il polling di Telegram in un thread separato"""
    if not API_KEY:
        log_msg("[TELEGRAM] API_KEY mancante. Bot disabilitato.")
        return
    
    app = Application.builder().token(API_KEY).build()
    
    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("info", cmd_info))
    app.add_handler(CommandHandler("accendi", cmd_accendi))
    app.add_handler(CommandHandler("spegni", cmd_spegni))
    app.add_handler(CommandHandler("setPotenzaPrelevabile", cmd_set_prelevabile))
    app.add_handler(CommandHandler("setPotenzaProtezione", cmd_set_protezione))
    app.add_handler(CommandHandler("grafici", cmd_grafici))
    
    log_msg(">>> BOT TELEGRAM ATTIVO. In attesa di comandi... <<<")
    # stop_signals=None evita conflitti di segnali con il thread principale
    app.run_polling(stop_signals=None)

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
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>☀️ Solar Controller</h1>
        
        <div class="grid">
            <div class="card">
                <h2>⚙️ Impostazioni</h2>
                <div class="input-group">
                    <label>Potenza Prelevabile (W)</label>
                    <input type="number" id="prelevabile" value="0">
                </div>
                <div class="input-group">
                    <label>Potenza Protezione (W)</label>
                    <input type="number" id="protezione" value="300">
                </div>
                <button onclick="updateSettings()">Salva Impostazioni</button>
                <button class="btn-warning" onclick="reinitWallbox()">🔄 Re-Inizializza Wallbox</button>
            </div>

            <div class="card">
                <h2>🔌 Stato Sistema</h2>
                <div class="stat">Wallbox: <span id="wb_status">--</span></div>
                <div class="stat">Potenza WB: <span id="wb_power">0</span> W</div>
                <div class="stat">Modalità: <span id="wb_mode">--</span></div>
                <div class="stat" style="font-size: 0.9em; color: #666;">Ultimo Agg. Fasi: <span id="last_fasi">--</span> <span id="sec_fasi" class="time-ago"></span></div>
                <div class="stat" style="font-size: 0.9em; color: #666;">Ultimo Agg. Solare: <span id="last_solar">--</span> <span id="sec_solar" class="time-ago"></span></div>
            </div>
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
            <h2>📈 Grafico Real-time</h2>
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

        async function fetchData() {
            try {
                const response = await fetch('/api/data');
                const data = await response.json();

                if (document.activeElement.id !== 'prelevabile') 
                    document.getElementById('prelevabile').placeholder = data.config.prelevabile;
                if (document.activeElement.id !== 'protezione') 
                    document.getElementById('protezione').placeholder = data.config.protezione;

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

                const history = data.history;
                chart.data.labels = history.map(h => formatTime(h.time));
                chart.data.datasets[0].data = history.map(h => h.grid);
                chart.data.datasets[1].data = history.map(h => h.solar);
                chart.data.datasets[2].data = history.map(h => h.wb);
                chart.update();

                const consoleDiv = document.getElementById('console');
                const isScrolledToBottom = consoleDiv.scrollHeight - consoleDiv.clientHeight <= consoleDiv.scrollTop + 5;
                
                consoleDiv.innerHTML = data.logs.join('<br>');
                
                if (isScrolledToBottom) {
                    consoleDiv.scrollTop = consoleDiv.scrollHeight;
                }

            } catch (e) { console.error("Errore fetch:", e); }
        }

        async function updateSettings() {
            const prelevabile = document.getElementById('prelevabile').value;
            const protezione = document.getElementById('protezione').value;
            
            await fetch('/api/settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ 
                    prelevabile: parseInt(prelevabile), 
                    protezione: parseInt(protezione) 
                })
            });
            alert("Impostazioni salvate!");
            fetchData();
        }

        async function reinitWallbox() {
            if (!confirm("Sei sicuro di voler forzare la re-inizializzazione della Wallbox?")) return;
            try {
                const response = await fetch('/api/init_wallbox', { method: 'POST' });
                const result = await response.json();
                if (result.success) {
                    alert("Comando inviato! Controlla la console per l'esito.");
                    fetchData();
                } else {
                    alert("Errore nell'invio del comando.");
                }
            } catch (e) { console.error("Errore:", e); }
        }

        fetchData();
        setInterval(fetchData, 2000); 
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/data')
def get_data():
    history = []
    for item in SYSTEM_STATE['ULTIME_LETTURE_FASI']:
        history.append({
            'grid': item[0],
            'solar': item[1],
            'time': item[3],
            'wb': item[4] if len(item) > 4 else 0 
        })
    
    fasi = SYSTEM_STATE['MONITOR_FASI']
    tot_grid = sum(fasi[0:3])
    tot_solar = sum(fasi[3:6])

    return jsonify({
        'config': {
            'prelevabile': CONFIG['POTENZA_PRELEVABILE'],
            'protezione': CONFIG['POTENZA_PROTEZIONE']
        },
        'status': {
            'server_time': time.time(),
            'wb_on': SYSTEM_STATE['WALLBOX_STATUS'],
            'wb_power': SYSTEM_STATE['WALLBOX_POWER'] if SYSTEM_STATE['WALLBOX_STATUS'] else 0,
            'fase_mode': SYSTEM_STATE['IMPIANTO_FASE'],
            'last_fasi': SYSTEM_STATE['ULTIMA_LETTURA_FASI'],
            'last_solar': SYSTEM_STATE['ULTIMA_LETTURA_SOLARE'],
            'fasi': fasi,
            'grid_total': tot_grid,
            'solar_total': tot_solar
        },
        'history': history,
        'logs': SYSTEM_STATE['LOGS']
    })

@app.route('/api/settings', methods=['POST'])
def update_settings():
    data = request.json
    if 'prelevabile' in data:
        CONFIG['POTENZA_PRELEVABILE'] = int(data['prelevabile'])
    if 'protezione' in data:
        CONFIG['POTENZA_PROTEZIONE'] = int(data['protezione'])
    log_msg(f"[WEB] Parametri aggiornati: Prelevabile={CONFIG['POTENZA_PRELEVABILE']}, Protezione={CONFIG['POTENZA_PROTEZIONE']}")
    return jsonify({'success': True})

@app.route('/api/init_wallbox', methods=['POST'])
def force_init_wallbox():
    global wallbox_instance
    if wallbox_instance:
        log_msg("[WEB] Richiesta manuale di re-inizializzazione Wallbox!")
        wallbox_instance.initialize()
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': 'Controller non disponibile'})

def run_flask():
    app.run(host='0.0.0.0', port=80, debug=False, use_reloader=False)

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

    def update_shared_state(self):
        SYSTEM_STATE['WALLBOX_POWER'] = int(round(self.display_power))
        SYSTEM_STATE['WALLBOX_STATUS'] = self.is_on
        SYSTEM_STATE['IMPIANTO_FASE'] = self.fase

    def send_command(self, params):
        try:
            response = requests.get(WALLBOX_URL, params=params, timeout=3)
            return response.status_code == 200
        except Exception:
            return False

    def set_power(self, watts, bypass):
        if self.fase == 0:
            min_p = CONFIG['MONOFASE_MIN_POWER']
            max_p = CONFIG['MONOFASE_MAX_POWER']
        else:
            min_p = CONFIG['TRIFASE_MIN_POWER']
            max_p = CONFIG['TRIFASE_MAX_POWER']
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

        if self.send_command({'btn': f'P{send_value}'}):
            self.current_set_power = send_value
            self.last_update_time = now
            self.last_power_cmd_time = now
            self.display_power = smoothed
            self.update_shared_state()
            try:
                now_t = time.time()
                fasi = SYSTEM_STATE.get('MONITOR_FASI', [0,0,0,0,0,0])
                grid_total = sum(fasi[0:3])
                solar_total = sum(fasi[3:6])
                SYSTEM_STATE['ULTIME_LETTURE_FASI'].append((grid_total, solar_total, fasi.copy(), now_t, int(round(self.display_power))))
                if len(SYSTEM_STATE['ULTIME_LETTURE_FASI']) > 30:
                    SYSTEM_STATE['ULTIME_LETTURE_FASI'].pop(0)
            except Exception:
                pass
        
    def turn_on(self):
        if not self.is_on:
            if self.time_turned_off > 0:
                tempo_trascorso = time.time() - self.time_turned_off
                if tempo_trascorso < CONFIG['COOLDOWN_ACCENSIONE']:
                    log_msg(f"[INFO] Attesa cooldown: {CONFIG['COOLDOWN_ACCENSIONE'] - tempo_trascorso:.1f}s prima di accendere")
                    return
            
            log_msg("[AZIONE] ACCENSIONE (ON)")
            self.set_power(CONFIG['MONOFASE_MIN_POWER'] if self.fase == 0 else CONFIG['TRIFASE_MIN_POWER'], bypass=True) 

            if self.send_command({'btn': 'i'}):
                self.is_on = True
                self.last_update_time = time.time()
                self.update_shared_state()
            
    def turn_off(self, force=False):
        now = time.time()
        if force and self.last_update_time != 0 and (now - self.last_update_time < CONFIG['UPDATE_INTERVAL_S']):
            return

        if self.is_on or force:
            log_msg("[AZIONE] SPEGNIMENTO (OFF)")
            if self.send_command({'btn': 'o'}):
                self.is_on = False
                self.time_turned_off = time.time() 
                self.last_update_time = time.time()
                time.sleep(0.5)
                min_p = CONFIG['MONOFASE_MIN_POWER'] if self.fase == 0 else CONFIG['TRIFASE_MIN_POWER']
                try:
                    self.set_power(min_p, bypass=True)
                except Exception:
                    self.current_set_power = min_p
                    self.display_power = float(self.current_set_power)
                    self.update_shared_state()

    def initialize(self):
        log_msg("=== INIZIALIZZAZIONE SISTEMA ===")
        try:
            log_msg(f"Richiesta dati a {WALLBOX_URL}...")
            response = requests.get(WALLBOX_URL, timeout=5)

            if response.status_code == 200:
                dati = response.json()
                valore_fase = dati.get("tfase")

                if valore_fase == "1":
                    modalita = "TRIFASE"
                    self.fase = 1
                else:
                    modalita = "MONOFASE"
                    self.fase = 0
                
                log_msg(f"TIPO IMPIANTO: {modalita}")
                self.update_shared_state()
            else:
                log_msg(f"Errore. centralina codice: {response.status_code}")

        except requests.exceptions.RequestException as e:
            log_msg(f"Errore di connessione: {e}")
        except json.JSONDecodeError:
            log_msg("Errore: La risposta del server non è un JSON valido.")

        log_msg("1. Metto in OFF (Attesa dati)...")
        self.last_update_time = 0 
        self.turn_off(force=True)
        
        if self.fase == 0:
            log_msg("1. Imposto potenza minima (1380W)...")
            self.set_power(CONFIG['MONOFASE_MIN_POWER'], bypass=True)
        elif self.fase == 1:
            log_msg("1. Imposto potenza minima (4140)...")
            self.set_power(CONFIG['TRIFASE_MIN_POWER'], bypass=True)

        time.sleep(1)
        log_msg("=== PRONTO. IN ATTESA PACCHETTI ===")

class EnergyMonitor:
    def __init__(self):
        self.solar_now = 0.0        
        self.total_grid_load = 0.0  
        self.fases = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        self.ctrletturefasi = 0
        self.time = None

    def parse_packet(self, data):
        try:
            xml_str = data.decode('utf-8', errors='ignore')
            root = ET.fromstring(xml_str)
            
            if root.tag == 'electricity': 
                channels = root.find('channels')
                if channels:
                    p = {}
                    for c in channels.findall('chan'):
                        try:
                            val = float(c.find('curr').text)
                        except:
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
                    self.time = SYSTEM_STATE['ULTIMA_LETTURA_FASI']
                    
                    wb_status = SYSTEM_STATE.get('WALLBOX_STATUS', False)
                    wb_power = SYSTEM_STATE.get('WALLBOX_POWER', 0) if wb_status else 0
                    
                    SYSTEM_STATE['ULTIME_LETTURE_FASI'].append((self.total_grid_load, self.solar_now, self.fases, self.time, wb_power))
                    if len(SYSTEM_STATE['ULTIME_LETTURE_FASI']) > 30:
                        SYSTEM_STATE['ULTIME_LETTURE_FASI'].pop(0)    
            
                    return "TRIGGER"

            elif root.tag == 'solar': 
                curr = root.find('current')
                if curr is not None:
                    gen = float(curr.find('generating').text)
                    self.solar_now = gen
                    SYSTEM_STATE['ULTIMA_LETTURA_SOLARE'] = time.time()
                    self.time = SYSTEM_STATE['ULTIMA_LETTURA_SOLARE']
                    
                    SYSTEM_STATE['ULTIME_LETTURE_SOLARE'].append((gen, self.time))
                    if len(SYSTEM_STATE['ULTIME_LETTURE_SOLARE']) > 30:
                        SYSTEM_STATE['ULTIME_LETTURE_SOLARE'].pop(0)
                    return "TRIGGER"
                
        except Exception:
            pass
        return None

def run_logic(monitor, wallbox):
    POTENZA_PRELEVABILE = CONFIG['POTENZA_PRELEVABILE']
    
    potenza_generata = monitor.solar_now
    potenza_consumata = monitor.total_grid_load
    potenza_carica = wallbox.current_set_power if wallbox.is_on else 0
    potenza_live = abs(potenza_consumata - potenza_carica) + potenza_carica if wallbox.is_on else potenza_consumata
    potenza_consumata = potenza_live
    potenza_generata += POTENZA_PRELEVABILE
    potenza_esportata = potenza_generata - potenza_consumata
    consumata_casa = monitor.total_grid_load - potenza_carica if wallbox.is_on else potenza_consumata    

    potenza_minima = CONFIG['MONOFASE_MIN_POWER'] if wallbox.fase == 0 else CONFIG['TRIFASE_MIN_POWER']
    potenza_massima = CONFIG['MONOFASE_MAX_POWER'] if wallbox.fase == 0 else CONFIG['TRIFASE_MAX_POWER']
    
    if potenza_consumata == 0:
        return

    log_msg(f"\n[INFO] Potenza Generata (+ prelevabile: {POTENZA_PRELEVABILE}W): {potenza_generata:.0f}W | Potenza Consumata: {monitor.total_grid_load:.0f}W | Consumata Live: {potenza_live:.0f}W | Potenza Esportata: {potenza_esportata:.0f}W | Wallbox: {'ON' if wallbox.is_on else 'OFF'} ({wallbox.current_set_power:.0f}W)")

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
                if potenza_generata < potenza_minima:
                    log_msg(f"[DECISIONE] Sole insufficiente. Spengo.")
                    try: 
                        asyncio.run(invia_notifica(f"⚠️ Potenza insufficiente ({potenza_generata:.0f}W). Spengo wallbox."))
                        if wallbox.fase == 1:
                            asyncio.run(invia_notifica(f"⚠️ Consiglio: mettere l'impianto in modalità monofase per sfruttare meglio la potenza disponibile."))
                        else:
                            asyncio.run(invia_notifica(f"⚠️ Consiglio: staccare la macchina"))
                    except Exception: pass
                    wallbox.turn_off(force=True)
                    return
                else:
                    log_msg(f"[DECISIONE] Generazione sufficiente. Continuo.")
                    wallbox.set_power(potenza_minima, bypass=True)
                    return

        if potenza_carica > (potenza_generata - consumata_casa) or potenza_esportata < 0:
            nuova_potenza = potenza_carica - abs(potenza_esportata)
            if nuova_potenza < potenza_minima or potenza_generata < potenza_minima:
                log_msg(f"[DECISIONE] Sole insufficiente. Minimo per {CONFIG['TIMER_SPEGNIMENTO']}s.")
                wallbox.set_power(potenza_minima, bypass=True)
                wallbox.pending_off_until = now + CONFIG['TIMER_SPEGNIMENTO']
            else:
                log_msg(f"[DECISIONE] Diminuisco a {nuova_potenza:.0f}W")
                wallbox.set_power(nuova_potenza, bypass=False)

        else: 
            nuova_potenza = potenza_carica + abs(potenza_esportata)
            if nuova_potenza > potenza_generata:
                return
            if nuova_potenza + consumata_casa > potenza_generata:
                nuova_potenza = potenza_generata - consumata_casa
            if nuova_potenza > potenza_massima:
                try: 
                    if wallbox.fase == 1:
                        asyncio.run(invia_notifica(f"⚠️ Potenza massima raggiunta ({potenza_massima:.0f}W)."))
                    else:
                        asyncio.run(invia_notifica(f"⚠️ Potenza massima raggiunta ({potenza_massima:.0f}W). Consiglio: mettere l'impianto in modalità trifase per sfruttare meglio la potenza disponibile."))
                except Exception: pass
                nuova_potenza = potenza_massima
                wallbox.set_power(nuova_potenza, bypass=True)
                log_msg(f"[DECISIONE] Aumento a {nuova_potenza:.0f}W")
                return
            log_msg(f"[DECISIONE] Aumento a {nuova_potenza:.0f}W")
            wallbox.set_power(nuova_potenza, bypass=False)

async def invia_notifica(messaggio):
    """Invia notifiche unilaterali (usato dal thread principale)"""
    if not API_KEY or not CHAT_ID: return
    try:
        bot = Bot(token=API_KEY)
        await bot.send_message(chat_id=CHAT_ID, text=messaggio)
    except Exception as e:
        log_msg(f"[ERRORE TELEGRAM] {e}")

# -----------------------------------------------------------
# MAIN
# -----------------------------------------------------------
def main():
    global wallbox_instance

    monitor = EnergyMonitor()
    wallbox_instance = WallboxController()
    wallbox = wallbox_instance

    # 1. AVVIO THREAD SERVER WEB
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True 
    flask_thread.start()
    log_msg(">>> INTERFACCIA WEB ATTIVA SU http://localhost:5000 <<<")

    # 2. AVVIO THREAD BOT TELEGRAM
    tg_thread = threading.Thread(target=run_telegram_polling)
    tg_thread.daemon = True
    tg_thread.start()

    try: 
        asyncio.run(invia_notifica(f"✅ SISTEMA AVVIATO."))
    except Exception: pass

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    try:
        sock.bind(('0.0.0.0', CONFIG['MCAST_PORT'])) 
        mreq = struct.pack("4s4s", socket.inet_aton(CONFIG['MCAST_GRP']), socket.inet_aton(CONFIG['IFACE']))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        log_msg(f"In ascolto su {CONFIG['IFACE']}:{CONFIG['MCAST_PORT']}...")
    except OSError as e:
        logging.critical(f"Errore Rete (Bind): {e}")
        return

    wallbox.initialize()
    
    while True:
        try:
            data, _ = sock.recvfrom(65535)
            evt = monitor.parse_packet(data)

            if evt == "TRIGGER":
                run_logic(monitor, wallbox)

        except KeyboardInterrupt:
            wallbox.turn_off(force=True)
            break
        except Exception as e:
            log_msg(f"[ERRORE] {e}")
            time.sleep(0.5)

if __name__ == "__main__":
    main()