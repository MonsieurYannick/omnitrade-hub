// guides.mjs — génère les 5 guides client (PDF A4, français) avec les
// captures réelles de l'app (captures/*.png). Usage : node guides.mjs
import puppeteer from 'puppeteer'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const CAP = path.join(HERE, 'captures')
const OUT = process.env.GUIDE_DIR || path.join(HERE, '..')

function img(name, caption, css = '') {
  const p = path.join(CAP, name)
  if (!fs.existsSync(p)) return `<p style="color:#e85d5d">[image manquante : ${name}]</p>`
  const data = fs.readFileSync(p).toString('base64')
  /* Toutes les captures sont pleine page (très hautes). On force une hauteur
     max pour ne jamais déborder la page A4, et un centrage. Le `css` optionnel
     (ex. max-height plus grand) écrase cette valeur par défaut. */
  const style = `max-height:14cm;width:auto;max-width:100%;margin:0 auto;display:block;`
    + `border-radius:10px;border:1px solid #26334d;${css}`
  return (
    `<figure><img src="data:image/png;base64,${data}" alt="${caption}" style="${style}">`
    + `<figcaption>${caption}</figcaption></figure>`
  )
}

const CSS = `
  *{box-sizing:border-box}
  body{margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;
    color:#0f172a;font-size:11.5pt;line-height:1.55;background:#fff}
  .band{background:linear-gradient(90deg,#0b1220,#14224a);color:#fff;padding:26px 34px 22px;
    border-bottom:4px solid #2563eb}
  .band .logo{font-size:20px;font-weight:900;letter-spacing:.5px}
  .band .logo span{color:#7dd3fc}
  .band .sub{font-size:12px;color:#aab6cf;margin-top:6px}
  .band .meta{margin-top:12px;font-size:10.5px;color:#7084a8;letter-spacing:.4px;text-transform:uppercase}
  main{padding:26px 34px 40px}
  h2{font-size:15pt;margin:0 0 10px;color:#111a2e;border-left:5px solid #2563eb;padding-left:10px;page-break-after:avoid}
  h3{font-size:12.5pt;margin:16px 0 6px;color:#1d2b49}
  p{margin:0 0 10px}
  ol,ul{margin:0 0 12px;padding-left:22px}
  li{margin-bottom:6px}
  code{background:#eef2fb;border:1px solid #d7e0f2;border-radius:4px;padding:0 5px;
    font-family:Menlo,Consolas,monospace;font-size:10pt;color:#1d4ed8}
  .note,.warn,.tip{border-radius:8px;padding:10px 13px;margin:10px 0 14px;font-size:10.5pt}
  .note{background:#eef4ff;border:1px solid #c7d8ff;color:#1e3a8a}
  .warn{background:#fff4e5;border:1px solid #ffd9a8;color:#92400e}
  .tip{background:#ecfdf3;border:1px solid #bdf2d5;color:#166534}
  figure{margin:0 0 16px;page-break-inside:avoid}
  figcaption{font-size:9.5pt;color:#52607a;margin-top:6px;font-style:italic;text-align:center}
  .kbd{display:inline-block;background:#0b1220;color:#fff;border-radius:5px;padding:1px 8px;font-size:9.5pt}
  table{border-collapse:collapse;width:100%;font-size:10pt;margin:8px 0 14px}
  th,td{border:1px solid #d8e0ef;padding:6px 9px;text-align:left}
  th{background:#f0f4fc}
  .qr{text-align:center;background:#fff6ec;border:1px dashed #f0cfa8;border-radius:8px;padding:10px;margin:10px 0}
  h2.newpage{page-break-before:always}
`

const band = (num, title, tag) => `
  <div class="band">
    <div class="logo">OmniTrade <span>Hub</span> — Guide n°${num}</div>
    <div class="sub">${title}</div>
    <div class="meta">Guide client · v9.1.2 · ${tag}</div>
  </div>`

function page(bandHtml, body) {
  return `<html><head><meta charset="utf-8"><style>${CSS}</style></head><body>${bandHtml}<main>${body}</main></body></html>`
}

const FOOTER = `
  <div style="width:100%;font-size:8pt;color:#8a97ad;text-align:center;padding:0 30px;">
    OMNITRADE · OmniTrade Hub — guide client — page <span class="pageNumber"></span>/<span class="totalPages"></span>
  </div>`

