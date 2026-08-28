@echo off
rem ═══════════════════════════════════════════════════════════════════════════
rem  OmniTrade Hub — DÉMARRAGE WINDOWS
rem  Double-cliquez ce fichier. C'est tout.
rem  Il lance le moteur de données puis ouvre OmniTrade Hub dans votre navigateur.
rem  LAISSEZ LA FENÊTRE « OmniTrade Hub - moteur » OUVERTE pendant votre session ;
rem  pour tout arrêter : fermez simplement cette fenêtre.
rem ═══════════════════════════════════════════════════════════════════════════

chcp 65001 >nul 2>&1
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"

set "PORT=8765"

echo ══════════════════════════════════════════════════════════════
echo    OmniTrade Hub — démarrage
echo ══════════════════════════════════════════════════════════════
echo.

rem ── Fichier de l'application : DÉTECTÉ, jamais codé en dur ──────────────
rem  On choisit le numéro de version le PLUS ÉLEVÉ des fichiers réellement
rem  présents (les copies de travail « pre-refactor » etc. sont ignorées).
set "APP="
set "BEST=-1"
for %%f in (omnitrade-v*.html) do (
  set "NAME=%%~nf"
  echo !NAME!| findstr /i /v "pre-refactor backup copie sauvegarde ancien old" >nul && (
    for /f "tokens=2 delims=-v." %%N in ("!NAME!") do (
      set "N=%%N"
      if !N! gtr !BEST! (
        set "BEST=!N!"
        set "APP=%%f"
      )
    )
  )
)
if not defined APP (
  for %%f in (omnitrade-*.html) do if not defined APP set "APP=%%f"
)
if not defined APP (
  echo [!] Aucun fichier omnitrade-*.html trouvé dans ce dossier.
  echo     Décompressez l'archive complète avant de lancer ce fichier.
  pause
  exit /b 1
)

rem ── Dossier de logs (toujours inscriptible) ─────────────────────────────
set "LOGDIR=%APPDATA%\OmniTrade Hub"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
set "LOGF=%LOGDIR%\OmniTradeHub.log"

rem ── Moteur : binaire compilé si présent, sinon Python ───────────────────
set "BIN="
if exist "bin\OmniTradeBridge\OmniTradeBridge.exe" set "BIN=bin\OmniTradeBridge\OmniTradeBridge.exe"
if not defined BIN if exist "OmniTradeBridge.exe" set "BIN=OmniTradeBridge.exe"

set "PY="
for /f "delims=" %%X in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%X"
if not defined PY for /f "delims=" %%X in ('python -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%X"
if not defined PY (
  @echo off
  echo.
  echo [!] Python est nécessaire pour le moteur de données.
  echo.
  echo     Installation en 2 minutes :
  echo       1. Ouvrez  https://www.python.org/downloads/windows/
  echo       2. Téléchargez le dernier « Windows installer (64-bit) »
  echo       3. À l'installation, COCHEZ « Add python.exe to PATH »
  echo       4. Relancez ce fichier
  echo.
  echo     OmniTrade Hub fonctionne malgré tout : le journal, les analyses
  echo     et les cours d'éducation n'ont besoin d'aucun moteur.
  echo.
  set /p OUVRE=Ouvrir OmniTrade Hub quand même ? [Entrée]
  if exist "%APP%" start "" "%~dp0%APP%"
  exit /b 0
)

rem ── Libération du port ───────────────────────────────────────────────────
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":%PORT% " ^| findstr "LISTENING"') do (
  taskkill /F /PID %%P >nul 2>&1
)
timeout /t 1 /nobreak >nul

rem ── Dépendances (une seule fois) ─────────────────────────────────────────
if defined PY (
  "%PY%" -c "import flask, flask_cors" >nul 2>&1 || (
    echo  · Première installation des composants (flask)…
    "%PY%" -m pip install --quiet flask flask-cors >nul 2>&1
  )
)

rem ── Clé d'accès de CE poste ──────────────────────────────────────────────
rem  Le moteur connaît sa clé (créée au premier démarrage) : on la lui
rem  demande plutôt que d'en inventer une.
set "TOKEN="
if defined BIN (
  for /f "usebackq delims=" %%T in (`"%BIN%" --show-token --no-keep-open 2^>nul`) do set "TOKEN=%%T"
) else (
  for /f "usebackq delims=" %%T in (`"%PY%" "9-moteur-de-donnees.py" --show-token --no-keep-open 2^>nul`) do set "TOKEN=%%T"
)
rem  Un token n'a pas d'espace : si --show-token a renvoyé du texte, on jette.
echo !TOKEN!| findstr /i /r " " >nul && set "TOKEN="
echo !TOKEN!| findstr /i "flask" >nul && set "TOKEN="
if not defined TOKEN set "TOKEN=ZELLA_TOKEN"

echo   Moteur : %BIN%%PY%
echo   Port   : %PORT%
echo   App    : %APP%
echo.

rem ── Lancement du moteur (fenêtre minimisée qui reste ouverte) ────────────
if defined BIN (
  start "OmniTrade Hub - moteur" /min "%BIN%" --host 127.0.0.1 --port %PORT% --token %TOKEN% --no-keep-open
) else (
  start "OmniTrade Hub - moteur" /min "%PY%" "9-moteur-de-donnees.py" --host 127.0.0.1 --port %PORT% --token %TOKEN% --no-keep-open
)

rem ── On attend que le moteur réponde avant d'ouvrir le navigateur ─────────
for /l %%i in (1,1,20) do (
  curl -s -o nul -m 1 "http://127.0.0.1:%PORT%/api/ping?token=%TOKEN%" 2>nul && goto :ping_ok
  timeout /t 1 /nobreak >nul
)
:ping_ok

echo  ▸ Ouverture de OmniTrade Hub dans votre navigateur…
start "" "http://127.0.0.1:%PORT%/"

echo.
echo ──────────────────────────────────────────────────────────────
echo   OmniTrade Hub est lancé.
echo.
echo   • Les données de marché (actualités, calendrier, sentiment)
echo     arrivent automatiquement.
echo   • Si MetaTrader 5 est ouvert avec l'EA, vos trades se
echo     synchronisent aussi.
echo.
echo   Pour arrêter : fermez la fenêtre « OmniTrade Hub - moteur ».
echo ──────────────────────────────────────────────────────────────
echo.
exit /b 0