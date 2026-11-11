"""Test automatici. Uso:  python -m unittest -v test_regolatore

I moduli puri (pianificatore, protezione, regolatore classico) si provano
ovunque. I test del servizio (sessioni, API web) richiedono le librerie del
venv del Pi (Flask, python-telegram-bot, ...): senza, vengono saltati.
Nessun test tocca la rete, la colonnina o i file del servizio: i file di
stato vanno in una cartella temporanea.
"""
import gzip
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from pianificatore import (Pianificatore, interpreta_orario, prossima_scadenza, valida_preset,
                           valida_programma, preset_attivo, PRESET_BASE)
from previsione import Previsione, fattore_nuvole
from protezione_contatore import prelevabile_effettivo, ProtezioneContatore
from regolatore_classico import RegolatoreClassico
import statistiche


class Orologio:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def config_base(**altro):
    cfg = {'POTENZA_PRELEVABILE': 1000, 'LIMITE_CONTATORE': 6600, 'MARGINE_CONTATORE': 300,
           'MODALITA': 'misto', 'MONOFASE_MIN_POWER': 1380, 'MONOFASE_MAX_POWER': 7360,
           'TRIFASE_MIN_POWER': 4140, 'TRIFASE_MAX_POWER': 22000, 'POTENZA_PROTEZIONE': 300,
           'TIMER_SPEGNIMENTO': 60, 'COOLDOWN_ACCENSIONE': 60, 'UPDATE_INTERVAL_S': 5,
           'SMOOTHING_ALPHA': 0.9, 'MAX_DELTA_PER_SEC': 1500, 'PID_FASE_MONOFASE': 0}
    cfg.update(altro)
    return cfg


def nuovo_pianificatore(cfg, minimo=1380, orologio=None):
    avvisi = []
    p = Pianificatore(cfg, minimo_colonnina=lambda: minimo, log=lambda m: None,
                      avvisa=avvisi.append, orologio=orologio or Orologio())
    return p, avvisi


class TestPianificatore(unittest.TestCase):
    def test_quota_per_modalita(self):
        cfg = config_base()
        p, _ = nuovo_pianificatore(cfg)
        self.assertEqual(p.quota_auto(0), 1000)
        cfg['MODALITA'] = 'sole'
        self.assertEqual(p.quota_auto(0), 0)
        cfg['MODALITA'] = 'preset'                 # senza elenco valgono quelli di serie
        self.assertEqual(p.quota_auto(0), 6600)
        cfg.update(PRESET=[{'nome': 'Notte', 'prelevabile': 4000, 'limite_kwh': 20},
                           {'nome': 'Rabbocco', 'prelevabile': 3000, 'limite_kwh': 10}], PRESET_ATTIVO=1)
        self.assertEqual(p.quota_auto(0), 3000)
        self.assertEqual(p.stato()['nome_modalita'], 'Preset Rabbocco')
        cfg['PRESET_ATTIVO'] = 7                    # indice rimasto da un elenco piu' lungo
        self.assertEqual(preset_attivo(cfg)['nome'], 'Notte')
        self.assertEqual(p.quota_auto(0), 4000)

    def test_preset_validi_e_no(self):
        ok, puliti = valida_preset([{'nome': '  Notte   piena ', 'prelevabile': '4000', 'limite_kwh': 20.4}])
        self.assertTrue(ok)
        self.assertEqual(puliti, [{'nome': 'Notte piena', 'prelevabile': 4000, 'limite_kwh': 20}])
        self.assertTrue(valida_preset(PRESET_BASE)[0])
        for sbagliato in ([], 'x', [{'nome': '', 'prelevabile': 1000}], [{'nome': 'A', 'prelevabile': 'tanto'}],
                          [{'nome': 'A', 'prelevabile': 30000}], [{'nome': 'A', 'prelevabile': 1000, 'limite_kwh': 200}],
                          [{'nome': 'A', 'prelevabile': 1}, {'nome': 'a', 'prelevabile': 2}],
                          [{'nome': str(n), 'prelevabile': 1000} for n in range(9)]):
            self.assertFalse(valida_preset(sbagliato)[0], sbagliato)

    def test_preset_veloce_resta_entro_il_contatore(self):
        cfg = config_base(MODALITA='preset')
        cfg['PIANIFICATORE'], _ = nuovo_pianificatore(cfg)
        # Casa a 2 kW senza sole: in tutto non oltre 6600 - 300
        self.assertEqual(prelevabile_effettivo(cfg, 2000), 6300)
        cfg['MODALITA'] = 'sole'
        self.assertEqual(prelevabile_effettivo(cfg, 2000), 2000)   # solo la casa
        self.assertEqual(prelevabile_effettivo(cfg, -3000), 0)     # il sole avanza

    def test_obiettivo_aspetta_il_minimo_poi_integra(self):
        orologio = Orologio()
        cfg = config_base(MODALITA='sole')
        # Trifase (minimo 4140): 10 kWh in 8 ore sono 1389 W medi, sotto il minimo
        p, _ = nuovo_pianificatore(cfg, minimo=4140, orologio=orologio)
        p.imposta_obiettivo(10, orologio.t + 8 * 3600)
        self.assertEqual(p.quota_auto(0), 0)
        self.assertFalse(p.in_recupero)
        # A 2,5 ore dalla scadenza ne servono 4444: si integra con la rete
        orologio.t += 5.5 * 3600
        quota = p.quota_auto(0)
        self.assertTrue(p.in_recupero)
        self.assertGreaterEqual(quota, 4140 + Pianificatore.MARGINE_ACCENSIONE)
        # Col sole che avanza ne serve meno dalla rete
        self.assertLess(p.quota_auto(-3000), quota)

    def test_obiettivo_resta_agganciato_fino_alla_fine(self):
        orologio = Orologio()
        cfg = config_base(MODALITA='sole')
        p, avvisi = nuovo_pianificatore(cfg, minimo=1380, orologio=orologio)
        p.imposta_obiettivo(10, orologio.t + 6 * 3600)       # 1852 W medi
        self.assertGreater(p.quota_auto(0), 1380)
        # Caricando al minimo la potenza necessaria scende sotto il minimo:
        # l'integrazione deve restare, o l'auto si spegnerebbe a ogni giro
        p.aggiungi_energia(8)
        orologio.t += 2 * 3600
        self.assertGreaterEqual(p.quota_auto(0), 1380 + Pianificatore.MARGINE_ACCENSIONE)
        p.aggiungi_energia(2.5)
        self.assertIsNone(p.obiettivo)
        self.assertEqual(p.quota_auto(0), 0)
        self.assertTrue(any('raggiunto' in a for a in avvisi))

    def test_obiettivo_scaduto(self):
        orologio = Orologio()
        p, avvisi = nuovo_pianificatore(config_base(), orologio=orologio)
        p.imposta_obiettivo(30, orologio.t + 3600)
        p.aggiungi_energia(5)
        orologio.t += 3601
        p.controlla()
        self.assertIsNone(p.obiettivo)
        self.assertTrue(any('non raggiunto' in a for a in avvisi))

    def test_obiettivo_valori_non_validi(self):
        orologio = Orologio()
        p, _ = nuovo_pianificatore(config_base(), orologio=orologio)
        self.assertFalse(p.imposta_obiettivo(0, orologio.t + 3600)[0])
        self.assertFalse(p.imposta_obiettivo(500, orologio.t + 3600)[0])
        self.assertFalse(p.imposta_obiettivo(10, orologio.t - 10)[0])
        self.assertFalse(p.imposta_obiettivo(10, orologio.t + 72 * 3600)[0])
        self.assertFalse(p.imposta_obiettivo('x', orologio.t + 3600)[0])

    def test_persistenza(self):
        orologio = Orologio()
        salvati = []
        p = Pianificatore(config_base(), log=lambda m: None, salva=salvati.append,
                          orologio=orologio)
        p.imposta_obiettivo(12, orologio.t + 5 * 3600)
        p.aggiungi_energia(2)
        q, _ = nuovo_pianificatore(config_base(), orologio=orologio)
        q.importa(salvati[-1])
        self.assertEqual(q.obiettivo['fatti'], 2)
        # Gia' scaduti al riavvio: non si riprendono
        orologio.t += 6 * 3600
        r, _ = nuovo_pianificatore(config_base(), orologio=orologio)
        r.importa(salvati[-1])
        self.assertIsNone(r.obiettivo)
        r.importa("spazzatura")
        r.importa({'obiettivo': {'kwh': 'x'}})
        self.assertIsNone(r.obiettivo)

    def test_salvataggio_che_fallisce_non_ferma_nulla(self):
        def rotto(_):
            raise OSError("disco pieno")
        p = Pianificatore(config_base(), log=lambda m: None, salva=rotto, orologio=Orologio())
        self.assertTrue(p.imposta_obiettivo(10, p.ora() + 3600)[0])

    def test_orari(self):
        self.assertEqual(interpreta_orario('7'), (7, 0))
        self.assertEqual(interpreta_orario('7:30'), (7, 30))
        self.assertEqual(interpreta_orario('07.45'), (7, 45))
        self.assertEqual(interpreta_orario('730'), (7, 30))
        self.assertEqual(interpreta_orario('1830'), (18, 30))
        for sbagliato in ('25:00', '7:61', 'domani', ''):
            self.assertIsNone(interpreta_orario(sbagliato), sbagliato)

    def test_prossima_scadenza(self):
        adesso = time.mktime((2026, 11, 10, 20, 0, 0, 0, 0, -1))
        domani = prossima_scadenza(7, 30, adesso)
        self.assertEqual(time.localtime(domani)[2:5], (11, 7, 30))
        stasera = prossima_scadenza(22, 0, adesso)
        self.assertEqual(time.localtime(stasera)[2:5], (10, 22, 0))


