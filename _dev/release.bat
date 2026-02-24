@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
set "REPO_DIR=%SCRIPT_DIR%.."
pushd "%REPO_DIR%" >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Failed to enter repo root: "%REPO_DIR%"
  exit /b 1
)

if not exist "tools\release.py" (
  echo [ERROR] tools\release.py not found. Please run this script from _dev under repo root.
  popd
  exit /b 1
)

where gh >nul 2>nul
if errorlevel 1 (
  echo [ERROR] GitHub CLI ^(gh^) not found. Please install gh first.
  exit /b 1
)
gh auth status >nul 2>nul
if errorlevel 1 (
  echo [ERROR] gh is not authenticated. Please run: gh auth login
  exit /b 1
)

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found.
  exit /b 1
)

set "VERSION_INPUT=%~1"
if not "%VERSION_INPUT%"=="" goto :PARSE_INPUT

echo.
echo ===== Sutu Blender Bridge Release =====
echo [1] patch ^(recommended for daily release^)
echo [2] minor
echo [3] major
echo [4] set exact version
echo [5] cancel
choice /c 12345 /n /m "Select release type [1-5]: "

if errorlevel 5 goto :CANCEL
if errorlevel 4 goto :INPUT_CUSTOM
if errorlevel 3 set "VERSION_INPUT=major" & goto :PARSE_INPUT
if errorlevel 2 set "VERSION_INPUT=minor" & goto :PARSE_INPUT
if errorlevel 1 set "VERSION_INPUT=patch" & goto :PARSE_INPUT

:INPUT_CUSTOM
set /p VERSION_INPUT=Enter exact version ^(e.g. 0.3.0^): 
if "%VERSION_INPUT%"=="" (
  echo [ERROR] Version cannot be empty.
  popd
  exit /b 1
)

:PARSE_INPUT
if /I "%VERSION_INPUT%"=="major" goto :BUMP
if /I "%VERSION_INPUT%"=="minor" goto :BUMP
if /I "%VERSION_INPUT%"=="patch" goto :BUMP
goto :SET_VERSION

:BUMP
echo [INFO] Bumping version: %VERSION_INPUT%
python tools\release.py bump %VERSION_INPUT%
if errorlevel 1 (
  popd
  exit /b 1
)
goto :AFTER_VERSION

:SET_VERSION
echo [INFO] Setting version: %VERSION_INPUT%
python tools\release.py set %VERSION_INPUT%
if errorlevel 1 (
  popd
  exit /b 1
)

:AFTER_VERSION
for /f %%V in ('python tools\release.py print') do set "NEW_VERSION=%%V"
if "%NEW_VERSION%"=="" (
  echo [ERROR] Failed to read updated version.
  popd
  exit /b 1
)

for /f %%B in ('git rev-parse --abbrev-ref HEAD') do set "CURRENT_BRANCH=%%B"
if "%CURRENT_BRANCH%"=="" (
  echo [ERROR] Failed to read current git branch.
  popd
  exit /b 1
)

git add addon_meta.json blender_manifest.toml
git commit -m "chore(release): v%NEW_VERSION%" -- addon_meta.json blender_manifest.toml
if errorlevel 1 (
  echo [ERROR] Git commit failed. Please check repository state.
  popd
  exit /b 1
)

git push
if errorlevel 1 (
  echo [ERROR] Git push failed.
  popd
  exit /b 1
)

gh workflow run release-addon.yml --ref "%CURRENT_BRANCH%"
if errorlevel 1 (
  echo [ERROR] Failed to trigger GitHub workflow.
  popd
  exit /b 1
)

echo [INFO] Release workflow triggered for v%NEW_VERSION% on branch %CURRENT_BRANCH%.
echo [INFO] Check latest run:
gh run list --workflow release-addon.yml --limit 1
popd
exit /b 0

:CANCEL
echo [INFO] Cancelled.
popd
exit /b 0
