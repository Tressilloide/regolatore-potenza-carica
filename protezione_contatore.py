"""Limite del contatore di casa: non prelevare mai dalla rete piu' di quanto
il contatore regge (LIMITE_CONTATORE, 6600 W: 6 kW di contratto + 10%).

Modulo senza dipendenze, come regolatore_pid.py: lo usano sia
solar_webinterface.py sia il simulatore.

Due livelli:
1. prelevabile_effettivo(): il prelievo totale (casa + auto) che i regolatori,
   classico e PID, possono puntare. POTENZA_PRELEVABILE e' quanto l'AUTO puo'
   prendere dalla rete, in aggiunta a quello che preleva gia' la casa (prima
   era il totale, casa compresa: con 3 kW e la casa a 2,7 kW all'auto restavano
   300 W e la sera del 3/11 non e' partita). Il totale pero' resta sotto
   LIMITE_CONTATORE - MARGINE_CONTATORE: se la casa sale, e' l'auto a cedere.
2. ProtezioneContatore: se il prelievo supera comunque LIMITE_CONTATORE (un
   carico acceso in casa, una nuvola), riduce subito l'auto o la spegne, senza
   i filtri e i timer dei regolatori (al minimo il classico aspettava 60 s).

Il consumo per fase arriva ogni 60 s: un carico che si accende si vede al
massimo un minuto dopo, ed e' allora che si interviene. Il contatore non
stacca all'istante: secondo i manuali dei contatori elettronici, oltre la
potenza disponibile stacca in circa 2 minuti se la supera di oltre il 27%,
altrimenti dopo circa 3 ore.
"""


def limite_regolazione(config):
    """Prelievo totale massimo a cui si regola, o None se il limite e' spento."""
    limite = config.get('LIMITE_CONTATORE') or 0
    if limite <= 0:
        return None
    return limite - config.get('MARGINE_CONTATORE', 0)


def prelevabile_effettivo(config, deficit_casa):
    """Prelievo totale (casa + auto) che i regolatori possono puntare, in W.

    `deficit_casa` = casa - sole: quanto la casa preleverebbe senza l'auto
    (negativo se il sole le basta). POTENZA_PRELEVABILE si aggiunge a quello,
    perche' e' la quota dell'auto; il totale resta entro il limite del contatore.
    """
    prelevabile = config['POTENZA_PRELEVABILE'] + max(0.0, deficit_casa)
    tetto = limite_regolazione(config)
    if tetto is not None:
        prelevabile = min(prelevabile, tetto)
    return prelevabile


def stima_auto(consumi, setpoint, trifase, fase_monofase=0):
    """Potenza dell'auto all'istante del campione delle fasi.

    Il setpoint, ma non piu' di quanto le fasi permettono: ogni fase consuma
    almeno la sua quota dell'auto. Conta quando l'auto e' al suo massimo
    (11 kW con 22 kW di setpoint): abbassare il setpoint da 22 a 20 kW non
    cambierebbe nulla.
    """
    if setpoint <= 0:
        return 0.0
    tetto = 3 * min(consumi) if trifase else consumi[fase_monofase]
    return max(0.0, min(float(setpoint), tetto))


