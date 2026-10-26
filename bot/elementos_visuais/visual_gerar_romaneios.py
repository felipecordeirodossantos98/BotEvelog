from pathlib import Path
from hashlib import sha256

import streamlit as st

from automacoes.gerar_romaneios import (
    executar_gerar_romaneios,
    executar_manifestos_romaneios,
)
from elementos_visuais.visual_baixar_relatorios_analitico import (
    renderizar_baixar_relatorios_analitico,
)
from elementos_visuais.visual_dar_entrada_pedidos import (
    renderizar_dar_entrada_pedidos,
)
from elementos_visuais.visual_gerar_romaneios_emissao import (
    renderizar_gerar_romaneios_emissao,
)
from servicos.fraction import CredenciaisFractionInvalidasError
from utils.config import RESULTADOS_ENTRADA_PEDIDOS, RESULTADOS_ROMANEIOS
from utils.interface import (
    LARGURA_BOTAO,
    avisar_variaveis_faltantes,
    criar_painel_execucao,
    error,
    success,
    warning,
)
from utils.logs import criar_logger_execucao


CHAVE_LOGS = "visual_gerar_romaneios_logs"
CHAVE_TABELA_DESTINOS = "visual_gerar_romaneios_tabela_destinos"
CHAVE_ARQUIVO_BASE = "visual_gerar_romaneios_arquivo_base"
CHAVE_IDENTIFICADOR_BASE = "visual_gerar_romaneios_identificador_base"
CHAVE_MANIFESTOS_CONCLUIDOS = "visual_gerar_romaneios_manifestos_concluidos"


def renderizar_menu_automacoes_operacao() -> None:
    tab_dar_entrada, tab_romaneios, tab_emissao, tab_analitico = st.tabs(
        [
            "Dar entrada em pedidos",
            "Gerar romaneios",
            "Gerar romaneios de Emissão",
            "Relatório Analítico",
        ]
    )

    with tab_dar_entrada:
        renderizar_dar_entrada_pedidos()

    with tab_romaneios:
        renderizar_gerar_romaneios()

    with tab_emissao:
        renderizar_gerar_romaneios_emissao()

    with tab_analitico:
        renderizar_baixar_relatorios_analitico()


def renderizar_gerar_romaneios() -> None:
    st.subheader("Gerar Romaneios")
    RESULTADOS_ENTRADA_PEDIDOS.mkdir(parents=True, exist_ok=True)
    arquivos = sorted(
        RESULTADOS_ENTRADA_PEDIDOS.glob("*.xlsx"),
        key=lambda caminho: caminho.stat().st_mtime,
        reverse=True,
    )
    fontes = ["Importar planilha"]
    if arquivos:
        fontes.insert(0, "Base salva em entrada_pedidos")
    fonte = st.radio(
        "Origem da base",
        fontes,
        horizontal=True,
        key="gerar_romaneios_fonte_base",
    )
    conteudo_upload = None
    if fonte == "Base salva em entrada_pedidos":
        caminho_selecionado = st.selectbox(
            "Base de pedidos",
            options=[str(caminho) for caminho in arquivos],
            format_func=lambda caminho: Path(caminho).name,
            key="gerar_romaneios_base_selecionada",
        )
        chave_base = caminho_selecionado
        st.caption(f"Pasta das bases: {RESULTADOS_ENTRADA_PEDIDOS}")
    else:
        arquivo_importado = st.file_uploader(
            "Importe a planilha de Dar entrada em pedidos",
            type=["xlsx"],
            key="gerar_romaneios_importar_xlsx",
            help="A planilha precisa conter as colunas CTE e Destino, além dos dados da conferência.",
        )
        if arquivo_importado is not None:
            conteudo_upload = arquivo_importado.getvalue()
            chave_base = f"upload:{sha256(conteudo_upload).hexdigest()}"
        else:
            chave_base = None
    if st.session_state.get(CHAVE_IDENTIFICADOR_BASE) != chave_base:
        st.session_state.pop(CHAVE_TABELA_DESTINOS, None)
        st.session_state.pop(CHAVE_ARQUIVO_BASE, None)
        st.session_state.pop(CHAVE_IDENTIFICADOR_BASE, None)
        st.session_state.pop(CHAVE_MANIFESTOS_CONCLUIDOS, None)

    iniciar = st.button(
        "Importar base e consultar rotas",
        type="primary",
        key="gerar_romaneios_iniciar",
        width=LARGURA_BOTAO,
        disabled=not chave_base,
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
        nome_base = (
            arquivo_importado.name
            if conteudo_upload is not None
            else Path(caminho_selecionado).name
        )
        log(f"Importando base de pedidos: {nome_base}.")
        with st.spinner("Importando a base e consultando as rotas..."):
            resultado = executar_gerar_romaneios(
                conteudo_upload if conteudo_upload is not None else caminho_selecionado,
                nome_arquivo=nome_base,
                log=log,
            )
        st.session_state[CHAVE_TABELA_DESTINOS] = resultado[
            "tabela_destinos"
        ]
        st.session_state[CHAVE_IDENTIFICADOR_BASE] = chave_base
        st.session_state[CHAVE_ARQUIVO_BASE] = resultado["arquivo_final"]

        mensagem = (
            "Base importada para conferência: "
            f"{resultado['linhas_importadas']} linha(s) em "
            f"{resultado['arquivo_final']}"
        )
        log(mensagem)
        success(mensagem)
        _renderizar_tabela_destinos()
    except CredenciaisFractionInvalidasError as erro_credenciais:
        warning(str(erro_credenciais))
    except Exception as erro_execucao:
        log(f"Erro: {type(erro_execucao).__name__}: {erro_execucao}")
        error(f"A geração dos romaneios foi interrompida: {erro_execucao}")


def _renderizar_tabela_destinos() -> None:
    tabela = st.session_state.get(CHAVE_TABELA_DESTINOS)
    if tabela is None:
        return

    st.markdown("#### Base de pedidos e rotas")
    st.caption(f"{len(tabela)} pedido(s) na base importada.")
    st.dataframe(
        tabela,
        hide_index=True,
        width="stretch",
        column_config={
            "Rota": st.column_config.TextColumn("Rota"),
            "Código da rota": st.column_config.TextColumn("Código da rota"),
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
        disabled=processado or bool(avisar_variaveis_faltantes("gerar_romaneios")),
    )
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
                tabela_previa=tabela,
                log=log,
            )
        st.session_state[CHAVE_MANIFESTOS_CONCLUIDOS] = True
        mensagem = (
            f"Romaneios enviados: {resultado['grupos_manifesto']} grupo(s), "
            f"{resultado['ctes_manifesto']} CTE(s) em "
            f"{resultado['lotes_manifesto']} lote(s). "
            f"PDFs salvos: {len(resultado['arquivos_pdf'])} em "
            f"{RESULTADOS_ROMANEIOS}."
        )
        log(mensagem)
        success(mensagem)
    except CredenciaisFractionInvalidasError as erro_credenciais:
        warning(str(erro_credenciais))
    except Exception as erro_execucao:
        log(f"Erro: {type(erro_execucao).__name__}: {erro_execucao}")
        error(f"A geração dos romaneios foi interrompida: {erro_execucao}")
