// test_lic_v9.mjs — valide la page Licence de la v9 (édition gratuite)
// sans moteur : statut « Édition gratuite », carte d'activation (e-mail + code),
// appareils, matrice gratuite/abonné, verrous de menu, journal lecture seule,
// Market Hub en mode calendrier seul.
//   usage : node --test test_lic_v9.mjs  (ou node test_lic_v9.mjs)
import puppeteer from '/Users/macbookdeeliysha/Documents/Default Project/OMNITRADE/OmniTradeHub-macOS v8.87/docs/_src/node_modules/puppeteer/lib/puppeteer/puppeteer.js';

async function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

const browser = await puppeteer.launch({ headless: true, args: ['--no-sandbox'] });
const page = await browser.newPage();
const errors = [];
page.on('pageerror', e => errors.push('PAGEERROR: ' + e.message));
page.on('console', m => { if (m.type() === 'error') errors.push('CONSOLE: ' + m.text()); });

await page.goto('http://127.0.0.1:8890/omnitrade-v21.html', { waitUntil: 'networkidle0', timeout: 90000 });
await sleep(1000);

await page.evaluate(() => { try { G('licence'); } catch (e) { window.__navErr = e.message; } });
await sleep(800);

const st = await page.evaluate(() => {
  const out = { navErr: window.__navErr || null };
  out.abonnementActif = (function () { try { return LIC_ABONNEMENT_ACTIF; } catch (e) { return 'scope-error'; } })();
  out.licOuvert = (typeof window.licOuvert === 'function') ? window.licOuvert() : null;
  const pg = document.getElementById('pg-licence');
  out.licenceOpen = !!(pg && pg.classList.contains('on'));
  // statut affiché (gratuit ou actif)
  out.statut = (document.querySelector('#pg-licence .lic-state') || {}).textContent || '';
  // carte d'activation
  out.hasEmail = !!document.getElementById('lic-email');
  out.hasCode = !!document.getElementById('lic-code');
  out.hasDevices = !!document.getElementById('lic-devices');
  out.hasDeviceId = !!document.getElementById('lic-mid');
  out.buyLink = (document.querySelector('#pg-licence a[href*="releases"]') || {}).getAttribute?.('href') || '';
  // bouton activer
  out.rawManualDisabled = (typeof window.licCanAddTrade === 'function') ? window.licCanAddTrade() : null;
  out.tradesLeft = (typeof window.licTradesLeft === 'function') ? window.licTradesLeft() : null;
  return out;
});

// Vérifications menu + navigation
const nav = await page.evaluate(() => {
  const lock = p => {
    const n = document.querySelector('.ni[data-page="' + p + '"]');
    return n ? n.classList.contains('lic-lock') : 'absent';
  };
  return { analytics: lock('analytics'), dashboard: lock('dashboard'), mt5: lock('mt5'), journal: lock('journal') };
});

// Journal lecture seule
await page.evaluate(() => { try { G('journal'); } catch (e) { window.__jErr = e.message; } });
await sleep(700);
const journal = await page.evaluate(() => {
  const pj = document.getElementById('pg-journal');
  const addBtn = document.getElementById('btn-add-trade');
  const note = document.getElementById('jc-free-note');
  return {
    jErr: window.__jErr || null,
    ro: !!(pj && pj.classList.contains('lic-ro')),
    addHidden: !!(addBtn && getComputedStyle(addBtn).display === 'none'),
    noteVis: !!(note && getComputedStyle(note).display !== 'none'),
  };
});

// Market Hub gratuit
await page.evaluate(() => { try { G('market'); } catch (e) { window.__mErr = e.message; } });
await sleep(1200);
const market = await page.evaluate(() => {
  const pm = document.getElementById('pg-market');
  return {
    mErr: window.__mErr || null,
    free: !!(pm && pm.classList.contains('pg-market-free')),
    sesHidden: !!(document.getElementById('mk-sec-ses') && getComputedStyle(document.getElementById('mk-sec-ses')).display === 'none'),
    calShown: !!(document.getElementById('mk-sec-cal') && getComputedStyle(document.getElementById('mk-sec-cal')).display !== 'none'),
  };
});

console.log('=== LIC ===', JSON.stringify(st, null, 2));
console.log('=== NAV ===', JSON.stringify(nav));
console.log('=== JOURNAL ===', JSON.stringify(journal));
console.log('=== MARKET ===', JSON.stringify(market));
console.log('=== ERREURS JS ===', errors.length ? errors : 'aucune');

let ok = true;
const checks = [
  ['licenceOpen', st.licenceOpen],
  ['statut est « Édition gratuite »', /Édition gratuite/.test(st.statut)],
  ['abonnementActif=true', st.abonnementActif === true],
  ['licOuvert()=false en gratuit', st.licOuvert === false],
  ['a #lic-email', st.hasEmail],
  ['a #lic-code', st.hasCode],
  ['a #lic-devices', st.hasDevices],
  ['a #lic-mid (device id)', st.hasDeviceId],
  ['a bouton Acheter (releases)', /releases/.test(st.buyLink)],
  ['licCanAddTrade()=false', st.rawManualDisabled === false],
  ['licTradesLeft()=30 (vide)', st.tradesLeft === 30],
  ['menu analytics verrouillé', nav.analytics === true],
  ['menu dashboard verrouillé', nav.dashboard === true],
  ['menu mt5 libre', nav.mt5 === false],
  ['menu journal libre', nav.journal === false],
  ['journal .lic-ro', journal.ro === true],
  ['journal btn-add-trade masqué', journal.addHidden === true],
  ['journal note gratuite visible', journal.noteVis === true],
  ['market .pg-market-free', market.free === true],
  ['market sessions masqués', market.sesHidden === true],
  ['market calendrier visible', market.calShown === true],
];
for (const [label, val] of checks) {
  console.log((val ? 'PASS' : 'FAIL') + '  ' + label);
  if (!val) ok = false;
}
const jsErr = errors.filter(e => !/favicon|browser-send|CFUN|ERR_|navigator.clipboard|supabase|Failed to load resource|reset.*host/i.test(e));
if (jsErr.length) { console.log('ERREURS JS PERSISTANTES:', jsErr); ok = false; }

await browser.close();
process.exit(ok ? 0 : 1);
