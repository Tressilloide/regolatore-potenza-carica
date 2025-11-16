import threading
import queue
import socket
import struct
import xml.etree.ElementTree as ET
import requests
from flask import Flask, render_template_string, Response, request, redirect, url_for

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
MCAST_GRP = '224.192.32.19'
MCAST_PORT = 22600
IFACE = '192.168.1.139'  # Cambia se la tua scheda ha un altro IP
BASE_URL = "http://192.168.1.22"

vmin = 1380
vmax = 7360

# Coda per i dati che arriveranno al frontend via SSE
data_queue = queue.Queue()

# ---------------------------------------------------------------------------
# FUNZIONE set_power
# ---------------------------------------------------------------------------
def set_power(value, base_url=BASE_URL, timeout=3):
    val_str = str(value)
    if not val_str.isdigit():
        raise ValueError("Valore non valido: usare solo cifre.")

    url = f"{base_url}/index.json?btn=P{val_str}"

    try:
        r = requests.get(url, timeout=timeout)
        return {"success": r.status_code == 200, "status": r.status_code, "text": r.text}
    except Exception as e:
        return {"success": False, "status": None, "error": str(e)}

# ---------------------------------------------------------------------------
# THREAD PER MULTICAST
# ---------------------------------------------------------------------------
def multicast_thread():

    print("Inizializzazione socket multicast…")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    try:
        sock.bind((IFACE, MCAST_PORT))
    except:
        print("⚠ ERRORE bind sull'interfaccia → provo bind globale")
        sock.bind(('', MCAST_PORT))

    mreq = struct.pack("4s4s",
                       socket.inet_aton(MCAST_GRP),
                       socket.inet_aton(IFACE))

    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

    print(f"🎧 Ascolto multicast su {MCAST_GRP}:{MCAST_PORT} (iface {IFACE})")

    while True:
        data, addr = sock.recvfrom(8192)

        try:
            xml_str = data.decode('utf-8')
            root = ET.fromstring(xml_str)

            # -------------------------------------------------------
            # SOLAR
            # -------------------------------------------------------
            if root.tag == 'solar':
                timestamp = root.findtext('timestamp', default='N/A')

                current = root.find('current')
                generating_w = current.findtext('generating', default='N/A') if current is not None else 'N/A'
                exporting_w = current.findtext('exporting', default='N/A') if current is not None else 'N/A'

                day = root.find('day')
                day_generated_wh = day.findtext('generated', default='N/A') if day is not None else 'N/A'
                day_exported_wh = day.findtext('exported', default='N/A') if day is not None else 'N/A'

                msg = (
                    "[SOLAR]\n"
                    f"Timestamp: {timestamp}\n"
                    f"Potenza generata (W): {generating_w}\n"
                    f"Potenza esportata (W): {exporting_w}\n"
                    f"Energia generata oggi (Wh): {day_generated_wh}\n"
                    f"Energia esportata oggi (Wh): {day_exported_wh}\n"
                    + "-"*50
                )

                print("RX SOLAR → OK")
                data_queue.put(msg)

            # -------------------------------------------------------
            # ELECTRICITY
            # -------------------------------------------------------
            elif root.tag == 'electricity':

                lines = ["[ELECTRICITY]"]

                channels_elem = root.find('channels')
                if channels_elem is not None:
                    channels = channels_elem.findall('chan')
                    for i in range(6):
                        if i < len(channels):
                            curr = channels[i].findtext('curr', default='N/A')
                            day = channels[i].findtext('day', default='N/A')
                            lines.append(f"Chan {i}: {curr} W (giorno: {day} Wh)")

                property_elem = root.find('property')
                if property_elem is not None:
                    current_elem = property_elem.find('current')
                    if current_elem is not None:
                        watts = current_elem.findtext('watts', default='N/A')
                        lines.append(f"Potenza totale: {watts} W")

                    day_elem = property_elem.find('day')
                    if day_elem is not None:
                        wh = day_elem.findtext('wh', default='N/A')
                        lines.append(f"Energia totale oggi: {wh} Wh")

                lines.append("-"*50)
                msg = "\n".join(lines)

                print("RX ELECTRICITY → OK")
                data_queue.put(msg)

        except Exception as e:
            print("❌ Errore XML:", e)
            data_queue.put(f"Errore parsing XML: {e}")

# ---------------------------------------------------------------------------
# FRONTEND HTML
# ---------------------------------------------------------------------------
HTML_PAGE = """
<!DOCTYPE html>
<html>
<head>
    <title>Monitor Multicast</title>
    <style>
        body { font-family: Arial; margin: 20px; }
        #log {
            width: 100%;
            height: 500px;
            overflow-y: scroll;
            white-space: pre-line;
            border: 1px solid #444;
            background: #f0f0f0;
            padding: 10px;
        }
        button { padding: 15px; font-size: 18px; }
    </style>
</head>
<body>
    <h1>Monitor Multicast</h1>

    <form action="/start" method="POST">
        <button type="submit">START (set_power vmin)</button>
    </form>

    <h2>Dati ricevuti</h2>
    <div id="log"></div>

<script>
    var evtSource = new EventSource("/stream");

    evtSource.onmessage = function(e) {
        let log = document.getElementById("log");
        log.textContent += e.data + "\\n";
        log.scrollTop = log.scrollHeight;
    };
</script>

</body>
</html>
"""

# ---------------------------------------------------------------------------
# ROTTE FLASK
# ---------------------------------------------------------------------------
app = Flask(__name__)

@app.route("/")
def index():
    return render_template_string(HTML_PAGE)

@app.route("/start", methods=["POST"])
def start():
    result = set_power(vmin)
    data_queue.put(f"Comando inviato: set_power({vmin}) → {result}")
    return redirect(url_for("index"))

@app.route("/stream")
def stream():
    print("🌐 Browser collegato allo stream SSE")

    def event_stream():
        while True:
            msg = data_queue.get()
            print("→ Inviato al browser:", msg.replace("\n", " | "))

            # Spezza ogni linea e inviala come singola riga SSE
            for line in msg.split("\n"):
                yield f"data: {line}\n"
            yield "\n"  # marca la fine dell'evento

    return Response(event_stream(),
                    mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    threading.Thread(target=multicast_thread, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=True)
