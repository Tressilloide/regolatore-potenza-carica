import socket
import struct
import xml.etree.ElementTree as ET
import time
import requests
import logging
import json
import os
from dotenv import load_dotenv
import asyncio
from telegram import Bot

# -----------------------------------------------------------
# CONFIGURAZIONE
# -----------------------------------------------------------
MCAST_GRP = '224.192.32.19'
MCAST_PORT = 22600
IFACE = '192.168.1.193' 

WALLBOX_IP = '192.168.1.22'
WALLBOX_URL = f"http://{WALLBOX_IP}/index.json"


#parametri fasi
MONOFASE_MIN_POWER = 1380  #6A
MONOFASE_MAX_POWER = 7360  #32A

TRIFASE_MIN_POWER = 4140  #6A
TRIFASE_MAX_POWER = 22000  #32A

POTENZA_PROTEZIONE = 300    
COOLDOWN_ACCENSIONE = 60   
POTENZA_PRELEVABILE = 0
UPDATE_INTERVAL_S = 5    
TIMER_SPEGNIMENTO = 60

load_dotenv()
API_KEY = os.getenv('API_KEY')
CHAT_ID = os.getenv('CHAT_ID')
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(message)s', datefmt='%H:%M:%S')

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

    def send_command(self, params):
        try:
            response = requests.get(WALLBOX_URL, params=params, timeout=3)
            return response.status_code == 200
        except Exception:
            return False

    def set_power(self, watts):
        if self.fase == 0:
            watts = max(MONOFASE_MIN_POWER, min(MONOFASE_MAX_POWER, int(watts)))
        elif self.fase == 1:
            watts = max(TRIFASE_MIN_POWER, min(TRIFASE_MAX_POWER, int(watts)))

        if abs(watts - self.current_set_power) < POTENZA_PROTEZIONE and self.is_on:
            print(f"[INFO] Variazione potenza ({watts}W) inferiore alla soglia di protezione ({POTENZA_PROTEZIONE}W). Nessun cambiamento.")
            return

        now = time.time()
        if self.last_update_time > 0 and (now - self.last_update_time < UPDATE_INTERVAL_S):
            return

        print(f"[AZIONE] CAMBIO POTENZA -> {watts} W")

        if self.send_command({'btn': f'P{watts}'}):
            self.current_set_power = watts
            self.last_update_time = now
        
        

    def turn_on(self):
        
        if not self.is_on:
            if self.time_turned_off > 0:
                tempo_trascorso = time.time() - self.time_turned_off
                if tempo_trascorso < COOLDOWN_ACCENSIONE:  #almeno 1 min
                    print(f"[INFO] Attesa cooldown: {COOLDOWN_ACCENSIONE - tempo_trascorso:.1f}s prima di accendere")
                    return
            
            print("[AZIONE] ACCENSIONE (ON)")
            self.set_power(MONOFASE_MIN_POWER if self.fase == 0 else TRIFASE_MIN_POWER) 

            if self.send_command({'btn': 'i'}):
                self.is_on = True
                self.last_update_time = time.time()
            
    

    def turn_off(self, force=False):
        
        now = time.time()
        if force and self.last_update_time != 0 and (now - self.last_update_time < UPDATE_INTERVAL_S):
            return

        if self.is_on or force:
            print("[AZIONE] SPEGNIMENTO (OFF)")
            if self.send_command({'btn': 'o'}):
                self.is_on = False
                self.time_turned_off = time.time() 
                self.last_update_time = time.time()
                time.sleep(0.5)
                self.send_command({'btn': f'P{MONOFASE_MIN_POWER if self.fase == 0 else TRIFASE_MIN_POWER}'})
                self.current_set_power = MONOFASE_MIN_POWER if self.fase == 0 else TRIFASE_MIN_POWER
            
            
        

    def initialize(self):
        print("\n=== INIZIALIZZAZIONE SISTEMA ===")

        try:
            print(f"Richiesta dati a {WALLBOX_URL}...")
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
                
                print("\n--------------------------------")
                print(f"TIPO IMPIANTO: {modalita}")
                print("--------------------------------")
                
            else:
                print(f"Errore. centralina codice: {response.status_code}")

        except requests.exceptions.RequestException as e:
            print(f"Errore di connessione: {e}")
        except json.JSONDecodeError:
            print("Errore: La risposta del server non è un JSON valido.")

        print("1. Metto in OFF (Attesa dati)...")
        self.last_update_time = 0 
        self.turn_off(force=True)
        
        if self.fase == 0:
            print("1. Imposto potenza minima (1380W)...")
            self.set_power(MONOFASE_MIN_POWER)
        elif self.fase == 1:
            print("1. Imposto potenza minima (4140)...")
            self.set_power(TRIFASE_MIN_POWER)

        time.sleep(1)
        print("=== PRONTO. IN ATTESA PACCHETTI ===\n")

# -----------------------------------------------------------
# MONITOR DATI 
# -----------------------------------------------------------
class EnergyMonitor:
    def __init__(self):
        self.solar_now = 0.0        
        self.total_grid_load = 0.0  
        self.fases = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        self.ctrletturefasi = 0

    def parse_packet(self, data):
        try:
            xml_str = data.decode('utf-8', errors='ignore')
            root = ET.fromstring(xml_str)
            
            if root.tag == 'electricity':#fasi
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
                    self.exporting = self.solar_now - self.total_grid_load
                    self.fases = [l1, l2, l3, l4, l5, l6]
                    
                    print("\n" + "="*60)
                    print(f" ⚡ DATO FASI")
                    print("-" * 60)
                    print(f" | RETE (Casa+WB) | L1: {l1:5.0f} | L2: {l2:5.0f} | L3: {l3:5.0f} | TOT: {self.total_grid_load:.0f}W")
                    print(f" | SOLARE (Inv)   | L4: {l4:5.0f} | L5: {l5:5.0f} | L6: {l6:5.0f} | TOT: {self.solar_now:.0f}W")
                    print("="*60)
                    print(f"")
                    self.ctrletturefasi += 1
                    
                    return "TRIGGER"


            elif root.tag == 'solar':#generata
                curr = root.find('current')
                if curr is not None:
                    gen = float(curr.find('generating').text)
                    self.solar_now = gen
                    return "TRIGGER"
                
        except Exception:
            pass
        return None

