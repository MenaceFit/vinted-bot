# 🛍️ Vinted Monitor

Bot de surveillance Vinted asynchrone : scan multi-mots-clés en parallèle,
anti-doublons fiable, notifications **Telegram** (nouveau) et Discord,
dashboard temps réel, base SQLite. Refonte complète de l'ancien `vinted_bot_v3`.

## 🚀 Installation

```bash
pip install -r requirements.txt
python app.py
```

Ou double-clic sur `start_windows.bat` / `./start_macos_linux.sh`.

Prérequis : Python 3.11+ avec Tkinter (inclus par défaut sur Windows/macOS ;
sur Linux, `sudo apt install python3-tk` si besoin).

## 📱 Configurer Telegram

1. Ouvre une conversation avec **@BotFather** sur Telegram, envoie `/newbot`
   et suis les instructions → tu obtiens un **Bot Token**.
2. Démarre une conversation avec ton nouveau bot (`/start`).
3. Récupère ton **Chat ID** : parle à **@userinfobot**, ou ouvre
   `https://api.telegram.org/bot<TOKEN>/getUpdates` après avoir envoyé un
   message au bot.
4. Dans l'appli → onglet **📱 Telegram** → colle le Token et le Chat ID →
   **🧪 Tester la connexion** → **🟢 Activer Telegram**.

