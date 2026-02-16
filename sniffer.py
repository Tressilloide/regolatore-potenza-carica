import socket
import struct

# CONFIGURAZIONE
MCAST_GRP = '224.192.32.19'
MCAST_PORT = 22600

# INSERISCI QUI L'IP DEL TUO RASPBERRY PI (es. 192.168.1.50)
# Esegui 'hostname -I' nel terminale per trovarlo
RPI_IP = '0.0.0.0' # <-- CAMBIA QUESTO CON IL TUO IP REALE SE NON FUNZIONA

def test_sniffer():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    print(f"Tentativo di bind su porta {MCAST_PORT}...")
    sock.bind(('', MCAST_PORT))

    print(f"Tentativo di join al gruppo {MCAST_GRP} tramite interfaccia {RPI_IP}...")
    
    # Qui forziamo l'interfaccia specifica usando l'IP del Raspberry
    if RPI_IP == '0.0.0.0':
        mreq = struct.pack("4sl", socket.inet_aton(MCAST_GRP), socket.INADDR_ANY)
    else:
        # Specifica l'interfaccia esatta
        mreq = struct.pack("4s4s", socket.inet_aton(MCAST_GRP), socket.inet_aton(RPI_IP))

    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

    print("--- IN ASCOLTO (CTRL+C per uscire) ---")
    
    while True:
        try:
            data, addr = sock.recvfrom(10240)
            print(f"RICEVUTO da {addr}: {data.decode('utf-8', errors='ignore')[:100]}...")
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"Errore: {e}")

if __name__ == '__main__':
    test_sniffer()