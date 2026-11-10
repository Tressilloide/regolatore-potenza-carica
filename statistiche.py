"""Statistiche di lungo periodo: dai campioni al minuto ai numeri della pagina.

Tre pezzi, tutti senza dipendenze dal servizio (si provano da soli):

  aggrega(campioni)        campioni al minuto -> un record per giorno
  Archivio(percorso)       i record dei giorni chiusi, una riga JSON per giorno
  calcola(giorni, da, a)   i record di un periodo -> tutto cio' che la pagina mostra

Un campione e' (ts, consumo_w, sole_w, auto_w, fasi) dove consumo e' il totale
letto dal sensore (casa + auto), auto e' la potenza DICHIARATA dalla colonnina
e fasi sono le sei letture L1..L6 (o None).

Il record di un giorno tiene l'energia ora per ora, i picchi, quanto tempo si
e' passati a ogni potenza e i tratti in cui l'auto caricava: un migliaio di
byte al giorno, cosi' si puo' tenere tutto per sempre anche su una scheda SD.

Da riga di comando ricostruisce l'archivio dai file dei campioni:

  TZ=Europe/Rome python3 statistiche.py importa <uscita.jsonl> <file>...

(TZ serve se la macchina non e' nel fuso di casa: i giorni e le ore sono quelli
locali.)
"""
import datetime
import json
import os
import sys
import threading
import time

# --- costanti ---------------------------------------------------------------
MINIMO_AUTO_W = 1380        # sotto questa potenza la colonnina non carica
PASSO_PRELIEVO_W = 500      # istogramma del prelievo: 16 classi da 500 W
CLASSI_PRELIEVO = 16
PASSO_AUTO_W = 1000         # istogramma della potenza in auto: 22 classi da 1 kW
CLASSI_AUTO = 22
SOLE_MINIMO_W = 50          # sotto, di notte, e' il rumore del sensore e non produzione
BUCO_MASSIMO_S = 150        # oltre, fra due campioni non si inventa energia
PAUSA_TRATTO_S = 300        # una pausa piu' corta non spezza un tratto di carica
PAUSA_RICARICA_S = 1800     # tratti piu' vicini di cosi' sono la stessa ricarica
RICARICA_MINIMA_KWH = 0.2   # sotto, e' un'accensione breve e non una ricarica
GIORNO_COMPLETO_MIN = 1300  # minuti coperti perche' un giorno conti nei record

# indici delle colonne di ogni ora in record['ore']
SOLE, CONSUMO, AUTO, PRELIEVO, IMMESSA, AUTO_SOLE = range(6)


def giorno_di(ts):
    return time.strftime('%Y-%m-%d', time.localtime(ts))


def data(giorno):
    return datetime.date.fromisoformat(giorno)


# --- quanto assorbe davvero l'auto ------------------------------------------
def auto_reale(campioni):
    """Potenza realmente assorbita dall'auto, campione per campione.

    La potenza dichiarata non basta, per due motivi visti nei dati:
      - colonnina spenta dal quadro mentre caricava: il valore resta fermo
        all'ultimo letto per ore (l'8/11: 3856 W dichiarati tutta la notte con
        la casa a 760 W; 86 kWh "in auto" in un giorno da 54 di consumo);
      - in trifase la colonnina dichiara quanto ha concesso, non quanto l'auto
        prende (22 kW dichiarati, 11 misurati dal sensore).
    Il totale del sensore invece e' una misura: l'auto non puo' assorbire piu'
    di quello. Dove il dichiarato lo supera, all'auto si da' il totale meno la
    casa (l'ultima vista quando i conti tornavano). Se quel che resta e' meno
    del minimo a cui la colonnina carica, l'auto e' ferma, e resta ferma finche'
    il valore dichiarato non cambia: un valore fermo non riparte da solo.
    Sul periodo con il contatore della colonnina (dal 29/10) il risultato
    coincide con quello entro pochi punti percentuali.
    """
    n = len(campioni)
    reale = [0.0] * n
    casa_stimata = 800.0
    precedente, ferma = None, False
    for i, c in enumerate(campioni):
        consumo, dichiarata = max(0.0, c[1] or 0), c[3] or 0
        if dichiarata != precedente or (i and c[0] - campioni[i - 1][0] > 600):
            ferma = False
        precedente = dichiarata
        if dichiarata <= 0:
            casa_stimata = min(3000.0, max(200.0, consumo))
            continue
        if ferma:
            continue
        if dichiarata <= consumo + max(200.0, 0.08 * dichiarata):
            reale[i] = min(dichiarata, consumo)
            casa_stimata = min(3000.0, max(200.0, consumo - reale[i]))
            continue
        resto = consumo - casa_stimata
        if resto < 0.9 * MINIMO_AUTO_W:
            ferma = True
        else:
            reale[i] = min(dichiarata, resto)
    return reale


