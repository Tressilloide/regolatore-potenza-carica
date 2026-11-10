"""Modalita' di carica e obiettivo "X kWh entro le HH:MM".

Decide QUANTO l'auto puo' prendere dalla rete, in aggiunta a quello che
preleva gia' la casa: la quota che prima era sempre POTENZA_PRELEVABILE.
Tutto il resto non cambia: i regolatori (classico e PID) chiamano
prelevabile_effettivo(), che chiede la quota a questo oggetto e la tiene
comunque entro il limite del contatore. La protezione del contatore resta
attiva in ogni modalita'.

Modalita' (MODALITA in config, persistita):
  sole    solo il sole in piu' rispetto alla casa (quota 0)
  misto   sole + POTENZA_PRELEVABILE dalla rete (il comportamento storico)
  veloce  quanto il contatore permette (quota = LIMITE_CONTATORE, poi
          prelevabile_effettivo la riduce a LIMITE - MARGINE meno la casa)

Obiettivo: "voglio N kWh in auto entro le HH:MM". Si carica col sole finche'
basta; quando la potenza media che servirebbe per finire in tempo,
    (kWh mancanti) / (ore rimaste) / EFFICIENZA,
arriva al minimo della colonnina, si integra con la rete quanto serve. Si
aspetta il minimo perche' sotto la colonnina non carica: prima si
accenderebbe e spegnerebbe di continuo. Una volta partita l'integrazione
resta agganciata fino all'obiettivo (in_recupero), con un margine sopra il
minimo: altrimenti, caricando al minimo, la potenza necessaria scenderebbe
sotto il minimo e l'auto si spegnerebbe a ogni giro. L'energia caricata e'
quella contata dalla centralina (la stessa del limite kWh e delle sessioni).

Modulo senza dipendenze esterne: lo usano il servizio, i test e (volendo) il
simulatore. L'orologio si passa da fuori.
"""
import threading
import time

MODALITA = ('sole', 'misto', 'veloce')
NOMI_MODALITA = {'sole': 'Solo sole', 'misto': 'Sole + rete', 'veloce': 'Veloce'}


