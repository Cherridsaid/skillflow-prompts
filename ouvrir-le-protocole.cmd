@echo off
rem Ouvre la page du protocole sans taper de commande : double-clic sur ce fichier.
rem
rem Ce fichier est volontairement mince, et plat : aucun bloc entre parentheses, aucune
rem etiquette, aucune logique. Il ne se teste pas comme le reste du depot, donc il ne porte
rem aucune logique. Toute la justesse vit dans le Python, qui se teste. Il ne fait que
rem quatre choses : se placer dans le depot, verifier qu il est bien a cote du projet,
rem verifier que uv est la, et lancer la page avec une racine absolue.
rem
rem La fenetre reste ouverte jusqu au bout, y compris sur un echec : une fenetre qui
rem clignote et se ferme apprend a son utilisateur que l outil est casse.
chcp 65001 >nul
cd /d "%~dp0"
rem Copie hors du projet : uv repondait alors Failed to spawn, en anglais, sans un mot de
rem ce lanceur. Ce controle doit vivre ICI, parce que le Python est injoignable quand le
rem projet manque. Mesure apres la cloture de la phase precedente.
if not exist "%~dp0pyproject.toml" echo Ce fichier a ete copie hors du projet skillflow-prompts.
if not exist "%~dp0pyproject.toml" echo Remets-le dans le dossier du projet, a cote de pyproject.toml.
if not exist "%~dp0pyproject.toml" echo Pour l avoir ailleurs, fais un RACCOURCI vers lui, pas une copie.
if not exist "%~dp0pyproject.toml" pause
if not exist "%~dp0pyproject.toml" exit /b 1
rem La racine d etat vit HORS du depot : la cle de signature et l historique sont la, et
rem recloner ou deplacer le depot ne doit pas les perdre.
set "RACINE=%LOCALAPPDATA%\skillflow-prompts\travail"
where uv >nul 2>nul
if errorlevel 1 echo uv n'est pas trouve sur cette machine.
if errorlevel 1 echo Installe uv, puis double-clique a nouveau : https://docs.astral.sh/uv/
if errorlevel 1 pause
if errorlevel 1 exit /b 1
uv run skillflow-prompts chatgpt serve --root "%RACINE%" --open
echo.
rem Deux fins differentes, et elles ne se disent pas de la meme facon : la page a tourne
rem puis s est arretee, ou elle n a jamais demarre. La phrase precedente affirmait l arret
rem dans les deux cas.
if errorlevel 1 echo La page ne s est pas ouverte. Le message au-dessus dit pourquoi.
if not errorlevel 1 echo La page est arretee. Tu peux fermer cette fenetre.
pause
