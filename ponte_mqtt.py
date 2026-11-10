"""Pubblica i dati della ricarica (e del fotovoltaico visto dal Pi) su MQTT,
per Home Assistant e per l'archivio al minuto sul server di casa
(192.168.1.36).

Topic, tutti con QoS 1:
  solare/pi/online    'online' / 'offline' (retained, Last Will)
  solare/pi/stato     un JSON al minuto, subito dopo il pacchetto 'electricity'
                      del sensore (retained: HA lo ritrova dopo un suo riavvio)
  solare/pi/sessione  l'ultima sessione di ricarica chiusa (retained)
  solare/pi/controlli modalita', prelevabile, boost, obiettivo (retained), a
                      ogni cambio e ogni minuto
  solare/pi/cmd/<nome> comandi da Home Assistant (NON retained): modalita
                      (sole|misto|veloce), prelevabile (W), boost (minuti,
                      0 = annulla), obiettivo ('<kWh> <HH:MM>' o 'off')
  homeassistant/device/colonnina_pi/config
                      MQTT Discovery: HA crea da solo il dispositivo
                      "Colonnina" con i sensori di SENSORI

In Home Assistant vanno solo i dati della ricarica: sole e rete li pubblica il
demone owl-fotovoltaico sul server, che ascolta lo stesso sensore OWL e cosi'
la dashboard Energia funziona anche a Pi spento. Nel JSON al minuto restano
anche sole e rete come li vede il Pi, per l'archivio.

Con QoS 1 paho tiene in RAM i messaggi finche' il broker non risponde (fino a
CODA_MAX, una settimana di minuti) e li consegna alla riconnessione: un
riavvio del server o un buco del Wi-Fi non lasciano buchi nell'archivio.

Configurazione nel .env: MQTT_HOST, MQTT_PORT (1883), MQTT_USER, MQTT_PASSWORD.
Senza MQTT_HOST, o senza paho-mqtt, il modulo non fa nulla.
"""
import json

try:
    import paho.mqtt.client as mqtt
except ImportError:          # simulatore e prove sul PC
    mqtt = None

NODO = 'colonnina_pi'
T_ONLINE = 'solare/pi/online'
T_STATO = 'solare/pi/stato'
T_SESSIONE = 'solare/pi/sessione'
T_CONTROLLI = 'solare/pi/controlli'
T_COMANDI = 'solare/pi/cmd/'
COMANDI = ('modalita', 'prelevabile', 'boost', 'obiettivo')
T_DISCOVERY = f'homeassistant/device/{NODO}/config'
CODA_MAX = 7 * 24 * 60
# Senza un nuovo 'stato' per 5 minuti i sensori diventano "non disponibili":
# meglio un buco nei grafici che un valore vecchio spacciato per attuale.
SCADENZA_S = 300

POTENZA = {'device_class': 'power', 'unit_of_measurement': 'W', 'state_class': 'measurement'}
# total_increasing: HA riconosce da solo l'azzeramento di mezzanotte e somma
# correttamente nelle statistiche a lungo termine (conservate per sempre).
ENERGIA = {'device_class': 'energy', 'unit_of_measurement': 'kWh',
           'state_class': 'total_increasing', 'suggested_display_precision': 1}


def _sensore(chiave, nome, campo, **opzioni):
    return {'p': 'sensor', 'name': nome, 'unique_id': f'{NODO}_{chiave}',
            'value_template': f'{{{{ value_json.{campo} }}}}',
            'expire_after': SCADENZA_S, **opzioni}