# -----------------------------------------------------------
# LOGICA DI CONTROLLO
# -----------------------------------------------------------
def run_logic(monitor, wallbox):
    potenza_generata = monitor.solar_now
    potenza_consumata = monitor.total_grid_load

    potenza_generata += POTENZA_PRELEVABILE
    potenza_esportata = potenza_generata - potenza_consumata
    potenza_carica = wallbox.current_set_power if wallbox.is_on else 0

    potenza_minima = MONOFASE_MIN_POWER if wallbox.fase == 0 else TRIFASE_MIN_POWER
    potenza_massima = MONOFASE_MAX_POWER if wallbox.fase == 0 else TRIFASE_MAX_POWER
    
    if potenza_consumata == 0:
        print("[INFO] Dati casa non ancora disponibili. Attendo...")
        return

    print(f"\n[INFO] Potenza Generata (+ prelevabile: {POTENZA_PRELEVABILE}W): {potenza_generata:.0f}W | Potenza Consumata: {potenza_consumata:.0f}W | Potenza Esportata: {potenza_esportata:.0f}W | Wallbox: {'ON' if wallbox.is_on else 'OFF'} ({wallbox.current_set_power:.0f}W)")
    #annullo timer spegnimento
    if wallbox.pending_off_until and potenza_generata >= potenza_minima:
        print("[INFO] Generazione ripristinata. Annullamento spegnimento programmato.")
        wallbox.pending_off_until = 0

    if not wallbox.is_on:
        if potenza_esportata > potenza_minima:
            print(f"[DECISIONE] Potenza esportata ({potenza_esportata:.0f}W) sufficiente per accendere. Accendo a {potenza_minima}W.")
            wallbox.turn_on()#accendo al minimo
        return

    if wallbox.is_on:
        if potenza_carica > potenza_generata or potenza_esportata < 0: #devo diminuire
            nuova_potenza = potenza_carica - abs(potenza_esportata)
            if nuova_potenza < potenza_minima or potenza_generata < potenza_minima:
                now = time.time()
                if not wallbox.pending_off_until:#guarso se c'è un timer
                    print(f"[DECISIONE] Sole insufficiente ({potenza_generata:.0f}W). Messo al minimo per {TIMER_SPEGNIMENTO}s prima di spegnere.")
                    wallbox.set_power(potenza_minima)
                    wallbox.pending_off_until = now + TIMER_SPEGNIMENTO
                else:
                    if now < wallbox.pending_off_until:#aggiorno timer
                        restante = wallbox.pending_off_until - now
                        print(f"[INFO] Attendo {restante:.0f}s prima dello spegnimento...")
                        return
                    wallbox.pending_off_until = 0
                    if potenza_generata < potenza_minima:#dopo 1 min non c'è ancora sole
                        print(f"[DECISIONE] Dopo attesa, sole ancora insufficiente ({potenza_generata:.0f}W). Spengo.")
                        try: 
                            asyncio.run(invia_notifica(f"⚠️ Attenzione! La potenza generata è insufficiente ({potenza_generata:.0f}W). Spengo il wallbox."))
                            if wallbox.fase == 1:
                                asyncio.run(invia_notifica(f"⚠️ Consiglio: mettere l'impianto in modalità monofase per sfruttare meglio la potenza disponibile."))
                            else:
                                asyncio.run(invia_notifica(f"⚠️ Consiglio: staccare la macchina"))
                        except Exception as e:
                            print(f"[ERRORE] Invio notifica fallito: {e}")
                        wallbox.turn_off(force=True)
                    else:#è tornato il sole siumm
                        print(f"[DECISIONE] Dopo attesa, generazione sufficiente. Mantengo acceso a {potenza_minima}W.")
                        wallbox.set_power(potenza_minima)
            else:
                #annullo spegnimento
                if wallbox.pending_off_until:
                    wallbox.pending_off_until = 0
                print(f"[DECISIONE] Diminuisco potenza a {nuova_potenza:.0f}W")
                wallbox.set_power(nuova_potenza)

        else: #posso aumentare
            if wallbox.pending_off_until:#se c'è un timer lo annullo
                print("[INFO] Annullamento spegnimento programmato (aumento potenza possibile).")
                wallbox.pending_off_until = 0
            nuova_potenza = potenza_carica + abs(potenza_esportata)
            if nuova_potenza > potenza_generata:
                return
            if nuova_potenza > potenza_massima:
                nuova_potenza = potenza_massima
            print(f"[DECISIONE] Aumento potenza a {nuova_potenza:.0f}W")
            wallbox.set_power(nuova_potenza)

async def invia_notifica(messaggio):
    bot = Bot(token=API_KEY)
    await bot.send_message(chat_id=CHAT_ID, text=messaggio)
    print(f"[INFO] Messaggio telegram ('{messaggio}') inviato correttamente!")

# -----------------------------------------------------------
# MAIN
# -----------------------------------------------------------
def main():
    try: 
        asyncio.run(invia_notifica(f"SISTEMA AVVIATO. Inizializzazione in corso..."))
    except Exception as e:
        print(f"[ERRORE] Invio notifica avvio fallito: {e}")
    
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