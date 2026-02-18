import requests
import json

url = "http://192.168.1.22/index.json"

try:
    print(f"Richiesta dati a {url}...")
    response = requests.get(url, timeout=5)

    if response.status_code == 200:
        # Decodifica il JSON
        dati = response.json()

        # --- LOGICA RICHIESTA PER TFASE ---
        # Prendo il valore di 'tfase'. Uso .get() per evitare errori se il campo manca.
        valore_fase = dati.get("tfase")

        if valore_fase == "1":
            modalita = "TRIFASE"
        else:
            # Se è "0" o qualsiasi altra cosa, lo consideriamo Monofase
            modalita = "MONOFASE"
        
        print("\n--------------------------------")
        print(f"TIPO IMPIANTO: {modalita}")
        print("--------------------------------")

        
        
    else:
        print(f"Errore. Il server ha risposto con codice: {response.status_code}")

except requests.exceptions.RequestException as e:
    print(f"Errore di connessione: {e}")
except json.JSONDecodeError:
    print("Errore: La risposta del server non è un JSON valido.")