from __future__ import annotations

from datetime import datetime
from pathlib import Path

import streamlit as st

from automacoes.mega_cartela_lsm import (
    adicionar_aba_solicitacao,
    atualizar_dados_cartela,
    extrair_nome_aba,
    localizar_mega_cartela,
)
from utils.interface import (
    LARGURA_BOTAO,
    LARGURA_UPLOAD,
)


CHAVE_RESULTADO = (
    "mega_cartela_lsm_resultado"
)

CHAVE_LOGS_UPDATE = (
    "mega_cartela_lsm_logs_update"
)

CHAVE_ARQUIVO_LOG_UPDATE = (
    "mega_cartela_lsm_arquivo_log_update"
)

PASTA_LOGS = (
    Path(__file__).resolve().parents[2]
    / "logs"
    / "mega_cartela_lsm"
)


def renderizar_mega_cartela_lsm() -> None:
    st.subheader(
        "Mega Cartela LSM"
    )

    try:
        caminho_cartela = (
            localizar_mega_cartela()
        )

        st.caption(
            "Cartela principal: "
            f"`{caminho_cartela.name}`"
        )

    except Exception as erro:
        st.warning(
            str(erro),
            width=LARGURA_UPLOAD,
        )

        caminho_cartela = None

    st.markdown(
        "#### Importador de abas"
    )

    st.caption(
        "Adiciona uma nova solicitação como uma nova aba "
        "na Mega Cartela. Nesta primeira etapa são inseridos "
        "somente os cabeçalhos e os valores da planilha importada, "
        "sem adicionar fórmulas."
    )

    arquivo = st.file_uploader(
        "Importe a planilha da nova solicitação",
        type=["xlsx"],
        key=(
            "mega_cartela_lsm_"
            "importador_abas"
        ),
        width=LARGURA_UPLOAD,
    )

    nome_aba = None

    if arquivo is not None:
        try:
            nome_aba = extrair_nome_aba(
                arquivo.name
            )

            st.info(
                f"Nova aba: **{nome_aba}**",
                width=LARGURA_UPLOAD,
            )

        except Exception as erro:
            st.error(
                str(erro),
                width=LARGURA_UPLOAD,
            )

        iniciar = st.button(
            "Adicionar aba",
            type="primary",
            key=(
                "mega_cartela_lsm_"
                "adicionar_aba"
            ),
            width=LARGURA_BOTAO,
            disabled=(
                caminho_cartela is None
                or nome_aba is None
            ),
        )

        if iniciar:
            try:
                with st.spinner(
                    "Adicionando nova aba..."
                ):
                    resultado = (
                        adicionar_aba_solicitacao(
                            arquivo
                        )
                    )

                st.session_state[
                    CHAVE_RESULTADO
                ] = str(
                    resultado[
                        "arquivo"
                    ]
                )

                st.success(
                    (
                        f"Aba **{resultado['aba']}** adicionada "
                        f"com {resultado['linhas']} linha(s)."
                    ),
                    width=LARGURA_UPLOAD,
                )

            except Exception as erro:
                st.error(
                    (
                        "Não foi possível adicionar a aba: "
                        f"{erro}"
                    ),
                    width=LARGURA_UPLOAD,
                )

    caminho_resultado = (
        st.session_state.get(
            CHAVE_RESULTADO
        )
    )

    if caminho_resultado:
        caminho_resultado = Path(
            caminho_resultado
        )

        if caminho_resultado.exists():
            st.download_button(
                "Baixar Mega Cartela atualizada",
                data=(
                    caminho_resultado
                    .read_bytes()
                ),
                file_name=(
                    "mega_cartela_lsm.xlsx"
                ),
                mime=(
                    "application/vnd.openxmlformats-officedocument."
                    "spreadsheetml.sheet"
                ),
                key=(
                    "mega_cartela_lsm_"
                    "download"
                ),
                width=LARGURA_BOTAO,
            )

    st.divider()

    st.markdown(
        "#### Atualizador de dados"
    )

    st.caption(
        "Primeiro localiza os CTEs das células B vazias, da aba mais "
        "recente para a mais antiga, validando Data de emissão e Peso Taxado. "
        "Depois atualiza todos os Rastreios não finalizados, em lotes de "
        "até 1000 CTEs."
    )

    if CHAVE_LOGS_UPDATE not in st.session_state:
        st.session_state[
            CHAVE_LOGS_UPDATE
        ] = []

    if CHAVE_ARQUIVO_LOG_UPDATE not in st.session_state:
        st.session_state[
            CHAVE_ARQUIVO_LOG_UPDATE
        ] = None

    iniciar_update = st.button(
        "Atualizar dados",
        type="primary",
        key=(
            "mega_cartela_lsm_"
            "atualizar_dados"
        ),
        width=LARGURA_BOTAO,
        disabled=(
            caminho_cartela is None
        ),
    )

    with st.expander(
        "Log da atualização",
        expanded=False,
        width="stretch",
    ):
        area_log = st.empty()

        if st.session_state[
            CHAVE_LOGS_UPDATE
        ]:
            area_log.code(
                "\n".join(
                    st.session_state[
                        CHAVE_LOGS_UPDATE
                    ]
                ),
                language=None,
            )

    def registrar_update(
        mensagem,
    ):
        mensagem = str(
            mensagem
        )

        st.session_state[
            CHAVE_LOGS_UPDATE
        ].append(
            mensagem
        )

        caminho_log = st.session_state.get(
            CHAVE_ARQUIVO_LOG_UPDATE
        )

        if caminho_log:
            with Path(
                caminho_log
            ).open(
                "a",
                encoding="utf-8",
            ) as arquivo_log:
                arquivo_log.write(
                    mensagem
                    + "\n"
                )

        area_log.code(
            "\n".join(
                st.session_state[
                    CHAVE_LOGS_UPDATE
                ]
            ),
            language=None,
        )

    if iniciar_update:
        st.session_state[
            CHAVE_LOGS_UPDATE
        ] = []

        PASTA_LOGS.mkdir(
            parents=True,
            exist_ok=True,
        )

        caminho_log = (
            PASTA_LOGS
            / (
                "execucao_"
                f"{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.txt"
            )
        )

        st.session_state[
            CHAVE_ARQUIVO_LOG_UPDATE
        ] = str(
            caminho_log
        )

        caminho_log.write_text(
            (
                "Mega Cartela LSM - log da atualização\n"
                f"Início: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n"
                "=" * 80
                + "\n"
            ),
            encoding="utf-8",
        )

        try:
            with st.spinner(
                "Consultando Fraction e atualizando a Mega Cartela..."
            ):
                resultado_update = (
                    atualizar_dados_cartela(
                        log=registrar_update
                    )
                )

            mensagem_sucesso = (
                f"{resultado_update['rastreios_encontrados']} Rastreio(s) "
                "encontrado(s) e adicionado(s) pela Folha de apoio. "
                f"Depois, {resultado_update['linhas_atualizadas']} linha(s) "
                f"atualizada(s) em "
                f"{len(resultado_update['abas_atualizadas'])} aba(s). "
                f"{resultado_update['rastreios_consultados']} Rastreio(s) "
                "consultado(s) na atualização."
            )

            registrar_update(
                mensagem_sucesso
            )

            st.success(
                mensagem_sucesso,
                width="stretch",
            )

            if resultado_update[
                "rastreios_sem_retorno"
            ]:
                mensagem_sem_retorno = (
                    f"{resultado_update['rastreios_sem_retorno']} "
                    "Rastreio(s) ficaram sem retorno. "
                    "Como continuam não finalizados, serão "
                    "tentados novamente na próxima atualização."
                )

                registrar_update(
                    mensagem_sem_retorno
                )

                st.warning(
                    mensagem_sem_retorno,
                    width="stretch",
                )

            registrar_update(
                "Execução concluída com sucesso."
            )

        except Exception as erro:
            registrar_update(
                (
                    "Erro: "
                    f"{type(erro).__name__}: "
                    f"{erro}"
                )
            )

            st.error(
                (
                    "Não foi possível atualizar a Mega Cartela: "
                    f"{erro}"
                ),
                width=LARGURA_UPLOAD,
            )