async function pdf(html, file) {
  const browser = await puppeteer.launch({ headless: 'new', args: ['--no-sandbox', '--disable-gpu'] })
  const p = await browser.newPage()
  await p.setContent(html, { waitUntil: 'domcontentloaded', timeout: 60000 })
  await p.pdf({
    path: file,
    format: 'A4',
    printBackground: true,
    margin: { top: '14mm', bottom: '16mm', left: '0', right: '0' },
    displayHeaderFooter: true,
    footerTemplate: FOOTER,
  })
  await browser.close()
  console.log('GUIDE OK →', file)
}

// ═══════════════════════════ GUIDE 1 — INSTALLATION ═══════════════════════════
const G1 = page(band(1, 'Installation sur Mac et Windows', 'prérequis · lancement · dépannage'), `
  <h2>1. Ce qu'il vous faut</h2>
  <ul>
    <li>Un <b>Mac</b> (Intel ou Apple Silicon, macOS 12 ou plus récent) ou un PC <b>Windows 10/11</b> (64 bits).</li>
    <li>Une connexion Internet (nécessaire pour les cours, les actualités et les agents IA).</li>
    <li>MetaTrader 5 à part, <i>uniquement</i> si vous voulez la synchronisation automatique de vos trades.</li>
    <li>Le logiciel démarre en <b>édition gratuite</b> (MT5 30 trades, calendrier, alertes, watchlist).
      Après achat d'un code, activez-le (page <b>Licence</b>) pour tout débloquer — voir le guide n°4.</li>
  </ul>

  <h2>2. Télécharger le programme</h2>
  <p>Tout se passe sur la page de téléchargement unique :</p>
  <div class="qr"><b>https://github.com/MonsieurYannick/omnitrade-hub/releases</b><br>
  <span style="font-size:9.5pt">(onglet « Releases », ouverture automatique du tag le plus récent)</span></div>
  <p>Choisissez le fichier qui correspond à <b>votre</b> machine :</p>
  <table>
    <tr><th>Type de Mac</th><th>Le fichier à prendre</th></tr>
    <tr><td><b>Intel</b> (processeur Intel Core / Xeon)</td><td><code>OmniTradeHub-macOS-Intel.dmg</code></td></tr>
    <tr><td><b>Apple Silicon</b> (puce M1/M2/M3/M4)</td><td><code>OmniTradeHub-macOS-AppleSilicon.dmg</code></td></tr>
    <tr><td>PC Windows</td><td><code>OmniTradeHub-Setup-*.exe</code> (le plus récent)</td></tr>
  </table>
  <div class="note">Comment connaître son type de Mac ? Menu <b></b> (en haut à gauche)
  → <b>À propos de ce Mac</b> : « <b>Puce</b> » = Apple Silicon · « <b>Processeur</b> » = Intel.
  Prendre le mauvais fichier empêche le lancement : l'application vous dira alors lequel télécharger.</div>
  <div class="tip">⚠️ Prenez la <b>dernière version</b> (le tag le plus en haut de la page « Releases »,
  actuellement <b>v9.0.4</b>) : elle inclut la correction de l'activation des licences.</div>

  <h2>3. Installer sur Mac (2 minutes)</h2>
  <ol>
    <li>Ouvrez le fichier <code>OmniTradeHub-macOS-&lt;Intel|AppleSilicon&gt;.dmg</code> (double-clic)
      <b>selon votre type de Mac</b> (voir tableau ci-dessus).</li>
    <li>La fenêtre « Omni Trade Hub Bridge » s'ouvre (<b>macOS 12+</b>).</li>
    <li>Glissez l'application <b>OmniTradeHub.app</b> dans le dossier <b>Applications</b>.</li>
    <li>La première ouverture : <b>clic droit sur l'icône → Ouvrir</b>, puis <b>Ouvrir</b> à la confirmation.
      (Signe aléatoire normal pour un logiciel indépendant, pas encore « notarisé » Apple.)</li>
  </ol>
  <div class="tip">Après la première ouverture, l'icône de flacon lance l'application normalement
  (double-clic). C'est un pont local : il ne transmettra jamais vos données ailleurs sans votre action.</div>

  <h2>4. Installer sur Windows (2 minutes)</h2>
  <ol>
    <li>Ouvrez <code>OmniTradeHub-Setup-&lt;version&gt;.exe</code> (double-clic).</li>
    <li>Acceptez l'avertissement éventuel de Windows Defender (⟨ Informations complémentaires → Exécuter quand même ⟩).</li>
    <li>Terminez l'installation avec le bouton « Terminer » (aucun réglage à changer).</li>
    <li>Une icône « OmniTrade Hub » apparaît sur le bureau.</li>
  </ol>

  <h2>5. Premier lancement</h2>
  <p>L'application ouvre votre page d'accueil dans le navigateur par défaut. Cette page tourne
  <b>en local sur votre ordinateur</b> (adresse <code>127.0.0.1</code>) : chacun de vos appareils a
  son propre accès, personne d'autre ne voit vos données.</p>
  ${img('01-dashboard.png', 'Écran d\u2019accueil : vos statistiques et la navigation (journal, calendrier, agents IA, sauvegardes…).')}
  <h3>Raccourcis utiles</h3>
  <ul>
    <li>Vous pouvez épingler la page dans votre navigateur pour l'ouvrir d'un clic.</li>
    <li>Fermez l'application via l'icône de flacon (ou le terminal) quand vous ne tradez pas.</li>
  </ul>

  <h2 class="newpage">6. Dépannage rapide</h2>
  <table>
    <tr><th>Problème</th><th>Solution</th></tr>
    <tr><td>« Apple ne peut pas vérifier qu'il ne contient pas de logiciel malveillant »</td>
        <td>Clic droit sur l'icône → <b>Ouvrir</b> → <b>Ouvrir</b>.</td></tr>
    <tr><td>« application est endommagée »</td>
        <td>Fermez, puis cliquez droit sur l'icône → <b>Ouvrir</b> à nouveau ; si le message persiste,
            supprimez l'application dans Applications et ressayez l'installation.</td></tr>
    <tr><td>La page ne s'ouvre pas sur Windows</td>
        <td>Windows Defender → « Réseau privé » (autoriser le port 8765).</td></tr>
    <tr><td>La licence est-elle déjà active ?</td>
        <td>Oui : tous les modules sont ouverts d'office (voir guide n°4).</td></tr>
  </table>
  <div class="warn">Ne partagez jamais l'adresse <code>127.0.0.1:8765</code> sur Internet : elle n'est
  accessible que depuis votre propre ordinateur.</div>
`)

