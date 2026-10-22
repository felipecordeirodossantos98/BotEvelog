from datetime import date
from pathlib import Path

import streamlit as st

from automacoes.gerar_romaneios import (
    executar_gerar_romaneios,
    executar_manifestos_romaneios,
)
from elementos_visuais.visual_baixar_relatorios_analitico import (
    renderizar_baixar_relatorios_analitico,
)
from utils.interface import (
    LARGURA_BOTAO,
    LARGURA_COMPONENTE,
    avisar_variaveis_faltantes,
    criar_painel_execucao,
    error,
    success,
)
from utils.logs import criar_logger_execucao


CHAVE_LOGS = "visual_gerar_romaneios_logs"
CHAVE_TABELA_DESTINOS = "visual_gerar_romaneios_tabela_destinos"
CHAVE_ARQUIVO_BASE = "visual_gerar_romaneios_arquivo_base"
CHAVE_MANIFESTOS_CONCLUIDOS = "visual_gerar_romaneios_manifestos_concluidos"


def renderizar_menu_automacoes_operacao() -> None:
    tab_romaneios, tab_analitico = st.tabs(
        ["Gerar romaneios", "Relatório Analítico"]
    )

    with tab_romaneios:
        renderizar_gerar_romaneios()

    with tab_analitico:
        renderizar_baixar_relatorios_analitico()


def renderizar_gerar_romaneios() -> None:
    st.subheader("Gerar Romaneios")
    faltantes = avisar_variaveis_faltantes("gerar_romaneios")

    hoje = date.today()
    periodo = st.date_input(
        "Período de extração",
        value=(hoje, hoje),
        min_value=date(2000, 1, 1),
        max_value=hoje,
        key="gerar_romaneios_periodo",
        format="DD/MM/YYYY",
        width=LARGURA_COMPONENTE,
    )
    if isinstance(periodo, tuple):
        if not periodo:
            data_inicial = data_final = None
        elif len(periodo) == 1:
            data_inicial = data_final = periodo[0]
        else:
            data_inicial, data_final = periodo[0], periodo[1]
    else:
        data_inicial = data_final = periodo

    if data_inicial and data_final:
        dias = (data_final - data_inicial).days + 1
        st.caption(
            f"Período selecionado: {dias} dia(s). "
            f"A data máxima permitida é {hoje:%d/%m/%Y}."
        )

    iniciar = st.button(
        "Executar geração",
        type="primary",
        key="gerar_romaneios_iniciar",
        width=LARGURA_BOTAO,
        disabled=bool(faltantes) or not data_inicial or not data_final,
    )
    if not iniciar:
        _renderizar_tabela_destinos()
        return

    st.session_state.pop(CHAVE_TABELA_DESTINOS, None)
    st.session_state.pop(CHAVE_ARQUIVO_BASE, None)
    st.session_state.pop(CHAVE_MANIFESTOS_CONCLUIDOS, None)

    _, log_visual = criar_painel_execucao(CHAVE_LOGS)
    log, _ = criar_logger_execucao(
        "gerar_romaneios",
        log_visual,
        rotulo="gerar_romaneios",
    )
    try:
        log("Iniciando geração de romaneios.")
        log(
            f"Período: {data_inicial:%d/%m/%Y} a "
            f"{data_final:%d/%m/%Y}."
        )

        def atualizar_progresso(atual, total, data):
            log(f"Extraindo {data:%d/%m/%Y} ({atual}/{total}).")

        with st.spinner("Extraindo e unificando romaneios..."):
            resultado = executar_gerar_romaneios(
                data_inicial,
                data_final,
                callback=atualizar_progresso,
                log=log,
            )
        st.session_state[CHAVE_TABELA_DESTINOS] = resultado[
            "tabela_destinos"
        ]
        st.session_state[CHAVE_ARQUIVO_BASE] = resultado["arquivo_final"]

        mensagem = (
            "Prévia pronta para conferência. Os romaneios ainda não foram "
            "gerados. "
            f"{resultado['linhas_emissao']} registro(s) com status EMISSAO "
            f"salvos em: {resultado['arquivo_final']}"
        )
        log(mensagem)
        success(mensagem)
        _renderizar_tabela_destinos()
    except Exception as erro_execucao:
        log(f"Erro: {type(erro_execucao).__name__}: {erro_execucao}")
        error(f"A geração dos romaneios foi interrompida: {erro_execucao}")


def _renderizar_tabela_destinos() -> None:
    tabela = st.session_state.get(CHAVE_TABELA_DESTINOS)
    if tabela is None:
        return

    st.markdown("#### CTEs agrupados por destino")
    st.caption(
        f"{len(tabela)} grupo(s) por região, UF e cidade. "
        "A coluna CTEs lista os conhecimentos incluídos em cada grupo."
    )
    st.dataframe(
        tabela,
        hide_index=True,
        width="stretch",
        column_config={
            "ROTA": st.column_config.TextColumn("Rota"),
            "CODIGO_ROTA": st.column_config.TextColumn("Código da rota"),
        },
    )
    arquivo_base = st.session_state.get(CHAVE_ARQUIVO_BASE)
    if not arquivo_base:
        return

    st.info(
        "Confira a tabela acima. Ao continuar, o sistema entrará no Fraction "
        "e enviará os CTEs agrupados para gerar os romaneios."
    )
    st.caption(f"Base que será usada: {Path(arquivo_base).name}")
    processado = st.session_state.get(CHAVE_MANIFESTOS_CONCLUIDOS, False)
    iniciar_manifestos = st.button(
        "Gerar romaneios no Fraction",
        type="primary",
        key="gerar_romaneios_iniciar_manifestos",
        width=LARGURA_BOTAO,
        disabled=True,
    )
    st.caption("Envio ao Fraction temporariamente desabilitado.")
    if processado:
        st.success("Os romaneios desta base já foram enviados ao Fraction.")
    if not iniciar_manifestos:
        return

    _, log_visual = criar_painel_execucao(CHAVE_LOGS)
    log, _ = criar_logger_execucao(
        "gerar_romaneios",
        log_visual,
        rotulo="processar_manifestos",
    )
    try:
        log("Usuário confirmou a prévia e iniciou a geração dos romaneios.")
        with st.spinner("Gerando romaneios no Fraction..."):
            resultado = executar_manifestos_romaneios(
                arquivo_base,
                log=log,
            )
        st.session_state[CHAVE_MANIFESTOS_CONCLUIDOS] = True
        mensagem = (
            f"Romaneios enviados: {resultado['grupos_manifesto']} grupo(s), "
            f"{resultado['ctes_manifesto']} CTE(s) em "
            f"{resultado['lotes_manifesto']} lote(s)."
        )
        log(mensagem)
        success(mensagem)
    except Exception as erro_execucao:
        log(f"Erro: {type(erro_execucao).__name__}: {erro_execucao}")
        error(f"A geração dos romaneios foi interrompida: {erro_execucao}")
