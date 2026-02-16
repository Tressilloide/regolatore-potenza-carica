import threading
import queue
import socket
import struct
import xml.etree.ElementTree as ET
import requests
import time
from flask import Flask, render_template_string, Response, request, redirect, url_for

# ---------------------------------------------------------------------------
# CONFIGURAZIONE
# ---------------------------------------------------------------------------
MCAST_GRP = '224.192.32.19'
MCAST_PORT = 22600
IFACE = '192.168.1.193'  # IMPORTANTE: Deve essere l'IP di QUESTO PC
BASE_URL = "http://192.168.1.22" # IP del Dimmer/Smart Plug

# Limiti del dispositivo
VMIN = 1380
VMAX = 7360

# Variabili di stato globali
state_lock = threading.Lock()
last_slider_value = 0
last_state = None
last_request_time = 0
MIN_REQUEST_INTERVAL = 2.0  # Secondi minimi tra due comandi HTTP

# Coda per i dati verso il frontend
data_queue = queue.Queue()

app = Flask(__name__)

# ---------------------------------------------------------------------------
# FUNZIONI DI CONTROLLO DISPOSITIVO (Con Throttling)
# ---------------------------------------------------------------------------
def can_send_request():
    """Controlla se è passato abbastanza tempo dall'ultima richiesta."""
    global last_request_time
    now = time.time()
    if (now - last_request_time) > MIN_REQUEST_INTERVAL:
        last_request_time = now
        return True
    return False

def set_power(value, force=False):
    global last_slider_value, last_state
    
    # Arrotonda e converte in stringa
    val_int = int(round(value))
    
    # Evita di inviare lo stesso valore se non è cambiato (a meno che force=True)
    with state_lock:
        if not force and val_int == last_slider_value and last_state == True:
            return {"success": True, "msg": "Valore invariato, skip."}

    if not force and not can_send_request():
        return {"success": False, "msg": "Troppe richieste, skip."}

    url = f"{BASE_URL}/index.json?btn=P{val_int}"
    try:
        r = requests.get(url, timeout=2)
        with state_lock:
            last_slider_value = val_int
            last_state = True
        return {"success": True, "msg": f"Potenza impostata a {val_int}"}
    except Exception as e:
        return {"success": False, "msg": f"Errore HTTP: {e}"}

def power_on():
    global last_state
    
    with state_lock:
        if last_state == True: return {"success": True, "msg": "Già acceso"}

    if not can_send_request(): return {"success": False, "msg": "Skip (throttle)"}

    try:
        requests.get(f"{BASE_URL}/index.json?btn=i", timeout=2)
        with state_lock:
            last_state = True
        return {"success": True, "msg": "Acceso"}
    except Exception as e:
        return {"success": False, "msg": str(e)}

def power_off():
    global last_state
    
    with state_lock:
        if last_state == False: return {"success": True, "msg": "Già spento"}

    if not can_send_request(): return {"success": False, "msg": "Skip (throttle)"}

    try:
        requests.get(f"{BASE_URL}/index.json?btn=o", timeout=2)
        with state_lock:
            last_state = False
        return {"success": True, "msg": "Spento"}
    except Exception as e:
        return {"success": False, "msg": str(e)}

