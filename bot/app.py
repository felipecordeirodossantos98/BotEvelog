import os
import unicodedata
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from automacoes.baixar_danfes.fluxo import (
    renderizar_baixar_danfes,
)
from automacoes.baixar_relatorios_performance.fluxo import (
    renderizar_baixar_relatorios_performance,
)
from automacoes.baixar_relatorios_analitico.fluxo import (
    renderizar_baixar_relatorios_analitico,
)
from automacoes.gerar_ordens_de_coleta.fluxo import (
    renderizar_gerar_ordens_de_coleta,
)
from automacoes.gerar_tickets_zendesk.fluxo import (
    renderizar_gerar_tickets_zendesk,
)
from automacoes.buscar_tdes.fluxo import (
    renderizar_buscar_tdes,
)


BASE_DIR = Path(__file__).resolve().parent
IMAGES_DIR = BASE_DIR / "images"
PAGE_ICON = IMAGES_DIR / "evelog-favicon.svg"

LARGURA_UPLOAD = 600


load_dotenv(BASE_DIR / ".env")


HEADLESS = (
    os.getenv("HEADLESS", "True")
    .strip()
    .lower()
    in {"1", "true", "yes", "sim", "on"}
)


st.set_page_config(
    page_title="Bot Evelog",
    page_icon=str(PAGE_ICON),
    layout="wide",
)


def normalizar_cabecalho(valor: object) -> str:
    texto = unicodedata.normalize(
        "NFKD",
        str(valor).strip(),
    )

    texto = "".join(
        caractere
        for caractere in texto
        if not unicodedata.combining(caractere)
    )

    return " ".join(
        texto.upper().split()
    )


def ler_cabecalho_excel(
    arquivo,
    linha_cabecalho: int,
) -> list[str]:
    arquivo.seek(0)

    try:
        df = pd.read_excel(
            arquivo,
            header=linha_cabecalho,
            nrows=0,
        )
    finally:
        arquivo.seek(0)

    return [
        normalizar_cabecalho(coluna)
        for coluna in df.columns
    ]


def identificar_fluxo_automacao_sac(
    arquivo,
) -> str | None:
    # Fluxos com cabeçalho na linha 1.
    colunas_linha_1 = ler_cabecalho_excel(
        arquivo,
        linha_cabecalho=0,
    )

    if colunas_linha_1[:2] == [
        "SIGLA DO RESTAURANTE",
        "NUMERO DE ORDENS",
    ]:
        return "gerar_ordens_de_coleta"

    if (
        colunas_linha_1
        and colunas_linha_1[0] == "ORDEM/CTE"
    ):
        return "baixar_danfes"

    if colunas_linha_1[:2] == [
        "CTE",
        "PESO",
    ]:
        return "buscar_tdes"

    if (
        colunas_linha_1
        and colunas_linha_1[0] in {
            "NUMERO DO PEDIDO",
            "DATA",
        }
    ):
        return "baixar_relatorios_performance"

    # Zendesk:
    # linha 2 = cabeçalho, dados a partir da linha 3.
    colunas_linha_2 = ler_cabecalho_excel(
        arquivo,
        linha_cabecalho=1,
    )

    if (
        colunas_linha_2
        and colunas_linha_2[0] == "CODIGO"
    ):
        return "gerar_tickets_zendesk"

    return None


def renderizar_automacao_sac(
    arquivo,
) -> None:
    try:
        fluxo = identificar_fluxo_automacao_sac(
            arquivo
        )

    except Exception as erro:
        st.error(
            (
                "Não foi possível identificar "
                f"o fluxo da planilha: {erro}"
            ),
            width=LARGURA_UPLOAD,
        )
        return

    if fluxo == "gerar_ordens_de_coleta":
        renderizar_gerar_ordens_de_coleta(
            arquivo_pedidos=arquivo,
            headless=HEADLESS,
        )
        return

    if fluxo == "gerar_tickets_zendesk":
        renderizar_gerar_tickets_zendesk(
            arquivo=arquivo,
        )
        return

    if fluxo == "baixar_danfes":
        renderizar_baixar_danfes(
            arquivo=arquivo,
        )
        return

    if fluxo == "buscar_tdes":
        renderizar_buscar_tdes(
            arquivo=arquivo,
        )
        return

    if fluxo == "baixar_relatorios_performance":
        renderizar_baixar_relatorios_performance(
            arquivo=arquivo,
        )
        return

    st.warning(
        (
            "Não foi possível identificar "
            "uma automação compatível com "
            "o arquivo importado."
        ),
        width=LARGURA_UPLOAD,
    )



def main() -> None:
    st.title("Bot Evelog")

    tab_sac, tab_analitico = st.tabs(
        [
            "Automações SAC",
            "Relatório Analítico",
        ],
        width="stretch",
    )

    with tab_sac:

        st.subheader("Automações SAC")
        
        arquivo_sac = st.file_uploader(
            "Importe o arquivo",
            type=["xlsx", "xls"],
            key="arquivo_automacoes_sac",
            width=LARGURA_UPLOAD,
        )

        if arquivo_sac is not None:
            renderizar_automacao_sac(
                arquivo_sac
            )

    with tab_analitico:
        renderizar_baixar_relatorios_analitico()


if __name__ == "__main__":
    main()
