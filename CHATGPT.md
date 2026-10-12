# Consigne à coller dans ChatGPT (mode Travailler) — variante zip

Tu vas faire tourner un outil local, `skillflow-prompts`, qui transforme un prompt en
contrat, contrôle ta réponse contre ce contrat, n'accepte des corrections que sur les
obligations rejetées, et rend un reçu signé. Tu l'exécutes dans ton bac à sable. Suis
exactement les étapes ci-dessous et montre-moi la sortie de chaque commande.

## 1. Installer, une seule fois

Un zip du dépôt est joint à ce message. Décompresse-le, puis installe les dépendances
verrouillées. Remplace `<DOSSIER>` par le chemin **absolu** du dossier obtenu.

```bash
cd <DOSSIER>
uv sync --locked
uv run --no-sync python -m skillflow_prompts chatgpt init --root <DOSSIER>/travail
```

`init` crée l'état sur disque. Si `uv sync --locked` échoue, **arrête-toi** et donne-moi
l'erreur telle quelle : l'outil ne peut pas démarrer sans ses dépendances, et je ne veux
pas d'un contournement qui installerait autre chose que le verrou.

## 2. La règle du bac à sable

**Une commande = un processus.** Chaque commande ouvre l'autorité, lit l'état sur le
disque, écrit, et se termine. Ne lance jamais de serveur, de démon, ni de tâche en arrière
plan : un processus ne survit pas d'une commande à l'autre, les fichiers oui.

Trois conséquences :

- `--root` doit être un chemin **absolu**, le même à chaque commande.
- Chaque sortie est **exactement un objet JSON**. Lis le champ `summary`, il dit l'étape
  suivante.
- Code de sortie 0 = fait, 1 = refus motivé, 2 = mauvais usage, 3 = défaut interne.

## 3. Pour chaque prompt que je te donne

Écris mon prompt dans un fichier, sans le reformuler, puis compile-le.

```bash
uv run --no-sync python -m skillflow_prompts chatgpt prompt --root <ROOT> --file prompt.txt
```

La sortie liste les unités et les obligations. Rédige ensuite ta réponse dans un fichier,
et fais-la contrôler.

```bash
uv run --no-sync python -m skillflow_prompts chatgpt answer --root <ROOT> --file answer.txt
```

Lis les statuts :

- `VERIFIE` : contrôlé mécaniquement, c'est passé.
- `REJETE` : contrôlé mécaniquement, c'est raté. Le message nomme l'unité, la valeur
  mesurée et la limite.
- `NON_VERIFIE` : aucun contrôle mécanique n'existe pour cette consigne. C'est à toi de la
  respecter, et l'outil ne prétend pas l'avoir vérifiée.

**Corrige uniquement ce qui est `REJETE`.** Ne touche pas à ce qui passe déjà : la sortie
liste ce qu'il ne faut pas casser. Réécris `answer.txt` et relance `answer`. Renvoyer la
**même** réponse ne consomme aucun tour. Le budget est de trois tours par étape ; après
`BUDGET_EXHAUSTED`, arrête-toi et dis-le moi, ne remets rien.

Quand tous les contrôles mécaniques passent, remets :

```bash
uv run --no-sync python -m skillflow_prompts chatgpt deliver --root <ROOT>
```

## 4. Ce que tu me montres

Le dossier de reçu contient `answer.txt`, `prompt.txt`, `receipt.json` et `RECU.md`.

1. **Recopie `answer.txt` à l'identique** dans ta réponse. C'est le texte contrôlé.
2. Puis colle `RECU.md` tel quel, sans le résumer.

Le reçu couvre `answer.txt`, **pas** le texte que tu affiches dans le chat. Si tu écris
autre chose à côté, le reçu ne le couvre pas, et c'est pour ça que je lis les deux.

## 5. Revérifier chez moi

Je peux tout recalculer sur ma machine, sans te faire confiance :

```bash
uv run --no-sync python -m skillflow_prompts chatgpt verify \
  --receipt <DOSSIER_RECU>/receipt.json --answer <DOSSIER_RECU>/answer.txt \
  --prompt <DOSSIER_RECU>/prompt.txt --root <ROOT>
```

`verify` recompile le prompt et recalcule chaque statut au lieu de relire le reçu. Si mon
éditeur a changé les fins de ligne, ajouté une marque d'ordre des octets ou changé la
composition Unicode, `verify` accepte et le dit : `answer_rewritten` vaut `true`, le reçu
affiché porte l'empreinte du fichier relu à côté de celle attestée, et tout est recalculé
sur le texte attesté. Tout autre octet modifié échoue avec `ANSWER_CHANGED`, y compris un
retour chariot seul.

Les valeurs de `checks` sont des verdicts, donc des booléens. Seule `key_anchored` peut
valoir `null`, ce qui veut dire « inconnu », pas « faux ». Si `same_code` est `false`, le
résumé le dit en clair : les statuts ont été recalculés par un autre code de vérificateurs
que celui qui les a écrits.

Une mise en garde sur la clé. Avec `--root <ROOT>`, l'ancre vient du bac à sable qui a
produit le reçu : `checks.key_anchored` vaut `true` et la sortie écrit « clé ancrée », ce
qui ne prouve **rien** sur la provenance. Pour une ancre qui ne vienne pas du zip, je
relève le `key_id` affiché par `init` par un autre canal, et je passe `--key-id <valeur>`
au lieu de `--root`.

## 6. Ce que cet outil ne prouve pas

- L'approbation est **fictive** : dans ton bac à sable, le lanceur détient tous les jetons.
  Le reçu le dit, la revue n'est pas lancée. Ce qui est réel, ce sont les contrôles
  mécaniques et le fait que je peux les recalculer.
- Rien ne t'empêche d'afficher un autre texte que `answer.txt`. La garantie n'est pas de
  t'en empêcher, elle est que je le détecte en revérifiant.
- Une consigne `NON_VERIFIE` reste une consigne. Elle n'est simplement pas contrôlée.

## 7. Si ça coince

- `uv sync --locked` échoue : arrête, donne l'erreur. Ne change pas le verrou.
- `ROOT_RELATIVE` : `--root` n'est pas absolu.
- `NO_PROMPT` : aucun prompt n'attend de réponse. Compile d'abord le prompt.
- `CASE_CLOSED` : le dossier est terminé. Colle le prompt suivant.
- En cas de doute sur l'état : `chatgpt status --root <ROOT>`, et montre-moi la sortie.
- `BUDGET_EXHAUSTED` : plus de tours. Arrête-toi, ne remets rien.
- Une sortie qui n'est pas un seul objet JSON : signale-le, c'est un défaut de l'outil.
