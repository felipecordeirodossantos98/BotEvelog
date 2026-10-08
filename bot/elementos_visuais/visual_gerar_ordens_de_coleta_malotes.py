import streamlit as st

from automacoes.gerar_ordens_de_coleta_malotes import (
    executar_automacao,
    preparar_arquivo_ordens,
    validar_arquivos_fixos,
)
from utils.config import HEADLESS
from utils.interface import (
    LARGURA_BOTAO,
    avisar_variaveis_faltantes,
    criar_painel_execucao,
    error,
    warning,
)
from utils.logs import criar_logger_execucao


CHAVE_LOGS = "visual_gerar_ordens_logs"


def renderizar_gerar_ordens_de_coleta(
    arquivo,
) -> None:
    st.subheader("Gerar ordens de coleta")

    faltantes = avisar_variaveis_faltantes(
        "gerar_ordens_de_coleta"
    )

    problemas_fixos = validar_arquivos_fixos()

    # Variáveis do .env já são apresentadas pelo aviso padronizado acima.
    problemas_arquivos = [
        item
        for item in problemas_fixos
        if not any(
            variavel in item
            for variavel in (
                "FRACTION_USER",
                "FRACTION_PASSWORD",
                "URL_FRACTION",
                ".env",
            )
        )
    ]

    for problema in problemas_arquivos:
        warning(problema)

    execucoes = []
    alertas = []
    erros_arquivo = []

    if not faltantes and not problemas_arquivos:
        try:
            execucoes, alertas, erros_arquivo = (
                preparar_arquivo_ordens(
                    arquivo
                )
            )
        except Exception as erro_arquivo:
            erros_arquivo = [
                str(erro_arquivo)
            ]

    for mensagem in erros_arquivo:
        error(mensagem)

    for mensagem in alertas:
        warning(mensagem)

    iniciar = st.button(
        "Gerar ordens",
        type="primary",
        width=LARGURA_BOTAO,
        disabled=(
            bool(faltantes)
            or bool(problemas_arquivos)
            or bool(erros_arquivo)
            or not execucoes
        ),
        key="visual_gerar_ordens_iniciar",
    )

    if not iniciar:
        return

    feedback, log_visual = criar_painel_execucao(
        CHAVE_LOGS
    )

    log, caminho_log = criar_logger_execucao(
        "gerar_ordens_de_coleta",
        log_visual,
    )

    try:
        with st.spinner(
            "Gerando ordens de coleta..."
        ):
            resultado = executar_automacao(
                execucoes=execucoes,
                headless=HEADLESS,
                continuar_em_erro=True,
                log=log,
            )

        caminho_resultado = resultado.get(
            "arquivo_resultado"
        )
        caminho_erros = resultado.get(
            "arquivo_erros"
        )

        caminho_verificar = resultado.get(
            "arquivo_verificar"
        )

        mensagem = (
            "Processamento concluído. "
            f"Autorizadas: {resultado['sucessos']} | "
            f"Falhas seguras: {resultado['falhas']} | "
            f"Status indeterminado: "
            f"{resultado.get('indeterminados', 0)}"
        )

        if caminho_resultado:
            mensagem += (
                f" | Resultado: {caminho_resultado}"
            )

        feedback.success(
            mensagem,
            width="stretch",
        )

        if caminho_erros:
            warning(
                "Planilha pronta para reimportação: "
                f"{caminho_erros}"
            )

        if caminho_verificar:
            warning(
                "Planilha de itens que precisam ser conferidos "
                "no Fraction antes de qualquer reimportação: "
                f"{caminho_verificar}"
            )

    except Exception as erro_execucao:
        log(
            "Erro: "
            f"{type(erro_execucao).__name__}: "
            f"{erro_execucao}"
        )
        feedback.error(
            f"A automação foi interrompida: {erro_execucao}",
            width="stretch",
        )
