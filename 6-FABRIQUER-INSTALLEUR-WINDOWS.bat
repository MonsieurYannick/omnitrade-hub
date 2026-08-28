@echo off
rem ═══════════════════════════════════════════════════════════════════════════
rem  OmniTrade Hub — FABRICATION DE L'INSTALLATEUR WINDOWS
rem ---------------------------------------------------------------------------
rem  À lancer sur un PC WINDOWS (celui qui a déjà Python). Produit un VRAI
rem  installateur Windows pour vos clients : OmniTradeHub-Setup-<version>.exe,
rem  qui fonctionne SANS Python et SANS MetaTrader d'installation préalable.
rem
rem  Double-cliquez simplement ce fichier.
rem  (Optionnel mais conseillé : Inno Setup → https://jrsoftware.org/isdl.php
rem   pour produire l'installeur .exe ; sinon une archive ZIP est créée.)
rem ═══════════════════════════════════════════════════════════════════════════

chcp 65001 >nul 2>&1
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"

echo ══════════════════════════════════════════════════════════════
echo    OmniTrade Hub — Fabrication de l'installateur Windows
echo ══════════════════════════════════════════════════════════════
echo.

if not exist "9-moteur-de-donnees.py" (
  echo [!] 9-moteur-de-donnees.py introuvable.
  echo     Placez ce script dans le MÊME dossier que 9-moteur-de-donnees.py.
  pause
  exit /b 1
)

rem ── Fichier source : la version la PLUS ÉLEVÉE, jamais de copie de travail ─
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
  pause
  exit /b 1
)
rem  Le moteur charge EN DUR "omnitrade-v21.html" (ligne ~9368) : on le
rem  replacera sous ce nom exact dans le paquet, quel que soit le numéro réel.
set "HTML_NAME=omnitrade-v21.html"

rem ── Eldorado : le noyau de licence est INDISPENSABLE ─────────────────────
if not exist "9-licence.py" (
  echo [!] 9-licence.py est introuvable dans ce dossier.
  echo     Sans lui, aucune clé ne pourra être activée.
  pause
  exit /b 1
)
if not exist "public_key.txt" (
  echo [!] public_key.txt est introuvable.
  echo     C'est la clé publique qui vérifie les licences. Sans lui,
  echo     le moteur refuserait TOUTE clé.
  pause
  exit /b 1
)

rem ── Python réel ──────────────────────────────────────────────────────────
set "PY="
py -3 -c "pass" >nul 2>&1 && set "PY=py -3"
if not defined PY python -c "pass" >nul 2>&1 && set "PY=python"
if not defined PY (
  echo [!] Python est nécessaire pour fabriquer l'installateur.
  echo     Installez-le : https://www.python.org/downloads/windows/
  echo     (COCHEZ « Add python.exe to PATH »)
  pause
  exit /b 1
)

rem ── Purge des caches et des anciens builds ──────────────────────────────
echo → Purge des caches et des anciens builds…
rmdir /s /q build dist .venv-build OmniTradeHub-Windows-PRET 2>nul
del /q OmniTradeHub-Windows-PRET.zip OmniTradeHub-installateur.iss 2>nul
del /q *.spec 2>nul

