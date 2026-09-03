from pathlib import Path

import pandas as pd
import streamlit as st

from .automacao import (
    ARQUIVO_BASE_CNPJS,
    ARQUIVO_ENV,
    ARQUIVO_EMAILS,
    executar_automacao,
    preparar_execucoes,
    validar_arquivos_fixos,
    validar_planilha_pedidos,
)


LARGURA_COMPONENTE = 600
LARGURA_METRICA = 220
LARGURA_BOTAO = 240


def renderizar_gerar_ordens_de_coleta(
    arquivo_pedidos,
    headless: bool,
) -> None:
    st.subheader("Gerar ordens de coleta")

    erros_fixos = validar_arquivos_fixos()

    if not ARQUIVO_ENV.exists():
        st.error(
            ".env não encontrado",
            width=LARGURA_COMPONENTE,
        )

    if not ARQUIVO_BASE_CNPJS.exists():
        st.error(
            "automacoes/gerar_ordens_de_coleta/dados/base_cnpjs.json não encontrado",
            width=LARGURA_COMPONENTE,
        )

    if not ARQUIVO_EMAILS.exists():
        st.error(
            "automacoes/gerar_ordens_de_coleta/dados/emails_unidades.json não encontrado",
            width=LARGURA_COMPONENTE,
        )

    if erros_fixos:
        st.warning(
            "Corrija os arquivos de configuração antes de executar a automação.",
            width=LARGURA_COMPONENTE,
        )

        with st.expander(
            "Ver problemas encontrados",
            width=LARGURA_COMPONENTE,
        ):
            for erro in erros_fixos:
                st.write(f"• {erro}")

    try:
        arquivo_pedidos.seek(0)

        df_pedidos = pd.read_excel(
            arquivo_pedidos,
            dtype=str,
        ).fillna("")

        arquivo_pedidos.seek(0)

    except Exception as erro:
        st.error(
            f"Não foi possível abrir a planilha: {erro}",
            width=LARGURA_COMPONENTE,
        )
        return

    erros_pedidos = validar_planilha_pedidos(df_pedidos)

    st.dataframe(
        df_pedidos,
        width="stretch",
        hide_index=True,
    )

    if erros_pedidos:
        st.error(
            "A planilha importada possui problemas:",
            width=LARGURA_COMPONENTE,
        )

        for erro in erros_pedidos:
            st.write(f"• {erro}")

        return

    try:
        execucoes, alertas = preparar_execucoes(df_pedidos)

    except Exception as erro:
        st.error(
            str(erro),
            width=LARGURA_COMPONENTE,
        )
        return

    with st.container(
        horizontal=True,
        width="stretch",
        gap="small",
    ):
        st.metric(
            "Siglas na planilha",
            df_pedidos["SIGLA DO RESTAURANTE"].nunique(),
            width=LARGURA_METRICA,
        )

        st.metric(
            "Ordens solicitadas",
            len(execucoes),
            width=LARGURA_METRICA,
        )

        st.metric(
            "Restaurantes com CNPJ",
            len({item["sigla"] for item in execucoes}),
            width=LARGURA_METRICA,
        )

    if alertas:
        st.warning(
            "Algumas linhas não serão processadas:",
            width=LARGURA_COMPONENTE,
        )

        for alerta in alertas:
            st.write(f"• {alerta}")

    with st.expander(
        "Ver fila de execução",
        width="stretch",
    ):
        st.dataframe(
            pd.DataFrame(execucoes),
            width="stretch",
            hide_index=True,
        )

    executar = st.button(
        "Gerar ordens",
        type="primary",
        width=LARGURA_BOTAO,
        disabled=(
            bool(erros_fixos)
            or len(execucoes) == 0
        ),
    )

    if not executar:
        return

    logs: list[str] = []

    try:
        with st.status(
            "Iniciando automação...",
            expanded=True,
            width="stretch",
        ) as painel:

            area_log = st.empty()

            def registrar(mensagem: str) -> None:
                logs.append(mensagem)

                area_log.code(
                    "\n".join(logs[-120:]),
                    language=None,
                )

            resultado = executar_automacao(
                execucoes=execucoes,
                headless=headless,
                modo_teste=False,
                continuar_em_erro=True,
                log=registrar,
            )

            painel.update(
                label="Processamento concluído",
                state="complete",
                expanded=True,
            )

        with st.container(
            horizontal=True,
            width="stretch",
            gap="small",
        ):
            st.metric(
                "Total tentado",
                resultado["total"],
                width=LARGURA_METRICA,
            )

            st.metric(
                "Autorizadas",
                resultado["sucessos"],
                width=LARGURA_METRICA,
            )

            st.metric(
                "Falhas",
                resultado["falhas"],
                width=LARGURA_METRICA,
            )

        st.markdown("#### Resultado da execução")

        st.dataframe(
            pd.DataFrame(resultado["detalhes"]),
            width="stretch",
            hide_index=True,
        )

        caminho_resultado = resultado.get(
            "arquivo_resultado"
        )

        if caminho_resultado:
            caminho_resultado = Path(
                caminho_resultado
            )

            st.success(
                (
                    "Planilha salva em "
                    "resultados/gerar_ordens_de_coleta/"
                    f"{caminho_resultado.name}"
                ),
                width=LARGURA_COMPONENTE,
            )

        caminho_erros = resultado.get(
            "arquivo_erros"
        )

        if caminho_erros:
            caminho_erros = Path(
                caminho_erros
            )

            st.warning(
                (
                    "Planilha de erros salva em "
                    "resultados/gerar_ordens_de_coleta/"
                    f"{caminho_erros.name}"
                ),
                width=LARGURA_COMPONENTE,
            )

    except Exception as erro:
        st.error(
            "A automação foi interrompida.",
            width=LARGURA_COMPONENTE,
        )
        st.exception(erro)
