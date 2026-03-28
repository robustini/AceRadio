@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

if exist "%~dp0.venv\Scripts\activate.bat" (
  call "%~dp0.venv\Scripts\activate.bat"
  set "VENV_DIR=%~dp0.venv"
) else if exist "%~dp0venv\Scripts\activate.bat" (
  call "%~dp0venv\Scripts\activate.bat"
  set "VENV_DIR=%~dp0venv"
) else (
  set "VENV_DIR="
)

if defined VENV_DIR (
  set "PY=%VENV_DIR%\Scripts\python.exe"
) else (
  set "PY=python"
)
if not exist "%PY%" set "PY=python"

set "PYTHONNOUSERSITE=1"
set "PYTHONHOME="
set "PYTHONPATH="
set "PYTORCH_ALLOC_CONF=expandable_segments:True"
set "CUDA_MODULE_LOADING=LAZY"

set "TORCH_LIB="
if defined VENV_DIR set "TORCH_LIB=%VENV_DIR%\Lib\site-packages\torch\lib"
if defined TORCH_LIB if exist "%TORCH_LIB%" set "PATH=%TORCH_LIB%;%PATH%"
if defined VENV_DIR if exist "%VENV_DIR%\Scripts" set "PATH=%VENV_DIR%\Scripts;%PATH%"

set "CUDA_BIN=C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v13.0\bin"
if exist "%CUDA_BIN%" set "PATH=%CUDA_BIN%;%PATH%"

set PORT=7862
set SERVER_NAME=0.0.0.0

set "ACESTEP_REMOTE_CONFIG_PATH=acestep-v15-turbo"
set "ACESTEP_REMOTE_LM_MODEL_PATH=acestep-5Hz-lm-4B"
set "ACESTEP_REMOTE_DEVICE=auto"
set "ACESTEP_REMOTE_RESULTS_DIR=%~dp0aceradio_outputs"
set "ACESTEP_REMOTE_LM_BACKEND=vllm"
set "ACESTEP_REMOTE_COMPILE_MODEL=0"
set "ACESTEP_REMOTE_USE_FLASH_ATTENTION=1"
set "ACESTEP_REMOTE_OFFLOAD_TO_CPU=0"
set "ACESTEP_REMOTE_OFFLOAD_DIT_TO_CPU=0"
set "ACESTEP_REMOTE_INT8_QUANTIZATION=0"
set "ACESTEP_REMOTE_USE_MLX_DIT=0"
set "ACESTEP_REMOTE_LM_OFFLOAD_TO_CPU=0"
set "ACESTEP_REMOTE_INIT_LLM=1"

set "ACERADIO_AUTH_ENABLED=0"
set "ACERADIO_SESSION_SECURE=0"
REM set "ACERADIO_AUTH_ENABLED=1"
REM set "ACERADIO_SESSION_SECURE=1"
REM set "ACERADIO_USERNAME=admin"
REM set "ACERADIO_PASSWORD=change-me"
set "ACERADIO_BYPASS_CORE_TURBO_STEP_CLAMP=1"
set "ACERADIO_CLEANUP_TTL_SECONDS=0"

REM set "ACERADIO_OLLAMA_MODEL=qwen3.5:4b"
REM set "ACERADIO_OLLAMA_MODEL=deepseek-r1:8b"
set "ACERADIO_OLLAMA_MODEL=qwen3.5:9b"
set "ACERADIO_OLLAMA_BASE_URL=http://127.0.0.1:11434"
set "ACERADIO_AUDIO_FORMAT=mp3"

echo Starting AceRadio UI...
echo http://%SERVER_NAME%:%PORT%
echo [ACE] PY=%PY% ^| CFG=%ACESTEP_REMOTE_CONFIG_PATH% ^| LM=%ACESTEP_REMOTE_LM_MODEL_PATH% ^| LM_BACKEND=%ACESTEP_REMOTE_LM_BACKEND% ^| COMPILE=%ACESTEP_REMOTE_COMPILE_MODEL%
echo [OLLAMA] %ACERADIO_OLLAMA_BASE_URL% ^| MODEL=%ACERADIO_OLLAMA_MODEL%
echo.

"%PY%" -m acestep.ui.aceradio.run --host %SERVER_NAME% --port %PORT%

pause
endlocal