// ═══════════════════════════ GUIDE 2 — INTELLIGENCE ARTIFICIELLE ═══════════════════════════
const G2 = page(band(2, 'Connecter l\u2019intelligence artificielle (clés Groq, OpenRouter…)', 'agents IA · créer sa clé · dépannage'), `
  <h2>1. À quoi sert l'IA dans OmniTrade Hub ?</h2>
  <p>OmniTrade Hub utilise des modèles de langage pour rédiger en français les analyses
  (économie, calendrier, or, brevet technique…) à partir de vos données. Le logiciel n'a pas
  <b>sa propre</b> machine : il utilise <b>votre clé</b> personnelle, à vos frais ou gratuitement
  (Groq a un niveau gratuit). Vous gardez le contrôle total.</p>
  <ul>
    <li><b>Pages concernées :</b> Analytics (analyse sèche), Market Hub (actu), Calendrier éco,
      Gold Event Risk, brève, coach & professeur — partout où un « analyseur » est proposé.</li>
    <li><b>Règle de confidentialité :</b> votre clé est stockée <b>uniquement sur votre ordinateur</b>
      (jamais sur un serveur OmniTrade). Seuls les textes nécessaires à la demande sont envoyés au
      fournisseur choisi.</li>
  </ul>

  <h2>2. Créer une clé Groq (la plus simple, gratuite)</h2>
  <ol>
    <li>Allez sur <code>console.groq.com</code> → <b>Sign up</b> (connexion Google ou e-mail).</li>
    <li>Menu à gauche → <b>API Keys</b> → bouton <b>Create API Key</b>.</li>
    <li>Donnez un nom (ex. « omnitrade ») puis copiez la clé : elle commence par <code>gsk_…</code>.</li>
    <li>Le niveau gratuit inclut <code>llama-3.3-70b</code>, largement suffisant pour les analyses.</li>
  </ol>

  <h2>3. Enregistrer la clé dans l'application</h2>
  <ol>
    <li>Menu de gauche → <b>Licence</b> (dernière page).</li>
    <li>Dans la carte <b>« Groq — analyse des discours (Fed &amp; Macro) »</b>, collez la clé <code>gsk_…</code>.</li>
    <li>Cliquez sur <b>Enregistrer Groq</b> : le message « Clé Groq enregistrée » s'affiche.</li>
  </ol>
  ${img('03-licence.png', 'Page Licence : les cartes de clés IA (Groq, OpenRouter, Gemini, Mistral, NVIDIA).', 'max-height:22cm;width:auto;margin:0 auto;display:block;')}
  <div class="note">Une fois enregistrée, la clé n'est plus affichée. Pour la changer : collez la
  nouvelle clé et cliquez de nouveau sur « Enregistrer ». Pour l'effacer, cliquez avec le champ vide.</div>

  <h2 class="newpage">4. Les autres clés (secours et spécialisées)</h2>
  <table>
    <tr><th>Carte</th><th>Où l'obtenir</th><th>Format</th><th>Utilisation</th></tr>
    <tr><td><b>OpenRouter</b></td><td>openrouter.ai → Settings → Keys</td><td><code>sk-or-v1-…</code></td>
        <td>Secours automatique si Groq est saturé (durée 429).</td></tr>
    <tr><td><b>Gemini</b></td><td>aistudio.google.com → Get API key</td><td><code>AIza…</code></td>
        <td>Seconde solution si Groq sature.</td></tr>
    <tr><td><b>Mistral</b></td><td>console.mistral.ai → API keys</td><td><code>Votre clé</code></td>
        <td>Chat en français (réponse de la lecture).</td></tr>
    <tr><td><b>NVIDIA NIM</b></td><td>build.nvidia.com → Get API Key</td><td><code>nvapi-…</code></td>
        <td>Secours rapide.</td></tr>
  </table>
  <p>Une seule clé (Groq) suffit pour commencer. Les autres sont des parachutes automatiques :
  si Groq renvoie une erreur de quota, le pont essaie la suivante dans l'ordre.</p>

  <h2>5. Vérifier que ça marche</h2>
  <ul>
    <li>Ouvrez une page qui propose un analyseur (ex. <b>Market Hub</b> → un article → « Analyser »).</li>
    <li>Le résultat arrive en quelques secondes : le score chiffré est calculé par le lexique local,
      puis le texte est rédigé par l'IA choisie.</li>
  </ul>
  <div class="tip">Groq répond vite mais ses serveurs gratuits peuvent refuser la requête
  (« 429 ») : c'est pour cela que les clés de secours existent. Aucune action de votre part n'est
  nécessaire.</div>

  <h2>6. Confidentialité — ce que vos clés ne font pas</h2>
  <ul>
    <li>Elles ne sont <b>jamais envoyées à OmniTrade Hub</b>.</li>
    <li>Elles ne sont <b>jamais affichées</b> dans la page (le champ est en mode « mot de passe »).</li>
    <li>Elles sont lisibles <b>uniquement par le moteur local</b> (fichiers dans le dossier du moteur
      sur votre Mac/PC).</li>
  </ul>
`)

