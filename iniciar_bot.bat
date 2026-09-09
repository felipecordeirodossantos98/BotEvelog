@echo off
setlocal EnableExtensions
cd /d "%~dp0"

title Bot Evelog

set "RAIZ=%CD%"
set "BOT_DIR=%RAIZ%\bot"
set "VENV_DIR=%RAIZ%\.venv"
set "PYTHON=%VENV_DIR%\Scripts\python.exe"
set "ERRO_ESTRUTURA="

echo ==========================================
echo               BOT EVELOG
echo ==========================================
echo.
echo Pasta do projeto:
echo %RAIZ%
echo.

rem ==================================================
rem 1. Verifica o Python
rem ==================================================

where python >nul 2>&1

if not errorlevel 1 (
    set "PYTHON_BASE=python"
    goto PYTHON_OK
)

where py >nul 2>&1

if not errorlevel 1 (
    set "PYTHON_BASE=py -3"
    goto PYTHON_OK
)

echo ERRO: Python nao foi encontrado.
echo.
echo Instale o Python 3.10 ou superior.
echo Durante a instalacao, marque a opcao:
echo Add Python to PATH
echo.
pause
exit /b 1

:PYTHON_OK

rem ==================================================
rem 2. Verifica Git e atualiza pelo GitHub
rem ==================================================

where git >nul 2>&1

if errorlevel 1 (
    echo AVISO: Git nao foi encontrado.
    echo O Bot Evelog sera iniciado sem procurar atualizacoes.
    echo.
    goto INSTALAR_AMBIENTE
)

git -C "%RAIZ%" rev-parse --is-inside-work-tree >nul 2>&1

if errorlevel 1 (
    echo AVISO: A pasta do Bot Evelog nao e um repositorio Git.
    echo O aplicativo sera iniciado sem procurar atualizacoes.
    echo.
    goto INSTALAR_AMBIENTE
)

echo ==========================================
echo Verificando atualizacoes no GitHub...
echo ==========================================
echo.

git -C "%RAIZ%" pull --ff-only

if errorlevel 1 (
    echo.
    echo AVISO: Nao foi possivel atualizar o projeto.
    echo.
    echo Possiveis motivos:
    echo - existem alteracoes locais;
    echo - computador sem internet;
    echo - acesso ao repositorio expirou;
    echo - branch local diferente da remota.
    echo.
    echo O Bot Evelog sera iniciado com a versao atual.
    echo.
) else (
    echo.
    echo Projeto atualizado com sucesso.
    echo.
)

:INSTALAR_AMBIENTE

rem ==================================================
rem 3. Cria ambiente virtual compartilhado
rem ==================================================

if not exist "%PYTHON%" (
    echo ==========================================
    echo Criando ambiente virtual...
    echo ==========================================
    echo.

    %PYTHON_BASE% -m venv "%VENV_DIR%"

    if errorlevel 1 (
        echo.
        echo ERRO: Nao foi possivel criar o ambiente virtual.
        echo.
        pause
        exit /b 1
    )

    echo Ambiente virtual criado em:
    echo %VENV_DIR%
    echo.
)

rem ==================================================
rem 4. Localiza o requirements.txt unificado
rem ==================================================

set "REQUIREMENTS="

if exist "%RAIZ%\requirements.txt" (
    set "REQUIREMENTS=%RAIZ%\requirements.txt"
)

if not defined REQUIREMENTS (
    if exist "%BOT_DIR%\requirements.txt" (
        set "REQUIREMENTS=%BOT_DIR%\requirements.txt"
    )
)

if not defined REQUIREMENTS (
    echo ERRO: requirements.txt nao foi encontrado.
    echo.
    echo O script procura em:
    echo %RAIZ%\requirements.txt
    echo ou
    echo %BOT_DIR%\requirements.txt
    echo.
    echo Crie um requirements.txt unificado com as dependencias
    echo de todos os fluxos do Bot Evelog.
    echo.
    pause
    exit /b 1
)

echo Requirements:
echo %REQUIREMENTS%
echo.

rem ==================================================
rem 5. Atualiza pip
rem ==================================================

echo ==========================================
echo Verificando pip...
echo ==========================================
echo.

"%PYTHON%" -m pip install --upgrade pip --quiet

if errorlevel 1 (
    echo.
    echo ERRO: Nao foi possivel atualizar o pip.
    echo.
    pause
    exit /b 1
)

echo Pip verificado.
echo.

rem ==================================================
rem 6. Instala / atualiza todas as dependencias
rem ==================================================

echo ==========================================
echo Verificando dependencias...
echo ==========================================
echo.

"%PYTHON%" -m pip install -r "%REQUIREMENTS%" --quiet

if errorlevel 1 (
    echo.
    echo ERRO: Nao foi possivel instalar as dependencias.
    echo.
    pause
    exit /b 1
)

"%PYTHON%" -m pip check >nul 2>&1

