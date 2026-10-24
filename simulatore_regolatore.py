"""Simulatore per confrontare gli algoritmi di regolazione sul PC.

Usa produzione e consumi reali dallo storico del Raspberry (storico.jsonl,
un campione al minuto) e riproduce:
  - il sensore OWL: pacchetto 'solar' ogni 6 s con 'exporting' calcolato fase
    per fase come fa lui, 'electricity' ogni 60 s con le singole fasi;
  - la centralina: pcar = setpoint (questa centralina non misura nulla);
  - l'auto: segue il setpoint con un piccolo ritardo, fino al suo massimo.

Il vecchio algoritmo e' una COPIA di run_logic/_set_power di
solar_webinterface.py: se si cambia quello, va aggiornata anche qui.
Il PID invece e' importato da regolatore_pid.py, lo stesso file del Pi.

Uso:
  python simulatore_regolatore.py storico.jsonl [giorno ...]
  python simulatore_regolatore.py storico.jsonl --taratura
"""
import json
import math
import random
import sys
import datetime

from regolatore_pid import RegolatorePID

CONFIG_BASE = {
    'MONOFASE_MIN_POWER': 1380, 'MONOFASE_MAX_POWER': 7360,
    'TRIFASE_MIN_POWER': 4140, 'TRIFASE_MAX_POWER': 22000,
    'POTENZA_PROTEZIONE': 300, 'POTENZA_PRELEVABILE': 0,
    'COOLDOWN_ACCENSIONE': 60, 'UPDATE_INTERVAL_S': 5, 'TIMER_SPEGNIMENTO': 60,
    'SMOOTHING_ALPHA': 0.9, 'MAX_DELTA_PER_SEC': 1500,
    'PID_KP': 0.1, 'PID_KI': 0.15, 'PID_KD': 0.0,
    'PID_MARGINE': 150, 'PID_IMPORT_SPEGNIMENTO': 200, 'PID_BANDA_DISCESA': 100,
}


# ---------------------------------------------------------------------------
# Mondo fisico
# ---------------------------------------------------------------------------
class Mondo:
    """Sole e casa per fase, secondo per secondo, da un giorno di storico."""

    QUOTE_SOLE = (0.336, 0.338, 0.326)          # inverter trifase, quasi bilanciato
    QUOTE_CASA = (0.12, 0.15, 0.73)             # misurato: la casa sta quasi tutta su L3

    def __init__(self, campioni, inizio, fine, seme):
        self.inizio, self.fine = inizio, fine
        self.t_c = [c[0] for c in campioni]
        self.sole_c = [c[2] for c in campioni]
        self.casa_c = [c[1] for c in campioni]      # giorni senza auto: consumo = casa
        rnd = random.Random(seme)
        # Carichi brevi che lo storico al minuto non vede (bollitore, forno...)
        self.eventi = []
        t = inizio
        while t < fine:
            t += rnd.expovariate(3 / 3600)          # ~3 all'ora
            self.eventi.append((t, t + rnd.uniform(30, 600), rnd.uniform(800, 2500),
                                rnd.choices((0, 1, 2), (1, 1, 2))[0]))
        self.rumore = [rnd.gauss(0, 60) for _ in range(int(fine - inizio) + 2)]
        self._i = 0

    def _interp(self, serie, t):
        while self._i + 1 < len(self.t_c) and self.t_c[self._i + 1] <= t:
            self._i += 1
        i = self._i
        if i + 1 >= len(self.t_c):
            return serie[i]
        t0, t1 = self.t_c[i], self.t_c[i + 1]
        if t1 - t0 > 600:                           # buco nello storico
            return serie[i]
        f = (t - t0) / (t1 - t0)
        return serie[i] + f * (serie[i + 1] - serie[i])

    def stato(self, t):
        sole = max(self._interp(self.sole_c, t), 0)
        casa = max(self._interp(self.casa_c, t), 150)
        soli = [sole * q for q in self.QUOTE_SOLE]
        case = [casa * q for q in self.QUOTE_CASA]
        case[2] += self.rumore[int(t - self.inizio)]
        for a, b, p, fase in self.eventi:
            if a <= t < b:
                case[fase] += p
        return soli, case


class Auto:
    """Segue il setpoint della wallbox con un ritardo del primo ordine."""

    # Misurato: 7 s dopo un aumento l'auto era a circa l'80% del nuovo valore
    def __init__(self, massimo, tau_su=3.7, tau_giu=1.5, ritardo=1.0, efficienza=0.97):
        self.massimo, self.tau_su, self.tau_giu = massimo, tau_su, tau_giu
        self.ritardo, self.efficienza = ritardo, efficienza
        self.potenza = 0.0
        self.coda = []          # (t effettivo, setpoint o None se spenta)
        self.obiettivo = 0.0

    def comando(self, t, potenza):
        self.coda.append((t + self.ritardo, potenza))

    def passo(self, t, dt=1.0):
        while self.coda and self.coda[0][0] <= t:
            self.obiettivo = self.coda.pop(0)[1]
        obiettivo = min(self.obiettivo, self.massimo) * self.efficienza
        tau = self.tau_su if obiettivo > self.potenza else self.tau_giu
        self.potenza += (obiettivo - self.potenza) * (1 - math.exp(-dt / tau))
        return self.potenza


