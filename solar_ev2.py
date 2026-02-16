import socket
import struct
import xml.etree.ElementTree as ET
import time
import requests
import logging

# -----------------------------------------------------------
# CONFIGURAZIONE
# -----------------------------------------------------------
MCAST_GRP = '224.192.32.19'
MCAST_PORT = 22600
IFACE = '192.168.1.193'  # <-- IP DEL TUO RASPBERRY

WALLBOX_IP = '192.168.1.22'
WALLBOX_URL = f"http://{WALLBOX_IP}/index.json"

# Limiti
MIN_POWER = 1380  # 6A
MAX_POWER = 7360  # 32A

# Parametri Logica
HYSTERESIS_W = 100       
UPDATE_INTERVAL_S = 5    
OFF_THRESHOLD_W = 1380   

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(message)s', datefmt='%H:%M:%S')

# -----------------------------------------------------------
# GESTORE WALLBOX 
# -----------------------------------------------------------
class WallboxController:
    def __init__(self):
        self.current_set_power = MIN_POWER
        self.is_on = False
        self.last_update_time = 0

    def send_command(self, params):
        try:
            response = requests.get(WALLBOX_URL, params=params, timeout=3)
            return response.status_code == 200
        except Exception:
            return False

    def set_power(self, watts):
        watts = max(MIN_POWER, min(MAX_POWER, int(watts)))
        
        if abs(watts - self.current_set_power) < HYSTERESIS_W and self.is_on:
            return

        now = time.time()
        if self.last_update_time > 0 and (now - self.last_update_time < UPDATE_INTERVAL_S):
            return

        print(f"   >>> [AZIONE] CAMBIO POTENZA -> {watts} W")
        if self.send_command({'btn': f'P{watts}'}):
            self.current_set_power = watts
            self.last_update_time = now

    def turn_on(self):
        if not self.is_on:
            print("   >>> [AZIONE] ACCENSIONE (ON)")
            if self.send_command({'btn': 'i'}):
                self.is_on = True
                self.last_update_time = time.time()

    def turn_off(self, force=False):
        now = time.time()
        if force and self.last_update_time != 0 and (now - self.last_update_time < UPDATE_INTERVAL_S):
            return

        if self.is_on or force:
            print("   >>> [AZIONE] SPEGNIMENTO (OFF)")
            if self.send_command({'btn': 'o'}):
                self.is_on = False
                self.last_update_time = time.time()
                time.sleep(0.5)
                self.send_command({'btn': f'P{MIN_POWER}'})
                self.current_set_power = MIN_POWER

    def initialize(self):
        print("\n=== INIZIALIZZAZIONE SISTEMA ===")
        print("1. Imposto potenza minima (1380W)...")
        self.set_power(MIN_POWER)
        time.sleep(1)
        print("2. Metto in OFF (Attesa dati)...")
        self.last_update_time = 0 
        self.turn_off(force=True)
        print("=== PRONTO. IN ATTESA PACCHETTI ===\n")

# -----------------------------------------------------------
# MONITOR DATI 
# -----------------------------------------------------------
class EnergyMonitor:
    def __init__(self):
        self.solar_now = 0.0        
        self.total_grid_load = 0.0  

    def parse_packet(self, data):
        try:
            xml_str = data.decode('utf-8', errors='ignore')
            root = ET.fromstring(xml_str)
            
            # --- PACCHETTO FASI (Completo: arriva ogni 60s) ---
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
                    
                    # STAMPA TABELLA GRANDE
                    print("\n" + "="*60)
                    print(f" ⚡ DATO FASI (60s)")
                    print("-" * 60)
                    print(f" | RETE (Casa+WB) | L1: {l1:5.0f} | L2: {l2:5.0f} | L3: {l3:5.0f} | TOT: {self.total_grid_load:.0f}W")
                    print(f" | SOLARE (Inv)   | L4: {l4:5.0f} | L5: {l5:5.0f} | L6: {l6:5.0f} | TOT: {self.solar_now:.0f}W")
                    print("="*60)
                    
                    return "TRIGGER"

            # --- PACCHETTO SOLAR (Veloce) ---
            elif root.tag == 'solar':
                curr = root.find('current')
                if curr is not None:
                    gen = float(curr.find('generating').text)
                    self.solar_now = gen
                    
                    # STAMPA SEMPRE IL PACCHETTO VELOCE
                    print(f" ☀️  SOLARE LIVE: {gen:.0f} W") 
                    
                    return "TRIGGER"
                
        except Exception:
            pass
        return None

# -----------------------------------------------------------
# LOGICA DI CONTROLLO
# -----------------------------------------------------------
def run_logic(monitor, wallbox):
    gen = monitor.solar_now
    grid_total = monitor.total_grid_load 
    
    # Calcolo Consumo Casa Puro
    wb_power = wallbox.current_set_power if wallbox.is_on else 0.0
    house_pure = max(0, grid_total - wb_power)
    
    # Calcolo Disponibile
    surplus = gen - house_pure
    
    # --- SICUREZZA NUVOLA ---
    if wallbox.is_on and wb_power > gen:
        print(f"   !!! ALLARME NUVOLA !!! (Carica {wb_power:.0f} > Sole {gen:.0f})")
        if gen < OFF_THRESHOLD_W:
            wallbox.turn_off(force=True)
        else:
            wallbox.set_power(gen)
        return

    # --- STANDARD ---
    
    # SPEGNIMENTO
    if gen < OFF_THRESHOLD_W:
        if wallbox.is_on: 
            print("   [DECISIONE] Sole insufficiente. Spengo.")
        wallbox.turn_off(force=True)
        return

    # AVVIO
    if not wallbox.is_on:
        if surplus > MIN_POWER:
            print(f"   [DECISIONE] Surplus ok ({surplus:.0f}W). Inizia Carica.")
            wallbox.turn_on()
            wallbox.set_power(MIN_POWER)
        return

    # REGOLAZIONE
    if wallbox.is_on:
        target = surplus
        if target > gen: target = gen
        wallbox.set_power(target)

# -----------------------------------------------------------
# MAIN
# -----------------------------------------------------------
def main():
    monitor = EnergyMonitor()
    wallbox = WallboxController()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    try:
        sock.bind((IFACE, MCAST_PORT))
        mreq = struct.pack("4s4s", socket.inet_aton(MCAST_GRP), socket.inet_aton(IFACE))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        print(f"In ascolto su {IFACE}:{MCAST_PORT}...")
    except OSError as e:
        logging.critical(f"Errore Rete (Bind): {e}")
        return

    wallbox.initialize()
    
    while True:
        try:
            data, _ = sock.recvfrom(65535)
            evt = monitor.parse_packet(data)

            # La logica parte per OGNI pacchetto ricevuto
            if evt == "TRIGGER":
                run_logic(monitor, wallbox)

        except KeyboardInterrupt:
            wallbox.turn_off(force=True)
            break
        except Exception as e:
            time.sleep(0.5)

if __name__ == "__main__":
    main()