if errorlevel 1 (
    echo.
    echo AVISO: O pip encontrou conflito entre dependencias.
    echo Executando "pip check" para exibir os detalhes:
    echo.
    "%PYTHON%" -m pip check
    echo.
    pause
    exit /b 1
)

echo Dependencias verificadas.
echo.

rem ==================================================
rem 7. Instala / verifica Chromium do Playwright
rem ==================================================

echo ==========================================
echo Verificando Chromium do Playwright...
echo ==========================================
echo.

"%PYTHON%" -m playwright install chromium

if errorlevel 1 (
    echo.
    echo ERRO: Nao foi possivel instalar o Chromium do Playwright.
    echo.
    pause
    exit /b 1
)

echo Chromium verificado.
echo.

rem ==================================================
rem 8. Cria as pastas compartilhadas de resultados
rem ==================================================

echo ==========================================
echo Verificando pastas de resultados...
echo ==========================================
echo.

if not exist "%RAIZ%\resultados" mkdir "%RAIZ%\resultados"

if not exist "%RAIZ%\resultados\ordens_de_coleta" (
    mkdir "%RAIZ%\resultados\ordens_de_coleta"
)

if not exist "%RAIZ%\resultados\danfes" (
    mkdir "%RAIZ%\resultados\danfes"
)

if not exist "%RAIZ%\resultados\tickets_zendesk" (
    mkdir "%RAIZ%\resultados\tickets_zendesk"
)

if not exist "%RAIZ%\resultados\relatorios_performance" (
    mkdir "%RAIZ%\resultados\relatorios_performance"
)

if not exist "%RAIZ%\resultados\relatorios_analitico" (
    mkdir "%RAIZ%\resultados\relatorios_analitico"
)

if not exist "%RAIZ%\resultados\relatorios_analitico\bases_diarias" (
    mkdir "%RAIZ%\resultados\relatorios_analitico\bases_diarias"
)

if not exist "%RAIZ%\resultados\relatorios_analitico\.originais" (
    mkdir "%RAIZ%\resultados\relatorios_analitico\.originais"
)

if not exist "%RAIZ%\resultados\tdes" (
    mkdir "%RAIZ%\resultados\tdes"
)

echo Pastas de resultados verificadas.
echo.

rem ==================================================
rem 9. Cria as pastas compartilhadas de logs
rem ==================================================

echo ==========================================
echo Verificando pastas de logs...
echo ==========================================
echo.

if not exist "%RAIZ%\logs" mkdir "%RAIZ%\logs"

if not exist "%RAIZ%\logs\baixar_danfes" mkdir "%RAIZ%\logs\baixar_danfes"
if not exist "%RAIZ%\logs\baixar_relatorios_analitico" mkdir "%RAIZ%\logs\baixar_relatorios_analitico"
if not exist "%RAIZ%\logs\baixar_relatorios_performance" mkdir "%RAIZ%\logs\baixar_relatorios_performance"
if not exist "%RAIZ%\logs\buscar_tdes" mkdir "%RAIZ%\logs\buscar_tdes"
if not exist "%RAIZ%\logs\gerar_ordens_de_coleta" mkdir "%RAIZ%\logs\gerar_ordens_de_coleta"
if not exist "%RAIZ%\logs\gerar_tickets_zendesk" mkdir "%RAIZ%\logs\gerar_tickets_zendesk"

echo Pastas de logs verificadas.
echo.

rem ==================================================
rem 10. Cria o estado persistente local
rem ==================================================

echo ==========================================
echo Verificando estado persistente...
echo ==========================================
echo.

if not exist "%RAIZ%\estado" mkdir "%RAIZ%\estado"
if not exist "%RAIZ%\estado\gerar_tickets_zendesk" (
    mkdir "%RAIZ%\estado\gerar_tickets_zendesk"
)

echo Estado persistente verificado.
echo.

rem ==================================================
rem 11. Cria o unico perfil persistente do Chromium
rem ==================================================

echo ==========================================
echo Verificando perfil do Zendesk...
echo ==========================================
echo.

if not exist "%RAIZ%\perfis" mkdir "%RAIZ%\perfis"

if not exist "%RAIZ%\perfis\gerar_tickets_zendesk\chromium" (
    mkdir "%RAIZ%\perfis\gerar_tickets_zendesk\chromium"
)

echo Perfil do Zendesk verificado.
echo.

rem ==================================================
rem 12. Verifica a estrutura principal do Bot Evelog
rem ==================================================

echo ==========================================
echo Verificando arquivos do projeto...
echo ==========================================
echo.

call :EXIGIR_ARQUIVO "%BOT_DIR%\app.py" "bot\app.py"
call :EXIGIR_ARQUIVO "%BOT_DIR%\.env" "bot\.env"

