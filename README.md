# 🗼 Atalaya — Veille sécuritaire automatisée pour l'Amérique latine

Service multi-utilisateurs de veille sécuritaire destiné aux agents de sécurité
des délégations de l'UE en Amérique latine. Remplace le processus manuel de
recherches Google Actualités par un pipeline automatisé :

- **Veille quotidienne** : alertes des dernières 24 h (10 pays, granularité
  quartier pour le Mexique), dashboard web avec carte, timeline, filtres et
  export PDF.
- **Veille hebdomadaire → synthèse mensuelle** : collecte par pays × thème
  (politique, économie, sanitaire, naturel, sécurité) et synthèse mensuelle
  avec table d'incidents, export DOCX.

L'espagnol est la langue canonique du pipeline ; chaque utilisateur consulte
l'outil dans sa langue (es/fr/en/pt) via une couche de traduction en cache.

---

## Sommaire

1. [Architecture](#architecture)
2. [Installation locale](#installation-locale)
3. [Déploiement Railway](#déploiement-railway)
4. [Déploiement serveur interne + TLS](#déploiement-serveur-interne--tls)
5. [Création du premier admin & invitations](#création-du-premier-admin--invitations)
6. [Configuration sans toucher au code](#configuration-sans-toucher-au-code)
7. [Planification cron](#planification-cron)
8. [Traductions](#traductions)
9. [Garanties anti-hallucination](#garanties-anti-hallucination)
10. [Tests & démo locale](#tests--démo-locale)

---

## Architecture

```
[cron] → Collecteur (Google News RSS par zone×mot-clé + RSS directs whitelist)
       → Extracteur (trafilatura, texte intégral + date réelle)
       → Dédoublonnage/Clustering (similarité titres + entités)
       → Scoring (récurrence ≥2 sources indépendantes, gravité, géo, fraîcheur)
       → Classification (ALERTA/NOTA, catégorie, niveau)
       → Rédaction extractive (ES) + recommandations
       → Traductions fr/en/pt (cache versionné)
       → Base (SQLite dev / PostgreSQL prod, SQLAlchemy + Alembic)
       → Dashboard FastAPI (comptes par invitation, préférences par utilisateur)
```

Détails notables :

- **Résumés strictement extractifs** : chaque phrase d'un résumé existe
  littéralement dans un article stocké en base (voir §Anti-hallucination).
- **Flux RSS jamais devinés** : les URL de flux sont soit déclarées dans
  `config/sources.yaml`, soit autodécouvertes (`<link rel="alternate">`) au
  runtime et persistées. En attendant, chaque source est couverte via Google
  News.
- **File « à confirmer »** : un événement à source unique mais de gravité
  extrême est visible dans le dashboard avec un avertissement explicite,
  jamais publié comme alerte.
- **Auth isolée** (`src/atalaya/web/auth.py`) : Argon2id, sessions révocables
  en base, CSRF, rate limiting IP + verrouillage progressif par compte —
  prête à être remplacée par un SSO.

## Installation locale

Prérequis : Python 3.11+. Pour l'export PDF (facultatif — dégradation
automatique en HTML) : `apt install libpango-1.0-0 libpangocairo-1.0-0
libgdk-pixbuf-2.0-0` puis `pip install weasyprint`.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
atalaya init-db                      # SQLite dans ./data/atalaya.db par défaut

export ATALAYA_ADMIN_EMAIL=vous@exemple.org
export ATALAYA_ADMIN_PASSWORD='un-mot-de-passe-long'
atalaya create-admin

atalaya collect-daily                # première collecte réelle (~30-60 min, réseau requis)
atalaya serve                        # http://localhost:8000
```

Variables d'environnement principales :

| Variable | Rôle | Défaut |
|---|---|---|
| `DATABASE_URL` | PostgreSQL (`postgresql://…`) ou SQLite | `sqlite:///data/atalaya.db` |
| `ATALAYA_ADMIN_EMAIL` / `ATALAYA_ADMIN_PASSWORD` | admin initial | — |
| `ATALAYA_BASE_URL` | URL publique (liens d'invitation/reset) | `http://localhost:8000` |
| `ANTHROPIC_API_KEY` | active les traductions + rédaction LLM des synthèses | — |
| `ATALAYA_TRANSLATE` | `claude` / `none` (forcer) | auto |
| `ATALAYA_TRANSLATE_MODEL` | modèle de traduction | `claude-opus-5` |
| `ATALAYA_CONFIG_DIR` | répertoire des YAML | `./config` |
| `ATALAYA_HSTS` | `0` pour désactiver l'en-tête HSTS (dev) | `1` |
| `ATALAYA_INSECURE_COOKIES` | `1` = cookies sans `Secure` (dev HTTP) | — |

## Déploiement Railway

Le repo contient tout le nécessaire : `Dockerfile` (une seule image pour tous
les services), `railway.json` (service web) et `railway/cron-*.json`.

1. **Créer le projet** : *New Project → Deploy from GitHub repo* → ce dépôt.
   Le service détecte `railway.json` et démarre le web (`scripts/start-web.sh`
   = migrations → admin → uvicorn, healthcheck `/health`).
2. **Ajouter PostgreSQL** : *Create → Database → PostgreSQL*. Dans le service
   web, ajouter la variable `DATABASE_URL` = `${{Postgres.DATABASE_URL}}`
   (référence de variable Railway). Faire de même pour chaque service cron.
3. **Variables du service web** : `ATALAYA_ADMIN_EMAIL`,
   `ATALAYA_ADMIN_PASSWORD`, `ATALAYA_BASE_URL` (l'URL publique Railway, ex.
   `https://atalaya-production.up.railway.app`), et `ANTHROPIC_API_KEY` si
   traductions.
4. **Services cron** — **3 services Railway supplémentaires**, créés depuis le
   même dépôt (*New → GitHub Repo* → ce dépôt, une fois par service). Le
   fichier `railway/cron-*.json` ne crée aucun service à lui seul : sans ces
   trois services, rien ne se déclenche jamais et seules les collectes
   lancées à la main depuis `/admin` tournent. Pour chacun,
   *Settings → Config-as-code file* :
   - `railway/cron-daily.json` — 9:00, 12:00 et 18:00 UTC = **03:00, 06:00 et
     12:00 Mexico** (America/Mexico_City est UTC-6 toute l'année depuis 2022)
   - `railway/cron-weekly.json` — vendredi 13:00 UTC = 07:00 Mexico
   - `railway/cron-monthly.json` — le 1er, 13:00 UTC = 07:00 Mexico
   Chaque service cron a besoin de `DATABASE_URL`, et le quotidien de
   `ANTHROPIC_API_KEY` — il porte les traductions **et le classificateur de
   pertinence**. Sans la clé, la collecte tourne quand même mais sans
   classifier : le panneau se remplit alors de faits non securitaires que les
   collectes manuelles, elles, écartaient. Railway lance le conteneur à
   l'horaire, la commande s'exécute puis le conteneur s'arrête
   (`restartPolicyType: NEVER`).

   Vérification : la table **Runs** de `/admin` affiche l'origine de chaque
   collecte. Tant qu'on n'y voit que `manual`, les crons ne tournent pas.
5. **Domaine** : Railway fournit HTTPS automatiquement (les cookies `Secure`
   et HSTS fonctionnent sans configuration).

> Récurrence des crons Railway : la granularité minimale est la minute et un
> job qui tourne encore à l'horaire suivant n'est pas relancé en double —
> compatible avec l'idempotence des jobs Atalaya.

## Déploiement serveur interne + TLS

Sans dépendance cloud obligatoire :

```bash
# systemd (extrait) — /etc/systemd/system/atalaya.service
[Service]
Environment=DATABASE_URL=postgresql://atalaya:***@localhost/atalaya
ExecStart=/opt/atalaya/.venv/bin/atalaya serve --host 127.0.0.1 --port 8000
```

Reverse proxy TLS (Caddy — obtient et renouvelle les certificats seul) :

```
atalaya.exemple.org {
    reverse_proxy 127.0.0.1:8000
}
```

ou nginx : `proxy_pass http://127.0.0.1:8000;` + certbot. L'app émet déjà
CSP, HSTS, X-Frame-Options ; les cookies sont `httpOnly`/`Secure`/`SameSite`.

Cron classique :

```cron
0 6 * * *  atalaya collect-daily     # 06:00 heure locale de Mexico
0 7 * * 5  atalaya collect-weekly
0 7 1 * *  atalaya monthly
```

## Création du premier admin & invitations

1. `ATALAYA_ADMIN_EMAIL` + `ATALAYA_ADMIN_PASSWORD` puis `atalaya create-admin`
   (fait automatiquement au boot sur Railway).
2. L'admin se connecte → **Administración** → *Invitar usuario* (e-mail +
   rôle). L'écran affiche le **lien d'invitation à usage unique** (validité
   72 h, configurable) à transmettre par canal sûr.
3. L'invité définit son mot de passe (≥10 caractères), passe l'onboarding
   (pays suivis, zones du Mexique, langue, fuseau) et voit son dashboard
   filtré et traduit en conséquence.
4. Réinitialisation de mot de passe : lien à usage unique via « ¿Olvidaste tu
   contraseña? ». Sans SMTP configuré, le lien apparaît dans les logs du
   serveur pour transmission par l'admin.
5. Restriction de domaines e-mail : `config/auth.yaml` →
   `invitations.allowed_email_domains: ["eeas.europa.eu"]`.

Rôles : `admin` (invitations, comptes, santé de la collecte) / `analista`
(consultation, filtres, exports).

## Configuration sans toucher au code

| Fichier | Contenu | Pour ajouter… |
|---|---|---|
| `config/sources.yaml` | liste blanche (domaine, pays d'origine, pays couverts, langue, type `independiente`/`estatal`/`internacional`, RSS, section régionale) | **une source** : ajouter un bloc sous `sources:` ; type `estatal` ⇒ tag « medio estatal — contrastar » automatique et jamais seule à fonder une alerte |
| `config/zones.yaml` | pays, zones/quartiers, termes de requête, coordonnées carte, paramètres Google News | **une ville/zone** : ajouter une entrée sous `zones:` du pays avec `id`, `name`, `query_terms`, `geo` |
| `config/keywords.yaml` | mots-clés quotidiens es/pt, thèmes hebdo, signaux de gravité/catégorie/niveau | **un mot-clé** : l'ajouter à la liste voulue |
| `config/recommendations.yaml` | gabarits de recommandations par catégorie | |
| `config/auth.yaml` | TTL invitations/reset, domaines autorisés, verrouillage, sessions | |
| `config/schedule.yaml` | fenêtres de collecte, chevauchement, délais de politesse, User-Agent, seuil d'alerte sources en échec | |
| `locales/{es,fr,en,pt}.json` | interface | **une langue** : créer `locales/xx.json`, ajouter `xx` à `SUPPORTED_LANGS` (`src/atalaya/config.py`) — seule modification de code requise |

Les YAML sont relus à chaque démarrage de job/app (redéployer ou relancer
après modification).

## Planification cron

- Quotidien 06:00 Mexico, fenêtre 24 h + 2 h de chevauchement (rien n'est
  perdu entre deux runs ; l'idempotence évite les doublons).
- Hebdo vendredi 07:00 ; mensuel le 1er à 07:00 (synthèse du mois écoulé).
- Journalisation complète par run (articles collectés/rejetés **avec motif**)
  visible dans **Administración → Runs** ; une source en échec 3 jours de
  suite est surlignée dans l'état de santé.

## Traductions

- Le pipeline stocke tout en **espagnol** (source de vérité unique).
- Avec `ANTHROPIC_API_KEY`, la fin de chaque run quotidien génère les
  versions fr/en/pt pour les langues **effectivement utilisées par au moins
  un compte**, en cache versionné : si le résumé canonique change, la
  traduction est régénérée. Consigne stricte : traduction fidèle, chiffres et
  citations intacts ; **les URL et titres d'articles sources ne sont jamais
  traduits**.
- Sans clé API : l'interface et les recommandations restent traduites
  (fichiers i18n + gabarits), les résumés s'affichent en espagnol avec la
  mention « résumé disponible en espagnol uniquement ».

## Garanties anti-hallucination

1. **Aucun contenu inventé** — les résumés sont *extractifs* : chaque phrase
   provient telle quelle du texte d'un article récupéré et stocké avec son
   timestamp de fetch. Un article sans texte intégral n'est jamais résumé
   (« título solamente »).
2. **Traçabilité totale** — chaque événement référence les IDs de ses
   articles sources (`event_articles`) ; le détail du scoring est stocké
   (`score_detail`).
3. **Jamais d'URL reconstruite** — seules les URL résolues à la collecte
   (redirections Google News décodées ou suivies) sont stockées ; un lien
   irrésoluble est rejeté.
4. **Vérification des dates** — la date de publication *réelle* extraite de
   l'article est re-contrôlée après extraction ; les vieux articles recyclés
   par les flux sont rejetés (motif journalisé).
5. **Détection du contenu automatisé** — heuristiques anti-fermes de contenu
   pour les domaines hors liste blanche ; un domaine hors liste ne peut que
   corroborer, jamais fonder une alerte.
6. **Pas d'extrapolation** — si les bilans chiffrés divergent entre sources,
   le résumé expose la fourchette en attribuant chaque chiffre à sa source.
7. **Séparation faits / recommandations** — les recommandations (gabarits
   par catégorie) sont affichées dans un bloc distinct clairement identifié.
8. **Pas de remplissage** — « Sin incidentes relevantes en las últimas 24 h »
   quand il n'y a rien.

Deux sources d'État ne comptent jamais comme indépendantes entre elles ; tout
contenu issu d'un média d'État porte le tag « medio estatal — contrastar ».

## Tests & démo locale

```bash
pip install -e '.[dev]'
pytest                      # 16 tests
```

Couverture exigée par le cahier des charges : dédoublonnage, scoring
(récurrence ≥ 2 indépendantes, gravité, fenêtre 24 h, file « à confirmer »),
classification, divergence de bilans, auth (invitation à usage unique,
verrouillage progressif, rate limiting IP, reset), filtrage par préférences,
parcours multi-utilisateurs complet, idempotence des trois jobs.

Les tests E2E utilisent un **serveur de fixtures HTTP local**
(`tests/fixture_server.py`) qui reproduit Google News RSS (liens encodés
compris), robots.txt et pages d'articles : le pipeline entier s'exécute par
HTTP réel. Pour une démo manuelle sans réseau externe :

```bash
python tests/fixture_server.py            # note l'URL affichée
atalaya collect-daily --country GT --fixture-base http://127.0.0.1:PORT
atalaya serve
```

En environnement connecté, `atalaya collect-daily` effectue la collecte
réelle sur les médias de la liste blanche.
