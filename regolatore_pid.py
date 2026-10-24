"""Regolatore PID della potenza di ricarica (sperimentale).

Modulo senza dipendenze (niente Flask, Telegram, requests): lo importano sia
solar_webinterface.py sia il simulatore, cosi' sul PC si prova esattamente la
stessa logica che gira sul Raspberry.

COSA MISURA IL SENSORE (OWL, multicast) - verificato sul campo:
  'solar'        ogni  6 s: produzione istantanea + 'exporting'
  'electricity'  ogni 60 s, 1 s dopo un 'solar': consumo e produzione
                 FASE PER FASE, istantanei

'exporting' NON e' lo scambio netto con la rete, per due motivi:
  1. e' la somma delle sole fasi che esportano, sum(max(0, sole_i - consumo_i)).
     Con la casa quasi tutta su una fase, quella importa mentre le altre
     esportano, ed 'exporting' sovrastima lo scambio anche di 1,5-2 kW. Il
     contatore invece fa la somma algebrica delle fasi;
  2. e' la MEDIA dei 6 s precedenti (coincide con l'incremento del contatore
     'exported' del pacchetto), non un valore istantaneo.
Lo scambio netto vero si conosce quindi solo ogni 60 s. Nei 6 s intermedi lo
si STIMA fase per fase:
  - dall'ultimo 'electricity' si ricava la casa su ogni fase;
  - a ogni 'solar' si ricostruisce il bilancio di ogni fase con il sole fresco
    e il setpoint dell'auto, e si PREVEDE 'exporting';
  - se 'exporting' si scosta dalla previsione PIU' di quanto si scostava
    subito dopo l'ultimo 'electricity', un carico si e' acceso o spento su una
    fase che esporta: la variazione si aggiunge alla stima.
Un carico che parte su una fase che gia' importa resta invisibile fino al
prossimo 'electricity': e' un limite del sensore, non della stima.

Il vecchio algoritmo usava "sole fresco - consumo vecchio", dove il consumo
vecchio conteneva ancora la potenza dell'auto PRIMA dell'ultimo comando: per
60 s ricorreggeva ogni 6 s lo stesso errore, e la potenza crollava al minimo
o saliva al massimo (oscillazione da integratore con misura ritardata).
"""


class PID:
    """PID in forma incrementale (velocity form).

    A ogni passo calcola la VARIAZIONE dell'uscita invece del valore assoluto.
    L'integrale e' l'uscita stessa: quando viene limitata (minimo o massimo
    della wallbox) non c'e' nulla che continui a crescere di nascosto, quindi
    niente windup, e si puo' riallineare l'uscita al valore reale in qualunque
    momento (accensione, comando manuale, cambio fase).
    """

    def __init__(self, kp, ki, kd=0.0, dt_nominale=6.0):
        self.kp, self.ki, self.kd = float(kp), float(ki), float(kd)
        self.dt_nominale = float(dt_nominale)
        self.uscita = 0.0
        self._e1 = None
        self._e2 = None
        self._t = None

    def reset(self, uscita):
        self.uscita = float(uscita)
        self._e1 = self._e2 = None
        self._t = None

    def passo(self, errore, t, minimo, massimo):
        """Ritorna (uscita limitata, uscita grezza prima del limite).

        La grezza serve al chiamante: se resta sotto il minimo vuol dire che
        anche alla potenza minima si importa, e si avvia il timer di spegnimento.
        """
        if self._t is None:
            dt = self.dt_nominale
        else:
            # Pacchetti persi: si limita dt, altrimenti l'integrale farebbe un
            # salto proporzionale al buco
            dt = min(max(t - self._t, 0.5), 2 * self.dt_nominale)
        e1 = errore if self._e1 is None else self._e1
        e2 = e1 if self._e2 is None else self._e2

        delta = (self.kp * (errore - e1)
                 + self.ki * dt * errore
                 + self.kd * (errore - 2 * e1 + e2) / dt)
        grezza = self.uscita + delta
        self.uscita = min(max(grezza, minimo), massimo)
        self._e2, self._e1, self._t = e1, errore, t
        return self.uscita, grezza


