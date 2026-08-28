// test_flow_purchase.mjs — prouve le câblage du bouton d'activation
// (serveur mock oth-activate → licCodeActivate → moteur local → verdict).
import puppeteer from 'puppeteer';
const chromium = puppeteer;

const SK = '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef';
const { makeLicense } = await import('/Users/macbookdeeliysha/Documents/Default Project/OMNITRADE/OmniTradeHub-macOS v8.87/activation/supabase/functions/_shared/oth_core.ts');

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const page = await browser.newPage();
const errors = [];
const reqs = [];
page.on('pageerror', e => errors.push('PAGEERROR: ' + e.message));
page.on('console', m => { if (m.type() === 'error') errors.push('CONSOLE: ' + m.text()); });
page.on('request', (req) => {
  if (req.url().includes('/functions/v1/oth-activate')) reqs.push(req.method() + ' ' + (req.postData() || ''));
});
await page.setRequestInterception(true);
page.on('request', async (req) => {
  if (req.url().includes('/functions/v1/oth-activate')) {
    // Répond au préflight CORS avec les bons en-têtes (Supabase le fait
    // automatiquement en production pour les fonctions déployées).
    if (req.method() === 'OPTIONS') {
      return await req.respond({ status: 204, headers: {
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Headers': 'authorization, apikey, content-type',
        'Access-Control-Allow-Methods': 'POST, OPTIONS',
      }});
    }
    let body; try { body = JSON.parse(req.postData()); } catch { body = {}; }
    try {
      const lic = await makeLicense(SK, 'm12', 365, body.machine_id, 1);
      await req.respond({ status: 200, contentType: 'application/json', headers: {
        'Access-Control-Allow-Origin': '*',
      }, body: JSON.stringify({ ok: true, key: lic.key, plan: 'm12', expires: lic.payload.exp, sn: lic.payload.sn }) });
    } catch (e) {
      await req.respond({ status: 500, contentType: 'application/json',
        body: JSON.stringify({ ok: false, code: 'mockfail', msg_fr: String(e.message) }) });
    }
    return;
  }
  await req.continue();
});

await page.goto('http://127.0.0.1:8890/omnitrade-v21.html', { waitUntil: 'networkidle0', timeout: 60000 });
await page.evaluate(() => { try { G('licence'); } catch (e) { window.__gErr = e.message; } });
// attendre que le moteur ait donné le code machine (sinon licCodeActivate refuse)
await page.waitForFunction(() =>
  (document.getElementById('lic-mid') || {}).textContent &&
  /^[A-Z2-7]{16}$/.test((document.getElementById('lic-mid') || {}).textContent.trim()),
  { timeout: 30000 }).catch(() => {});
await page.waitForSelector('#lic-code', { timeout: 10000 }).catch(() => {});

// On exécute le flux ENTIEREMENT dans la page (pas de course puppeteer/DOM)
const verdicts = await page.evaluate(async () => {
  const V = {
    hasFn: typeof window.licCodeActivate === 'function',
    hasBtn: !!document.querySelector('[onclick="licCodeActivate()"]'),
    machineId: (window.LIC && (window.LIC.machineId || window.LIC.machine)) || '',
  };
  const inp = document.getElementById('lic-code');
  if (inp) {
    inp.value = 'OTH-AAAAAAA-BBBBBBBB';
    try { await window.licCodeActivate(); } catch (e) { V.callErr = String(e.message || e); }
    await new Promise(r => setTimeout(r, 1500));
  }
  V.codePreserve = (document.getElementById('lic-code') || {}).value || '(vide)';
  const g = (id) => { const el = document.getElementById(id); return el ? el.textContent.trim().slice(0, 150) : '(absent)'; };
  V.licMsg = g('lic-msg');
  V.codeMsg = g('lic-code-msg');
  V.licValid = !!(window.LIC && window.LIC.valid);
  const pg = document.getElementById('pg-licence');
  V.pageDump = pg ? pg.innerHTML.slice(0, 1200) : '(page absente)';
  return V;
});

console.log(JSON.stringify({ reqs, verdicts, errors }, null, 2));
await browser.close();
process.exit(errors.filter(e => !e.includes('favicon')).length ? 1 : 0);