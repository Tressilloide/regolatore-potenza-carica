from flask import Flask, render_template, request, jsonify
import threading
import time

app = Flask(__name__)

# Memoria condivisa (in un'app reale useresti un database)
app_state = {
    'valore_base_1': 0,
    'valore_base_2': 0,
    'output_corrente': 0,
    'messaggio': "In attesa di input...",
    'running': False
}

def task_in_background(v1, v2):
    """
    Simula un processo lungo che aggiorna i dati nel tempo.
    Esempio: un contatore che usa i due valori come moltiplicatori.
    """
    app_state['running'] = True
    app_state['messaggio'] = "Calcolo in corso..."
    
    # Esempio: facciamo 10 step di aggiornamento
    for i in range(1, 101):
        if not app_state['running']: break
        
        # Logica di esempio: (v1 + v2) * progresso
        calcolo = (float(v1) + float(v2)) * i
        
        # AGGIORNIAMO LO STATO
        app_state['output_corrente'] = calcolo
        app_state['messaggio'] = f"Elaborazione al {i}%"
        
        time.sleep(0.1) # Simula tempo di calcolo
        
    app_state['messaggio'] = "Calcolo completato!"
    app_state['running'] = False

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/start', methods=['POST'])
def start_process():
    # Prende i dati dal form web (JSON)
    data = request.json
    val1 = data.get('val1')
    val2 = data.get('val2')
    
    # Aggiorna lo stato iniziale
    app_state['valore_base_1'] = val1
    app_state['valore_base_2'] = val2
    
    # Avvia il thread separato per non bloccare la pagina web
    thread = threading.Thread(target=task_in_background, args=(val1, val2))
    thread.daemon = True
    thread.start()
    
    return jsonify({'status': 'ok', 'message': 'Processo avviato'})

@app.route('/get_status', methods=['GET'])
def get_status():
    # Questa rotta viene chiamata ripetutamente dal frontend
    return jsonify(app_state)

if __name__ == '__main__':
    app.run(debug=True)