from datetime import date, timedelta

import streamlit as st

from automacoes.baixar_relatorios_analitico import (
    executar_extracao_e_filtragem,
    listar_bases_diarias,
    unificar_bases,
)
from utils.interface import (
    LARGURA_BOTAO,
    LARGURA_COMPONENTE,
    avisar_variaveis_faltantes,
    criar_painel_execucao,
    error,
    info,
    renderizar_logs,
    success,
)
from utils.logs import criar_logger_execucao


CHAVE_LOGS_EXTRACAO = "visual_analitico_logs_extracao"
CHAVE_LOGS_UNIFICACAO = "visual_analitico_logs_unificacao"

CHAVE_ACAO = "visual_analitico_acao_em_execucao"
CHAVE_STATUS_EXTRACAO = "visual_analitico_status_extracao"
CHAVE_STATUS_UNIFICACAO = "visual_analitico_status_unificacao"

ACAO_EXTRACAO = "extracao"
ACAO_UNIFICACAO = "unificacao"


def _normalizar_periodo(
    valor,
):
    if isinstance(valor, tuple):
        if not valor:
            return None, None

        if len(valor) == 1:
            return valor[0], valor[0]

        return valor[0], valor[1]

    return valor, valor


def _inicializar_estado() -> None:
    if CHAVE_ACAO not in st.session_state:
        st.session_state[
            CHAVE_ACAO
        ] = None

    if CHAVE_STATUS_EXTRACAO not in st.session_state:
        st.session_state[
            CHAVE_STATUS_EXTRACAO
        ] = None

    if CHAVE_STATUS_UNIFICACAO not in st.session_state:
        st.session_state[
            CHAVE_STATUS_UNIFICACAO
        ] = None


def _renderizar_status(
    chave: str,
) -> None:
    status = st.session_state.get(
        chave
    )

    if not status:
        return

    tipo = status.get(
        "tipo"
    )
    mensagem = status.get(
        "mensagem",
        "",
    )

    if tipo == "success":
        success(
            mensagem
        )

    elif tipo == "error":
        error(
            mensagem
        )


def _agendar_acao(
    acao: str,
    chave_status: str,
) -> None:
    st.session_state[
        chave_status
    ] = None

    st.session_state[
        CHAVE_ACAO
    ] = acao

    st.rerun()


