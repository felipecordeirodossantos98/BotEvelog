import streamlit as st

from automacoes.baixar_relatorios_performance import executar_extracao
from utils.interface import (
    LARGURA_BOTAO,
    avisar_variaveis_faltantes,
    criar_painel_execucao,
)
from utils.logs import criar_logger_execucao


CHAVE_LOGS = "visual_relatorios_performance_logs"


def renderizar_baixar_relatorios_performance(
    arquivo,
) -> None:
    st.subheader(
        "Baixar relatório de performance"
    )

    faltantes = avisar_variaveis_faltantes(
        "baixar_relatorios_performance"
    )

    iniciar = st.button(
        "Iniciar extração",
        type="primary",
        width=LARGURA_BOTAO,
        disabled=bool(faltantes),
        key="visual_relatorios_performance_iniciar",
    )

    if not iniciar:
        return

    feedback, log_visual = criar_painel_execucao(
        CHAVE_LOGS
    )

    log, caminho_log = criar_logger_execucao(
        "baixar_relatorios_performance",
        log_visual,
    )

    try:
        log("Iniciando extração do relatório de performance.")
        arquivo.seek(0)

        with st.spinner(
            "Extraindo relatório de performance..."
        ):
            resultado = executar_extracao(
                arquivo,
                log=log,
            )

        arquivo.seek(0)

        caminho = resultado.get("arquivo")
        arquivo_erros = resultado.get("arquivo_erros")

        if caminho:
            log(f"Arquivo salvo em: {caminho}")

        if arquivo_erros:
            feedback.warning(
                "Extração concluída com pendências. "
                f"{resultado['total_erros']} código(s) ficaram para "
                "nova tentativa. "
                f"Resultado: {caminho or 'nenhuma base concluída'} | "
                f"Planilha pronta para reimportar: {arquivo_erros}",
                width="stretch",
            )
        else:
            feedback.success(
                f"Extração concluída. Arquivo salvo em: {caminho}",
                width="stretch",
            )

    except Exception as erro:
        log(
            f"Erro: {type(erro).__name__}: {erro}"
        )
        feedback.error(
            f"Erro durante a extração: {erro}",
            width="stretch",
        )
