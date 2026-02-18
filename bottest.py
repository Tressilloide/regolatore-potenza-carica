import asyncio
from telegram import Bot

# Inserisci i tuoi dati qui
TOKEN = ''
CHAT_ID = '' # Può essere un numero (utente) o un numero negativo (gruppo)
SOGLIA_MASSIMA = 100

async def invia_notifica(valore):
    bot = Bot(token=TOKEN)
    messaggio = f"⚠️ Attenzione! La soglia è stata superata. Valore attuale: {valore}"
    await bot.send_message(chat_id=CHAT_ID, text=messaggio)
    print("Messaggio inviato correttamente!")

async def monitoraggio():
    print("Monitoraggio avviato...")
    
    # Esempio di logica di controllo
    valore_da_controllare = 120 # Questo valore solitamente arriverebbe da un sensore o un'API
    
    if valore_da_controllare > SOGLIA_MASSIMA:
        await invia_notifica(valore_da_controllare)
    else:
        print("Tutto nella norma.")

if __name__ == "__main__":
    asyncio.run(monitoraggio())