# --- dai campioni ai giorni --------------------------------------------------
def _record_vuoto(giorno):
    return {
        'g': giorno,
        'n': 0.0,
        'ore': [[0.0] * 6 for _ in range(24)],
        'pk': {'prelievo': [0, 0], 'sole': [0, 0], 'consumo': [0, 0], 'casa': [0, 0], 'auto': [0, 0]},
        'ip': [0.0] * CLASSI_PRELIEVO,
        'ia': [0.0] * CLASSI_AUTO,
        'fasi': [0.0] * 6,
        'fmax': [0, 0, 0],
        'nf': 0.0,
        'ric': [],
    }


def _chiudi(r):
    """Arrotonda per la scrittura: Wh interi, minuti interi."""
    r['n'] = round(r['n'])
    r['ore'] = [[round(v) for v in ora] for ora in r['ore']]
    r['ip'] = [round(v) for v in r['ip']]
    r['ia'] = [round(v) for v in r['ia']]
    if r['nf'] >= 1:
        r['fasi'] = [round(v) for v in r['fasi']]
        r['nf'] = round(r['nf'])
    else:
        r['fasi'], r['fmax'], r['nf'] = None, None, 0
    r['ric'] = [[int(t[0]), int(t[1]), round(t[2]), round(t[3]), int(t[4])] for t in r['ric']]
    return r


