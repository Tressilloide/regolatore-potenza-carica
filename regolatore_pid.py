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


def quote_auto(potenza, trifase, fase_monofase=0):
    """Potenza dell'auto su ciascuna fase.

    In monofase sta tutta sulla fase della wallbox (L1, misurato: con setpoint
    7360 W la L1 segnava 7116 W e le altre non si muovevano). Spalmarla su tre
    faceva vedere al modello effetti su fasi che l'auto non tocca: a ogni
    discesa del setpoint prevedeva piu' export sulle fasi libere, l'export vero
    non cambiava, la correzione diventava negativa e il PID scendeva ancora
    (visto sul campo: da 5,2 a 2,3 kW in un minuto). In salita lo stesso
    meccanismo porterebbe a importare.
    """
    if trifase:
        return [potenza / 3.0] * 3
    quote = [0.0, 0.0, 0.0]
    quote[fase_monofase] = float(potenza)
    return quote


class StimatoreScambio:
    """Stima lo scambio netto con la rete ogni 6 s (vedi docstring del modulo)."""

    # Sotto questa differenza fra setpoint e tetto misurato si considera che
    # l'auto stia seguendo. Non va tenuta stretta: sul campo l'auto assorbe
    # stabilmente il 10-15% meno del setpoint (~1 kW a 7 kW). Quello scarto lo
    # assorbe gia' la casa per fase (che viene negativa); trattarlo da tetto
    # faceva credere al modello che alzare il setpoint non cambiasse nulla,
    # mentre l'auto saliva davvero: la correzione da 'exporting' oscillava di
    # +-600 W e il setpoint con lei. Il tetto serve per i casi grossi: auto
    # carica, o limitata ben sotto quanto concesso.
    TOLLERANZA_TETTO = 1500

    def __init__(self, fase_monofase=0):
        self.fase_monofase = fase_monofase
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
        scarica (trifase) o della sua fase (monofase).

        La casa per fase NON viene limitata a >= 0: e' cosi' che il modello
        riproduce esattamente le misure all'istante del campione.
        """
        tetto = 3 * min(consumi) if trifase else consumi[self.fase_monofase]
        self.tetto_auto = tetto if setpoint - tetto > self.TOLLERANZA_TETTO else None
        quote = quote_auto(self.auto_modello(setpoint), trifase, self.fase_monofase)
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
        auto = quote_auto(self.auto_modello(setpoint), trifase, self.fase_monofase)
        return [s - h - a for s, h, a in zip(soli, self.casa_fasi, auto)]

    def scambio(self, sole, setpoint, trifase, exporting, affidabile):
        """Ritorna (scambio stimato, correzione da 'exporting'). Positivo = export.

        `affidabile` e' False se nei 6 s coperti da 'exporting' la potenza
        dell'auto e' cambiata: la media mescolerebbe prima e dopo il comando.
        Dopo un aumento farebbe credere che ci sia PIU' sole disponibile, cioe'
        una retroazione positiva che porta a importare. In quei passi si tiene
        l'ultima correzione affidabile.
        """
        # La produzione istantanea alterna +-500 W da un campione all'altro
        # (8355, 9449, 8677, 9369...): presa cosi' com'e', il PID la inseguiva
        # cambiando potenza a ogni pacchetto. La media degli ultimi due elimina
        # l'alternanza ed e' anche coerente con 'exporting', che e' una media.
        sole_medio = sole if self.sole_precedente is None else (sole + self.sole_precedente) / 2
        self.sole_precedente = sole
        netto = sum(self._bilanci(sole_medio, setpoint, trifase))

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
    # Salita massima per pacchetto (6 s): rete di sicurezza contro gli errori
    # di stima. Sul campo, con l'auto appena fermata dalla centralina, l'export
    # improvviso e' sembrato sole libero e il PID e' salito da 5 a 10 kW in
    # 30 s; se l'auto fosse ripartita lassu' si sarebbe importato parecchio.
    SALITA_MAX = 1000

    def __init__(self, config, log=print):
        self.config = config
        self.log = log
        self.pid = PID(config['PID_KP'], config['PID_KI'], config['PID_KD'])
        self.stimatore = StimatoreScambio(config.get('PID_FASE_MONOFASE', 0))
        # Potenza dell'auto quando e' stato preso l'ultimo campione 'solar',
        # PRIMA del comando eventualmente inviato in risposta: e' quella che il
        # successivo 'electricity' (stesso campione) contiene nei consumi.
        self.auto_al_campione = 0
        # Passi 'solar' dall'ultimo comando: 'exporting' e' affidabile solo se
        # l'auto non e' cambiata nei 6 s che copre (vedi StimatoreScambio)
        self.passi_da_comando = 99
        self._stato_osservato = None
        # Wallbox accesa alla fine dell'ultimo passo: se al passo dopo e' accesa
        # e non l'abbiamo accesa noi, il controller ha adottato una carica
        self.acceso = False
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

    def osserva(self, wallbox, sole):
        """A ogni 'solar' quando comanda il classico: tiene aggiornato lo stato
        che passo() userebbe, cosi' al passaggio al PID la media della
        produzione e l'affidabilita' di 'exporting' sono gia' giuste. Senza,
        il primo passo usava una produzione vecchia di ore e prendeva come
        riferimento un 'exporting' a cavallo di un comando del classico."""
        if self._sole_impossibile(sole):
            return
        stato = (wallbox.is_on, wallbox.current_set_power)
        # Il comando del classico parte dopo questa chiamata: lo si vede al
        # pacchetto successivo, che e' gia' il primo con 'exporting' sporco
        self.passi_da_comando = 1 if stato != self._stato_osservato else self.passi_da_comando + 1
        self._stato_osservato = stato
        self.acceso = wallbox.is_on
        self.auto_al_campione = wallbox.current_set_power if wallbox.is_on else 0
        self.stimatore.sole_precedente = sole

    def _sole_impossibile(self, sole):
        """La produzione non passa da oltre 1 kW a zero in 6 s: succede solo
        quando il sensore OWL si riavvia (visto sul campo, insieme a un
        timestamp di 9 secondi). Preso per buono, faceva crollare la stima."""
        precedente = self.stimatore.sole_precedente
        if sole < 50 and precedente is not None and precedente > 1000:
            self.log(f"[PID] Produzione {sole:.0f}W subito dopo {precedente:.0f}W: "
                     f"pacchetto scartato (sensore che si riavvia?)")
            return True
        return False

    def _apri_finestra(self, t):
        self.timer_spegnimento = t
        self.somma_finestra, self.campioni_finestra = 0.0, 0

    def passo(self, wallbox, sole, exporting, t):
        if self._sole_impossibile(sole):
            return
        if wallbox.is_on and not self.acceso:
            # Accesa senza di noi: carica gia' in corso adottata (colonnina
            # riaccesa, riavvio del servizio). L'ultimo 'electricity' e' stato
            # letto con l'auto a 0 nel modello, quindi la casa per fase contiene
            # anche l'auto: ora che e' nel modello verrebbe contata due volte, il
            # disponibile crollerebbe di 4 kW e partirebbe il timer di
            # spegnimento. Si aspetta il prossimo 'electricity'.
            self.stimatore.casa_fasi = None
            self.log(f"[PID] Carica in corso adottata a {wallbox.current_set_power}W: "
                     f"attendo il prossimo campione delle fasi per regolare")
        self.passi_da_comando += 1
        prima = (wallbox.is_on, wallbox.current_set_power)
        try:
            self._passo(wallbox, sole, exporting, t)
        finally:
            if (wallbox.is_on, wallbox.current_set_power) != prima:
                # All'accensione l'auto ci mette qualche secondo a partire:
                # due intervalli non affidabili invece di uno
                self.passi_da_comando = -1 if wallbox.is_on and not prima[0] else 0
            self.acceso = wallbox.is_on

    def _passo(self, wallbox, sole, exporting, t):
        cfg = self.config
        trifase = wallbox.fase == 1
        auto = wallbox.current_set_power if wallbox.is_on else 0
        self.auto_al_campione = auto
        if not self.stimatore.pronto():
            return   # nessun 'electricity' ancora: senza la casa non si stima nulla

        # 'exporting' copre i 6 s PRIMA di questo pacchetto: se il comando e'
        # partito al pacchetto precedente, quell'intervallo contiene la salita
        # o la discesa dell'auto. Serve un intervallo intero senza comandi.
        scambio, correzione = self.stimatore.scambio(
            sole, auto, trifase, exporting, affidabile=self.passi_da_comando >= 2)
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
        if wallbox.is_on:
            salita = self.SALITA_MAX
            if not getattr(wallbox, 'in_carica', True):
                # La centralina dice che l'auto non assorbe (pausa, attesa,
                # batteria piena): l'export che si vede e' l'auto ferma, non
                # sole libero. Si puo' scendere ma non salire.
                salita = 0
            massimo = max(minimo, min(massimo, wallbox.current_set_power + salita))
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
        # Si decide sulla media dell'intera finestra, non campione per
        # campione: al limite del minimo il disponibile oscilla attorno alla
        # soglia e il timer, annullato al primo campione buono, ripartiva ogni
        # 12-18 s (sul campo il 28/10: oltre 60 righe di log in un quarto
        # d'ora) senza arrivare mai a decidere, anche con l'import medio oltre
        # la soglia.
        if self.timer_spegnimento is None and grezza < minimo:
            self._apri_finestra(t)
            self.log(f"[PID] Sole insufficiente ({disponibile:.0f}W disponibili): "
                     f"minimo, spengo tra {cfg['TIMER_SPEGNIMENTO']}s se continua")
        if self.timer_spegnimento is not None:
            # scambio che si avrebbe alla potenza minima
            self.somma_finestra += obiettivo - minimo
            self.campioni_finestra += 1
            if self.somma_finestra >= 0 and uscita - minimo >= cfg['POTENZA_PROTEZIONE']:
                # Il PID e' gia' risalito e la finestra non e' piu' in import.
                # Il solo salto dell'uscita non basta: con il sole che oscilla
                # di 1-2 kW il termine proporzionale la alza per un campione.
                self.log(f"[PID] Sole tornato ({disponibile:.0f}W disponibili): annullo lo spegnimento")
                self.timer_spegnimento = None
            elif t - self.timer_spegnimento >= cfg['TIMER_SPEGNIMENTO']:
                medio = self.somma_finestra / max(self.campioni_finestra, 1)
                self.timer_spegnimento = None
                oltre_soglia = medio < -cfg['PID_IMPORT_SPEGNIMENTO'] or sole + cfg['POTENZA_PRELEVABILE'] < minimo
                if grezza >= minimo and medio >= 0:
                    self.log(f"[PID] Sole tornato ({disponibile:.0f}W disponibili): annullo lo spegnimento")
                elif grezza < minimo and oltre_soglia:
                    self.log(f"[PID] Al minimo si importerebbero in media {-medio:.0f}W: spengo")
                    wallbox.turn_off(force=True)
                    self.ultimo_inviato = None
                    return
                else:
                    # Ancora al limite: nuova finestra, senza riannunciarla. Con
                    # l'import oltre soglia ma il PID che ora vuole almeno il
                    # minimo, la nuvola e' appena passata: non si spegne per un
                    # import ormai finito.
                    self._apri_finestra(t)
                    motivo = "ma il sole e' appena tornato" if oltre_soglia else "sotto soglia"
                    self.log(f"[PID] Al minimo import medio {max(-medio, 0):.0f}W, {motivo}: continuo")

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