// ═══════════════════════════ GUIDE 3 — TÉLÉGRAMME ═══════════════════════════
const G3 = page(band(3, 'Alertes Telegram — recevoir vos briefs de séance sur WhatsApp-like', 'BotFather · jeton · tests'), `
  <h2>1. Ce que vous recevez</h2>
  <ul>
    <li><b>Brief de chaque session</b> automatique, 10 min avant l'ouverture réelle de la ville :
      Sydney, Tokyo, Londres, New York (+ Radar du jour + variation des biais).</li>
    <li><b>High T-15</b> (15 min avant l'événement High), <b>flash Or</b>, <b>communiqué G4</b>,
      <b>synthèse 18h Abidjan</b>.</li>
    <li>Vous pouvez <b>poser une question libre</b> au bot (ex. « où est l'or ce soir ? ») : il utilise
      l'IA pour répondre <b>sur ce sujet précis</b>.</li>
  </ul>
  <div class="note">Le bot exige que <b>votre ordinateur soit allumé</b> (le pont doit tourner).
  Pas de signal d'entrée : c'est un assistant de veille, pas un conseiller, conformément aux règles.</div>

  <h2>2. Créer votre bot TOUTE SEULE (2 minutes)</h2>
  <ol>
    <li>Ouvrez Telegram et cherchez <b>@BotFather</b> (le robot officiel de création).</li>
    <li>Envoyez <b>/newbot</b>.</li>
    <li>Choisissez un <b>nom</b> (ex. « Alerte Or Yannick ») puis un <b>username</b> se terminant par <code>bot</code> (ex. <code>alerteor_yannick_bot</code>).</li>
    <li>BotFather vous donne un <b>jeton</b> : il ressemble à <code>123456:AAF…</code> — copiez-le, vous ne
      pourrez plus le revoir.</li>
    <li>Ouvrez le chat de <b>votre bot</b> (depuis Telegram, tapez votre username) et envoyez <b>/start</b>.</li>
  </ol>
  <div class="warn">Chaque trader a <b>son propre bot</b> : si vous vous connectez au bot de quelqu'un
  d'autre, rien ne fonctionnera (et l'autre trader ne verra pas vos données). Créez le vôtre.</div>

  <h2>3. Connecter le bot dans OmniTrade Hub</h2>
  <ol>
    <li>Menu de gauche → <b>Support</b> (la carte « Telegram — alertes et dossiers »).</li>
    <li>Collez le jeton dans le champ <b>« Jeton du bot »</b>.</li>
    <li>Cliquez sur <b>Enregistrer</b>, puis <b>Test</b> : vous recevez un message de confirmation.</li>
    <li>Pour recevoir un récapitulatif immédiat, cliquez <b>Dossier complet</b> ou <b>Macro</b>.</li>
  </ol>
  ${img('04-support.png', 'Page Support : la carte Telegram avec jeton, boutons Enregistrer / Test / Dossier complet.', 'max-height:22cm;width:auto;margin:0 auto;display:block;')}

  <h2>4. Activer l'IA du bot</h2>
  <p>Le bot peut répondre à vos questions avec l'IA (or, EURUSD, session…) si vos clés IA sont
  installées (guide n°2). Sur la carte Telegram, cliquez <b>« Connecter l'IA au bot »</b> — il lit les
  mêmes clés que l'application (fichiers à côté du moteur).</p>

  <h2>5. Dépannage</h2>
  <table>
    <tr><th>Problème</th><th>Solution</th></tr>
    <tr><td>« Bot introuvable »</td><td>Vérifiez le username (il finit par <code>bot</code>).</td></tr>
    <tr><td>« Le bot ne répond pas / Test muet »</td><td>Envoyez d'abord <b>/start</b> dans le chat de votre bot, puis Test.</td></tr>
    <tr><td>« Le bot répond depuis un autre ordinateur »</td><td>Normal : c'est le pont de <b>cet</b> ordinateur qui tourne. Le bot suit l'ordinateur allumé.</td></tr>
  </table>
`)