class TestProtezione(unittest.TestCase):
    def test_spegne_se_anche_il_minimo_supera_il_limite(self):
        cfg = config_base()
        wb = FintaWallbox(cfg, fase=1, setpoint=6000)
        p = ProtezioneContatore(cfg, log=lambda m: None)
        p.aggiorna_fasi([2000, 2000, 2000 + 1500], 6000, True)   # casa 1,5 kW + auto 6 kW
        self.assertFalse(p.controlla(wb, sole=2000))             # 5,5 kW: sotto il limite
        p.aggiorna_fasi([3000, 3000, 6000], 6000, True)           # casa 6 kW
        self.assertTrue(p.controlla(wb, sole=0))
        self.assertFalse(wb.is_on)

    def test_riduce_l_auto(self):
        cfg = config_base()
        wb = FintaWallbox(cfg, fase=1, setpoint=6000)
        p = ProtezioneContatore(cfg, log=lambda m: None)
        p.aggiorna_fasi([2200, 2200, 3600], 6000, True)           # casa 2 kW + auto 6 kW
        self.assertTrue(p.controlla(wb, sole=0))
        self.assertTrue(wb.is_on)
        self.assertEqual(wb.current_set_power, 4300)              # 6300 - 2000


class FintaWallbox:
    """Il minimo di WallboxController che serve a regolatori e protezione."""

    def __init__(self, cfg, fase=0, setpoint=0):
        self.cfg, self.fase = cfg, fase
        self.is_on = setpoint > 0
        self.current_set_power = setpoint
        self.display_power = float(setpoint)
        self.pending_off_until = 0
        self.max_reached_start = None
        self.max_notified = False
        self.tetto_contatore = None
        self.accensioni = 0

    def limiti_potenza(self):
        if self.fase == 0:
            return self.cfg['MONOFASE_MIN_POWER'], self.cfg['MONOFASE_MAX_POWER']
        return self.cfg['TRIFASE_MIN_POWER'], self.cfg['TRIFASE_MAX_POWER']

    def potenza_reale(self):
        return self.current_set_power if self.is_on else None

    def set_power(self, watts, bypass):
        minimo, massimo = self.limiti_potenza()
        if self.tetto_contatore is not None:
            massimo = max(minimo, min(massimo, self.tetto_contatore))
        self.current_set_power = int(max(minimo, min(massimo, watts)))
        self.display_power = float(self.current_set_power)

    def turn_on(self):
        if self.is_on:
            return False
        self.set_power(self.limiti_potenza()[0], True)
        self.is_on = True
        self.accensioni += 1
        return True

    def turn_off(self, force=False, urgente=False):
        self.is_on = False


