from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from .automacao import buscar_chaves
from .meudanfe import baixar_danfes


PASTA_BOT = Path(__file__).resolve().parents[2]
PASTA_RAIZ = PASTA_BOT.parent

RESULTADOS_DIR = (
    PASTA_RAIZ
    / "resultados"
    / "danfes"
)

LARGURA_COMPONENTE = 600
LARGURA_BOTAO = 240

CHAVE_LOGS = "baixar_danfes_logs"
CHAVE_RESULTADO = "baixar_danfes_resultado"
CHAVE_PASTA_RESULTADO = "baixar_danfes_pasta_resultado"


def renderizar_baixar_danfes(
    arquivo,
) -> None:
    st.subheader(
        "Baixar Danfes"
    )

    try:
        arquivo.seek(0)

        # Linha 1 = cabeçalho (A1 = Ordem/CTE).
        # Linha 2 em diante = dados.
        df = pd.read_excel(
            arquivo,
            header=0,
        )

        arquivo.seek(0)

        df = df.iloc[:, [0]].copy()
        df.columns = ["pedido"]

        df = (
            df[df["pedido"].notna()]
            .reset_index(drop=True)
        )

        st.info(
            f"{len(df)} pedido(s) encontrado(s).",
            width=LARGURA_COMPONENTE,
        )

    except Exception as exc:
        st.error(
            f"Não foi possível ler a planilha: {exc}",
            width=LARGURA_COMPONENTE,
        )
        return

    clicou_baixar = st.button(
        "Baixar DANFEs",
        type="primary",
        width=LARGURA_BOTAO,
    )

    if clicou_baixar:
        st.session_state[CHAVE_LOGS] = []
        st.session_state.pop(
            CHAVE_RESULTADO,
            None,
        )
        st.session_state.pop(
            CHAVE_PASTA_RESULTADO,
            None,
        )

    if CHAVE_LOGS not in st.session_state:
        st.session_state[CHAVE_LOGS] = []

    with st.expander(
        "Log da execução",
        expanded=False,
        width=LARGURA_COMPONENTE,
    ):
        log_area = st.empty()

        if st.session_state[CHAVE_LOGS]:
            log_area.code(
                "\n".join(
                    st.session_state[CHAVE_LOGS]
                ),
                language=None,
            )

    def registrar(msg):
        st.session_state[
            CHAVE_LOGS
        ].append(msg)

        log_area.code(
            "\n".join(
                st.session_state[CHAVE_LOGS]
            ),
            language=None,
        )

    if clicou_baixar:
        try:
            agora = datetime.now().strftime(
                "%Y-%m-%d_%H-%M-%S"
            )

            pasta_resultado = (
                RESULTADOS_DIR
                / f"danfes_{agora}"
            )

            pasta_resultado.mkdir(
                parents=True,
                exist_ok=True,
            )

            with st.spinner(
                "Buscando chaves no FractionWeb...",
                width=LARGURA_COMPONENTE,
            ):
                resultado = buscar_chaves(
                    df,
                    log=registrar,
                )

            with st.spinner(
                "Consultando e baixando DANFEs...",
                width=LARGURA_COMPONENTE,
            ):
                resultado = baixar_danfes(
                    resultado,
                    pasta=pasta_resultado,
                    log=registrar,
                )

            st.session_state[
                CHAVE_RESULTADO
            ] = resultado

            st.session_state[
                CHAVE_PASTA_RESULTADO
            ] = str(
                pasta_resultado
            )

            registrar(
                f"📁 Arquivos salvos em: {pasta_resultado}"
            )
            registrar(
                "🏁 Processo finalizado."
            )

        except Exception as exc:
            registrar(
                f"❌ Erro geral: {exc}"
            )
            st.error(
                str(exc),
                width=LARGURA_COMPONENTE,
            )

    if CHAVE_RESULTADO in st.session_state:
        resultado = st.session_state[
            CHAVE_RESULTADO
        ]

        st.subheader(
            "Resultado final"
        )

        st.dataframe(
            resultado,
            width="stretch",
            hide_index=True,
        )

        pasta_resultado = st.session_state.get(
            CHAVE_PASTA_RESULTADO
        )

        if pasta_resultado:
            st.success(
                f"DANFEs salvos em: {pasta_resultado}",
                width=LARGURA_COMPONENTE,
            )

