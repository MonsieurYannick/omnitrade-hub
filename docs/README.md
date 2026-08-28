# 📚 Guides clients — OmniTrade Hub (v8.88)

Guides prêts à envoyer aux clients, au format **A4 PDF**. Ils sont générés à
partir de **captures réelles** de l'application (voir `_src/README.md` pour la
régénération).

| # | Fichier | Contenu |
|---|---------|---------|
| 1 | [`01-Installation-macOS-Windows.pdf`](01-Installation-macOS-Windows.pdf) | Installer sur Mac (DMG) et Windows (EXE), premier lancement, dépannage Gatekeeper/Defender |
| 2 | [`02-IA-Groq-OpenRouter.pdf`](02-IA-Groq-OpenRouter.pdf) | Créer une clé Groq (gratuite), l'enregistrer, clés de secours (OpenRouter, Gemini, Mistral, NVIDIA), confidentialité |
| 3 | [`03-Telegram-Bot.pdf`](03-Telegram-Bot.pdf) | Créer son bot avec BotFather, connecter le jeton dans l'app, briefs de séance automatiques, dépannage |
| 4 | [`04-Licence-Activation.pdf`](04-Licence-Activation.pdf) | Version déjà complète, activer un code d'achat `OTH-…`, nombre d'ordinateurs, renouvellement |
| 5 | [`05-Sauvegarde-MT5.pdf`](05-Sauvegarde-MT5.pdf) | Sauvegarde Cloud (compte e-mail), backup local, synchronisation MetaTrader 5 |

## À savoir avant envoi

- Les captures montrent l'app réelle (bureau sombre, interface propriétaire) :
  pas de retouche, elles reflètent le v8.88.
- Le téléchargement pointé dans les guides est la page publique
  `github.com/MonsieurYannick/omnitrade-hub/releases` (le tag le plus récent).
- Prix, liens du bot Telegram et informations de broker → ajuster dans
  `_src/guides.mjs` puis régénérer si besoin.