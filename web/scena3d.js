/* Scena 3D dei flussi di energia: casa con i pannelli, colonnina, auto e
   palo della rete, costruiti qui in codice (nessun modello da scaricare).
   Le palline che corrono lungo i cavi sono i flussi veri: quante e quanto
   veloci dipende dai watt, il verso sulla linea della rete dice se si
   preleva o si immette. Sole, luna, luci e nuvole seguono l'ora e la
   produzione.

   La scena e' un abbellimento: i numeri stanno nelle etichette HTML, che
   questo modulo si limita a posizionare (suEtichette). Se qualcosa qui
   fallisce, app.js mostra le stesse etichette come riquadri. */
import * as THREE from './vendor/three.module.min.js';

/* Italia settentrionale: serve solo a far sorgere e tramontare il sole
   all'ora giusta, non a fare calcoli. */
const LATITUDINE = 45.5, LONGITUDINE = 11.0;
const RAD = Math.PI / 180;

const CASA_X = -2.3, CASA_Z = -0.4;          // centro della casa
const MURO_DX = CASA_X + 2.6;                // parete verso l'auto
const AUTO_X = 2.75, AUTO_Z = 0.9;
const PALO = [6.0, -3.6];
const CENTRO = new THREE.Vector3(0.6, 2.3, -0.3);
const AZIMUT_0 = 0.6, POLARE_0 = 1.17;

function posizioneSole(secondi) {
    /* Altezza del sole (rad) e angolo orario (rad, negativo al mattino) */
    const d = new Date(secondi * 1000);
    const inizio = Date.UTC(d.getUTCFullYear(), 0, 0);
    const giorno = Math.floor((d.getTime() - inizio) / 86400000);
    const declinazione = 23.44 * RAD * Math.sin(2 * Math.PI * (284 + giorno) / 365);
    const oraSolare = d.getUTCHours() + d.getUTCMinutes() / 60 + LONGITUDINE / 15;
    const orario = (oraSolare - 12) * 15 * RAD;
    const lat = LATITUDINE * RAD;
    const seno = Math.sin(lat) * Math.sin(declinazione) + Math.cos(lat) * Math.cos(declinazione) * Math.cos(orario);
    const mezzogiorno = Math.sin(lat) * Math.sin(declinazione) + Math.cos(lat) * Math.cos(declinazione);
    return { altezza: Math.asin(Math.max(-1, Math.min(1, seno))), orario, senoMezzogiorno: mezzogiorno };
}
const limita = (v, a, b) => Math.min(b, Math.max(a, v));
const sfuma = (a, b, v) => { const t = limita((v - a) / (b - a), 0, 1); return t * t * (3 - 2 * t); };