def quote_auto(potenza, trifase):
    """Potenza dell'auto su ciascuna fase.

    In monofase la fase della wallbox non e' nota: si spalma su tre. L'errore
    resta piccolo perche' la casa per fase viene ricavata con la STESSA
    ripartizione, quindi all'istante di ogni 'electricity' il modello riproduce
    esattamente le misure; si sbaglia solo sulle variazioni di potenza
    successive, fino al pacchetto dopo.
    """
    return [potenza / 3.0] * 3


class StimatoreScambio:
    """Stima lo scambio netto con la rete ogni 6 s (vedi docstring del modulo)."""

    # Sotto questa differenza fra setpoint e tetto misurato si considera che
    # l'auto stia seguendo: ritardi di salita e rumore della casa
    TOLLERANZA_TETTO = 300

    def __init__(self):
        self.casa_fasi = None           # consumo casa per fase (W), dall'ultimo 'electricity'
        self.quote_sole = [1 / 3.0] * 3
        self.tetto_auto = None          # massimo che l'auto puo' star assorbendo, se sotto il setpoint
        self.riferimento = None         # scarto exporting-previsione subito dopo l'ultimo 'electricity'
        self.correzione = 0.0           # ultima correzione affidabile
        self.sole_precedente = None
        self.t_fasi = None

    def pronto(self):
        return self.casa_fasi is not None

    def auto_modello(self, setpoint):
        """Potenza dell'auto da usare nel modello: il setpoint, a meno che le
        fasi non abbiano dimostrato che l'auto assorbe meno."""
        if self.tetto_auto is None:
            return setpoint
        return min(setpoint, self.tetto_auto)

    def aggiorna_fasi(self, consumi, soli, setpoint, trifase, t):
        """Da chiamare a ogni 'electricity'. `setpoint` e' quello in vigore
        all'istante del campione (vedi RegolatorePID.auto_al_campione).

        L'auto non sempre assorbe quanto concesso: ha un suo massimo, riduce a
        batteria quasi piena, si ferma quando e' carica, e dopo un comando ci
        mette qualche secondo a salire. La centralina non lo dice (pcar e'
        l'eco del setpoint), ma le fasi si': ognuna consuma almeno la quota
        dell'auto, quindi l'auto non puo' assorbire piu' di 3 x la fase piu'
        scarica (trifase) o della fase piu' carica (monofase).

        La casa per fase NON viene limitata a >= 0: e' cosi' che il modello
        riproduce esattamente le misure all'istante del campione.
        """
        tetto = 3 * min(consumi) if trifase else max(consumi)
        self.tetto_auto = tetto if setpoint - tetto > self.TOLLERANZA_TETTO else None
        quote = quote_auto(self.auto_modello(setpoint), trifase)
        self.casa_fasi = [c - a for c, a in zip(consumi, quote)]
        totale = sum(soli)
        if totale > 300:   # di notte le quote sarebbero solo rumore
            self.quote_sole = [s / totale for s in soli]
        # La casa e' appena stata misurata: le correzioni ripartono da zero, e
        # il riferimento si prende al primo 'solar' affidabile
        self.riferimento = None
        self.correzione = 0.0
        self.t_fasi = t

    def _bilanci(self, sole, setpoint, trifase):
        soli = [sole * q for q in self.quote_sole]
        auto = quote_auto(self.auto_modello(setpoint), trifase)
        return [s - h - a for s, h, a in zip(soli, self.casa_fasi, auto)]

    def scambio(self, sole, setpoint, trifase, exporting, affidabile):
        """Ritorna (scambio stimato, correzione da 'exporting'). Positivo = export.

        `affidabile` e' False se nei 6 s coperti da 'exporting' la potenza
        dell'auto e' cambiata: la media mescolerebbe prima e dopo il comando.
        Dopo un aumento farebbe credere che ci sia PIU' sole disponibile, cioe'
        una retroazione positiva che porta a importare. In quei passi si tiene
        l'ultima correzione affidabile.
        """
        netto = sum(self._bilanci(sole, setpoint, trifase))
        # 'exporting' e' la media dell'intervallo: la previsione si fa con il
        # sole medio (l'ultimo e il precedente), non con quello istantaneo
        sole_medio = sole if self.sole_precedente is None else (sole + self.sole_precedente) / 2
        self.sole_precedente = sole

        if exporting is not None and affidabile:
            previsto = sum(max(0.0, b) for b in self._bilanci(sole_medio, setpoint, trifase))
            scarto = exporting - previsto
            if self.riferimento is None:
                self.riferimento = scarto
            self.correzione = scarto - self.riferimento
        return netto + self.correzione, self.correzione