class TestClassicoConPianificatore(unittest.TestCase):
    """Di notte, casa a 500 W, colonnina monofase."""

    def passo_notte(self, modalita, **altro):
        orologio = Orologio()
        cfg = config_base(MODALITA=modalita, **altro)
        cfg['PIANIFICATORE'], _ = nuovo_pianificatore(cfg, orologio=orologio)
        wb = FintaWallbox(cfg, fase=0)
        reg = RegolatoreClassico(cfg, log=lambda m: None, orologio=orologio)
        reg.passo(wb, sole=0, consumo=500, casa=500)
        return wb, cfg

    def test_solo_sole_non_accende(self):
        wb, _ = self.passo_notte('sole')
        self.assertFalse(wb.is_on)

    def test_misto_accende_se_il_prelevabile_basta(self):
        self.assertFalse(self.passo_notte('misto', POTENZA_PRELEVABILE=1000)[0].is_on)
        self.assertTrue(self.passo_notte('misto', POTENZA_PRELEVABILE=2000)[0].is_on)

    def test_preset_accende(self):
        self.assertTrue(self.passo_notte('preset')[0].is_on)
        debole = [{'nome': 'Poco', 'prelevabile': 1000, 'limite_kwh': 0}]
        self.assertFalse(self.passo_notte('preset', PRESET=debole)[0].is_on)

    def regolatore_calmo(self, setpoint=3000, **altro):
        orologio = Orologio()
        cfg = config_base(**dict(dict(REGOLAZIONE_CALMA=True, CALMA_SOGLIA_W=500, CALMA_INTERVALLO_S=30), **altro))
        wb = FintaWallbox(cfg, fase=0, setpoint=setpoint)
        righe = []
        return RegolatoreClassico(cfg, log=righe.append, orologio=orologio), wb, orologio, righe

    def test_calma_salta_i_cambi_piccoli_e_ravvicinati(self):
        reg, wb, orologio, righe = self.regolatore_calmo()
        reg._imposta(wb, 3300, False, "Aumento")           # sotto la soglia
        self.assertEqual(wb.current_set_power, 3000)
        reg._imposta(wb, 3600, False, "Aumento")           # sopra: passa
        self.assertEqual(wb.current_set_power, 3600)
        orologio.t += 10
        reg._imposta(wb, 4300, False, "Aumento")           # troppo presto
        reg._imposta(wb, 3000, False, "Diminuisco")        # calo normale: troppo presto anche lui
        self.assertEqual(wb.current_set_power, 3600)
        reg._imposta(wb, 2500, False, "Diminuisco")        # calo grosso (due soglie): subito
        self.assertEqual(wb.current_set_power, 2500)
        orologio.t += 31
        reg._imposta(wb, 3100, False, "Diminuisco")        # passato l'intervallo
        self.assertEqual(wb.current_set_power, 3100)
        self.assertEqual(reg.cambi_evitati, 3)
        # il verbo del registro segue il verso vero del cambio
        self.assertIn("Aumento: 2500W -> 3100W", righe[-1])
        self.assertIn("Diminuisco: 3600W -> 2500W", righe[-2])

    def test_senza_calma_passa_tutto(self):
        reg, wb, _, _ = self.regolatore_calmo(REGOLAZIONE_CALMA=False)
        reg._imposta(wb, 3100, False, "Aumento")
        self.assertEqual(wb.current_set_power, 3100)
        # i comandi forzati (minimo, massimo) passano anche in calma
        reg, wb, _, _ = self.regolatore_calmo()
        reg._imposta(wb, 3100, True, "Aumento al massimo")
        self.assertEqual(wb.current_set_power, 3100)

    def test_programma_settimanale(self):
        # lunedi' 15 giugno 2026, ore 10
        orologio = Orologio(mezzanotte() + 10 * 3600)
        cfg = config_base(MODALITA='sole')
        p, avvisi = nuovo_pianificatore(cfg, orologio=orologio)
        ok, _ = p.imposta_programma([{'giorni': [0, 1, 2, 3, 4], 'ora': '7:00', 'kwh': 15, 'attivo': True},
                                     {'giorni': [5], 'ora': '09:30', 'kwh': 30, 'attivo': False}])
        self.assertTrue(ok)
        # la prossima e' martedi' alle 7: fra 21 ore, quindi e' gia' l'obiettivo
        self.assertEqual(p.obiettivo['kwh'], 15)
        self.assertTrue(p.obiettivo['ricorrente'])
        martedi = mezzanotte() + 86400 + 7 * 3600
        self.assertEqual(p.obiettivo['entro'], martedi)
        # annullarlo salta solo martedi': il prossimo e' mercoledi', che parte 24 ore prima
        p.annulla_obiettivo()
        p.controlla()
        self.assertIsNone(p.obiettivo)
        self.assertEqual(p.stato()['prossima_programma']['entro'], martedi + 86400)
        orologio.t = martedi + 60
        p.controlla()
        self.assertEqual(p.obiettivo['entro'], martedi + 86400)
        # auto mai vista: alla scadenza niente avviso, e riparte quello di giovedi'
        orologio.t = martedi + 86400 + 1
        p.controlla()
        self.assertEqual(avvisi, [])
        self.assertEqual(p.obiettivo['entro'], martedi + 2 * 86400)
        # l'auto carica 6 kWh e poi viene staccata: l'obiettivo del programma resta e riparte da zero
        p.aggiungi_energia(6)
        self.assertFalse(p.auto_scollegata())
        self.assertEqual((p.obiettivo['fatti'], p.obiettivo['ricorrente']), (0.0, True))
        # un obiettivo messo a mano ha la precedenza, e lo scollegamento lo annulla
        p.imposta_obiettivo(5, orologio.t + 3600)
        p.controlla()
        self.assertFalse(p.obiettivo['ricorrente'])
        self.assertTrue(p.auto_scollegata())
        self.assertIsNone(p.obiettivo)
        p.controlla()
        self.assertTrue(p.obiettivo['ricorrente'])     # e il programma riprende il suo posto
        # raggiunto col sole, poi l'auto esce: al rientro va ricaricata, quindi torna attivo
        p.aggiungi_energia(15)
        self.assertIsNone(p.obiettivo)
        self.assertFalse(p.auto_scollegata())
        self.assertEqual((p.obiettivo['entro'], p.obiettivo['fatti']), (martedi + 2 * 86400, 0.0))
        # persistenza
        q, _ = nuovo_pianificatore(cfg, orologio=orologio)
        q.importa(p.esporta())
        self.assertEqual(len(q.programma), 2)
        self.assertEqual(q.programma[0]['ora'], '07:00')
        # regole sbagliate
        for sbagliata in ([{'giorni': [], 'ora': '7:00', 'kwh': 10}], [{'giorni': [9], 'ora': '7:00', 'kwh': 10}],
                          [{'giorni': [1], 'ora': '25:00', 'kwh': 10}], [{'giorni': [1], 'ora': '7:00', 'kwh': 0}], 'x'):
            self.assertFalse(valida_programma(sbagliata)[0], sbagliata)
        self.assertTrue(p.imposta_programma([])[0])

    def test_obiettivo_aspetta_il_sole_previsto(self):
        orologio = Orologio()
        cfg = config_base(MODALITA='sole')
        p, _ = nuovo_pianificatore(cfg, orologio=orologio)
        p.imposta_obiettivo(20, orologio.t + 10 * 3600)
        # senza previsione: 20 kWh in 10 ore sono 2222 W medi, sopra il minimo -> rete subito
        self.assertGreater(p.quota_auto(500), 0)
        self.assertTrue(p.in_recupero)
        # con 20 kWh di sole previsti (se ne contano 14): dalla rete ne servono 6, cioe' 667 W: si aspetta
        p.imposta_obiettivo(20, orologio.t + 10 * 3600)
        p.sole_atteso = lambda da, a: 20.0
        self.assertEqual(p.quota_auto(500), 0)
        self.assertFalse(p.in_recupero)
        self.assertEqual(p.stato()['obiettivo']['sole_previsto_kwh'], 20.0)
        self.assertAlmostEqual(p.stato()['obiettivo']['potenza_necessaria'], 667, delta=1)
        # il sole non e' arrivato e manca poco: la rete da sola farebbe fatica, si smette di aspettare
        orologio.t += 5.5 * 3600
        self.assertGreater(p.quota_auto(500), 3000)
        self.assertTrue(p.in_recupero)
        # una previsione che si rompe non ferma la carica
        p.imposta_obiettivo(20, orologio.t + 10 * 3600)
        p.sole_atteso = lambda da, a: 1 / 0
        self.assertGreater(p.quota_auto(500), 0)

    def test_obiettivo_accende_di_notte(self):
        orologio = Orologio()
        cfg = config_base(MODALITA='sole')
        cfg['PIANIFICATORE'], _ = nuovo_pianificatore(cfg, orologio=orologio)
        cfg['PIANIFICATORE'].imposta_obiettivo(10, orologio.t + 5 * 3600)
        wb = FintaWallbox(cfg, fase=0)
        RegolatoreClassico(cfg, log=lambda m: None, orologio=orologio).passo(
            wb, sole=0, consumo=500, casa=500)
        self.assertTrue(wb.is_on)


# ---------------------------------------------------------------------------
# Servizio (richiede le librerie del venv del Pi)
# ---------------------------------------------------------------------------
def mezzanotte(anno=2026, mese=6, giorno=15):
    return time.mktime((anno, mese, giorno, 0, 0, 0, 0, 0, -1))


def giornata(inizio, consumo=lambda ora: 1000, sole=lambda ora: 0, auto=lambda ora: 0, minuti=1440):
    """Un campione al minuto; le funzioni ricevono l'ora del giorno (decimale)."""
    return [(inizio + m * 60, consumo(m / 60), sole(m / 60), auto(m / 60), None) for m in range(minuti)]


