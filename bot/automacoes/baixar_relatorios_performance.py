import re
from datetime import datetime

import pandas as pd
from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from servicos.fraction import (
    executar_com_retentativas_fraction,
    recuperar_sessao_fraction,
)
from utils.arquivos import salvar_planilha_reimportacao

from utils.config import (
    FRACTION_PASSWORD,
    FRACTION_USER,
    HEADLESS,
    RESULTADOS_PERFORMANCE,
    URL_FRACTION,
    variaveis_faltantes,
)


RESULTADOS_DIR = RESULTADOS_PERFORMANCE
LIMITE_POR_CONSULTA = 1000


def validar_env():
    faltantes = variaveis_faltantes(
        "baixar_relatorios_performance"
    )

    if faltantes:
        raise RuntimeError(
            "Variáveis não configuradas no .env: "
            + ", ".join(faltantes)
        )


def normalizar_codigo(valor):
    if pd.isna(valor):
        return ""

    if isinstance(valor, float) and valor.is_integer():
        return str(int(valor))

    texto = str(valor).strip()

    # Evita transformar códigos alfanuméricos.
    if re.fullmatch(r"\d+\.0", texto):
        return texto[:-2]

    return texto


def localizar_coluna(df, nome):
    for coluna in df.columns:
        if str(coluna).strip().upper() == nome.upper():
            return coluna
    return None


def preparar_base_performance(arquivo):
    """
    Aplica exatamente os mesmos filtros do fluxo e também preserva as
    linhas originais para uma eventual planilha de reimportação.
    """
    arquivo.seek(0)
    df = pd.read_excel(
        arquivo,
        dtype=object,
    )
    arquivo.seek(0)

    if df.columns.empty:
        raise ValueError("A planilha não possui cabeçalho.")

    primeira_coluna = (
        str(df.columns[0])
        .strip()
        .upper()
    )

    coluna_status = localizar_coluna(
        df,
        "STATUS",
    )

    if primeira_coluna == "DATA":
        coluna_codigo = localizar_coluna(
            df,
            "CTE",
        )

        if coluna_codigo is None:
            raise ValueError(
                "A planilha com A1 = DATA precisa possuir a coluna CTE."
            )

        if coluna_status is not None:
            status = (
                df[coluna_status]
                .fillna("")
                .astype(str)
                .str.strip()
                .str.upper()
            )

            df = df[
                ~status.eq("ENTREGUE")
            ].copy()

        nome_coluna_codigo = "CTE"

    else:
        coluna_codigo = localizar_coluna(
            df,
            "REMESSA",
        )

        if coluna_codigo is None:
            raise ValueError(
                "A planilha precisa possuir a coluna REMESSA."
            )

        if coluna_status is not None:
            status = (
                df[coluna_status]
                .fillna("")
                .astype(str)
                .str.strip()
                .str.upper()
            )

            remover = {
                "PENDENTE_COLETA",
                "PENDENTE COLETA",
            }

            df = df[
                ~status.isin(remover)
            ].copy()

        nome_coluna_codigo = "REMESSA"

    df["__CODIGO_CONSULTA__"] = (
        df[coluna_codigo]
        .map(normalizar_codigo)
    )

    df = df[
        df["__CODIGO_CONSULTA__"] != ""
    ].copy()

    if df.empty:
        raise ValueError(
            f"Nenhum código válido foi encontrado na coluna {nome_coluna_codigo}."
        )

    return df, coluna_codigo


def preparar_codigos(arquivo):
    df, _ = preparar_base_performance(arquivo)
    return df["__CODIGO_CONSULTA__"].tolist()

def criar_pasta_execucao():
    agora = datetime.now()
    nome = agora.strftime("extracao_%Y-%m-%d_%H-%M-%S")
    pasta = RESULTADOS_DIR / nome
    bases = pasta / "bases"
    bases.mkdir(parents=True, exist_ok=True)
    return pasta, bases


def fazer_login(page):
    page.goto(URL_FRACTION, wait_until="domcontentloaded")

    page.get_by_role("textbox", name="Usuário").fill(FRACTION_USER)
    page.get_by_role("textbox", name="Senha").fill(FRACTION_PASSWORD)
    page.get_by_role("button", name="Login").click()

    # Aguarda a tela pós-login.
    page.get_by_role("link", name="Consultas").wait_for(
        state="visible",
        timeout=120000,
    )


