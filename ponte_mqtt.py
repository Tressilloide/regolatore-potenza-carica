"""Pubblica i dati della ricarica (e del fotovoltaico visto dal Pi) su MQTT,
per Home Assistant e per l'archivio al minuto sul server di casa
(192.168.1.36).

Topic, tutti con QoS 1:
  solare/pi/online    'online' / 'offline' (retained, Last Will)
  solare/pi/stato     un JSON al minuto, subito dopo il pacchetto 'electricity'
                      del sensore (retained: HA lo ritrova dopo un suo riavvio)
  solare/pi/sessione  l'ultima sessione di ricarica chiusa (retained)
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

    def _connessione_fallita(self, client, userdata):
        self._segnala(f"[MQTT] {self.host} non raggiungibile: riprovo, i dati restano in coda")

    def _disconnesso(self, client, userdata, flags, reason_code, properties):
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
        self.client.disconnect()
        self.client.loop_stop()
