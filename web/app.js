/* Casa solare - logica della pagina.
   Chiede lo stato al Raspberry ogni 2 s (/api/data, in versione leggera),
   aggiorna numeri, comandi e grafici, e passa i dati alla scena 3D
   (scena3d.js), che e' facoltativa: se WebGL manca o qualcosa va storto la
   pagina funziona lo stesso con quattro riquadri al posto della scena. */

const $ = id => document.getElementById(id);
const VERSIONE = document.querySelector('meta[name=versione]')?.content || '0';
const INTERVALLO_MS = 2000;

/* ------------------------------------------------------------------
   Formati
   ------------------------------------------------------------------ */
const nf0 = new Intl.NumberFormat('it-IT', { maximumFractionDigits: 0 });
const nf1 = new Intl.NumberFormat('it-IT', { minimumFractionDigits: 1, maximumFractionDigits: 1 });
const nf2 = new Intl.NumberFormat('it-IT', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const manca = v => v === null || v === undefined || Number.isNaN(v);

function watt(w) {
    if (manca(w)) return '--';
    const a = Math.abs(w);
    return a < 1000 ? nf0.format(Math.round(a / 10) * 10) + ' W' : nf1.format(a / 1000) + ' kW';
}
const kwh = (v, cifre = 1) => manca(v) ? '--' : (cifre === 2 ? nf2 : nf1).format(v);
const ora = ts => new Date(ts * 1000).toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit' });
const giornoOra = ts => new Date(ts * 1000).toLocaleString('it-IT',
    { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
function durata(minuti) {
    if (manca(minuti)) return '--';
    const m = Math.max(0, Math.round(minuti));
    return m < 60 ? m + ' min' : Math.floor(m / 60) + ' h ' + String(m % 60).padStart(2, '0') + ' min';
}
function testo(id, valore) {
    const el = $(id);
    if (el && el.textContent !== valore) el.textContent = valore;
}
function memoria(chiave, valore) {
    /* localStorage puo' mancare o lanciare (navigazione privata): mai fidarsi */
    try {
        if (valore === undefined) return localStorage.getItem(chiave);
        localStorage.setItem(chiave, valore);
    } catch (e) { /* si vive senza */ }
    return null;
}

/* ------------------------------------------------------------------
   Tema
   ------------------------------------------------------------------ */
const TEMI = ['auto', 'light', 'dark'];
const NOMI_TEMA = { auto: 'automatico', light: 'chiaro', dark: 'scuro' };
let tema = TEMI.includes(memoria('tema')) ? memoria('tema') : 'auto';

function applicaTema() {
    if (tema === 'auto') delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = tema;
    $('btn_tema').title = 'Tema: ' + NOMI_TEMA[tema];
    $('btn_tema').setAttribute('aria-label', 'Cambia tema (ora: ' + NOMI_TEMA[tema] + ')');
}
function colori() {
    const c = getComputedStyle(document.documentElement);
    const v = n => c.getPropertyValue(n).trim();
    return { sole: v('--sole'), casa: v('--casa'), auto: v('--auto'), rete: v('--rete'), neutro: v('--neutro'),
             testo: v('--testo'), testo2: v('--testo-2'), testo3: v('--testo-3'), griglia: v('--griglia'),
             asse: v('--asse'), superficie: v('--superficie') };
}
applicaTema();
$('btn_tema').addEventListener('click', () => {
    tema = TEMI[(TEMI.indexOf(tema) + 1) % TEMI.length];
    memoria('tema', tema);
    applicaTema();
    ridipingi();
});
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => { if (tema === 'auto') ridipingi(); });

/* ------------------------------------------------------------------
   Esiti e comandi
   ------------------------------------------------------------------ */
const timerEsito = {};
function esito(messaggio, ok, id = 'esito') {
    const b = $(id);
    b.textContent = messaggio;
    b.className = 'esito ' + (ok ? 'ok' : 'ko');
    b.hidden = false;
    clearTimeout(timerEsito[id]);
    if (ok) timerEsito[id] = setTimeout(() => { b.hidden = true; }, 4500);
}

async function invia(url, corpo) {
    /* Ritorna {ok, dati}. Mai eccezioni: gli errori di rete diventano ok=false */
    try {
        const r = await fetch(url, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: corpo === undefined ? undefined : JSON.stringify(corpo)
        });
        const dati = await r.json().catch(() => ({}));
        return { ok: r.ok && dati.success !== false, dati };
    } catch (e) {
        return { ok: false, dati: { messaggio: 'Errore di rete: il Raspberry non risponde.' } };
    }
}

async function comando(nome, valore) {
    const { ok, dati } = await invia('/api/comando', { nome, valore });
    esito(dati.messaggio || dati.error || (ok ? 'Fatto.' : 'Comando non riuscito.'), ok, 'esito_carica');
    aggiorna();
    return ok;
}

/* ------------------------------------------------------------------
   Stato
   ------------------------------------------------------------------ */
let ultimo = null;          // ultima risposta di /api/data
let logSeq = null;          // numero dell'ultima riga di log ricevuta
let erroriRete = 0;
let scena = null;           // oggetto restituito da scena3d.js, se c'e'
const sporchi = new Set();  // campi modificati e non ancora salvati

function setCampo(id, valore) {
    const el = $(id);
    if (!el || document.activeElement === el || sporchi.has(id) || manca(valore)) return;
    if (String(el.value) !== String(valore)) el.value = valore;
}
function mostraSporchi() {
    $('non_salvate').hidden = ![...sporchi].some(id => id !== 'limite');
}
['protezione', 'prezzo_acquisto', 'prezzo_vendita', 'limite'].forEach(id => {
    $(id).addEventListener('input', () => { sporchi.add(id); mostraSporchi(); if (ultimo) mostraLimite(ultimo); });
});

/* Le grandezze che servono a tutti, ricavate una volta sola */
function bilancio(s) {
    const sole = s.solar_total || 0;
    const consumo = s.grid_total || 0;
    const auto = Math.max(0, s.wb_pcar || 0);
    const casa = Math.max(0, consumo - auto);
    const scambio = consumo - sole;                       // > 0 prelievo, < 0 immissione
    const autoDalSole = Math.min(auto, Math.max(0, sole - casa));
    return { sole, consumo, auto, casa, scambio, autoDalSole, autoDallaRete: auto - autoDalSole };
}

async function aggiorna() {
    let d;
    try {
        const r = await fetch('/api/data?storia=0' + (logSeq === null ? '' : '&log_da=' + logSeq), { cache: 'no-store' });
        if (!r.ok) throw new Error(r.status);
        d = await r.json();
        erroriRete = 0;
    } catch (e) {
        erroriRete++;
        if (erroriRete >= 3) mostraAvvisi(null);
        return;
    }
    ultimo = d;
    const s = d.status, c = d.config;
    const b = bilancio(s);

    mostraTestata(s);
    mostraAvvisi(d);
    mostraScena(s, c, b, d.piano);
    mostraCarica(d, b);
    mostraLimite(d);
    mostraOggi(d.energia || {});
    mostraColonnina(s, c);
    mostraImpostazioni(c);
    mostraFasi(s);
    mostraTecnici(d);
    testo('piede_ora', 'aggiornato alle ' + new Date(s.server_time * 1000).toLocaleTimeString('it-IT'));
}

function mostraTestata(s) {
    const col = $('st_colonnina');
    let nome, stato;
    if (!s.centralina_online) { nome = 'Colonnina spenta'; stato = 'attesa'; }
    else if (s.in_carica) { nome = 'In carica'; stato = 'ok'; }
    else if (s.auto_collegata) { nome = (s.wb_desc || 'Auto collegata').replace(/_/g, ' '); stato = 'attesa'; }
    else { nome = 'Nessuna auto'; stato = ''; }
    col.dataset.s = stato;
    col.lastElementChild.textContent = nome;
    const sen = $('st_sensore');
    sen.dataset.s = s.sensore_online ? 'ok' : 'errore';
    sen.lastElementChild.textContent = s.sensore_online ? 'Sensore' : 'Sensore muto';
    $('st_fase').lastElementChild.textContent = s.fase_mode === 1 ? 'Trifase' : 'Monofase';
}

function mostraAvvisi(d) {
    const av = [];
    if (!d) {
        av.push(['errore', 'Il Raspberry non risponde.', 'I numeri qui sotto sono gli ultimi ricevuti.']);
    } else {
        const s = d.status;
        if (!s.sensore_online) av.push(['errore', 'Nessun dato dal sensore.', 'La potenza resta all’ultimo valore deciso.']);
        if (s.manual_off) av.push(['avviso', 'Carica spenta a mano.', 'Il regolatore non riaccende finché non mandi /accendi su Telegram.']);
        if (s.alg_manuale === false) av.push(['avviso', 'La colonnina non è in modalità Man.', 'I comandi di potenza vengono ignorati.']);
        if (!manca(s.wb_temp_presa) && s.wb_temp_presa >= 60 && s.centralina_online)
            av.push(['errore', 'Presa a ' + Math.round(s.wb_temp_presa) + ' °C.', 'Controlla il collegamento.']);
    }
    const box = $('avvisi');
    const firma = JSON.stringify(av);
    if (box.dataset.firma === firma) return;
    box.dataset.firma = firma;
    box.textContent = '';
    av.forEach(([gravita, titolo, dettaglio]) => {
        const el = document.createElement('div');
        el.className = 'avviso';
        el.dataset.g = gravita;
        const t = document.createElement('b');
        t.textContent = titolo;
        el.append(t, ' ' + dettaglio);
        box.appendChild(el);
    });
}

/* ------------------------------------------------------------------
   Scena e frase
   ------------------------------------------------------------------ */
function frase(s, c, b, piano) {
    if (!s.sensore_online) return 'In attesa dei dati del sensore.';
    const parti = [];
    if (b.auto > 50) {
        let origine;
        if (b.autoDallaRete < 150) origine = 'tutta dal sole';
        else if (b.autoDalSole < 150) origine = 'dalla rete';
        else origine = watt(b.autoDalSole) + ' dal sole e ' + watt(b.autoDallaRete) + ' dalla rete';
        parti.push('L’auto carica a ' + watt(b.auto) + ', ' + origine + '.');
    } else if (!s.centralina_online) {
        parti.push('La colonnina è spenta.');
    } else if (s.auto_collegata) {
        const avanza = b.sole - b.casa;
        const modalita = piano ? piano.modalita_effettiva : 'misto';
        if (s.manual_off) parti.push('Auto collegata, carica spenta a mano.');
        else if (modalita === 'sole' && avanza < c.potenza_minima)
            parti.push('Auto in attesa del sole: ' + (avanza > 0 ? 'avanzano ' + watt(avanza) : 'non avanza nulla')
                       + ', ne servono ' + watt(c.potenza_minima) + '.');
        else parti.push('Auto collegata, non sta caricando.');
    } else {
        parti.push('Nessuna auto collegata.');
    }
    if (b.scambio < -50) parti.push('Stai immettendo ' + watt(b.scambio) + ' in rete.');
    else if (b.scambio > 50) {
        if (b.auto > 50) parti.push('In tutto prelevi ' + watt(b.scambio) + ' dalla rete.');
        else if (b.sole > 50) parti.push('Dalla rete arrivano ' + watt(b.scambio) + '.');
        else parti.push('La casa prende ' + watt(b.scambio) + ' dalla rete.');
    }
    else parti.push('Sole e consumi sono in pari.');
    return parti.join(' ');
}

const larghezze = new Map();    // larghezza delle etichette, per tenerle dentro la scena
function misuraEtichette() {
    document.querySelectorAll('.etichetta').forEach(el => larghezze.set(el.dataset.nodo, [el.offsetWidth, el.offsetHeight]));
}

function mostraScena(s, c, b, piano) {
    testo('n_sole', watt(b.sole));
    testo('n_casa', watt(b.casa));
    testo('n_auto', s.auto_collegata || b.auto > 0 ? watt(b.auto) : '--');
    testo('n_auto_nome', b.auto > 50 ? 'Auto in carica' : (s.auto_collegata ? 'Auto in attesa' : 'Nessuna auto'));
    testo('n_rete', watt(b.scambio));
    testo('n_rete_nome', b.scambio >= 0 ? 'Prelievo dalla rete' : 'Immissione in rete');
    document.querySelector('.etichetta[data-nodo=auto]').classList.toggle('spenta', !s.auto_collegata && b.auto <= 0);
    testo('frase', frase(s, c, b, piano));
    const eta = s.last_solar ? Math.max(0, Math.round(s.server_time - s.last_solar)) : null;
    testo('frase_eta', eta === null ? '' : (eta < 90 ? 'Dati del sensore di ' + eta + ' s fa.'
                                                       : 'Ultimo dato del sensore: ' + durata(eta / 60) + ' fa.'));
    misuraEtichette();
    if (scena) scena.aggiorna({ sole: b.sole, casa: b.casa, auto: b.auto, scambio: b.scambio,
                                collegata: !!s.auto_collegata, inCarica: b.auto > 50,
                                colonnina: !!s.centralina_online, tempo: s.server_time,
                                picco: s.picco_solare_w || 0 });
}

function posizionaEtichette(posizioni, larghezza, altezza) {
    for (const el of document.querySelectorAll('.etichetta')) {
        const p = posizioni[el.dataset.nodo];
        if (!p) continue;
        const [w, h] = larghezze.get(el.dataset.nodo) || [120, 44];
        const x = Math.min(Math.max(p.x, w / 2 + 6), larghezza - w / 2 - 6);
        const y = p.sotto ? Math.min(Math.max(p.y, 6), altezza - h - 6) : Math.min(Math.max(p.y, h + 6), altezza - 6);
        el.style.transform = `translate(calc(${x.toFixed(1)}px - 50%), calc(${y.toFixed(1)}px - ${p.sotto ? 0 : 100}%))`;
        el.classList.add('visibile');
    }
}

function scenaPiatta(motivo) {
    $('scena').classList.add('piatta');
    document.querySelectorAll('.etichetta').forEach(el => { el.style.transform = ''; });
    if (scena) { scena.ferma(); }
    const b = $('btn_scena');
    b.hidden = motivo === 'non disponibile';
    b.textContent = 'Vista 3D';
}

async function avviaScena() {
    if (memoria('scena') === 'piatta') { scenaPiatta('scelta'); return; }
    try {
        if (!scena) {
            const modulo = await import('./scena3d.js?v=' + VERSIONE);
            scena = modulo.creaScena($('scena_tela'), $('scena'), { colori: colori(), suEtichette: posizionaEtichette });
        } else {
            scena.riprendi();
        }
        $('scena').classList.remove('piatta');
        $('btn_scena').hidden = false;
        $('btn_scena').textContent = 'Vista semplice';
        if (ultimo) mostraScena(ultimo.status, ultimo.config, bilancio(ultimo.status), ultimo.piano);
    } catch (e) {
        console.warn('Scena 3D non disponibile:', e);
        scena = null;
        scenaPiatta('non disponibile');
    }
}
$('btn_scena').addEventListener('click', () => {
    const piatta = $('scena').classList.contains('piatta');
    memoria('scena', piatta ? '3d' : 'piatta');
    if (piatta) avviaScena(); else scenaPiatta('scelta');
});

/* ------------------------------------------------------------------
   Carica: modalita', prelevabile, obiettivo, limite
   ------------------------------------------------------------------ */

function mostraCarica(d, b) {
    const s = d.status, c = d.config, p = d.piano;
    const stato = $('carica_stato');
    if (s.manual_off) { stato.textContent = 'Spenta a mano'; stato.dataset.s = 'attesa'; }
    else if (b.auto > 50) { stato.textContent = 'In carica a ' + watt(b.auto); stato.dataset.s = 'ok'; }
    else if (!s.centralina_online) { stato.textContent = 'Colonnina spenta'; stato.dataset.s = ''; }
    else if (s.auto_collegata) { stato.textContent = 'In attesa'; stato.dataset.s = 'attesa'; }
    else { stato.textContent = 'Nessuna auto'; stato.dataset.s = ''; }
    if (!p) return;

    testo('seg_misto_w', '+ ' + watt(p.prelevabile_w));
    document.querySelectorAll('#seg_modalita button').forEach(el => {
        const attivo = el.dataset.m === p.modalita_effettiva;
        el.classList.toggle('attivo', attivo);
        el.classList.toggle('scelta', !attivo && el.dataset.m === p.modalita);
        el.setAttribute('aria-pressed', attivo);
    });
    const tetto = c.limite_contatore - (c.margine_contatore || 0);
    let nota;
    if (p.modalita === 'sole') {
        nota = 'L’auto prende solo il sole che avanza alla casa (parte da ' + watt(c.potenza_minima)
             + ' di avanzo). Il prelevabile qui sopra non conta.';
    } else if (p.modalita === 'misto') {
        nota = 'Oltre al sole, l’auto può prendere dalla rete fino al prelevabile, in aggiunta a quello che '
             + 'preleva la casa. In tutto si resta sotto ' + watt(tetto) + ' (limite del contatore).';
    } else {
        nota = 'L’auto prende tutto quello che il contatore permette: fino a ' + watt(tetto)
             + ' di prelievo, casa compresa. Il prelevabile qui sopra non conta.';
    }
    testo('nota_modalita', nota);
    mostraPrelevabile(c, p);

    const ob = p.obiettivo;
    $('box_obiettivo').hidden = !ob;
    $('form_obiettivo').hidden = !!ob;
    $('nota_obiettivo').hidden = !!ob;
    if (ob) {
        const perc = Math.min(100, ob.fatti / ob.kwh * 100);
        testo('ob_testo', kwh(ob.fatti) + ' di ' + nf0.format(ob.kwh) + ' kWh');
        testo('ob_tempo', 'entro le ' + ora(ob.entro) + ' · ' + durata(ob.minuti_rimasti));
        $('ob_barra').style.width = perc + '%';
        $('ob_prog').setAttribute('aria-valuenow', Math.round(perc));
        testo('ob_nota', ob.in_recupero
            ? 'Il sole non basta: integro con la rete (servono ' + watt(ob.potenza_necessaria) + ' in media).'
            : 'Per ora niente rete: servirebbero ' + watt(ob.potenza_necessaria)
              + ' in media, si integra quando arrivano al minimo della colonnina (' + watt(c.potenza_minima) + ').');
    }
}

/* Prelevabile: il cursore e il campo numerico mostrano lo stesso valore, e
   il comando parte quando si rilascia il cursore o si conferma il numero.
   Mentre lo si tocca, il valore che arriva dal Pi non lo sovrascrive. */
const prel = $('prelevabile'), prelNum = $('prelevabile_num');
let prelInModifica = false;

function mostraPrelevabile(c, p) {
    const massimo = c.limite_contatore || 6600;
    prel.max = prelNum.max = massimo;
    if (!prelInModifica && document.activeElement !== prel && document.activeElement !== prelNum) {
        prel.value = prelNum.value = c.prelevabile;
    }
    testo('prelevabile_kw', watt(Number(prel.value)));
    $('box_prelevabile').classList.toggle('inattivo', p.modalita !== 'misto');
}
async function inviaPrelevabile(grezzo) {
    const massimo = Number(prel.max) || 6600;
    let v = Math.round(Number(String(grezzo).replace(',', '.')));
    if (!Number.isFinite(v)) { esito('Prelevabile: valore non valido.', false, 'esito_carica'); prelInModifica = false; return; }
    v = Math.min(massimo, Math.max(0, v));
    prel.value = prelNum.value = v;
    testo('prelevabile_kw', watt(v));
    await comando('prelevabile', v);
    prelInModifica = false;
}
prel.addEventListener('input', () => {
    prelInModifica = true;
    prelNum.value = prel.value;
    testo('prelevabile_kw', watt(Number(prel.value)));
});
prel.addEventListener('change', () => inviaPrelevabile(prel.value));
prelNum.addEventListener('input', () => {
    prelInModifica = true;
    const v = Number(prelNum.value);
    if (Number.isFinite(v)) { prel.value = v; testo('prelevabile_kw', watt(Math.min(Number(prel.max), Math.max(0, v)))); }
});
prelNum.addEventListener('change', () => inviaPrelevabile(prelNum.value));
prelNum.addEventListener('keydown', e => { if (e.key === 'Enter') prelNum.blur(); });

function mostraLimite(d) {
    const s = d.status, c = d.config;
    setCampo('limite', c.limite);
    const scelto = Number($('limite').value) || c.limite;
    testo('limite_val', scelto + ' kWh');
    const letto = s.limite_kwh_centralina;
    const ignoto = manca(letto);
    const attivo = !ignoto && Number(letto) > 0;
    const stato = $('limite_stato');
    stato.textContent = ignoto ? 'sconosciuto: colonnina spenta' : (attivo ? 'attivo: ' + letto + ' kWh' : 'nessuno');
    stato.dataset.s = attivo ? 'ok' : '';
    const on = $('btn_limite_on'), off = $('btn_limite_off');
    on.textContent = attivo && Number(letto) === scelto ? 'Limite a ' + scelto + ' kWh' : 'Imposta ' + scelto + ' kWh';
    on.disabled = ignoto || !s.limite_supportato || (attivo && Number(letto) === scelto);
    off.disabled = ignoto || !s.limite_supportato || !attivo;
}

async function impostaLimite(attivo) {
    const corpo = { attivo };
    if (attivo) corpo.kwh = Number($('limite').value);
    $('btn_limite_on').disabled = $('btn_limite_off').disabled = true;
    const { ok, dati } = await invia('/api/limite_toggle', corpo);
    if (ok) sporchi.delete('limite');
    esito(dati.messaggio || dati.error || (ok ? 'Fatto.' : 'Comando non riuscito.'), ok, 'esito_carica');
    aggiorna();
}

document.querySelectorAll('#seg_modalita button').forEach(el =>
    el.addEventListener('click', () => comando('modalita', el.dataset.m)));
$('btn_ob_off').addEventListener('click', () => comando('obiettivo', 'off'));
$('btn_limite_on').addEventListener('click', () => impostaLimite(true));
$('btn_limite_off').addEventListener('click', () => impostaLimite(false));
$('form_obiettivo').addEventListener('submit', async e => {
    e.preventDefault();
    const quanti = $('ob_kwh').value.trim(), quando = $('ob_ora').value;
    if (!quanti || !quando) { esito('Indica i kWh e l’orario.', false, 'esito_carica'); return; }
    if (await comando('obiettivo', quanti + ' ' + quando)) $('ob_kwh').value = '';
});

/* ------------------------------------------------------------------
   Oggi, colonnina, impostazioni, dettagli
   ------------------------------------------------------------------ */
function misura(id, valore) {
    testo(id, manca(valore) ? '--' : Math.round(valore) + '%');
    $(id + '_bar').style.width = (manca(valore) ? 0 : Math.max(0, Math.min(100, valore))) + '%';
}

function mostraOggi(en) {
    testo('e_solare', kwh(en.solare_kwh));
    testo('e_import', kwh(en.rete_importata_kwh));
    testo('e_export', kwh(en.rete_esportata_kwh));
    testo('e_wb', kwh(en.wallbox_kwh));
    testo('e_wb_fv', kwh(en.wallbox_da_fv_kwh));
    testo('e_tempo', durata(en.minuti_carica));
    misura('m_eff', en.efficienza);
    /* Autoconsumo: quanta parte del sole e' stata usata in casa (auto
       compresa). Autosufficienza: quanta parte dei consumi ha coperto il sole. */
    const prodotta = en.solare_kwh || 0, immessa = en.rete_esportata_kwh || 0, prelevata = en.rete_importata_kwh || 0;
    const usata = Math.max(0, prodotta - immessa);
    const consumata = usata + prelevata;
    misura('m_autoc', prodotta > 0.05 ? usata / prodotta * 100 : null);
    misura('m_autos', consumata > 0.05 ? usata / consumata * 100 : null);
}

function mostraColonnina(s, c) {
    const d = $('c_desc');
    d.textContent = !s.centralina_online ? 'non raggiungibile' : (s.wb_desc || '--').replace(/_/g, ' ');
    d.dataset.s = !s.centralina_online ? 'errore' : (s.in_carica ? 'ok' : '');
    testo('c_set', s.wb_on ? watt(s.wb_power) : 'spenta');
    testo('c_pcar', manca(s.wb_pcar) ? '--' : watt(s.wb_pcar));
    testo('c_sess', manca(s.wb_energia_sessione) ? '--'
        : kwh(s.wb_energia_sessione, 2) + ' kWh' + (s.wb_energia_misurata ? '' : ' (stima)'));
    testo('c_temp', manca(s.wb_temp_presa) ? '--' : Math.round(s.wb_temp_presa) + ' °C');
    testo('c_limiti', watt(c.potenza_minima) + ' – ' + watt(c.potenza_massima)
        + (s.fase_mode === 1 ? ' (trifase)' : ' (monofase)'));
    testo('c_alg', c.algoritmo === 'pid' ? 'PID' : 'classico');
}

function mostraImpostazioni(c) {
    setCampo('protezione', c.protezione);
    setCampo('prezzo_acquisto', c.prezzo_acquisto);
    setCampo('prezzo_vendita', c.prezzo_vendita);
}

function mostraFasi(s) {
    const fasi = s.fasi || [];
    /* una sola scala per le sei barre, mai sotto 1 kW: cosi' 50 W non sembrano tanti */
    const scala = Math.max(1000, ...fasi.map(v => Math.abs(v || 0)));
    fasi.forEach((v, i) => {
        testo('l' + (i + 1), watt(v));
        const barra = $('l' + (i + 1) + '_b');
        if (barra) barra.style.width = Math.min(100, Math.abs(v || 0) / scala * 100) + '%';
    });
    testo('tot_grid', watt(s.grid_total));
    testo('tot_solar', watt(s.solar_total));
    const eta = s.last_fasi ? Math.max(0, Math.round(s.server_time - s.last_fasi)) : null;
    testo('fasi_eta', eta === null ? '' : (eta < 120 ? 'misura di ' + eta + ' s fa' : 'misura di ' + durata(eta / 60) + ' fa'));
}

let righeLog = [];
function mostraTecnici(d) {
    const s = d.status;
    if (d.logs_completi) righeLog = d.logs || [];
    else if (d.logs && d.logs.length) righeLog = righeLog.concat(d.logs).slice(-300);
    else if (logSeq !== null) { logSeq = d.log_seq; return; }
    logSeq = d.log_seq;
    const reg = $('registro');
    const inFondo = reg.scrollHeight - reg.clientHeight <= reg.scrollTop + 8;
    reg.textContent = righeLog.join('\n');     // textContent: niente markup interpretato
    if (inFondo) reg.scrollTop = reg.scrollHeight;
}

async function salva() {
    const corpo = {}, errori = [];
    const leggi = (id, nome, intero) => {
        const grezzo = $(id).value.trim().replace(',', '.');
        const v = Number(grezzo);
        if (grezzo === '' || !Number.isFinite(v) || v < 0) { errori.push(nome + ': valore non valido'); return; }
        corpo[id] = intero ? Math.round(v) : v;
    };
    leggi('protezione', 'Soglia di protezione', true);
    leggi('prezzo_acquisto', 'Prezzo di acquisto', false);
    leggi('prezzo_vendita', 'Prezzo di vendita', false);
    if (errori.length) { esito('Correggi:\n' + errori.join('\n'), false); return; }
    const { ok, dati } = await invia('/api/settings', corpo);
    if (!ok) { esito('Salvataggio non riuscito:\n' + (dati.errori || [dati.messaggio || 'errore sconosciuto']).join('\n'), false); return; }
    ['protezione', 'prezzo_acquisto', 'prezzo_vendita'].forEach(id => sporchi.delete(id));
    mostraSporchi();
    esito('Impostazioni salvate.', true);
    aggiorna();
    caricaGiorni();
}
$('btn_salva').addEventListener('click', salva);
$('btn_reinit').addEventListener('click', async () => {
    if (!confirm('Re-inizializzare la colonnina? La carica in corso continua, ma un limite di energia attivo viene tolto.')) return;
    const { ok, dati } = await invia('/api/init_wallbox');
    esito(ok ? 'Re-inizializzazione avviata: segui il registro in fondo alla pagina.' : 'Errore: ' + (dati.error || 'comando non riuscito'), ok);
});

/* ------------------------------------------------------------------
   Grafici
   ------------------------------------------------------------------ */
let grafAndamento = null, grafGiorni = null;
let intervallo = ['1h', '6h', '24h', '7g'].includes(memoria('intervallo')) ? memoria('intervallo') : '1h';
let puntiAndamento = [], giorni = [];
const alfa = (hex, a) => hex + Math.round(a * 255).toString(16).padStart(2, '0');

/* Linea verticale sul punto indicato: aiuta a leggere le tre serie insieme */
const mirino = {
    id: 'mirino',
    afterDatasetsDraw(chart) {
        const attivi = chart.tooltip?.getActiveElements?.() || [];
        if (!attivi.length) return;
        const { ctx, chartArea } = chart;
        ctx.save();
        ctx.strokeStyle = colori().asse;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(attivi[0].element.x, chartArea.top);
        ctx.lineTo(attivi[0].element.x, chartArea.bottom);
        ctx.stroke();
        ctx.restore();
    }
};

/* Suggerimento in HTML: usa i colori del tema e non taglia il testo */
function suggerimento(unita, cifre) {
    return ({ chart, tooltip }) => {
        const box = $('suggerimento');
        if (!tooltip || tooltip.opacity === 0 || !tooltip.dataPoints?.length) { box.hidden = true; return; }
        box.textContent = '';
        const titolo = document.createElement('div');
        titolo.className = 'titolo';
        titolo.textContent = tooltip.title?.[0] || '';
        box.appendChild(titolo);
        tooltip.dataPoints.forEach(p => {
            const riga = document.createElement('div');
            riga.className = 'voce';
            const tinta = document.createElement('i');
            tinta.className = 'tinta' + (p.dataset.type === 'line' || p.dataset.fill === false ? ' linea' : '');
            tinta.style.background = p.dataset.coloreSerie;
            const valore = document.createElement('b');
            valore.textContent = (cifre ? nf1 : nf0).format(p.parsed.y) + ' ' + unita;
            riga.append(tinta, p.dataset.label, valore);
            box.appendChild(riga);
        });
        box.hidden = false;
        const r = chart.canvas.getBoundingClientRect();
        const x = r.left + tooltip.caretX, y = r.top + tooltip.caretY;
        const w = box.offsetWidth, h = box.offsetHeight;
        box.style.left = Math.max(8, Math.min(innerWidth - w - 8, x + (x + w + 24 > innerWidth ? -w - 14 : 14))) + 'px';
        box.style.top = Math.max(8, Math.min(innerHeight - h - 8, y - h / 2)) + 'px';
    };
}

function opzioniBase(col, unitaAsse) {
    return {
        responsive: true, maintainAspectRatio: false, animation: false,
        interaction: { mode: 'index', intersect: false },
        plugins: { legend: { display: false }, tooltip: { enabled: false } },
        scales: {
            x: { ticks: { color: col.testo3, maxTicksLimit: 7, maxRotation: 0, autoSkip: true, font: { size: 11 } },
                 grid: { display: false }, border: { color: col.asse } },
            y: { beginAtZero: true,
                 title: { display: true, text: unitaAsse, color: col.testo3, font: { size: 11 } },
                 ticks: { color: col.testo3, font: { size: 11 }, maxTicksLimit: 6 },
                 grid: { color: col.griglia, drawTicks: false }, border: { display: false } }
        }
    };
}

function creaGrafici() {
    if (!window.Chart) return;
    const col = colori();
    grafAndamento?.destroy();
    grafGiorni?.destroy();

    const o1 = opzioniBase(col, 'kW');
    o1.scales.y.stacked = true;
    o1.scales.y.ticks.callback = v => nf1.format(v / 1000);
    o1.plugins.tooltip.external = suggerimento('W', false);
    grafAndamento = new Chart($('grafico_andamento'), {
        type: 'line',
        data: { labels: [], datasets: [
            { label: 'Produzione', coloreSerie: col.sole, data: [], stack: 'produzione', fill: false,
              borderColor: col.sole, borderWidth: 2, pointRadius: 0, pointHoverRadius: 4, tension: .25, order: 1 },
            { label: 'Casa', coloreSerie: col.casa, data: [], stack: 'consumi', fill: 'origin',
              borderColor: col.casa, backgroundColor: alfa(col.casa, .26), borderWidth: 1.5,
              pointRadius: 0, pointHoverRadius: 4, tension: .25, order: 2 },
            { label: 'Auto', coloreSerie: col.auto, data: [], stack: 'consumi', fill: '-1',
              borderColor: col.auto, backgroundColor: alfa(col.auto, .26), borderWidth: 1.5,
              pointRadius: 0, pointHoverRadius: 4, tension: .25, order: 2 }
        ] },
        options: o1, plugins: [mirino]
    });

    const o2 = opzioniBase(col, 'kWh');
    o2.scales.x.stacked = true;
    o2.scales.y.stacked = true;
    o2.plugins.tooltip.external = suggerimento('kWh', true);
    /* Estremita' arrotondata solo sul segmento in cima alla pila */
    const cima = quale => ctx => {
        const sopra = quale === 0 ? (ctx.chart.data.datasets[1].data[ctx.dataIndex] || 0) : 0;
        return sopra > 0 ? 0 : { topLeft: 4, topRight: 4 };
    };
    grafGiorni = new Chart($('grafico_giorni'), {
        type: 'bar',
        data: { labels: [], datasets: [
            { label: 'In auto dal sole', coloreSerie: col.sole, data: [], stack: 'auto', backgroundColor: col.sole,
              borderRadius: cima(0), maxBarThickness: 22, order: 2 },
            { label: 'In auto dalla rete', coloreSerie: col.rete, data: [], stack: 'auto', backgroundColor: col.rete,
              borderRadius: cima(1), maxBarThickness: 22, order: 2,
              /* 2 px del colore della scheda fra i due segmenti, al posto di un bordo */
              borderColor: col.superficie, borderWidth: { bottom: 2 }, borderSkipped: false },
            { label: 'Prodotti dal fotovoltaico', coloreSerie: col.neutro, type: 'line', data: [], stack: 'prodotti',
              borderColor: col.neutro, backgroundColor: col.neutro, borderWidth: 1.5, pointRadius: 2.5,
              pointHoverRadius: 5, tension: .25, order: 1 }
        ] },
        options: o2, plugins: [mirino]
    });
    disegnaAndamento();
    disegnaGiorni();
}

function ridipingi() {
    creaGrafici();
    if (scena) scena.tema(colori());
}

function etichettaTempo(ts) {
    const d = new Date(ts * 1000);
    if (intervallo === '7g') return d.toLocaleString('it-IT', { weekday: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit' });
    return d.toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit' });
}

function disegnaAndamento() {
    if (!grafAndamento) return;
    const p = puntiAndamento;
    grafAndamento.data.labels = p.map(x => etichettaTempo(x.time));
    grafAndamento.data.datasets[0].data = p.map(x => Math.round(x.solar));
    grafAndamento.data.datasets[1].data = p.map(x => Math.round(Math.max(0, x.grid - x.wb)));
    grafAndamento.data.datasets[2].data = p.map(x => Math.round(Math.max(0, x.wb)));
    grafAndamento.update();
    if ($('tab_andamento').closest('details').open) tabellaAndamento();
}

function tabella(id, intestazioni, righe) {
    const t = $(id);
    t.textContent = '';
    const testa = t.createTHead().insertRow();
    intestazioni.forEach(h => { const th = document.createElement('th'); th.textContent = h; testa.appendChild(th); });
    const corpo = t.createTBody();
    righe.forEach(r => { const tr = corpo.insertRow(); r.forEach(v => { tr.insertCell().textContent = v; }); });
}
function tabellaAndamento() {
    const passo = Math.max(1, Math.ceil(puntiAndamento.length / 60));
    const righe = puntiAndamento.filter((_, i) => i % passo === 0).reverse().map(x => [
        intervallo === '7g' || intervallo === '24h' ? giornoOra(x.time) : ora(x.time),
        nf0.format(x.solar), nf0.format(Math.max(0, x.grid - x.wb)), nf0.format(Math.max(0, x.wb))]);
    tabella('tab_andamento', ['Ora', 'Produzione (W)', 'Casa (W)', 'Auto (W)'], righe);
}
function tabellaGiorni() {
    tabella('tab_giorni', ['Giorno', 'Prodotti', 'In auto', 'dal sole', 'dalla rete', 'Risparmio'],
        giorni.slice().reverse().map(g => {
            const fv = g.wallbox_da_fv_kwh || 0, tot = g.wallbox_kwh || 0;
            return [(g.giorno || '').slice(5).split('-').reverse().join('/'), kwh(g.solare_kwh), kwh(tot), kwh(fv),
                    kwh(Math.max(0, tot - fv)), manca(g.risparmio_eur) ? '--' : nf2.format(g.risparmio_eur) + ' €'];
        }));
}
$('tab_andamento').closest('details').addEventListener('toggle', e => { if (e.target.open) tabellaAndamento(); });
$('tab_giorni').closest('details').addEventListener('toggle', e => { if (e.target.open) tabellaGiorni(); });

async function caricaAndamento() {
    try {
        const d = await (await fetch('/api/storico?range=' + intervallo, { cache: 'no-store' })).json();
        if (d.success) { puntiAndamento = d.punti || []; disegnaAndamento(); }
    } catch (e) { /* si riprova al prossimo giro: resta il disegno di prima */ }
}
function mostraIntervallo() {
    document.querySelectorAll('#filtri_andamento button').forEach(b => {
        b.classList.toggle('attivo', b.dataset.r === intervallo);
        b.setAttribute('aria-pressed', b.dataset.r === intervallo);
    });
}
document.querySelectorAll('#filtri_andamento button').forEach(b => b.addEventListener('click', () => {
    intervallo = b.dataset.r;
    memoria('intervallo', intervallo);
    mostraIntervallo();
    caricaAndamento();
}));

function disegnaGiorni() {
    if (!grafGiorni) return;
    const fv = giorni.map(g => g.wallbox_da_fv_kwh || 0);
    grafGiorni.data.labels = giorni.map(g => (g.giorno || '').slice(5).split('-').reverse().join('/'));
    grafGiorni.data.datasets[0].data = fv;
    grafGiorni.data.datasets[1].data = giorni.map((g, i) => Math.max(0, (g.wallbox_kwh || 0) - fv[i]));
    grafGiorni.data.datasets[2].data = giorni.map(g => g.solare_kwh || 0);
    grafGiorni.update();
    if ($('tab_giorni').closest('details').open) tabellaGiorni();
}

async function caricaGiorni() {
    try {
        const d = await (await fetch('/api/giorni?quanti=14', { cache: 'no-store' })).json();
        if (!d.success) return;
        giorni = d.giorni || [];
        const somma = k => giorni.reduce((a, g) => a + (g[k] || 0), 0);
        const totAuto = somma('wallbox_kwh'), totFv = somma('wallbox_da_fv_kwh');
        testo('g_solare', nf0.format(somma('solare_kwh')));
        testo('g_auto', nf0.format(totAuto));
        testo('g_eff', totAuto > 0 ? Math.round(totFv / totAuto * 100) + '%' : '--');
        testo('g_eur', nf2.format(somma('risparmio_eur')) + ' €');
        disegnaGiorni();
    } catch (e) { /* si riprova fra un minuto */ }
}

async function caricaSessioni() {
    let d;
    try {
        d = await (await fetch('/api/sessioni?quante=8', { cache: 'no-store' })).json();
    } catch (e) { return; }
    if (!d.success) return;
    const corso = $('sessione_corso');
    corso.hidden = !d.in_corso;
    if (d.in_corso) corso.textContent = 'In corso da ' + durata(d.in_corso.minuti) + ' · '
        + kwh(d.in_corso.kwh, 2) + ' kWh' + (d.in_corso.misurata ? '' : ' (stima)');
    const elenco = $('elenco_sessioni');
    elenco.textContent = '';
    if (!d.sessioni.length) {
        const li = document.createElement('li');
        li.className = 'vuoto';
        li.textContent = 'Nessuna sessione registrata finora.';
        elenco.appendChild(li);
        return;
    }
    d.sessioni.forEach(s => {
        const li = document.createElement('li');
        const quando = document.createElement('span');
        quando.className = 'quando';
        quando.textContent = giornoOra(s.inizio) + ' · ' + durata(s.minuti);
        const energia = document.createElement('span');
        energia.className = 'kwh';
        energia.textContent = kwh(s.kwh, 2) + ' kWh';
        li.append(quando, energia);
        if (!manca(s.quota_fv)) {
            const quota = document.createElement('span');
            quota.className = 'quota';
            const barra = document.createElement('span');
            barra.className = 'barra';
            const pieno = document.createElement('i');
            pieno.className = 'b-sole';
            pieno.style.width = Math.max(0, Math.min(100, s.quota_fv)) + '%';
            barra.appendChild(pieno);
            quota.append(barra, Math.round(Math.max(0, s.quota_fv)) + '% dal sole');
            li.appendChild(quota);
        }
        elenco.appendChild(li);
    });
}

/* ------------------------------------------------------------------
   Avvio e ritmo
   ------------------------------------------------------------------ */
if (memoria('tecnici') === 'aperti') $('dettagli_tecnici').open = true;
$('dettagli_tecnici').addEventListener('toggle', e => memoria('tecnici', e.target.open ? 'aperti' : 'chiusi'));

mostraIntervallo();
creaGrafici();
aggiorna();
caricaAndamento();
caricaGiorni();
caricaSessioni();
avviaScena();
addEventListener('resize', misuraEtichette);

/* A pagina nascosta non si interroga il Raspberry: al ritorno si riparte subito */
let timer = [];
function avviaTimer() {
    fermaTimer();
    timer = [setInterval(aggiorna, INTERVALLO_MS),
             setInterval(caricaAndamento, 60000),
             setInterval(caricaGiorni, 120000),
             setInterval(caricaSessioni, 30000)];
}
function fermaTimer() { timer.forEach(clearInterval); timer = []; }
document.addEventListener('visibilitychange', () => {
    if (document.hidden) { fermaTimer(); return; }
    aggiorna(); caricaAndamento(); caricaSessioni();
    avviaTimer();
});
if (!document.hidden) avviaTimer();