class FintaWallbox:
    """Stessa interfaccia e stessi filtri di WallboxController, con l'orologio
    simulato al posto di time.time()."""

    def __init__(self, config, auto, orologio):
        self.cfg, self.auto, self.ora = config, auto, orologio
        self.current_set_power = 0
        self.is_on = False
        self.last_update_time = 0
        self.fase = 1
        self.time_turned_off = 0
        self.pending_off_until = 0
        self.smoothing_alpha = config['SMOOTHING_ALPHA']
        self.max_delta_per_sec = config['MAX_DELTA_PER_SEC']
        self.last_power_cmd_time = orologio()
        self.display_power = 0
        self.max_reached_start = None
        self.max_notified = False
        self.manual_off = False
        self.comandi_potenza = 0
        self.accensioni = 0
        self.pcar = 0           # come la centralina: eco del setpoint
        self.pcar_letto = 0     # ...ma il servizio la rilegge solo ogni 5 s

    def limiti_potenza(self):
        if self.fase == 0:
            return self.cfg['MONOFASE_MIN_POWER'], self.cfg['MONOFASE_MAX_POWER']
        return self.cfg['TRIFASE_MIN_POWER'], self.cfg['TRIFASE_MAX_POWER']

    def send_command(self, params):
        btn = params['btn']
        if btn.startswith('P'):
            self.comandi_potenza += 1
            self.pcar = int(btn[1:])
            if self.is_on:
                self.auto.comando(self.ora(), self.pcar)
        elif btn == 'i':
            self.accensioni += 1
            self.auto.comando(self.ora() + 4, self.pcar)   # l'auto riparte dopo qualche secondo
        elif btn == 'o':
            self.auto.comando(self.ora(), 0)
        return True

    def potenza_reale(self):
        return self.pcar_letto if self.is_on else 0

    def set_power(self, watts, bypass):
        return self._set_power(watts, bypass)

    def _set_power(self, watts, bypass):
        min_p, max_p = self.limiti_potenza()
        requested = int(max(min_p, min(max_p, int(watts))))
        now = self.ora()
        if not bypass:
            if abs(requested - self.current_set_power) < self.cfg['POTENZA_PROTEZIONE'] and self.is_on:
                return
            elapsed = now - (self.last_power_cmd_time or now)
            allowed_delta = self.max_delta_per_sec * max(elapsed, 0.01)
            if requested > self.current_set_power + allowed_delta:
                limited = int(self.current_set_power + allowed_delta)
            elif requested < self.current_set_power - allowed_delta:
                limited = int(self.current_set_power - allowed_delta)
            else:
                limited = requested
            if self.last_update_time > 0 and (now - self.last_update_time < self.cfg['UPDATE_INTERVAL_S']):
                return
            if self.display_power == 0:
                smoothed = float(limited)
            else:
                smoothed = self.smoothing_alpha * float(limited) + (1 - self.smoothing_alpha) * float(self.display_power)
            send_value = int(round(smoothed))
            if send_value == self.current_set_power:
                self.display_power = smoothed
                return
        else:
            send_value = requested
            smoothed = float(send_value)
        if self.send_command({'btn': f'P{send_value}'}):
            self.current_set_power = send_value
            self.last_update_time = now
            self.last_power_cmd_time = now
            self.display_power = smoothed

    def turn_on(self):
        if self.is_on:
            return False
        if self.time_turned_off > 0 and self.ora() - self.time_turned_off < self.cfg['COOLDOWN_ACCENSIONE']:
            return False
        min_p, _ = self.limiti_potenza()
        self.set_power(min_p, bypass=True)
        self.send_command({'btn': 'i'})
        self.is_on = True
        self.last_update_time = self.ora()
        return True

    def turn_off(self, force=False):
        now = self.ora()
        if force and self.last_update_time != 0 and (now - self.last_update_time < self.cfg['UPDATE_INTERVAL_S']):
            return
        if not (self.is_on or force):
            return
        self.send_command({'btn': 'o'})
        self.is_on = False
        self.time_turned_off = now
        self.last_update_time = now
        min_p, _ = self.limiti_potenza()
        self.set_power(min_p, bypass=True)