// ═══════════════════════════ GUIDE 4 — LICENCE & ACTIVATION ═══════════════════════════
const G4 = page(band(4, 'Licence, code d\u2019achat et activation', 'code OTH · e-mail · appareils'), `
  <h2>1. Édition gratuite : que puis-je utiliser ?</h2>
  <p>À l'installation, OmniTrade Hub démarre en <b>édition gratuite</b>. Vous gardez l'essentiel :</p>
  <ul>
    <li><b>MT5 Sync</b> — jusqu'à <b>30 trades</b> synchronisés depuis MetaTrader 5.</li>
    <li><b>Calendrier économique</b> (Market Hub).</li>
    <li><b>Alertes de prix</b> &amp; <b>watchlist</b>.</li>
    <li><b>Journal</b> — consultation de vos trades MT5 (30 max) en <b>lecture seule</b>.</li>
  </ul>
  <p>Le reste (Analytiques, Playbook, Risque, Éducation &amp; Macro, Gold, COT, Sauvegarde Cloud,
  saisie manuelle…) est réservé aux abonnés. Un bandeau en haut de l'interface indique
  toujours les trades MT5 restants (gratuit) et propose <b>« Obtenir l'abonnement »</b>.</p>
  ${img('03-licence.png', 'La page Licence en édition gratuite : statut, activation par code + e-mail, vos appareils, identifiant logiciel et liste de ce que débloque l\u2019abonnement.', 'max-height:22cm;width:auto;margin:0 auto;display:block;')}

  <h2 class="newpage">2. Activer avec votre code d'achat (OTH-…)</h2>
  <p>Après achat, vous recevez un <b>code d'achat</b> (ex. <code>OTH-XXXXXXXX-XXXXXXXX</code>)
  par e-mail. Pour activer :</p>
  <ol>
    <li>Menu de gauche → <b>Licence</b>.</li>
    <li>Renseignez l'<b>e-mail utilisé lors de l'achat</b> (c'est votre compte) puis collez le
        code reçu dans le champ <b>« code d'achat »</b>.</li>
    <li>Cliquez <b>« Activer automatiquement »</b> : l'application contacte le serveur,
       crée votre clé liée à <b>cet appareil</b> et vous déverrouille — <b>rien d'autre à faire</b>.</li>
    <li>La page affiche alors « Licence active — &lt;plan&gt;, jusqu'au &lt;date&gt;, X jours restants ».</li>
  </ol>
  <div class="tip">Activation en quelques secondes, même le week-end. Aucune manipulation de clé,
  aucun fichier à copier. Chaque code est limité à un nombre d'appareils (indiqué lors de l'achat).</div>

  <h2>3. Vos appareils &amp; l'identifiant logiciel</h2>
  <p>La partie <b>« Identifiant de votre appareil »</b> est un code local qui n'a pas de secret :
  c'est lui que le serveur associe à votre licence. Il est <b>stable</b> — il ne change pas quand
  vous réinstallez OmniTrade Hub ou mettez à jour votre système.</p>
  <p>La carte <b>« Vos appareils »</b> et son bouton <b>« Afficher mes appareils »</b> listent les
  appareils enregistrés sur votre code. Cliquez <b>Retirer</b> pour libérer un emplacement
  (utile après un changement de machine).</p>
  <div class="warn">Ne partagez votre code d'achat avec personne ; il est lié à votre e-mail de
  commande et à un quota d'appareils. Restez bien connecté à Internet le jour de l'activation.</div>

  <h2>4. Renouvellement / nouvelles commandes</h2>
  <ul>
    <li><b>Renouveler</b> un plan payant ? Revenez sur la page de vente et recommandez : vous recevrez
      un nouveau code, à activer de la même façon.</li>
    <li><b>Changer d'appareil</b> ? Retirez l'ancien appareil depuis la carte « Vos appareils », puis
      activez sur le nouveau avec le même code + e-mail.</li>
    <li><b>Perdre son code ?</b> Écrivez au support avec votre e-mail de commande : on retrouve votre
      achat et on vous renvoie le code.</li>
  </ul>
`)