class Pianificatore:
    # La centralina conta un po' meno di quanto concesso (l'auto assorbe
    # ~90% del setpoint): si chiede un 10% in piu', meglio finire prima
    EFFICIENZA = 0.9
    # Sopra il minimo della colonnina quando si integra con la rete: il
    # classico accende solo con un'eccedenza OLTRE il minimo, e il PID tiene
    # 300 W di riserva (PID_MARGINE)
    MARGINE_ACCENSIONE = 400
    OBIETTIVO_MAX_KWH = 100
    OBIETTIVO_MAX_ORE = 48

    def __init__(self, config, minimo_colonnina=lambda: 1380, log=print, avvisa=None,
                 salva=None, orologio=time.time):
        """`minimo_colonnina()`: potenza minima della colonnina adesso (dipende
        dalla fase). `salva(stato)`: chiamata a ogni cambio, per la persistenza."""
        self.config = config
        self.minimo_colonnina = minimo_colonnina
        self.log = log
        self.avvisa = avvisa or (lambda messaggio: None)
        self.salva = salva or (lambda stato: None)
        self.ora = orologio
        self.obiettivo = None       # {'kwh', 'entro', 'fatti', 'impostato'}
        self.in_recupero = False
        self.ultima_quota = None
        # controlla() gira sia nel ciclo dei pacchetti sia nel thread
        # periodico: senza lock una scadenza verrebbe notificata due volte
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Persistenza
    # ------------------------------------------------------------------
    def esporta(self):
        return {'obiettivo': self.obiettivo, 'in_recupero': self.in_recupero}

    def importa(self, dati):
        """Ripristina lo stato salvato, ignorando cio' che e' gia' scaduto."""
        if not isinstance(dati, dict):
            return
        adesso = self.ora()
        ob = dati.get('obiettivo')
        try:
            if (isinstance(ob, dict) and float(ob['entro']) > adesso
                    and float(ob['fatti']) < float(ob['kwh'])):
                self.obiettivo = {'kwh': float(ob['kwh']), 'entro': float(ob['entro']),
                                  'fatti': float(ob['fatti']),
                                  'impostato': float(ob.get('impostato', adesso))}
                self.in_recupero = bool(dati.get('in_recupero'))
        except (KeyError, TypeError, ValueError):
            self.obiettivo = None

    def _cambiato(self):
        try:
            self.salva(self.esporta())
        except Exception as e:      # la persistenza non deve fermare la regolazione
            self.log(f"[PIANO] Salvataggio fallito: {e}")

    # ------------------------------------------------------------------
    # Comandi
    # ------------------------------------------------------------------
    def imposta_obiettivo(self, kwh, entro):
        """`entro`: timestamp. Ritorna (ok, messaggio)."""
        try:
            kwh, entro = float(kwh), float(entro)
        except (TypeError, ValueError):
            return False, "Valori non validi"
        adesso = self.ora()
        if not (0 < kwh <= self.OBIETTIVO_MAX_KWH):
            return False, f"Energia fra 0 e {self.OBIETTIVO_MAX_KWH} kWh"
        if not (adesso + 60 < entro <= adesso + self.OBIETTIVO_MAX_ORE * 3600):
            return False, f"Scadenza fra 1 minuto e {self.OBIETTIVO_MAX_ORE} ore"
        self.obiettivo = {'kwh': round(kwh, 2), 'entro': entro, 'fatti': 0.0, 'impostato': adesso}
        self.in_recupero = False
        quando = time.strftime('%d/%m %H:%M', time.localtime(entro))
        self.log(f"[PIANO] Obiettivo: {kwh:g} kWh entro {quando}")
        self._cambiato()
        return True, f"Obiettivo: {kwh:g} kWh entro {quando}"

    def annulla_obiettivo(self, motivo="annullato"):
        if self.obiettivo is None:
            return True, "Nessun obiettivo attivo"
        self.obiettivo = None
        self.in_recupero = False
        self.log(f"[PIANO] Obiettivo {motivo}")
        self._cambiato()
        return True, f"Obiettivo {motivo}"

    # ------------------------------------------------------------------
    # Stato
    # ------------------------------------------------------------------
    def controlla(self):
        """Scadenza dell'obiettivo. Idempotente, la si chiama
        spesso (a ogni quota e dal thread periodico)."""
        with self._lock:
            self._controlla()

    def _controlla(self):
        adesso = self.ora()
        ob = self.obiettivo
        if ob is not None and adesso >= ob['entro']:
            mancano = ob['kwh'] - ob['fatti']
            self.obiettivo = None
            self.in_recupero = False
            self.log(f"[PIANO] Scadenza dell'obiettivo: caricati {ob['fatti']:.1f} "
                     f"di {ob['kwh']:g} kWh")
            self.avvisa(f"⏰ Obiettivo non raggiunto: caricati {ob['fatti']:.1f} di "
                        f"{ob['kwh']:g} kWh (mancano {mancano:.1f} kWh).")
            self._cambiato()

    def modalita_effettiva(self):
        modalita = self.config.get('MODALITA', 'misto')
        return modalita if modalita in MODALITA else 'misto'

    def potenza_necessaria(self):
        """Potenza media (W) che servirebbe da adesso per finire in tempo, o
        None senza obiettivo."""
        ob = self.obiettivo
        if ob is None:
            return None
        mancano = max(0.0, ob['kwh'] - ob['fatti'])
        ore = max((ob['entro'] - self.ora()) / 3600.0, 1 / 60)
        return mancano * 1000.0 / ore / self.EFFICIENZA

    def aggiungi_energia(self, kwh):
        """Energia caricata (kWh, dal contatore della centralina)."""
        with self._lock:
            self._aggiungi_energia(kwh)

    def _aggiungi_energia(self, kwh):
        ob = self.obiettivo
        if ob is None or kwh <= 0:
            return
        ob['fatti'] = round(ob['fatti'] + kwh, 3)
        if ob['fatti'] >= ob['kwh']:
            minuti = (self.ora() - ob['impostato']) / 60
            self.obiettivo = None
            self.in_recupero = False
            self.log(f"[PIANO] Obiettivo raggiunto: {ob['fatti']:.1f} kWh in {minuti:.0f} min")
            self.avvisa(f"🎯 *Obiettivo raggiunto*: {ob['kwh']:g} kWh in auto. "
                        f"Torno alla modalità *{NOMI_MODALITA[self.modalita_effettiva()]}*.")
            self._cambiato()

    def quota_auto(self, deficit_casa):
        """Quanto l'auto puo' prendere dalla rete adesso, in W, oltre alla casa.
        `deficit_casa` = casa - sole (negativo se il sole avanza)."""
        self.controlla()
        modalita = self.modalita_effettiva()
        if modalita == 'veloce':
            quota = float(self.config.get('LIMITE_CONTATORE') or 22000)
        elif modalita == 'sole':
            quota = 0.0
        else:
            quota = float(self.config.get('POTENZA_PRELEVABILE', 0))

        necessaria = self.potenza_necessaria()
        if necessaria is not None:
            minimo = self.minimo_colonnina()
            if not self.in_recupero and necessaria >= minimo:
                self.in_recupero = True
                self.log(f"[PIANO] Per l'obiettivo servono {necessaria:.0f}W in media: "
                         f"integro con la rete")
                self._cambiato()
            if self.in_recupero:
                sole_libero = max(0.0, -deficit_casa)
                serve = max(necessaria, minimo + self.MARGINE_ACCENSIONE)
                quota = max(quota, serve - sole_libero)
        self.ultima_quota = round(quota)
        return quota

    def stato(self):
        """Per pagina web, Telegram e Home Assistant."""
        adesso = self.ora()
        ob = self.obiettivo
        obiettivo = None
        if ob is not None:
            necessaria = self.potenza_necessaria()
            obiettivo = {'kwh': ob['kwh'], 'fatti': round(ob['fatti'], 2), 'entro': ob['entro'],
                         'minuti_rimasti': max(0, round((ob['entro'] - adesso) / 60)),
                         'potenza_necessaria': round(necessaria) if necessaria is not None else None,
                         'in_recupero': self.in_recupero}
        return {'modalita': self.config.get('MODALITA', 'misto'),
                'modalita_effettiva': self.modalita_effettiva(),
                'obiettivo': obiettivo,
                'quota_w': self.ultima_quota}

    def descrizione(self):
        """Una riga per Telegram."""
        s = self.stato()
        testo = NOMI_MODALITA[s['modalita_effettiva']]
        ob = s['obiettivo']
        if ob:
            testo += (f"\n🎯 Obiettivo {ob['fatti']:.1f}/{ob['kwh']:g} kWh entro "
                      f"{time.strftime('%d/%m %H:%M', time.localtime(ob['entro']))}")
            if ob['in_recupero']:
                testo += f", integro con la rete ({ob['potenza_necessaria']} W medi)"
        return testo


def prossima_scadenza(ore, minuti, adesso=None):
    """Timestamp della prossima occorrenza di HH:MM (oggi, o domani se e'
    gia' passata)."""
    adesso = time.time() if adesso is None else adesso
    t = time.localtime(adesso)
    scadenza = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, int(ore), int(minuti), 0, 0, 0, -1))
    if scadenza <= adesso + 60:
        t = time.localtime(adesso + 86400)
        scadenza = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, int(ore), int(minuti), 0, 0, 0, -1))
    return scadenza


def interpreta_orario(testo):
    """'7', '7:30', '07.30', '730' -> (ore, minuti), oppure None."""
    testo = str(testo).strip().replace('.', ':').replace('h', ':')
    try:
        if ':' in testo:
            ore, minuti = testo.split(':', 1)
            ore, minuti = int(ore), int(minuti or 0)
        elif len(testo) in (3, 4):
            ore, minuti = int(testo[:-2]), int(testo[-2:])
        else:
            ore, minuti = int(testo), 0
    except ValueError:
        return None
    if 0 <= ore <= 23 and 0 <= minuti <= 59:
        return ore, minuti
    return None
