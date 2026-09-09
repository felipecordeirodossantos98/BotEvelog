from __future__ import annotations

from collections.abc import Callable

import streamlit as st

from .config import (
    variaveis_faltantes,
    variaveis_opcionais_faltantes,
)


LARGURA_BOTAO = 240
LARGURA_COMPONENTE = 600
LARGURA_UPLOAD = 600
MAX_LINHAS_LOG = 150


def warning(mensagem: str) -> None:
    st.warning(
        mensagem,
        width="stretch",
    )


def error(mensagem: str) -> None:
    st.error(
        mensagem,
        width="stretch",
    )


def success(mensagem: str) -> None:
    st.success(
        mensagem,
        width="stretch",
    )


def info(mensagem: str) -> None:
    st.info(
        mensagem,
        width="stretch",
    )


def avisar_variaveis_faltantes(
    fluxo: str,
) -> list[str]:
    """
    Exibe warnings de configuração.

    O retorno contém somente variáveis bloqueantes. Variáveis opcionais
    são informadas ao usuário, mas não desabilitam a automação.
    """
    faltantes = variaveis_faltantes(fluxo)
    opcionais = variaveis_opcionais_faltantes(fluxo)

    if faltantes:
        warning(
            "Configuração incompleta no .env. "
            "Variável(is) obrigatória(s) faltante(s): "
            + ", ".join(faltantes)
        )

    if opcionais:
        warning(
            "Configuração parcial. A automação pode continuar, "
            "mas a etapa opcional relacionada a "
            + ", ".join(opcionais)
            + " não será executada."
        )

    return faltantes


def _conteudo_log(
    chave_logs: str,
) -> str:
    logs = st.session_state.get(
        chave_logs,
        [],
    )

    return "\n".join(
        logs[-MAX_LINHAS_LOG:]
    )


def criar_painel_execucao(
    chave_logs: str,
    *,
    limpar: bool = True,
) -> tuple[object, Callable[[str], None]]:
    if limpar or chave_logs not in st.session_state:
        st.session_state[chave_logs] = []

    feedback = st.empty()

    with st.expander(
        "Log da execução",
        expanded=False,
        width="stretch",
    ):
        area_log = st.empty()

    def atualizar_log() -> None:
        area_log.code(
            _conteudo_log(chave_logs),
            language=None,
            wrap_lines=True,
        )

    def registrar(mensagem: str) -> None:
        st.session_state[
            chave_logs
        ].append(str(mensagem))
        atualizar_log()

    atualizar_log()

    return feedback, registrar


def renderizar_logs(
    logs: list[str],
    *,
    titulo: str = "Log da execução",
) -> None:
    if not logs:
        return

    with st.expander(
        titulo,
        expanded=False,
        width="stretch",
    ):
        st.code(
            "\n".join(
                logs[-MAX_LINHAS_LOG:]
            ),
            language=None,
            wrap_lines=True,
        )