// ═══════════════════════════ GUIDE 5 — SAUVEGARDE & SYNCHRO MT5 ═══════════════════════════
const G5 = page(band(5, 'Sauvegardes et synchronisation MetaTrader 5', 'cloud · backup local · sync MT5'), `
  <h2>1. Protéger vos données : trois outils complémentaires</h2>
  <p>Votre journal de trades, objectifs et analyses sont précieux. OmniTrade Hub enregistre tout en
  local et propose 2 sauvegardes :</p>
  <ul>
    <li><b>Sauvegarde Cloud</b> (page <b>Sauvegarde Cloud</b>) : compte e-mail sécurisé, synchronisation
      multi-appareils. Activez la crépine « Sauvegarder après modification » pour l'automatique.</li>
    <li><b>💾 Backup local</b> : exporte un fichier de données JSON sur ce Mac/PC — idéal pour un
      dossier externe ou un cloud personnel (iCloud, OneDrive…).</li>
  </ul>

  <h2>2. Première sauvegarde cloud (3 minutes)</h2>
  <ol>
    <li>Menu de gauche → <b>Sauvegarde Cloud</b>.</li>
    <li>Créez un compte avec votre e-mail (ou connectez-vous).</li>
    <li>Cliquez <b>☁️ Sauvegarder maintenant</b>. L'état passe de « 🔴 Non connecté » à « Connecté ».</li>
    <li>Cochez <b>« Sauvegarder après modification »</b> : le pont pousse vos données seul.</li>
  </ol>
  <div class="note">« Restaurer à l'ouverture » ramène la sauvegarde sur un autre ordinateur :
  installez l'app sur le nouveau poste, connectez-vous, et vos données reviennent.</div>

  <h2 class="newpage">3. Synchroniser vos trades MetaTrader 5</h2>
  <p>La page <b>MT5</b> connecte OmniTrade Hub à votre terminal MetaTrader 5 pour importer
  automatiquement vos ordres, gains et pertes dans le journal.</p>
  <div class="note"><b>Comment ça marche ?</b> Il n'y a aucune connexion entre MetaTrader et le pont.
  Un petit <b>module</b> (un « Expert Advisor ») tourne <b>dans</b> MetaTrader et <b>écrit des fichiers</b>
  (compte, historique, positions) ; le pont les <b>lit</b>. Le module ne place <b>jamais</b> d'ordre :
  il se contente de lire votre historique. Rien ne sort de votre ordinateur.</div>

  <h3>A. Sur Windows (simple)</h3>
  <ol>
    <li>Ouvrez MetaTrader 5 et connectez-vous au broker.</li>
    <li>Dans OmniTrade Hub → <b>MT5</b>, renseignez <b>login</b>, <b>mot de passe (investisseur de préférence)</b>
      et <b>serveur du broker</b> (ex. <code>ICMarkets-Demo01</code>).</li>
    <li>Cliquez <b>Se connecter à MT5</b> : la synchronisation démarre (le pont utilise le terminal pour
      ne jamais exposer votre mot de passe au navigateur).</li>
    <li>Activez l'algorithme : <b>Outils → Options → Expert Advisors → « Autoriser le trading algorithmique »</b>.</li>
  </ol>
  <div class="tip">Sur Windows, rien à installer de plus : le module est inclus.</div>

  <h3>B. Sur Mac : c'est automatique</h3>
  <p>MetaTrader 5 tourne sous Wine sur Mac. Le pont prépare <b>tout seul</b> le petit module
  (<code>OmniTradeExport</code>) au premier lancement : il le copie dans le bon dossier, le
  <b>compile</b> et <b>ouvre MetaTrader 5</b>. Il ne vous reste que 2 gestes :</p>
  <ol>
    <li>Dans MetaTrader 5 : ouvrez un <b>graphique</b> (n'importe quelle paire) et <b>glissez</b>
      <code>OmniTradeExport</code> dessus depuis le panneau <b>Navigateur</b>.</li>
    <li>Cochez <b>« Autoriser le trading algorithmique »</b> → <b>OK</b>. Un petit <b>visage souriant</b>
      apparaît en haut du graphique.</li>
  </ol>
  <div class="tip">Aucune compilation, aucun collage de code : le pont s'occupe de tout.
  Si MetaTrader 5 n'est pas encore installé, le pont vous prévient et vous guide.</div>
  ${img('05-mt5.png', 'Page MT5 : connexion au compte broker (login, mot de passe, serveur) et espace de synchronisation.', 'max-height:20cm;width:auto;margin:0 auto;display:block;')}
  <div class="warn">Le module <b>ne passe aucun ordre</b> : il lit seulement votre historique (compte, trades,
  positions) et l'écrit sur disque. Laissez le graphique avec le visage souriant <b>ouvert</b>.</div>

  <h3>C. Dans les deux cas</h3>
  <ul>
    <li>Laissez la fenêtre noire (le pont) <b>ouverte</b> : c'est elle qui lit les fichiers.</li>
    <li>L'app reste utilisable <b>sans MetaTrader</b> : seule la sync des ordres en a besoin.</li>
    <li>Fermez MetaTrader le temps d'une pause ? La sync reprend au prochain lancement.</li>
  </ul>

  <h2>4. Après une réinstallation</h2>
  <ol>
    <li>Réinstallez OmniTrade Hub (guide n°1).</li>
    <li>Page <b>Sauvegarde Cloud</b> → connectez-vous → <b>Restaurer du Cloud</b> (ou cliquez le bouton
      local « ⋯ » si vous avez fait un Backup local).</li>
    <li>Vos journaux, objectifs et réglages reviennent.</li>
  </ol>
  <div class="tip">La licence n'étant pas liée à la machine, elle reste valable après réinstallation.</div>
`)

