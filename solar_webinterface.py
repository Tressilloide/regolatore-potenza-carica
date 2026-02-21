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
from dotenv import load_dotenv
from telegram import Bot
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
    'POTENZA_PROTEZIONE': 300,      # Modificabile da Web
    'POTENZA_PRELEVABILE': 0,       # Modificabile da Web
    'COOLDOWN_ACCENSIONE': 60,
    'UPDATE_INTERVAL_S': 5,
    'TIMER_SPEGNIMENTO': 60,
    'MCAST_GRP': '224.192.32.19',
    'MCAST_PORT': 22600,
    'IFACE': '192.168.1.193',
    'WALLBOX_IP': '192.168.1.22'
}

WALLBOX_URL = f"http://{CONFIG['WALLBOX_IP']}/index.json"

# Stato condiviso per la Web UI
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
# INTERFACCIA WEB (HTML/JS)
# -----------------------------------------------------------
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

                // Aggiorna Inputs (solo se non hanno focus)
                if (document.activeElement.id !== 'prelevabile') 
                    document.getElementById('prelevabile').placeholder = data.config.prelevabile;
                if (document.activeElement.id !== 'protezione') 
                    document.getElementById('protezione').placeholder = data.config.protezione;

                // Aggiorna Stato e Calcolo Secondi Trascorsi
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

                // Aggiorna Fasi e Totali
                const f = data.status.fasi;
                for(let i=0; i<6; i++) {
                    document.getElementById('l'+(i+1)).innerText = Math.round(f[i]);
                }
                document.getElementById('tot_grid').innerText = Math.round(data.status.grid_total);
                document.getElementById('tot_solar').innerText = Math.round(data.status.solar_total);

                // Aggiorna Grafico
                const history = data.history;
                chart.data.labels = history.map(h => formatTime(h.time));
                chart.data.datasets[0].data = history.map(h => h.grid);
                chart.data.datasets[1].data = history.map(h => h.solar);
                chart.data.datasets[2].data = history.map(h => h.wb);
                chart.update();

                // Aggiorna Console
                const consoleDiv = document.getElementById('console');
                // Controlliamo se l'utente ha scrollato in su, in tal caso non forziamo lo scroll in basso
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

        // Avvio
        fetchData();
        setInterval(fetchData, 2000); // Aggiorna ogni 2 secondi
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
    # Combina i dati. Assumiamo che la lista fasi sia la principale per il timing
    for item in SYSTEM_STATE['ULTIME_LETTURE_FASI']:
        history.append({
            'grid': item[0],
            'solar': item[1],
            'time': item[3],
            'wb': item[4] if len(item) > 4 else 0 
        })
    
    # Calcolo totale fasi in background
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

def run_flask():
    app.run(host='0.0.0.0', port=80, debug=False, use_reloader=False)

# -----------------------------------------------------------
# GESTORE WALLBOX 
# -----------------------------------------------------------
class WallboxController:
    def __init__(self):
        self.current_set_power = 0
        self.is_on = False
        self.last_update_time = 0
        self.fase = 0
        self.time_turned_off = 0  
        self.pending_off_until = 0

    def update_shared_state(self):
        SYSTEM_STATE['WALLBOX_POWER'] = self.current_set_power
        SYSTEM_STATE['WALLBOX_STATUS'] = self.is_on
        SYSTEM_STATE['IMPIANTO_FASE'] = self.fase

    def send_command(self, params):
        try:
            response = requests.get(WALLBOX_URL, params=params, timeout=3)
            return response.status_code == 200
        except Exception:
            return False

    def set_power(self, watts):
        # Usa i valori da CONFIG invece delle costanti globali
        if self.fase == 0:
            watts = max(CONFIG['MONOFASE_MIN_POWER'], min(CONFIG['MONOFASE_MAX_POWER'], int(watts)))
        elif self.fase == 1:
            watts = max(CONFIG['TRIFASE_MIN_POWER'], min(CONFIG['TRIFASE_MAX_POWER'], int(watts)))

        if abs(watts - self.current_set_power) < CONFIG['POTENZA_PROTEZIONE'] and self.is_on:
            log_msg(f"[INFO] Variazione potenza ({watts}W) inferiore alla soglia di protezione ({CONFIG['POTENZA_PROTEZIONE']}W). Nessun cambiamento.")
            return

        now = time.time()
        if self.last_update_time > 0 and (now - self.last_update_time < CONFIG['UPDATE_INTERVAL_S']):
            return

        log_msg(f"[AZIONE] CAMBIO POTENZA -> {watts} W")

        if self.send_command({'btn': f'P{watts}'}):
            self.current_set_power = watts
            self.last_update_time = now
            self.update_shared_state()
        

    def turn_on(self):
        if not self.is_on:
            if self.time_turned_off > 0:
                tempo_trascorso = time.time() - self.time_turned_off
                if tempo_trascorso < CONFIG['COOLDOWN_ACCENSIONE']:
                    log_msg(f"[INFO] Attesa cooldown: {CONFIG['COOLDOWN_ACCENSIONE'] - tempo_trascorso:.1f}s prima di accendere")
                    return
            
            log_msg("[AZIONE] ACCENSIONE (ON)")
            self.set_power(CONFIG['MONOFASE_MIN_POWER'] if self.fase == 0 else CONFIG['TRIFASE_MIN_POWER']) 

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
                self.send_command({'btn': f'P{min_p}'})
                self.current_set_power = min_p
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
            self.set_power(CONFIG['MONOFASE_MIN_POWER'])
        elif self.fase == 1:
            log_msg("1. Imposto potenza minima (4140)...")
            self.set_power(CONFIG['TRIFASE_MIN_POWER'])

        time.sleep(1)
        log_msg("=== PRONTO. IN ATTESA PACCHETTI ===")

