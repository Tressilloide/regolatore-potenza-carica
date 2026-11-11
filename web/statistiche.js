/* Statistiche - pagina di lungo periodo della casa solare.
   Una sola richiesta per periodo (/api/statistiche): i conti li fa il
   Raspberry (statistiche.py), qui si disegna soltanto.
   Chart.js e' caricato come script classico (window.Chart). */

const $ = id => document.getElementById(id);

/* ------------------------------------------------------------------
   Formati
   ------------------------------------------------------------------ */
const nf0 = new Intl.NumberFormat('it-IT', { maximumFractionDigits: 0 });
const nf1 = new Intl.NumberFormat('it-IT', { minimumFractionDigits: 1, maximumFractionDigits: 1 });
const nf2 = new Intl.NumberFormat('it-IT', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const manca = v => v === null || v === undefined || Number.isNaN(v);
const GIORNI_SETT = ['lun', 'mar', 'mer', 'gio', 'ven', 'sab', 'dom'];

/* energia in kWh -> [numero, unita'] con le cifre che servono */
function energia(v) {
    if (manca(v)) return ['--', 'kWh'];
    const a = Math.abs(v);
    if (a >= 10000) return [nf2.format(v / 1000), 'MWh'];
    if (a >= 100) return [nf0.format(v), 'kWh'];
    return [nf1.format(v), 'kWh'];
}
const kwh = v => energia(v).join(' ');
const euro = v => manca(v) ? '--' : (Math.abs(v) >= 100 ? nf0 : nf2).format(v) + ' €';
const pct = v => manca(v) ? '--' : nf0.format(v) + '%';
function watt(w) {
    if (manca(w)) return '--';
    return Math.abs(w) < 1000 ? nf0.format(Math.round(w / 10) * 10) + ' W' : nf1.format(w / 1000) + ' kW';
}
function durata(minuti) {
    if (manca(minuti)) return '--';
    if (minuti < 60) return minuti + ' min';
    const h = Math.floor(minuti / 60), m = Math.round(minuti % 60);
    return m ? `${h} h ${m} min` : `${h} h`;
}
const ore = minuti => minuti >= 600 ? nf0.format(minuti / 60) + ' h' : minuti >= 60 ? nf1.format(minuti / 60) + ' h' : nf0.format(minuti) + ' min';
const data = iso => { const [a, m, g] = iso.split('-').map(Number); return new Date(a, m - 1, g); };
const dataBreve = iso => data(iso).toLocaleDateString('it-IT', { day: 'numeric', month: 'short' });
const dataMedia = iso => data(iso).toLocaleDateString('it-IT', { weekday: 'short', day: 'numeric', month: 'short' });
const dataLunga = iso => data(iso).toLocaleDateString('it-IT', { day: 'numeric', month: 'long', year: 'numeric' });
const dataOra = ts => new Date(ts * 1000).toLocaleString('it-IT', { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
const nomeMese = (m, lungo) => data(m + '-01').toLocaleDateString('it-IT', lungo ? { month: 'long', year: 'numeric' } : { month: 'short', year: '2-digit' });
const fasciaOra = h => `${String(h).padStart(2, '0')}–${String((h + 1) % 24).padStart(2, '0')}`;

function memoria(chiave, valore) {
    try {
        if (valore === undefined) return localStorage.getItem(chiave);
        localStorage.setItem(chiave, valore);
    } catch (e) { /* navigazione privata: si vive senza */ }
    return null;
}
function elemento(tag, classe, testo) {
    const e = document.createElement(tag);
    if (classe) e.className = classe;
    if (testo !== undefined) e.textContent = testo;
    return e;
}

/* ------------------------------------------------------------------
   Tema e colori (stessi ruoli del pannello)
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
             asse: v('--asse'), superficie: v('--superficie'), superficie2: v('--superficie-2'), critico: v('--critico') };
}
applicaTema();
$('btn_tema').addEventListener('click', () => {
    tema = TEMI[(TEMI.indexOf(tema) + 1) % TEMI.length];
    memoria('tema', tema);
    applicaTema();
    disegna();
});
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => { if (tema === 'auto') disegna(); });

const alfa = (hex, a) => hex + Math.round(a * 255).toString(16).padStart(2, '0');
const rgb = hex => [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16));
/* Scala a una tinta: dal fondo della scheda al colore pieno */
function scala(base, tinta) {
    const a = rgb(base), b = rgb(tinta);
    return t => {
        if (!(t >= .02)) return base;              // quasi niente: resta il fondo
        const k = .12 + .88 * Math.pow(Math.min(1, t), .75);
        return 'rgb(' + a.map((v, i) => Math.round(v + (b[i] - v) * k)).join(',') + ')';
    };
}

/* ------------------------------------------------------------------
   Stato della pagina
   ------------------------------------------------------------------ */
const PERIODI = ['7g', '30g', '90g', 'anno', 'tutto'];
const stato = {
    periodo: memoria('stat_periodo') || '30g',
    tempo: memoria('stat_tempo') || 'sole',
    mappa: memoria('stat_mappa') || 'sole',
    profilo: 'tutti',
    calendario: memoria('stat_calendario') || 'autosufficienza',
};
if (!PERIODI.includes(stato.periodo) && !/^\d{4}-\d{2}$/.test(stato.periodo)) stato.periodo = '30g';
/* 'g:AAAA-MM-GG' = un giorno solo (scelto dal calendario): non si ricorda fra una visita e l'altra */
const giornoScelto = () => stato.periodo.startsWith('g:') ? stato.periodo.slice(2) : null;
const queryPeriodo = () => giornoScelto() ? `da=${giornoScelto()}&a=${giornoScelto()}` : 'periodo=' + encodeURIComponent(stato.periodo);
function scegliPeriodo(p) {
    stato.periodo = p;
    if (!p.startsWith('g:')) memoria('stat_periodo', p);
    carica();
}
let d = null;                 // ultima risposta di /api/statistiche
const grafici = {};

const IPOTESI_BASE = { auto: 16, litri: 6.5, benzina: 1.8, co2: 0.25 };
let ipotesi = { ...IPOTESI_BASE };
try { ipotesi = { ...IPOTESI_BASE, ...JSON.parse(memoria('stat_ipotesi') || '{}') }; } catch (e) { /* tengo le basi */ }

/* ------------------------------------------------------------------
   Suggerimento (uno solo per tutta la pagina)
   ------------------------------------------------------------------ */
function mostraSuggerimento(x, y, titolo, voci) {
    const box = $('suggerimento');
    box.textContent = '';
    box.appendChild(elemento('div', 'titolo', titolo));
    voci.forEach(v => {
        const riga = elemento('div', 'voce');
        if (v.colore) {
            const tinta = elemento('i', 'tinta' + (v.linea ? ' linea' : ''));
            tinta.style.background = v.colore;
            riga.appendChild(tinta);
        }
        riga.append(v.nome, elemento('b', '', v.valore));
        box.appendChild(riga);
    });
    box.hidden = false;
    const w = box.offsetWidth, h = box.offsetHeight;
    box.style.left = Math.max(8, Math.min(innerWidth - w - 8, x + (x + w + 24 > innerWidth ? -w - 14 : 14))) + 'px';
    box.style.top = Math.max(8, Math.min(innerHeight - h - 8, y - h / 2)) + 'px';
}
const nascondiSuggerimento = () => { $('suggerimento').hidden = true; };
addEventListener('scroll', nascondiSuggerimento, { passive: true });

/* per Chart.js: `formato(valore, serie)` -> testo */
function suggerimentoGrafico(formato, titolo) {
    return ({ chart, tooltip }) => {
        if (!tooltip || tooltip.opacity === 0 || !tooltip.dataPoints?.length) { nascondiSuggerimento(); return; }
        const punti = tooltip.dataPoints.filter(p => !manca(p.parsed.y));
        const r = chart.canvas.getBoundingClientRect();
        mostraSuggerimento(r.left + tooltip.caretX, r.top + tooltip.caretY,
            titolo ? titolo(punti[0]?.dataIndex ?? 0) : (tooltip.title?.[0] || ''),
            punti.map(p => ({ colore: p.dataset.coloreSerie, linea: p.dataset.type === 'line' || chart.config.type === 'line' && p.dataset.fill === false,
                              nome: p.dataset.label, valore: formato(p.parsed.y, p.dataset) })));
    };
}

const mirino = {
    id: 'mirino',
    afterDatasetsDraw(chart) {
        const attivi = chart.tooltip?.getActiveElements?.() || [];
        if (!attivi.length || chart.config.type !== 'line') return;
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

function opzioniBase(col, unitaAsse) {
    return {
        responsive: true, maintainAspectRatio: false, animation: false,
        interaction: { mode: 'index', intersect: false },
        plugins: { legend: { display: false }, tooltip: { enabled: false } },
        scales: {
            x: { ticks: { color: col.testo3, maxTicksLimit: 8, maxRotation: 0, autoSkip: true, font: { size: 11 } },
                 grid: { display: false }, border: { color: col.asse } },
            y: { beginAtZero: true,
                 title: { display: true, text: unitaAsse, color: col.testo3, font: { size: 11 } },
                 ticks: { color: col.testo3, font: { size: 11 }, maxTicksLimit: 6 },
                 grid: { color: col.griglia, drawTicks: false }, border: { display: false } }
        }
    };
}
function grafico(id, config) {
    grafici[id]?.destroy();
    delete grafici[id];
    if (!window.Chart) return null;
    config.plugins = [mirino, ...(config.plugins || [])];
    grafici[id] = new Chart($(id), config);
    return grafici[id];
}
/* barre: estremita' arrotondata sul lato libero */
const barra = (col, etichetta, colore, dati, altro = {}) => ({
    label: etichetta, coloreSerie: colore, data: dati, backgroundColor: colore,
    borderRadius: 4, maxBarThickness: 26, ...altro });

function tabella(id, intestazioni, righe) {
    const t = $(id);
    t.textContent = '';
    const testa = t.createTHead().insertRow();
    intestazioni.forEach(h => testa.appendChild(elemento('th', '', h)));
    const corpo = t.createTBody();
    righe.forEach(r => { const tr = corpo.insertRow(); r.forEach(v => { tr.insertCell().textContent = v; }); });
}
function legenda(id, voci) {
    const box = $(id);
    box.textContent = '';
    voci.forEach(([colore, nome, linea]) => {
        const s = elemento('span');
        const t = elemento('i', 'tinta' + (linea ? ' linea' : ''));
        t.style.background = colore;
        s.append(t, nome);
        box.appendChild(s);
    });
}
function riquadri(id, voci) {
    const box = $(id);
    box.textContent = '';
    voci.filter(Boolean).forEach(([valore, unita, nome, nota]) => {
        const r = elemento('div', 'riquadro');
        const b = elemento('b', '', valore);
        if (unita) b.appendChild(elemento('small', '', unita));
        r.append(b, elemento('span', '', nome));
        if (nota) r.appendChild(elemento('em', '', nota));
        box.appendChild(r);
    });
}
function filtri(id, chiave, dopo) {
    const box = $(id);
    const segna = () => box.querySelectorAll('button').forEach(b => {
        b.classList.toggle('attivo', b.dataset.v === stato[chiave]);
        b.setAttribute('aria-pressed', b.dataset.v === stato[chiave]);
    });
    box.addEventListener('click', e => {
        const b = e.target.closest('button');
        if (!b) return;
        stato[chiave] = b.dataset.v;
        memoria('stat_' + chiave, b.dataset.v);
        segna();
        dopo();
    });
    if (![...box.querySelectorAll('button')].some(b => b.dataset.v === stato[chiave])) {
        stato[chiave] = box.querySelector('button').dataset.v;
    }
    segna();
}

/* ------------------------------------------------------------------
   Caricamento
   ------------------------------------------------------------------ */
let richiesta = 0;
async function carica() {
    const numero = ++richiesta;
    segnaPeriodo();
    $('periodo_testo').textContent = 'Carico…';
    try {
        const r = await fetch('/api/statistiche?' + queryPeriodo(), { cache: 'no-store' });
        const corpo = await r.json();
        if (numero !== richiesta) return;            // nel frattempo e' stato scelto un altro periodo
        if (!r.ok) throw new Error(corpo.messaggio || 'Errore ' + r.status);
        d = corpo;
        $('avvisi').textContent = '';
        disegna();
    } catch (e) {
        if (numero !== richiesta) return;
        avviso('Statistiche non disponibili', e.message || 'Il Raspberry non risponde.');
        $('periodo_testo').textContent = '';
    }
}
function avviso(titolo, testo) {
    const box = $('avvisi');
    box.textContent = '';
    const a = elemento('div', 'avviso');
    a.dataset.g = 'errore';
    const corpo = elemento('div');
    corpo.append(elemento('b', '', titolo + '. '), testo);
    a.appendChild(corpo);
    box.appendChild(a);
}
function segnaPeriodo() {
    $('filtri_periodo').querySelectorAll('button').forEach(b => {
        b.classList.toggle('attivo', b.dataset.p === stato.periodo);
        b.setAttribute('aria-pressed', b.dataset.p === stato.periodo);
    });
    const mese = /^\d{4}-\d{2}$/.test(stato.periodo);
    $('sel_mese').value = mese ? stato.periodo : '';
    $('sel_mese').classList.toggle('attivo', mese);
    $('csv').href = '/api/statistiche.csv?' + queryPeriodo();
    $('passo_giorno').hidden = !giornoScelto();
}
$('filtri_periodo').addEventListener('click', e => {
    const b = e.target.closest('button');
    if (!b) return;
    scegliPeriodo(b.dataset.p);
});
$('sel_mese').addEventListener('change', e => { if (e.target.value) scegliPeriodo(e.target.value); });
function passoGiorno(verso) {
    const g = giornoScelto();
    if (!g || !d?.calendario) return;
    const tutti = d.calendario.map(c => c[0]);
    const nuovo = tutti[tutti.indexOf(g) + verso];
    if (nuovo) scegliPeriodo('g:' + nuovo);
}
$('btn_prima').addEventListener('click', () => passoGiorno(-1));
$('btn_dopo').addEventListener('click', () => passoGiorno(1));

/* ------------------------------------------------------------------
   Disegno
   ------------------------------------------------------------------ */
function disegna() {
    if (!d) return;
    nascondiSuggerimento();
    const cop = d.copertura;
    if (!cop) {
        $('contenuto').hidden = true;
        $('periodo_testo').textContent = '';
        avviso('Ancora niente da mostrare', 'L’archivio delle statistiche è vuoto: si riempie da solo, un giorno alla volta.');
        return;
    }
    $('copertura').textContent = `Casa solare · dati dal ${dataLunga(cop.primo)} (${nf0.format(cop.giorni)} giorni)`;
    const sel = $('sel_mese');
    if (sel.options.length !== cop.mesi.length + 1) {
        sel.length = 1;
        cop.mesi.slice().reverse().forEach(m => sel.add(new Option(nomeMese(m, true), m)));
        segnaPeriodo();
    }
    if (d.vuoto) {
        $('contenuto').hidden = true;
        $('periodo_testo').textContent = 'Nessun dato in questo periodo';
        return;
    }
    const p = d.periodo;
    let testo = p.da === p.a ? data(p.da).toLocaleDateString('it-IT', { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' })
        : `${dataBreve(p.da)} – ${dataLunga(p.a)} · ${p.giorni} giorni`;
    if (giornoScelto()) {
        const tutti = d.calendario.map(c => c[0]), i = tutti.indexOf(p.da);
        $('btn_prima').disabled = i <= 0;
        $('btn_dopo').disabled = i < 0 || i >= tutti.length - 1;
    }
    if (p.a === d.oggi) testo += ' · oggi fino alle ' + new Date(d.adesso * 1000).toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit' });
    $('periodo_testo').textContent = testo;
    $('piede_ora').textContent = 'aggiornate alle ' + new Date(d.adesso * 1000).toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit' });
    $('contenuto').hidden = false;

    const col = colori();
    const passi = [tessere, flussi, nelTempo, mappa, profilo, settimana, calendario, sole, casa, rete, ricariche, soldi, primati, mesi];
    passi.forEach(passo => {
        try { passo(col); } catch (e) { console.error(passo.name, e); }
    });
}

/* --- tessere in testa ------------------------------------------------ */
function scintilla(valori, colore) {
    const ns = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('class', 'scintilla');
    svg.setAttribute('viewBox', '0 0 100 34');
    svg.setAttribute('preserveAspectRatio', 'none');
    svg.setAttribute('aria-hidden', 'true');
    const v = valori.map(x => manca(x) ? 0 : x);
    if (v.length < 2) return svg;
    const min = Math.min(0, ...v), max = Math.max(...v);
    const y = x => 31 - (max > min ? (x - min) / (max - min) : 0) * 27;
    const punti = v.map((x, i) => `${(i / (v.length - 1) * 100).toFixed(2)},${y(x).toFixed(2)}`);
    const area = document.createElementNS(ns, 'path');
    area.setAttribute('d', `M0,34 L${punti.join(' L')} L100,34 Z`);
    area.setAttribute('fill', alfa(colore, .16));
    const linea = document.createElementNS(ns, 'path');
    linea.setAttribute('d', 'M' + punti.join(' L'));
    linea.setAttribute('fill', 'none');
    linea.setAttribute('stroke', colore);
    linea.setAttribute('stroke-width', '2');
    linea.setAttribute('stroke-linejoin', 'round');
    linea.setAttribute('vector-effect', 'non-scaling-stroke');
    svg.append(area, linea);
    return svg;
}
function variazione(adesso, prima, punti) {
    if (manca(adesso) || manca(prima)) return null;
    if (punti) {
        const diff = adesso - prima;
        return (diff >= 0 ? '+' : '−') + nf1.format(Math.abs(diff)).replace(',0', '') + ' punti';
    }
    if (Math.abs(prima) < 0.05) return null;
    const diff = (adesso - prima) / Math.abs(prima) * 100;
    return (diff >= 0 ? '+' : '−') + nf0.format(Math.abs(diff)) + '%';
}
function tessere(col) {
    const t = d.totali, prima = d.precedente, g = d.giorni;
    const box = $('tessere');
    box.textContent = '';
    const voci = [
        { nome: 'Prodotta', tinta: col.sole, v: energia(t.sole),
          sotto: t.giorni === 1 ? `punta di ${watt(g[0].picco_sole)}` : `${nf1.format(t.sole / t.giorni)} kWh al giorno`,
          delta: variazione(t.sole, prima?.sole), serie: g.map(x => x.sole) },
        { nome: 'Consumata', tinta: col.casa, v: energia(t.consumo), sotto: `casa ${kwh(t.casa)} · auto ${kwh(t.auto)}`,
          delta: variazione(t.consumo, prima?.consumo), serie: g.map(x => x.casa + x.auto) },
        { nome: 'Autosufficienza', tinta: col.sole, v: [manca(t.autosufficienza_pct) ? '--' : nf0.format(t.autosufficienza_pct), '%'],
          sotto: 'dei consumi coperti dal sole',
          delta: variazione(t.autosufficienza_pct, prima?.autosufficienza_pct, true), serie: g.map(x => x.autosufficienza) },
        { nome: 'Autoconsumo', tinta: col.sole, v: [manca(t.autoconsumo_pct) ? '--' : nf0.format(t.autoconsumo_pct), '%'],
          sotto: 'della produzione usata in casa',
          delta: variazione(t.autoconsumo_pct, prima?.autoconsumo_pct, true), serie: g.map(x => x.autoconsumo) },
        { nome: 'Prelevata dalla rete', tinta: col.rete, v: energia(t.prelievo), sotto: `spesa ${euro(t.euro.spesa)}`,
          delta: variazione(t.prelievo, prima?.prelievo), serie: g.map(x => x.prelievo) },
        { nome: 'Immessa in rete', tinta: col.rete, v: energia(t.immessa), sotto: `ricavo ${euro(t.euro.ricavo)}`,
          delta: variazione(t.immessa, prima?.immessa), serie: g.map(x => x.immessa) },
        { nome: 'In auto', tinta: col.auto, v: energia(t.auto),
          sotto: manca(t.auto_sole_pct) ? 'nessuna ricarica' : `${pct(t.auto_sole_pct)} dal sole · ≈ ${nf0.format(t.auto / ipotesi.auto * 100)} km`,
          delta: variazione(t.auto, prima?.auto), serie: g.map(x => x.auto) },
        { nome: 'Resa dell’impianto', tinta: col.neutro, v: [(Math.abs(t.euro.beneficio) >= 100 ? nf0 : nf2).format(t.euro.beneficio), '€'],
          sotto: t.euro.netto < 0 ? `${euro(-t.euro.netto)} a tuo favore invece di ${euro(t.euro.senza_fv)} da pagare`
              : `bolletta ${euro(t.euro.netto)} invece di ${euro(t.euro.senza_fv)}`,
          delta: variazione(t.euro.beneficio, prima?.euro.beneficio),
          serie: g.map(x => (x.sole - x.immessa) * d.prezzi.acquisto + x.immessa * d.prezzi.vendita) },
    ];
    voci.forEach(v => {
        const tessera = elemento('div', 'tessera');
        const nome = elemento('div', 'nome');
        const tinta = elemento('i', 'tinta');
        tinta.style.background = v.tinta;
        nome.append(tinta, v.nome);
        const valore = elemento('div', 'valore', v.v[0]);
        valore.appendChild(elemento('small', '', v.v[1]));
        const delta = elemento('div', 'delta');
        if (v.delta) delta.append(elemento('b', '', v.delta), ' sul periodo prima');
        tessera.append(nome, valore, elemento('div', 'sotto', v.sotto), delta, scintilla(v.serie, v.tinta));
        box.appendChild(tessera);
    });
}

/* --- flussi (Sankey a due colonne) ----------------------------------- */
function flussi(col) {
    const t = d.totali;
    const box = $('flussi');
    box.textContent = '';
    $('flussi_totale').textContent = `${kwh(t.sole + t.prelievo)} in gioco`;
    const sorgenti = [
        { id: 'sole', nome: 'Sole', v: t.sole, colore: col.sole },
        { id: 'rete', nome: 'Rete', v: t.prelievo, colore: col.rete },
    ];
    const mete = [
        { id: 'immessa', nome: 'Immessa in rete', v: t.immessa, colore: col.rete },
        { id: 'casa', nome: 'Casa', v: t.casa, colore: col.casa },
        { id: 'auto', nome: 'Auto', v: t.auto, colore: col.auto },
    ];
    const nastri = [
        ['sole', 'immessa', t.immessa], ['sole', 'casa', t.casa_sole], ['sole', 'auto', t.auto_sole],
        ['rete', 'casa', t.casa_rete], ['rete', 'auto', t.auto_rete],
    ].filter(n => n[2] > 0.005);
    const totale = t.sole + t.prelievo;
    if (!(totale > 0)) { box.appendChild(elemento('p', 'nota', 'Nessuna energia misurata nel periodo.')); return; }

    const L = Math.max(300, box.clientWidth || 600), stretto = L < 460;
    const A = stretto ? 250 : 290, spazio = 16, nodo = 12;
    const margineSx = stretto ? 84 : 112, margineDx = stretto ? 110 : 150;
    const scalaV = (A - spazio * 2) / totale;
    const x0 = margineSx, x1 = L - margineDx - nodo;

    const disponi = (nodi, alto) => {
        const occupato = nodi.reduce((s, n) => s + n.v * scalaV, 0) + spazio * (nodi.length - 1);
        let y = (A - occupato) / 2;
        nodi.forEach(n => { n.y = y; n.h = Math.max(n.v > 0 ? 2 : 0, n.v * scalaV); n.cursore = y; y += n.h + spazio; });
    };
    disponi(sorgenti);
    disponi(mete);

    const ns = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('viewBox', `0 0 ${L} ${A}`);
    svg.setAttribute('width', L);
    svg.setAttribute('height', A);
    const crea = (tag, attributi, testo) => {
        const e = document.createElementNS(ns, tag);
        Object.entries(attributi).forEach(([k, v]) => e.setAttribute(k, v));
        if (testo !== undefined) e.textContent = testo;
        return e;
    };
    nastri.forEach(([da, a, v]) => {
        const s = sorgenti.find(n => n.id === da), m = mete.find(n => n.id === a);
        const h = v * scalaV, ys = s.cursore, ym = m.cursore;
        s.cursore += h; m.cursore += h;
        const xa = x0 + nodo, xb = x1, xm = (xa + xb) / 2;
        const nastro = crea('path', {
            class: 'nastro',
            d: `M${xa},${ys} C${xm},${ys} ${xm},${ym} ${xb},${ym} L${xb},${ym + h} C${xm},${ym + h} ${xm},${ys + h} ${xa},${ys + h} Z`,
            fill: s.colore, 'fill-opacity': .38 });
        const titolo = `${s.nome} → ${m.nome}`;
        nastro.addEventListener('pointermove', e => mostraSuggerimento(e.clientX, e.clientY, titolo, [
            { colore: s.colore, nome: 'Energia', valore: kwh(v) },
            { nome: 'Quota della sorgente', valore: pct(v / s.v * 100) }]));
        nastro.addEventListener('pointerleave', nascondiSuggerimento);
        svg.appendChild(nastro);
    });
    const etichetta = (n, sinistra) => {
        if (!(n.v > 0.005)) return;
        svg.appendChild(crea('rect', { x: sinistra ? x0 : x1, y: n.y, width: nodo, height: n.h, rx: 3, fill: n.colore }));
        const x = sinistra ? x0 - 9 : x1 + nodo + 9, ancora = sinistra ? 'end' : 'start';
        const yc = n.y + n.h / 2;
        svg.appendChild(crea('text', { x, y: yc - 3, 'text-anchor': ancora, class: 'numero' }, kwh(n.v)));
        svg.appendChild(crea('text', { x, y: yc + 12, 'text-anchor': ancora }, n.nome));
    };
    sorgenti.forEach(n => etichetta(n, true));
    mete.forEach(n => etichetta(n, false));
    box.appendChild(svg);

    const quota = (id, v) => { $(id).textContent = pct(v); $(id + '_b').style.width = (manca(v) ? 0 : Math.min(100, v)) + '%'; };
    quota('q_autosuff', t.autosufficienza_pct);
    quota('q_autocons', t.autoconsumo_pct);
    quota('q_auto', t.auto_sole_pct);
    $('q_auto_nota').textContent = manca(t.auto_sole_pct) ? 'Nessuna ricarica nel periodo.'
        : `${kwh(t.auto_sole)} dal fotovoltaico, ${kwh(t.auto_rete)} dalla rete.`;
    tabella('tab_flussi', ['Da', 'A', 'kWh', 'Quota'], nastri.map(([da, a, v]) => [
        sorgenti.find(n => n.id === da).nome, mete.find(n => n.id === a).nome, nf1.format(v), pct(v / totale * 100)]));
}

/* --- nel tempo ------------------------------------------------------- */
function raggruppa(giorni) {
    /* giorni -> barre: per giorno fino a due mesi, poi per settimana, poi per mese */
    const n = giorni.length;
    if (n === 1) {
        /* un giorno solo: le sue ventiquattro ore */
        const o = d.ore, k = c => o[c][0];
        return { passo: 'ora', barre: Array.from({ length: 24 }, (_, h) => ({
            nome: String(h).padStart(2, '0'), esteso: 'Ore ' + fasciaOra(h),
            sole: k('sole')[h] / 1000, casa: k('casa')[h] / 1000, auto: k('auto')[h] / 1000,
            auto_sole: k('auto_sole')[h] / 1000, prelievo: k('prelievo')[h] / 1000, immessa: k('immessa')[h] / 1000 })) };
    }
    const chiave = n <= 62 ? g => g.g : n <= 400 ? g => {
        const dt = data(g.g);
        dt.setDate(dt.getDate() - ((dt.getDay() + 6) % 7));           // il lunedi' della settimana
        return `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, '0')}-${String(dt.getDate()).padStart(2, '0')}`;
    } : g => g.g.slice(0, 7);
    const passo = n <= 62 ? 'giorno' : n <= 400 ? 'settimana' : 'mese';
    const gruppi = new Map();
    giorni.forEach(g => {
        const k = chiave(g);
        if (!gruppi.has(k)) gruppi.set(k, { k, n: 0, sole: 0, casa: 0, auto: 0, auto_sole: 0, prelievo: 0, immessa: 0 });
        const s = gruppi.get(k);
        s.n++;
        ['sole', 'casa', 'auto', 'auto_sole', 'prelievo', 'immessa'].forEach(c => { s[c] += g[c]; });
    });
    const nome = passo === 'giorno' ? k => dataBreve(k) : passo === 'settimana' ? k => 'sett. ' + dataBreve(k) : k => nomeMese(k);
    const esteso = passo === 'giorno' ? k => dataMedia(k) : passo === 'settimana' ? k => 'Settimana dal ' + dataBreve(k) : k => nomeMese(k, true);
    return { passo, barre: [...gruppi.values()].map(s => ({ ...s, nome: nome(s.k), esteso: esteso(s.k) })) };
}
function nelTempo(col) {
    const { passo, barre } = raggruppa(d.giorni);
    const vista = stato.tempo;
    const fm = passo === 'ora' ? nf2 : nf1;
    /* 2 px del colore della scheda fra i segmenti; arrotondata solo la cima della pila */
    const sep = { borderColor: col.superficie, borderWidth: { top: 2 }, borderSkipped: false };
    const cima = ctx => {
        const sopra = ctx.chart.data.datasets.slice(ctx.datasetIndex + 1).some(s => Math.abs(s.data[ctx.dataIndex] || 0) > 0.005);
        return sopra ? 0 : { topLeft: 4, topRight: 4 };
    };
    let serie, righe, intestazioni;
    if (vista === 'consumi') {
        serie = [
            barra(col, 'Dal sole', col.sole, barre.map(b => b.casa + b.auto - b.prelievo), { borderRadius: cima }),
            barra(col, 'Dalla rete', col.rete, barre.map(b => b.prelievo), { ...sep, borderRadius: cima }),
        ];
        intestazioni = ['Periodo', 'Consumi', 'dal sole', 'dalla rete'];
        righe = barre.map(b => [b.esteso, fm.format(b.casa + b.auto), fm.format(b.casa + b.auto - b.prelievo), fm.format(b.prelievo)]);
    } else if (vista === 'rete') {
        serie = [
            barra(col, 'Prelevata', col.rete, barre.map(b => b.prelievo)),
            barra(col, 'Immessa', col.sole, barre.map(b => -b.immessa)),
        ];
        intestazioni = ['Periodo', 'Prelevata', 'Immessa', 'Saldo'];
        righe = barre.map(b => [b.esteso, fm.format(b.prelievo), fm.format(b.immessa), fm.format(b.immessa - b.prelievo)]);
    } else {
        serie = [
            barra(col, 'Usato dalla casa', col.casa, barre.map(b => Math.max(0, b.sole - b.immessa - b.auto_sole)), { borderRadius: cima }),
            barra(col, 'Messo in auto', col.auto, barre.map(b => b.auto_sole), { ...sep, borderRadius: cima }),
            barra(col, 'Immesso in rete', col.rete, barre.map(b => b.immessa), { ...sep, borderRadius: cima }),
        ];
        intestazioni = ['Periodo', 'Prodotta', 'alla casa', 'all’auto', 'in rete'];
        righe = barre.map(b => [b.esteso, fm.format(b.sole), nf1.format(Math.max(0, b.sole - b.immessa - b.auto_sole)),
                                fm.format(b.auto_sole), fm.format(b.immessa)]);
    }
    legenda('leg_tempo', serie.map(s => [s.coloreSerie, s.label]));
    const o = opzioniBase(col, 'kWh per ' + passo);
    o.scales.x.stacked = true;
    o.scales.y.stacked = true;
    o.scales.y.ticks.callback = v => (passo === 'ora' ? nf1 : nf0).format(Math.abs(v));
    if (passo === 'ora') o.scales.x.ticks.maxTicksLimit = 12;
    o.plugins.tooltip.external = suggerimentoGrafico(v => (passo === 'ora' ? nf2 : nf1).format(Math.abs(v)) + ' kWh', i => barre[i].esteso);
    grafico('g_tempo', { type: 'bar', data: { labels: barre.map(b => b.nome), datasets: serie }, options: o });
    tabella('tab_tempo', intestazioni, righe.slice().reverse());
}

/* --- mappa delle ore -------------------------------------------------- */
const NOMI_MISURA = { sole: 'Produzione', casa: 'Casa', auto: 'Auto', prelievo: 'Prelievo', immessa: 'Immissione', autosufficienza: 'Autosufficienza' };
const tintaMisura = (col, m) => ({ sole: col.sole, casa: col.casa, auto: col.auto, prelievo: col.rete, immessa: col.rete, autosufficienza: col.sole })[m];
let geometriaMappa = null;

function mappa(col) {
    const misura = stato.mappa, valori = d.ore[misura], giorni = d.giorni;
    const tela = $('mappa'), scorri = $('mappa_scorri');
    const n = giorni.length;
    const sx = 38, alto = 4, basso = 20, riga = innerWidth < 560 ? 8 : 10;
    const larghezza = Math.max(200, scorri.clientWidth);
    const colonna = Math.max(3, (larghezza - sx) / n);          // a tutta larghezza; sotto i 3 px si scorre
    const L = Math.max(larghezza, Math.ceil(sx + colonna * n)), A = alto + riga * 24 + basso;
    const dpr = Math.min(2, devicePixelRatio || 1);
    tela.width = L * dpr; tela.height = A * dpr;
    tela.style.width = L + 'px'; tela.style.height = A + 'px';
    const ctx = tela.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, L, A);

    const tutti = valori.flat().filter(v => v > 0).sort((a, b) => a - b);
    const massimo = tutti.length ? tutti[Math.min(tutti.length - 1, Math.floor(tutti.length * .99))] : 1;
    const colore = scala(col.superficie2, tintaMisura(col, misura));
    const vuoto = colonna > 4 ? 1 : 0;
    for (let i = 0; i < n; i++) {
        for (let h = 0; h < 24; h++) {
            ctx.fillStyle = colore(valori[i][h] / massimo);
            const x = Math.round(sx + i * colonna);
            ctx.fillRect(x, alto + h * riga, Math.max(1, Math.round(sx + (i + 1) * colonna) - x - vuoto), riga - 1);
        }
    }
    ctx.fillStyle = col.testo3;
    ctx.font = '10.5px system-ui, sans-serif';
    ctx.textBaseline = 'middle';
    ctx.textAlign = 'right';
    [0, 6, 12, 18].forEach(h => ctx.fillText(String(h).padStart(2, '0') + ':00', sx - 5, alto + h * riga + riga / 2));
    ctx.textBaseline = 'top';
    ctx.textAlign = 'left';
    const ogni = Math.max(1, Math.ceil(64 / colonna));
    ctx.textAlign = 'center';
    for (let i = (n - 1) % ogni; i < n; i += ogni) {
        const x = Math.max(sx + 20, Math.min(L - 20, sx + (i + .5) * colonna));
        ctx.fillText(dataBreve(giorni[i].g), x, alto + riga * 24 + 5);
    }
    geometriaMappa = { sx, alto, riga, colonna, n, misura, tinta: tintaMisura(col, misura) };
    scorri.scrollLeft = scorri.scrollWidth;
    disegnaScala('mappa_scala', colore, '0', nf1.format(massimo / 1000) + ' kWh in un’ora' + (tutti.length ? '' : ' (nessun dato)'));
}
function disegnaScala(id, colore, minimo, massimo) {
    const box = $(id);
    box.textContent = '';
    const i = elemento('i');
    i.style.background = `linear-gradient(90deg, ${[0.001, .25, .5, .75, 1].map(colore).join(',')})`;
    box.append(minimo, i, massimo);
}
function puntaMappa(e) {
    const g = geometriaMappa;
    if (!g || !d) return;
    const r = $('mappa').getBoundingClientRect();
    const i = Math.floor((e.clientX - r.left - g.sx) / g.colonna), h = Math.floor((e.clientY - r.top - g.alto) / g.riga);
    if (i < 0 || i >= g.n || h < 0 || h > 23) { nascondiSuggerimento(); return; }
    mostraSuggerimento(e.clientX, e.clientY, `${dataMedia(d.giorni[i].g)} · ${fasciaOra(h)}`, [
        { colore: g.tinta, nome: NOMI_MISURA[g.misura], valore: nf2.format(d.ore[g.misura][i][h] / 1000) + ' kWh' }]);
}
$('mappa').addEventListener('pointermove', puntaMappa);
$('mappa').addEventListener('pointerdown', puntaMappa);
$('mappa').addEventListener('pointerleave', nascondiSuggerimento);

/* --- giornata e settimana tipo ----------------------------------------- */
function profilo(col) {
    const p = d.profilo[stato.profilo] || d.profilo.tutti;
    $('filtri_profilo').querySelectorAll('button').forEach(b => { b.disabled = !d.profilo[b.dataset.v]; });
    if (!p) return;
    const etichette = Array.from({ length: 24 }, (_, h) => String(h).padStart(2, '0'));
    const o = opzioniBase(col, 'kW');
    o.scales.y.stacked = true;
    o.scales.y.ticks.callback = v => nf1.format(v / 1000);
    o.scales.x.ticks.maxTicksLimit = 12;
    o.plugins.tooltip.external = suggerimentoGrafico(v => watt(v), i => 'Ore ' + fasciaOra(i));
    const comune = { pointRadius: 0, pointHoverRadius: 4, tension: .3 };
    grafico('g_profilo', { type: 'line', data: { labels: etichette, datasets: [
        { label: 'Produzione', coloreSerie: col.sole, data: p.sole, stack: 'produzione', fill: false,
          borderColor: col.sole, borderWidth: 2, order: 1, ...comune },
        { label: 'Casa', coloreSerie: col.casa, data: p.casa, stack: 'consumi', fill: 'origin',
          borderColor: col.casa, backgroundColor: alfa(col.casa, .26), borderWidth: 1.5, order: 2, ...comune },
        { label: 'Auto', coloreSerie: col.auto, data: p.auto, stack: 'consumi', fill: '-1',
          borderColor: col.auto, backgroundColor: alfa(col.auto, .26), borderWidth: 1.5, order: 2, ...comune },
    ] }, options: o });
    const picco = p.casa.indexOf(Math.max(...p.casa)), piccoSole = p.sole.indexOf(Math.max(...p.sole));
    $('profilo_nota').textContent = `Media di ${p.giorni} giorni. La casa consuma di più fra le ${fasciaOra(picco)} (${watt(p.casa[picco])}), ` +
        `il sole rende di più fra le ${fasciaOra(piccoSole)} (${watt(p.sole[piccoSole])}).`;
    tabella('tab_profilo', ['Ora', 'Produzione (W)', 'Casa (W)', 'Auto (W)', 'Prelievo (W)', 'Immissione (W)'],
        etichette.map((e, h) => [fasciaOra(h), nf0.format(p.sole[h]), nf0.format(p.casa[h]), nf0.format(p.auto[h]),
                                 nf0.format(p.prelievo[h]), nf0.format(p.immessa[h])]));
}
function settimana(col) {
    const s = d.settimana;
    const o = opzioniBase(col, 'kWh al giorno');
    o.plugins.tooltip.external = suggerimentoGrafico(v => nf1.format(v) + ' kWh',
        i => `${['Lunedì', 'Martedì', 'Mercoledì', 'Giovedì', 'Venerdì', 'Sabato', 'Domenica'][i]} · media di ${s[i].n} ${s[i].n === 1 ? 'giorno' : 'giorni'}`);
    const stretta = { maxBarThickness: 16, categoryPercentage: .78, barPercentage: .92 };
    grafico('g_settimana', { type: 'bar', data: { labels: GIORNI_SETT, datasets: [
        barra(col, 'Produzione', col.sole, s.map(x => x.sole), stretta),
        barra(col, 'Casa', col.casa, s.map(x => x.casa), stretta),
        barra(col, 'Auto', col.auto, s.map(x => x.auto), stretta),
    ] }, options: o });
    tabella('tab_settimana', ['Giorno', 'Giorni', 'Produzione', 'Casa', 'Auto', 'Prelievo', 'Immissione'],
        s.map((x, i) => [GIORNI_SETT[i], x.n, ...['sole', 'casa', 'auto', 'prelievo', 'immessa'].map(c => manca(x[c]) ? '--' : nf1.format(x[c]))]));
}

/* --- calendario --------------------------------------------------------- */
const COLONNE_CALENDARIO = { sole: 1, consumo: 2, auto: 3, prelievo: 4, autosufficienza: 5 };
function calendario(col) {
    const misura = stato.calendario, giorni = d.calendario, p = d.periodo;
    const box = $('calendario');
    box.textContent = '';
    if (!giorni?.length) return;
    const k = COLONNE_CALENDARIO[misura];
    const percento = misura === 'autosufficienza';
    const massimo = percento ? 100 : Math.max(0.001, ...giorni.map(g => g[k] || 0));
    const colore = scala(col.superficie2, tintaMisura(col, misura));
    const inizio = data(giorni[0][0]);
    inizio.setDate(inizio.getDate() - ((inizio.getDay() + 6) % 7));
    const settimane = Math.ceil((Math.round((data(giorni[giorni.length - 1][0]) - inizio) / 864e5) + 1) / 7);
    const sx = 30, alto = 16;
    const spazio = Math.max(200, box.parentElement.clientWidth - sx);
    const passo = Math.max(14, Math.min(30, Math.floor(spazio / settimane))), lato = passo - 3;
    const L = sx + settimane * passo, A = alto + 7 * passo;
    const ns = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('width', L);
    svg.setAttribute('height', A);
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', `Calendario: ${NOMI_MISURA[misura]} di ogni giorno`);
    const crea = (tag, attributi, testo) => {
        const e = document.createElementNS(ns, tag);
        Object.entries(attributi).forEach(([n, v]) => e.setAttribute(n, v));
        if (testo !== undefined) e.textContent = testo;
        return e;
    };
    [0, 2, 4, 6].forEach(r => svg.appendChild(crea('text', { x: 0, y: alto + r * passo + lato / 2 + 4 }, GIORNI_SETT[r])));
    let meseVisto = null, ultimaEtichetta = -99;
    giorni.forEach(g => {
        const dt = data(g[0]);
        const dalInizio = Math.round((dt - inizio) / 864e5);
        const c = Math.floor(dalInizio / 7), r = dalInizio % 7;
        const mese = g[0].slice(0, 7);
        if (mese !== meseVisto) {
            meseVisto = mese;
            const x = sx + c * passo;
            if (x - ultimaEtichetta > 28) {
                svg.appendChild(crea('text', { x, y: 10 }, dt.toLocaleDateString('it-IT', { month: 'short' })));
                ultimaEtichetta = x;
            }
        }
        const v = g[k];
        const cella = crea('rect', { class: 'g' + (g[0] < p.da || g[0] > p.a ? ' fuori' : ''), x: sx + c * passo, y: alto + r * passo,
                                     width: lato, height: lato, rx: 3, fill: manca(v) ? col.superficie2 : colore(v / massimo) });
        cella.addEventListener('pointermove', e => mostraSuggerimento(e.clientX, e.clientY, dataMedia(g[0]), [
            { colore: tintaMisura(col, misura), nome: NOMI_MISURA[misura], valore: manca(v) ? '--' : percento ? pct(v) : kwh(v) },
            { nome: 'Prodotta', valore: kwh(g[1]) }, { nome: 'Consumata', valore: kwh(g[2]) }]));
        cella.addEventListener('pointerleave', nascondiSuggerimento);
        cella.addEventListener('click', () => { nascondiSuggerimento(); scegliPeriodo('g:' + g[0]); });
        svg.appendChild(cella);
    });
    box.appendChild(svg);
    box.parentElement.scrollLeft = box.parentElement.scrollWidth;
    disegnaScala('calendario_scala', colore, '0', percento ? '100%' : kwh(massimo) + ' in un giorno');
}

/* --- sole, casa, rete ---------------------------------------------------- */
function sole(col) {
    const s = d.sole, t = d.totali;
    riquadri('riq_sole', [
        [nf1.format(s.media_giorno), 'kWh', 'al giorno in media', `mediana ${nf1.format(s.mediana_giorno)}`],
        [watt(s.picco_w), '', 'potenza massima vista'],
        [nf1.format(s.ore_produttive).replace(',0', ''), 'h', 'di produzione al giorno', `punta fra le ${fasciaOra(s.ora_di_punta)}`],
        [nf0.format(t.sole * ipotesi.co2), 'kg', 'di CO₂ evitata', `a ${nf2.format(ipotesi.co2)} kg per kWh`],
        [nf1.format(s.picco_w > 0 ? t.sole / t.giorni / (s.picco_w / 1000) : 0), 'h', 'equivalenti al giorno', 'energia ÷ potenza massima vista'],
        [pct(t.autoconsumo_pct), '', 'usata sul posto'],
    ]);
    const ultimi = d.mesi.slice(-12);
    const chiaro = scala(col.superficie, col.sole);
    const tinte = ultimi.map((_, i) => ultimi.length === 1 ? col.sole : chiaro(.42 + .58 * i / (ultimi.length - 1)));
    legenda('leg_mesi', ultimi.map((m, i) => [tinte[i], `${nomeMese(m.mese, true)} · ${nf1.format(m.sole_medio)} kWh al giorno`, true]));
    const o = opzioniBase(col, 'kW');
    o.scales.y.ticks.callback = v => nf1.format(v / 1000);
    o.scales.x.ticks.maxTicksLimit = 12;
    o.plugins.tooltip.external = suggerimentoGrafico(v => watt(v), i => 'Ore ' + fasciaOra(i));
    grafico('g_mesi', { type: 'line', data: { labels: Array.from({ length: 24 }, (_, h) => String(h).padStart(2, '0')),
        datasets: ultimi.map((m, i) => ({ label: nomeMese(m.mese, true), coloreSerie: tinte[i], data: m.profilo_sole, fill: false,
            borderColor: tinte[i], borderWidth: 2, pointRadius: 0, pointHoverRadius: 4, tension: .3 })) }, options: o });

    /* Quanto ci ha preso la previsione della sera prima */
    const conPrevisto = d.giorni.filter(g => !manca(g.previsto));
    $('box_previsto').hidden = !conPrevisto.length;
    if (!conPrevisto.length) { grafici.g_previsto?.destroy(); delete grafici.g_previsto; return; }
    const op = opzioniBase(col, 'kWh');
    op.plugins.tooltip.external = suggerimentoGrafico(v => nf1.format(v) + ' kWh', i => dataMedia(conPrevisto[i].g));
    grafico('g_previsto', { type: 'bar', data: { labels: conPrevisto.map(g => dataBreve(g.g)), datasets: [
        { type: 'line', label: 'Previsto', coloreSerie: col.neutro, data: conPrevisto.map(g => g.previsto), borderColor: col.neutro,
          backgroundColor: col.neutro, borderWidth: 2, pointRadius: 3, pointHoverRadius: 5, tension: 0, order: 1 },
        barra(col, 'Prodotto', col.sole, conPrevisto.map(g => g.sole), { order: 2 }),
    ] }, options: op });
    const p = d.previsione;
    $('previsto_nota').textContent = p
        ? `Su ${p.giorni} ${p.giorni === 1 ? 'giorno' : 'giorni'} la previsione ha sbagliato in media di ${nf1.format(p.errore_medio_kwh)} kWh`
          + (manca(p.errore_pct) ? '' : ` (${p.errore_pct}% del prodotto)`) + `; ${p.col_meteo} con il meteo di Home Assistant.`
        : 'La previsione viene messa da parte ogni sera alle 20 per il giorno dopo.';
}
function casa(col) {
    const c = d.casa, t = d.totali;
    riquadri('riq_casa', [
        [nf1.format(c.media_giorno), 'kWh', 'al giorno in media', 'senza l’auto'],
        [manca(c.media_feriale) ? '--' : nf1.format(c.media_feriale), 'kWh', 'nei giorni feriali'],
        [manca(c.media_festivo) ? '--' : nf1.format(c.media_festivo), 'kWh', 'nel fine settimana'],
        [watt(c.fondo_w), '', 'carico di fondo', 'quel che resta sempre acceso'],
        [fasciaOra(c.ora_di_punta), '', 'l’ora che consuma di più'],
        [pct(t.casa > 0 ? t.casa_sole / t.casa * 100 : null), '', 'coperta dal sole'],
    ]);
    const fondo = $('casa_fondo');
    fondo.textContent = '';
    if (!manca(c.fondo_w) && c.media_giorno > 0) {
        const alGiorno = c.fondo_w * 24 / 1000;
        fondo.append('Il carico di fondo vale ', elemento('b', '', nf1.format(alGiorno) + ' kWh al giorno'),
            `, il ${pct(Math.min(100, alGiorno / c.media_giorno * 100))} dei consumi della casa: circa `,
            elemento('b', '', euro(alGiorno * 365 * d.prezzi.acquisto) + ' l’anno'), ' al prezzo di acquisto.');
    }
    const box = $('fasi_box');
    box.textContent = '';
    const f = d.fasi;
    if (!f) { box.appendChild(elemento('p', 'nota', 'Nessuna lettura per fase in questo periodo (ci sono dal 28 ottobre 2026).')); return; }
    const totale = f.kwh[0] + f.kwh[1] + f.kwh[2];
    const massimo = Math.max(...f.kwh.slice(0, 3), 0.001);
    [0, 1, 2].forEach(k => {
        const riga = elemento('div', 'fase-riga');
        const b = elemento('div', 'barra');
        const i = elemento('i');
        i.style.width = (f.kwh[k] / massimo * 100) + '%';
        b.appendChild(i);
        const v = elemento('b', '', `${kwh(f.kwh[k])} · ${pct(totale > 0 ? f.kwh[k] / totale * 100 : null)}`);
        v.appendChild(elemento('small', '', `massimo ${watt(f.massimi[k].w)} il ${dataBreve(f.massimi[k].giorno)}`));
        riga.append(elemento('span', '', 'L' + (k + 1)), b, v);
        box.appendChild(riga);
    });
    box.appendChild(elemento('p', 'nota', `Consumi (casa e auto) misurati su ${f.giorni} giorni. ` +
        `Produzione per fase: ${[3, 4, 5].map(k => kwh(f.kwh[k])).join(' · ')}.`));
}
function rete(col) {
    const dist = d.distribuzioni, t = d.totali, limite = d.limite_contatore;
    const passo = dist.passo_prelievo;
    const sopra = soglia => dist.prelievo.reduce((s, v, i) => s + (i * passo >= soglia ? v : 0), 0);
    const inPrelievo = dist.prelievo.reduce((s, v) => s + v, 0);
    const classeLimite = limite > 0 ? Math.ceil(limite / passo) * passo : null;
    $('rete_limite').textContent = limite > 0 ? `contatore ${watt(limite)}` : 'limite del contatore disattivato';
    riquadri('riq_rete', [
        [pct(dist.minuti > 0 ? inPrelievo / dist.minuti * 100 : null), '', 'del tempo in prelievo', `${ore(inPrelievo)} su ${ore(dist.minuti)}`],
        [ore(sopra(3000)), '', 'sopra i 3 kW'],
        [ore(sopra(4500)), '', 'sopra i 4,5 kW'],
        classeLimite ? [ore(sopra(classeLimite)), '', `sopra i ${nf1.format(classeLimite / 1000).replace(',0', '')} kW`, 'oltre il limite del contatore'] : null,
    ]);
    const etichette = dist.prelievo.map((_, i) => i === dist.prelievo.length - 1 ? `${nf1.format(i * passo / 1000)}+` : nf1.format(i * passo / 1000));
    const o = opzioniBase(col, 'ore');
    o.scales.x.title = { display: true, text: 'kW prelevati (da … in su)', color: col.testo3, font: { size: 11 } };
    o.plugins.tooltip.external = suggerimentoGrafico(v => ore(v * 60),
        i => i === dist.prelievo.length - 1 ? `Oltre ${watt(i * passo)}` : `Fra ${watt(i * passo)} e ${watt((i + 1) * passo)}`);
    /* le classi oltre il limite del contatore sono uno stato, non una serie: colore di stato */
    const oltre = i => classeLimite !== null && i * passo >= classeLimite;
    grafico('g_prelievo', { type: 'bar', data: { labels: etichette, datasets: [{
        label: 'Tempo', coloreSerie: col.rete, data: dist.prelievo.map(v => v / 60),
        backgroundColor: dist.prelievo.map((_, i) => oltre(i) ? col.critico : col.rete), borderRadius: 4, maxBarThickness: 30 }] }, options: o });
    tabella('tab_prelievo', ['Potenza', 'Tempo', 'Quota del tempo in prelievo'], dist.prelievo.map((v, i) => [
        i === dist.prelievo.length - 1 ? `oltre ${watt(i * passo)}` : `${watt(i * passo)} – ${watt((i + 1) * passo)}`,
        ore(v), inPrelievo > 0 ? nf1.format(v / inPrelievo * 100) + '%' : '--']));
    const lista = $('picchi_prelievo');
    lista.textContent = '';
    if (!dist.picchi_prelievo.length) lista.appendChild(elemento('li', 'vuoto', 'Nessun prelievo nel periodo.'));
    dist.picchi_prelievo.forEach(([w, ts]) => {
        const li = elemento('li');
        li.append(elemento('span', '', dataOra(ts)), elemento('b', limite > 0 && w > limite ? 'oltre' : '', watt(w)));
        lista.appendChild(li);
    });
}

/* --- ricariche ------------------------------------------------------------- */
function ricariche(col) {
    const r = d.ricariche, t = d.totali, dist = d.distribuzioni;
    const brevi = $('ric_brevi');
    brevi.hidden = !r.brevi;
    brevi.textContent = r.brevi === 1 ? '1 accensione breve non contata' : `${r.brevi} accensioni brevi non contate`;
    if (!r.n) {
        riquadri('riq_ricariche', [['0', '', 'ricariche nel periodo']]);
        riquadri('riq_strada', []);
        ['g_ric_kwh', 'g_ric_ora', 'g_ric_potenza'].forEach(id => { grafici[id]?.destroy(); delete grafici[id]; });
        tabella('tab_ricariche', ['Nessuna ricarica in questo periodo'], []);
        return;
    }
    riquadri('riq_ricariche', [
        [nf0.format(r.n), '', r.n === 1 ? 'ricarica' : 'ricariche', `${nf1.format(r.n / t.giorni * 7)} a settimana`],
        [nf1.format(r.media_kwh), 'kWh', 'in media per ricarica', `mediana ${nf1.format(r.mediana_kwh)} · massimo ${nf1.format(r.massima_kwh)}`],
        [durata(r.mediana_minuti), '', 'durata tipica', `media ${durata(r.media_minuti)}`],
        [watt(r.potenza_media), '', 'potenza media di carica', `${ore(t.minuti_carica)} in carica`],
        [pct(r.sole_pct), '', 'dell’energia dal sole'],
        [nf0.format(r.tutte_sole), '', 'ricariche tutte dal sole', `su ${r.n}`],
        [...energia(r.kwh), 'in auto in tutto', `${pct(t.consumo > 0 ? t.auto / t.consumo * 100 : null)} dei consumi`],
        [`${d.serie.giorni_con_ricarica}`, '', 'giorni con una ricarica', `su ${t.giorni}`],
    ]);
    const km = t.auto / ipotesi.auto * 100;
    const viva = t.euro.auto_rete, piena = t.euro.auto_rete + t.euro.auto_mancata_vendita;
    const benzina = km * ipotesi.litri / 100 * ipotesi.benzina;
    riquadri('riq_strada', [
        [nf0.format(km), 'km', 'percorribili', `a ${nf1.format(ipotesi.auto).replace(',0', '')} kWh ogni 100 km`],
        [euro(viva), '', 'pagati alla rete', km > 0 ? `${nf2.format(viva / km * 100)} € ogni 100 km` : ''],
        [euro(piena), '', 'contando il sole non venduto', km > 0 ? `${nf2.format(piena / km * 100)} € ogni 100 km` : ''],
        [euro(benzina), '', 'la stessa strada a benzina', `${nf2.format(ipotesi.litri * ipotesi.benzina)} € ogni 100 km`],
    ]);

    const mini = (id, etichette, valori, unitaAsse, formato, titolo, asseX) => {
        const o = opzioniBase(col, unitaAsse);
        o.scales.x.ticks.maxTicksLimit = 8;
        if (asseX) o.scales.x.title = { display: true, text: asseX, color: col.testo3, font: { size: 11 } };
        o.scales.y.ticks.precision = 0;
        o.plugins.tooltip.external = suggerimentoGrafico(formato, titolo);
        grafico(id, { type: 'bar', data: { labels: etichette, datasets: [barra(col, 'Ricariche', col.auto, valori, { maxBarThickness: 22 })] }, options: o });
    };
    const ik = r.ist_kwh;
    mini('g_ric_kwh', ik.valori.map((_, i) => i === ik.valori.length - 1 ? `${i * ik.passo}+` : String(i * ik.passo)), ik.valori, 'ricariche',
        v => nf0.format(v), i => i === ik.valori.length - 1 ? `Oltre ${i * ik.passo} kWh` : `Fra ${i * ik.passo} e ${(i + 1) * ik.passo} kWh`, 'kWh (da … in su)');
    mini('g_ric_ora', r.ist_ora.map((_, h) => String(h).padStart(2, '0')), r.ist_ora, 'ricariche',
        v => nf0.format(v), i => 'Cominciate fra le ' + fasciaOra(i), 'ora di inizio');
    const ultima = dist.auto.reduce((u, v, i) => v > 0 ? i : u, 0);
    const potenze = dist.auto.slice(0, Math.max(8, ultima + 1));
    const op = (v, serie) => ore(v * 60);
    mini('g_ric_potenza', potenze.map((_, i) => String(i)), potenze.map(v => v / 60), 'ore',
        op, i => `Fra ${i} e ${i + 1} kW`, 'kW (da … in su)');
    grafici.g_ric_potenza.data.datasets[0].label = 'Tempo in carica';
    grafici.g_ric_potenza.options.scales.y.ticks.precision = undefined;
    grafici.g_ric_potenza.update();

    const t2 = $('tab_ricariche');
    t2.textContent = '';
    const testa = t2.createTHead().insertRow();
    ['Inizio', 'Durata', 'kWh', 'Dal sole', 'Potenza media', 'Massima'].forEach(h => testa.appendChild(elemento('th', '', h)));
    const corpo = t2.createTBody();
    r.elenco.forEach(x => {
        const tr = corpo.insertRow();
        tr.insertCell().textContent = dataOra(x.inizio);
        tr.insertCell().textContent = durata(x.minuti);
        tr.insertCell().textContent = nf1.format(x.kwh);
        const q = tr.insertCell();
        q.className = 'quota';
        const s = elemento('span', '', pct(x.sole_pct));
        const b = elemento('div', 'barra');
        const i = elemento('i', 'b-sole');
        i.style.width = x.sole_pct + '%';
        b.appendChild(i);
        s.appendChild(b);
        q.appendChild(s);
        tr.insertCell().textContent = watt(x.potenza_media);
        tr.insertCell().textContent = watt(x.picco);
    });
}

/* --- conti, primati, mesi ---------------------------------------------------- */
function soldi(col) {
    const e = d.totali.euro, p = d.prezzi, t = d.totali;
    $('soldi_prezzi').textContent = `acquisto ${nf2.format(p.acquisto)} € · vendita ${nf2.format(p.vendita)} € al kWh`;
    riquadri('riq_soldi', [
        [euro(e.spesa), '', 'spesi per l’energia prelevata', kwh(t.prelievo)],
        [euro(e.ricavo), '', 'ricavati dall’energia immessa', kwh(t.immessa)],
        [euro(Math.abs(e.netto)), '', e.netto < 0 ? 'saldo a tuo favore' : 'bolletta netta', e.netto < 0 ? 'ricavo meno spesa' : 'spesa meno ricavo'],
        [euro(e.senza_fv), '', 'la bolletta senza fotovoltaico', `${kwh(t.consumo)} tutti comprati`],
        [euro(e.beneficio), '', 'resa dell’impianto', `${nf2.format(e.beneficio / t.giorni)} € al giorno`],
        [euro(t.autoconsumo * p.acquisto), '', 'di energia non comprata', kwh(t.autoconsumo)],
        [t.sole > 0 ? nf2.format(e.beneficio / t.sole) + ' €' : '--', '', 'resi da ogni kWh prodotto'],
        [t.consumo > 0 ? nf2.format(e.spesa / t.consumo) + ' €' : '--', '', 'pagati in media per ogni kWh consumato', `contro ${nf2.format(p.acquisto)} € senza impianto`],
    ]);
    let a = 0, b = 0;
    const risparmio = [], venduta = [];
    d.giorni.forEach(g => {
        a += (g.sole - g.immessa) * p.acquisto;
        b += g.immessa * p.vendita;
        risparmio.push(Math.round(a * 100) / 100);
        venduta.push(Math.round(b * 100) / 100);
    });
    const o = opzioniBase(col, '€ accumulati');
    o.scales.y.stacked = true;
    o.plugins.tooltip.external = suggerimentoGrafico(v => euro(v), i => `Fino a ${dataMedia(d.giorni[i].g)} · totale ${euro(risparmio[i] + venduta[i])}`);
    const comune = { pointRadius: 0, pointHoverRadius: 4, tension: .2, borderWidth: 1.5 };
    grafico('g_soldi', { type: 'line', data: { labels: d.giorni.map(g => dataBreve(g.g)), datasets: [
        { label: 'Energia non comprata', coloreSerie: col.sole, data: risparmio, fill: 'origin', borderColor: col.sole, backgroundColor: alfa(col.sole, .28), ...comune },
        { label: 'Energia venduta', coloreSerie: col.rete, data: venduta, fill: '-1', borderColor: col.rete, backgroundColor: alfa(col.rete, .28), ...comune },
    ] }, options: o });
}
const NOMI_RECORD = {
    giorno_sole_max: 'Il giorno con più sole', giorno_sole_min: 'Il giorno con meno sole', picco_sole: 'Produzione più alta',
    ora_sole_max: 'L’ora più produttiva', giorno_casa_max: 'La casa ha consumato di più', giorno_casa_min: 'La casa ha consumato di meno',
    picco_casa: 'Picco della casa', picco_consumo: 'Picco di consumo totale', giorno_prelievo_max: 'Più energia prelevata',
    giorno_prelievo_min: 'Meno energia prelevata', picco_prelievo: 'Prelievo più alto', giorno_immessa_max: 'Più energia immessa',
    giorno_autosufficienza_max: 'Il giorno più autosufficiente', giorno_auto_max: 'Più energia in auto in un giorno',
    picco_auto: 'Potenza di carica più alta', ricarica_max: 'La ricarica più grande', ricarica_lunga: 'La ricarica più lunga',
};
function primati() {
    const box = $('record');
    box.textContent = '';
    d.record.forEach(r => {
        const valore = r.unita === 'W' ? watt(r.valore) : r.unita === 'kWh' ? kwh(r.valore) : r.unita === '%' ? pct(r.valore) : durata(r.valore);
        let quando = r.giorno ? dataMedia(r.giorno) : r.ts ? dataOra(r.ts) : '';
        if (r.ora !== undefined) quando += ' · ' + fasciaOra(r.ora);
        const voce = elemento('div', 'primato');
        voce.append(elemento('span', '', NOMI_RECORD[r.chiave] || r.chiave), elemento('b', '', valore), elemento('em', '', quando));
        box.appendChild(voce);
    });
}
function mesi() {
    tabella('tab_mesi', ['Mese', 'Giorni', 'Prodotta', 'Casa', 'Auto', 'Prelevata', 'Immessa', 'Autosuff.', 'Resa'],
        d.mesi.slice().reverse().map(m => [nomeMese(m.mese, true), m.giorni, nf0.format(m.sole), nf0.format(m.casa), nf0.format(m.auto),
            nf0.format(m.prelievo), nf0.format(m.immessa), pct(m.autosufficienza_pct), euro(m.euro.beneficio)]));
    const s = d.serie, box = $('serie');
    box.textContent = '';
    const riga = (nome, valore, nota) => {
        const r = elemento('div');
        const dd = elemento('dd', '', valore);
        if (nota) dd.appendChild(elemento('small', '', nota));
        r.append(elemento('dt', '', nome), dd);
        box.appendChild(r);
    };
    const tratto = x => x.giorni > 1 ? `dal ${dataBreve(x.da)} al ${dataBreve(x.a)}` : x.giorni === 1 ? dataBreve(x.da) : '';
    const n = d.periodo.giorni;
    riga('Giorni in attivo (più immesso che prelevato)', `${s.giorni_in_attivo} su ${n}`);
    riga('Striscia più lunga in attivo', `${s.in_attivo.giorni} giorni`, tratto(s.in_attivo));
    riga('Giorni con almeno 1 kWh in auto', `${s.giorni_con_ricarica} su ${n}`);
    riga('Striscia più lunga di ricariche', `${s.con_ricarica.giorni} giorni`, tratto(s.con_ricarica));
    riga('Giorni coperti dal sole per almeno il 90%', `${s.giorni_autosufficienti} su ${n}`);
}

/* ------------------------------------------------------------------
   Avvio
   ------------------------------------------------------------------ */
filtri('filtri_tempo', 'tempo', () => { const c = colori(); nelTempo(c); });
filtri('filtri_mappa', 'mappa', () => mappa(colori()));
filtri('filtri_profilo', 'profilo', () => profilo(colori()));
filtri('filtri_calendario', 'calendario', () => calendario(colori()));

const CAMPI_IPOTESI = { ip_auto: 'auto', ip_litri: 'litri', ip_benzina: 'benzina', ip_co2: 'co2' };
Object.entries(CAMPI_IPOTESI).forEach(([id, chiave]) => {
    $(id).value = ipotesi[chiave];
    $(id).addEventListener('change', e => {
        const v = parseFloat(e.target.value);
        if (!(v > 0) && !(chiave === 'co2' && v === 0)) { e.target.value = ipotesi[chiave]; return; }
        ipotesi[chiave] = v;
        memoria('stat_ipotesi', JSON.stringify(ipotesi));
        if (d && !d.vuoto && d.copertura) { const c = colori(); tessere(c); sole(c); ricariche(c); }
    });
});

let attesaRidimensiona = null, larghezzaVista = innerWidth;
addEventListener('resize', () => {
    if (innerWidth === larghezzaVista) return;       // su telefono la barra degli indirizzi cambia solo l'altezza
    larghezzaVista = innerWidth;
    clearTimeout(attesaRidimensiona);
    attesaRidimensiona = setTimeout(() => {
        if (!d || d.vuoto || !d.copertura) return;
        const c = colori();
        flussi(c); mappa(c); calendario(c);
    }, 150);
});

/* Chart.js e' un <script defer>: e' gia' eseguito quando parte questo modulo */
carica();
/* la giornata in corso cambia: si ricarica ogni cinque minuti se la pagina e' in vista */
setInterval(() => { if (!document.hidden) carica(); }, 300000);
document.addEventListener('visibilitychange', () => { if (!document.hidden && d && Date.now() / 1000 - d.adesso > 300) carica(); });
