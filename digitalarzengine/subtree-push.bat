@echo off
setlocal

set "REMOTE=digitalarzengine"
set "BRANCH=main"
set "PREFIX=digitalarzengine"

echo ====================================
echo Subtree PUSH (%REMOTE%)
echo ====================================

REM Confirm that this is a Git repository.
git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 (
    echo.
    echo ERROR: This directory is not a Git repository.
    goto :failed
)

REM Confirm that the subtree directory exists.
if not exist "%PREFIX%\" (
    echo.
    echo ERROR: Subtree directory does not exist:
    echo %PREFIX%
    goto :failed
)

REM Confirm that the configured remote exists.
git remote get-url "%REMOTE%" >nul 2>&1
if errorlevel 1 (
    echo.
    echo ERROR: Git remote "%REMOTE%" is not configured.
    echo.
    echo Configure it using:
    echo git remote add %REMOTE% https://github.com/atherashraf/digitalarzengine.git
    goto :failed
)

REM Subtree push includes committed changes only.
git status --porcelain -- "%PREFIX%" | findstr /r "." >nul
if not errorlevel 1 (
    echo.
    echo ERROR: There are uncommitted changes inside:
    echo %PREFIX%
    echo.
    echo Commit them before pushing:
    echo git add "%PREFIX%"
    echo git commit -m "Update DigitalARZEngine"
    goto :failed
)

echo.
echo Pushing committed subtree changes...
echo Source: %PREFIX%
echo Target: %REMOTE%/%BRANCH%
echo.

git subtree push --prefix="%PREFIX%" "%REMOTE%" "%BRANCH%"
if errorlevel 1 (
    echo.
    echo ERROR: DigitalARZEngine subtree push failed.
    goto :failed
)

echo.
echo DigitalARZEngine subtree pushed successfully.
goto :finished

:failed
echo.
echo Operation finished with errors.
pause
exit /b 1

:finished
echo.
echo Done.
pause
exit /b 0