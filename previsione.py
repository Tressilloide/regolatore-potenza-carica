"""Previsione del sole per le prossime ore: quanto produrra' il tetto e quanto
ne restera' per l'auto.

Serve al pianificatore (pianificatore.py) per l'obiettivo "N kWh entro le
HH:MM": se domattina e' previsto sole, stanotte non si compra dalla rete.

Due modi, a seconda di cosa c'e':

  meteo    Home Assistant manda la copertura nuvolosa ora per ora (met.no,
           via MQTT: vedi homeassistant/previsione_meteo.yaml). La produzione
           di un'ora e' quella di una giornata serena, ricavata dalle
           statistiche di casa (la seconda ora migliore degli ultimi 14
           giorni), per un fattore che scende con le nuvole:
               1 - 0,75 * nuvole^3,4        (Kasten e Czeplak, 1980)
  storico  senza meteo (o con meteo vecchio): la media degli ultimi 5 giorni
           a quell'ora. Non sa che tempo fara': e' un "piu' o meno come ieri".

In tutti e due i casi le ore che restano di OGGI si correggono con quello che
il tetto ha fatto davvero finora rispetto al previsto (se stamattina ha reso
la meta', si dimezza anche il pomeriggio).

Modulo senza dipendenze esterne: i dati arrivano da fuori (i record di
statistiche.py e l'elenco delle nuvole), l'orologio pure.
"""
import datetime
import time

# colonne di un'ora nei record di statistiche.py
SOLE, CONSUMO, AUTO = 0, 1, 2

GIORNI_SERENO = 14          # finestra per la giornata serena
GIORNI_TIPICO = 5           # finestra per la media "come ieri"
GIORNO_COMPLETO_MIN = 1200  # minuti misurati perche' un giorno faccia storia
METEO_VALIDO_S = 9 * 3600   # un meteo piu' vecchio di cosi' non si usa


def fattore_nuvole(percento):
    """Quota di irraggiamento che resta con quella copertura (0-100%)."""
    c = min(1.0, max(0.0, float(percento) / 100.0))
    return 1.0 - 0.75 * c ** 3.4


def inizio_ora(ts):
    return int(ts) - int(ts) % 3600


