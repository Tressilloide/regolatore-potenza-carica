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

        # Parsing SOLAR
        if root.tag == 'solar':
            timestamp = root.findtext('timestamp', default='N/A')
            current = root.find('current')
            if current is not None:
                generating_w = current.findtext('generating', default='N/A')
                exporting_w = current.findtext('exporting', default='N/A')
            else:
                generating_w = exporting_w = 'N/A'

            day = root.find('day')
            if day is not None:
                day_generated_wh = day.findtext('generated', default='N/A')
                day_exported_wh = day.findtext('exported', default='N/A')
            else:
                day_generated_wh = day_exported_wh = 'N/A'

            print(f"[SOLAR]")
            print(f"Timestamp: {timestamp}")
            print(f"Potenza generata (W): {generating_w}")
            print(f"Potenza esportata (W): {exporting_w}")
            print(f"Energia generata oggi (Wh): {day_generated_wh}")
            print(f"Energia esportata oggi (Wh): {day_exported_wh}")
            print("-" * 50)

        # Parsing ELECTRICITY
        elif root.tag == 'electricity':
            print(f"[ELECTRICITY]")
            channels_elem = root.find('channels')
            if channels_elem is not None:
                channels = channels_elem.findall('chan')
                
                for i in range(6):
                    if i < len(channels):
                        curr = channels[i].findtext('curr', default='N/A')
                        day = channels[i].findtext('day', default='N/A')
                        print(f"Chan {i}: {curr} W (giorno: {day} Wh)")

            property_elem = root.find('property')
            if property_elem is not None:
                current_elem = property_elem.find('current')
                if current_elem is not None:
                    watts = current_elem.findtext('watts', default='N/A')
                    print(f"Potenza totale: {watts} W")
                
                day_elem = property_elem.find('day')
                if day_elem is not None:
                    wh = day_elem.findtext('wh', default='N/A')
                    print(f"Energia totale oggi: {wh} Wh")
            print("-" * 50)

    except ET.ParseError:
        print("Errore nel parsing XML")
    except UnicodeDecodeError:
        print("Pacchetto non decodificabile come UTF-8")
