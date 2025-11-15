import requests

# IP del dispositivo
BASE_URL = "http://192.168.1.22"

def set_power(value, base_url=BASE_URL, timeout=3):
    """
    Imposta la potenza sul dispositivo.

    Args:
        value (int | str): valore intero (solo cifre) della potenza in Watt.
        base_url (str): URL base del dispositivo.
        timeout (float): timeout in secondi per la richiesta HTTP.

    Returns:
        dict: {
            "success": bool,
            "url": str,
            "status_code": int | None,
            "text": str | None,
            "error": str | None
        }

    Raises:
        ValueError: se value non è un intero non negativo in stringa o int.
    """
    val_str = str(value)

    # Controllo rapido
    if not val_str.isdigit():
        raise ValueError("Devi inserire un numero intero non negativo (solo cifre).")

    url = f"{base_url}/index.json?btn=P{val_str}"

    try:
        r = requests.get(url, timeout=timeout)
        if r.status_code == 200:
            return {"success": True, "url": url, "status_code": r.status_code, "text": r.text, "error": None}
        else:
            return {"success": False, "url": url, "status_code": r.status_code, "text": r.text, "error": None}
    except requests.exceptions.RequestException as e:
        return {"success": False, "url": url, "status_code": None, "text": None, "error": str(e)}

if __name__ == "__main__":
    vmin = 1380
    vmax = 7360

    try: 
        result = set_power(vmin)
    except ValueError as ve:
        print("Errore:", ve)

    valore = input("Inserisci il valore della potenza in Watt: ")
    try:
        result = set_power(valore)
        if result["success"]:
            print("✓ Valore inviato correttamente!")
            print("Risposta del dispositivo:", result["text"])
        elif result["error"]:
            print("Errore di comunicazione:", result["error"])
        else:
            print("Risposta inaspettata:", result["status_code"], result["text"])
    except ValueError as ve:
        print("Errore:", ve)







