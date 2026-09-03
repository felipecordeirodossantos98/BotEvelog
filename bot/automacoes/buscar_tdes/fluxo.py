import streamlit as st

from .automacao import (
    executar_busca_tdes,
    preparar_base,
)


LARGURA_COMPONENTE = 600
LARGURA_BOTAO = 240


def renderizar_buscar_tdes(
    arquivo,
) -> None:
    st.subheader(
        "Buscar TDEs"
    )

    try:
        base = preparar_base(
            arquivo
        )

        arquivo.seek(0)

        st.info(
            (
                f"{len(base)} pedido(s) com "
                "peso maior ou igual a 30 kg."
            ),
            width=LARGURA_COMPONENTE,
        )

    except Exception as erro:
        st.error(
            str(erro),
            width=LARGURA_COMPONENTE,
        )
        return

    iniciar = st.button(
        "Buscar TDEs",
        type="primary",
        width=LARGURA_BOTAO,
    )

    if not iniciar:
        return

    mensagem = st.empty()

    def registrar(texto: str) -> None:
        mensagem.caption(
            texto,
            width=LARGURA_COMPONENTE,
        )

    try:
        arquivo.seek(0)

        with st.spinner(
            "Buscando TDEs no Fraction..."
        ):
            resultado = executar_busca_tdes(
                arquivo,
                log=registrar,
            )

        arquivo.seek(0)

        st.success(
            (
                "Busca concluída com sucesso. "
                f"{resultado['total']} pedido(s) consultado(s)."
            ),
            width=LARGURA_COMPONENTE,
        )

        st.write(
            f"Arquivo salvo em: `{resultado['arquivo']}`"
        )

    except Exception as erro:
        st.error(
            f"Erro durante a busca de TDEs: {erro}",
            width=LARGURA_COMPONENTE,
        )