// ═══════════════════════════ GUIDE 6 — NOTIFICATIONS MOBILES (ntfy) ═══════════════════════════
const G6 = page(band(6, 'Notifications mobiles gratuites (ntfy)', 'installer l\u2019app · topic privé · alerte'), `
  <h2>1. Qu\u2019est-ce que ntfy ?</h2>
  <p><b>ntfy</b> est un service de notifications push <b>100 % gratuit</b>, <b>sans compte</b> et <b>sans
  carte bancaire</b>. Il envoie des alertes directement sur votre téléphone, <b>même quand l\u2019application
  est fermée</b>. C\u2019est l\u2019alternative idéale à Telegram si vous ne voulez pas créer de bot.</p>
  <p>Avec OmniTrade Hub, ntfy vous envoie les <b>mêmes messages que Telegram</b> : briefs de séance,
  alertes de prix, suivi Prop Firm, radar, or, calendrier… dès que le pont détecte quelque chose.</p>

  <h2>2. Trouver et installer l\u2019app ntfy</h2>
  <ol>
    <li><b>Apple (iPhone)</b> : ouvrez l\u2019<code>App Store</code>, recherchez <b>"ntfy"</b>.
      L\u2019app officielle a une icône <b>orange/jaune</b>. Installez-la.</li>
    <li><b>Android</b> : ouvrez <code>Google Play</code>, recherchez <b>"ntfy"</b>. Installez-la.</li>
    <li>Lancez l\u2019app ntfy : elle vous demande d\u2019activer les notifications système. <b>Acceptez</b>.</li>
  </ol>
  <div class="note">Le site officiel est <code>https://ntfy.sh</code>. Vous pouvez aussi y tester l\u2019envoi
  sans installer l\u2019app (on verra plus bas).</div>

  <h2>3. Choisir un « topic » privé</h2>
  <p>Un <b>topic</b> est une chaîne secrète qui identifie votre canal de notifications. Elle doit être
  <b>unique et imprévisible</b> : ne mettez jamais votre e-mail ni votre nom.</p>
  <div class="tip">Exemple de bon topic : <code>omnitrade-x7Kp2-9zqL-Mrv5</code>.
  Plus c\u2019est long/aléatoire, plus c\u2019est privé — personne d\u2019autre ne pourra s\u2019y abonner.</div>

  <h2>4. S\u2019abonner au topic dans l\u2019app ntfy</h2>
  <ol>
    <li>Dans l\u2019app ntfy, en bas à gauche : icône <b>+ (ajouter)</b> ou menu <b>Abonnements</b>.</li>
    <li>Entrez votre <b>topic</b> (ex. <code>omnitrade-x7Kp2-9zqL-Mrv5</code>).</li>
    <li>Validez : vous êtes abonné. Les notifications arriveront ici.</li>
  </ol>

  <h2>5. Configurer OmniTrade Hub (page Licence)</h2>
  <ol>
    <li>Dans OmniTrade Hub, ouvrez la page <b>« Licence »</b> (ou « Support »).</li>
    <li>Trouvez la carte <b>« 📱 Notifications mobiles (ntfy) »</b>.</li>
    <li>Collez votre <b>topic</b> dans le champ (celui que vous avez créé à l\u2019étape 4).</li>
    <li>Cliquez <b>« Enregistrer »</b> puis <b>« ➤ Tester »</b>.</li>
    <li>Une notification « ... fonctionne ! » doit apparaître sur votre téléphone. ✅</li>
  </ol>
  <div class="warn">Le pont (la fenêtre noire) doit <b>tourner</b> et votre Mac/PC doit être allumé.
  C\u2019est lui qui envoie les notifications vers ntfy.</div>

  <h2>6. Que recevez-vous ?</h2>
  <ul>
    <li><b>Alertes de prix</b> (seuil, %, franchissement).</li>
    <li><b>Suivi Prop Firm</b> : cible atteinte, drawdown proche de la limite, challenge validé.</li>
    <li><b>Briefs de séance</b>, radar du jour, variation des biais, flash Or, calendrier…</li>
  </ul>
  <div class="tip">Tout ce que Telegram reçoit est <b>aussi</b> envoyé sur ntfy. Vous pouvez donc activer
  les deux : Telegram pour discuter, ntfy pour la simplicité.</div>
`)

const files = [
  ['01-Installation-macOS-Windows.pdf', G1, 'Guide 1 — Installation (Mac & Windows)'],
  ['02-IA-Groq-OpenRouter.pdf', G2, 'Guide 2 — Intelligence artificielle'],
  ['03-Telegram-Bot.pdf', G3, 'Guide 3 — Alertes Telegram'],
  ['04-Licence-Activation.pdf', G4, 'Guide 4 — Licence & activation'],
  ['05-Sauvegarde-MT5.pdf', G5, 'Guide 5 — Sauvegardes & MT5'],
  ['06-Notifications-ntfy.pdf', G6, 'Guide 6 — Notifications mobiles (ntfy)'],
]

for (const [name, html, label] of files) {
  await pdf(html, path.join(OUT, name))
}
console.log('Tous les guides sont dans ' + OUT)