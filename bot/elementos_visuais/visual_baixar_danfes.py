import streamlit as st

from automacoes.baixar_danfes import executar_baixar_danfes
from utils.interface import (
    LARGURA_BOTAO,
    avisar_variaveis_faltantes,
    criar_painel_execucao,
)
from utils.logs import criar_logger_execucao


CHAVE_LOGS = "visual_baixar_danfes_logs"


def renderizar_baixar_danfes(
    arquivo,
) -> None:
    st.subheader("Baixar Danfes")

    faltantes = avisar_variaveis_faltantes(
        "baixar_danfes"
    )

    iniciar = st.button(
        "Baixar DANFEs",
        type="primary",
        width=LARGURA_BOTAO,
        disabled=bool(faltantes),
        key="visual_baixar_danfes_iniciar",
    )

    if not iniciar:
        return

    feedback, log_visual = criar_painel_execucao(
        CHAVE_LOGS
    )

    log, caminho_log = criar_logger_execucao(
        "baixar_danfes",
        log_visual,
    )

    try:
        with st.spinner(
            "Baixando DANFEs..."
        ):
            resultado = executar_baixar_danfes(
                arquivo,
                log=log,
            )

        if resultado.get("arquivo_erros"):
            feedback.warning(
                "Processo concluído com pendências. "
                f"{resultado['total_erros']} pedido(s) ficaram para nova "
                "tentativa. "
                f"Arquivos: {resultado['pasta']} | "
                "Planilha pronta para reimportar: "
                f"{resultado['arquivo_erros']}",
                width="stretch",
            )

        else:
            feedback.success(
                f"Processo concluído. Arquivos salvos em: {resultado['pasta']}",
                width="stretch",
            )

    except Exception as erro:
        log(
            f"Erro: {type(erro).__name__}: {erro}"
        )
        feedback.error(
            f"Erro durante a execução: {erro}",
            width="stretch",
        )
