# Consigne à coller dans ChatGPT (mode Travailler) — variante zip

Tu vas faire tourner un outil local, `skillflow-prompts`, qui transforme un prompt en
contrat, contrôle ta réponse contre ce contrat, nomme les obligations qu'il rejette, et
rend un reçu signé. Ne corriger que ce qui a été rejeté est une consigne que je te donne,
pas une contrainte que l'outil impose. Tu l'exécutes dans ton bac à sable. Suis exactement
les étapes ci-dessous et montre-moi la sortie de chaque commande.

## 1. Installer, une seule fois

Un zip du dépôt est joint à ce message. Décompresse-le, puis installe les dépendances
verrouillées.

Deux chemins, posés **une seule fois** et réutilisés tels quels partout ensuite :

- `<DOSSIER>` : le chemin **absolu** du dossier obtenu en décompressant.
- `<ROOT>` : **toujours** `<DOSSIER>/travail`. C'est l'état sur disque, et c'est le même à
  chaque commande.

```bash
cd <DOSSIER>
uv sync --locked
uv run --no-sync python -m skillflow_prompts chatgpt init --root <ROOT>
```

`init` crée l'état sur disque et affiche un `key_id`. **Donne-moi ce `key_id`** : c'est la
seule façon pour moi d'ancrer la clé autrement qu'avec un fichier venu du même zip.

Si `uv sync --locked` échoue, **arrête-toi** et donne-moi l'erreur telle quelle : l'outil
ne peut pas démarrer sans ses dépendances, et je ne veux pas d'un contournement qui
installerait autre chose que le verrou.

Deux choses à savoir sur l'environnement :

- **Chemin court obligatoire sous Windows.** Décompresse dans un dossier court, par exemple
  `C:\sfp`. Au-delà d'environ 150 caractères de chemin, Windows tronque l'installation
  **en silence** et il manque des fichiers de données. L'outil refuse alors de tourner,
  sous `INSTALL_INCOMPLETE` : réinstalle dans un chemin plus court, ne contourne pas.
- **Ignore `stderr`.** Certains environnements y écrivent des avertissements, par exemple
  sur `UV_NATIVE_TLS`. Ce n'est pas une erreur. Ce qui compte est `stdout`, qui porte
  **un seul objet JSON** par commande, et le code de sortie.
- **Cette consigne compte même rangée en pièce jointe.** Si l'interface transforme ce
  collage en fichier joint, tu la suis exactement de la même façon. Essayé le 13 octobre
  2026 : rangée en pièce jointe, suivie quand même, au niveau d'effort le plus bas.

### Essai d'une minute, tout de suite après `init`

N'attends pas un vrai prompt de ma part pour savoir si l'installation marche. Écris deux
fichiers avec **exactement** ce contenu.

`essai.txt` :

```text
Réponds en 12 mots maximum.
Mentionne « SkillFlow ».
```

`essai-reponse.txt` :

```text
SkillFlow fonctionne.
```

Puis lance les trois commandes :

```bash
uv run --no-sync python -m skillflow_prompts chatgpt prompt --root <ROOT> --file essai.txt
uv run --no-sync python -m skillflow_prompts chatgpt answer --root <ROOT> --file essai-reponse.txt
uv run --no-sync python -m skillflow_prompts chatgpt deliver --root <ROOT>
```

Ce que tu dois obtenir, et que je veux voir : `prompt` donne **deux** obligations
mécaniques, une limite de mots et un terme requis ; `answer` rend le code 0 avec
`VERIFIE: 2` et `REJETE: 0` ; `deliver` rend le code 0 et un dossier de reçu qui contient
`answer.txt`, `prompt.txt`, `receipt.json` et `RECU.md`. Si l'un des trois diffère,
arrête-toi et donne-moi la sortie : c'est l'installation qui est en cause, pas le prompt.

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

La sortie liste les unités et les obligations, en trois sortes :

- `mechanical` : un contrôle que l'outil fera lui-même, limite de mots ou terme requis.
- `candidate` : une limite ou un terme que l'outil a **reconnu puis mis de côté**, avec le
  mot qui l'a mis de côté dans `demoted_by`. Par exemple « résumé » dans une ligne de
  contexte fait passer tout ce qui suit en candidat. Un candidat est **affiché et jamais
  rejeté** : il n'est pas dans le contrat, donc il ne peut pas faire échouer ta réponse.
- `semantic` : une règle de sens, à respecter, que l'outil ne contrôle pas.

**S'il y a des candidats, arrête-toi et montre-les moi.** C'est moi qui confirme, pas toi :
une obligation confirmée devient un contrôle, et le reçu dira qui l'a confirmée. Attends ma
réponse, puis lance ce que je te dicte, par exemple :

```bash
uv run --no-sync python -m skillflow_prompts chatgpt confirm --root <ROOT> --by said --id u2-max-words --id u3-contains-1
```

`--id` monte un candidat en contrôle, `--refuse` descend un contrôle en candidat, `--by`
nomme la personne qui décide. Ce nom est **déclaré au clavier**, pas authentifié : le reçu
l'affiche comme tel, et c'est moi qui sais si c'est bien moi qui l'ai tapé. La confirmation
compile un **nouveau** contrat et rouvre le dossier.

