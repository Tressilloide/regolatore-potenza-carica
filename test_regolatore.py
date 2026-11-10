"""Test automatici. Uso:  python -m unittest -v test_regolatore

I moduli puri (pianificatore, protezione, regolatore classico) si provano
ovunque. I test del servizio (sessioni, API web) richiedono le librerie del
venv del Pi (Flask, python-telegram-bot, ...): senza, vengono saltati.
Nessun test tocca la rete, la colonnina o i file del servizio: i file di
stato vanno in una cartella temporanea.
"""
import os
import tempfile
import time
import unittest
from unittest import mock

from pianificatore import Pianificatore, interpreta_orario, prossima_scadenza
from protezione_contatore import prelevabile_effettivo, ProtezioneContatore
from regolatore_classico import RegolatoreClassico


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
        cfg['MODALITA'] = 'veloce'
        self.assertEqual(p.quota_auto(0), 6600)

    def test_veloce_resta_entro_il_contatore(self):
        cfg = config_base(MODALITA='veloce')
        cfg['PIANIFICATORE'], _ = nuovo_pianificatore(cfg)
        # Casa a 2 kW senza sole: in tutto non oltre 6600 - 300
        self.assertEqual(prelevabile_effettivo(cfg, 2000), 6300)
        cfg['MODALITA'] = 'sole'
        self.assertEqual(prelevabile_effettivo(cfg, 2000), 2000)   # solo la casa
        self.assertEqual(prelevabile_effettivo(cfg, -3000), 0)     # il sole avanza

    def test_boost_scade_e_torna_alla_modalita(self):
        orologio = Orologio()
        cfg = config_base(MODALITA='sole')
        p, avvisi = nuovo_pianificatore(cfg, orologio=orologio)
        ok, _ = p.imposta_boost(60)
        self.assertTrue(ok)
        self.assertEqual(p.modalita_effettiva(), 'veloce')
        self.assertEqual(p.quota_auto(0), 6600)
        orologio.t += 61 * 60
        self.assertEqual(p.quota_auto(0), 0)
        self.assertIsNone(p.boost_fino)
        self.assertEqual(len(avvisi), 1)

    def test_boost_valori_non_validi(self):
        p, _ = nuovo_pianificatore(config_base())
        for valore in ('abc', -5, 100000, float('nan')):
            self.assertFalse(p.imposta_boost(valore)[0], valore)
        self.assertTrue(p.imposta_boost(0)[0])      # annulla: sempre ok

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
        p.imposta_boost(30)
        p.imposta_obiettivo(12, orologio.t + 5 * 3600)
        p.aggiungi_energia(2)
        q, _ = nuovo_pianificatore(config_base(), orologio=orologio)
        q.importa(salvati[-1])
        self.assertEqual(q.obiettivo['fatti'], 2)
        self.assertIsNotNone(q.boost_fino)
        # Gia' scaduti al riavvio: non si riprendono
        orologio.t += 6 * 3600
        r, _ = nuovo_pianificatore(config_base(), orologio=orologio)
        r.importa(salvati[-1])
        self.assertIsNone(r.obiettivo)
        self.assertIsNone(r.boost_fino)
        r.importa("spazzatura")
        r.importa({'obiettivo': {'kwh': 'x'}})
        self.assertIsNone(r.obiettivo)

    def test_salvataggio_che_fallisce_non_ferma_nulla(self):
        def rotto(_):
            raise OSError("disco pieno")
        p = Pianificatore(config_base(), log=lambda m: None, salva=rotto, orologio=Orologio())
        self.assertTrue(p.imposta_boost(10)[0])

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

    def test_veloce_accende(self):
        self.assertTrue(self.passo_notte('veloce')[0].is_on)

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
        r = client.post('/api/comando', json={'nome': 'boost', 'valore': '30'})
        self.assertEqual(r.get_json()['piano']['modalita_effettiva'], 'veloce')
        r = client.post('/api/comando', data='non json')
        self.assertEqual(r.status_code, 400)
        r = client.get('/api/data')
        self.assertEqual(r.status_code, 200)
        self.assertIn('piano', r.get_json())
        pagina = client.get('/').get_data(as_text=True)
        self.assertIn('seg_modalita', pagina)
        self.assertIn('impostaObiettivo', pagina)


if __name__ == '__main__':
    unittest.main()
