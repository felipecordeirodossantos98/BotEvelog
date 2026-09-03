import os
import re
from datetime import datetime
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright


PASTA_FLUXO = Path(__file__).resolve().parent
PASTA_PROJETO = Path(__file__).resolve().parents[2]

BASE_DIR = PASTA_FLUXO
PASTA_RAIZ = PASTA_PROJETO.parent

RESULTADOS_DIR = (
    PASTA_RAIZ
    / "resultados"
    / "relatorios_performance"
)

LIMITE_POR_CONSULTA = 1000

load_dotenv(PASTA_PROJETO / ".env")

URL_FRACTION = os.getenv("URL_FRACTION")
FRACTION_USER = os.getenv("FRACTION_USER")
FRACTION_PASSWORD = os.getenv("FRACTION_PASSWORD")

HEADLESS = (
    os.getenv("HEADLESS", "True")
    .strip()
    .lower()
    in {"1", "true", "yes", "sim", "on"}
)

def validar_env():
    campos = {
        "URL_FRACTION": URL_FRACTION,
        "FRACTION_USER": FRACTION_USER,
        "FRACTION_PASSWORD": FRACTION_PASSWORD,
    }

    faltantes = [nome for nome, valor in campos.items() if not valor]

    if faltantes:
        raise RuntimeError(
            "Variáveis não configuradas no .env: " + ", ".join(faltantes)
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


def preparar_codigos(arquivo):
    """
    Identifica automaticamente qual dos dois layouts foi importado.

    Layout 1:
      A1 = NUMERO DO PEDIDO
      - remove STATUS = PENDENTE_COLETA / PENDENTE COLETA
      - pesquisa a coluna REMESSA

    Layout 2:
      A1 = DATA
      - remove STATUS = ENTREGUE
      - pesquisa a coluna CTE
    """
    df = pd.read_excel(
        arquivo,
        dtype=object,
    )

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

    codigos = (
        df[coluna_codigo]
        .map(normalizar_codigo)
        .loc[lambda s: s != ""]
        .tolist()
    )

    if not codigos:
        raise ValueError(
            f"Nenhum código válido foi encontrado na coluna {nome_coluna_codigo}."
        )

    return codigos

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


def unificar_bases(arquivos, caminho_saida):
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

    # Mantém a primeira linha em branco.
    # Cabeçalho na linha 2 e dados a partir da linha 3.
    final.to_excel(
        caminho_saida,
        index=False,
        startrow=1,
    )


def executar_extracao(arquivo):
    validar_env()

    codigos = preparar_codigos(arquivo)
    pasta_execucao, pasta_bases = criar_pasta_execucao()

    arquivos_bases = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        page = browser.new_page()

        try:
            fazer_login(page)

            total = len(codigos)

            for inicio in range(0, total, LIMITE_POR_CONSULTA):
                lote = codigos[inicio:inicio + LIMITE_POR_CONSULTA]
                numero_lote = (inicio // LIMITE_POR_CONSULTA) + 1

                arquivo_base = processar_lote(
                    page,
                    lote,
                    numero_lote,
                    pasta_bases,
                )

                arquivos_bases.append(arquivo_base)

        finally:
            browser.close()

    nome_unificado = (
        f"extracao_unificada_"
        f"{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.xlsx"
    )

    caminho_unificado = pasta_execucao / nome_unificado

    unificar_bases(arquivos_bases, caminho_unificado)

    return caminho_unificado