def renderizar_baixar_relatorios_analitico() -> None:
    _inicializar_estado()

    acao_em_execucao = (
        st.session_state[
            CHAVE_ACAO
        ]
    )

    executando = (
        acao_em_execucao
        is not None
    )

    st.subheader(
        "Baixar relatório analítico"
    )

    faltantes = avisar_variaveis_faltantes(
        "baixar_relatorios_analitico"
    )

    hoje = date.today()
    max_data_extracao = (
        hoje - timedelta(days=1)
    )

    data_extracao = st.date_input(
        "Período de extração",
        value=(
            max_data_extracao,
            max_data_extracao,
        ),
        min_value=date(2000, 1, 1),
        max_value=max_data_extracao,
        key=(
            "baixar_relatorios_analitico_"
            "periodo_extracao"
        ),
        format="DD/MM/YYYY",
        width=LARGURA_COMPONENTE,
    )

    (
        data_extracao_inicial,
        data_extracao_final,
    ) = _normalizar_periodo(
        data_extracao
    )

    quantidade_dias = (
        data_extracao_final
        - data_extracao_inicial
    ).days + 1

    st.caption(
        "Período selecionado: "
        f"{quantidade_dias} dia(s). "
        "A data máxima permitida é "
        f"{max_data_extracao.strftime('%d/%m/%Y')}."
    )

    token = st.text_input(
        "Token MFA",
        type="password",
        max_chars=6,
        placeholder="Digite o código MFA",
        key=(
            "baixar_relatorios_analitico_"
            "token_mfa"
        ),
        width=LARGURA_COMPONENTE,
    )

    iniciar_extracao = st.button(
        "Extrair e filtrar bases",
        type="primary",
        key=(
            "baixar_relatorios_analitico_"
            "extrair"
        ),
        width=LARGURA_BOTAO,
        disabled=(
            bool(faltantes)
            or executando
        ),
    )

    if iniciar_extracao:
        if not token.strip():
            error(
                "Informe o Token MFA."
            )
        else:
            _agendar_acao(
                ACAO_EXTRACAO,
                CHAVE_STATUS_EXTRACAO,
            )

    area_extracao = st.container()

    if acao_em_execucao != ACAO_EXTRACAO:
        with area_extracao:
            _renderizar_status(
                CHAVE_STATUS_EXTRACAO
            )

            renderizar_logs(
                st.session_state.get(
                    CHAVE_LOGS_EXTRACAO,
                    [],
                )
            )

    st.divider(
        width="stretch"
    )

    bases_disponiveis = (
        listar_bases_diarias()
    )

    data_unificacao_inicial = None
    data_unificacao_final = None
    bases_selecionadas = []

    area_unificacao = st.container()

    if not bases_disponiveis:
        info(
            "Ainda não existem bases diárias para unificação."
        )

    else:
        min_emissao = (
            bases_disponiveis[0][0]
        )
        max_emissao = (
            bases_disponiveis[-1][0]
        )

        hash_base = (
            len(bases_disponiveis),
            min_emissao,
            max_emissao,
        )

        chave_hash = (
            "baixar_relatorios_analitico_"
            "hash_base_unificacao"
        )

        chave_filtro = (
            "baixar_relatorios_analitico_"
            "filtro_global_unificacao"
        )

        if chave_hash not in st.session_state:
            st.session_state[
                chave_hash
            ] = None

        if (
            st.session_state[chave_hash]
            != hash_base
        ):
            st.session_state[
                chave_filtro
            ] = (
                min_emissao,
                max_emissao,
            )

            st.session_state[
                chave_hash
            ] = hash_base

        data_unificacao = st.date_input(
            "Período das bases para unificar",
            min_value=min_emissao,
            max_value=max_emissao,
            key=chave_filtro,
            format="DD/MM/YYYY",
            width=LARGURA_COMPONENTE,
        )

        (
            data_unificacao_inicial,
            data_unificacao_final,
        ) = _normalizar_periodo(
            data_unificacao
        )

        bases_selecionadas = [
            (
                data_base,
                arquivo,
            )
            for data_base, arquivo
            in bases_disponiveis
            if (
                data_unificacao_inicial
                <= data_base
                <= data_unificacao_final
            )
        ]

        st.caption(
            "Bases disponíveis: "
            f"{min_emissao.strftime('%d/%m/%Y')} "
            "a "
            f"{max_emissao.strftime('%d/%m/%Y')}."
        )

        with st.expander(
            (
                "Bases encontradas no período: "
                f"{len(bases_selecionadas)}"
            ),
            expanded=False,
            width="stretch",
        ):
            if not bases_selecionadas:
                st.caption(
                    "Nenhuma base encontrada no período selecionado."
                )
            else:
                for data_base, arquivo in bases_selecionadas:
                    st.markdown(
                        f"**{data_base.strftime('%d/%m/%Y')}** "
                        f"— `{arquivo.name}`"
                    )

        iniciar_unificacao = st.button(
            "Unificar bases diárias",
            type="primary",
            key=(
                "baixar_relatorios_analitico_"
                "unificar"
            ),
            width=LARGURA_BOTAO,
            disabled=executando,
        )

        if iniciar_unificacao:
            arquivos = [
                arquivo
                for _, arquivo
                in bases_selecionadas
            ]

            if not arquivos:
                error(
                    "Nenhuma base diária encontrada "
                    "dentro do período selecionado."
                )
            else:
                _agendar_acao(
                    ACAO_UNIFICACAO,
                    CHAVE_STATUS_UNIFICACAO,
                )

    if acao_em_execucao != ACAO_UNIFICACAO:
        with area_unificacao:
            _renderizar_status(
                CHAVE_STATUS_UNIFICACAO
            )

            renderizar_logs(
                st.session_state.get(
                    CHAVE_LOGS_UNIFICACAO,
                    [],
                )
            )

    # Os dois botões já foram desenhados desabilitados
    # antes de iniciar qualquer operação pesada.

    if acao_em_execucao == ACAO_EXTRACAO:
        with area_extracao:
            feedback, log_visual = (
                criar_painel_execucao(
                    CHAVE_LOGS_EXTRACAO
                )
            )

            log, caminho_log = criar_logger_execucao(
                "baixar_relatorios_analitico",
                log_visual,
                rotulo="extracao",
            )

            try:
                log(
                    "Iniciando extração do relatório analítico."
                )
                log(
                    "Período: "
                    f"{data_extracao_inicial.strftime('%d/%m/%Y')} "
                    "a "
                    f"{data_extracao_final.strftime('%d/%m/%Y')}."
                )

                def atualizar_extracao(
                    atual,
                    total,
                    data,
                ):
                    log(
                        "Extraindo "
                        f"{data.strftime('%d/%m/%Y')} "
                        f"({atual}/{total})."
                    )

                def atualizar_tratamento(
                    atual,
                    total,
                    mensagem,
                ):
                    log(
                        f"{mensagem} "
                        f"({atual}/{total})."
                    )

                with st.spinner(
                    "Extraindo e filtrando bases..."
                ):
                    resultado = (
                        executar_extracao_e_filtragem(
                            data_inicial=(
                                data_extracao_inicial
                            ),
                            data_final=(
                                data_extracao_final
                            ),
                            token=token.strip(),
                            atualizar_extracao=(
                                atualizar_extracao
                            ),
                            atualizar_tratamento=(
                                atualizar_tratamento
                            ),
                            log=log,
                        )
                    )

                st.session_state[
                    CHAVE_STATUS_EXTRACAO
                ] = {
                    "tipo": "success",
                    "mensagem": (
                        "Extração e filtragem concluídas. "
                        f"{len(resultado['arquivos_filtrados'])} "
                        "base(s) diária(s) preparada(s)."
                    ),
                }

            except Exception as erro_execucao:
                log(
                    "Erro: "
                    f"{type(erro_execucao).__name__}: "
                    f"{erro_execucao}"
                )

                st.session_state[
                    CHAVE_STATUS_EXTRACAO
                ] = {
                    "tipo": "error",
                    "mensagem": (
                        "A extração do relatório analítico "
                        f"foi interrompida: {erro_execucao}"
                    ),
                }

            finally:
                st.session_state[
                    CHAVE_ACAO
                ] = None

                st.rerun()

    if acao_em_execucao == ACAO_UNIFICACAO:
        arquivos = [
            arquivo
            for _, arquivo
            in bases_selecionadas
        ]

        with area_unificacao:
            feedback, log_visual = (
                criar_painel_execucao(
                    CHAVE_LOGS_UNIFICACAO
                )
            )

            log, caminho_log = criar_logger_execucao(
                "baixar_relatorios_analitico",
                log_visual,
                rotulo="unificacao",
            )

            try:
                log(
                    f"{len(arquivos)} base(s) selecionada(s) "
                    "para unificação."
                )

                with st.spinner(
                    "Unificando bases diárias..."
                ):
                    resultado = unificar_bases(
                        arquivos=arquivos,
                        data_inicial=(
                            data_unificacao_inicial
                        ),
                        data_final=(
                            data_unificacao_final
                        ),
                        log=log,
                    )

                log(
                    "Arquivo final salvo em: "
                    f"{resultado['arquivo_final']}"
                )

                st.session_state[
                    CHAVE_STATUS_UNIFICACAO
                ] = {
                    "tipo": "success",
                    "mensagem": (
                        "Unificação concluída com sucesso. "
                        "Arquivo salvo em: "
                        f"{resultado['arquivo_final']}"
                    ),
                }

            except Exception as erro_execucao:
                log(
                    "Erro: "
                    f"{type(erro_execucao).__name__}: "
                    f"{erro_execucao}"
                )

                st.session_state[
                    CHAVE_STATUS_UNIFICACAO
                ] = {
                    "tipo": "error",
                    "mensagem": (
                        "A unificação foi interrompida: "
                        f"{erro_execucao}"
                    ),
                }

            finally:
                st.session_state[
                    CHAVE_ACAO
                ] = None

                st.rerun()