def aggrega(campioni):
    """Campioni (in qualunque ordine) -> {giorno: record}."""
    campioni = sorted((c for c in campioni if c and c[0]), key=lambda c: c[0])
    reale = auto_reale(campioni)
    giorni = {}
    n = len(campioni)
    for i, c in enumerate(campioni):
        ts, consumo, sole = c[0], max(0.0, c[1] or 0), max(0.0, c[2] or 0)
        if sole < SOLE_MINIMO_W:
            sole = 0.0                   # le pinze leggono 16-48 W anche al buio
        fasi = c[4] if len(c) > 4 else None
        dt = campioni[i + 1][0] - ts if i + 1 < n else 60
        if dt <= 0:
            continue                     # doppione
        if dt > BUCO_MASSIMO_S:
            dt = 60                      # buco: vale il suo minuto e basta
        locale = time.localtime(ts)
        giorno = time.strftime('%Y-%m-%d', locale)
        r = giorni.get(giorno)
        if r is None:
            r = giorni[giorno] = _record_vuoto(giorno)
        auto = reale[i]
        casa = consumo - auto
        prelievo = max(0.0, consumo - sole)
        immessa = max(0.0, sole - consumo)
        # prima la casa, poi l'auto: all'auto va il sole che la casa non usa
        auto_sole = min(auto, max(0.0, sole - casa))
        ore, minuti = dt / 3600.0, dt / 60.0

        r['n'] += minuti
        ora = r['ore'][locale.tm_hour]
        ora[SOLE] += sole * ore
        ora[CONSUMO] += consumo * ore
        ora[AUTO] += auto * ore
        ora[PRELIEVO] += prelievo * ore
        ora[IMMESSA] += immessa * ore
        ora[AUTO_SOLE] += auto_sole * ore

        for chiave, valore in (('prelievo', prelievo), ('sole', sole), ('consumo', consumo),
                               ('casa', casa), ('auto', auto)):
            if valore > r['pk'][chiave][0]:
                r['pk'][chiave] = [int(valore), int(ts)]
        if prelievo > 0:
            r['ip'][min(CLASSI_PRELIEVO - 1, int(prelievo // PASSO_PRELIEVO_W))] += minuti
        if auto > 0:
            r['ia'][min(CLASSI_AUTO - 1, int(auto // PASSO_AUTO_W))] += minuti
            tratti = r['ric']
            if tratti and ts - tratti[-1][1] <= PAUSA_TRATTO_S:
                t = tratti[-1]
                t[1], t[2], t[3], t[4] = ts + dt, t[2] + auto * ore, t[3] + auto_sole * ore, max(t[4], auto)
            else:
                tratti.append([ts, ts + dt, auto * ore, auto_sole * ore, auto])
        if fasi and len(fasi) >= 6 and any(fasi[:3]):
            for k in range(6):
                r['fasi'][k] += max(0.0, fasi[k]) * ore
            for k in range(3):
                r['fmax'][k] = max(r['fmax'][k], int(fasi[k]))
            r['nf'] += minuti
    return {g: _chiudi(r) for g, r in giorni.items()}


# --- archivio dei giorni chiusi ------------------------------------------------
class Archivio:
    """I record dei giorni, una riga JSON per giorno. Si scrive una volta al
    giorno (in coda); se un giorno compare due volte vale l'ultima riga."""

    def __init__(self, percorso, log=None):
        self.percorso = percorso
        self.log = log or (lambda testo: None)
        self._blocco = threading.Lock()
        self._giorni = None
        self._letto = None

    def giorni(self):
        with self._blocco:
            try:
                firma = os.stat(self.percorso)
                firma = (firma.st_mtime_ns, firma.st_size)
            except OSError:
                self._giorni, self._letto = {}, None
                return {}
            if self._giorni is None or firma != self._letto:
                giorni = {}
                try:
                    with open(self.percorso, encoding='utf-8') as f:
                        for riga in f:
                            try:
                                r = json.loads(riga)
                                giorni[r['g']] = r
                            except (ValueError, KeyError, TypeError):
                                continue      # riga troncata da uno spegnimento
                except OSError as e:
                    self.log(f"[STATISTICHE] Lettura archivio fallita: {e}")
                self._giorni, self._letto = giorni, firma
            return self._giorni

    def aggiungi(self, record):
        righe = ''.join(json.dumps(r, separators=(',', ':')) + '\n' for r in record)
        if not righe:
            return
        with self._blocco:
            with open(self.percorso, 'a', encoding='utf-8') as f:
                f.write(righe)
                f.flush()
                os.fsync(f.fileno())
            self._giorni = None

    def archivia(self, campioni, oggi=None):
        """Chiude i giorni passati presenti nei campioni e non ancora in
        archivio. Ritorna i giorni aggiunti."""
        oggi = oggi or giorno_di(time.time())
        presenti = self.giorni()
        da_fare = {giorno_di(c[0]) for c in campioni} - set(presenti) - {oggi}
        da_fare = {g for g in da_fare if g < oggi}
        if not da_fare:
            return []
        nuovi = [r for g, r in sorted(aggrega(campioni).items()) if g in da_fare]
        self.aggiungi(nuovi)
        return [r['g'] for r in nuovi]


# --- dai giorni ai numeri della pagina -----------------------------------------
def _somma_ore(r):
    """Totali del giorno in Wh: [sole, consumo, auto, prelievo, immessa, auto_sole]."""
    tot = [0.0] * 6
    for ora in r['ore']:
        for k in range(6):
            tot[k] += ora[k]
    return tot


def _totali(record, prezzi):
    wh = [0.0] * 6
    minuti = 0
    for r in record:
        t = _somma_ore(r)
        for k in range(6):
            wh[k] += t[k]
        minuti += r['n']
    sole, consumo, auto, prelievo, immessa, auto_sole = (v / 1000.0 for v in wh)
    casa = consumo - auto
    autoconsumo = sole - immessa
    casa_sole = max(0.0, autoconsumo - auto_sole)
    acquisto, vendita = prezzi['acquisto'], prezzi['vendita']
    spesa, ricavo = prelievo * acquisto, immessa * vendita
    senza_fv = consumo * acquisto
    auto_rete = auto - auto_sole
    r3 = lambda v: round(v, 3)
    r2 = lambda v: round(v, 2)
    return {
        'giorni': len(record),
        'minuti': minuti,
        'sole': r3(sole), 'consumo': r3(consumo), 'casa': r3(casa), 'auto': r3(auto),
        'prelievo': r3(prelievo), 'immessa': r3(immessa), 'autoconsumo': r3(autoconsumo),
        'casa_sole': r3(casa_sole), 'casa_rete': r3(max(0.0, casa - casa_sole)),
        'auto_sole': r3(auto_sole), 'auto_rete': r3(auto_rete),
        'autoconsumo_pct': round(autoconsumo / sole * 100, 1) if sole > 0.05 else None,
        'autosufficienza_pct': round(autoconsumo / consumo * 100, 1) if consumo > 0.05 else None,
        'auto_sole_pct': round(auto_sole / auto * 100, 1) if auto > 0.05 else None,
        'minuti_carica': round(sum(sum(r['ia']) for r in record)),
        'euro': {
            'spesa': r2(spesa), 'ricavo': r2(ricavo), 'netto': r2(spesa - ricavo),
            'senza_fv': r2(senza_fv), 'beneficio': r2(senza_fv - (spesa - ricavo)),
            # l'auto: quel che si e' pagato alla rete, e il sole che non si e' venduto
            'auto_rete': r2(auto_rete * acquisto), 'auto_mancata_vendita': r2(auto_sole * vendita),
        },
    }


def _profilo(record):
    """Potenza media (W) per ora del giorno."""
    if not record:
        return None
    wh = [[0.0] * 24 for _ in range(6)]
    for r in record:
        for h, ora in enumerate(r['ore']):
            for k in range(6):
                wh[k][h] += ora[k]
    n = len(record)
    m = lambda k: [round(v / n) for v in wh[k]]
    return {'giorni': n, 'sole': m(SOLE), 'casa': [round((wh[CONSUMO][h] - wh[AUTO][h]) / n) for h in range(24)],
            'auto': m(AUTO), 'prelievo': m(PRELIEVO), 'immessa': m(IMMESSA)}


def _percentile(valori, p):
    if not valori:
        return None
    valori = sorted(valori)
    return valori[min(len(valori) - 1, int(p / 100.0 * len(valori)))]


def _mediana(valori):
    if not valori:
        return None
    valori = sorted(valori)
    m = len(valori) // 2
    return valori[m] if len(valori) % 2 else (valori[m - 1] + valori[m]) / 2.0


def ricariche(record):
    """Unisce i tratti di carica dei giorni in ricariche (anche a cavallo
    della mezzanotte). Ritorna (ricariche, accensioni brevi scartate)."""
    tratti = sorted(t for r in record for t in (r.get('ric') or []))
    unite = []
    for inizio, fine, wh, wh_sole, picco in tratti:
        if unite and inizio - unite[-1]['fine'] <= PAUSA_RICARICA_S:
            u = unite[-1]
            u['fine'] = max(u['fine'], fine)
            u['wh'] += wh
            u['wh_sole'] += wh_sole
            u['picco'] = max(u['picco'], picco)
            u['attivi'] += fine - inizio
        else:
            unite.append({'inizio': inizio, 'fine': fine, 'wh': wh, 'wh_sole': wh_sole,
                          'picco': picco, 'attivi': fine - inizio})
    buone = []
    for u in unite:
        kwh = u['wh'] / 1000.0
        if kwh < RICARICA_MINIMA_KWH:
            continue
        ore_attive = max(60, u['attivi']) / 3600.0
        buone.append({
            'inizio': u['inizio'], 'fine': u['fine'],
            'minuti': round((u['fine'] - u['inizio']) / 60),
            'kwh': round(kwh, 2),
            'sole_pct': round(min(100.0, u['wh_sole'] / u['wh'] * 100)) if u['wh'] > 0 else 0,
            'potenza_media': round(kwh * 1000 / ore_attive),
            'picco': u['picco'],
        })
    return buone, len(unite) - len(buone)


def _statistiche_ricariche(record, prezzi):
    elenco, brevi = ricariche(record)
    if not elenco:
        return {'n': 0, 'brevi': brevi, 'elenco': []}
    kwh = [r['kwh'] for r in elenco]
    minuti = [r['minuti'] for r in elenco]
    passo = 5
    classi = [0] * 12                       # 0-5, 5-10, ... 55+ kWh
    ore_inizio = [0] * 24
    kwh_ora_inizio = [0.0] * 24
    giorni_sett = [0] * 7
    for r in elenco:
        classi[min(len(classi) - 1, int(r['kwh'] // passo))] += 1
        inizio = time.localtime(r['inizio'])
        ore_inizio[inizio.tm_hour] += 1
        kwh_ora_inizio[inizio.tm_hour] += r['kwh']
        giorni_sett[inizio.tm_wday] += 1
    totale = sum(kwh)
    dal_sole = sum(r['kwh'] * r['sole_pct'] / 100.0 for r in elenco)
    return {
        'n': len(elenco), 'brevi': brevi,
        'kwh': round(totale, 1),
        'sole_pct': round(dal_sole / totale * 100) if totale else None,
        'media_kwh': round(totale / len(elenco), 1),
        'mediana_kwh': round(_mediana(kwh), 1),
        'massima_kwh': max(kwh),
        'media_minuti': round(sum(minuti) / len(minuti)),
        'mediana_minuti': round(_mediana(minuti)),
        'potenza_media': round(sum(r['potenza_media'] * r['kwh'] for r in elenco) / totale) if totale else None,
        'tutte_sole': sum(1 for r in elenco if r['sole_pct'] >= 95),
        'ist_kwh': {'passo': passo, 'valori': classi},
        'ist_ora': ore_inizio,
        'ist_giorno': giorni_sett,
        'elenco': sorted(elenco, key=lambda r: -r['inizio'])[:300],
    }


def _record_del_periodo(record, giornalieri, elenco_ricariche):
    """I primati del periodo. `giornalieri` e' allineato a `record`."""
    out = []

    def aggiungi(chiave, valore, unita, quando=None, ora=None):
        if valore is None:
            return
        out.append({'chiave': chiave, 'valore': valore, 'unita': unita, 'giorno': quando, 'ts': ora})

    completi = [(r, g) for r, g in zip(record, giornalieri) if r['n'] >= GIORNO_COMPLETO_MIN]
    tutti = list(zip(record, giornalieri))

    def migliore(coppie, chiave, minimo=False):
        coppie = [c for c in coppie if c[1].get(chiave) is not None]
        if not coppie:
            return None
        return (min if minimo else max)(coppie, key=lambda c: c[1][chiave])[1]

    def picco(chiave):
        validi = [r['pk'][chiave] for r in record if r['pk'][chiave][0] > 0]
        return max(validi) if validi else None

    g = migliore(tutti, 'sole')
    if g and g['sole'] > 0:
        aggiungi('giorno_sole_max', g['sole'], 'kWh', g['g'])
    g = migliore(completi, 'sole', minimo=True)
    if g:
        aggiungi('giorno_sole_min', g['sole'], 'kWh', g['g'])
    p = picco('sole')
    if p:
        aggiungi('picco_sole', p[0], 'W', ora=p[1])
    g = migliore(tutti, 'casa')
    if g:
        aggiungi('giorno_casa_max', g['casa'], 'kWh', g['g'])
    g = migliore(completi, 'casa', minimo=True)
    if g:
        aggiungi('giorno_casa_min', g['casa'], 'kWh', g['g'])
    p = picco('casa')
    if p:
        aggiungi('picco_casa', p[0], 'W', ora=p[1])
    p = picco('consumo')
    if p:
        aggiungi('picco_consumo', p[0], 'W', ora=p[1])
    g = migliore(tutti, 'prelievo')
    if g and g['prelievo'] > 0:
        aggiungi('giorno_prelievo_max', g['prelievo'], 'kWh', g['g'])
    g = migliore(completi, 'prelievo', minimo=True)
    if g:
        aggiungi('giorno_prelievo_min', g['prelievo'], 'kWh', g['g'])
    p = picco('prelievo')
    if p:
        aggiungi('picco_prelievo', p[0], 'W', ora=p[1])
    g = migliore(tutti, 'immessa')
    if g and g['immessa'] > 0:
        aggiungi('giorno_immessa_max', g['immessa'], 'kWh', g['g'])
    g = migliore(completi, 'autosufficienza')
    if g:
        aggiungi('giorno_autosufficienza_max', g['autosufficienza'], '%', g['g'])
    g = migliore(tutti, 'auto')
    if g and g['auto'] > 0:
        aggiungi('giorno_auto_max', g['auto'], 'kWh', g['g'])
    p = picco('auto')
    if p:
        aggiungi('picco_auto', p[0], 'W', ora=p[1])
    if elenco_ricariche:
        r = max(elenco_ricariche, key=lambda r: r['kwh'])
        aggiungi('ricarica_max', r['kwh'], 'kWh', ora=r['inizio'])
        r = max(elenco_ricariche, key=lambda r: r['minuti'])
        aggiungi('ricarica_lunga', r['minuti'], 'min', ora=r['inizio'])
    # l'ora migliore: quella con piu' sole in assoluto
    ora_top = max(((ora[SOLE], r['g'], h) for r in record for h, ora in enumerate(r['ore'])), default=None)
    if ora_top and ora_top[0] > 0:
        aggiungi('ora_sole_max', round(ora_top[0] / 1000.0, 2), 'kWh', ora_top[1])
        out[-1]['ora'] = ora_top[2]
    return out


def _serie(giornalieri):
    """Strisce di giorni consecutivi: in attivo (piu' immesso che prelevato)
    e con ricarica."""
    def striscia(condizione):
        migliore = (0, None, None)
        corrente, inizio, precedente = 0, None, None
        for g in giornalieri:
            d = data(g['g'])
            if condizione(g):
                if corrente and precedente and (d - precedente).days == 1:
                    corrente += 1
                else:
                    corrente, inizio = 1, g['g']
                if corrente > migliore[0]:
                    migliore = (corrente, inizio, g['g'])
            else:
                corrente = 0
            precedente = d
        return {'giorni': migliore[0], 'da': migliore[1], 'a': migliore[2]}

    attivo = lambda g: g['immessa'] > g['prelievo']
    return {
        'in_attivo': striscia(attivo),
        'giorni_in_attivo': sum(1 for g in giornalieri if attivo(g)),
        'con_ricarica': striscia(lambda g: g['auto'] >= 1),
        'giorni_con_ricarica': sum(1 for g in giornalieri if g['auto'] >= 1),
        'giorni_autosufficienti': sum(1 for g in giornalieri if (g['autosufficienza'] or 0) >= 90),
    }


def _giornaliero(r):
    sole, consumo, auto, prelievo, immessa, auto_sole = (v / 1000.0 for v in _somma_ore(r))
    autoconsumo = sole - immessa
    return {
        'g': r['g'], 'minuti': r['n'],
        'sole': round(sole, 2), 'casa': round(consumo - auto, 2), 'auto': round(auto, 2),
        'auto_sole': round(auto_sole, 2), 'prelievo': round(prelievo, 2), 'immessa': round(immessa, 2),
        'autosufficienza': round(autoconsumo / consumo * 100, 1) if consumo > 0.05 else None,
        'autoconsumo': round(autoconsumo / sole * 100, 1) if sole > 0.05 else None,
        'picco_prelievo': r['pk']['prelievo'][0], 'picco_sole': r['pk']['sole'][0],
    }


def calcola(giorni, da, a, prezzi, limite_contatore=6600):
    """Tutti i numeri della pagina per il periodo [da, a] (giorni ISO, inclusi).

    giorni: {giorno: record}, gia' comprensivo del giorno in corso se serve.
    """
    tutti = sorted(giorni)
    if not tutti:
        return {'copertura': None}
    da, a = max(da, tutti[0]), min(a, tutti[-1])
    record = [giorni[g] for g in tutti if da <= g <= a]
    out = {
        'copertura': {'primo': tutti[0], 'ultimo': tutti[-1], 'giorni': len(tutti),
                      'mesi': sorted({g[:7] for g in tutti})},
        'periodo': {'da': da, 'a': a, 'giorni': len(record)},
        'prezzi': prezzi,
        'limite_contatore': limite_contatore,
    }
    if not record:
        out['vuoto'] = True
        return out

    out['totali'] = _totali(record, prezzi)

    # periodo precedente di pari durata, se e' coperto quasi tutto
    durata = (data(a) - data(da)).days + 1
    prima_a = (data(da) - datetime.timedelta(days=1)).isoformat()
    prima_da = (data(da) - datetime.timedelta(days=durata)).isoformat()
    prima = [giorni[g] for g in tutti if prima_da <= g <= prima_a]
    if len(prima) >= max(1, 0.8 * durata):
        out['precedente'] = dict(_totali(prima, prezzi), da=prima_da, a=prima_a)
    else:
        out['precedente'] = None

    giornalieri = [_giornaliero(r) for r in record]
    out['giorni'] = giornalieri
    # tutto l'archivio in piccolo, per il calendario: [giorno, sole, casa + auto, auto, prelievo, autosufficienza]
    del_periodo = {x['g']: x for x in giornalieri}
    out['calendario'] = []
    for g in tutti:
        x = del_periodo.get(g) or _giornaliero(giorni[g])
        out['calendario'].append([g, x['sole'], round(x['casa'] + x['auto'], 2), x['auto'], x['prelievo'],
                                  x['autosufficienza']])
    out['ore'] = {
        'sole': [[ora[SOLE] for ora in r['ore']] for r in record],
        'casa': [[ora[CONSUMO] - ora[AUTO] for ora in r['ore']] for r in record],
        'auto': [[ora[AUTO] for ora in r['ore']] for r in record],
        'auto_sole': [[ora[AUTO_SOLE] for ora in r['ore']] for r in record],
        'prelievo': [[ora[PRELIEVO] for ora in r['ore']] for r in record],
        'immessa': [[ora[IMMESSA] for ora in r['ore']] for r in record],
    }

    feriali = [r for r in record if data(r['g']).weekday() < 5]
    festivi = [r for r in record if data(r['g']).weekday() >= 5]
    out['profilo'] = {'tutti': _profilo(record), 'feriali': _profilo(feriali), 'festivi': _profilo(festivi)}

    settimana = []
    for giorno in range(7):
        del_giorno = [g for g in giornalieri if data(g['g']).weekday() == giorno]
        n = len(del_giorno)
        media = lambda chiave: round(sum(g[chiave] for g in del_giorno) / n, 2) if n else None
        settimana.append({'n': n, 'sole': media('sole'), 'casa': media('casa'), 'auto': media('auto'),
                          'prelievo': media('prelievo'), 'immessa': media('immessa')})
    out['settimana'] = settimana

    mesi = []
    for mese in sorted({r['g'][:7] for r in record}):
        del_mese = [r for r in record if r['g'].startswith(mese)]
        t = _totali(del_mese, prezzi)
        t['mese'] = mese
        t['profilo_sole'] = _profilo(del_mese)['sole']
        t['sole_medio'] = round(t['sole'] / len(del_mese), 2)
        mesi.append(t)
    out['mesi'] = mesi

    out['distribuzioni'] = {
        'passo_prelievo': PASSO_PRELIEVO_W,
        'prelievo': [round(sum(r['ip'][k] for r in record if k < len(r['ip']))) for k in range(CLASSI_PRELIEVO)],
        'passo_auto': PASSO_AUTO_W,
        'auto': [round(sum(r['ia'][k] for r in record if k < len(r['ia']))) for k in range(CLASSI_AUTO)],
        'minuti': sum(r['n'] for r in record),
        'picchi_prelievo': sorted(([r['pk']['prelievo'][0], r['pk']['prelievo'][1]] for r in record
                                   if r['pk']['prelievo'][0] > 0), reverse=True)[:5],
    }

    con_fasi = [r for r in record if r.get('fasi')]
    if con_fasi:
        massimi = []
        for k in range(3):
            r = max(con_fasi, key=lambda r: r['fmax'][k])
            massimi.append({'w': r['fmax'][k], 'giorno': r['g']})
        out['fasi'] = {
            'giorni': len(con_fasi),
            'kwh': [round(sum(r['fasi'][k] for r in con_fasi) / 1000.0, 1) for k in range(6)],
            'massimi': massimi,
        }
    else:
        out['fasi'] = None

    # la casa: carico di fondo (quel che resta acceso sempre) e ora di punta
    ore_casa = [ora[CONSUMO] - ora[AUTO] for r in record if r['n'] >= GIORNO_COMPLETO_MIN for ora in r['ore']]
    profilo = out['profilo']['tutti']
    out['casa'] = {
        'fondo_w': round(_percentile(ore_casa, 5)) if ore_casa else None,
        'ora_di_punta': max(range(24), key=lambda h: profilo['casa'][h]),
        'media_giorno': round(sum(g['casa'] for g in giornalieri) / len(giornalieri), 2),
        'media_feriale': round(sum(_somma_ore(r)[CONSUMO] - _somma_ore(r)[AUTO] for r in feriali)
                               / len(feriali) / 1000.0, 2) if feriali else None,
        'media_festivo': round(sum(_somma_ore(r)[CONSUMO] - _somma_ore(r)[AUTO] for r in festivi)
                               / len(festivi) / 1000.0, 2) if festivi else None,
    }
    produttive = [sum(1 for ora in r['ore'] if ora[SOLE] >= 50) for r in record]
    out['sole'] = {
        'media_giorno': round(sum(g['sole'] for g in giornalieri) / len(giornalieri), 2),
        'mediana_giorno': round(_mediana([g['sole'] for g in giornalieri]), 2),
        'ore_produttive': round(sum(produttive) / len(produttive), 1),
        'ora_di_punta': max(range(24), key=lambda h: profilo['sole'][h]),
        'picco_w': max(r['pk']['sole'][0] for r in record),
    }

    stat_ricariche = _statistiche_ricariche(record, prezzi)
    out['ricariche'] = stat_ricariche
    out['record'] = _record_del_periodo(record, giornalieri, stat_ricariche['elenco'])
    out['serie'] = _serie(giornalieri)
    return out


def intervallo(periodo, oggi, primo, da=None, a=None):
    """Da una scelta della pagina ai due giorni estremi (ISO, inclusi).

    periodo: '7g', '30g', '90g', '365g', 'anno' (da gennaio), 'tutto', oppure
    un mese 'AAAA-MM'. `da` e `a` espliciti hanno la precedenza.
    Solleva ValueError se la richiesta non si capisce.
    """
    if da or a:
        da, a = da or primo, a or oggi
        data(da), data(a)                  # ValueError se non sono date
        if da > a:
            raise ValueError("Il periodo finisce prima di cominciare.")
        return da, a
    periodo = (periodo or '30g').strip().lower()
    if periodo == 'tutto':
        return primo, oggi
    if periodo == 'anno':
        return oggi[:4] + '-01-01', oggi
    if periodo.endswith('g') and periodo[:-1].isdigit() and 1 <= int(periodo[:-1]) <= 3660:
        return (data(oggi) - datetime.timedelta(days=int(periodo[:-1]) - 1)).isoformat(), oggi
    if len(periodo) == 7 and periodo[4] == '-':
        inizio = data(periodo + '-01')
        fine = (inizio + datetime.timedelta(days=32)).replace(day=1) - datetime.timedelta(days=1)
        return inizio.isoformat(), fine.isoformat()
    raise ValueError(f"Periodo non valido: {periodo}")


def csv_giorni(stat):
    """La tabella dei giorni del periodo, per il foglio di calcolo."""
    righe = ['giorno;prodotta_kwh;casa_kwh;auto_kwh;auto_dal_sole_kwh;prelevata_kwh;immessa_kwh;'
             'autosufficienza_pct;autoconsumo_pct;picco_prelievo_w;picco_produzione_w;minuti_misurati']
    for g in stat.get('giorni') or []:
        celle = [g['g'], g['sole'], g['casa'], g['auto'], g['auto_sole'], g['prelievo'], g['immessa'],
                 g['autosufficienza'], g['autoconsumo'], g['picco_prelievo'], g['picco_sole'], g['minuti']]
        righe.append(';'.join('' if c is None else str(c).replace('.', ',') for c in celle))
    return '\n'.join(righe) + '\n'


# --- lettura dei file dei campioni (per l'importazione) -------------------------
def leggi_campioni(percorso):
    """Legge i due formati esistenti: le righe [ts, consumo, sole, auto, (fasi)]
    dello storico del Pi e i record al minuto dell'archivio sul server."""
    campioni = []
    with open(percorso, encoding='utf-8') as f:
        for riga in f:
            try:
                r = json.loads(riga)
            except ValueError:
                continue
            if isinstance(r, list) and len(r) >= 4:
                campioni.append((r[0], r[1], r[2], r[3], r[4:10] if len(r) >= 10 else None))
            elif isinstance(r, dict) and 'ts' in r and 'consumo_w' in r:
                campioni.append((r['ts'], r['consumo_w'], r.get('produzione_w') or 0,
                                 r.get('auto_w') or 0, r.get('fasi_w')))
    return campioni


def importa(uscita, sorgenti, fino_a=None):
    """Ricostruisce l'archivio dai file dei campioni. Se piu' file coprono lo
    stesso minuto vale il primo elencato."""
    visti, campioni = set(), []
    for percorso in sorgenti:
        for c in leggi_campioni(percorso):
            minuto = int(c[0] // 60)
            if minuto in visti:
                continue
            visti.add(minuto)
            campioni.append(c)
    giorni = aggrega(campioni)
    fino_a = fino_a or giorno_di(time.time())
    record = [r for g, r in sorted(giorni.items()) if g < fino_a]
    with open(uscita, 'w', encoding='utf-8') as f:
        for r in record:
            f.write(json.dumps(r, separators=(',', ':')) + '\n')
    return record


if __name__ == '__main__':
    if len(sys.argv) >= 4 and sys.argv[1] == 'importa':
        fatti = importa(sys.argv[2], sys.argv[3:])
        if fatti:
            print(f"{len(fatti)} giorni, dal {fatti[0]['g']} al {fatti[-1]['g']} -> {sys.argv[2]}")
        else:
            print("Nessun giorno trovato.")
    else:
        print(__doc__)
