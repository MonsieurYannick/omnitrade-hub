// Test headless : vérifie les erreurs JS, ouvre la page Licence via G('licence'),
// attend licRender, et contrôle la présence des nouvelles cartes d'activation.
import puppeteer from 'puppeteer';
const chromium = puppeteer;

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const page = await browser.newPage();
const errors = [];
page.on('pageerror', e => errors.push('PAGEERROR: ' + e.message));
page.on('console', m => { if (m.type() === 'error') errors.push('CONSOLE: ' + m.text()); });

await page.goto('http://127.0.0.1:8890/omnitrade-v21.html', { waitUntil: 'networkidle0', timeout: 60000 });

await page.evaluate(() => { try { G('licence'); } catch (e) { window.__navErr = e.message; } });

await page.waitForFunction(() => {
  const pg = document.getElementById('pg-licence');
  return pg && pg.classList.contains('on') && document.querySelector('#lic-mid')
         && document.querySelector('#lic-mid').textContent.trim().length > 0;
}, { timeout: 30000 }).catch(() => {});

const card = await page.evaluate(() => {
  const navErr = window.__navErr || null;
  const pg = document.getElementById('pg-licence');
  if (!pg) return { open: false, navErr };
  return {
    open: pg.classList.contains('on'),
    navErr,
    hasPurchaseCard: !!document.getElementById('lic-code'),
    hasClassicKey: !!document.getElementById('lic-key'),
    hasCodeBtn: !!document.querySelector('[onclick="licCodeActivate()"]'),
    machineId: (document.getElementById('lic-mid') || {}).textContent || '',
    licValid: window.LIC ? window.LIC.valid : null,
    licAbonnement: window.LIC_ABONNEMENT_ACTIF,
    reasons: window.licReasonText ? window.licReasonText('absente') : '(non défini)',
  };
});

console.log('CARTES:', JSON.stringify(card, null, 2));
console.log('ERREURS JS:', errors.length ? errors : 'aucune');
await browser.close();
process.exit(card.open && card.hasPurchaseCard && errors.length === 0 ? 0 : 1);