echo → Préparation de l'environnement (1re fois : ~1 minute)…
%PY% -m venv .venv-build || (echo [!] Création de l'environnement impossible.& pause & exit /b 1)
call .venv-build\Scripts\activate.bat

python -m pip install --upgrade pip wheel >nul 2>&1
echo → Téléchargement de PyInstaller et Flask…
rem  certifi : sans lui, TOUTES les requêtes HTTPS échouent chez le client.
python -m pip install "pyinstaller>=6.0" flask flask-cors certifi >nul 2>&1 || (
  echo [!] Téléchargement impossible. Vérifiez votre connexion Internet.
  pause & exit /b 1
)

rem ── Compilation ─────────────────────────────────────────────────────────--
echo → Compilation en cours (2 à 5 minutes, soyez patient)…
rmdir /s /q build dist 2>nul
python -m PyInstaller --clean --noconfirm --name OmniTradeBridge --console --noupx ^
  --icon "OmniTradeHub.ico" ^
  --hidden-import flask --hidden-import flask_cors ^
  --hidden-import werkzeug --hidden-import werkzeug.serving ^
  --hidden-import jinja2 --hidden-import itsdangerous ^
  --hidden-import click --hidden-import blinker ^
  --hidden-import license_core ^
  --hidden-import certifi --collect-data certifi ^
  --hidden-import concurrent.futures --hidden-import concurrent.futures.thread ^
  --hidden-import ssl --hidden-import _ssl ^
  --exclude-module tkinter --exclude-module numpy --exclude-module pandas ^
  --exclude-module matplotlib --exclude-module PIL --exclude-module pytest ^
  --add-data "%APP%;." ^
  --add-data "9-licence.py;." ^
  --add-data "public_key.txt;." ^
  9-moteur-de-donnees.py >"build-log.txt" 2>&1
if errorlevel 1 (
  echo [!] Échec de la compilation. Détails dans build-log.txt
  type build-log.txt
  pause & exit /b 1
)
if not exist "dist\OmniTradeBridge\OmniTradeBridge.exe" (
  echo [!] Le binaire n'a pas été produit. Voir build-log.txt
  pause & exit /b 1
)

rem ── Le moteur lit "_internal\omnitrade-v21.html" : copie normalisée ──────
set "INT=dist\OmniTradeBridge\_internal"
if not exist "%INT%" set "INT=dist\OmniTradeBridge"
copy /y "%APP%" "%INT%\%HTML_NAME%" >nul 2>&1

rem ── Vérification RÉELLE du binaire ──────────────────────────────────────
echo → Vérification du binaire compilé…
"dist\OmniTradeBridge\OmniTradeBridge.exe" --list-dirs --no-keep-open >nul 2>&1
if errorlevel 1 (
  echo [!] Le binaire compilé ne démarre pas. Voir build-log.txt
  pause & exit /b 1
)
echo    ✓ le binaire fonctionne sans Python
"dist\OmniTradeBridge\OmniTradeBridge.exe" --selftest-ssl --no-keep-open >nul 2>&1
if errorlevel 1 (
  echo [!] HTTPS échoue dans le binaire : certifi manquant.
  pause & exit /b 1
)
echo    ✓ requêtes HTTPS opérationnelles

rem ── Assemblage du paquet ────────────────────────────────────────────────
echo → Assemblage du paquet…
set "OUT=OmniTradeHub-Windows-PRET"
mkdir "%OUT%"
xcopy /s /i /y "dist\OmniTradeBridge" "%OUT%\bin" >nul 2>&1
copy /y "%APP%" "%OUT%\" >nul 2>&1
copy /y "9-moteur-de-donnees.py" "%OUT%\" >nul 2>&1
copy /y "9-licence.py" "%OUT%\" >nul 2>&1
copy /y "public_key.txt" "%OUT%\" >nul 2>&1
if exist "2-OmniTradeExport.mq5" copy /y "2-OmniTradeExport.mq5" "%OUT%\" >nul 2>&1
if exist "0-LISEZ-MOI.txt" copy /y "0-LISEZ-MOI.txt" "%OUT%\" >nul 2>&1
if not exist "1-START-WINDOWS.bat" (
  echo [!] 1-START-WINDOWS.bat introuvable : impossible de livrer.
  pause & exit /b 1
)
copy /y "1-START-WINDOWS.bat" "%OUT%\" >nul 2>&1
if exist "OmniTradeHub.ico" copy /y "OmniTradeHub.ico" "%OUT%\" >nul 2>&1

rem ── Aucun secret ne doit partir chez un client ──────────────────────────
del /q "%OUT%\private_key*" "%OUT%\GENERATEUR*" "%OUT%\groq.key" "%OUT%\openrouter.key" 2>nul
rmdir /s /q "%OUT%\oth_admin" 2>nul
for /r "%OUT%" %%S in (private_key* GENERATEUR* oth_admin groq.key openrouter.key) do (
  if exist "%%S" (
    echo [!] Un secret de licence a été détecté dans le paquet. Annulation.
    pause & exit /b 1
  )
)

rem ── Contrôle : le HTML de production est le SEUL présent ────────────────
for /r "%OUT%" %%H in (omnitrade-v*.html zellatrack-v*.html) do (
  if /i not "%%~nxH"=="%HTML_NAME%" del /q "%%H" 2>nul
)

rem ── Installeur Inno Setup (si présent) ou ZIP ───────────────────────────
set "ISCC="
for %%p in ("%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" "%ProgramFiles%\Inno Setup 6\ISCC.exe" "%LocalAppData%\Programs\Inno Setup 6\ISCC.exe") do (
  if exist "%%~p" set "ISCC=%%~p"
)
set "VER=%BEST%"
set "SETUPEXE=OmniTradeHub-Setup-%VER%.exe"

if defined ISCC (
  echo → Génération de l'installeur Inno Setup…
  >"OmniTradeHub-installateur.iss" (
    echo [Setup]
    echo AppName=OmniTrade Hub
    echo AppVersion=%VER%
    echo AppPublisher=OmniTrade
    echo DefaultDirName={autopf}\OmniTradeHub
    echo DefaultGroupName=OmniTrade Hub
    echo SetupIconFile=%CD%\OmniTradeHub.ico
    echo UninstallDisplayIcon={app}\bin\OmniTradeBridge.exe
    echo DisableProgramGroupPage=yes
    echo OutputDir=.
    echo OutputBaseFilename=OmniTradeHub-Setup-%VER%
    echo Compression=lzma2
    echo SolidCompression=yes
    echo PrivilegesRequired=lowest
    echo ArchitecturesAllowed=x64compatible
    echo ArchitecturesInstallIn64BitMode=x64compatible
    echo [Tasks]
    echo Name: desktopicon; Description: Créer un raccourci sur le Bureau; GroupDescription: Raccourcis:
    echo [Files]
    echo Source: "%OUT%\*"; DestDir: {app}; Flags: recursesubdirs ignoreversion; Permissions: users-modify
    echo [Icons]
    echo Name: "{group}\OmniTrade Hub"; Filename: "{app}\1-START-WINDOWS.bat"
    echo Name: "{autodesktop}\OmniTrade Hub"; Filename: "{app}\1-START-WINDOWS.bat"; Tasks: desktopicon
    echo [Run]
    echo Filename: "{app}\1-START-WINDOWS.bat"; Description: Lancer OmniTrade Hub; Flags: nowait postinstall skipifsilent
  )
  "%ISCC%" "OmniTradeHub-installateur.iss" >nul 2>&1
  if errorlevel 1 (
    echo    ⚠️  Inno Setup a échoué — création d'une archive ZIP à la place.
    set "ISCC="
  )
)

if not defined ISCC (
  echo → Création de l'archive ZIP…
  powershell -NoProfile -Command "Compress-Archive -Path '%OUT%\*' -DestinationPath '%CD%\OmniTradeHub-Windows-PRET.zip' -Force" >nul 2>&1
)

rem ── Nettoyage ───────────────────────────────────────────────────────────
call deactivate 2>nul
rmdir /s /q build .venv-build 2>nul

echo.
echo ══════════════════════════════════════════════════════════════
if defined ISCC (
  echo   ✅ TERMINÉ
  echo.
  echo      Installateur à distribuer :
  echo        %SETUPEXE%
) else (
  echo   ✅ TERMINÉ — archive à distribuer :
  echo        OmniTradeHub-Windows-PRET.zip
)
echo.
echo      Vos clients n'auront QU'À :
echo        - exécuter l'installateur, ou
echo        - décompresser l'archive et double-cliquer 1-START-WINDOWS.bat
echo.
echo      Aucun Python à installer côté client.
echo ══════════════════════════════════════════════════════════════
echo.
pause