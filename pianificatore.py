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
  preset  sole + il prelevabile del preset scelto (PRESET_ATTIVO fra i
          PRESET in config). Un preset e' una coppia "tanti W dalla rete,
          fermati a tanti kWh": il limite di energia lo applica il servizio
          alla colonnina quando lo si sceglie. Ha preso il posto di "veloce"
          (l'11/11/2026): il preset "Veloce" di serie vale lo stesso.

Obiettivo: "voglio N kWh in auto entro le HH:MM". Si carica col sole finche'
basta; quando la potenza media che servirebbe per finire in tempo,
    (kWh mancanti) / (ore rimaste) / EFFICIENZA,
arriva al minimo della colonnina, si integra con la rete quanto serve.
Con una previsione del sole (previsione.py) dai kWh mancanti si toglie prima
il sole atteso entro la scadenza, preso con prudenza: cosi' non si compra di
notte quello che domani arriva dal tetto. Si smette di aspettare il sole
quando la rete da sola farebbe fatica a finire in tempo.

Programma settimanale: regole "nei giorni tali, N kWh entro le HH:MM". Senza
un obiettivo attivo, la prossima scadenza del programma diventa l'obiettivo
(da 24 ore prima). Annullarlo salta solo quella scadenza. Se l'auto viene
staccata, un obiettivo messo a mano si annulla (era per quella carica); uno
del programma riparte da zero, anche se era gia' raggiunto: i kWh caricati
se ne sono andati con l'auto. Si
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

MODALITA = ('sole', 'misto', 'preset')
NOMI_MODALITA = {'sole': 'Solo sole', 'misto': 'Sole + rete', 'preset': 'Preset'}

# --- preset: "tanti W dalla rete, fermati a tanti kWh" ----------------------
PRESET_MAX = 8
PRESET_BASE = [
    {'nome': 'Veloce', 'prelevabile': 6600, 'limite_kwh': 0},
    {'nome': 'Notte', 'prelevabile': 4000, 'limite_kwh': 20},
    {'nome': 'Rabbocco', 'prelevabile': 3000, 'limite_kwh': 10},
]


def valida_preset(lista):
    """Controlla un elenco di preset. Ritorna (True, elenco pulito) oppure
    (False, messaggio). limite_kwh 0 = nessun limite."""
    if not isinstance(lista, list) or not lista:
        return False, "Serve almeno un preset"
    if len(lista) > PRESET_MAX:
        return False, f"Al massimo {PRESET_MAX} preset"
    puliti, nomi = [], set()
    for n, p in enumerate(lista, 1):
        if not isinstance(p, dict):
            return False, f"Preset {n}: formato non valido"
        nome = ' '.join(str(p.get('nome') or '').split())[:24]
        if not nome:
            return False, f"Preset {n}: manca il nome"
        if nome.lower() in nomi:
            return False, f"Due preset si chiamano {nome}"
        nomi.add(nome.lower())
        try:
            prelevabile, limite = float(p.get('prelevabile')), float(p.get('limite_kwh') or 0)
        except (TypeError, ValueError):
            return False, f"{nome}: valori non numerici"
        if not (0 <= prelevabile <= 20000):
            return False, f"{nome}: prelevabile fra 0 e 20000 W"
        if not (0 <= limite <= 100):
            return False, f"{nome}: limite fra 0 (nessuno) e 100 kWh"
        puliti.append({'nome': nome, 'prelevabile': int(round(prelevabile)), 'limite_kwh': int(round(limite))})
    return True, puliti


def preset_attivo(config):
    """Il preset scelto (sempre uno valido: se l'indice salvato non esiste
    piu', il primo)."""
    elenco = config.get('PRESET') or PRESET_BASE
    indice = config.get('PRESET_ATTIVO', 0)
    if not isinstance(indice, int) or not (0 <= indice < len(elenco)):
        indice = 0
    return elenco[indice]


def descrivi_preset(p):
    kw = f"{p['prelevabile'] / 1000:.1f}".replace('.', ',')
    return f"{kw} kW" + (f", fino a {p['limite_kwh']} kWh" if p['limite_kwh'] else ", senza limite")


# --- programma settimanale ---------------------------------------------------
PROGRAMMA_MAX = 10


def valida_programma(regole):
    """Regole {'giorni': [0..6, 0 = lunedi'], 'ora': 'HH:MM', 'kwh': N,
    'attivo': bool}. Ritorna (True, regole pulite) oppure (False, messaggio)."""
    if not isinstance(regole, list):
        return False, "Formato non valido"
    if len(regole) > PROGRAMMA_MAX:
        return False, f"Al massimo {PROGRAMMA_MAX} regole"
    pulite = []
    for n, r in enumerate(regole, 1):
        if not isinstance(r, dict):
            return False, f"Regola {n}: formato non valido"
        try:
            giorni = sorted({int(g) for g in r.get('giorni') or []})
            kwh = float(r.get('kwh'))
        except (TypeError, ValueError):
            return False, f"Regola {n}: valori non validi"
        if not giorni or giorni[0] < 0 or giorni[-1] > 6:
            return False, f"Regola {n}: scegli almeno un giorno"
        orario = interpreta_orario(r.get('ora', ''))
        if orario is None:
            return False, f"Regola {n}: orario non valido"
        if not (0 < kwh <= Pianificatore.OBIETTIVO_MAX_KWH):
            return False, f"Regola {n}: energia fra 0 e {Pianificatore.OBIETTIVO_MAX_KWH} kWh"
        pulite.append({'giorni': giorni, 'ora': f"{orario[0]:02d}:{orario[1]:02d}",
                       'kwh': round(kwh, 1), 'attivo': bool(r.get('attivo', True))})
    return True, pulite


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
    # Del sole previsto ci si fida fino a qui: meglio comprare un po' di
    # rete in piu' che arrivare alla scadenza con l'auto a meta'
    PRUDENZA_PREVISIONE = 0.7
    # Si aspetta il sole finche' la potenza media che servirebbe SENZA sole
    # resta sotto questa parte di quanto la rete puo' dare all'auto
    SOGLIA_ATTESA = 0.6
    CASA_TIPICA_W = 1000
    # Una scadenza del programma diventa obiettivo da queste ore prima
    ANTICIPO_PROGRAMMA_ORE = 24

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
        self.obiettivo = None       # {'kwh', 'entro', 'fatti', 'impostato', 'ricorrente'}
        self.in_recupero = False
        self.ultima_quota = None
        self.programma = []         # regole del programma settimanale
        self.gestita_fino = 0.0     # ultima scadenza del programma gia' usata o saltata
        # sole_atteso(da_ts, a_ts) -> kWh che il sole dovrebbe dare all'auto in
        # quell'intervallo, oppure None se non c'e' una previsione
        self.sole_atteso = None
        self._atteso = (0.0, 0.0)   # (quando, kWh): la previsione si richiede una volta al minuto
        self._raggiunto = None      # ultimo obiettivo del programma raggiunto: {'kwh', 'entro'}
        # controlla() gira sia nel ciclo dei pacchetti sia nel thread
        # periodico: senza lock una scadenza verrebbe notificata due volte
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Persistenza
    # ------------------------------------------------------------------
    def esporta(self):
        return {'obiettivo': self.obiettivo, 'in_recupero': self.in_recupero,
                'programma': self.programma, 'gestita_fino': self.gestita_fino}

    def importa(self, dati):
        """Ripristina lo stato salvato, ignorando cio' che e' gia' scaduto."""
        if not isinstance(dati, dict):
            return
        adesso = self.ora()
        ok, regole = valida_programma(dati.get('programma') or [])
        self.programma = regole if ok else []
        try:
            self.gestita_fino = float(dati.get('gestita_fino') or 0)
        except (TypeError, ValueError):
            self.gestita_fino = 0.0
        ob = dati.get('obiettivo')
        try:
            if (isinstance(ob, dict) and float(ob['entro']) > adesso
                    and float(ob['fatti']) < float(ob['kwh'])):
                self.obiettivo = {'kwh': float(ob['kwh']), 'entro': float(ob['entro']),
                                  'fatti': float(ob['fatti']),
                                  'impostato': float(ob.get('impostato', adesso)),
                                  'ricorrente': bool(ob.get('ricorrente'))}
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
    def imposta_obiettivo(self, kwh, entro, ricorrente=False):
        """`entro`: timestamp. Ritorna (ok, messaggio)."""
        with self._lock:
            return self._imposta_obiettivo(kwh, entro, ricorrente)

    def _imposta_obiettivo(self, kwh, entro, ricorrente):
        try:
            kwh, entro = float(kwh), float(entro)
        except (TypeError, ValueError):
            return False, "Valori non validi"
        adesso = self.ora()
        if not (0 < kwh <= self.OBIETTIVO_MAX_KWH):
            return False, f"Energia fra 0 e {self.OBIETTIVO_MAX_KWH} kWh"
        if not (adesso + 60 < entro <= adesso + self.OBIETTIVO_MAX_ORE * 3600):
            return False, f"Scadenza fra 1 minuto e {self.OBIETTIVO_MAX_ORE} ore"
        self.obiettivo = {'kwh': round(kwh, 2), 'entro': entro, 'fatti': 0.0, 'impostato': adesso,
                          'ricorrente': bool(ricorrente)}
        self.in_recupero = False
        self._atteso = (0.0, 0.0)
        self._raggiunto = None
        quando = time.strftime('%d/%m %H:%M', time.localtime(entro))
        self.log(f"[PIANO] Obiettivo{' dal programma' if ricorrente else ''}: {kwh:g} kWh entro {quando}")
        self._cambiato()
        return True, f"Obiettivo: {kwh:g} kWh entro {quando}"

    def annulla_obiettivo(self, motivo="annullato"):
        with self._lock:
            if self.obiettivo is None:
                return True, "Nessun obiettivo attivo"
            self._chiudi_obiettivo()
            self.log(f"[PIANO] Obiettivo {motivo}")
            self._cambiato()
            return True, f"Obiettivo {motivo}"

    def _chiudi_obiettivo(self):
        """Toglie l'obiettivo. Se veniva dal programma, quella scadenza e'
        andata: raggiunta, scaduta o saltata non si ripropone."""
        ob = self.obiettivo
        if ob is not None and ob.get('ricorrente'):
            self.gestita_fino = max(self.gestita_fino, ob['entro'])
        self.obiettivo = None
        self.in_recupero = False

    def auto_scollegata(self):
        """L'auto e' stata staccata. Ritorna True se un obiettivo messo a mano
        e' stato annullato (c'e' da avvisare). Uno del programma riparte da
        zero, e se era gia' raggiunto ma la scadenza non e' passata torna
        attivo: al rientro l'auto va ricaricata."""
        with self._lock:
            ob = self.obiettivo
            if ob is not None and not ob.get('ricorrente'):
                self._chiudi_obiettivo()
                self.log("[PIANO] Obiettivo annullato: auto scollegata")
                self._cambiato()
                return True
            if ob is not None:
                if ob['fatti'] > 0 or self.in_recupero:
                    self.log(f"[PIANO] Auto scollegata: l'obiettivo del programma riparte da zero "
                             f"(erano {ob['fatti']:.1f} kWh)")
                    ob['fatti'] = 0.0
                    self.in_recupero = False
                    self._cambiato()
                return False
            fatto = self._raggiunto
            if fatto is not None and fatto['entro'] > self.ora() + 120:
                self.log("[PIANO] Auto scollegata dopo aver raggiunto l'obiettivo del programma: torna attivo")
                self._imposta_obiettivo(fatto['kwh'], fatto['entro'], True)
            return False

    def imposta_programma(self, regole):
        """Sostituisce il programma settimanale. Ritorna (ok, messaggio)."""
        ok, esito = valida_programma(regole)
        if not ok:
            return False, esito
        with self._lock:
            self.programma = esito
            # un obiettivo nato dal programma di prima non vale piu'
            if self.obiettivo is not None and self.obiettivo.get('ricorrente'):
                self.obiettivo = None
                self.in_recupero = False
            self.gestita_fino = 0.0
            attive = sum(1 for r in esito if r['attivo'])
            self.log(f"[PIANO] Programma settimanale: {attive} regole attive su {len(esito)}")
            self._cambiato()
            self._controlla()
        return True, ("Programma salvato" if esito else "Programma svuotato")

    def prossima_del_programma(self, adesso=None):
        """(scadenza, regola) della prossima scadenza attiva non ancora
        gestita, oppure None."""
        adesso = self.ora() if adesso is None else adesso
        migliore = None
        for regola in self.programma:
            if not regola['attivo']:
                continue
            ore, minuti = (int(x) for x in regola['ora'].split(':'))
            for avanti in range(8):
                t = time.localtime(adesso + avanti * 86400)
                scadenza = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, ore, minuti, 0, 0, 0, -1))
                if (time.localtime(scadenza).tm_wday in regola['giorni']
                        and scadenza > adesso + 120 and scadenza > self.gestita_fino):
                    if migliore is None or scadenza < migliore[0]:
                        migliore = (scadenza, regola)
                    break
        return migliore

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
            self._chiudi_obiettivo()
            self.log(f"[PIANO] Scadenza dell'obiettivo: caricati {ob['fatti']:.1f} "
                     f"di {ob['kwh']:g} kWh")
            # Un obiettivo del programma con l'auto mai vista in carica (era
            # via) non e' una notizia: arriverebbe un avviso ogni mattina
            if not ob.get('ricorrente') or ob['fatti'] > 0:
                self.avvisa(f"⏰ Obiettivo non raggiunto: caricati {ob['fatti']:.1f} di "
                            f"{ob['kwh']:g} kWh (mancano {mancano:.1f} kWh).")
            self._cambiato()
        if self.obiettivo is None and self.programma:
            prossima = self.prossima_del_programma(adesso)
            if prossima and prossima[0] - adesso <= self.ANTICIPO_PROGRAMMA_ORE * 3600:
                self._imposta_obiettivo(prossima[1]['kwh'], prossima[0], True)

    def modalita_effettiva(self):
        modalita = self.config.get('MODALITA', 'misto')
        return modalita if modalita in MODALITA else 'misto'

    def nome_modalita(self):
        modalita = self.modalita_effettiva()
        if modalita == 'preset':
            return f"Preset {preset_attivo(self.config)['nome']}"
        return NOMI_MODALITA[modalita]

    def capacita_rete(self):
        """Quanti W la rete puo' dare all'auto, a spanne: il tetto del
        contatore meno una casa tipica."""
        limite = self.config.get('LIMITE_CONTATORE') or 0
        if limite <= 0:
            return 7000.0
        return max(1500.0, limite - self.config.get('MARGINE_CONTATORE', 0) - self.CASA_TIPICA_W)

    def sole_previsto(self):
        """kWh di sole attesi per l'auto entro la scadenza (0 senza
        previsione). La previsione si richiede al massimo una volta al minuto."""
        ob = self.obiettivo
        if ob is None or self.sole_atteso is None:
            return 0.0
        adesso = self.ora()
        if adesso - self._atteso[0] > 60:
            try:
                kwh = self.sole_atteso(adesso, ob['entro'])
                kwh = max(0.0, float(kwh)) if kwh is not None else 0.0
            except Exception as e:      # una previsione rotta non ferma la carica
                self.log(f"[PIANO] Previsione non disponibile: {e}")
                kwh = 0.0
            self._atteso = (adesso, kwh)
        return self._atteso[1]

    def potenza_dalla_rete(self):
        """Potenza media (W) da chiedere alla rete per l'obiettivo, tolto il
        sole previsto. Uguale a potenza_necessaria() se non c'e' previsione o
        se non c'e' piu' margine per aspettare. None senza obiettivo."""
        piena = self.potenza_necessaria()
        if piena is None:
            return None
        atteso = self.sole_previsto() * self.PRUDENZA_PREVISIONE
        if atteso <= 0 or piena >= self.SOGLIA_ATTESA * self.capacita_rete():
            return piena
        ob = self.obiettivo
        mancano = max(0.0, ob['kwh'] - ob['fatti'] - atteso)
        ore = max((ob['entro'] - self.ora()) / 3600.0, 1 / 60)
        return mancano * 1000.0 / ore / self.EFFICIENZA

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
            self._chiudi_obiettivo()
            if ob.get('ricorrente'):
                self._raggiunto = {'kwh': ob['kwh'], 'entro': ob['entro']}
            self.log(f"[PIANO] Obiettivo raggiunto: {ob['fatti']:.1f} kWh in {minuti:.0f} min")
            self.avvisa(f"🎯 *Obiettivo raggiunto*: {ob['kwh']:g} kWh in auto. "
                        f"Torno alla modalità *{self.nome_modalita()}*.")
            self._cambiato()

    def quota_auto(self, deficit_casa):
        """Quanto l'auto puo' prendere dalla rete adesso, in W, oltre alla casa.
        `deficit_casa` = casa - sole (negativo se il sole avanza)."""
        self.controlla()
        modalita = self.modalita_effettiva()
        if modalita == 'preset':
            quota = float(preset_attivo(self.config)['prelevabile'])
        elif modalita == 'sole':
            quota = 0.0
        else:
            quota = float(self.config.get('POTENZA_PRELEVABILE', 0))

        necessaria = self.potenza_dalla_rete()
        if necessaria is not None:
            minimo = self.minimo_colonnina()
            if not self.in_recupero and necessaria >= minimo:
                self.in_recupero = True
                atteso = self.sole_previsto()
                self.log(f"[PIANO] Per l'obiettivo servono {necessaria:.0f}W in media"
                         f"{f' (sole previsto {atteso:.1f} kWh)' if atteso > 0 else ''}: "
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
            necessaria = self.potenza_dalla_rete()
            obiettivo = {'kwh': ob['kwh'], 'fatti': round(ob['fatti'], 2), 'entro': ob['entro'],
                         'minuti_rimasti': max(0, round((ob['entro'] - adesso) / 60)),
                         'potenza_necessaria': round(necessaria) if necessaria is not None else None,
                         'sole_previsto_kwh': round(self.sole_previsto(), 1),
                         'ricorrente': bool(ob.get('ricorrente')),
                         'in_recupero': self.in_recupero}
        prossima = self.prossima_del_programma(adesso) if self.programma else None
        return {'modalita': self.modalita_effettiva(),
                'modalita_effettiva': self.modalita_effettiva(),
                'nome_modalita': self.nome_modalita(),
                'obiettivo': obiettivo,
                'programma': self.programma,
                'prossima_programma': ({'entro': prossima[0], 'kwh': prossima[1]['kwh']}
                                       if prossima else None),
                'quota_w': self.ultima_quota}

    def descrizione(self):
        """Una riga per Telegram."""
        s = self.stato()
        testo = s['nome_modalita']
        if s['modalita_effettiva'] == 'preset':
            testo += f" ({descrivi_preset(preset_attivo(self.config))})"
        ob = s['obiettivo']
        if ob:
            testo += (f"\n🎯 Obiettivo {ob['fatti']:.1f}/{ob['kwh']:g} kWh entro "
                      f"{time.strftime('%d/%m %H:%M', time.localtime(ob['entro']))}")
            if ob['in_recupero']:
                testo += f", integro con la rete ({ob['potenza_necessaria']} W medi)"
            elif ob['sole_previsto_kwh'] > 0:
                testo += f", aspetto il sole (previsti {ob['sole_previsto_kwh']:.1f} kWh)"
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
