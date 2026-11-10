"""Algoritmo classico di regolazione della potenza di ricarica.

La logica storica di run_logic, spostata qui senza cambiarne il
comportamento: prima il simulatore ne teneva una COPIA a mano, che andava
riallineata a ogni modifica (e non sempre lo era). Ora Raspberry e
simulatore usano lo stesso codice, come per regolatore_pid.py e
protezione_contatore.py. Modulo senza dipendenze esterne.

Bilancio a ogni pacchetto del sensore:
  disponibile = sole + prelievo concesso (prelevabile_effettivo)
  consumo     = consumo totale dell'ultimo 'electricity' (auto compresa)
- spenta: si accende al minimo se l'eccedenza supera il minimo della colonnina;
- si consuma piu' del disponibile: si scende di quanto serve;
- avanza potenza: si sale lasciando 100 W di margine, ma non se l'auto
  assorbe gia' 1,5 kW meno del setpoint (e' al suo massimo);
- non basta nemmeno il minimo: si resta al minimo per TIMER_SPEGNIMENTO, poi
  si spegne se si importano ancora piu' di 200 W.

Lavora su un oggetto wallbox con l'interfaccia di WallboxController:
is_on, current_set_power, display_power, fase, pending_off_until,
max_reached_start, max_notified, limiti_potenza(), potenza_reale(),
set_power(w, bypass), turn_on(), turn_off(force).
"""
import time

from protezione_contatore import prelevabile_effettivo


