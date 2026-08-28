// test_smoke_shipped.mjs — fume test de la VERSION EXPÉDIÉE actuelle
// (LIC_ABONNEMENT_ACTIF = false) : zéro erreur JS, navigation et pages OK.
import puppeteer from 'puppeteer';
const chromium = puppeteer;

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const page = await browser.newPage();
const errors = [];
page.on('pageerror', e => errors.push('PAGEERROR: ' + e.message));
page.on('console', m => { if (m.type() === 'error') errors.push('CONSOLE: ' + m.text()); });

await page.goto('http://127.0.0.1:8890/omnitrade-v21.html', { waitUntil: 'networkidle0', timeout: 60000 });
await new Promise(r => setTimeout(r, 2500));

const st = await page.evaluate(() => {
  const nav = document.querySelectorAll('#sb .nv, #sb [class*="nav"], nav a').length;
  const licence = document.getElementById('pg-licence');
  if (!licence) return { erreur: 'page licence absente' };
  return {
    abonnementActif: window.LIC_ABONNEMENT_ACTIF,
    pageLicenceExiste: true,
    lienLicence: licence.classList.contains('on') ? false : true,
    fnAjoutee: typeof window.licCodeActivate === 'function',
  };
});

console.log(JSON.stringify({ st, erreurs: errors.filter(e => !e.includes('favicon')) }, null, 2));
await browser.close();
process.exit((errors.filter(e => !e.includes('favicon')).length || !st.pageLicenceExiste) ? 1 : 0);