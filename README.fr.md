# skillflow-prompts

[English](README.md)

skillflow-prompts fait tourner le contrôleur SkillFlow à partir d'un **prompt** au lieu
d'un skill, pour que ChatGPT (mode Travailler) compile un prompt en contrat, contrôle
une réponse contre ce contrat et rende un reçu signé.

**État : en construction.** Ce dépôt contient pour l'instant le noyau SkillFlow importé
de skillflow-mcp (commit `c26ff68`) et renommé en `skillflow_prompts`. La couche
« prompt » et le lanceur ChatGPT ne sont pas encore disponibles. Ce README ne décrit
aucune fonction qui ne soit pas encore testée.

## Développement local

Python 3.12 et [uv](https://docs.astral.sh/uv/) sont requis.

```console
uv sync --locked
uv run python -m pytest
uv run skillflow-prompts --help
```

## Licence

Apache-2.0. Dérivé de skillflow-mcp, même auteur.