export function creaScena(tela, contenitore, { colori, suEtichette }) {
    const prova = document.createElement('canvas');
    if (!(prova.getContext('webgl2') || prova.getContext('webgl'))) throw new Error('WebGL non disponibile');

    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: 'low-power' });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1.05;
    tela.appendChild(renderer.domElement);

    const scena = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(30, 1.5, 0.5, 120);
    const moto = matchMedia('(prefers-reduced-motion: reduce)');

    /* ---------------- materiali e forme di base ---------------- */
    const mat = (colore, altro = {}) => new THREE.MeshStandardMaterial(
        { color: colore, roughness: .9, metalness: 0, flatShading: true, ...altro });
    function aggiungi(geometria, materiale, x, y, z, genitore = scena, ombra = true) {
        const m = new THREE.Mesh(geometria, materiale);
        m.position.set(x, y, z);
        m.castShadow = ombra;
        m.receiveShadow = true;
        genitore.add(m);
        return m;
    }
    const scatola = (w, h, d, materiale, x, y, z, genitore, ombra) =>
        aggiungi(new THREE.BoxGeometry(w, h, d), materiale, x, y, z, genitore, ombra);

    /* ---------------- terreno ---------------- */
    const prato = mat(0x8dbb5e);
    aggiungi(new THREE.CylinderGeometry(8.7, 8.5, .5, 44), prato, .6, -.25, -.2, scena, false);
    aggiungi(new THREE.CylinderGeometry(8.5, 6.2, 1.9, 44), mat(0x8c6b50), .6, -1.45, -.2, scena, false);
    scatola(3.7, .05, 6.6, mat(0xa3a9b0), AUTO_X, .025, AUTO_Z + .3, scena, false);       // vialetto dell'auto
    scatola(1.1, .05, 3.2, mat(0xc9c2b4), CASA_X, .025, CASA_Z + 3.7, scena, false);      // sentiero della porta

    /* ---------------- casa ---------------- */
    const muro = mat(0xf2ede4);
    scatola(5.2, 2.7, 4.4, muro, CASA_X, 1.35, CASA_Z);
    const timpano = new THREE.Shape();
    timpano.moveTo(-2.5, 0); timpano.lineTo(2.5, 0); timpano.lineTo(0, 1.9); timpano.closePath();
    const tettoGeo = new THREE.ExtrudeGeometry(timpano, { depth: 5.8, bevelEnabled: false });
    tettoGeo.translate(0, 0, -2.9);
    const tetto = aggiungi(tettoGeo, mat(0xb4553d), CASA_X, 2.7, CASA_Z);
    tetto.rotation.y = Math.PI / 2;
    scatola(.5, 1.0, .5, mat(0xa04a35), CASA_X - 1.7, 4.2, CASA_Z - 1.0);                 // camino

    const vetro = new THREE.MeshStandardMaterial({ color: 0x7fa6c9, roughness: .25, metalness: .1,
                                                   emissive: 0xffd9a0, emissiveIntensity: 0 });
    const telaio = mat(0xffffff);
    const fronte = CASA_Z + 2.2;
    for (const dx of [-1.65, 1.65]) {
        scatola(1.1, 1.1, .06, telaio, CASA_X + dx, 1.55, fronte, scena, false);
        scatola(.92, .92, .08, vetro, CASA_X + dx, 1.55, fronte + .01, scena, false);
    }
    scatola(.9, 1.8, .08, mat(0x6d4a38), CASA_X, .9, fronte + .01, scena, false);         // porta
    scatola(.06, 1.0, 1.0, telaio, MURO_DX, 1.6, CASA_Z - 1.45, scena, false);
    scatola(.08, .84, .84, vetro, MURO_DX + .01, 1.6, CASA_Z - 1.45, scena, false);
    const lampada = new THREE.MeshStandardMaterial({ color: 0xfff3d6, emissive: 0xffb057, emissiveIntensity: 0 });
    aggiungi(new THREE.SphereGeometry(.11, 12, 10), lampada, CASA_X, 2.3, fronte + .12, scena, false);

    /* pannelli sulla falda verso chi guarda */
    const falda = Math.atan2(1.9, 2.5);
    const pannelli = new THREE.Group();
    pannelli.position.set(CASA_X, 2.7 + .95 + Math.cos(falda) * .05, CASA_Z + 1.25 + Math.sin(falda) * .05);
    pannelli.rotation.x = falda;
    scena.add(pannelli);
    const cella = new THREE.MeshStandardMaterial({ color: 0x16264a, roughness: .28, metalness: .5,
                                                   emissive: 0x1baf7a, emissiveIntensity: 0 });
    const cornice = mat(0xd3d9df, { roughness: .5 });
    for (const px of [-2.01, -.67, .67, 2.01]) {
        for (const pz of [-.72, .72]) {
            scatola(1.29, .04, 1.39, cornice, px, 0, pz, pannelli);
            scatola(1.2, .05, 1.3, cella, px, .012, pz, pannelli, false);
        }
    }

    /* quadro e colonnina sulla parete verso l'auto */
    const QUADRO = new THREE.Vector3(MURO_DX + .1, 1.85, CASA_Z + .05);
    const COLONNINA = new THREE.Vector3(MURO_DX + .1, 1.15, CASA_Z + 1.25);
    scatola(.14, .6, .42, mat(0x8b939c, { roughness: .6 }), QUADRO.x - .03, QUADRO.y, QUADRO.z);
    scatola(.16, .52, .36, mat(0xfafafa, { roughness: .5 }), COLONNINA.x - .02, COLONNINA.y, COLONNINA.z);
    const spia = new THREE.MeshStandardMaterial({ color: 0x20262e, emissive: 0x2a78d6, emissiveIntensity: 0 });
    scatola(.03, .3, .2, spia, COLONNINA.x + .07, COLONNINA.y + .03, COLONNINA.z, scena, false);

    /* ---------------- auto ---------------- */
    const auto = new THREE.Group();          // coordinate locali: x = lunghezza, y = alto, z = larghezza
    auto.position.set(AUTO_X, 0, AUTO_Z);
    auto.rotation.y = -Math.PI / 2;          // il muso verso chi guarda
    scena.add(auto);
    const profilo = new THREE.Shape();
    [[-2.05, .32], [-2.1, .75], [-1.95, .98], [-1.35, 1.42], [.15, 1.46], [.95, 1.0], [1.95, .86], [2.1, .62], [2.05, .32]]
        .forEach(([x, y], i) => (i ? profilo.lineTo(x, y) : profilo.moveTo(x, y)));
    profilo.closePath();
    const scoccaGeo = new THREE.ExtrudeGeometry(profilo, { depth: 1.66, bevelEnabled: true, bevelSize: .07,
                                                           bevelThickness: .07, bevelSegments: 2 });
    scoccaGeo.translate(0, 0, -.83);
    aggiungi(scoccaGeo, mat(0xe9eef3, { roughness: .35, metalness: .25, flatShading: false }), 0, 0, 0, auto);
    const finestrini = new THREE.Shape();
    [[-1.2, 1.03], [-1.12, 1.36], [.1, 1.4], [.72, 1.03]].forEach(([x, y], i) =>
        (i ? finestrini.lineTo(x, y) : finestrini.moveTo(x, y)));
    finestrini.closePath();
    const vetriGeo = new THREE.ExtrudeGeometry(finestrini, { depth: 1.84, bevelEnabled: false });
    vetriGeo.translate(0, 0, -.92);
    const vetroAuto = mat(0x1f2a3a, { roughness: .2, metalness: .5, flatShading: false });
    aggiungi(vetriGeo, vetroAuto, 0, 0, 0, auto, false);
    const parabrezza = scatola(.84, .03, 1.5, vetroAuto, .56, 1.262, 0, auto, false);
    parabrezza.rotation.z = Math.atan2(-.46, .8);
    const lunotto = scatola(.72, .03, 1.5, vetroAuto, -1.66, 1.215, 0, auto, false);
    lunotto.rotation.z = Math.atan2(.44, .6);
    const gomma = mat(0x23262b, { roughness: .8 });
    const cerchio = mat(0xc4cad2, { roughness: .4, metalness: .6 });
    for (const rx of [-1.32, 1.3]) {
        for (const rz of [-.86, .86]) {
            const ruota = aggiungi(new THREE.CylinderGeometry(.35, .35, .24, 18), gomma, rx, .35, rz, auto);
            ruota.rotation.x = Math.PI / 2;
            const coppa = aggiungi(new THREE.CylinderGeometry(.2, .2, .26, 12), cerchio, rx, .35, rz, auto, false);
            coppa.rotation.x = Math.PI / 2;
        }
    }
    const fari = new THREE.MeshStandardMaterial({ color: 0xffffff, emissive: 0xffffff, emissiveIntensity: .15 });
    const stop = new THREE.MeshStandardMaterial({ color: 0xb3261e, emissive: 0xff2a1f, emissiveIntensity: .1 });
    for (const rz of [-.62, .62]) {
        scatola(.06, .12, .36, fari, 2.13, .72, rz, auto, false);
        scatola(.06, .1, .34, stop, -2.13, .82, rz, auto, false);
    }
    const PRESA = new THREE.Vector3(AUTO_X - .93, .9, AUTO_Z - 1.3);       // fianco verso la casa, dietro
    const presa = new THREE.MeshStandardMaterial({ color: 0x20262e, emissive: 0x2a78d6, emissiveIntensity: 0 });
    scatola(.2, .16, .04, presa, -1.3, .9, .915, auto, false);
    /* alone sotto l'auto quando carica */
    let alone = null;        // creato piu' sotto, quando c'e' la sfumatura

    /* ---------------- palo della rete ---------------- */
    const legno = mat(0x7a5a42);
    aggiungi(new THREE.CylinderGeometry(.09, .12, 5.3, 8), legno, PALO[0], 2.65, PALO[1]);
    const traversa = scatola(1.5, .1, .1, legno, PALO[0], 5.0, PALO[1]);
    traversa.rotation.y = .5;
    const CIMA_PALO = new THREE.Vector3(PALO[0], 5.1, PALO[1]);
    const ATTACCO = new THREE.Vector3(MURO_DX + .05, 3.55, CASA_Z);

    /* ---------------- alberi e siepi ---------------- */
    const tronco = mat(0x6f5240), chioma = mat(0x4f9150), chioma2 = mat(0x62a85c);
    function albero(x, z, s = 1) {
        aggiungi(new THREE.CylinderGeometry(.12 * s, .16 * s, 1.0 * s, 7), tronco, x, .5 * s, z);
        aggiungi(new THREE.ConeGeometry(1.0 * s, 1.7 * s, 8), chioma, x, 1.6 * s, z);
        aggiungi(new THREE.ConeGeometry(.75 * s, 1.4 * s, 8), chioma2, x, 2.5 * s, z);
    }
    albero(-6.0, -2.6, 1.25); albero(-5.3, 3.0, .9); albero(.8, -6.0, 1.1); albero(6.3, 2.6, .8); albero(-2.6, -6.2, .85);
    for (const [x, z, r] of [[-5.1, 1.4, .45], [-4.1, 2.9, .38], [.9, 3.3, .42]]) {
        aggiungi(new THREE.IcosahedronGeometry(r, 0), chioma2, x, r * .7, z);
    }

    /* ---------------- cielo ---------------- */
    function alonePunto(colore) {
        const c = document.createElement('canvas');
        c.width = c.height = 128;
        const g = c.getContext('2d');
        const sfumatura = g.createRadialGradient(64, 64, 0, 64, 64, 64);
        sfumatura.addColorStop(0, colore);
        sfumatura.addColorStop(.25, colore.replace(/[\d.]+\)$/, '.45)'));
        sfumatura.addColorStop(1, colore.replace(/[\d.]+\)$/, '0)'));
        g.fillStyle = sfumatura;
        g.fillRect(0, 0, 128, 128);
        return new THREE.CanvasTexture(c);
    }
    alone = new THREE.Mesh(new THREE.PlaneGeometry(3.6, 6.0), new THREE.MeshBasicMaterial(
        { map: alonePunto('rgba(255,255,255,1)'), color: colori.auto, transparent: true, opacity: 0,
          depthWrite: false, toneMapped: false }));
    alone.rotation.x = -Math.PI / 2;
    alone.position.set(AUTO_X, .07, AUTO_Z);
    scena.add(alone);

    const sole = new THREE.Mesh(new THREE.SphereGeometry(.7, 24, 16),
        new THREE.MeshBasicMaterial({ color: 0xffd24a, toneMapped: false, fog: false }));
    const soleAlone = new THREE.Sprite(new THREE.SpriteMaterial({ map: alonePunto('rgba(255,214,102,1)'),
        transparent: true, depthWrite: false, blending: THREE.AdditiveBlending, toneMapped: false }));
    soleAlone.scale.setScalar(6);
    sole.add(soleAlone);
    scena.add(sole);
    const luna = new THREE.Mesh(new THREE.SphereGeometry(.5, 20, 14),
        new THREE.MeshBasicMaterial({ color: 0xe6ecff, toneMapped: false, transparent: true }));
    const lunaAlone = new THREE.Sprite(new THREE.SpriteMaterial({ map: alonePunto('rgba(190,205,255,1)'),
        transparent: true, depthWrite: false, blending: THREE.AdditiveBlending, toneMapped: false }));
    lunaAlone.scale.setScalar(4);
    luna.add(lunaAlone);
    luna.position.set(-7.5, 6.9, -10);
    scena.add(luna);

    const puntiStelle = new Float32Array(160 * 3);
    let seme = 7;
    const caso = () => { seme = (seme * 16807) % 2147483647; return seme / 2147483647; };
    for (let i = 0; i < 160; i++) {
        puntiStelle[i * 3] = -20 + caso() * 40;
        puntiStelle[i * 3 + 1] = 1.5 + caso() * 9;
        puntiStelle[i * 3 + 2] = -14 - caso() * 4;
    }
    const stelleGeo = new THREE.BufferGeometry();
    stelleGeo.setAttribute('position', new THREE.BufferAttribute(puntiStelle, 3));
    const stelle = new THREE.Points(stelleGeo, new THREE.PointsMaterial(
        { color: 0xffffff, size: .13, transparent: true, opacity: 0, depthWrite: false, toneMapped: false }));
    scena.add(stelle);

    const nuvolaMat = new THREE.MeshStandardMaterial({ color: 0xffffff, roughness: 1, flatShading: true,
                                                       transparent: true, opacity: .8 });
    const nuvole = [];
    for (const [x, y, z, s, v] of [[-8, 6.3, -7.5, 1.0, .16], [3.5, 7.0, -8.5, 1.25, .11], [10, 5.6, -6.5, .8, .2]]) {
        const g = new THREE.Group();
        for (const [dx, dy, r] of [[0, 0, .9], [.95, .12, .7], [-.9, .05, .62], [.3, .42, .62]]) {
            const p = new THREE.Mesh(new THREE.IcosahedronGeometry(r * s, 1), nuvolaMat);
            p.position.set(dx * s, dy * s, 0);
            g.add(p);
        }
        g.position.set(x, y, z);
        g.userData.velocita = v;
        scena.add(g);
        nuvole.push(g);
    }

    /* ---------------- luci ---------------- */
    const cielo = new THREE.HemisphereLight(0xdcecff, 0x8f8168, 1.4);
    scena.add(cielo);
    const luceSole = new THREE.DirectionalLight(0xfff0d8, 2.6);
    luceSole.castShadow = true;
    luceSole.shadow.mapSize.set(1024, 1024);
    Object.assign(luceSole.shadow.camera, { left: -12, right: 12, top: 12, bottom: -12, near: 1, far: 50 });
    luceSole.shadow.bias = -.0006;
    luceSole.shadow.normalBias = .04;
    scena.add(luceSole, luceSole.target);
    luceSole.target.position.copy(CENTRO).setY(0);
    const luceLuna = new THREE.DirectionalLight(0x9db4ff, 0);
    luceLuna.position.set(-6, 10, 7);
    scena.add(luceLuna);
    const luceCasa = new THREE.PointLight(0xffb866, 0, 9, 1.6);
    luceCasa.position.set(CASA_X, 2.2, fronte + .7);
    scena.add(luceCasa);

    /* ---------------- cavi e flussi ---------------- */
    const curva = punti => new THREE.CatmullRomCurve3(punti.map(p => new THREE.Vector3(...p)), false, 'catmullrom', .35);
    const cavoMat = mat(0x39414d, { roughness: .7, flatShading: false });
    function cavo(c, raggio = .022, segmenti = 48) {
        const m = new THREE.Mesh(new THREE.TubeGeometry(c, segmenti, raggio, 6, false), cavoMat);
        m.castShadow = true;
        scena.add(m);
        return m;
    }
    const percorsi = {
        sole: curva([[CASA_X, 3.75, CASA_Z + 1.32], [CASA_X + 2.2, 3.75, CASA_Z + 1.32],
                     [MURO_DX + .22, 3.25, CASA_Z + 1.0], [MURO_DX + .12, 2.5, CASA_Z + .3], [QUADRO.x, 2.12, QUADRO.z]]),
        rete: curva([CIMA_PALO.toArray(), [3.4, 4.05, -2.2], ATTACCO.toArray(),
                     [MURO_DX + .09, 2.7, CASA_Z - .05], [QUADRO.x, 2.12, QUADRO.z - .08]]),
        casa: curva([[QUADRO.x, 1.6, QUADRO.z + .1], [MURO_DX + .1, 2.48, CASA_Z + 1.2], [MURO_DX + .08, 2.52, fronte + .06],
                     [CASA_X + 1.2, 2.52, fronte + .1], [CASA_X, 2.42, fronte + .12]]),
        auto: curva([[QUADRO.x, 1.58, QUADRO.z + .12], [MURO_DX + .1, 1.42, CASA_Z + .7], [COLONNINA.x + .04, 1.0, COLONNINA.z],
                     [1.05, .2, AUTO_Z - .35], [1.55, .26, AUTO_Z - 1.0], PRESA.toArray()]),
    };
    cavo(percorsi.sole); cavo(percorsi.rete, .018, 64); cavo(percorsi.casa);
    cavo(curva([[QUADRO.x, 1.58, QUADRO.z + .12], [MURO_DX + .1, 1.42, CASA_Z + .7], [COLONNINA.x + .04, 1.22, COLONNINA.z]]));
    const cavoAuto = cavo(curva([[COLONNINA.x + .04, .95, COLONNINA.z], [1.05, .2, AUTO_Z - .35],
                                 [1.55, .26, AUTO_Z - 1.0], PRESA.toArray()]), .035);
    cavo(curva([CIMA_PALO.toArray(), [10, 4.5, -5.6], [15, 5.2, -8]]), .016, 24);        // la linea che arriva da fuori

    const N = 16;
    const sfera = new THREE.SphereGeometry(.105, 12, 10);
    const flussi = {};
    for (const nome of ['sole', 'rete', 'casa', 'auto']) {
        const m = new THREE.InstancedMesh(sfera, new THREE.MeshBasicMaterial({ color: colori[nome], toneMapped: false }), N);
        m.frustumCulled = false;
        scena.add(m);
        flussi[nome] = { mesh: m, curva: percorsi[nome], fase: Math.random(), watt: 0, verso: 1 };
    }
    const fittizio = new THREE.Object3D();
    function muoviFlussi(dt) {
        for (const f of Object.values(flussi)) {
            const w = Math.abs(f.watt);
            const attive = w < 40 ? 0 : limita(2 + Math.floor(w / 450), 2, N);
            if (!moto.matches) f.fase = (f.fase + dt * f.verso * (.09 + .3 * Math.min(1, w / 6000)) + 1) % 1;
            for (let i = 0; i < N; i++) {
                if (i < attive) {
                    const t = (i / attive + f.fase) % 1;
                    fittizio.position.copy(f.curva.getPointAt(t));
                    /* nascono e spariscono in dissolvenza alle estremita' */
                    fittizio.scale.setScalar(Math.min(1, t * 9, (1 - t) * 9));
                } else {
                    fittizio.scale.setScalar(0);
                }
                fittizio.updateMatrix();
                f.mesh.setMatrixAt(i, fittizio.matrix);
            }
            f.mesh.instanceMatrix.needsUpdate = true;
        }
    }

    /* ---------------- stato ---------------- */
    const stato = { sole: 0, casa: 0, auto: 0, scambio: 0, collegata: false, inCarica: false, colonnina: true,
                    tempo: Date.now() / 1000, picco: 0 };
    let luce = 1;                 // 0 notte, 1 giorno
    let coperto = 0;
    const blu = new THREE.Color(colori.auto), verde = new THREE.Color(colori.sole);

    function applicaStato() {
        const s = stato;
        const p = posizioneSole(s.tempo);
        luce = sfuma(-6 * RAD, 9 * RAD, p.altezza);
        if (s.sole > 80) luce = Math.max(luce, .65);
        const fase = luce < .25 ? 'notte' : (luce < .8 ? 'alba' : 'giorno');
        if (contenitore.dataset.fase !== fase) contenitore.dataset.fase = fase;

        /* quanto e' coperto: produzione rispetto a quella attesa a quest'ora */
        const attesa = s.picco * Math.max(.12, Math.sin(Math.max(0, p.altezza)) / Math.max(.2, p.senoMezzogiorno));
        coperto = s.picco > 500 && luce > .3 ? limita(1 - s.sole / attesa, 0, 1) : .25;

        const su = Math.sin(Math.max(p.altezza, -.2));
        sole.position.set(.6 - Math.sin(p.orario) * 11.5, 2.6 + su * 7.2, -10.5);
        sole.visible = p.altezza > -4 * RAD;
        sole.material.color.setHSL(.125 - .045 * (1 - sfuma(0, .35, su)), 1, .62 - .08 * coperto);
        soleAlone.material.opacity = (.95 - .5 * coperto) * luce;
        luna.material.opacity = lunaAlone.material.opacity = 1 - sfuma(.1, .5, luce);
        luna.visible = luce < .5;
        stelle.material.opacity = .9 * (1 - sfuma(.05, .45, luce));
        nuvolaMat.opacity = luce > .3 ? .35 + .6 * coperto : .16;
        nuvolaMat.color.setScalar(.45 + .55 * luce - .12 * coperto * luce);

        luceSole.position.set(CENTRO.x - Math.sin(p.orario) * 9, 5.5 + su * 9, 8.5);
        luceSole.intensity = (2.9 - 1.5 * coperto) * luce;
        luceSole.color.setHSL(.1, .75, .78 + .16 * sfuma(.1, .5, su));
        cielo.intensity = .32 + (1.15 - .25 * coperto) * luce;
        luceLuna.intensity = .85 * (1 - luce);
        renderer.shadowMap.needsUpdate = true;

        const notte = 1 - luce;
        const carico = Math.min(1, s.casa / 3000);
        vetro.emissiveIntensity = notte * (.55 + .9 * carico);
        lampada.emissiveIntensity = notte * 2.2 + .25;
        luceCasa.intensity = notte * (5 + 9 * carico);
        cella.emissive.set(colori.sole);
        cella.emissiveIntensity = .09 * Math.min(1, s.sole / Math.max(3000, s.picco || 6000));

        auto.visible = cavoAuto.visible = s.collegata || s.auto > 50;
        fari.emissiveIntensity = .12 + .5 * notte;

        flussi.sole.watt = s.sole;
        flussi.casa.watt = s.casa;
        flussi.auto.watt = auto.visible ? s.auto : 0;
        flussi.rete.watt = s.scambio;
        flussi.rete.verso = s.scambio >= 0 ? 1 : -1;       // prelievo: dal palo alla casa
    }

    /* ---------------- camera, trascinamento ---------------- */
    let azimut = AZIMUT_0, polare = POLARE_0, raggio = 24, largo = 1, alto = 1;
    function inquadra(t) {
        const a = azimut + (moto.matches || trascina ? 0 : Math.sin(t * .22) * .045);
        camera.position.set(CENTRO.x + raggio * Math.sin(polare) * Math.sin(a),
                            CENTRO.y + raggio * Math.cos(polare),
                            CENTRO.z + raggio * Math.sin(polare) * Math.cos(a));
        camera.lookAt(CENTRO);
    }
    function ridimensiona() {
        largo = Math.max(1, tela.clientWidth);
        alto = Math.max(1, tela.clientHeight);
        renderer.setSize(largo, alto, false);
        camera.aspect = largo / alto;
        const mezzoV = camera.fov * RAD / 2;
        const mezzoO = Math.atan(Math.tan(mezzoV) * camera.aspect);
        /* Tutta l'isola nei formati larghi; su telefono si stringe sui lati
           (casa, auto e palo restano dentro) per non rimpicciolire tutto */
        const meta = camera.aspect > 1.45 ? 9.7 : 8.3;
        raggio = Math.max(meta / Math.tan(mezzoO), 6.9 / Math.tan(mezzoV));
        camera.updateProjectionMatrix();
        daDisegnare = true;
    }
    let trascina = null;
    tela.addEventListener('pointerdown', e => {
        trascina = { x: e.clientX, y: e.clientY, azimut, polare };
        tela.setPointerCapture?.(e.pointerId);
    });
    tela.addEventListener('pointermove', e => {
        if (!trascina) return;
        azimut = limita(trascina.azimut - (e.clientX - trascina.x) * .006, AZIMUT_0 - 1.1, AZIMUT_0 + 1.1);
        polare = limita(trascina.polare - (e.clientY - trascina.y) * .004, .8, 1.42);
        daDisegnare = true;
    });
    for (const fine of ['pointerup', 'pointercancel', 'pointerleave']) tela.addEventListener(fine, () => { trascina = null; });
    tela.addEventListener('dblclick', () => { azimut = AZIMUT_0; polare = POLARE_0; daDisegnare = true; });

    /* ---------------- etichette ---------------- */
    const ancore = {
        sole: new THREE.Vector3(CASA_X, 5.35, CASA_Z),
        casa: new THREE.Vector3(CASA_X - 2.5, 2.0, CASA_Z + 2.9),
        auto: new THREE.Vector3(AUTO_X + .5, 0, AUTO_Z + 3.3),
        rete: new THREE.Vector3(PALO[0], 5.9, PALO[1]),
    };
    const v = new THREE.Vector3();
    function etichette() {
        const pos = {};
        for (const [nome, punto] of Object.entries(ancore)) {
            v.copy(punto).project(camera);
            /* l'etichetta dell'auto pende sotto il punto (davanti al muso), le altre stanno sopra */
            pos[nome] = { x: (v.x * .5 + .5) * largo, y: (-v.y * .5 + .5) * alto, sotto: nome === 'auto' };
        }
        suEtichette(pos, largo, alto);
    }

    /* ---------------- ciclo ---------------- */
    let attiva = true, visibile = true, daDisegnare = true, ultimoT = 0, richiesta = 0;
    function fotogramma(ms) {
        richiesta = requestAnimationFrame(fotogramma);
        if (!attiva || !visibile || document.hidden) return;
        const t = ms / 1000;
        const dt = Math.min(.1, t - ultimoT);
        /* 30 fotogrammi al secondo bastano, e scaldano meno il telefono; con
           "riduci movimento" si disegna solo quando cambia qualcosa */
        if (dt < 1 / 32 && !daDisegnare) return;
        if (moto.matches && !daDisegnare) return;
        ultimoT = t;
        daDisegnare = false;

        muoviFlussi(dt);
        const battito = .5 + .5 * Math.sin(t * 3.2);
        if (stato.inCarica) {
            alone.material.opacity = .32 + .3 * battito;
            presa.emissiveIntensity = spia.emissiveIntensity = 1.2 + 1.6 * battito;
            presa.emissive.copy(verde); spia.emissive.copy(verde);
        } else {
            alone.material.opacity = 0;
            const accesa = stato.colonnina ? (stato.collegata ? 1.1 : .35) : 0;
            presa.emissiveIntensity = stato.collegata ? .9 : 0;
            spia.emissiveIntensity = accesa;
            presa.emissive.copy(blu); spia.emissive.copy(blu);
        }
        if (!moto.matches) {
            for (const n of nuvole) {
                n.position.x += n.userData.velocita * dt;
                if (n.position.x > 16) n.position.x = -16;
            }
        }
        inquadra(t);
        renderer.render(scena, camera);
        etichette();
    }

    new ResizeObserver(ridimensiona).observe(tela);
    new IntersectionObserver(voci => { visibile = voci[0].isIntersecting; daDisegnare = true; }).observe(tela);
    renderer.domElement.addEventListener('webglcontextlost', e => e.preventDefault());
    renderer.domElement.addEventListener('webglcontextrestored', () => { daDisegnare = true; });
    ridimensiona();
    applicaStato();
    richiesta = requestAnimationFrame(fotogramma);

    return {
        aggiorna(dati) { Object.assign(stato, dati); applicaStato(); daDisegnare = true; },
        tema(nuovi) {
            Object.assign(colori, nuovi);
            for (const [nome, f] of Object.entries(flussi)) f.mesh.material.color.set(colori[nome]);
            blu.set(colori.auto); verde.set(colori.sole);
            alone.material.color.set(colori.auto);
            applicaStato();
            daDisegnare = true;
        },
        ferma() { attiva = false; },
        riprendi() { attiva = true; daDisegnare = true; ridimensiona(); },
    };
}