# ---------------------------------------------------------------------------
# Vecchio algoritmo: copia di EnergyMonitor + run_logic
# ---------------------------------------------------------------------------
class AlgoritmoClassico:
    def __init__(self, cfg, wallbox, orologio):
        self.cfg, self.wb, self.ora = cfg, wallbox, orologio
        self.solar_now = 0.0
        self.total_grid_load = 0.0
        self.house_load = 0.0

    def pacchetto_solar(self, sole, exporting):
        self.solar_now = sole
        self.run_logic()

    def pacchetto_electricity(self, consumi, soli):
        self.total_grid_load = sum(consumi)
        self.solar_now = sum(soli)
        self.house_load = self.total_grid_load - self.wb.potenza_reale()
        self.run_logic()

    def run_logic(self):
        wallbox, cfg = self.wb, self.cfg
        potenza_generata = self.solar_now + cfg['POTENZA_PRELEVABILE']
        potenza_consumata = self.total_grid_load
        potenza_carica = wallbox.potenza_reale() if wallbox.is_on else 0
        potenza_casa = self.house_load
        potenza_esportata = potenza_generata - potenza_consumata
        potenza_minima, potenza_massima = wallbox.limiti_potenza()
        now = self.ora()

        if potenza_consumata == 0:
            return
        if not wallbox.is_on:
            if potenza_esportata > potenza_minima:
                wallbox.turn_on()
            return
        if wallbox.pending_off_until > 0:
            if now < wallbox.pending_off_until:
                return
            wallbox.pending_off_until = 0
            if potenza_generata < potenza_minima or potenza_esportata < -200:
                wallbox.turn_off(force=True)
            else:
                wallbox.set_power(potenza_minima, bypass=True)
            return
        if potenza_consumata > potenza_generata:
            nuova_potenza = potenza_generata - potenza_casa - 200
            wallbox.set_power(nuova_potenza, bypass=False)
        if potenza_carica > (potenza_generata - potenza_casa) or potenza_esportata < 0:
            nuova_potenza = potenza_carica - abs(potenza_esportata)
            if nuova_potenza < potenza_minima or potenza_generata < potenza_minima:
                wallbox.set_power(potenza_minima, bypass=True)
                wallbox.pending_off_until = now + cfg['TIMER_SPEGNIMENTO']
            else:
                wallbox.set_power(nuova_potenza, bypass=False)
        else:
            nuova_potenza = potenza_carica + abs(potenza_generata - potenza_consumata) - 100
            if nuova_potenza > potenza_generata:
                return
            delta_potenza = nuova_potenza - potenza_carica
            if potenza_casa + delta_potenza > potenza_generata or nuova_potenza + potenza_casa > potenza_generata:
                return
            if nuova_potenza > potenza_massima:
                if wallbox.current_set_power >= potenza_massima:
                    return
                wallbox.set_power(potenza_massima, bypass=True)
                return
            wallbox.set_power(nuova_potenza, bypass=False)


class AlgoritmoPID:
    def __init__(self, cfg, wallbox, orologio):
        self.wb, self.ora = wallbox, orologio
        self.reg = RegolatorePID(cfg, log=lambda m: None)

    def pacchetto_solar(self, sole, exporting):
        self.reg.passo(self.wb, sole, exporting, self.ora())

    def pacchetto_electricity(self, consumi, soli):
        self.reg.aggiorna_fasi(consumi, soli, self.wb.fase == 1, self.ora())


