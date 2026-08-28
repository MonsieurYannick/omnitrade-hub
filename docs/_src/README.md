# Source des guides PDF (dossier docs/)

Les 5 PDF du dossier `docs/` sont générés par Puppeteer à partir de **captures
réelles** de l'application (le moteur local sert le HTML embarqué = le produit
shippé exactement).

## Régénérer les PDF

1. Démarrer le moteur local (le pont du bundle) sur le port 8765 :
   ```
   export PATH="$HOME/bin:$PATH"   # si nécessaire
   BIN="../../OmniTradeHub.app/Contents/Resources/OmniTradeBridge/OmniTradeBridge"
   "$BIN" --host 127.0.0.1 --port 8765 --token "$("$BIN" --show-token --no-keep-open | tail -1)" &
   ```
2. Captures :
   ```
   npm install
   APP_URL="http://127.0.0.1:8765/?token=⋯" npm run capture
   ```
3. Guides :
   ```
   GUIDE_DIR="$(pwd)/.." npm run guides    # = dossier docs/
   ```
Puis tuer le moteur.

## Contenu

| Fichier                | Sujet                         | Captures utilisées |
|------------------------|-------------------------------|--------------------|
| `01-Installation…pdf`  | Installation Mac + Windows    | 01-dashboard       |
| `02-IA-Groq…pdf`       | Clés IA (Groq, OpenRouter…)   | 03-licence, 03     |
| `03-Telegram-Bot.pdf`  | Bot BotFather + alertes       | 04-support         |
| `04-Licence…pdf`       | Licence, code d'achat, renouv | 03-licence         |
| `05-Sauvegarde-MT5.pdf`| Cloud, export, sync MT5       | 05-mt5             |

Note : les captures doivent être refaites avant de republier si l'interface
change (les fichiers `captures/*.png` sont ignorés par git, les PDF sont
versionnés).