def navegar_para_consulta(page):
    """
    O caminho é feito novamente a cada consulta:
    Consultas -> Consulta Geral.
    """
    page.get_by_role("link", name="Consultas").click()
    page.get_by_role("link", name="Consulta Geral").click()

    page.locator('[id="frmConsulta:cte"]').wait_for(
        state="visible",
        timeout=120000,
    )


def processar_lote(page, codigos, numero_lote, pasta_bases):
    navegar_para_consulta(page)

    campo = page.locator('[id="frmConsulta:cte"]')

    # Um código por linha, permitindo até 1000 por consulta.
    campo.fill("\n".join(codigos))

    page.get_by_role("button", name="Processar").click()

    exportador = page.locator('[id="formConsultaData:id_exportar_excel"]')
    exportador.wait_for(state="visible", timeout=240000)

    inicio_linha = 2 + (numero_lote - 1) * LIMITE_POR_CONSULTA
    fim_linha = inicio_linha + len(codigos) - 1

    # O primeiro arquivo usa "02" para manter a ordenação correta na pasta.
    inicio_nome = f"{inicio_linha:02d}" if numero_lote == 1 else str(inicio_linha)
    nome = f"{inicio_nome}_a_{fim_linha}.xlsx"
    caminho = pasta_bases / nome

    with page.expect_download(timeout=240000) as download_info:
        exportador.click()

    download = download_info.value
    download.save_as(caminho)

    return caminho


def unificar_bases(arquivos, caminho_saida, modo_data=False):
    """
    Os arquivos do Fraction possuem cabeçalho na linha 2.

    Primeiro arquivo:
      - lê cabeçalho na linha 2;
      - mantém o cabeçalho.

    Arquivos seguintes:
      - lê a mesma estrutura;
      - descarta a linha de cabeçalho;
      - acrescenta somente os dados.
    """
    if not arquivos:
        raise ValueError("Nenhuma base foi exportada.")

    partes = []

    for indice, arquivo in enumerate(arquivos):
        if indice == 0:
            df = pd.read_excel(arquivo, header=1)
        else:
            df = pd.read_excel(arquivo, header=1)

        partes.append(df)

    final = pd.concat(partes, ignore_index=True)

    if modo_data:
        coluna_status = localizar_coluna(
            final,
            "Status",
        )

        if coluna_status is not None:
            status = (
                final[coluna_status]
                .astype(str)
                .str.strip()
                .str.upper()
            )

            final = final[
                ~status.eq("ENTREGUE")
            ].copy()

        colunas_remover = []

        for nome_coluna in (
            "Nota Fiscal",
            "Pedido",
        ):
            coluna = localizar_coluna(
                final,
                nome_coluna,
            )

            if coluna is not None:
                colunas_remover.append(
                    coluna
                )

        if colunas_remover:
            final.drop(
                columns=colunas_remover,
                inplace=True,
            )

        final.drop_duplicates(
            inplace=True,
            ignore_index=True,
        )

    # Mantém a primeira linha em branco.
    # Cabeçalho na linha 2 e dados a partir da linha 3.
    final.to_excel(
        caminho_saida,
        index=False,
        startrow=1,
    )


