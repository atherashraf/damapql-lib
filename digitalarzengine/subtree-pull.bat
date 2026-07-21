@echo off
setlocal

set "REMOTE=digitalarzengine"
set "BRANCH=main"
set "PREFIX=digitalarzengine"
set "STASHED=0"

echo ====================================
echo Subtree PULL (%REMOTE%)
echo ====================================

REM Confirm that this is a Git repository.
git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 (
    echo.
    echo ERROR: This directory is not a Git repository.
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

REM Detect staged, unstaged, and untracked changes.
git status --porcelain | findstr /r "." >nul
if not errorlevel 1 (
    echo.
    echo Working tree has changes. Stashing them...

    git stash push -u -m "auto-stash before digitalarzengine subtree pull"
    if errorlevel 1 (
        echo.
        echo ERROR: Could not stash local changes.
        goto :failed
    )

    set "STASHED=1"
) else (
    echo.
    echo Working tree is clean.
)

echo.
echo Fetching %REMOTE%...
git fetch "%REMOTE%"
if errorlevel 1 (
    echo.
    echo ERROR: Could not fetch the remote repository.
    goto :restore_and_fail
)

echo.
echo Pulling subtree updates...
echo Source: %REMOTE%/%BRANCH%
echo Target: %PREFIX%
echo.

git subtree pull --prefix="%PREFIX%" "%REMOTE%" "%BRANCH%" --squash
set "PULL_RESULT=%ERRORLEVEL%"

if "%STASHED%"=="1" (
    echo.
    echo Restoring stashed changes...

    git stash pop --index
    if errorlevel 1 (
        echo.
        echo WARNING: The stash could not be restored cleanly.
        echo Resolve the conflicts manually.
        goto :failed
    )

    set "STASHED=0"
)

if not "%PULL_RESULT%"=="0" (
    echo.
    echo ERROR: Subtree pull failed.
    goto :failed
)

echo.
echo DigitalARZEngine subtree updated successfully.
goto :finished

:restore_and_fail
if "%STASHED%"=="1" (
    echo.
    echo Restoring stashed changes...
    git stash pop --index

    if errorlevel 1 (
        echo.
        echo WARNING: The stash could not be restored cleanly.
        echo Resolve the conflicts manually.
    )
)

goto :failed

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