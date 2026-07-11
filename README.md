# CROUS Discord Monitor V2

Le dépôt est prêt à être importé directement à la racine d’un repository GitHub.

Structure attendue :

```text
.github/
  workflows/
    crous-monitor.yml
data/
  seen_listings.json
monitor.py
requirements.txt
README.md
.gitignore
```

## Installation

1. Crée un dépôt GitHub privé et vide.
2. Décompresse le ZIP.
3. Importe le contenu du dossier, pas le dossier lui-même.
4. Dans GitHub : Settings > Secrets and variables > Actions.
5. Ajoute `DISCORD_WEBHOOK_URL`.
6. Ajoute `CROUS_SEARCH_URLS`, avec une URL CROUS par ligne.
7. Va dans Actions > Surveillance logements CROUS > Run workflow.

Le premier lancement mémorise les annonces déjà présentes. Les suivants alertent seulement sur les nouvelles annonces.