class ProtezioneContatore:
    """Riduce o spegne l'auto quando il prelievo supera LIMITE_CONTATORE.

    Il prelievo si conosce davvero solo a ogni 'electricity' (ogni 60 s). Ai
    'solar' intermedi (ogni 6 s di giorno) lo si stima con la casa
    dell'ultimo 'electricity', il sole fresco e l'auto corretta per i comandi
    inviati dopo: cosi' si vede subito una nuvola, e dopo un intervento non si
    riinterviene sullo stesso campione vecchio.

    Dopo un intervento, fino al campione delle fasi successivo, la wallbox
    non accetta setpoint piu' alti (wallbox.tetto_contatore): i regolatori
    lavorano su un consumo vecchio fino a 60 s, che contiene ancora l'auto di
    prima, e il classico la rialzava 6 s dopo (visto nel simulatore: si
    alternavano ogni 12 s fino al campione successivo).

    Lavora su un oggetto wallbox con l'interfaccia di WallboxController:
    is_on, current_set_power, tetto_contatore, limiti_potenza(),
    set_power(w, bypass), turn_off(force, urgente).
    """

    def __init__(self, config, log=print, avvisa=None):
        self.config = config
        self.log = log
        self.avvisa = avvisa or (lambda messaggio: None)
        self.casa = None        # consumo senza auto all'ultimo 'electricity' (W)
        self.auto = 0.0         # potenza stimata dell'auto in quell'istante
        self.setpoint = 0       # setpoint in quell'istante (0 se spenta)
        self.campione_nuovo = False
        self.interventi = 0

    def aggiorna_fasi(self, consumi, setpoint, trifase):
        """A ogni 'electricity'. `consumi`: consumo per fase, auto compresa;
        `setpoint`: quello in vigore, 0 se la wallbox e' spenta."""
        fase = self.config.get('PID_FASE_MONOFASE', 0)
        self.auto = stima_auto(consumi, setpoint, trifase, fase)
        self.casa = sum(consumi) - self.auto
        self.setpoint = setpoint
        self.campione_nuovo = True

    def auto_adesso(self, setpoint):
        """L'auto non supera il setpoint attuale, e dalla misura e' salita al
        massimo di quanto e' salito il setpoint."""
        if setpoint <= 0:
            return 0.0
        return max(0.0, min(float(setpoint), self.auto + max(0, setpoint - self.setpoint)))

    def prelievo_stimato(self, sole, setpoint):
        """(prelievo dalla rete, auto) adesso. Al pacchetto 'electricity' il
        prelievo e' esattamente consumo - produzione misurati."""
        auto = self.auto_adesso(setpoint)
        return self.casa + auto - sole, auto

    def controlla(self, wallbox, sole):
        """Da chiamare a ogni pacchetto, prima del regolatore. Ritorna True se
        e' intervenuta: il regolatore salta il pacchetto, che e' quello appena
        corretto."""
        if self.campione_nuovo:
            # Misura fresca: i regolatori tornano liberi (entro il loro tetto)
            wallbox.tetto_contatore = None
            self.campione_nuovo = False
        limite = self.config.get('LIMITE_CONTATORE') or 0
        if limite <= 0 or self.casa is None or not wallbox.is_on:
            return False
        prelievo, auto = self.prelievo_stimato(sole, wallbox.current_set_power)
        if prelievo <= limite:
            return False

        obiettivo = limite_regolazione(self.config)
        nuova = int(auto - (prelievo - obiettivo))
        minimo, _ = wallbox.limiti_potenza()
        if nuova < minimo and prelievo - auto + minimo <= limite:
            # Al minimo si resta sotto il limite, anche se sopra l'obiettivo:
            # se spegnere lo decide il regolatore, con il suo timer
            nuova = minimo
        self.interventi += 1
        if nuova >= minimo:
            wallbox.tetto_contatore = None
            wallbox.set_power(nuova, bypass=True)
            wallbox.tetto_contatore = nuova
            esito = f"auto da {auto:.0f} a {nuova} W"
        else:
            wallbox.turn_off(force=True, urgente=True)
            esito = f"anche al minimo ({minimo} W) si supererebbe: auto spenta"
        self.log(f"[CONTATORE] Prelievo {prelievo:.0f}W oltre il limite di {limite}W "
                 f"(casa {self.casa:.0f}W, sole {sole:.0f}W): {esito}")
        self.avvisa(f"⚡ Prelievo dalla rete {prelievo / 1000:.1f} kW, oltre il limite del "
                    f"contatore ({limite / 1000:.1f} kW): {esito}.")
        return True
