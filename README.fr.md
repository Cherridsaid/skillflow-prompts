# skillflow-prompts

[English](README.md)

![Décoratif seulement : des feuilles de papier et une règle métallique. Ne montre aucune sortie de l'outil.](docs/images/paper-rule.png)

**Un prompt devient un contrat, la réponse est contrôlée contre lui, et ce qui échoue est
nommé ligne par ligne.** Aucune intégration native, aucun serveur, aucune extension :
l'outil tourne dans une conversation ordinaire, dans le bac à sable que l'assistant a déjà,
et rend un reçu qui se vérifie ensuite sur votre propre machine.

**Ce reçu prouve que des règles mesurables ont été tenues sur des octets précis. Il ne
prouve pas que le contenu est juste, et la règle de sens n'est jamais contrôlée.** Rien
plus bas dans cette page n'adoucit cette phrase, et elle est ici parce que le lecteur qui
s'arrête après le premier paragraphe est précisément celui qu'elle protège.

Nous n'avons pas trouvé d'équivalent. C'est la forme honnête de l'affirmation : un flux qui
compile un prompt en contrat, contrôle la réponse obtenue, demande une correction sur les
seules lignes en échec, et signe un reçu qui se recalcule au lieu d'être cru.

## La boucle, dans une conversation

1. Vous collez un prompt. L'outil lit ce qui y est **mécaniquement contrôlable** : une
   limite de mots, un terme qui doit apparaître, un terme qui ne doit pas.
2. Chaque obligation est affichée avec son statut. Ce que l'outil reconnaît mais met de
   côté, sous une portée ou une condition, est affiché comme **candidat** et ne rejette
   jamais rien tant que vous ne l'avez pas confirmé nommément.
3. L'assistant écrit une réponse. L'outil la contrôle et nomme les lignes en échec.
4. L'assistant corrige. Lui demander de corriger **seulement** les lignes rejetées est une
   consigne donnée au modèle, pas une contrainte que l'outil impose.
5. La remise produit `receipt.json`, `answer.txt` et `prompt.txt`.
6. Vous les vérifiez à la maison, avec ce même outil, hors du bac à sable qui les a
   produits.

La consigne à coller dans une conversation neuve est dans [CHATGPT.md](CHATGPT.md).

## Ce qui a réellement été fait tourner

Essayé le **13 octobre 2026** de bout en bout, deux fois, avec le zip de ce dépôt :

| Plateforme | Ce qui a été fait |
|---|---|
| ChatGPT (mode travail) | installation, essai d'une minute, prompt, rejet, correction, remise, reçu |
| claude.ai | la même séquence, les mêmes résultats |

Le reçu du premier essai a ensuite été vérifié sur une machine locale : tous les contrôles
vrais, clé ancrée, et **un seul octet** changé dans le fichier de réponse fait passer la
vérification à `ANSWER_CHANGED`. C'est ce contre-essai qui donne sa valeur au premier : un
vérificateur qui dit oui à tout dirait oui à celui-là aussi.

Un troisième essai, le même jour, a été fait **à froid** : [CHATGPT.md](CHATGPT.md) collé
tel quel, aucune autre consigne et aucune aide. Trois tours, moins de quatre minutes en
tout, de l'installation au reçu. Deux faits de cet essai, datés et non promis :

- ChatGPT a rangé le collage de dix mille caractères en **pièce jointe** et l'a suivi quand
  même : la consigne survit donc au fait d'être transformée en fichier.
- L'essai s'est déroulé au **niveau d'effort le plus bas** du modèle. Le document a suffi à
  ce réglage, ce jour-là.

« Essayé le 13 octobre 2026 » n'est pas « fonctionne partout ». Les deux plateformes
changent sans nous, et rien ici n'est une promesse de compatibilité.

![Décoratif seulement : du vélin plissé. Ne montre aucune sortie de l'outil.](docs/images/vellum-pleats.png)

## Ce qui est contrôlé, et ce qui ne l'est pas

| Statut | Sens |
|---|---|
| `VERIFIE` | un vérificateur mécanique a tourné sur les octets de la réponse et il passe |
| `REJETE` | le même vérificateur a tourné et il échoue ; la ligne dit quoi corriger |
| `CANDIDAT` | reconnu, mis de côté, **incapable** de rejeter : il attend une confirmation nommée |
| `NON_VERIFIE` | une règle de sens ; rien dans cet outil ne la contrôle |

Les vérificateurs sont trois, et chacun est borné : voir
[docs/verifiers.md](docs/verifiers.md), qui écrit aussi comment un mot est compté.

**Les limites, en entier :**

- L'approbation est **fictive** : le lanceur détient tous les jetons de son propre bac à
  sable. Le reçu le dit, et `verify` aussi.
- **Aucune revue indépendante** n'est lancée. Le reçu porte `NOT_RUN`.
- La clé de signature est produite dans ce même bac à sable. Une signature valide prouve
  que le reçu n'a pas été modifié, **pas** qui l'a produit. Ancrer la clé veut dire passer
  son identifiant depuis l'extérieur, ce que fait `verify --key-id`.
- Une règle de sens reste `NON_VERIFIE`. L'outil ne prétend pas lire.
- Un reçu écrit par une autre version de cet outil ne se recompile pas à l'identique ici.
  La vérification échoue et **nomme la version** qui l'a écrit, au lieu d'accuser le
  contrat.
- Une fois qu'une réponse a été lue pour un prompt, un contrôle peut encore être **monté**
  nommément, et ne peut plus être **descendu** : on ne choisit pas ses règles en
  connaissant déjà la copie.

## Installation

Python 3.12 et [uv](https://docs.astral.sh/uv/).

```console
uv sync --locked
uv run python -m pytest
uv run skillflow-prompts chatgpt --help
```

Sous **Windows**, décompressez dans un dossier au chemin court, par exemple `C:\sfp`.
Windows ne résout que 260 caractères par chemin tant que les chemins longs ne sont pas
activés, et le fichier le plus profond créé par une installation se trouve une centaine de
caractères sous le dossier choisi : au-delà d'environ 150 caractères,
`uv sync --locked` tronque l'installation **en silence**. L'outil refuse désormais de
tourner dans cet état, sous `INSTALL_INCOMPLETE`, au lieu de planter plus tard.

![Décoratif seulement : une pierre sur une feuille quadrillée. Ne montre aucune sortie de l'outil.](docs/images/stone-grid.png)

## La démo

```console
uv run skillflow-prompts demo --root .skillflow/demo
```

Elle ouvre un dossier, le remet, vérifie le reçu, puis essaie de tricher. Ce qu'elle prouve
et ce qu'elle ne prouve pas est dans [DEMO.md](DEMO.md), un fichier **généré par la démo
elle-même** et comparé par un test, donc il ne peut pas décrire une exécution qui n'a pas
eu lieu.

## Pour aller plus loin

- [CHATGPT.md](CHATGPT.md) : la consigne pour piloter la boucle depuis une conversation.
- [docs/verifiers.md](docs/verifiers.md) : les trois vérificateurs, paramètre par paramètre.
- [docs/architecture.md](docs/architecture.md) : l'autorité, l'espace de travail, les hooks.
- [docs/contracts.md](docs/contracts.md) : écrire des contrats approuvés à la main.
- [docs/threat-model.md](docs/threat-model.md) : ce que la conception suppose, et ce contre
  quoi elle ne défend pas.
- [SECURITY.md](SECURITY.md), [CONTRIBUTING.md](CONTRIBUTING.md).

Tous les exemples de ce dépôt et de ses documents sont synthétiques.

## Provenance

Le cœur SkillFlow vient de skillflow-mcp, du même auteur (commit `c26ff68`), renommé en
`skillflow_prompts`. La couche prompt, le lanceur et les reçus sont le travail de ce dépôt.
La suite tourne sous Windows et sous Linux en intégration continue, à chaque push.

Restent ouverts : les vérificateurs supplémentaires, la couverture déclarée, le kernel, et
une attestation qui mérite son nom. Rien dans ce README ne décrit une fonction qui n'est
pas testée.

## Licence

Apache-2.0. Dérivé de skillflow-mcp, du même auteur.
