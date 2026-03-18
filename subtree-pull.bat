@echo off
setlocal

echo =============================
echo Subtree PULL (damapql-lib)
echo =============================

REM Detect changes
git diff --quiet
if errorlevel 1 (
    echo.
    echo Working tree has changes. Stashing...
    git stash push -u -m "auto-stash before subtree pull"
    set STASHED=1
) else (
    set STASHED=0
)

echo.
echo Pulling subtree...
git subtree pull --prefix=app/damapql damapql-lib main --squash

if "%STASHED%"=="1" (
    echo.
    echo Restoring stashed changes...
    git stash pop
)

echo.
echo Done.
pause