class TestPrevisione(unittest.TestCase):
    def storia(self, giorni=6, sole=lambda h: 4000 if 10 <= h < 16 else 0, **altro):
        campioni = []
        for n in range(giorni):
            campioni += giornata(mezzanotte() + n * 86400, sole=sole, **altro)
        return statistiche.aggrega(campioni)

    def test_senza_storia_non_si_prevede(self):
        p = Previsione(orologio=lambda: mezzanotte())
        self.assertIsNone(p.sole_per_auto(mezzanotte(), mezzanotte() + 86400))
        self.assertEqual(p.riepilogo(), {'fonte': None})

    def test_dallo_storico(self):
        adesso = mezzanotte() + 6 * 86400 + 60          # il settimo giorno, appena dopo mezzanotte
        p = Previsione(orologio=lambda: adesso)
        p.aggiorna_storia(self.storia())
        self.assertEqual(p.fonte(), 'storico')
        r = p.riepilogo()
        self.assertEqual(r['oggi_restante_kwh'], 24.0)      # sei ore a 4 kW
        self.assertEqual(r['domani_kwh'], 24.0)
        self.assertEqual(r['oggi_restante_auto_kwh'], 18.0) # la casa da 1 kW passa prima
        # solo un pezzo della giornata, anche con ore a meta'
        self.assertAlmostEqual(p.sole_per_auto(adesso + 10.5 * 3600 - 60, adesso + 12 * 3600 - 60), 4.5)

    def test_col_meteo_e_con_le_nuvole(self):
        adesso = mezzanotte() + 6 * 86400 + 60
        p = Previsione(orologio=lambda: adesso)
        p.aggiorna_storia(self.storia())
        ore = [{'t': adesso - 60 + h * 3600, 'nuvole': 100} for h in range(48)]
        self.assertEqual(p.imposta_meteo(ore + [{'t': 'boh', 'nuvole': 5}, {'nuvole': 5}]), 48)
        self.assertEqual(p.fonte(), 'meteo')
        self.assertAlmostEqual(fattore_nuvole(100), 0.15)
        self.assertAlmostEqual(fattore_nuvole(100, 0.75), 0.25)
        self.assertAlmostEqual(fattore_nuvole(0), 1.0)
        self.assertEqual(p.riepilogo()['domani_kwh'], 3.6)   # il 15% del sereno
        self.assertEqual(p.nuvole_di_domani(), 100.0)
        # la perdita si impara dai giorni nuvolosi gia' passati: qui hanno reso il 10% e il 12%
        self.assertEqual(p.impara_perdita([(100, 7.2, 72)]), 0.85)             # un giorno solo non basta
        self.assertAlmostEqual(p.impara_perdita([(100, 7.2, 72), (100, 8.64, 72), (20, 70, 72), ('x', 1, 1)]), 0.89)
        self.assertEqual(p.giorni_imparati, 2)
        self.assertAlmostEqual(p.riepilogo()['domani_kwh'], 2.6, places=1)
        self.assertEqual(p.impara_perdita([(100, 60, 72), (100, 65, 72)]), 0.6)   # mai sotto 0,6
        p.impara_perdita([])
        # data in formato ISO, come la manda Home Assistant
        self.assertEqual(p.imposta_meteo([{'t': '2026-06-21T10:00:00+00:00', 'nuvole': 0}]), 1)
        # un meteo vecchio non si usa piu'
        p.meteo_del = adesso - 12 * 3600
        self.assertEqual(p.fonte(), 'storico')

    def test_correzione_con_la_mattina_di_oggi(self):
        # oggi fino alle 13 il tetto ha reso la meta' del solito
        giorni = self.storia()
        oggi = statistiche.aggrega(giornata(mezzanotte() + 6 * 86400, minuti=13 * 60,
                                            sole=lambda h: 2000 if 10 <= h < 16 else 0))
        giorni.update(oggi)
        adesso = mezzanotte() + 6 * 86400 + 13 * 3600
        p = Previsione(orologio=lambda: adesso)
        p.aggiorna_storia(giorni)
        self.assertAlmostEqual(p.correzione_di_oggi(), 0.5)
        r = p.riepilogo()
        self.assertEqual(r['oggi_restante_kwh'], 6.0)        # tre ore a 2 kW invece di 4
        self.assertEqual(r['domani_kwh'], 24.0)              # domani no