Une fois qu'une réponse a été contrôlée, **monter reste possible** : `--id` ne fait
qu'ajouter un contrôle, donc cela n'excuse jamais ce qui a été rejeté. **Descendre est
refusé** (`--refuse`, `CONFIRM_AFTER_ANSWER`) : on ne retire pas la règle qui vient de
rejeter la copie. Recoller le même prompt ne contourne rien, parce que le dossier est
adressé par le prompt : le même prompt reprend le même dossier, avec ses réponses déjà
comptées et les décisions déjà prises. Un identifiant inconnu est refusé avec la liste des
identifiants connus (`CONFIRM_UNKNOWN`).

Rédige ensuite ta réponse dans un fichier, et fais-la contrôler.

```bash
uv run --no-sync python -m skillflow_prompts chatgpt answer --root <ROOT> --file answer.txt
```

Cinq choses sont contrôlées mécaniquement, et rien d'autre : une **limite de mots**, un
**plancher de mots** quand j'écris « au moins N mots », un **terme requis**, un **terme
interdit** quand je l'écris sous la forme « ne mentionne pas « X » » avec le terme entre
guillemets, et une **réponse qui finit par une question** quand j'écris « termine par une
question ».

Une limite et un plancher ne comptent pas les mots de la même façon, et c'est voulu. Sous
une limite, « l'eau » et « porte-monnaie » comptent pour un mot ; sous un plancher, ils
comptent pour deux. Mesuré sur une phrase française : 21 mots d'un côté, 33 de l'autre.
Chaque verdict nomme le comptage qui a décidé, donc la même réponse peut lire 21 sous une
limite et 33 sous un plancher sans que ce soit un défaut. La règle derrière : un contrôle
peut être plus faible que la consigne, jamais plus fort.

Un plancher qui compte autre chose que des mots met la limite voisine en `CANDIDAT` : « au
moins deux exemples de 5 mots maximum » demande cinq mots par exemple, pas cinq mots en
tout, et l'outil ne contrôle pas une limite qu'il lirait plus strictement que moi.

Un terme interdit est cherché comme **mot entier**, jamais à l'intérieur d'un mot plus
long : interdire « roue » n'interdit pas « brouette ». Éviter le mot n'évite pas l'idée non
plus : ce qui reste de la consigne demeure `NON_VERIFIE`.

Finir par une question se lit ainsi : le dernier caractère de ta réponse qui est une
lettre, un chiffre ou un point d'interrogation doit être un point d'interrogation. Ce qui
décore la fin est ignoré, donc le gras, les guillemets, les parenthèses et un emoji final
ne te feront pas échouer. Que la question soit **ouverte** n'est pas contrôlé.

Lis les statuts :

- `VERIFIE` : contrôlé mécaniquement, c'est passé.
- `REJETE` : contrôlé mécaniquement, c'est raté. Le message nomme l'unité, la valeur
  mesurée et la limite.
- `NON_VERIFIE` : aucun contrôle mécanique n'existe pour cette consigne. C'est à toi de la
  respecter, et l'outil ne prétend pas l'avoir vérifiée.
- `CANDIDAT` : une limite ou un terme reconnu mais non confirmé. Affiché pour que je le
  voie, jamais rejeté. Si je le confirme, il devient un contrôle au prochain dossier.

**Corrige uniquement ce qui est `REJETE`.** Ne touche pas à ce qui passe déjà : la sortie
liste ce qu'il ne faut pas casser. Réécris `answer.txt` et relance `answer`. Renvoyer la
**même** réponse ne consomme aucun tour. Le budget est de trois tours par étape ; après
`BUDGET_EXHAUSTED`, arrête-toi et dis-le moi, ne remets rien. Recoller le même prompt pour
repartir d'un dossier neuf ne rouvre rien : le budget est attaché au prompt, pas à la
conversation, et un dossier rouvert avec le même texte revient épuisé.

Quand tous les contrôles mécaniques passent, remets :

```bash
uv run --no-sync python -m skillflow_prompts chatgpt deliver --root <ROOT>
```

## 4. Ce que tu me montres

Le dossier de reçu contient `answer.txt`, `prompt.txt`, `receipt.json` et `RECU.md`.

1. **Recopie `answer.txt` à l'identique** dans ta réponse. C'est le texte contrôlé.
2. Puis colle `RECU.md` tel quel, sans le résumer.
3. **Donne-moi `receipt.json`, `answer.txt` et `prompt.txt` en fichiers téléchargeables**,
   pas recopiés dans le chat. Le chat reformate, et une seule espace changée fait échouer
   la vérification chez moi. Ces trois fichiers sont ce que je rejoue ; le texte collé
   n'est que pour la lecture.

Le reçu couvre `answer.txt`, **pas** le texte que tu affiches dans le chat. Si tu écris
autre chose à côté, le reçu ne le couvre pas, et c'est pour ça que je lis les deux.

## 5. Revérifier chez moi

Je peux tout recalculer sur ma machine, sans te faire confiance :

```bash
uv run --no-sync python -m skillflow_prompts chatgpt verify \
  --receipt receipt.json --answer answer.txt --prompt prompt.txt
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

La commande ci-dessus n'a **pas** de `--root`, exprès. Avec `--root`, l'ancre de la clé
viendrait du bac à sable qui a produit le reçu, `checks.key_anchored` vaudrait `true` et la
sortie écrirait « clé ancrée » sans rien prouver sur la provenance. Sans `--root`,
`key_anchored` vaut `null` et la sortie dit « clé non ancrée » : c'est l'état honnête, et
tout le reste est quand même recalculé. Pour une ancre qui ne vienne pas du zip, j'utilise
le `key_id` que tu m'as donné à l'étape 1, avec `--key-id <valeur>`.

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
