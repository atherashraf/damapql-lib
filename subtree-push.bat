@echo off
setlocal

echo =============================
echo Subtree PUSH (damapql-lib)
echo =============================

echo.
echo Pushing subtree changes from app/damapql ...
git subtree push --prefix=app/damapql damapql-lib main

if errorlevel 1 (
    echo.
    echo Subtree push failed.
    pause
    exit /b 1
)

echo.
echo Done.
pause