# -----------------------------------------------------------
# MONITOR DATI 
# -----------------------------------------------------------
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
            
            if root.tag == 'electricity': #fasi
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
                    
                    # Rimuoviamo il print "gigante" per non spammare la nuova console/terminale
                    # print(f" | RETE | TOT: {self.total_grid_load:.0f}W   ---   | SOLARE | TOT: {self.solar_now:.0f}W")
                    
                    self.ctrletturefasi += 1
                    SYSTEM_STATE['ULTIMA_LETTURA_FASI'] = time.time()
                    SYSTEM_STATE['MONITOR_FASI'] = self.fases
                    self.time = SYSTEM_STATE['ULTIMA_LETTURA_FASI']
                    
                    # Recupero la potenza della wallbox SOLO SE ACCESA, altrimenti 0 per il grafico
                    wb_status = SYSTEM_STATE.get('WALLBOX_STATUS', False)
                    wb_power = SYSTEM_STATE.get('WALLBOX_POWER', 0) if wb_status else 0
                    
                    # Aggiorno la lista per il grafico aggiungendo wb_power come quinto elemento
                    SYSTEM_STATE['ULTIME_LETTURE_FASI'].append((self.total_grid_load, self.solar_now, self.fases, self.time, wb_power))
                    if len(SYSTEM_STATE['ULTIME_LETTURE_FASI']) > 30:
                        SYSTEM_STATE['ULTIME_LETTURE_FASI'].pop(0)    
            
                    return "TRIGGER"


            elif root.tag == 'solar': #generata
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

# -----------------------------------------------------------
# LOGICA DI CONTROLLO
# -----------------------------------------------------------
def run_logic(monitor, wallbox):
    # Recupera i valori dinamici da CONFIG
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

    # Manteniamo questo log per capire sempre il contesto ad ogni ciclo
    log_msg(f"\n[INFO] Potenza Generata (+ prelevabile: {POTENZA_PRELEVABILE}W): {potenza_generata:.0f}W | Potenza Consumata: {monitor.total_grid_load:.0f}W | Consumata Live: {potenza_live:.0f}W | Potenza Esportata: {potenza_esportata:.0f}W | Wallbox: {'ON' if wallbox.is_on else 'OFF'} ({wallbox.current_set_power:.0f}W)")
    
    if wallbox.pending_off_until and potenza_generata >= potenza_minima:
        log_msg("[INFO] Generazione ripristinata. Annullamento spegnimento programmato.")
        wallbox.pending_off_until = 0

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
                log_msg(f"[INFO] Timer spegnimento: {restante:.0f}s...")
                if potenza_generata >= potenza_minima:
                    log_msg("[INFO] Generazione ripristinata. Annullo spegnimento.")
                    wallbox.pending_off_until = 0
                else:
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
                    wallbox.set_power(potenza_minima)
                    return

        if potenza_carica > (potenza_generata - consumata_casa) or potenza_esportata < 0:
            nuova_potenza = potenza_carica - abs(potenza_esportata)
            if nuova_potenza < potenza_minima or potenza_generata < potenza_minima:
                log_msg(f"[DECISIONE] Sole insufficiente. Minimo per {CONFIG['TIMER_SPEGNIMENTO']}s.")
                wallbox.set_power(potenza_minima)
                wallbox.pending_off_until = now + CONFIG['TIMER_SPEGNIMENTO']
            else:
                log_msg(f"[DECISIONE] Diminuisco a {nuova_potenza:.0f}W")
                wallbox.set_power(nuova_potenza)

        else: 
            nuova_potenza = potenza_carica + abs(potenza_esportata)
            if nuova_potenza > potenza_generata:
                return
            if nuova_potenza > potenza_massima:
                nuova_potenza = potenza_massima
            log_msg(f"[DECISIONE] Aumento a {nuova_potenza:.0f}W")
            wallbox.set_power(nuova_potenza)

async def invia_notifica(messaggio):
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
    # AVVIO THREAD SERVER WEB
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True # Si chiude quando chiudi lo script
    flask_thread.start()
    log_msg(">>> INTERFACCIA WEB ATTIVA SU http://localhost:5000 <<<")

    try: 
        asyncio.run(invia_notifica(f"SISTEMA AVVIATO."))
    except Exception: pass
    
    monitor = EnergyMonitor()
    wallbox = WallboxController()

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