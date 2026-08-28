# ✈️ ACTIVATION AUTOMATIQUE — Guide d'installation (côté vendeur)

Ce dossier transforme l'app en logiciel « à codes d'achat » : le client achète
un code (`OTH-XXXXXXXX-XXXXXXXX`), le colle dans l'app, et l'app déverrouille
la licence TOUTE SEULE, sans copier-coller de clé. C'est la version moderne du
système de licence actuel (précisément : l'app génère la clé de licence
signée exactement comme tes outils python, mais côté serveur).

**Ce dossier contient :**

| Fichier                     | Rôle                                                     |
|-----------------------------|----------------------------------------------------------|
| `schema.sql`                | La table SQL des codes d'achat (à lancer 1× dans Supabase) |
| `supabase/functions/`       | Les 2 fonctions serveur (activation + outil vendeur)    |
| `admin.py`                  | Ton outil du vendeur : créer/annuler/consulter les codes |
| `creer_cles.py`             | Génère la paire de clés de licence (si tu n'en as pas)  |
| `tests/`                    | Vérifie que les clés émises marchent avec ton moteur    |

---

## ÉTAPE 1 — Créer un projet Supabase (gratuit, 5 min)

1. Va sur https://supabase.com → **Start your project** (connexion GitHub).
2. Crée une organisation, puis **New Project** : nom `omnitrade`, mot de passe
   de base de données (WhatsApp-toi le mot de passe), région **West EU** (proche).
3. Note le **Project URL** (ex : `https://abcdefgh.supabase.co`) et la
   **anon public key** (Dashboard → Settings → API).

## ÉTAPE 2 — Créer la table

Dans le dashboard : **SQL Editor** → *New query* → colle tout le contenu de
`activation/schema.sql` → **Run**.

## ÉTAPE 3 — Installer l'outil de déploiement (une seule fois, sur ton Mac)

Ouvre le Terminal et colle :

```bash
brew install supabase/tap/supabase   # si pas déjà fait
```

Puis, dans le dossier du projet :

```bash
cd "/Users/macbookdeeliysha/Documents/Default Project/OMNITRADE/OmniTradeHub-macOS v8.87/activation"
supabase login
supabase link --project-ref <REF>     # REF = sous-domaine de ton URL (abcdefgh)
```

## ÉTAPE 4 — Les 2 secrets (clés privées)

`supabase secrets set OTH_ADMIN_KEY` — choisis un mot de passe vendeur LONG et
aléatoire (ex : génère un avec ta tête ; il sert à appeler l'outil vendeur).

`supabase secrets set OTH_PRIV_KEY` — la clé PRIVÉE de licence :
- Si tu avais déjà vendu des licences (clé existante) : c'est LE MÊME hex
  secret que celui qui correspond au `public_key.txt` actuel de l'app.
- Sinon : `python3 creer_cles.py` puis copie le SK_HEX → **public_key.txt de
  l'app DOIT être remplacé** par la clé publique correspondante.

## ÉTAPE 5 — Déployer les 2 fonctions

```bash
supabase functions deploy oth-activate --no-verify-jwt
supabase functions deploy oth-issue     --no-verify-jwt
```

## ÉTAPE 6 — Configurer ton outil vendeur

Crée le fichier `activation/.adm.json` :

```json
{
  "function_url": "https://abcdefgh.supabase.co/functions/v1/oth-issue",
  "admin_key":   "<le mot de passe OTH_ADMIN_KEY>"
}
```

Tout est prêt. Teste :

```bash
python3 admin.py create m12                    # → un code s'affiche
python3 admin.py list                          # → tu le vois dans la liste
python3 admin.py get OTH-XXXXXXXX-XXXXXXXX     # → détail, clé émise comprise
```

Envoie le code au client. Dans l'app, il colle le code → l'app appelle
`oth-activate` → reçoit sa clé signée → la valide avec le moteur local.

---

## Sécurité — ce qu'il faut comprendre (relis ce paragraphe !)

- **Le client ne peut RIEN inventer** : impossible de fabriquer une clé
  (signature Ed25519 vérifiée localement) ni de prolonger sa date (échéance
  signée). Chaque achat = nb d'ordinateurs limité (`--max`), compté côté
  serveur lors de l'activation.
- **Depuis la v9, plus aucun blocage par code machine** : si l'empreinte
  change (mise à jour Windows, réinstallation), la licence reste active.
  Le code machine n'est plus qu'un compteur d'ordinateurs côté serveur.
- **La table est verrouillée** (RLS) : seul le serveur y touche.
- **L'outil vendeur exige `OTH_ADMIN_KEY`** : seuls toi et le serveur de
  paiement (plus tard) peuvent créer des codes.
- Anciennement : tu créais la clé toi-même, le client la copiait. Maintenant
  c'est la même clé, mais produite par le serveur à la volée — et c'est
  l'app qui fait le copier-coller.

## ÉTAPE 7 (à venir) — Le paiement

`oth-issue` accepte déjà `create` pour un futur `stripe-event` : quand Stripe
confirmera le paiement, il pourra créer le code automatiquement. Cette étape
sera faite au moment de brancher le paiement (choix : Stripe / Flutterwave CV
selon la région).