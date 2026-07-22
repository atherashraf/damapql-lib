@echo off
setlocal EnableExtensions

set "REMOTE_NAME=damapql-lib"
set "REMOTE_BRANCH=main"
set "SUBTREE_PREFIX=app/damapql"
set "STASHED=0"

echo ========================================
echo DAMapQL Subtree Pull
echo ========================================
echo.

REM --------------------------------------------------
REM Locate and enter the Git repository root
REM --------------------------------------------------

for /f "delims=" %%I in ('git rev-parse --show-toplevel 2^>nul') do (
    set "REPO_ROOT=%%I"
)

if not defined REPO_ROOT (
    echo ERROR: This script is not running inside a Git repository.
    goto failed
)

pushd "%REPO_ROOT%"

echo Repository: %REPO_ROOT%
echo Subtree:    %SUBTREE_PREFIX%
echo Remote:     %REMOTE_NAME%/%REMOTE_BRANCH%
echo.

REM --------------------------------------------------
REM Validate subtree directory and remote
REM --------------------------------------------------

if not exist "%SUBTREE_PREFIX%" (
    echo ERROR: Subtree directory does not exist:
    echo        %REPO_ROOT%\%SUBTREE_PREFIX%
    goto failed_with_popd
)

git remote get-url "%REMOTE_NAME%" >nul 2>&1

if errorlevel 1 (
    echo ERROR: Git remote "%REMOTE_NAME%" is not configured.
    echo.
    echo Configure it using:
    echo git remote add %REMOTE_NAME% https://github.com/atherashraf/damapql-lib.git
    goto failed_with_popd
)

REM --------------------------------------------------
REM Detect tracked, staged, modified and untracked files
REM --------------------------------------------------

git status --porcelain | findstr . >nul

if not errorlevel 1 (
    echo Local changes detected.
    echo Creating a temporary stash...
    echo.

    git stash push -u -m "auto-stash before DAMapQL subtree pull"

    if errorlevel 1 (
        echo.
        echo ERROR: Could not stash the local changes.
        goto failed_with_popd
    )

    set "STASHED=1"
) else (
    echo Working tree is clean.
)

REM --------------------------------------------------
REM Pull the DAMapQL subtree
REM --------------------------------------------------

echo.
echo Pulling DAMapQL subtree...
echo.

git subtree pull ^
    --prefix="%SUBTREE_PREFIX%" ^
    "%REMOTE_NAME%" ^
    "%REMOTE_BRANCH%" ^
    --squash

if errorlevel 1 (
    echo.
    echo ERROR: DAMapQL subtree pull failed.

    if "%STASHED%"=="1" (
        echo.
        echo Your local changes remain safely stored in the Git stash.
        echo Resolve or abort the failed subtree operation first.
        echo Then restore your changes with:
        echo.
        echo     git stash pop
    )

    goto failed_with_popd
)

REM --------------------------------------------------
REM Restore temporarily stashed changes
REM --------------------------------------------------

if "%STASHED%"=="1" (
    echo.
    echo Restoring local changes...
    echo.

    git stash pop

    if errorlevel 1 (
        echo.
        echo WARNING: The subtree pull succeeded, but restoring the
        echo stashed changes produced conflicts.
        echo.
        echo Resolve the conflicts and commit the result.
        goto failed_with_popd
    )
)

echo.
echo ========================================
echo DAMapQL subtree pull completed
echo successfully.
echo ========================================

popd
pause
exit /b 0


:failed_with_popd
popd

:failed
echo.
echo Operation did not complete successfully.
pause
exit /b 1