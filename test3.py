import requests
import socket
import struct
import xml.etree.ElementTree as ET

# --- CONFIGURAZIONE ---
BASE_URL = "http://192.168.1.22"   # Per leggere lo slider
MCAST_GRP = '224.192.32.19'
MCAST_PORT = 22600
IFACE = '192.168.1.139'  # IP della scheda Wi-Fi

# --- FUNZIONE PER LEGGERE VALORE SLIDER ---
def leggi_slider():
    try:
        r = requests.get(f"{BASE_URL}/index.json", timeout=3)
        if r.status_code == 200:
            data = r.json()
            # Supponendo che il JSON contenga {"slider": valore_attuale}
            slider_val = data.get("slider", "N/A")
            return slider_val
        else:
            print("Errore nella risposta HTTP:", r.status_code)
            return "N/A"
    except requests.exceptions.RequestException as e:
        print("Errore di comunicazione con il dispositivo:", e)
        return "N/A"

# --- FUNZIONE PER LEGGERE POTENZA DAL MULTICAST ---
def leggi_potenza():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((IFACE, MCAST_PORT))

    # Join al gruppo multicast
    mreq = struct.pack("4s4s", socket.inet_aton(MCAST_GRP), socket.inet_aton(IFACE))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

    print(f"Ascoltando multicast {MCAST_GRP}:{MCAST_PORT} sull'interfaccia {IFACE}...\n")

    data, addr = sock.recvfrom(8192)
    try:
        xml_str = data.decode('utf-8')
        root = ET.fromstring(xml_str)

        current = root.find('current')
        if current is not None:
            generating_w = current.findtext('generating', default='N/A')
            exporting_w = current.findtext('exporting', default='N/A')
            try:
                delta_w = float(generating_w) - float(exporting_w)
            except ValueError:
                delta_w = 'N/A'
        else:
            generating_w = exporting_w = delta_w = 'N/A'

        return generating_w, exporting_w, delta_w

    except ET.ParseError:
        print("Errore nel parsing XML")
        return 'N/A', 'N/A', 'N/A'
    except UnicodeDecodeError:
        print("Pacchetto non decodificabile come UTF-8")
        return 'N/A', 'N/A', 'N/A'

# --- MAIN ---
if __name__ == "__main__":
    # Legge potenze
    generating, exporting, consumata = leggi_potenza()

    # Legge valore attuale dello slider
    slider_val = leggi_slider()

    # Stampa chiara
    print(f"Potenza generata (W): {generating}")
    print(f"Potenza esportata (W): {exporting}")
    print(f"Potenza consumata (W): {consumata}")
    print(f"Valore attuale dello slider: {slider_val}")
