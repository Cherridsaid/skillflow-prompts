# skillflow-prompts

[English](README.md)

skillflow-prompts fait tourner le contrôleur SkillFlow à partir d'un **prompt** au lieu
d'un skill, pour que ChatGPT (mode Travailler) compile un prompt en contrat, contrôle
une réponse contre ce contrat et rende un reçu signé.

**État : en cours.** Le noyau SkillFlow vient de skillflow-mcp (commit `c26ff68`), renommé
en `skillflow_prompts`. La couche « prompt » et le lanceur ChatGPT existent et sont testés :
`skillflow-prompts chatgpt` compile un prompt en contrat, contrôle une réponse contre ce
contrat, nomme les obligations qu'il rejette pour que seules celles-là soient corrigées, et
rend un reçu signé que `verify` recalcule au lieu de le relire. Une commande est un
processus : rien ne reste en marche entre deux commandes. Ne corriger que ce qui a été
rejeté est une consigne donnée au modèle, pas une contrainte que l'outil impose.

Ce qui manque encore : les vérificateurs supplémentaires, la couverture déclarée et le
kernel, la documentation complète, et un essai réel dans ChatGPT. Le lanceur détient tous
les jetons de ce bac à sable, donc son approbation est **fictive** et le reçu comme `verify`
le disent : ce qui est contrôlé mécaniquement est marqué `VERIFIE`, tout le reste reste
`NON_VERIFIE`. Ce README ne décrit aucune fonction qui ne soit pas encore testée.

Pour le piloter depuis une conversation ChatGPT, voir [CHATGPT.md](CHATGPT.md).

## Développement local

Python 3.12 et [uv](https://docs.astral.sh/uv/) sont requis.

```console
uv sync --locked
uv run python -m pytest
uv run skillflow-prompts --help
```

## Licence

Apache-2.0. Dérivé de skillflow-mcp, même auteur.
