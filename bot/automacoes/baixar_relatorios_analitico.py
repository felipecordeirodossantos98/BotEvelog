from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
import re

import pandas as pd
import xlsxwriter
from playwright.sync_api import (
    TimeoutError,
    sync_playwright,
)

from utils.config import (
    BASES_DIARIAS_ANALITICO,
    FRACTION_PASSWORD_ANALYTIC,
    FRACTION_USER_ANALYTIC,
    HEADLESS,
    ORIGINAIS_ANALITICO,
    RESULTADOS_ANALITICO,
    SLOW_MO_MS,
    URL_FRACTION,
)


# Configuração centralizada em bot/utils/config.py.
FRACTION_USER = FRACTION_USER_ANALYTIC
FRACTION_PASSWORD = FRACTION_PASSWORD_ANALYTIC

RESULTADOS_PATH = str(RESULTADOS_ANALITICO)
ORIGINAIS_PATH = str(ORIGINAIS_ANALITICO)
BASES_DIARIAS_PATH = str(BASES_DIARIAS_ANALITICO)

ORIGINAIS = Path(ORIGINAIS_PATH)
ORIGINAIS.mkdir(parents=True, exist_ok=True)

# ==========================================================
# CONFIGURAÇÃO
# ==========================================================

BASES = Path(ORIGINAIS_PATH)
BASES.mkdir(parents=True, exist_ok=True)

MAX_TENTATIVAS_EXTRACAO_DIA = 5


def _log(
    log,
    mensagem: str,
) -> None:
    if log is None:
        print(mensagem)
    else:
        log(mensagem)


def recuperar_pagina_analitico(
    page,
    log=None,
) -> None:
    """
    Recupera a navegação sem refazer o login.

    É o equivalente ao F5:
    atualiza a página atual, mantém o mesmo contexto/sessão
    e permite repetir a navegação do relatório.
    """
    _log(
        log,
        "Atualizando a página para recuperar a navegação..."
    )

    try:
        page.reload(
            wait_until="domcontentloaded"
        )

    except TimeoutError as erro:
        _log(
            log,
            "A atualização da página também atingiu timeout: "
            f"{erro}"
        )

    # Executado somente no caminho de recuperação.
    page.wait_for_timeout(1000)


# ==========================================================
# LOGIN
# ==========================================================

def login(page, token, log=None):

    _log(log, "Realizando login...")

    page.goto(URL_FRACTION)

    page.get_by_role(
        "textbox",
        name="Usuário"
    ).fill(FRACTION_USER)

    page.get_by_role(
        "textbox",
        name="Senha"
    ).fill(FRACTION_PASSWORD)

    page.get_by_role(
        "button",
        name="Login"
    ).click()

    page.get_by_role(
        "textbox",
        name="Digite o código MFA:"
    ).wait_for(timeout=60000)

    page.get_by_role(
        "textbox",
        name="Digite o código MFA:"
    ).fill(token)

    page.get_by_role(
        "button",
        name="Validar"
    ).click()

    page.get_by_role(
        "link",
        name="Financeiro"
    ).wait_for(timeout=60000)

    _log(log, "Login concluído.")


# ==========================================================
# ABRIR RELATÓRIO
# ==========================================================

def abrir_relatorio(page, log=None):

    _log(log, "Abrindo relatório...")

    page.get_by_role(
        "link",
        name="Financeiro"
    ).click()

    page.get_by_role(
        "link",
        name="Faturamento"
    ).click()

    page.get_by_role(
        "link",
        name="Relatórios"
    ).click()

    page.get_by_text(
        "Analítico"
    ).click()

    page.wait_for_load_state("networkidle")

    page.locator(
        '[id="form_relatorios:id_data_ini_input"]'
    ).wait_for()

    _log(log, "Tela do relatório aberta.")


# ==========================================================
# PREENCHER DATA
# ==========================================================