# ---------------------------------------------------------------------------
# THREAD MULTICAST
# ---------------------------------------------------------------------------
def multicast_thread():
    print(f"Inizializzazione socket multicast su {MCAST_GRP}:{MCAST_PORT}...")
    
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    try:
        # Tenta il bind sull'interfaccia specifica
        sock.bind((IFACE, MCAST_PORT))
    except Exception as e:
        print(f"⚠️ Errore bind su {IFACE}: {e}. Provo bind su 0.0.0.0")
        sock.bind(('', MCAST_PORT))

    mreq = struct.pack("4s4s", socket.inet_aton(MCAST_GRP), socket.inet_aton(IFACE))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

    print("🎧 In ascolto...")

    while True:
        try:
            data, addr = sock.recvfrom(8192)
            xml_str = data.decode('utf-8', errors='ignore')
            
            # Parsing XML manuale per evitare errori su XML malformati
            try:
                root = ET.fromstring(xml_str)
            except ET.ParseError:
                continue

            msg_lines = []

            # -------------------------------------------------------
            # GESTIONE PACCHETTO SOLAR (Logica principale)
            # -------------------------------------------------------
            if root.tag == 'solar':
                timestamp = root.findtext('timestamp', 'N/A')
                
                # Parsing basato sul TUO xml
                # <current><generating>...</current>
                curr_node = root.find('current')
                gen_w = 0.0
                exp_w = 0.0
                
                if curr_node is not None:
                    gen_txt = curr_node.findtext('generating', '0')
                    exp_txt = curr_node.findtext('exporting', '0')
                    try:
                        gen_w = float(gen_txt)
                        exp_w = float(exp_txt)
                    except ValueError:
                        pass

                msg_lines.append(f"[SOLAR] Gen: {gen_w}W | Exp: {exp_w}W")

                # --- LOGICA DI AUTOMAZIONE ---
                with state_lock:
                    current_setpoint = last_slider_value
                    is_on = last_state

                # Caso 1: C'è esportazione (Surplus) -> Aumentiamo il carico
                if exp_w > 10: # Soglia di tolleranza 10W
                    target = current_setpoint + exp_w
                    if target > VMAX: target = VMAX
                    if target < VMIN: target = VMIN # Se eravamo a 0
                    
                    # Se era spento, accendiamo prima al minimo
                    if not is_on:
                        res = power_on()
                        print(f"⚡ ACCENSIONE: {res['msg']}")
                        time.sleep(0.5) # Pausa tecnica
                        res = set_power(VMIN, force=True)
                    else:
                        res = set_power(target)
                        if res['success'] and "invariato" not in res['msg']:
                            print(f"📈 AUMENTO CARICO: {res['msg']}")

                # Caso 2: Stiamo consumando dalla rete (Gen < Load) -> Riduciamo
                # Se non esportiamo nulla, e la generazione è inferiore al nostro setpoint attuale
                elif exp_w <= 0 and gen_w < current_setpoint:
                    # Calcoliamo quanto ridurre. 
                    # Se il carico è 2000W ma il solare genera solo 1500W, dobbiamo scendere a 1500W.
                    target = gen_w
                    
                    if target < VMIN:
                        # Sotto il minimo tecnico -> Spegniamo
                        if is_on:
                            res = power_off()
                            print(f"📉 STOP (Gen insufficiente): {res['msg']}")
                    else:
                        res = set_power(target)
                        if res['success'] and "invariato" not in res['msg']:
                            print(f"📉 RIDUZIONE CARICO: {res['msg']}")

            # -------------------------------------------------------
            # GESTIONE PACCHETTO ELECTRICITY (Monitoraggio)
            # -------------------------------------------------------
            elif root.tag == 'electricity':
                # <property><current><watts>
                prop = root.find('property')
                total_watts = "N/A"
                if prop:
                    curr = prop.find('current')
                    if curr:
                        total_watts = curr.findtext('watts', 'N/A')
                
                msg_lines.append(f"[ELEC] Consumo Totale Casa: {total_watts} W")
                
                # Lista canali per debug
                chans = root.find('channels')
                if chans:
                    c_list = []
                    for c in chans.findall('chan'):
                        cid = c.get('id')
                        cw = c.findtext('curr', '0')
                        c_list.append(f"Ch{cid}:{cw}W")
                    msg_lines.append(" | ".join(c_list))

            # Invia al frontend se c'è qualcosa
            if msg_lines:
                full_msg = "\n".join(msg_lines)
                data_queue.put(full_msg)

        except Exception as e:
            print(f"Errore loop multicast: {e}")

# ---------------------------------------------------------------------------
# FRONTEND
# ---------------------------------------------------------------------------
HTML_PAGE = """
<!DOCTYPE html>
<html>
<head>
    <title>Solar Manager</title>
    <style>
        body { font-family: monospace; background: #222; color: #0f0; padding: 20px; }
        .box { border: 1px solid #444; padding: 10px; margin-bottom: 10px; background: #111; }
        #log { height: 400px; overflow-y: scroll; white-space: pre-wrap; font-size: 14px; }
        .status { font-size: 1.2em; font-weight: bold; color: white; }
    </style>
</head>
<body>
    <h1>Solar Manager Multicast</h1>
    
    <div class="box">
        <span class="status">Power: <span id="st-val">--</span> W | Stato: <span id="st-on">--</span></span>
        <form action="/reset" method="post" style="display:inline; float:right;">
            <button>RESET / START MIN</button>
        </form>
    </div>

    <div id="log" class="box"></div>

<script>
    const evtSource = new EventSource("/stream");
    const log = document.getElementById("log");
    
    evtSource.onmessage = function(e) {
        const line = document.createElement("div");
        line.textContent = new Date().toLocaleTimeString() + " " + e.data;
        log.appendChild(line);
        log.scrollTop = log.scrollHeight;
        
        // Aggiorna stato live se serve (opzionale)
        fetch('/status').then(r=>r.json()).then(d=>{
            document.getElementById("st-val").innerText = d.val;
            document.getElementById("st-on").innerText = d.on ? "ON" : "OFF";
        });
    };
</script>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(HTML_PAGE)

@app.route("/reset", methods=["POST"])
def reset():
    set_power(VMIN, force=True)
    power_on()
    return redirect("/")

@app.route("/status")
def status():
    with state_lock:
        return {"val": last_slider_value, "on": last_state}

@app.route("/stream")
def stream():
    def generate():
        while True:
            msg = data_queue.get()
            yield f"data: {msg}\n\n"
    return Response(generate(), mimetype="text/event-stream")

if __name__ == "__main__":
    t = threading.Thread(target=multicast_thread, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=5000, debug=True)