class RegolatoreClassico:
    def __init__(self, config, log=print, avvisa=None, log_raro=None, orologio=time.time):
        """`avvisa(messaggio, chiave, intervallo)`: notifica con anti-spam.
        `log_raro(chiave, messaggio, intervallo)`: log al massimo una volta
        ogni `intervallo` secondi (le righe che uscirebbero a ogni pacchetto)."""
        self.config = config
        self.log = log
        self.avvisa = avvisa or (lambda messaggio, chiave=None, intervallo=0: None)
        self.log_raro = log_raro or (lambda chiave, messaggio, intervallo=60: None)
        self.ora = orologio

    def notifica_potenza_massima(self, wallbox, potenza_carica, potenza_massima):
        """Avvisa se la wallbox resta alla potenza massima per almeno 60 s."""
        if not wallbox.is_on:
            return
        if potenza_carica >= potenza_massima:
            if wallbox.max_reached_start is None:
                wallbox.max_reached_start = self.ora()
            elif not wallbox.max_notified and self.ora() - wallbox.max_reached_start >= 60:
                consiglio = "" if wallbox.fase == 1 else (
                    " Consiglio: mettere l'impianto in modalità trifase per sfruttare meglio "
                    "la potenza disponibile.")
                self.avvisa(f"⚠️ Potenza massima raggiunta ({potenza_massima:.0f}W).{consiglio}",
                            'max_potenza', 1800)
                wallbox.max_notified = True
        else:
            wallbox.max_reached_start = None
            wallbox.max_notified = False

    def _imposta(self, wallbox, potenza, bypass, motivo):
        """set_power, e una riga di log solo se il setpoint e' cambiato davvero:
        prima "Aumento a ..." usciva anche quando il comando veniva poi
        filtrato (soglia di protezione, un comando ogni 5 s), cioe' 4 volte
        su 5 (sul Pi: 5960 "Aumento" e 1297 comandi davvero inviati in 72 h)."""
        prima = wallbox.current_set_power
        wallbox.set_power(potenza, bypass=bypass)
        if wallbox.current_set_power != prima:
            self.log(f"[DECISIONE] {motivo}: {prima}W -> {wallbox.current_set_power}W "
                     f"(richiesti {potenza:.0f}W)")

    def passo(self, wallbox, sole, consumo, casa):
        """Un passo a ogni pacchetto. `sole`: produzione piu' recente;
        `consumo`: consumo totale dell'ultimo 'electricity', auto compresa;
        `casa`: lo stesso consumo senza l'auto."""
        cfg = self.config
        potenza_generata = sole
        potenza_consumata = consumo
        # Potenza in carica: si preferisce la misura della centralina (pcar).
        # Con il solo setpoint, se l'auto assorbe meno di quanto concesso il
        # regolatore crede di erogare piu' di quanto faccia e continua ad alzare.
        misurata = wallbox.potenza_reale()
        if misurata is not None and wallbox.is_on:
            potenza_carica = misurata
        else:
            potenza_carica = wallbox.display_power if wallbox.is_on else 0
        potenza_casa = casa
        # Prelievo totale concesso: la quota dell'auto piu' quello che preleva
        # gia' la casa, entro il limite del contatore (protezione_contatore.py)
        prelevabile = prelevabile_effettivo(cfg, potenza_casa - sole)
        potenza_generata += prelevabile
        potenza_esportata = potenza_generata - potenza_consumata

        self.log_raro('classico_stato',
                      f"[INFO] Gen: {potenza_generata:.0f}W (prel {prelevabile:.0f}W) | "
                      f"Casa: {potenza_casa:.0f}W | Esp: {potenza_esportata:.0f}W | "
                      f"WB: {'ON' if wallbox.is_on else 'OFF'} ({potenza_carica:.0f}W)", 60)

        potenza_minima, potenza_massima = wallbox.limiti_potenza()
        self.notifica_potenza_massima(wallbox, potenza_carica, potenza_massima)

        if potenza_consumata == 0:
            return

        if not wallbox.is_on:
            if potenza_esportata > potenza_minima:
                # Si logga solo se turn_on ha davvero acceso: durante il
                # cooldown la richiesta viene rifiutata
                if wallbox.turn_on():
                    self.log(f"[DECISIONE] Export sufficiente ({potenza_esportata:.0f}W). "
                             f"Acceso a {potenza_minima}W.")
            return

        now = self.ora()
        if wallbox.pending_off_until > 0:
            if now < wallbox.pending_off_until:
                self.log_raro('classico_timer',
                              f"[INFO] Timer minimo attivo: {wallbox.pending_off_until - now:.0f}s "
                              f"restanti", 30)
                return
            wallbox.pending_off_until = 0
            # Si spegne se si continua a importare piu' di 200 W
            if potenza_generata < potenza_minima or potenza_esportata < -200:
                self.log("[DECISIONE] Sole insufficiente. Spengo.")
                consiglio = ("Consiglio: mettere l'impianto in modalità monofase per sfruttare "
                             "meglio la potenza disponibile." if wallbox.fase == 1
                             else "Consiglio: staccare la macchina.")
                self.avvisa(f"⚠️ Potenza insufficiente ({potenza_generata:.0f}W), consumo casa "
                            f"({potenza_casa:.0f}W). Spengo wallbox.\n{consiglio}",
                            'sole_insufficiente', 900)
                wallbox.turn_off(force=True)
            else:
                self.log("[DECISIONE] Generazione sufficiente. Continuo.")
                wallbox.set_power(potenza_minima, bypass=True)
            return

        if potenza_consumata > potenza_generata:
            nuova_potenza = potenza_generata - potenza_casa - 200   # 200 W evitano on/off
            self._imposta(wallbox, nuova_potenza, False, "Diminuisco")
        if potenza_carica > (potenza_generata - potenza_casa) or potenza_esportata < 0:
            if potenza_esportata < 0:
                nuova_potenza = potenza_carica - abs(potenza_esportata)
            else:
                # L'auto supera il disponibile ma l'export e' positivo: il
                # consumo e' di prima dell'ultimo aumento (arriva ogni 60 s).
                # Togliere l'export, come si faceva, scendeva troppo: il 1/11
                # alle 11:10 da 8543 a 5866 W quando ne bastavano 8046.
                nuova_potenza = potenza_generata - potenza_casa - 100
            if nuova_potenza < potenza_minima or potenza_generata < potenza_minima:
                self.log(f"[DECISIONE] Sole insufficiente. Minimo per {cfg['TIMER_SPEGNIMENTO']}s.")
                wallbox.set_power(potenza_minima, bypass=True)
                wallbox.pending_off_until = now + cfg['TIMER_SPEGNIMENTO']
            else:
                self._imposta(wallbox, nuova_potenza, False, "Diminuisco")
            return

        # Qui c'era "if nuova_potenza > potenza_generata: return", che
        # equivale a "casa sotto -100 W". pcar e' il setpoint e l'auto ne
        # assorbe il 10-15% in meno, quindi la casa calcolata scende sotto
        # zero quasi sempre: il 1/11 ha bloccato 921 aumenti su 1011, e alle
        # 11:10 l'auto e' rimasta a 6 kW con 11-13 kW di sole per 4 minuti.
        # Oltre 1,5 kW l'auto e' al suo massimo (11 kW) e alzare non serve.
        # Senza controllo il setpoint arrivava a 22 kW, e al primo calo di
        # sole servivano 4-5 passi prima che l'auto rallentasse davvero.
        if potenza_casa < -1500:
            return
        nuova_potenza = potenza_carica + abs(potenza_generata - potenza_consumata) - 100
        delta_potenza = nuova_potenza - potenza_carica
        if (potenza_casa + delta_potenza > potenza_generata
                or nuova_potenza + potenza_casa > potenza_generata):
            return

        if nuova_potenza > potenza_massima:
            # Gia' al massimo: niente btn=P<max> ripetuto a ogni ciclo
            if wallbox.current_set_power >= potenza_massima:
                self.log_raro('gia_al_massimo',
                              f"[DECISIONE] Gia' alla potenza massima ({potenza_massima:.0f}W).", 600)
                return
            self._imposta(wallbox, potenza_massima, True, "Aumento al massimo")
            return
        self._imposta(wallbox, nuova_potenza, False, "Aumento")