def executar_extracao(arquivo, log=lambda msg: None):
    validar_env()

    base_preparada, coluna_codigo = preparar_base_performance(
        arquivo
    )

    modo_data = (
        str(base_preparada.columns[0])
        .strip()
        .upper()
        == "DATA"
    )

    codigos = base_preparada[
        "__CODIGO_CONSULTA__"
    ].tolist()

    pasta_execucao, pasta_bases = criar_pasta_execucao()

    log(
        f"{len(codigos)} código(s) preparado(s) para consulta."
    )
    log(
        f"Pasta da execução: {pasta_execucao}"
    )

    arquivos_bases = []
    codigos_falhos: set[str] = set()

    with sync_playwright() as playwright:
        browser = None
        context = None
        page = None

        def fechar_sessao():
            nonlocal browser, context, page

            if context is not None:
                try:
                    context.close()
                except Exception as erro:
                    log(
                        "Aviso ao fechar contexto do Fraction: "
                        f"{type(erro).__name__}: {erro}"
                    )

            if browser is not None:
                try:
                    browser.close()
                except Exception as erro:
                    log(
                        "Aviso ao fechar Chromium do Fraction: "
                        f"{type(erro).__name__}: {erro}"
                    )

            browser = None
            context = None
            page = None

        def abrir_sessao():
            nonlocal browser, context, page

            fechar_sessao()

            browser = playwright.chromium.launch(
                headless=HEADLESS
            )
            context = browser.new_context()
            page = context.new_page()

            log("Abrindo e autenticando no Fraction...")
            fazer_login(page)
            log("Login no Fraction concluído.")

        def recuperar_sessao():
            recuperar_sessao_fraction(
                page,
                log,
                login_callback=lambda: fazer_login(page),
            )

        try:
            try:
                abrir_sessao()
            except Exception as erro_login:
                log(
                    "Fraction indisponível no início da extração. "
                    "Todos os códigos preparados irão para a planilha "
                    "de reimportação. "
                    f"Detalhe: {type(erro_login).__name__}: {erro_login}"
                )
                codigos_falhos.update(codigos)

            total = len(codigos)

            for inicio in range(0, total, LIMITE_POR_CONSULTA):
                lote = codigos[
                    inicio:inicio + LIMITE_POR_CONSULTA
                ]
                numero_lote = (
                    inicio // LIMITE_POR_CONSULTA
                ) + 1

                if all(
                    codigo in codigos_falhos
                    for codigo in lote
                ):
                    continue

                log(
                    f"Processando lote {numero_lote} "
                    f"com {len(lote)} código(s)."
                )

                def consultar_lote():
                    return processar_lote(
                        page,
                        lote,
                        numero_lote,
                        pasta_bases,
                    )

                try:
                    arquivo_base = executar_com_retentativas_fraction(
                        consultar_lote,
                        recuperar_sessao,
                        log,
                        descricao=f"Lote {numero_lote}",
                        tentativas=5,
                    )

                    arquivos_bases.append(
                        arquivo_base
                    )
                    log(
                        f"Lote {numero_lote} salvo em: {arquivo_base}"
                    )

                except PlaywrightTimeoutError:
                    log(
                        f"Lote {numero_lote}: retentativas com refresh "
                        "esgotadas. Reiniciando o Chromium para uma "
                        "última tentativa."
                    )

                    try:
                        abrir_sessao()
                        arquivo_base = consultar_lote()

                        arquivos_bases.append(
                            arquivo_base
                        )
                        log(
                            f"Lote {numero_lote}: recuperado após "
                            "reiniciar o Chromium."
                        )

                    except Exception as erro_final:
                        codigos_falhos.update(lote)

                        log(
                            f"Lote {numero_lote}: erro definitivo. "
                            f"{type(erro_final).__name__}: {erro_final}"
                        )

                        # Antes do lote seguinte, tenta deixar uma sessão nova.
                        try:
                            abrir_sessao()
                        except Exception as erro_reabertura:
                            restantes = codigos[
                                inicio + LIMITE_POR_CONSULTA:
                            ]
                            codigos_falhos.update(
                                restantes
                            )

                            log(
                                "Não foi possível restabelecer o Fraction. "
                                "Os lotes restantes irão diretamente para "
                                "a planilha de reimportação. "
                                f"Detalhe: {type(erro_reabertura).__name__}: "
                                f"{erro_reabertura}"
                            )
                            break

                except Exception as erro_lote:
                    codigos_falhos.update(lote)
                    log(
                        f"Lote {numero_lote}: erro na consulta. "
                        f"{type(erro_lote).__name__}: {erro_lote}"
                    )

        finally:
            fechar_sessao()

    caminho_unificado = None

    if arquivos_bases:
        nome_unificado = (
            f"extracao_unificada_"
            f"{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.xlsx"
        )

        caminho_unificado = pasta_execucao / nome_unificado

        log(
            f"Unificando {len(arquivos_bases)} base(s) exportada(s)."
        )
        unificar_bases(
            arquivos_bases,
            caminho_unificado,
            modo_data=modo_data,
        )
        log(
            f"Arquivo unificado salvo em: {caminho_unificado}"
        )

    arquivo_erros = None

    if codigos_falhos:
        reimportar = base_preparada[
            base_preparada["__CODIGO_CONSULTA__"].isin(
                codigos_falhos
            )
        ].copy()

        reimportar.drop(
            columns=["__CODIGO_CONSULTA__"],
            inplace=True,
        )

        arquivo_erros = salvar_planilha_reimportacao(
            reimportar,
            pasta_execucao,
            "erros_reimportar",
            colunas_texto=(
                str(coluna_codigo),
            ),
        )

        log(
            f"{len(reimportar)} linha(s) ficaram pendentes. "
            f"Planilha pronta para reimportação: {arquivo_erros}"
        )

    return {
        "arquivo": caminho_unificado,
        "arquivo_erros": arquivo_erros,
        "total_erros": len(codigos_falhos),
        "pasta": pasta_execucao,
    }