class TestStatistiche(unittest.TestCase):
    PREZZI = {'acquisto': 0.25, 'vendita': 0.10}

    def test_bilancio_di_un_giorno(self):
        g = statistiche.aggrega(giornata(mezzanotte(), sole=lambda h: 3000 if 10 <= h < 14 else 0))
        r = g['2026-06-15']
        self.assertEqual(r['n'], 1440)
        sole, consumo, auto, prelievo, immessa, auto_sole = statistiche._somma_ore(r)
        self.assertEqual((sole, consumo, auto, prelievo, immessa), (12000, 24000, 0, 20000, 8000))
        self.assertEqual(r['ore'][11], [3000, 1000, 0, 0, 2000, 0])
        self.assertEqual(r['ore'][3], [0, 1000, 0, 1000, 0, 0])
        self.assertEqual(r['pk']['sole'][0], 3000)
        self.assertEqual(r['pk']['prelievo'][0], 1000)
        self.assertEqual(sum(r['ip']), 1200)            # venti ore di prelievo, quattro in immissione
        self.assertEqual(r['ip'][2], 1200)              # tutte fra 1000 e 1500 W
        self.assertIsNone(r['fasi'])

    def test_il_rumore_notturno_non_e_produzione(self):
        r = statistiche.aggrega(giornata(mezzanotte(), sole=lambda h: 32))['2026-06-15']
        self.assertEqual(statistiche._somma_ore(r)[statistiche.SOLE], 0)

    def test_all_auto_va_il_sole_che_avanza_alla_casa(self):
        carica = lambda h: 2000 if 12 <= h < 14 else 0
        r = statistiche.aggrega(giornata(
            mezzanotte(), consumo=lambda h: 1000 + carica(h), sole=lambda h: 2500, auto=carica))['2026-06-15']
        tot = statistiche._somma_ore(r)
        self.assertEqual(tot[statistiche.AUTO], 4000)
        self.assertEqual(tot[statistiche.AUTO_SOLE], 3000)          # 2500 di sole - 1000 di casa, per due ore
        self.assertEqual(len(r['ric']), 1)
        self.assertEqual(r['ric'][0][2:], [4000, 3000, 2000])
        self.assertEqual(r['ia'][2], 120)

    def test_valore_fermo_a_colonnina_spenta(self):
        # l'auto carica un'ora a 3 kW, poi la colonnina viene spenta dal quadro:
        # il dichiarato resta 3000 ma il consumo torna quello della casa
        reale = lambda h: 3000 if 20 <= h < 21 else 0
        r = statistiche.aggrega(giornata(
            mezzanotte(), consumo=lambda h: 800 + reale(h), auto=lambda h: 3000 if h >= 20 else 0))['2026-06-15']
        self.assertEqual(statistiche._somma_ore(r)[statistiche.AUTO], 3000)
        # anche se dopo la casa consuma tanto, un valore fermo non riparte
        r = statistiche.aggrega(giornata(
            mezzanotte(), consumo=lambda h: 800 + reale(h) + (2500 if h >= 22 else 0),
            auto=lambda h: 3000 if h >= 20 else 0))['2026-06-15']
        self.assertEqual(statistiche._somma_ore(r)[statistiche.AUTO], 3000)

    def test_dichiarato_oltre_il_misurato(self):
        # trifase: la colonnina dichiara 22 kW, il sensore ne misura 11,5 in tutto
        r = statistiche.aggrega(giornata(
            mezzanotte(), consumo=lambda h: 11500 if 10 <= h < 12 else 500,
            auto=lambda h: 22000 if 10 <= h < 12 else 0))['2026-06-15']
        self.assertEqual(statistiche._somma_ore(r)[statistiche.AUTO], 22000)   # (11500 - 500) x 2 h
        self.assertEqual(r['pk']['auto'][0], 11000)

    def test_buchi_e_doppioni(self):
        t = mezzanotte()
        campioni = [(t, 6000, 0, 0, None), (t, 6000, 0, 0, None),        # doppione
                    (t + 60, 6000, 0, 0, None), (t + 3600, 6000, 0, 0, None)]   # buco di un'ora
        r = statistiche.aggrega(campioni)['2026-06-15']
        self.assertEqual(r['n'], 3)
        self.assertEqual(statistiche._somma_ore(r)[statistiche.CONSUMO], 300)

    def test_fasi(self):
        t = mezzanotte()
        campioni = [(t + m * 60, 1200, 600, 0, [600, 400, 200, 200, 200, 200]) for m in range(60)]
        r = statistiche.aggrega(campioni)['2026-06-15']
        self.assertEqual(r['fasi'], [600, 400, 200, 200, 200, 200])
        self.assertEqual(r['fmax'], [600, 400, 200])
        self.assertEqual(r['nf'], 60)

    def test_ricarica_a_cavallo_della_mezzanotte(self):
        t = mezzanotte()
        carica = lambda h: 2000 if h >= 23 else 0
        ieri = giornata(t - 86400, consumo=lambda h: 500 + carica(h), auto=carica)
        oggi = giornata(t, consumo=lambda h: 500 + (2000 if h < 1 else 0) + (1500 if 8 <= h < 8.05 else 0),
                        auto=lambda h: 2000 if h < 1 else (1500 if 8 <= h < 8.05 else 0))
        giorni = statistiche.aggrega(ieri + oggi)
        elenco, brevi = statistiche.ricariche(giorni.values())
        self.assertEqual(len(elenco), 1)
        self.assertEqual(brevi, 1)                      # i tre minuti delle 8
        self.assertEqual(elenco[0]['kwh'], 4.0)
        self.assertEqual(elenco[0]['minuti'], 120)
        self.assertEqual(elenco[0]['potenza_media'], 2000)

    def test_archivio(self):
        t = mezzanotte()
        campioni = giornata(t - 86400) + giornata(t, minuti=600)
        with tempfile.TemporaryDirectory() as d:
            percorso = os.path.join(d, 'stat.jsonl')
            archivio = statistiche.Archivio(percorso)
            self.assertEqual(archivio.giorni(), {})
            self.assertEqual(archivio.archivia(campioni, oggi='2026-06-15'), ['2026-06-14'])
            self.assertEqual(archivio.archivia(campioni, oggi='2026-06-15'), [])    # gia' fatto
            with open(percorso, 'a') as f:
                f.write('{"g": "2026-06-1')                                          # riga troncata
            riletto = statistiche.Archivio(percorso).giorni()
            self.assertEqual(list(riletto), ['2026-06-14'])
            self.assertEqual(riletto['2026-06-14']['n'], 1440)

    def test_intervallo(self):
        i = statistiche.intervallo
        self.assertEqual(i('7g', '2026-11-10', '2026-09-14'), ('2026-11-04', '2026-11-10'))
        self.assertEqual(i('tutto', '2026-11-10', '2026-09-14'), ('2026-09-14', '2026-11-10'))
        self.assertEqual(i('anno', '2026-11-10', '2025-09-14'), ('2026-01-01', '2026-11-10'))
        self.assertEqual(i('2026-02', '2026-11-10', '2026-01-01'), ('2026-02-01', '2026-02-28'))
        self.assertEqual(i(None, '2026-11-10', '2026-01-01', da='2026-03-01', a='2026-03-05'),
                         ('2026-03-01', '2026-03-05'))
        for sbagliato in ('ieri', '0g', '2026-13', 'g'):
            with self.assertRaises(ValueError, msg=sbagliato):
                i(sbagliato, '2026-11-10', '2026-01-01')
        with self.assertRaises(ValueError):
            i(None, '2026-11-10', '2026-01-01', da='2026-03-09', a='2026-03-05')
        with self.assertRaises(ValueError):
            i(None, '2026-11-10', '2026-01-01', da='domani')

    def test_calcola(self):
        t = mezzanotte()
        carica = lambda h: 2000 if 12 <= h < 14 else 0
        campioni = []
        for n in range(14):
            campioni += giornata(t + n * 86400, consumo=lambda h: 1000 + carica(h),
                                 sole=lambda h: 4000 if 10 <= h < 16 else 0, auto=carica)
        giorni = statistiche.aggrega(campioni)
        s = statistiche.calcola(giorni, '2026-06-22', '2026-06-28', self.PREZZI)
        self.assertEqual(s['periodo'], {'da': '2026-06-22', 'a': '2026-06-28', 'giorni': 7})
        self.assertEqual(s['copertura']['giorni'], 14)
        tot = s['totali']
        # al giorno: 24 kWh di sole, 24 di casa, 4 d'auto; 10 autoconsumati, 14 immessi, 18 prelevati
        self.assertEqual((tot['sole'], tot['casa'], tot['auto'], tot['immessa'], tot['prelievo']),
                         (168, 168, 28, 98, 126))
        self.assertEqual(tot['auto_sole'], 28)
        self.assertEqual(tot['casa_sole'], 42)
        self.assertAlmostEqual(tot['autoconsumo_pct'], 41.7)
        self.assertAlmostEqual(tot['autosufficienza_pct'], 35.7)
        self.assertEqual(tot['euro']['spesa'], 31.5)
        self.assertEqual(tot['euro']['ricavo'], 9.8)
        self.assertEqual(tot['euro']['beneficio'], round(196 * 0.25 - (31.5 - 9.8), 2))
        self.assertEqual(s['precedente']['sole'], 168)
        self.assertEqual(len(s['giorni']), 7)
        self.assertEqual(len(s['ore']['sole']), 7)
        self.assertEqual(len(s['calendario']), 14)                   # tutto l'archivio, non solo il periodo
        self.assertEqual(s['calendario'][0], ['2026-06-15', 24.0, 28.0, 4.0, 18.0, 35.7])
        self.assertEqual(s['profilo']['tutti']['sole'][12], 4000)
        self.assertEqual(s['profilo']['tutti']['auto'][12], 2000)
        self.assertEqual(s['profilo']['feriali']['giorni'], 5)
        self.assertEqual(s['settimana'][0], {'n': 1, 'sole': 24, 'casa': 24, 'auto': 4,
                                             'prelievo': 18, 'immessa': 14})
        self.assertEqual(s['mesi'][0]['mese'], '2026-06')
        self.assertEqual(s['ricariche']['n'], 7)
        self.assertEqual(s['ricariche']['ist_ora'][12], 7)
        self.assertEqual(s['casa']['fondo_w'], 1000)
        self.assertEqual(s['serie']['giorni_con_ricarica'], 7)
        chiavi = {r['chiave'] for r in s['record']}
        self.assertTrue({'giorno_sole_max', 'picco_sole', 'picco_prelievo', 'ricarica_max'} <= chiavi)
        # senza periodo precedente coperto non si confronta
        self.assertIsNone(statistiche.calcola(giorni, '2026-06-15', '2026-06-28', self.PREZZI)['precedente'])
        # periodo senza dati, archivio vuoto
        self.assertTrue(statistiche.calcola(giorni, '2026-06-01', '2026-06-10', self.PREZZI).get('vuoto'))
        self.assertIsNone(statistiche.calcola({}, '2026-06-01', '2026-06-10', self.PREZZI)['copertura'])
        self.assertIn('2026-06-22;24', statistiche.csv_giorni(s).replace('.0;', ';'))

    def test_importa_dai_due_formati(self):
        t = mezzanotte()
        with tempfile.TemporaryDirectory() as d:
            vecchio, nuovo, uscita = (os.path.join(d, n) for n in ('a.jsonl', 'b.jsonl', 'u.jsonl'))
            with open(vecchio, 'w') as f:
                for m in range(1440):
                    f.write(json.dumps([int(t + m * 60), 1000, 0, 0]) + '\n')
            with open(nuovo, 'w') as f:
                for m in range(1380, 2880):         # si sovrappone di un'ora al primo
                    f.write(json.dumps({'ts': int(t + m * 60), 'consumo_w': 2000, 'produzione_w': 0,
                                        'auto_w': 0, 'fasi_w': [1000, 500, 500, 0, 0, 0]}) + '\n')
            fatti = statistiche.importa(uscita, [vecchio, nuovo], fino_a='2026-06-17')
            self.assertEqual([r['g'] for r in fatti], ['2026-06-15', '2026-06-16'])
            self.assertEqual(statistiche._somma_ore(fatti[0])[statistiche.CONSUMO], 24000)   # vale il primo file
            self.assertEqual(fatti[1]['fasi'][0], 24000)
            self.assertEqual(len(statistiche.Archivio(uscita).giorni()), 2)