rem ----- Estrutura V2: automações -----
call :EXIGIR_ARQUIVO "%BOT_DIR%\automacoes\gerar_ordens_de_coleta.py" "bot\automacoes\gerar_ordens_de_coleta.py"
call :EXIGIR_ARQUIVO "%BOT_DIR%\automacoes\gerar_tickets_zendesk.py" "bot\automacoes\gerar_tickets_zendesk.py"
call :EXIGIR_ARQUIVO "%BOT_DIR%\automacoes\baixar_danfes.py" "bot\automacoes\baixar_danfes.py"
call :EXIGIR_ARQUIVO "%BOT_DIR%\automacoes\baixar_relatorios_performance.py" "bot\automacoes\baixar_relatorios_performance.py"
call :EXIGIR_ARQUIVO "%BOT_DIR%\automacoes\buscar_tdes.py" "bot\automacoes\buscar_tdes.py"
call :EXIGIR_ARQUIVO "%BOT_DIR%\automacoes\baixar_relatorios_analitico.py" "bot\automacoes\baixar_relatorios_analitico.py"

rem ----- Estrutura V2: elementos visuais -----
call :EXIGIR_ARQUIVO "%BOT_DIR%\elementos_visuais\visual_gerar_ordens_de_coleta.py" "bot\elementos_visuais\visual_gerar_ordens_de_coleta.py"
call :EXIGIR_ARQUIVO "%BOT_DIR%\elementos_visuais\visual_gerar_tickets_zendesk.py" "bot\elementos_visuais\visual_gerar_tickets_zendesk.py"
call :EXIGIR_ARQUIVO "%BOT_DIR%\elementos_visuais\visual_baixar_danfes.py" "bot\elementos_visuais\visual_baixar_danfes.py"
call :EXIGIR_ARQUIVO "%BOT_DIR%\elementos_visuais\visual_baixar_relatorios_performance.py" "bot\elementos_visuais\visual_baixar_relatorios_performance.py"
call :EXIGIR_ARQUIVO "%BOT_DIR%\elementos_visuais\visual_buscar_tdes.py" "bot\elementos_visuais\visual_buscar_tdes.py"
call :EXIGIR_ARQUIVO "%BOT_DIR%\elementos_visuais\visual_baixar_relatorios_analitico.py" "bot\elementos_visuais\visual_baixar_relatorios_analitico.py"

rem ----- Estrutura V2: serviços e utilitários -----
call :EXIGIR_ARQUIVO "%BOT_DIR%\servicos\fraction.py" "bot\servicos\fraction.py"
call :EXIGIR_ARQUIVO "%BOT_DIR%\servicos\meudanfe.py" "bot\servicos\meudanfe.py"
call :EXIGIR_ARQUIVO "%BOT_DIR%\utils\config.py" "bot\utils\config.py"
call :EXIGIR_ARQUIVO "%BOT_DIR%\utils\arquivos.py" "bot\utils\arquivos.py"
call :EXIGIR_ARQUIVO "%BOT_DIR%\utils\interface.py" "bot\utils\interface.py"
call :EXIGIR_ARQUIVO "%BOT_DIR%\utils\logs.py" "bot\utils\logs.py"
call :EXIGIR_ARQUIVO "%BOT_DIR%\utils\estado.py" "bot\utils\estado.py"

rem ----- Dados fixos -----
call :EXIGIR_ARQUIVO "%BOT_DIR%\dados\gerar_ordens_de_coleta\base_cnpjs.json" "bot\dados\gerar_ordens_de_coleta\base_cnpjs.json"
call :EXIGIR_ARQUIVO "%BOT_DIR%\dados\gerar_ordens_de_coleta\emails_unidades.json" "bot\dados\gerar_ordens_de_coleta\emails_unidades.json"


if defined ERRO_ESTRUTURA (
    echo.
    echo ERRO: A estrutura do projeto esta incompleta.
    echo Corrija os arquivos indicados acima antes de iniciar.
    echo.
    pause
    exit /b 1
)

echo Estrutura do projeto verificada.
echo.

rem ==================================================
rem 13. Inicia o Bot Evelog
rem ==================================================

echo ==========================================
echo          Iniciando o Bot Evelog...
echo ==========================================
echo.
echo Para encerrar corretamente, pressione Ctrl+C.
echo Nao use Ctrl+Z.
echo.

set "PYTHONPATH=%BOT_DIR%;%PYTHONPATH%"

pushd "%BOT_DIR%"

"%PYTHON%" -m streamlit run app.py

set "STREAMLIT_EXIT=%ERRORLEVEL%"

popd

echo.
echo Bot Evelog encerrado.
echo.

if not "%STREAMLIT_EXIT%"=="0" (
    echo O Streamlit foi encerrado com codigo %STREAMLIT_EXIT%.
    echo.
)

pause
exit /b %STREAMLIT_EXIT%


rem ==================================================
rem Subrotina: verifica arquivo obrigatorio
rem ==================================================

:EXIGIR_ARQUIVO

if not exist "%~1" (
    echo ERRO: arquivo nao encontrado:
    echo %~2
    echo.
    set "ERRO_ESTRUTURA=1"
)

exit /b 0