Le Bot Token et le Chat ID sont stockés dans `.env` (jamais dans
`config.json`, jamais dans les logs — voir [Sécurité](#-sécurité)).

## 🎯 Utilisation

1. Onglet **🔎 Scanner** : ajoute tes mots-clés (`Nike`, `Jordan`, `Supreme`...),
   coche les marchés (FR/PL/UK), règle l'intervalle (5s minimum).
2. **🟢 DÉMARRER**. Le premier cycle de chaque mot-clé est un *warmup* : il
   mémorise les annonces déjà en ligne sans notifier, pour éviter un déluge
   de notifications sur des annonces existantes.
3. Dès le cycle suivant, toute nouvelle annonce part vers Telegram (et
   Discord si configuré) et apparaît dans **📦 Annonces**.
4. **📊 Dashboard** : statistiques en direct (annonces/heure, latence,
   scanners actifs, statut Telegram) et détail de performance
   (API / Parsing / Processing / Total, latence min/moy/max, erreurs, retries).

Ajouter ou retirer un mot-clé pendant que le scan tourne prend effet
immédiatement (pas besoin d'arrêter/redémarrer) : chaque mot-clé est une
tâche asyncio indépendante que le scanner ajoute ou annule à la volée.

## 🏗️ Architecture

```
vinted-bot/
├── app.py                  # Point d'entrée — assemble tout
├── config.py                # config.json (réglages) + .env (secrets)
│
├── core/
│   ├── scanner.py            # Moteur de scan — 1 Task asyncio par mot-clé
│   └── performance.py        # Monitoring temps réel (latence, erreurs, retries)
│
├── services/
│   ├── vinted.py              # Scraper aiohttp (sessions persistantes, retry)
│   ├── telegram.py            # Notifications Telegram (queue, retry, fallback image→texte)
│   └── discord.py             # Notifications Discord (conservé, historique)
│
├── database/
│   └── database.py           # SQLite : dédup O(1) + historique des annonces
│
├── gui/
│   ├── app_window.py          # Fenêtre principale, sidebar
│   ├── dashboard_view.py, scanner_view.py, listings_view.py,
│   │   telegram_view.py, settings_view.py
│   └── theme.py
│
├── utils/
│   ├── logger.py              # Logging + redaction automatique des secrets
│   └── formatting.py          # Construction des messages Telegram
│
├── tests/                    # pytest — voir "Tests"
├── benchmark.py               # Mesures avant/après reproductibles localement
└── data/                     # SQLite (créé au démarrage, ignoré par git)
```

```
GUI (thread Tkinter)
  │  after(0, …) ──────────────────────► mutations UI thread-safe
  ▼
Scanner (thread asyncio dédié)
  ├─ une Task par mot-clé, indépendante (erreur/backoff isolés)
  ├─ Database (SQLite : dédup + historique, écriture en arrière-plan)
  ├─ Telegram / Discord (queues async, ne bloquent jamais le scan)
  └─ Logging
```

## ⚙️ Configuration

`config.json` (réglages, non sensible) : intervalle, marchés, mots-clés,
filtres de prix, URL personnalisée, options Telegram (envoyer images, son).

`.env` (secrets, jamais commité — voir `.env.example`) :
```
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
DISCORD_WEBHOOK_URL=
```

Une ancienne config `v3` (webhook Discord en clair dans `config.json`) est
migrée automatiquement vers `.env` au premier lancement.

## 🔐 Sécurité

- Bot Token, Chat ID et webhook Discord vivent uniquement dans `.env`
  (`.gitignore` l'exclut du dépôt).
- `utils/logger.py` ajoute un filtre qui masque automatiquement toute valeur
  secrète connue si elle apparaissait par erreur dans un message de log
  (défense en profondeur, en plus du soin apporté à ne jamais les logger).
- Les champs Bot Token / Chat ID / Webhook sont masqués dans l'interface
  (bouton 👁 pour les révéler ponctuellement).

## 🧪 Tests

```bash
pip install -r requirements-dev.txt
pytest
```

29 tests, aucun accès réseau réel (HTTP mocké) : détection de nouvelle
annonce, doublon (y compris entre mots-clés différents), persistance de la
dédup après redémarrage, écriture en base, envoi Telegram (succès, échec,
repli image→texte, erreur réseau + retry, rate-limit 429), test de connexion
Telegram, plusieurs mots-clés surveillés en parallèle, arrêt du scanner
(aucune tâche ne doit survivre — régression directe du bug ci-dessous),
redémarrage du scanner.

## 📊 Benchmark avant/après

```bash
python benchmark.py
```

Mesures réelles, exécutées localement (voir le script pour le détail de
chaque protocole). Cet environnement de développement n'a pas de sortie
réseau vers Vinted/Telegram — impossible d'y mesurer une latence de scan
réelle sans inventer un chiffre. Utilise plutôt le dashboard intégré à
l'application (section ⚡ PERFORMANCE) une fois le bot lancé avec ta propre
connexion : il calcule ces temps en direct, sur du trafic réel.

| Mesure | Avant | Après |
| --- | ---: | ---: |
| Tâches de scan fantômes après 5 cycles démarrer/arrêter (bug critique, voir Audit) | 5 (croissance illimitée) | 0 |
| Itérations de boucle de scan cumulées sur ces 5 cycles | 69 | 10 |
| Écriture SQLite, 500 lignes (dédup) | 0,54 s (sans WAL — config d'origine) | 0,12 s (WAL) — ×4,4 |
| Rendu de 500 annonces dans l'UI | 0,035 s / **1501 widgets** créés | 0,005 s / **1 widget** (Treeview) |
| Débit dédup mémoire (référence, inchangé) | 1,76M ops/s (1ère passe) · 5,68M ops/s (doublons) | — capacité déjà O(1) avant et après |

Les micro-benchmarks I/O (SQLite) varient de quelques dixièmes d'un run à
l'autre selon la machine — relance `python benchmark.py` pour vérifier.

## 🩺 Audit technique — problèmes trouvés et corrigés

Le code existant (`vinted_bot_v3`) partait déjà d'une base async raisonnable
(aiohttp, sessions persistantes, dédup mémoire O(1)). Cette refonte a
conservé cette base et corrigé :

1. **Bug critique — double tâche de scan par mot-clé** (`core/scheduler.py`).
   `start()` planifiait `_keyword_loop()` deux fois par mot-clé : une fois via
   `run_coroutine_threadsafe(...)` jamais trackée, une fois via une Task
   trackée. `stop()` n'annulait que la seconde — la première tournait à
   l'infini. À chaque cycle démarrer/arrêter, une boucle fantôme de plus
   s'accumulait, multipliant les requêtes vers Vinted sans limite (voir
   benchmark ci-dessus). **Corrigé** dans `core/scanner.py` : la création des
   Tasks se fait une seule fois, entièrement dans le thread de la loop.
2. **Bug — "Scan maintenant" ne notifiait jamais** tant que le scan auto
   n'avait pas tourné une fois (condition de warmup toujours vraie sur un
   dict vide). **Corrigé** : état de warmup dédié au scan manuel.
3. **Aucune intégration Telegram** — ajoutée intégralement
   (`services/telegram.py`, vue dédiée, retry des envois échoués).
4. **Pas d'historique des annonces en base** — seule la dédup était
   persistée. Ajout de la table `listings` (titre/prix/url/image/statuts
   d'envoi) avec index sur `detected_at`, `keyword`, `telegram_status`.
5. **Code mort** — `MarketScraper._last_seen_id` était calculé à chaque scan
   mais jamais lu. Supprimé.
6. **Goulot d'étranglement de connexions** — chaque marché limitait ses
   requêtes à `limit_per_host=3`, partagé par tous les mots-clés surveillés :
   avec 6 mots-clés actifs, 3 requêtes seulement partaient en parallèle.
   Relevé à 20.
7. **Widgets Tkinter illimités** — le flux d'annonces créait un nouveau
   `Frame` par annonce sans jamais en supprimer (fuite mémoire/lenteur sur
   une session longue). Remplacé par un `ttk.Treeview` borné à 500 lignes
   affichées (l'historique complet reste en base).
8. **Pas de monitoring de performance structuré** — ajouté
   (`core/performance.py` : latence API/parsing/traitement, min/moy/max,
   erreurs, retries, exposé en direct dans le Dashboard).
9. **Secrets en clair** — le webhook Discord vivait dans `config.json`.
   Déplacé vers `.env`, migration automatique de l'ancienne config.
10. **`Pillow`** était listée dans `requirements.txt` sans être utilisée nulle
    part. Maintenant utilisée pour les miniatures dans le tableau d'annonces.

## ❓ Dépannage

**0 annonce reçue** → session Vinted expirée, réinitialisation automatique
au prochain scan.
**Telegram : "chat not found"** → le Chat ID est faux, ou tu n'as jamais
envoyé `/start` au bot.
**Telegram : image jamais envoyée** → l'URL d'image a pu échouer côté
Telegram (taille, format) ; le texte part quand même automatiquement, c'est
le comportement voulu.
**Trop de notifications au démarrage** → vérifie que "Warmup au premier
scan" est coché (Settings).
**Scan lent** → vérifie ta connexion, l'intervalle configuré (5s minimum),
et le nombre de mots-clés/marchés actifs simultanément.
