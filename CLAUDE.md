# Atalaya — contexte projet

Veille sécuritaire Amérique latine : collecte quotidienne de presse,
corroboration, alertes et briefings multilingues.

## Destinataire

**Atalaya est l'outil de veille des analystes GEOS**, société de gestion des
risques. Ce n'est pas un produit de diffusion : les analystes filtrent en
aval et remettent en page à leur façon s'ils le souhaitent. Atalaya leur
fournit la matière première et le premier tri, pas le livrable final.

Ce que ça implique, par ordre d'importance :

- **Ne jamais écarter en silence.** L'analyste est là pour trancher les cas
  douteux ; ce qui est supprimé sans trace ne lui parvient jamais et ne peut
  pas être rattrapé. Face à un doute, marquer et laisser passer plutôt que
  jeter. Un événement mal étiqueté qu'il corrige coûte moins cher qu'un
  événement réel qu'il n'a jamais vu.
- **Trancher n'est pas notre rôle, exposer l'incertitude l'est.** Statut
  « à confirmer », sources visibles, motif du classement : l'outil montre son
  raisonnement pour que l'analyste puisse le contredire vite.
- **Tout doit être vérifiable en un clic.** D'où les résumés strictement
  extractifs et les sources toujours citées. Une reformulation fluide mais
  invérifiable fait perdre du temps à l'analyste au lieu de lui en faire
  gagner : c'est un défaut, pas une amélioration.
- **Optimiser le temps de tri.** Filtres, recherche, tri, dédoublonnage :
  l'analyste passe la journée dans l'outil, chaque friction se paie.
- **Les recommandations sont un point de départ, pas une consigne validée.**
  Elles relèvent de la doctrine GEOS. À faire relire par eux ; ne jamais en
  inventer ni en durcir seul.

Ce qui n'est donc *pas* l'objectif : charte graphique client, livrables
brandés, cloisonnement multi-clients. La mise en page finale leur appartient.

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
