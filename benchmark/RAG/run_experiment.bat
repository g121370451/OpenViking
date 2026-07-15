@echo off
setlocal enabledelayedexpansion

set "DATASET=%~1"
set "BUILD_COUNT=%~2"
set "STEP=%~3"
set "OV_CONF=%~4"

if "%DATASET%"=="" set "DATASET=hotpotqa"
if "%BUILD_COUNT%"=="" set "BUILD_COUNT=1"
if "%STEP%"=="" set "STEP=gen+eval"
if "%OV_CONF%"=="" set "OV_CONF=ov.conf"

set "CONFIG_DIR=config\%DATASET%"

cd /d "%~dp0"

echo ==========================================
echo  OpenViking Relations Experiment
echo  Dataset: %DATASET%
echo  Build count: %BUILD_COUNT%
echo  Step: %STEP%
echo  OV Config: %OV_CONF%
echo ==========================================

echo.
echo [1/5] Bot baseline: %DATASET%_bot_config.yaml
echo ------------------------------------------
uv run python run.py --config "%CONFIG_DIR%\%DATASET%_bot_config.yaml" --step %STEP% --ov-conf "%OV_CONF%"
if errorlevel 1 goto :error

echo.
echo [2/5] Generated questions build_links_review x%BUILD_COUNT%: %DATASET%_generated_questions_build_links_review.yaml
echo ------------------------------------------
for /l %%i in (1,1,%BUILD_COUNT%) do (
    echo   ^>^> Build round %%i / %BUILD_COUNT%
    uv run python run.py --config "%CONFIG_DIR%\%DATASET%_generated_questions_build_links_review.yaml" --step gen --ov-conf "%OV_CONF%"
    if errorlevel 1 goto :error
)

echo.
echo [3/5] Bot relations_review: %DATASET%_bot_config_relations_review.yaml
echo ------------------------------------------
uv run python run.py --config "%CONFIG_DIR%\%DATASET%_bot_config_relations_review.yaml" --step gen+eval --ov-conf "%OV_CONF%"
if errorlevel 1 goto :error

echo.
echo [4/5] OV fallback bot relations: %DATASET%_ov_fallback_bot_relations_config.yaml
echo ------------------------------------------
uv run python run.py --config "%CONFIG_DIR%\%DATASET%_ov_fallback_bot_relations_config.yaml" --step gen+eval --ov-conf "%OV_CONF%"
if errorlevel 1 goto :error

echo.
echo [5/5] OV fallback bot relations naive rule: %DATASET%_ov_fallback_bot_relations_naive_rule_config.yaml
echo ------------------------------------------
uv run python run.py --config "%CONFIG_DIR%\%DATASET%_ov_fallback_bot_relations_naive_rule_config.yaml" --step gen+eval --ov-conf "%OV_CONF%"
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
