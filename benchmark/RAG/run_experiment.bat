@echo off
setlocal enabledelayedexpansion

set "DATASET=%~1"
set "BUILD_COUNT=%~2"
set "STEP=%~3"

if "%DATASET%"=="" set "DATASET=hotpotqa"
if "%BUILD_COUNT%"=="" set "BUILD_COUNT=1"
if "%STEP%"=="" set "STEP=gen+eval"

set "CONFIG_DIR=config\%DATASET%"

cd /d "%~dp0"

echo ==========================================
echo  OpenViking Relations Experiment
echo  Dataset: %DATASET%
echo  Build count: %BUILD_COUNT%
echo  Step: %STEP%
echo ==========================================

echo.
echo [1/7] Non-bot baseline: %DATASET%_config.yaml
echo ------------------------------------------
python run.py --config "%CONFIG_DIR%\%DATASET%_config.yaml" --step %STEP%
if errorlevel 1 goto :error

echo.
echo [2/7] Bot baseline: %DATASET%_bot_config.yaml
echo ------------------------------------------
python run.py --config "%CONFIG_DIR%\%DATASET%_bot_config.yaml" --step gen+eval
if errorlevel 1 goto :error

echo.
echo [3/7] Bot build_links_review x%BUILD_COUNT%: %DATASET%_bot_config_build_links_review.yaml
echo ------------------------------------------
for /l %%i in (1,1,%BUILD_COUNT%) do (
    echo   ^>^> Build round %%i / %BUILD_COUNT%
    python run.py --config "%CONFIG_DIR%\%DATASET%_bot_config_build_links_review.yaml" --step gen+eval
    if errorlevel 1 goto :error
)

echo.
echo [4/7] Non-bot relations_review: %DATASET%_config_relations_review.yaml
echo ------------------------------------------
python run.py --config "%CONFIG_DIR%\%DATASET%_config_relations_review.yaml" --step gen+eval
if errorlevel 1 goto :error

echo.
echo [5/7] Bot relations_review: %DATASET%_bot_config_relations_review.yaml
echo ------------------------------------------
python run.py --config "%CONFIG_DIR%\%DATASET%_bot_config_relations_review.yaml" --step gen+eval
if errorlevel 1 goto :error

echo.
echo [6/7] OV fallback bot: %DATASET%_ov_fallback_bot_config.yaml
echo ------------------------------------------
python run.py --config "%CONFIG_DIR%\%DATASET%_ov_fallback_bot_config.yaml" --step gen+eval
if errorlevel 1 goto :error

echo.
echo [7/7] OV fallback bot relations: %DATASET%_ov_fallback_bot_relations_config.yaml
echo ------------------------------------------
python run.py --config "%CONFIG_DIR%\%DATASET%_ov_fallback_bot_relations_config.yaml" --step gen+eval
if errorlevel 1 goto :error

echo.
echo ==========================================
echo  Experiment complete!
echo ==========================================
goto :end

:error
echo.
echo [ERROR] Step failed, aborting.
exit /b 1

:end
endlocal