def preencher_data(page, data, log=None):

    texto = data.strftime("%d/%m/%Y")

    _log(log, f"Preenchendo {texto}")

    inicio = page.locator(
        '[id="form_relatorios:id_data_ini_input"]'
    )

    fim = page.locator(
        '[id="form_relatorios:id_data_fim_input"]'
    )

    inicio.click()
    inicio.clear()
    inicio.fill(texto)
    inicio.press("Tab")

    fim.click()
    fim.clear()
    fim.fill(texto)
    fim.press("Tab")

    page.wait_for_timeout(500)


# ==========================================================
# PROCESSAR
# ==========================================================

def processar(page, log=None):

    _log(log, "Processando relatório...")

    botao = page.get_by_role(
        "button",
        name="Processar"
    )

    botao.wait_for(timeout=60000)

    botao.click()

    page.wait_for_load_state("networkidle")

    exportar = page.get_by_role(
        "button",
        name=" Exportar para Excel"
    )

    # Mesmo timeout da versão original que foi validada pelo usuário:
    # 10 minutos como limite máximo para o sistema gerar o relatório.
    exportar.wait_for(timeout=600000)

    _log(log, "Processamento concluído.")


# ==========================================================
# DOWNLOAD
# ==========================================================

def baixar(page, data, log=None):

    nome = f"Analítico_{data.strftime('%d-%m-%Y')}.xlsx"

    destino = BASES / nome

    _log(log, f"Baixando {nome}")

    # Mesmo timeout da versão original que foi validada pelo usuário.
    with page.expect_download(timeout=600000) as download:

        page.get_by_role(
            "button",
            name=" Exportar para Excel"
        ).click(
            force=True,
            no_wait_after=True
        )

    download = download.value

    download.save_as(destino)

    page.wait_for_timeout(1000)

    _log(log, "Download concluído.")

    return str(destino)


# ==========================================================
# EXECUÇÃO
# ==========================================================

def executar(
    data_inicial,
    data_final,
    token,
    callback=None,
    arquivo_callback=None,
    log=None,
):

    arquivos = []

    total = (data_final - data_inicial).days + 1

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=HEADLESS,
            slow_mo=SLOW_MO_MS,
        )

        context = browser.new_context(
            accept_downloads=True
        )

        page = context.new_page()

        try:

            # -----------------------------
            # LOGIN (APENAS UMA VEZ)
            # -----------------------------
            login(page, token, log=log)

            data = data_inicial
            contador = 1

            while data <= data_final:

                _log(log, "=" * 60)
                _log(log, f"Extraindo {data.strftime('%d/%m/%Y')}")
                _log(log, "=" * 60)

                if callback:

                    callback(
                        contador,
                        total,
                        data
                    )

                tentativa = 1

                while tentativa <= MAX_TENTATIVAS_EXTRACAO_DIA:

                    try:

                        if tentativa > 1:
                            _log(
                                log,
                                "Nova tentativa para "
                                f"{data.strftime('%d/%m/%Y')} "
                                f"({tentativa}/{MAX_TENTATIVAS_EXTRACAO_DIA}).",
                            )

                        # -----------------------------------
                        # REABRE A TELA DO RELATÓRIO
                        # -----------------------------------

                        abrir_relatorio(page, log=log)

                        # -----------------------------------
                        # PREENCHE A DATA
                        # -----------------------------------

                        preencher_data(
                            page,
                            data,
                            log=log,
                        )

                        # -----------------------------------
                        # PROCESSA
                        # -----------------------------------

                        processar(page, log=log)

                        # -----------------------------------
                        # DOWNLOAD
                        # -----------------------------------

                        arquivo = baixar(
                            page,
                            data,
                            log=log,
                        )

                        arquivos.append(arquivo)

                        # A limpeza é iniciada assim que o download termina,
                        # mas a extração segue seu loop original normalmente.
                        if arquivo_callback:
                            arquivo_callback(
                                arquivo,
                                contador,
                                total,
                                data,
                            )

                        _log(
                            log,
                            f"✔ {data.strftime('%d/%m/%Y')} concluído.",
                        )

                        break

                    except TimeoutError as erro:

                        _log(
                            log,
                            "Timeout na data "
                            f"{data.strftime('%d/%m/%Y')} "
                            f"(tentativa {tentativa}/"
                            f"{MAX_TENTATIVAS_EXTRACAO_DIA}).",
                        )

                        if (
                            tentativa
                            >= MAX_TENTATIVAS_EXTRACAO_DIA
                        ):
                            _log(
                                log,
                                f"Erro na data {data.strftime('%d/%m/%Y')}",
                            )

                            raise Exception(
                                "Falha ao extrair "
                                f"{data.strftime('%d/%m/%Y')} "
                                "após "
                                f"{MAX_TENTATIVAS_EXTRACAO_DIA} tentativas."
                                f"\n\n{erro}"
                            )

                        recuperar_pagina_analitico(
                            page,
                            log=log,
                        )

                        tentativa += 1

                # -----------------------------------
                # PEQUENA PAUSA
                # -----------------------------------

                page.wait_for_timeout(1000)

                data += timedelta(days=1)

                contador += 1

        finally:

            _log(log, "Encerrando navegador...")

            context.close()

            browser.close()

    return arquivos


