import socket
import struct
import xml.etree.ElementTree as ET
import time

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By


# -----------------------------------------------------------
# 1) CONFIGURAZIONE SELENIUM
# -----------------------------------------------------------
URL = "http://192.168.1.22/"

service = Service()
driver = webdriver.Chrome(service=service)
driver.get(URL)

time.sleep(2)  # attesa caricamento pagina

# -----------------------------------------------------------
# 2) CONFIGURAZIONE MULTICAST UDP
# -----------------------------------------------------------
MCAST_GRP = '224.192.32.19'
MCAST_PORT = 22600
IFACE = '192.168.1.139'  # IP della tua interfaccia Wi-Fi

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind((IFACE, MCAST_PORT))

mreq = struct.pack("4s4s", socket.inet_aton(MCAST_GRP), socket.inet_aton(IFACE))
sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

print(f"Ascoltando multicast {MCAST_GRP}:{MCAST_PORT} sull'interfaccia {IFACE}...\n")


# -----------------------------------------------------------
# 3) CICLO PRINCIPALE
# -----------------------------------------------------------
while True:
    data, addr = sock.recvfrom(8192)

    # === LETTURA DELLO SLIDER SELENIUM ===
    try:
        slider = driver.find_element(By.ID, "powers")
        slider_value = slider.get_attribute("value")
    except Exception:
        slider_value = "N/A"

    # === PARSING XML MULTICAST ===
    try:
        xml_str = data.decode('utf-8')
        root = ET.fromstring(xml_str)

        timestamp = root.findtext('timestamp', default='N/A')
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

        # === OUTPUT COMPLETO ===
        print(f"Timestamp: {timestamp}")
        print(f"Potenza generata (W): {generating_w}")
        print(f"Potenza esportata (W): {exporting_w}")
        print(f"Potenza consumata (W): {delta_w}")
        print(f"Valore slider (powers): {slider_value}")
        print("-" * 50)

    except ET.ParseError:
        print("Errore nel parsing XML")
    except UnicodeDecodeError:
        print("Pacchetto non decodificabile come UTF-8")
