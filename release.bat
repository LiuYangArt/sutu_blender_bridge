@echo off
setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0"

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
if "%VERSION_INPUT%"=="" set "VERSION_INPUT=patch"

if /I "%VERSION_INPUT%"=="major" goto :BUMP
if /I "%VERSION_INPUT%"=="minor" goto :BUMP
if /I "%VERSION_INPUT%"=="patch" goto :BUMP
goto :SET_VERSION

:BUMP
echo [INFO] Bumping version: %VERSION_INPUT%
python tools\release.py bump %VERSION_INPUT%
if errorlevel 1 exit /b 1
goto :AFTER_VERSION

:SET_VERSION
echo [INFO] Setting version: %VERSION_INPUT%
python tools\release.py set %VERSION_INPUT%
if errorlevel 1 exit /b 1

:AFTER_VERSION
for /f %%V in ('python tools\release.py print') do set "NEW_VERSION=%%V"
if "%NEW_VERSION%"=="" (
  echo [ERROR] Failed to read updated version.
  exit /b 1
)

for /f %%B in ('git rev-parse --abbrev-ref HEAD') do set "CURRENT_BRANCH=%%B"
if "%CURRENT_BRANCH%"=="" (
  echo [ERROR] Failed to read current git branch.
  exit /b 1
)

git add addon_meta.json blender_manifest.toml
git commit -m "chore(release): v%NEW_VERSION%" -- addon_meta.json blender_manifest.toml
if errorlevel 1 (
  echo [ERROR] Git commit failed. Please check repository state.
  exit /b 1
)

git push
if errorlevel 1 (
  echo [ERROR] Git push failed.
  exit /b 1
)

gh workflow run release-addon.yml --ref "%CURRENT_BRANCH%"
if errorlevel 1 (
  echo [ERROR] Failed to trigger GitHub workflow.
  exit /b 1
)

echo [INFO] Release workflow triggered for v%NEW_VERSION% on branch %CURRENT_BRANCH%.
echo [INFO] Check latest run:
gh run list --workflow release-addon.yml --limit 1
exit /b 0
