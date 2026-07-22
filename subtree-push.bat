@echo off
setlocal EnableExtensions

set "REMOTE_NAME=damapql-lib"
set "REMOTE_BRANCH=main"
set "SUBTREE_PREFIX=app/damapql"

echo ========================================
echo DAMapQL Subtree Push
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
REM Validate subtree directory
REM --------------------------------------------------

if not exist "%SUBTREE_PREFIX%" (
    echo ERROR: Subtree directory does not exist:
    echo        %REPO_ROOT%\%SUBTREE_PREFIX%
    goto failed_with_popd
)

REM --------------------------------------------------
REM Validate subtree remote
REM --------------------------------------------------

git remote get-url "%REMOTE_NAME%" >nul 2>&1

if errorlevel 1 (
    echo ERROR: Git remote "%REMOTE_NAME%" is not configured.
    echo.
    echo Configure it using:
    echo.
    echo git remote add %REMOTE_NAME% https://github.com/atherashraf/damapql-lib.git
    echo git fetch %REMOTE_NAME%
    echo.
    goto failed_with_popd
)

REM --------------------------------------------------
REM Check for uncommitted DAMapQL changes
REM --------------------------------------------------

git status --porcelain -- "%SUBTREE_PREFIX%" | findstr . >nul

if not errorlevel 1 (
    echo ERROR: The DAMapQL subtree contains uncommitted changes.
    echo.
    echo A subtree push only includes committed changes.
    echo Review and commit them first:
    echo.
    echo     git status -- "%SUBTREE_PREFIX%"
    echo     git add "%SUBTREE_PREFIX%"
    echo     git commit -m "Update DAMapQL"
    echo.
    goto failed_with_popd
)

echo DAMapQL subtree has no uncommitted changes.

REM --------------------------------------------------
REM Show the commit being used
REM --------------------------------------------------

echo.
echo Current host repository commit:
git log -1 --oneline

if errorlevel 1 (
    echo.
    echo ERROR: Unable to read the current Git commit.
    goto failed_with_popd
)

REM --------------------------------------------------
REM Push subtree history to the standalone repository
REM --------------------------------------------------

echo.
echo Pushing DAMapQL subtree...
echo.

git subtree push ^
    --prefix="%SUBTREE_PREFIX%" ^
    "%REMOTE_NAME%" ^
    "%REMOTE_BRANCH%"

if errorlevel 1 (
    echo.
    echo ERROR: DAMapQL subtree push failed.
    echo.
    echo Possible reasons:
    echo   - The remote contains newer commits.
    echo   - Authentication failed.
    echo   - The remote URL is incorrect.
    echo   - The remote branch rejected the update.
    echo.
    echo If the remote contains newer changes, run the subtree
    echo pull script first, resolve any conflicts, commit, and
    echo then run this push script again.
    goto failed_with_popd
)

echo.
echo ========================================
echo DAMapQL subtree push completed
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