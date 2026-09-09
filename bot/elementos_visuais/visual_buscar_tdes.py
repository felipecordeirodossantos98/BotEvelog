import streamlit as st

from automacoes.buscar_tdes import executar_busca_tdes
from utils.interface import (
    LARGURA_BOTAO,
    avisar_variaveis_faltantes,
    criar_painel_execucao,
    warning,
)
from utils.logs import criar_logger_execucao


CHAVE_LOGS = "visual_buscar_tdes_logs"


def renderizar_buscar_tdes(
    arquivo,
) -> None:
    st.subheader("Buscar TDEs")

    faltantes = avisar_variaveis_faltantes(
        "buscar_tdes"
    )

    iniciar = st.button(
        "Buscar TDEs",
        type="primary",
        width=LARGURA_BOTAO,
        disabled=bool(faltantes),
        key="visual_buscar_tdes_iniciar",
    )

    if not iniciar:
        return

    feedback, log_visual = criar_painel_execucao(
        CHAVE_LOGS
    )

    log, caminho_log = criar_logger_execucao(
        "buscar_tdes",
        log_visual,
    )

    try:
        arquivo.seek(0)

        with st.spinner(
            "Buscando TDEs no Fraction..."
        ):
            resultado = executar_busca_tdes(
                arquivo,
                log=log,
            )

        arquivo.seek(0)

        if resultado.get("arquivo_erros"):
            feedback.warning(
                "Busca concluída com pendências. "
                f"{resultado['total_erros']} pedido(s) não puderam ser "
                "consultados após as tentativas de recuperação. "
                f"Resultado: {resultado['arquivo']} | "
                "Planilha pronta para reimportar: "
                f"{resultado['arquivo_erros']}",
                width="stretch",
            )

        elif resultado.get("fraction_disponivel", True):
            feedback.success(
                f"Busca concluída. Arquivo salvo em: {resultado['arquivo']}",
                width="stretch",
            )

        else:
            feedback.warning(
                "A planilha foi gerada, mas o Fraction estava "
                "indisponível. As descrições não consultadas ficaram "
                f"em branco. Arquivo: {resultado['arquivo']}",
                width="stretch",
            )

    except Exception as erro:
        log(
            f"Erro: {type(erro).__name__}: {erro}"
        )
        feedback.error(
            f"Erro durante a busca de TDEs: {erro}",
            width="stretch",
        )
