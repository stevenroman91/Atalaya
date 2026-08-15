# Atalaya — contexte projet

Veille sécuritaire Amérique latine : collecte quotidienne de presse,
corroboration, alertes et briefings multilingues.

## Destinataire

**Le produit est destiné à être utilisé par GEOS**, société de gestion des
risques. Les lecteurs sont donc des professionnels de la sûreté, pas des
utilisateurs occasionnels. Trois conséquences qui priment sur le confort de
développement :

- **Un faux positif coûte plus cher qu'un manque.** Un analyste qui voit une
  fois un séisme colombien étiqueté Mexique cesse de faire confiance à
  l'outil, et cette confiance ne revient pas. En cas d'arbitrage entre bruit
  et silence, choisir le silence et le signaler.
- **L'outil assiste l'analyste, il ne le remplace pas.** D'où les résumés
  strictement extractifs et les sources toujours citées : le lecteur doit
  pouvoir remonter à la source en un clic et vérifier. Une reformulation
  fluide mais invérifiable est un défaut, pas une amélioration.
- **Les recommandations sont des consignes de sûreté.** Elles peuvent être
  transmises à des personnes sur le terrain. Elles relèvent de la doctrine du
  client : à faire valider par GEOS, jamais à inventer ni à durcir seul.

## Règles non négociables

- **Résumés strictement extractifs** : phrases découpées dans les sources,
  jamais générées. Seules les traductions fr/en/pt passent par un modèle.
- **Deux sources indépendantes** pour publier une alerte. Source unique +
  gravité extrême → « à confirmer », jamais diffusé comme alerte.
- **Le robot s'identifie** (UA « AtalayaBot/1.0 »), respecte robots.txt et
  les délais de politesse. Ne jamais se déguiser en navigateur ni contourner
  une protection anti-bot, même si un site bloque.
- **Jamais d'URL inventée.** Un flux n'entre en base que s'il a été vérifié
  par une requête qui renvoie un flux parsable, ou s'il est confirmé par
  l'opérateur.
- **Le fait doit se produire dans le périmètre.** L'attribution vient du lieu
  de l'événement, pas du pays du média qui le publie.
- Accès sur invitation uniquement, pas d'inscription publique.

## Environnement

- Déploiement Railway : service web + 3 crons (`railway/*.json`), depuis
  `main`. Chaque push sur `main` redéploie et interrompt une collecte en cours.
- PostgreSQL, Alembic pour les migrations.
- CSP `script-src 'self'` : tout le JS dans `static/app.js`, jamais en ligne.
- Tests : le paquet n'est pas installé en editable — réinstaller avant de
  lancer pytest (`pip install --force-reinstall --no-deps .`).