ID_USER_ALVO = "129948"
REMETENTE_EXCLUIR = "L OREAL BRASIL COMERCIAL DE COSMETICOS LTDA"
STATUS_EXCLUIR = "TRAVADO"
PONTO_ATUAL_EXCLUIR = "TC EMISSAO TECA"
PONTO_FINAL_EXCLUIR = "TC EMISSAO TECA"


BASES_DIARIAS = Path(BASES_DIARIAS_PATH)
BASES_DIARIAS.mkdir(parents=True, exist_ok=True)


def _normalizar_texto(serie):
    return (
        serie
        .fillna("")
        .astype(str)
        .str.strip()
        .str.replace(r"\s+", " ", regex=True)
        .str.upper()
    )


def _normalizar_id_user(serie):
    return (
        serie
        .fillna("")
        .astype(str)
        .str.strip()
        .str.replace(r"\.0$", "", regex=True)
    )


def _localizar_coluna(df, nome_esperado, arquivo):
    mapa = {
        str(coluna).strip().upper(): coluna
        for coluna in df.columns
    }

    chave = nome_esperado.strip().upper()

    if chave not in mapa:
        raise ValueError(
            f"A coluna '{nome_esperado}' não foi encontrada em "
            f"'{arquivo.name}'."
        )

    return mapa[chave]


def _ler_excel(arquivo):
    # openpyxl é mantido para não mudar o comportamento das planilhas
    # que já estão sendo geradas pelo sistema.
    return pd.read_excel(
        arquivo,
        engine="openpyxl",
    )


def filtrar_dataframe(df, arquivo):
    """
    Aplica as regras na ordem definida:

    1. Mantém somente ID_USER = 129948.
    2. Remove REMETENTE = L OREAL BRASIL COMERCIAL DE COSMETICOS LTDA.
    3. Remove somente quando STATUS = TRAVADO e
       PONTO_ATUAL = TC EMISSAO TECA simultaneamente.
    """
    coluna_id = _localizar_coluna(
        df, "ID_USER", arquivo
    )
    coluna_remetente = _localizar_coluna(
        df, "REMETENTE", arquivo
    )
    coluna_status = _localizar_coluna(
        df, "STATUS", arquivo
    )
    coluna_ponto = _localizar_coluna(
        df, "PONTO_ATUAL", arquivo
    )
    coluna_unidade = _localizar_coluna(
        df, "UNIDADE_DESTINO", arquivo
    )

    # 1. Primeiro deixa apenas o ID_USER desejado.
    id_user = _normalizar_id_user(
        df[coluna_id]
    )

    mascara_id = id_user.eq(ID_USER_ALVO)

    removidas_id_user = int((~mascara_id).sum())
    df = df.loc[mascara_id].copy()

    # 2. Depois remove o remetente específico.
    remetente = _normalizar_texto(
        df[coluna_remetente]
    )

    mascara_remetente = remetente.eq(
        REMETENTE_EXCLUIR
    )

    removidas_remetente = int(
        mascara_remetente.sum()
    )

    df = df.loc[~mascara_remetente].copy()

    # 3. Por fim remove somente a combinação das duas condições.
    status = _normalizar_texto(
        df[coluna_status]
    )

    ponto_atual = _normalizar_texto(
        df[coluna_ponto]
    )

    ponto_final = _normalizar_texto(
        df[coluna_unidade]
    )

    mascara_status_ponto = (
        status.eq(STATUS_EXCLUIR)
        & ponto_atual.eq(PONTO_ATUAL_EXCLUIR)
        & ponto_final.eq(PONTO_FINAL_EXCLUIR)
    )

    removidas_status_ponto = int(
        mascara_status_ponto.sum()
    )

    df = df.loc[
        ~mascara_status_ponto
    ].copy()

    # ======================================================
    # 4. REMOVER LINHAS DUPLICADAS
    # ======================================================

    antes_duplicadas = len(df)

    df = df.drop_duplicates().copy()

    removidas_duplicadas = (
        antes_duplicadas - len(df)
    )

    return df, {
        "removidas_id_user": removidas_id_user,
        "removidas_remetente": removidas_remetente,
        "removidas_status_ponto": removidas_status_ponto,
        "removidas_duplicadas": removidas_duplicadas,
        "linhas_finais": len(df),
    }


