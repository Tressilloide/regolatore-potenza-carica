import socket
import struct

# -----------------------------------------------------------
# CONFIGURAZIONE MULTICAST UDP
# -----------------------------------------------------------
MCAST_GRP = '224.192.32.19'
MCAST_PORT = 22600
IFACE = '192.168.1.193'  # Assicurati che questo sia ancora il tuo IP attuale

# Creazione del socket UDP
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

try:
    sock.bind((IFACE, MCAST_PORT))
except OSError as e:
    print(f"Errore nel bind: {e}")
    exit()

# Aggiunta al gruppo Multicast
mreq = struct.pack("4s4s", socket.inet_aton(MCAST_GRP), socket.inet_aton(IFACE))
sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

print(f"In ascolto di TUTTI i pacchetti su {MCAST_GRP}:{MCAST_PORT}...")
print(f"Interfaccia locale: {IFACE}")
print("-" * 50)

# -----------------------------------------------------------
# CICLO PRINCIPALE
# -----------------------------------------------------------
while True:
    try:
        # Buffer aumentato a 65535 per essere sicuri di prendere pacchetti grossi
        data, addr = sock.recvfrom(65535)

        print(f"\n[Ricevuto da {addr[0]}:{addr[1]}]")
        
        # Tentativo di decodifica e stampa del testo completo
        try:
            decoded_data = data.decode('utf-8')
            print(decoded_data)
        except UnicodeDecodeError:
            # Se i dati sono binari e non testo, stampiamo i byte grezzi
            print(f"Dati binari (non testuali): {data}")

        print("-" * 50)

    except KeyboardInterrupt:
        print("\nChiusura script.")
        break
    except Exception as e:
        print(f"Errore generico: {e}")