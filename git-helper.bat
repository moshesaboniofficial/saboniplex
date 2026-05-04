@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
color 0B
title InStage - Git Helper

call :check_git
if errorlevel 1 goto end

:menu
cls
echo ==========================================
echo           InStage - Git Helper
echo ==========================================
echo.
echo Current folder:
cd
echo.
echo Current branch:
for /f "delims=" %%b in ('git branch --show-current 2^>nul') do set "CURRENT_BRANCH=%%b"
if not defined CURRENT_BRANCH set "CURRENT_BRANCH=main"
echo %CURRENT_BRANCH%
echo.
echo ------------------------------------------
echo Working tree summary:
echo ------------------------------------------
git status --short
echo.
echo ------------------------------------------
echo Changed files summary:
echo ------------------------------------------
git diff --name-only | more
echo.
echo ==========================================
echo Select an option:
echo ==========================================
echo 1. Refresh screen
echo 2. View full changes
echo 3. Git Pull
echo 4. Add + Commit
echo 5. Add + Commit + Push
echo 6. Pull + Commit + Push
echo 7. Push only
echo 8. View staged files
echo 9. Exit
echo ==========================================
echo.

set /p CHOICE=Enter your choice [1-9]: 

if "%CHOICE%"=="1" goto menu
if "%CHOICE%"=="2" goto diffview
if "%CHOICE%"=="3" goto pullonly
if "%CHOICE%"=="4" goto commitonly
if "%CHOICE%"=="5" goto commitpush
if "%CHOICE%"=="6" goto fullsync
if "%CHOICE%"=="7" goto pushonly
if "%CHOICE%"=="8" goto stagedview
if "%CHOICE%"=="9" goto end

echo.
echo Invalid choice.
pause
goto menu

:check_git
git --version >nul 2>&1
if errorlevel 1 (
    echo Git is not installed or not available in PATH.
    pause
    exit /b 1
)

git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 (
    echo This folder is not a Git repository.
    pause
    exit /b 1
)
exit /b 0

:get_branch
set "CURRENT_BRANCH="
for /f "delims=" %%b in ('git branch --show-current 2^>nul') do set "CURRENT_BRANCH=%%b"
if not defined CURRENT_BRANCH set "CURRENT_BRANCH=main"
exit /b 0

:pullonly
cls
call :get_branch
echo ==========================================
echo                Git Pull
echo ==========================================
echo.
echo Pulling from origin/%CURRENT_BRANCH% ...
echo.

git pull origin %CURRENT_BRANCH%
if errorlevel 1 (
    echo.
    echo Pull failed.
    pause
    goto menu
)

echo.
echo Pull completed successfully.
pause
goto menu

:diffview
cls
echo ==========================================
echo              Full Changes
echo ==========================================
echo.
git diff | more
echo.
pause
goto menu

:stagedview
cls
echo ==========================================
echo              Staged Files
echo ==========================================
echo.
git diff --cached --name-only | more
echo.
pause
goto menu

:commitonly
cls
echo ==========================================
echo              Add + Commit
echo ==========================================
echo.

git add .
if errorlevel 1 (
    echo.
    echo git add failed.
    pause
    goto menu
)

git diff --cached --quiet
if %errorlevel%==0 (
    echo.
    echo No staged changes to commit.
    pause
    goto menu
)

echo.
echo Files that will be committed:
git diff --cached --name-only | more
echo.

set /p MESSAGE=Enter commit message: 
if "%MESSAGE%"=="" (
    echo.
    echo Commit message cannot be empty.
    pause
    goto menu
)

git commit -m "%MESSAGE%"
if errorlevel 1 (
    echo.
    echo Commit failed.
    pause
    goto menu
)

echo.
echo Commit completed successfully.
pause
goto menu

:commitpush
cls
call :get_branch
echo ==========================================
echo          Add + Commit + Push
echo ==========================================
echo.

git add .
if errorlevel 1 (
    echo.
    echo git add failed.
    pause
    goto menu
)

git diff --cached --quiet
if %errorlevel%==0 (
    echo.
    echo No staged changes to commit.
    pause
    goto menu
)

echo Files that will be committed:
git diff --cached --name-only | more
echo.

set /p MESSAGE=Enter commit message: 
if "%MESSAGE%"=="" (
    echo.
    echo Commit message cannot be empty.
    pause
    goto menu
)

git commit -m "%MESSAGE%"
if errorlevel 1 (
    echo.
    echo Commit failed.
    pause
    goto menu
)

git push origin %CURRENT_BRANCH%
if errorlevel 1 (
    echo.
    echo Push failed.
    pause
    goto menu
)

echo.
echo Commit and push completed successfully.
pause
goto menu

:fullsync
cls
call :get_branch
echo ==========================================
echo         Pull + Commit + Push
echo ==========================================
echo.

echo Pulling latest changes from origin/%CURRENT_BRANCH% ...
git pull origin %CURRENT_BRANCH%
if errorlevel 1 (
    echo.
    echo Pull failed.
    pause
    goto menu
)

echo.
echo Staging changes...
git add .
if errorlevel 1 (
    echo.
    echo git add failed.
    pause
    goto menu
)

git diff --cached --quiet
if %errorlevel%==0 (
    echo.
    echo No new changes after pull.
    pause
    goto menu
)

echo.
echo Files that will be committed:
git diff --cached --name-only | more
echo.

set /p MESSAGE=Enter commit message: 
if "%MESSAGE%"=="" (
    echo.
    echo Commit message cannot be empty.
    pause
    goto menu
)

git commit -m "%MESSAGE%"
if errorlevel 1 (
    echo.
    echo Commit failed.
    pause
    goto menu
)

git push origin %CURRENT_BRANCH%
if errorlevel 1 (
    echo.
    echo Push failed.
    pause
    goto menu
)

echo.
echo Sync completed successfully.
pause
goto menu

:pushonly
cls
call :get_branch
echo ==========================================
echo               Push Only
echo ==========================================
echo.

git push origin %CURRENT_BRANCH%
if errorlevel 1 (
    echo.
    echo Push failed.
    pause
    goto menu
)

echo.
echo Push completed successfully.
pause
goto menu

:end
endlocal
exit