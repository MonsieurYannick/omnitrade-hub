// capture.mjs — captures réelles de l'app (moteur local + HTML embarqué).
// Usage : APP_URL=http://127.0.0.1:8765/?token=… node capture.mjs
import puppeteer from 'puppeteer'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const OUT = process.env.CAPTURE_DIR || path.join(HERE, 'captures')
fs.mkdirSync(OUT, { recursive: true })

const APP_URL = process.env.APP_URL || 'http://127.0.0.1:8765/'

const browser = await puppeteer.launch({
  headless: 'new',
  args: ['--no-sandbox', '--disable-gpu', '--hide-scrollbars', '--force-device-scale-factor=2'],
})
const page = await browser.newPage()
await page.setViewport({ width: 1280, height: 820, deviceScaleFactor: 2 })

page.on('console', (m) => {
  if (m.type() === 'error') console.log('[app console error]', m.text().slice(0, 160))
})
page.on('pageerror', (e) => console.log('[app pageerror]', String(e).slice(0, 160)))

await page.goto(APP_URL, { waitUntil: 'networkidle0', timeout: 60000 })
await page.waitForSelector('.sidebar', { timeout: 30000 }).catch(() => {})
await new Promise((r) => setTimeout(r, 3000))

async function shot(id, name) {
  try {
    const ok = await page.evaluate((pid) => {
      const doNav = () => {
        if (typeof G === 'function') { G(pid); return true }
        if (typeof montrer === 'function') { montrer(pid); return true }
        const m = document.querySelector('.ni[data-page="' + pid + '"]')
        if (m) { m.onclick ? m.onclick() : m.click(); return true }
        return false
      }
      const r = doNav()
      const p = document.querySelector('#pt')
      if (p && typeof TITLES !== 'undefined' && TITLES[pid]) p.textContent = TITLES[pid]
      return r
    }, id)
    await new Promise((r) => setTimeout(r, 2000))
    await page.screenshot({ path: path.join(OUT, name), fullPage: true })
    console.log('ok', name, 'nav=' + ok)
  } catch (e) {
    console.log('er', id, e.message)
  }
}

await shot('dashboard', '01-dashboard.png')
await shot('journal', '02-journal.png')
await shot('licence', '03-licence.png')
await shot('support', '04-support.png')
await shot('mt5', '05-mt5.png')
await shot('cb', '06-market-intel.png')

await browser.close()
console.log('terminé →', OUT)