# Gli entity_id vengono dal nome: sensor.colonnina_in_auto_oggi, ...
SENSORI = {
    'potenza': _sensore('potenza', 'Potenza', 'auto_w', icon='mdi:car-electric', **POTENZA),
    'stato': _sensore('stato', 'Stato', 'colonnina', icon='mdi:ev-station'),
    'algoritmo': _sensore('algoritmo', 'Algoritmo', 'algoritmo', icon='mdi:tune',
                          entity_category='diagnostic'),
    # in auto oggi (la dashboard Energia lo usa come consumo dell'auto)
    'auto_oggi': _sensore('auto_oggi', 'In auto oggi', 'oggi_auto_kwh',
                          icon='mdi:car-electric', **ENERGIA),
    'auto_sole_oggi': _sensore('auto_sole_oggi', 'In auto dal sole oggi', 'oggi_auto_sole_kwh',
                               icon='mdi:solar-power-variant', **ENERGIA),
    'minuti_carica_oggi': _sensore('minuti_carica_oggi', 'Minuti di carica oggi',
                                   'oggi_minuti_carica', icon='mdi:timer-outline',
                                   device_class='duration', unit_of_measurement='min',
                                   state_class='total_increasing'),
    # --- Controlli: Home Assistant comanda la carica ---------------------
    # Lo stato viene da T_CONTROLLI, pubblicato subito dopo ogni comando:
    # quello al minuto farebbe tornare indietro il selettore per un minuto.
    'modalita': {'p': 'select', 'name': 'Modalità', 'unique_id': f'{NODO}_modalita',
                 'icon': 'mdi:car-cog', 'options': ['sole', 'misto', 'veloce'],
                 'command_topic': T_COMANDI + 'modalita', 'state_topic': T_CONTROLLI,
                 'value_template': '{{ value_json.modalita }}'},
    'prelevabile': {'p': 'number', 'name': 'Prelevabile per l\'auto',
                    'unique_id': f'{NODO}_prelevabile', 'icon': 'mdi:transmission-tower-import',
                    'min': 0, 'max': 6600, 'step': 100, 'mode': 'box',
                    'unit_of_measurement': 'W', 'device_class': 'power',
                    'command_topic': T_COMANDI + 'prelevabile', 'state_topic': T_CONTROLLI,
                    'value_template': '{{ value_json.prelevabile_w }}'},
    'boost': {'p': 'button', 'name': 'Boost 1 ora', 'unique_id': f'{NODO}_boost',
              'icon': 'mdi:lightning-bolt', 'command_topic': T_COMANDI + 'boost',
              'payload_press': '60'},
    'boost_annulla': {'p': 'button', 'name': 'Annulla boost', 'unique_id': f'{NODO}_boost_annulla',
                      'icon': 'mdi:lightning-bolt-outline', 'command_topic': T_COMANDI + 'boost',
                      'payload_press': '0'},
    'modalita_attiva': {'p': 'sensor', 'name': 'Modalità attiva', 'unique_id': f'{NODO}_modalita_attiva',
                        'icon': 'mdi:car-clock', 'state_topic': T_CONTROLLI,
                        'value_template': '{{ value_json.modalita_effettiva }}',
                        'json_attributes_topic': T_CONTROLLI},
    'obiettivo': {'p': 'sensor', 'name': 'Obiettivo caricato', 'unique_id': f'{NODO}_obiettivo',
                  'icon': 'mdi:target', 'state_topic': T_CONTROLLI, 'unit_of_measurement': 'kWh',
                  'value_template': '{{ value_json.obiettivo.fatti if value_json.obiettivo else 0 }}'},
    'ultima_sessione': {'p': 'sensor', 'name': 'Ultima sessione', 'unique_id': f'{NODO}_ultima_sessione',
                        'state_topic': T_SESSIONE, 'value_template': '{{ value_json.kwh }}',
                        'json_attributes_topic': T_SESSIONE, 'icon': 'mdi:ev-plug-type2',
                        'device_class': 'energy', 'unit_of_measurement': 'kWh',
                        'suggested_display_precision': 1},
}


