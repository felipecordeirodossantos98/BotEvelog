from pathlib import Path

import streamlit as st

from .automacao import executar_extracao


LARGURA_COMPONENTE = 600
LARGURA_BOTAO = 240


def renderizar_baixar_relatorios_performance(
    arquivo,
) -> None:
    st.subheader(
        "Baixar relatório de performance"
    )

    st.success(
        f"Arquivo carregado: {arquivo.name}",
        width=LARGURA_COMPONENTE,
    )

    iniciar = st.button(
        "Iniciar extração",
        type="primary",
        width=LARGURA_BOTAO,
    )

    if not iniciar:
        return

    try:
        arquivo.seek(0)

        with st.spinner(
            "Extraindo relatório de performance..."
        ):
            resultado = executar_extracao(
                arquivo
            )

        arquivo.seek(0)

        st.success(
            "Extração concluída com sucesso.",
            width=LARGURA_COMPONENTE,
        )

        caminho_resultado = Path(
            resultado
        )

        st.write(
            f"Arquivo unificado: `{caminho_resultado}`"
        )

    except Exception as erro:
        st.error(
            f"Erro durante a extração: {erro}",
            width=LARGURA_COMPONENTE,
        )