# ---------------------------------------------------------------------------
# Simulazione
# ---------------------------------------------------------------------------
def simula(campioni, inizio, fine, algoritmo, cfg, auto_max=11000, seme=1, traccia=None, **auto_opzioni):
    t = [inizio]
    orologio = lambda: t[0]
    mondo = Mondo(campioni, inizio, fine, seme)
    auto = Auto(auto_max, **auto_opzioni)
    wb = FintaWallbox(cfg, auto, orologio)
    algo = algoritmo(cfg, wb, orologio)

    import_wh = export_wh = auto_wh = ideale_wh = import_base_wh = 0.0
    ultimo_soli = ultimo_case = None
    passo_owl = 0
    somma_export = 0.0      # 'exporting' e' la media dei 6 s precedenti
    while t[0] < fine:
        soli, case = mondo.stato(t[0])
        p_auto = auto.passo(t[0])
        q = p_auto / 3
        netto = sum(s - c - q for s, c in zip(soli, case))
        import_wh += max(0.0, -netto) / 3600
        export_wh += max(0.0, netto) / 3600
        auto_wh += p_auto / 3600
        disponibile = sum(soli) - sum(case)
        import_base_wh += max(0.0, -disponibile) / 3600
        minimo, massimo = wb.limiti_potenza()
        ideale = min(disponibile, auto_max * auto.efficienza, massimo) if disponibile >= minimo else 0
        ideale_wh += max(ideale, 0) / 3600
        if traccia is not None and int(t[0]) % 6 == 0:
            traccia.append((t[0], sum(soli), sum(case), p_auto, wb.current_set_power if wb.is_on else 0, netto))

        # Lettura di index.json ogni 5 s (INTERVALLO_SYNC_FASE)
        if int(t[0] - inizio) % 5 == 2:
            wb.pcar_letto = wb.pcar

        # Il sensore campiona ogni 6 s; 'electricity' ogni 10 campioni, 1 s dopo
        somma_export += sum(max(0.0, s - c - q) for s, c in zip(soli, case))
        if int(t[0] - inizio) % 6 == 0:
            exporting = somma_export / 6
            somma_export = 0.0
            algo.pacchetto_solar(sum(soli), exporting)
            passo_owl += 1
            if passo_owl % 10 == 0:
                ultimo_soli, ultimo_case = list(soli), [c + q for c in case]
        elif ultimo_soli is not None:
            algo.pacchetto_electricity(ultimo_case, ultimo_soli)
            ultimo_soli = None
        t[0] += 1

    import_extra = import_wh - import_base_wh
    return {
        'auto_kwh': auto_wh / 1000,
        'ideale_kwh': ideale_wh / 1000,
        'import_extra_kwh': import_extra / 1000,
        'da_fv_kwh': (auto_wh - import_extra) / 1000,
        'export_kwh': export_wh / 1000,
        'comandi': wb.comandi_potenza,
        'accensioni': wb.accensioni,
    }


def carica(percorso):
    with open(percorso) as f:
        return [json.loads(r) for r in f if r.strip()]


def giorni_senza_auto(campioni):
    """Giorni con buona produzione e auto mai collegata: consumo = casa pulita."""
    per_giorno = {}
    for c in campioni:
        g = datetime.datetime.fromtimestamp(c[0]).strftime('%Y-%m-%d')
        per_giorno.setdefault(g, []).append(c)
    scelti = {}
    for g, cc in per_giorno.items():
        if len(cc) > 1300 and all(c[3] == 0 for c in cc) and sum(c[2] for c in cc) / 60000 > 20:
            scelti[g] = cc
    return scelti


def finestra(cc, g):
    base = datetime.datetime.strptime(g, '%Y-%m-%d')
    return base.replace(hour=8).timestamp(), base.replace(hour=18).timestamp()


def stampa(nome, r):
    perso = r['ideale_kwh'] - r['da_fv_kwh']
    print(f"  {nome:28s} auto {r['auto_kwh']:6.1f} kWh  da FV {r['da_fv_kwh']:6.1f}  sole perso {perso:5.1f}  "
          f"import extra {r['import_extra_kwh']:5.2f} kWh  comandi {r['comandi']:5d}  accensioni {r['accensioni']:3d}")


def somma(giorni, scelti, algoritmo, cfg, **opzioni):
    tot = {}
    for g in scelti:
        a, b = finestra(giorni[g], g)
        for k, v in simula(giorni[g], a, b, algoritmo, cfg, **opzioni).items():
            tot[k] = tot.get(k, 0) + v
    return tot


def main():
    campioni = carica(sys.argv[1])
    giorni = giorni_senza_auto(campioni)
    argomenti = sys.argv[2:]

    if '--taratura' in argomenti:
        scelti = sorted(giorni)
        print("Giorni:", ", ".join(scelti))
        combinazioni = [dict(PID_KI=ki, PID_KP=kp, PID_MARGINE=m)
                        for ki in (0.03, 0.06, 0.10, 0.16)
                        for kp in (0.0, 0.3)
                        for m in (0, 100, 200)]
        print("Riferimento:")
        stampa('classico', somma(giorni, scelti, AlgoritmoClassico, CONFIG_BASE))
        for c in combinazioni:
            stampa(" ".join(f"{k[4:]}={v}" for k, v in c.items()),
                   somma(giorni, scelti, AlgoritmoPID, dict(CONFIG_BASE, **c)))
        return

    scelti = [g for g in argomenti if g in giorni] or sorted(giorni)
    totali = {'classico': {}, 'pid': {}}
    for g in scelti:
        a, b = finestra(giorni[g], g)
        print(f"{g}  sole {sum(c[2] for c in giorni[g]) / 60000:.0f} kWh")
        for nome, algo in (('classico', AlgoritmoClassico), ('pid', AlgoritmoPID)):
            r = simula(giorni[g], a, b, algo, CONFIG_BASE)
            stampa(nome, r)
            for k, v in r.items():
                totali[nome][k] = totali[nome].get(k, 0) + v
    print("TOTALE")
    for nome in totali:
        stampa(nome, totali[nome])


if __name__ == '__main__':
    main()