class RegolatorePID:
    """Decide accensione, potenza e spegnimento a ogni pacchetto 'solar'.

    Lavora su un oggetto wallbox con l'interfaccia di WallboxController:
    is_on, current_set_power, fase, limiti_potenza(), set_power(w, bypass),
    turn_on(), turn_off(force). Nel simulatore e' un finto controller.
    """

    # Quanto si concede sopra la potenza che l'auto sta davvero assorbendo,
    # quando assorbe meno del setpoint: abbastanza per accorgersi (al campione
    # successivo) se vuole di piu', poco per non importare se riparte di colpo
    MARGINE_TETTO = 1000

    def __init__(self, config, log=print):
        self.config = config
        self.log = log
        self.pid = PID(config['PID_KP'], config['PID_KI'], config['PID_KD'])
        self.stimatore = StimatoreScambio()
        # Potenza dell'auto quando e' stato preso l'ultimo campione 'solar',
        # PRIMA del comando eventualmente inviato in risposta: e' quella che il
        # successivo 'electricity' (stesso campione) contiene nei consumi.
        self.auto_al_campione = 0
        # Passi 'solar' dall'ultimo comando: 'exporting' e' affidabile solo se
        # l'auto non e' cambiata nei 6 s che copre (vedi StimatoreScambio)
        self.passi_da_comando = 99
        self.ultimo_inviato = None      # per accorgersi se qualcun altro cambia la potenza
        self.timer_spegnimento = None   # quando il PID ha iniziato a volere meno del minimo
        self.somma_finestra = 0.0       # scambio medio al minimo durante il timer
        self.campioni_finestra = 0
        self.ultima_stima = None        # per UI e diagnostica

    def aggiorna_guadagni(self):
        """Rilegge Kp/Ki/Kd dalla config senza perdere lo stato."""
        self.pid.kp = float(self.config['PID_KP'])
        self.pid.ki = float(self.config['PID_KI'])
        self.pid.kd = float(self.config['PID_KD'])

    def aggiorna_fasi(self, consumi, soli, trifase, t):
        self.stimatore.aggiorna_fasi(consumi, soli, self.auto_al_campione, trifase, t)

    def passo(self, wallbox, sole, exporting, t):
        self.passi_da_comando += 1
        prima = (wallbox.is_on, wallbox.current_set_power)
        try:
            self._passo(wallbox, sole, exporting, t)
        finally:
            if (wallbox.is_on, wallbox.current_set_power) != prima:
                # All'accensione l'auto ci mette qualche secondo a partire:
                # due intervalli non affidabili invece di uno
                self.passi_da_comando = -1 if wallbox.is_on and not prima[0] else 0

    def _passo(self, wallbox, sole, exporting, t):
        cfg = self.config
        trifase = wallbox.fase == 1
        auto = wallbox.current_set_power if wallbox.is_on else 0
        self.auto_al_campione = auto
        if not self.stimatore.pronto():
            return   # nessun 'electricity' ancora: senza la casa non si stima nulla

        scambio, correzione = self.stimatore.scambio(
            sole, auto, trifase, exporting, affidabile=self.passi_da_comando >= 1)
        disponibile = scambio + self.stimatore.auto_modello(auto)
        obiettivo = disponibile + cfg['POTENZA_PRELEVABILE'] - cfg['PID_MARGINE']
        minimo, massimo = wallbox.limiti_potenza()
        tetto = self.stimatore.tetto_auto
        if tetto is not None:
            # L'auto assorbe meno del concesso: alzare ancora il setpoint non
            # cambierebbe nulla adesso, ma se poi l'auto riprende (fine
            # bilanciamento celle, riscaldamento batteria) assorbirebbe di colpo
            # tutto il concesso e si importerebbe fino al prossimo campione.
            massimo = max(minimo, min(massimo, int(tetto + self.MARGINE_TETTO)))
        self.ultima_stima = {'scambio': round(scambio), 'correzione': round(correzione),
                             'disponibile': round(disponibile),
                             'tetto_auto': None if tetto is None else round(tetto), 't': t}

        # --- Spenta: si accende se il disponibile supera il minimo ---------
        if not wallbox.is_on:
            self.timer_spegnimento = None
            if obiettivo >= minimo and wallbox.turn_on():
                self.pid.reset(wallbox.current_set_power)
                self.ultimo_inviato = wallbox.current_set_power
                self.log(f"[PID] Disponibili {disponibile:.0f}W: acceso a {wallbox.current_set_power}W")
            return

        # Qualcuno ha cambiato la potenza al posto nostro (riavvio con carica
        # adottata, cambio fase, comando manuale): si riparte da li'.
        if self.ultimo_inviato != wallbox.current_set_power:
            self.pid.reset(wallbox.current_set_power)
            self.ultimo_inviato = wallbox.current_set_power

        # L'errore e' rispetto all'uscita INTERNA del PID, non al setpoint
        # inviato: con la banda morta i due differiscono, e usando quello
        # inviato l'integrale continuerebbe a crescere finche' supera la banda,
        # per poi oscillare attorno al valore giusto.
        errore = obiettivo - self.pid.uscita
        uscita, grezza = self.pid.passo(errore, t, minimo, massimo)

        # --- Spegnimento: il PID vuole meno del minimo -----------------------
        if grezza < minimo:
            if self.timer_spegnimento is None:
                self.timer_spegnimento = t
                self.somma_finestra, self.campioni_finestra = 0.0, 0
                self.log(f"[PID] Sole insufficiente ({disponibile:.0f}W disponibili): "
                         f"minimo, spengo tra {cfg['TIMER_SPEGNIMENTO']}s se continua")
            # scambio che si avrebbe alla potenza minima
            self.somma_finestra += obiettivo - minimo
            self.campioni_finestra += 1
            if t - self.timer_spegnimento >= cfg['TIMER_SPEGNIMENTO']:
                medio = self.somma_finestra / max(self.campioni_finestra, 1)
                self.timer_spegnimento = None
                if medio < -cfg['PID_IMPORT_SPEGNIMENTO'] or sole + cfg['POTENZA_PRELEVABILE'] < minimo:
                    self.log(f"[PID] Al minimo si importerebbero in media {-medio:.0f}W: spengo")
                    wallbox.turn_off(force=True)
                    self.ultimo_inviato = None
                    return
                self.log(f"[PID] Al minimo import medio {max(-medio, 0):.0f}W, sotto soglia: continuo")
        elif self.timer_spegnimento is not None:
            self.log(f"[PID] Sole tornato ({disponibile:.0f}W disponibili): annullo lo spegnimento")
            self.timer_spegnimento = None

        # --- Invio: banda morta ASIMMETRICA -----------------------------------
        # Si sale solo per variazioni oltre la soglia di protezione (meno
        # comandi), ma si scende subito: con una banda simmetrica di 300 W,
        # quando il sole cala si resta fino a 300 W in import prima di reagire,
        # e l'import costa mentre il sole non sfruttato no.
        richiesta = int(round(uscita))
        attuale = wallbox.current_set_power
        ai_limiti = richiesta in (minimo, massimo) and attuale != richiesta
        if (richiesta - attuale >= cfg['POTENZA_PROTEZIONE']
                or attuale - richiesta >= cfg['PID_BANDA_DISCESA'] or ai_limiti):
            wallbox.set_power(richiesta, bypass=True)
            self.ultimo_inviato = wallbox.current_set_power
            self.log(f"[PID] scambio {scambio:+.0f}W (corr {correzione:+.0f}) "
                     f"disp {disponibile:.0f}W: {attuale}W => {richiesta}W")