def limpar_base(
    arquivo_original,
    arquivo_filtrado=None,
):
    """Lê uma base bruta, aplica a limpeza e salva uma base diária."""
    arquivo_original = Path(arquivo_original)

    if not arquivo_original.exists():
        raise FileNotFoundError(
            f"Arquivo original não encontrado: {arquivo_original}"
        )

    if arquivo_filtrado is None:
        arquivo_filtrado = (
            BASES_DIARIAS / arquivo_original.name
        )
    else:
        arquivo_filtrado = Path(arquivo_filtrado)

    arquivo_filtrado.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    df = _ler_excel(arquivo_original)
    linhas_originais = len(df)

    df_limpo, resumo = filtrar_dataframe(
        df,
        arquivo_original,
    )

    # Mantém o cabeçalho uma única vez nesta base diária.
    df_limpo.to_excel(
        arquivo_filtrado,
        index=False,
        engine="openpyxl",
    )

    resultado = {
        "arquivo_original": str(arquivo_original),
        "arquivo_filtrado": str(arquivo_filtrado),
        "linhas_originais": linhas_originais,
        "removidas_id_user": resumo["removidas_id_user"],
        "removidas_remetente": resumo["removidas_remetente"],
        "removidas_status_ponto": resumo["removidas_status_ponto"],
        "linhas_finais": resumo["linhas_finais"],
        "linhas_removidas_total": (
            resumo["removidas_id_user"]
            + resumo["removidas_remetente"]
            + resumo["removidas_status_ponto"]
        ),
    }

    del df
    del df_limpo

    return resultado


RESULTADOS = Path(RESULTADOS_PATH)
RESULTADOS.mkdir(parents=True, exist_ok=True)


def _valor_excel(valor):
    if pd.isna(valor):
        return None

    if isinstance(valor, pd.Timestamp):
        return valor.to_pydatetime()

    return valor


