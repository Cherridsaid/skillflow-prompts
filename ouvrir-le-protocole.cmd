@echo off
rem Ouvre la page du protocole sans taper de commande : double-clic sur ce fichier.
rem
rem Ce fichier est volontairement mince, et plat : aucun bloc entre parentheses, aucune
rem etiquette, aucune logique. Il ne se teste pas comme le reste du depot, donc toute la
rem justesse vit dans le Python, qui se teste. Il ne fait que trois choses : se placer dans
rem le depot, verifier que uv est la, et lancer la page avec une racine absolue.
rem
rem La fenetre reste ouverte jusqu'au bout, y compris sur un echec : une fenetre qui
rem clignote et se ferme apprend a son utilisateur que l'outil est casse.
chcp 65001 >nul
cd /d "%~dp0"
rem La racine d'etat vit HORS du depot : la cle de signature et l'historique sont la, et
rem recloner ou deplacer le depot ne doit pas les perdre.
set "RACINE=%LOCALAPPDATA%\skillflow-prompts\travail"
where uv >nul 2>nul
if errorlevel 1 echo uv n'est pas trouve sur cette machine.
if errorlevel 1 echo Installe uv, puis double-clique a nouveau : https://docs.astral.sh/uv/
if errorlevel 1 pause
if errorlevel 1 exit /b 1
uv run skillflow-prompts chatgpt serve --root "%RACINE%" --open
echo.
echo La page est arretee. Tu peux fermer cette fenetre.
pause
