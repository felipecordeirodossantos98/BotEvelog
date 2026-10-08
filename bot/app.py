from pathlib import Path

import streamlit as st

from elementos_visuais.visual_baixar_relatorios_analitico import (
    renderizar_baixar_relatorios_analitico,
)
from elementos_visuais.visual_baixar_danfes import (
    renderizar_baixar_danfes,
)
from elementos_visuais.visual_baixar_relatorios_performance import (
    renderizar_baixar_relatorios_performance,
)
from elementos_visuais.visual_buscar_tdes import (
    renderizar_buscar_tdes,
)
from elementos_visuais.visual_gerar_ordens_de_coleta_malotes import (
    renderizar_gerar_ordens_de_coleta,
)
from elementos_visuais.visual_gerar_ordens_de_coleta_equipamentos import (
    renderizar_gerar_ordens_de_coleta_equipamentos,
)
from elementos_visuais.visual_gerar_tickets_zendesk import (
    renderizar_gerar_tickets_zendesk,
)
from elementos_visuais.visual_mega_cartela_lsm import (
    renderizar_mega_cartela_lsm,
)

from utils.arquivos import identificar_fluxo_automacao_sac
from utils.interface import LARGURA_UPLOAD, error, warning

import streamlit.components.v1 as components

components.html(
    """
    <script>
        window.parent.addEventListener("beforeunload", function (event) {
            event.preventDefault();
            event.returnValue = "";
        });
    </script>
    """,
    height=0,
)


BASE_DIR = Path(__file__).resolve().parent
PAGE_ICON = BASE_DIR / "images" / "evelog-favicon.svg"


st.set_page_config(
    page_title="Bot Evelog",
    page_icon=str(PAGE_ICON),
    layout="wide",
)


RENDERIZADORES = {
    "gerar_ordens_de_coleta": renderizar_gerar_ordens_de_coleta,
    "gerar_tickets_zendesk": renderizar_gerar_tickets_zendesk,
    "baixar_danfes": renderizar_baixar_danfes,
    "buscar_tdes": renderizar_buscar_tdes,
    "baixar_relatorios_performance": renderizar_baixar_relatorios_performance,
}


def renderizar_automacao_sac(arquivos) -> None:
    if not isinstance(arquivos, list):
        arquivos = [arquivos]

    if not arquivos:
        return

    # O mesmo import aceita:
    # - planilha XLSX/XLS -> fluxo SAC existente;
    # - um ou mais PDFs -> fluxo de Ordens de Coleta — Equipamentos.
    extensoes = {
        Path(getattr(arquivo, "name", "")).suffix.lower()
        for arquivo in arquivos
    }

    if extensoes <= {".pdf"}:
        renderizar_gerar_ordens_de_coleta_equipamentos(arquivos)
        return

    if ".pdf" in extensoes:
        warning(
            "No mesmo import, use apenas PDFs para Equipamentos "
            "ou uma planilha XLS/XLSX para as demais automações."
        )
        return

    if len(arquivos) != 1:
        warning(
            "Para as automações por planilha, importe apenas um arquivo "
            "XLS ou XLSX por vez."
        )
        return

    arquivo = arquivos[0]

    try:
        fluxo = identificar_fluxo_automacao_sac(arquivo)
    except Exception as erro_identificacao:
        error(
            "Não foi possível identificar o fluxo da planilha: "
            f"{erro_identificacao}"
        )
        return

    renderizador = RENDERIZADORES.get(fluxo)

    if renderizador is None:
        warning(
            "Não foi possível identificar uma automação "
            "compatível com o arquivo importado."
        )
        return

    renderizador(arquivo)


def main() -> None:
    st.title("Bot Evelog")

    tab_sac, tab_analitico, tab_mega_cartela = st.tabs(
        [
            "Automações SAC",
            "Relatório Analítico",
            "Mega Cartela LSM",
        ],
        width="stretch",
    )

    with tab_sac:
        st.subheader("Automações SAC")

        arquivo_sac = st.file_uploader(
            "Importe o arquivo",
            type=["xlsx", "xls", "pdf"],
            accept_multiple_files=True,
            key="arquivo_automacoes_sac",
            width=LARGURA_UPLOAD,
        )

        if arquivo_sac:
            renderizar_automacao_sac(arquivo_sac)

    with tab_analitico:
        renderizar_baixar_relatorios_analitico()

    with tab_mega_cartela:
        renderizar_mega_cartela_lsm()


if __name__ == "__main__":
    main()