class Previsione:
    def __init__(self, orologio=time.time, potenza_massima_auto=7360):
        self.ora = orologio
        self.potenza_massima_auto = potenza_massima_auto
        self.sereno = [0.0] * 24        # Wh per ora in una giornata serena
        self.tipico = [0.0] * 24        # Wh per ora, media degli ultimi giorni
        self.casa = [0.0] * 24          # Wh per ora della casa, senza auto
        self.giorni_storia = 0
        self.meteo = {}                 # inizio dell'ora (ts) -> nuvole %
        self.meteo_del = 0.0
        self.oggi = None                # record di statistiche.py della giornata in corso

    # ------------------------------------------------------------------
    # Dati in ingresso
    # ------------------------------------------------------------------
    def aggiorna_storia(self, giorni, oggi=None):
        """`giorni`: {giorno ISO: record di statistiche.py}. `oggi`: il giorno
        in corso (ISO), escluso dalla storia e tenuto per la correzione."""
        oggi = oggi or time.strftime('%Y-%m-%d', time.localtime(self.ora()))
        self.oggi = giorni.get(oggi)
        passati = [giorni[g] for g in sorted(giorni) if g < oggi and giorni[g].get('n', 0) >= GIORNO_COMPLETO_MIN]
        self.giorni_storia = len(passati)
        recenti, ultimi = passati[-GIORNI_SERENO:], passati[-GIORNI_TIPICO:]
        for h in range(24):
            valori = sorted((r['ore'][h][SOLE] for r in recenti), reverse=True)
            # la seconda ora migliore: la prima puo' essere un riflesso fra le nuvole
            self.sereno[h] = float(valori[1] if len(valori) > 1 else valori[0] if valori else 0)
            self.tipico[h] = sum(r['ore'][h][SOLE] for r in ultimi) / len(ultimi) if ultimi else 0.0
            self.casa[h] = (sum(max(0, r['ore'][h][CONSUMO] - r['ore'][h][AUTO]) for r in recenti)
                            / len(recenti)) if recenti else 0.0

    def imposta_meteo(self, ore, ricevuto_il=None):
        """`ore`: elenco di {'t': ISO 8601 o timestamp, 'nuvole': 0-100}.
        Ritorna quante ore sono state capite."""
        nuovo = {}
        for voce in ore or []:
            try:
                t, nuvole = voce['t'], float(voce['nuvole'])
                if isinstance(t, str):
                    t = datetime.datetime.fromisoformat(t.replace('Z', '+00:00')).timestamp()
                nuovo[inizio_ora(float(t))] = min(100.0, max(0.0, nuvole))
            except (KeyError, TypeError, ValueError):
                continue
        if nuovo:
            self.meteo = nuovo
            self.meteo_del = self.ora() if ricevuto_il is None else ricevuto_il
        return len(nuovo)

    # ------------------------------------------------------------------
    # Modello
    # ------------------------------------------------------------------
    def meteo_valido(self):
        return bool(self.meteo) and self.ora() - self.meteo_del <= METEO_VALIDO_S

    def fonte(self):
        if self.giorni_storia == 0:
            return None
        return 'meteo' if self.meteo_valido() else 'storico'

    def _grezza(self, ts_ora):
        """Wh attesi nell'ora che comincia a `ts_ora`, senza la correzione di oggi."""
        h = time.localtime(ts_ora + 1800).tm_hour
        if self.meteo_valido() and ts_ora in self.meteo:
            return self.sereno[h] * fattore_nuvole(self.meteo[ts_ora])
        return self.tipico[h]

    def correzione_di_oggi(self):
        """Quanto ha reso il tetto oggi rispetto al previsto (1 = come atteso).
        Vale solo dopo almeno 1 kWh atteso, e resta fra 0,35 e 1,6."""
        if self.oggi is None:
            return 1.0
        mezzanotte = self._mezzanotte()
        atteso = fatto = 0.0
        for h in range(time.localtime(self.ora()).tm_hour):          # solo le ore gia' chiuse
            atteso += self._grezza(inizio_ora(mezzanotte + h * 3600 + 1800))
            fatto += self.oggi['ore'][h][SOLE]
        if atteso < 1000:
            return 1.0
        return min(1.6, max(0.35, fatto / atteso))

    def ore_previste(self, da, a):
        """Elenco di (inizio dell'ora, Wh prodotti attesi, Wh per l'auto) per
        le ore fra `da` e `a`; la prima e l'ultima in proporzione."""
        if self.giorni_storia == 0 or a <= da:
            return []
        oggi = time.strftime('%Y-%m-%d', time.localtime(self.ora()))
        correzione = self.correzione_di_oggi()
        ore, t = [], inizio_ora(da)
        while t < a:
            parte = (min(a, t + 3600) - max(da, t)) / 3600.0
            h = time.localtime(t + 1800).tm_hour
            prodotti = self._grezza(t)
            if time.strftime('%Y-%m-%d', time.localtime(t + 1800)) == oggi:
                prodotti *= correzione
            # prima la casa, poi l'auto; e l'auto non prende piu' del suo massimo
            libero = min(max(0.0, prodotti - self.casa[h]), self.potenza_massima_auto)
            ore.append((t, prodotti * parte, libero * parte))
            t += 3600
        return ore

    def sole_per_auto(self, da, a):
        """kWh che il sole dovrebbe lasciare all'auto fra `da` e `a`, oppure
        None se non c'e' storia su cui basarsi."""
        if self.giorni_storia == 0:
            return None
        return sum(o[2] for o in self.ore_previste(da, a)) / 1000.0

    def _mezzanotte(self, giorni_avanti=0):
        t = time.localtime(self.ora())
        # mktime sistema da solo fine mese e ora legale
        return time.mktime((t.tm_year, t.tm_mon, t.tm_mday + giorni_avanti, 0, 0, 0, 0, 0, -1))

    def riepilogo(self):
        """Per pagina, Telegram e Home Assistant."""
        fonte = self.fonte()
        if fonte is None:
            return {'fonte': None}
        adesso = self.ora()
        domani, dopodomani = self._mezzanotte(1), self._mezzanotte(2)
        resto = self.ore_previste(adesso, domani)
        poi = self.ore_previste(domani, dopodomani)
        kwh = lambda ore, k: round(sum(o[k] for o in ore) / 1000.0, 1)
        return {
            'fonte': fonte,
            'giorni_storia': self.giorni_storia,
            'meteo_del': self.meteo_del if fonte == 'meteo' else None,
            'correzione_oggi': round(self.correzione_di_oggi(), 2),
            'oggi_restante_kwh': kwh(resto, 1), 'oggi_restante_auto_kwh': kwh(resto, 2),
            'domani_kwh': kwh(poi, 1), 'domani_auto_kwh': kwh(poi, 2),
            'sereno_kwh': round(sum(self.sereno) / 1000.0, 1),
            'ore': [[o[0], round(o[1]), round(o[2])] for o in (resto + poi) if o[1] >= 1],
        }
