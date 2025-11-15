import socket
import struct
import xml.etree.ElementTree as ET

MCAST_GRP = '224.192.32.19'
MCAST_PORT = 22600
IFACE = '192.168.1.139'  # IP della scheda Wi-Fi

# Creazione socket UDP
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind((IFACE, MCAST_PORT))

# Join al gruppo multicast
mreq = struct.pack("4s4s", socket.inet_aton(MCAST_GRP), socket.inet_aton(IFACE))
sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

print(f"Ascoltando multicast {MCAST_GRP}:{MCAST_PORT} sull'interfaccia {IFACE}...\n")

while True:
    data, addr = sock.recvfrom(8192)
    try:
        xml_str = data.decode('utf-8')
        root = ET.fromstring(xml_str)

        solar = root.findtext('solar', default='N/A')
        electricity = root.findtext('electricity', default='N/A')
        timestamp = root.findtext('timestamp', default='N/A')

        if(solar != 'N/A'):
            generating_w = solar.findtext('generating', default='N/A')
            exporting_w = solar.findtext('exporting', default='N/A')
             # Stampa chiara e pulita
            print(f"Timestamp: {timestamp}")
            print(f"Potenza generata (W): {generating_w}")
            print(f"Potenza esportata (W): {exporting_w}")
            print("-" * 50)

        if(electricity != 'N/A'):
            channels = electricity.findall('chan')
            
            chan0_curr = channels[0].findtext('curr', default='N/A') if len(channels) > 0 else 'N/A'
            chan1_curr = channels[1].findtext('curr', default='N/A') if len(channels) > 1 else 'N/A'
            chan2_curr = channels[2].findtext('curr', default='N/A') if len(channels) > 2 else 'N/A'
            chan3_curr = channels[3].findtext('curr', default='N/A') if len(channels) > 3 else 'N/A'
            chan4_curr = channels[4].findtext('curr', default='N/A') if len(channels) > 4 else 'N/A'
            chan5_curr = channels[5].findtext('curr', default='N/A') if len(channels) > 5 else 'N/A'
            
            print(f"Chan 0: {chan0_curr} W")
            print(f"Chan 1: {chan1_curr} W")
            print(f"Chan 2: {chan2_curr} W")
            print(f"Chan 3: {chan3_curr} W")
            print(f"Chan 4: {chan4_curr} W")
            print(f"Chan 5: {chan5_curr} W")
            print("-" * 50)
    

    except ET.ParseError:
        print("Errore nel parsing XML")
    except UnicodeDecodeError:
        print("Pacchetto non decodificabile come UTF-8")