# ---------------------------------------------------------------------------
try:
    import solar_webinterface as sw
except ImportError as e:      # Flask, telegram... non installati
    sw = None
    MOTIVO = f"librerie del servizio non installate ({e.name})"
else:
    MOTIVO = ""


@unittest.skipIf(sw is None, MOTIVO)
class TestServizio(unittest.TestCase):
    def setUp(self):
        self.cartella = tempfile.TemporaryDirectory()
        d = self.cartella.name
        self.patch = mock.patch.multiple(
            sw, FILE_CONFIG=os.path.join(d, 'config.json'),
            FILE_SESSIONI=os.path.join(d, 'sessioni.json'),
            FILE_STORICO_GIORNALIERO=os.path.join(d, 'giorni.json'),
            FILE_STATO_ENERGIA=os.path.join(d, 'energia.json'),
            FILE_PIANO=os.path.join(d, 'piano.json'),
            FILE_STORICO=os.path.join(d, 'storico.jsonl'),
            FILE_PREVISIONI=os.path.join(d, 'previsioni.jsonl'),
            salva_config=lambda: True,
            archivio_statistiche=statistiche.Archivio(os.path.join(d, 'statistiche.jsonl')),
            ponte_mqtt_instance=None, wallbox_instance=None,
            notifica=lambda *a, **k: None, log_msg=lambda m: None)
        self.patch.start()
        self.config_prima = dict(sw.CONFIG)
        sw.contatori_instance = sw.ContatoriEnergia()
        sw.pianificatore_instance, _ = nuovo_pianificatore(sw.CONFIG, orologio=time.time)

    def tearDown(self):
        sw.CONFIG.clear()
        sw.CONFIG.update(self.config_prima)
        sw.contatori_instance = None
        sw.pianificatore_instance = None
        self.patch.stop()
        self.cartella.cleanup()

    def test_incrementi_del_contatore_della_centralina(self):
        c = sw.contatori_instance
        c.inizio_sessione()
        with mock.patch.object(sw.time, 'time', side_effect=[1000, 1060, 1120, 1180, 1240]):
            self.assertEqual(c.energia_centralina(3.0, False), 0)       # primo riferimento
            self.assertAlmostEqual(c.energia_centralina(3.1, False), 0.1)
            self.assertAlmostEqual(c.energia_centralina(0.05, False), 0.05)  # cavo staccato: riparte
            self.assertEqual(c.energia_centralina(40.0, False), 0)      # salto impossibile
            self.assertAlmostEqual(c.energia_centralina(40.1, False), 0.1)
        self.assertAlmostEqual(c.kwh_sessione(), 0.25)

    def test_sessione_a_cavallo_della_mezzanotte(self):
        c = sw.contatori_instance
        t = time.time()
        c.giorno = '2000-01-01'          # il prossimo aggiorna() fa il cambio di giorno
        c.wallbox_wh = 9000.0            # contatori di "ieri" piu' alti della sessione
        c.wallbox_da_fv_wh = 4000.0
        c.ultimo_t = t - 3600
        with mock.patch.object(sw.time, 'time', return_value=t - 3600):
            c.inizio_sessione()
        c.aggiorna(solare_w=0, rete_w=3500, wallbox_w=3000, casa_w=500, ora=t - 3540)
        for i in range(1, 60):
            c.aggiorna(solare_w=0, rete_w=3500, wallbox_w=3000, casa_w=500, ora=t - 3540 + 60 * i)
        c.fine_sessione()
        s = sw.leggi_sessioni(1)[0]
        self.assertEqual(s['quota_fv'], 0.0)        # di notte: niente sole, e mai negativa
        self.assertAlmostEqual(s['kwh'], 3.0, places=2)

    def test_sessione_chiusa_se_la_centralina_sparisce(self):
        wb = sw.WallboxController()
        c = sw.contatori_instance
        c.inizio_sessione()
        c.sessione['inizio'] -= 1800
        wb.in_carica = True
        wb.ultima_carica_vista = time.time() - 700
        wb.sync_falliti = 3
        wb.offline_dal = time.time() - 700
        wb._lettura_fallita()
        self.assertIsNone(c.sessione)
        self.assertFalse(wb.in_carica)
        s = sw.leggi_sessioni(1)[0]
        self.assertLessEqual(s['fine'], time.time() - 690)

    def test_niente_accensione_se_la_centralina_non_risponde(self):
        wb = sw.WallboxController()
        wb.sync_falliti = 3
        with mock.patch.object(wb, 'send_command') as invio:
            self.assertFalse(wb.turn_on())
            invio.assert_not_called()
        wb.sync_falliti = 0
        with mock.patch.object(wb, 'send_command', return_value=True) as invio:
            self.assertTrue(wb.turn_on())
            self.assertEqual(invio.call_count, 2)       # potenza minima, poi accensione

    def test_api_comando_e_pagina(self):
        client = sw.app.test_client()
        r = client.post('/api/comando', json={'nome': 'modalita', 'valore': 'sole'})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(sw.CONFIG['MODALITA'], 'sole')
        r = client.post('/api/comando', json={'nome': 'modalita', 'valore': 'turbo'})
        self.assertEqual(r.status_code, 400)
        r = client.post('/api/comando', json={'nome': 'obiettivo', 'valore': '12 7:30'})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()['piano']['obiettivo']['kwh'], 12)
        r = client.post('/api/comando', json={'nome': 'obiettivo', 'valore': 'off'})
        self.assertIsNone(r.get_json()['piano']['obiettivo'])
        r = client.post('/api/comando', json={'nome': 'boost', 'valore': '30'})     # tolto il 10/11/2026
        self.assertEqual(r.status_code, 400)
        r = client.post('/api/comando', json={'nome': 'calma', 'valore': 'on'})
        self.assertTrue(r.get_json()['piano']['calma'])
        self.assertTrue(sw.CONFIG['REGOLAZIONE_CALMA'])
        r = client.post('/api/comando', json={'nome': 'prelevabile', 'valore': '2500'})
        self.assertEqual(r.get_json()['piano']['prelevabile_w'], 2500)
        r = client.post('/api/comando', json={'nome': 'prelevabile', 'valore': '-5'})
        self.assertEqual(r.status_code, 400)
        r = client.post('/api/comando', data='non json')
        self.assertEqual(r.status_code, 400)
        r = client.get('/api/data')
        self.assertEqual(r.status_code, 200)
        self.assertIn('piano', r.get_json())

    def test_pagina_e_file_statici(self):
        client = sw.app.test_client()
        r = client.get('/')
        self.assertEqual(r.status_code, 200)
        pagina = r.get_data(as_text=True)
        self.assertIn('seg_modalita', pagina)
        self.assertIn('form_obiettivo', pagina)
        self.assertNotIn('__V__', pagina)           # versione dei file sostituita
        self.assertEqual(r.headers.get('Cache-Control'), 'no-cache')
        # tutto quello che la pagina carica arriva dal Pi, niente CDN
        self.assertNotIn('https://', pagina)
        for percorso in ('app.js', 'app.css', 'scena3d.js', 'icona.svg', 'manifest.webmanifest',
                         'vendor/three.module.min.js', 'vendor/chart.umd.min.js'):
            with client.get('/static/' + percorso) as f:
                self.assertEqual(f.status_code, 200, percorso)

    def test_statistiche_archivio_api_e_pagina(self):
        adesso = time.time()
        oggi = statistiche.giorno_di(adesso)
        inizio_oggi = time.mktime(time.localtime(adesso)[:3] + (0, 0, 0, 0, 0, -1))
        ieri = statistiche.giorno_di(inizio_oggi - 43200)
        memoria = sw.SYSTEM_STATE['ULTIME_LETTURE_FASI']
        prima = list(memoria)
        memoria.clear()
        sw._stat_oggi.update(t=0.0, record=None)
        try:
            # ieri intero a 1 kW, oggi un'ora a 2 kW con le fasi
            for m in range(1440):
                memoria.append((1000, 0, [0] * 6, inizio_oggi - 86400 + m * 60, 0))
            for m in range(60):
                memoria.append((2000, 0, [1000, 600, 400, 0, 0, 0], inizio_oggi + m * 60, 0))
            sw.archivia_statistiche()
            self.assertEqual(list(sw.archivio_statistiche.giorni()), [ieri])

            client = sw.app.test_client()
            r = client.get('/api/statistiche?periodo=7g')
            self.assertEqual(r.status_code, 200)
            s = r.get_json()
            self.assertEqual(s['oggi'], oggi)
            self.assertEqual(s['periodo']['giorni'], 2)
            self.assertAlmostEqual(s['totali']['consumo'], 26, places=0)
            self.assertEqual(s['giorni'][-1]['g'], oggi)
            self.assertEqual(s['fasi']['giorni'], 1)
            self.assertEqual(s['prezzi']['acquisto'], sw.CONFIG['PREZZO_ACQUISTO'])

            r = client.get('/api/statistiche?periodo=tutto', headers={'Accept-Encoding': 'gzip'})
            self.assertEqual(r.headers.get('Content-Encoding'), 'gzip')
            self.assertEqual(json.loads(gzip.decompress(r.get_data()))['copertura']['giorni'], 2)
            self.assertEqual(client.get('/api/statistiche?periodo=boh').status_code, 400)
            self.assertEqual(client.get('/api/statistiche?da=2026-13-01').status_code, 400)

            r = client.get('/api/statistiche.csv?periodo=tutto')
            self.assertEqual(r.status_code, 200)
            self.assertIn('text/csv', r.headers['Content-Type'])
            self.assertEqual(len(r.get_data(as_text=True).strip().splitlines()), 3)

            r = client.get('/statistiche')
            self.assertEqual(r.status_code, 200)
            pagina = r.get_data(as_text=True)
            self.assertNotIn('__V__', pagina)
            self.assertNotIn('https://', pagina)
            for percorso in ('statistiche.js', 'statistiche.css'):
                with client.get('/static/' + percorso) as f:
                    self.assertEqual(f.status_code, 200, percorso)
            self.assertIn('/statistiche', client.get('/').get_data(as_text=True))
        finally:
            memoria.clear()
            memoria.extend(prima)
            sw._stat_oggi.update(t=0.0, record=None)

    def test_storico_su_disco_con_le_fasi(self):
        memoria = sw.SYSTEM_STATE['ULTIME_LETTURE_FASI']
        prima = list(memoria)
        memoria.clear()
        try:
            adesso = time.time()
            with open(sw.FILE_STORICO, 'w') as f:
                f.write(json.dumps([int(adesso - 300), 900, 100, 0]) + '\n')      # riga di prima, senza fasi
            sw._ultimo_campione_t = 0.0
            sw.registra_campione(1200, 300, 0, [700, 300, 200, 100, 100, 100], ts=adesso - 100)
            sw.flush_storico()
            memoria.clear()
            sw.carica_storico()
            self.assertEqual([p[2] for p in memoria], [[0] * 6, [700, 300, 200, 100, 100, 100]])
            self.assertEqual(sw.campioni_memoria()[-1][1:4], (1200, 300, 0))
        finally:
            memoria.clear()
            memoria.extend(prima)

    def test_colonnina_sparita_risulta_spenta(self):
        wb = sw.WallboxController()
        wb.is_on = True
        with mock.patch.object(sw.time, 'time', return_value=1000.0):
            wb._lettura_fallita()
        self.assertTrue(wb.is_on)                     # un errore solo non basta
        with mock.patch.object(sw.time, 'time', return_value=1000.0 + sw.CONFIG['SESSIONE_OFFLINE_S'] + 5):
            wb._lettura_fallita()
        self.assertFalse(wb.is_on)
        self.assertFalse(sw.SYSTEM_STATE['WALLBOX_STATUS'])

    def test_colonnina_sparita_non_conta_energia(self):
        self.assertEqual(sw.potenza_auto_per_i_conteggi(3000), 3000)
        w = mock.Mock(offline_dal=None)
        with mock.patch.object(sw, 'wallbox_instance', w):
            self.assertEqual(sw.potenza_auto_per_i_conteggi(3000), 3000)
            w.offline_dal = time.time() - 30         # errore passeggero: si conta ancora
            self.assertEqual(sw.potenza_auto_per_i_conteggi(3000), 3000)
            w.offline_dal = time.time() - 600
            self.assertEqual(sw.potenza_auto_per_i_conteggi(3000), 0)

    def test_preset_dalla_pagina(self):
        client = sw.app.test_client()
        # colonnina che risponde: il limite del preset va subito sulla centralina
        w = mock.Mock(sync_falliti=0, limite_in_attesa=None)
        w.set_limite_kwh.return_value = (True, 'ok')
        w.limiti_potenza.return_value = (1380, 7360)
        with mock.patch.object(sw, 'wallbox_instance', w), \
                mock.patch.dict(sw.CONFIG, {'CMD_LIMITE_TEMPLATE': 'L{valore}'}):
            r = client.post('/api/comando', json={'nome': 'preset', 'valore': 1})
            self.assertEqual(r.status_code, 200, r.get_json())
            piano = r.get_json()['piano']
            self.assertEqual((piano['modalita'], piano['preset_attivo'], piano['preset_nome']), ('preset', 1, 'Notte'))
            w.set_limite_kwh.assert_called_with(20)
            self.assertEqual(sw.CONFIG['LIMITE_KWH'], 20)
            self.assertEqual(sw.pianificatore_instance.quota_auto(0), 4000)
            # per nome, e "veloce" come si chiamava prima
            self.assertEqual(client.post('/api/comando', json={'nome': 'preset', 'valore': 'rabbocco'})
                             .get_json()['piano']['preset_attivo'], 2)
            r = client.post('/api/comando', json={'nome': 'modalita', 'valore': 'veloce'})
            self.assertEqual(r.get_json()['piano']['preset_nome'], 'Veloce')
            w.set_limite_kwh.assert_called_with(0)
            self.assertEqual(client.post('/api/comando', json={'nome': 'preset', 'valore': 'boh'}).status_code, 400)
            # colonnina spenta: il limite resta in attesa
            w.sync_falliti = 5
            w.set_limite_kwh.reset_mock()
            r = client.post('/api/comando', json={'nome': 'preset', 'valore': 'Notte'})
            w.set_limite_kwh.assert_not_called()
            self.assertEqual(w.limite_in_attesa, 20)
            self.assertIn('appena la colonnina risponde', r.get_json()['messaggio'])
            # modifica dell'elenco: il preset attivo cambia limite, che viene riapplicato
            w.sync_falliti = 0
            nuovi = [{'nome': 'Notte', 'prelevabile': 4500, 'limite_kwh': 25}]
            r = client.post('/api/preset', json={'preset': nuovi})
            self.assertEqual(r.status_code, 200, r.get_json())
            self.assertEqual(r.get_json()['piano']['preset'], nuovi)
            self.assertEqual(r.get_json()['piano']['preset_attivo'], 0)
            w.set_limite_kwh.assert_called_with(25)
            self.assertEqual(client.post('/api/preset', json={'preset': []}).status_code, 400)
            self.assertEqual(client.post('/api/preset', data='x').status_code, 400)

    def test_programma_previsione_e_rapporto(self):
        client = sw.app.test_client()
        regole = [{'giorni': [0, 2, 4], 'ora': '07:00', 'kwh': 12, 'attivo': True}]
        r = client.post('/api/programma', json={'programma': regole})
        self.assertEqual(r.status_code, 200, r.get_json())
        piano = r.get_json()['piano']
        self.assertEqual(piano['programma'], [dict(regole[0], kwh=12.0)])
        self.assertIsNotNone(piano['prossima_programma'])
        self.assertEqual(client.post('/api/programma', json={'programma': [{'giorni': []}]}).status_code, 400)

        memoria = sw.SYSTEM_STATE['ULTIME_LETTURE_FASI']
        prima = list(memoria)
        memoria.clear()
        sw._stat_oggi.update(t=0.0, record=None)
        try:
            adesso = time.time()
            inizio_oggi = time.mktime(time.localtime(adesso)[:3] + (0, 0, 0, 0, 0, -1))
            for giorno in (2, 1):                    # due giorni interi di storia: 3 kW di sole dalle 10 alle 16
                for m in range(1440):
                    sole = 3000 if 600 <= m < 960 else 0
                    memoria.append((1000, sole, [0] * 6, inizio_oggi - giorno * 86400 + m * 60, 0))
            memoria.append((1000, 0, [0] * 6, inizio_oggi + 30, 0))
            sw.archivia_statistiche()
            p = sw.aggiorna_previsione(forza=True)
            self.assertEqual(p['fonte'], 'storico')
            self.assertEqual(p['domani_kwh'], 18.0)
            self.assertEqual(client.get('/api/previsione').get_json()['domani_kwh'], 18.0)
            self.assertEqual(client.get('/api/data').get_json()['piano']['previsione']['fonte'], 'storico')
            self.assertIn('Domani: 18.0 kWh', sw.testo_previsione())
            # gli ultimi giorni della pagina vengono dalle stesse statistiche
            ultimi = client.get('/api/giorni').get_json()['giorni']
            self.assertEqual([g['solare_kwh'] for g in ultimi[:2]], [18.0, 18.0])
            self.assertEqual(ultimi[-1]['giorno'], statistiche.giorno_di(adesso))
            self.assertIn('risparmio_eur', ultimi[0])
            # la previsione per domani si mette da parte una volta sola
            sw.salva_previsione_di_domani()
            sw.salva_previsione_di_domani()
            salvate = sw.previsioni_salvate()
            self.assertEqual([(p['kwh'], p['nuvole'], p['sereno']) for p in salvate.values()], [(18.0, None, 18.0)])
            # e si confronta con il prodotto: ieri previsti 20, prodotti 18
            ieri = statistiche.giorno_di(inizio_oggi - 43200)
            with open(sw.FILE_PREVISIONI, 'a') as f:
                f.write(json.dumps({'g': ieri, 'kwh': 20, 'fonte': 'meteo'}) + '\nriga rotta\n')
            s = client.get('/api/statistiche?periodo=7g').get_json()
            self.assertEqual(s['previsione'], {'giorni': 1, 'errore_medio_kwh': 2.0, 'errore_pct': 11,
                                               'previsti': 20, 'prodotti': 18.0, 'col_meteo': 1})
            self.assertEqual([g['previsto'] for g in s['giorni']][-2], 20)
            # il meteo di Home Assistant: tutto coperto, un quarto del sereno
            sw.meteo_ricevuto({'ore': [{'t': adesso + h * 3600, 'nuvole': 100} for h in range(-2, 50)]})
            self.assertEqual(sw.aggiorna_previsione()['fonte'], 'meteo')
            self.assertEqual(sw.aggiorna_previsione()['domani_kwh'], 2.7)
            sw.meteo_ricevuto('rotto')               # non deve sollevare eccezioni

            ha = sw.statistiche_per_ha(mock.Mock(solar_now=2500, total_grid_load=900))
            self.assertEqual(ha['potenza_libera_w'], 1600)
            self.assertEqual(ha['carico_fondo_w'], 1000)
            self.assertEqual(ha['previsione_domani_kwh'], 2.7)
            self.assertIn('autosufficienza_oggi', ha)

            testo = sw.rapporto_settimanale()
            self.assertIn('La settimana', testo)
            self.assertIn('Prodotti', testo)
        finally:
            memoria.clear()
            memoria.extend(prima)
            sw._stat_oggi.update(t=0.0, record=None)
            sw._previsione.update(t=0.0, riepilogo={'fonte': None})
            sw._fondo.update(t=0.0, w=None)
            sw.previsione_instance.meteo = {}

    def test_api_data_leggera(self):
        client = sw.app.test_client()
        sw.SYSTEM_STATE['LOGS'].clear()
        for i in range(5):
            sw.SYSTEM_STATE['LOGS'].append(f'riga {i}')
        sw.SYSTEM_STATE['LOG_SEQ'] = 105
        tutto = client.get('/api/data').get_json()
        self.assertIn('history', tutto)
        self.assertEqual(len(tutto['logs']), 5)
        self.assertTrue(tutto['logs_completi'])
        self.assertEqual(tutto['log_seq'], 105)
        for chiave in ('potenza_minima', 'potenza_massima', 'margine_contatore'):
            self.assertIn(chiave, tutto['config'])
        for chiave in ('auto_collegata', 'in_carica', 'picco_solare_w'):
            self.assertIn(chiave, tutto['status'])
        leggero = client.get('/api/data?storia=0&log_da=103').get_json()
        self.assertNotIn('history', leggero)
        self.assertEqual(leggero['logs'], ['riga 3', 'riga 4'])
        self.assertFalse(leggero['logs_completi'])
        self.assertEqual(client.get('/api/data?storia=0&log_da=105').get_json()['logs'], [])
        # pagina piu' avanti del servizio (riavviato) o troppo indietro: si rimanda tutto
        for da in (900, 3):
            d = client.get(f'/api/data?storia=0&log_da={da}').get_json()
            self.assertTrue(d['logs_completi'], da)
            self.assertEqual(len(d['logs']), 5)


if __name__ == '__main__':
    unittest.main()