def unificar_bases(
    arquivos,
    data_inicial,
    data_final,
    log=None,
):
    """
    Unifica somente as bases diárias recebidas.

    O processamento é feito uma planilha por vez e a saída é escrita
    linha por linha com XlsxWriter em constant_memory, evitando manter
    todas as bases em um único DataFrame.
    """
    caminhos = [Path(arquivo) for arquivo in arquivos]

    if not caminhos:
        raise ValueError(
            "Nenhuma base diária foi fornecida para unificação."
        )

    agora = datetime.now()

    nome_final = (
        "Analítico_"
        f"{data_inicial.strftime('%d-%m-%Y')}_a_"
        f"{data_final.strftime('%d-%m-%Y')}_"
        "Extraido_"
        f"{agora.strftime('%d-%m-%Y_%H-%M-%S')}.xlsx"
    )

    destino = RESULTADOS / nome_final
    temporario = RESULTADOS / f".{nome_final}.tmp.xlsx"

    workbook = None
    worksheet = None
    linha_atual = 0
    primeira_planilha = True
    colunas_referencia = None

    estatisticas = {
        "arquivos": 0,
        "linhas_finais": 0,
    }

    try:
        workbook = xlsxwriter.Workbook(
            temporario,
            {
                "constant_memory": True,
            },
        )

        worksheet = workbook.add_worksheet("Analítico")

        for caminho in caminhos:
            if not caminho.exists():
                raise FileNotFoundError(
                    f"Base diária não encontrada: {caminho}"
                )

            _log(log, f"Unificando {caminho.name}...")

            df = pd.read_excel(
                caminho,
                engine="openpyxl",
            )

            colunas = list(df.columns)

            if colunas_referencia is None:
                colunas_referencia = colunas
            elif colunas != colunas_referencia:
                raise ValueError(
                    "O layout da base "
                    f"'{caminho.name}' é diferente do primeiro arquivo."
                )

            if primeira_planilha:
                for coluna, nome in enumerate(colunas):
                    worksheet.write(
                        linha_atual,
                        coluna,
                        nome,
                    )

                linha_atual += 1
                primeira_planilha = False

            for valores in df.itertuples(
                index=False,
                name=None,
            ):
                for coluna, valor in enumerate(valores):
                    worksheet.write(
                        linha_atual,
                        coluna,
                        _valor_excel(valor),
                    )

                linha_atual += 1
                estatisticas["linhas_finais"] += 1

            estatisticas["arquivos"] += 1

            del df

        workbook.close()
        workbook = None

        temporario.replace(destino)

    except Exception:
        if workbook is not None:
            try:
                workbook.close()
            except Exception:
                pass

        if temporario.exists():
            temporario.unlink()

        raise

    return {
        "arquivo_final": str(destino),
        **estatisticas,
    }


PADRAO_BASE = re.compile(
    r"^Analítico_(\d{2}-\d{2}-\d{4})\.xlsx$",
    re.IGNORECASE,
)


def executar_extracao_e_filtragem(
    data_inicial,
    data_final,
    token,
    atualizar_extracao=None,
    atualizar_tratamento=None,
    log=None,
):
    arquivos_filtrados = []
    futuros = []

    executor = ThreadPoolExecutor(max_workers=1)

    try:
        def callback_extracao(atual, total_dias, data):
            if atualizar_extracao:
                atualizar_extracao(atual, total_dias, data)

        def callback_arquivo(
            arquivo_original,
            atual,
            total_dias,
            data,
        ):
            if atualizar_tratamento:
                atualizar_tratamento(
                    atual,
                    total_dias,
                    f"Filtrando {Path(arquivo_original).name}",
                )

            futuro = executor.submit(
                limpar_base,
                arquivo_original,
            )
            futuros.append(futuro)

        arquivos_originais = executar(
            data_inicial=data_inicial,
            data_final=data_final,
            token=token,
            callback=callback_extracao,
            arquivo_callback=callback_arquivo,
            log=log,
        )

        for futuro in futuros:
            resultado = futuro.result()
            arquivos_filtrados.append(
                resultado["arquivo_filtrado"]
            )

            _log(
                log,
                "Base diária preparada: "
                f"{resultado['arquivo_filtrado']}",
            )

        # Só remove os originais quando todos os downloads e filtros
        # do período terminaram com sucesso.
        for arquivo in arquivos_originais:
            caminho = Path(arquivo)
            if caminho.exists():
                caminho.unlink()

        ORIGINAIS.mkdir(parents=True, exist_ok=True)

        return {
            "arquivos_originais": arquivos_originais,
            "arquivos_filtrados": arquivos_filtrados,
        }

    finally:
        executor.shutdown(wait=True)


def listar_bases_diarias():
    bases = []

    for arquivo in BASES_DIARIAS.glob("Analítico_*.xlsx"):
        match = PADRAO_BASE.match(arquivo.name)

        if not match:
            continue

        try:
            data_base = date.fromisoformat(
                "-".join(reversed(match.group(1).split("-")))
            )
        except ValueError:
            continue

        bases.append((data_base, arquivo))

    bases.sort(key=lambda item: item[0])
    return bases