class PonteMQTT:
    def __init__(self, host, porta=1883, utente=None, password=None, log=print):
        self.log = log
        self.attivo = mqtt is not None and bool(host)
        self._soli = []              # produzione dei pacchetti 'solar' dell'ultimo minuto
        self._errore_segnalato = False
        self._chiuso = False
        self._comandi = None         # funzione(nome, valore) -> (ok, messaggio)
        self._ultimi_controlli = None
        if not self.attivo:
            if host and mqtt is None:
                log("[MQTT] paho-mqtt non installato: niente dati verso Home Assistant")
            return
        self.host = host
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='solare-pi')
        if utente:
            c.username_pw_set(utente, password)
        c.will_set(T_ONLINE, 'offline', qos=1, retain=True)
        c.max_queued_messages_set(CODA_MAX)
        c.reconnect_delay_set(5, 120)
        c.on_connect = self._connesso
        c.on_connect_fail = self._connessione_fallita
        c.on_disconnect = self._disconnesso
        c.on_message = self._messaggio
        # Asincrono: se il server e' spento il regolatore parte lo stesso, e
        # il thread di paho continua a riprovare da solo.
        c.connect_async(host, int(porta), keepalive=60)
        c.loop_start()
        self.client = c

    def _connesso(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            self._segnala(f"[MQTT] {self.host} ha rifiutato la connessione: {reason_code}")
            return
        self._errore_segnalato = False
        self.log(f"[MQTT] Connesso a {self.host}")
        configurazione = {
            'dev': {'ids': [NODO], 'name': 'Colonnina', 'mf': 'regolatore-potenza-carica',
                    'mdl': 'Raspberry Pi e colonnina EVR'},
            'o': {'name': 'regolatore-potenza-carica'},
            'state_topic': T_STATO,
            'availability_topic': T_ONLINE,
            'qos': 1,
            'cmps': SENSORI,
        }
        client.publish(T_DISCOVERY, json.dumps(configurazione), qos=1, retain=True)
        client.publish(T_ONLINE, 'online', qos=1, retain=True)
        if self._ultimi_controlli is not None:
            self._pubblica(T_CONTROLLI, self._ultimi_controlli)
        # Ad ogni (ri)connessione: la sottoscrizione non sopravvive a una
        # sessione pulita del broker
        client.subscribe(T_COMANDI + '#', qos=1)

    def imposta_comandi(self, funzione):
        """`funzione(nome, valore)` esegue un comando arrivato da Home
        Assistant e ritorna (ok, messaggio)."""
        self._comandi = funzione

    def _messaggio(self, client, userdata, msg):
        """Comandi da Home Assistant. Gira nel thread di paho: non deve mai
        sollevare eccezioni, ne' bloccarsi a lungo."""
        try:
            nome = msg.topic[len(T_COMANDI):] if msg.topic.startswith(T_COMANDI) else None
            if msg.retain:
                # Un comando retained verrebbe rieseguito a ogni riconnessione
                self.log(f"[MQTT] Comando retained su {msg.topic} ignorato")
                return
            if nome not in COMANDI or self._comandi is None:
                return
            valore = msg.payload.decode('utf-8', errors='replace')[:64]
            ok, messaggio = self._comandi(nome, valore)
            self.log(f"[MQTT] Comando {nome}={valore!r}: {messaggio}")
        except Exception as e:
            self.log(f"[MQTT] Comando su {msg.topic} non eseguito: {e}")

    def pubblica_controlli(self, stato):
        """Stato dei controlli: subito dopo un comando e ogni minuto (retained)."""
        if not self.attivo:
            return
        self._ultimi_controlli = stato
        self._pubblica(T_CONTROLLI, stato)

    def _connessione_fallita(self, client, userdata):
        self._segnala(f"[MQTT] {self.host} non raggiungibile: riprovo, i dati restano in coda")

    def _disconnesso(self, client, userdata, flags, reason_code, properties):
        if self._chiuso:
            return     # l'abbiamo chiesta noi all'arresto: non si riprova
        self._segnala(f"[MQTT] Disconnesso da {self.host} ({reason_code}): riprovo, i dati restano in coda")

    def _segnala(self, messaggio):
        # Una riga per interruzione, non una per tentativo (fino a ogni 5 s)
        if not self._errore_segnalato:
            self._errore_segnalato = True
            self.log(messaggio)

    def campione_solare(self, watt):
        if self.attivo:
            self._soli.append(watt)

    def pubblica_minuto(self, dati):
        """dati: il record del minuto, vedi dati_minuto() in solar_webinterface.py.
        Si aggiungono media, minimo e massimo della produzione nel minuto: col
        sole variabile dicono quanto ha dovuto inseguire il regolatore."""
        if not self.attivo:
            return
        soli, self._soli = self._soli, []
        if soli:
            dati['produzione_media_w'] = round(sum(soli) / len(soli))
            dati['produzione_min_w'] = round(min(soli))
            dati['produzione_max_w'] = round(max(soli))
        self._pubblica(T_STATO, dati)

    def pubblica_sessione(self, sessione):
        if self.attivo:
            self._pubblica(T_SESSIONE, sessione)

    def _pubblica(self, topic, dati):
        try:
            info = self.client.publish(topic, json.dumps(dati, separators=(',', ':')), qos=1, retain=True)
            if info.rc == mqtt.MQTT_ERR_QUEUE_SIZE:
                self._segnala(f"[MQTT] Coda piena ({CODA_MAX} messaggi): scarto i dati nuovi")
        except Exception as e:
            self.log(f"[MQTT] Pubblicazione su {topic} fallita: {e}")

    def chiudi(self):
        """All'arresto: 'offline' subito, invece di aspettare che il broker
        si accorga della connessione caduta (fino a 90 s)."""
        if not self.attivo:
            return
        try:
            self.client.publish(T_ONLINE, 'offline', qos=1, retain=True).wait_for_publish(2)
        except Exception:
            pass   # broker gia' irraggiungibile: ci pensera' il Last Will
        self._chiuso = True
        self.client.disconnect()
        self.client.loop_stop()
