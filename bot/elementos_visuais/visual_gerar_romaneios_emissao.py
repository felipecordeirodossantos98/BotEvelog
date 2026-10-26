from __future__ import annotations

from hashlib import sha256
import json
import unicodedata

import pandas as pd
import streamlit as st

from automacoes.gerar_romaneios import (
    associar_destinos_fraction,
    carregar_destinos_fraction,
    executar_manifestos_romaneios,
    enviar_pdf_para_impressora_padrao,
    mapear_rotas_para_grupos,
    preparar_lote_impressao_romaneios,
)
from automacoes.gerar_romaneios_emissao import montar_fila_emissao
from servicos.fraction import CredenciaisFractionInvalidasError
from utils.interface import (
    LARGURA_BOTAO,
    avisar_variaveis_faltantes,
    criar_painel_execucao,
    error,
    renderizar_logs,
    success,
    warning,
)
from utils.config import ARQUIVO_ROTAS_ROMANEIOS
from utils.logs import criar_logger_execucao


CHAVE_RESULTADO = "visual_gerar_romaneios_emissao_resultado"
CHAVE_LOGS = "visual_gerar_romaneios_emissao_logs"


def _normalizar_rota(valor: object) -> str:
    texto = unicodedata.normalize("NFKD", str(valor).strip())
    texto = "".join(
        caractere
        for caractere in texto
        if not unicodedata.combining(caractere)
    )
    return " ".join(texto.upper().split())


def _montar_tabela_rotas_json(
    origens_relevantes: set[str],
    destinos_ambiguos: set[str],
) -> pd.DataFrame:
    with ARQUIVO_ROTAS_ROMANEIOS.open("r", encoding="utf-8-sig") as arquivo:
        dados = json.load(arquivo)

    registros = dados.get("rotas", []) if isinstance(dados, dict) else []
    origens_normalizadas = {_normalizar_rota(origem) for origem in origens_relevantes}
    destinos_normalizados = {
        _normalizar_rota(destino) for destino in destinos_ambiguos
    }
    por_origem: dict[str, dict[str, object]] = {}

    for registro in registros:
        if not isinstance(registro, dict):
            continue
        origem = str(registro.get("origem") or "").strip()
        destino = str(registro.get("destino") or "").strip()
        if not origem or not destino:
            continue
        if (
            _normalizar_rota(origem) not in origens_normalizadas
            and _normalizar_rota(destino) not in destinos_normalizados
        ):
            continue

        chave_origem = _normalizar_rota(origem)
        item = por_origem.setdefault(
            chave_origem,
            {"Origem": origem, "destinos": []},
        )
        destinos = item["destinos"]
        if _normalizar_rota(destino) not in {
            _normalizar_rota(valor) for valor in destinos
        }:
            destinos.append(destino)

    linhas = [
        {
            "Origem": item["Origem"],
            "Destino(s)": "\n".join(sorted(item["destinos"], key=_normalizar_rota)),
        }
        for _, item in sorted(por_origem.items())
    ]
    return pd.DataFrame(linhas, columns=["Origem", "Destino(s)"])


def _volume_divergente(linha: pd.Series) -> bool:
    volume_sistema = linha["Volumes Sistema"]
    if pd.isna(volume_sistema):
        return False
    return (
        int(volume_sistema) != int(linha["Volumes Etiquetas"])
        or bool(str(linha["Etiquetas faltantes"] or "").strip())
    )


def renderizar_gerar_romaneios_emissao() -> None:
    st.subheader("Gerar romaneios de Emissão")
    st.caption(
        "Importe a planilha Performance e um TXT ou ZIP com os TXT(s) de etiquetas. "
        "Os ShipmentIds ausentes serão consultados individualmente no Fraction; "
        "os CTEs de toda a fila serão enviados em um relatório complementar para "
        "atualizar Destino e Status. Só pedidos cujo Status final seja EMISSAO "
        "permanecem na fila."
    )

    arquivo_performance = st.file_uploader(
        "Planilha Performance de Entrega",
        type=["xlsx"],
        key="gerar_romaneios_emissao_performance",
        help=(
            "A planilha precisa conter ShipmentId, Codigo, Destino e Volume. "
            "Status será preenchido quando a coluna estiver disponível."
        ),
    )
    arquivo_etiquetas = st.file_uploader(
        "Etiquetas bipadas (TXT ou ZIP de TXT)",
        type=["txt", "zip"],
        key="gerar_romaneios_emissao_etiquetas",
        help=(
            "O ZIP pode conter vários TXT, inclusive em subpastas. Cada TXT é "
            "validado separadamente e todos alimentam a mesma fila. Também aceita "
            "TXT simples ou exportação estruturada do Coletor Evelog."
        ),
    )

    conteudo_performance = (
        arquivo_performance.getvalue() if arquivo_performance else b""
    )
    conteudo_etiquetas = arquivo_etiquetas.getvalue() if arquivo_etiquetas else b""
    identificador = (
        sha256(
            conteudo_performance
            + b"\0"
            + (arquivo_etiquetas.name.encode("utf-8") if arquivo_etiquetas else b"")
            + b"\0"
            + conteudo_etiquetas
        ).hexdigest()
        if arquivo_performance is not None and arquivo_etiquetas is not None
        else None
    )

    resultado_atual = st.session_state.get(CHAVE_RESULTADO)
    if not resultado_atual or resultado_atual.get("identificador") != identificador:
        st.session_state.pop(CHAVE_RESULTADO, None)
        resultado_atual = None

    montar = st.button(
        "Montar fila de envio",
        type="primary",
        key="gerar_romaneios_emissao_montar",
        width=LARGURA_BOTAO,
        disabled=identificador is None,
    )

    if montar:
        st.session_state.pop(CHAVE_RESULTADO, None)
        st.session_state[CHAVE_LOGS] = []
        _, log_visual = criar_painel_execucao(CHAVE_LOGS)
        log, _ = criar_logger_execucao(
            "gerar_romaneios",
            log_visual,
            rotulo="emissao",
        )
        try:
            with st.spinner(
                "Consultando pedidos e atualizando Destino e Status da fila..."
            ):
                dados = montar_fila_emissao(
                    conteudo_performance,
                    conteudo_etiquetas,
                    nome_arquivo_etiquetas=arquivo_etiquetas.name,
                    log=log,
                )
            st.session_state[CHAVE_RESULTADO] = {
                "identificador": identificador,
                **dados,
                "confirmacoes": [],
                "enviado": False,
                "resumo_envio": None,
            }
            resultado_atual = st.session_state[CHAVE_RESULTADO]
            if dados["shipment_ids_sem_match"]:
                success(
                    f"{len(dados['shipment_ids_sem_match'])} ShipmentId(s) ausentes "
                    "da Performance foram consultados individualmente no Fraction; "
                    "somente os que ficam com Status final EMISSAO permanecem na fila."
                )
            if dados["shipment_ids_sem_retorno_fraction"]:
                warning(
                    f"{len(dados['shipment_ids_sem_retorno_fraction'])} pedido(s) "
                    "consultados não retornaram CTE no Fraction; permanecerão na "
                    "fila e não serão enviados até haver um CTE."
                )
            if dados["shipment_ids_sem_destino_performance"]:
                warning(
                    f"{len(dados['shipment_ids_sem_destino_performance'])} pedido(s) "
                    "consultados ficaram sem Destino no relatório de Performance; "
                    "permanecerão na fila e não serão enviados até haver uma rota."
                )
            if dados["shipment_ids_sem_status_performance"]:
                warning(
                    f"{len(dados['shipment_ids_sem_status_performance'])} pedido(s) "
                    "não têm Status preenchido nas planilhas de Performance e foram "
                    "removidos da fila."
                )
            if dados["shipment_ids_fora_emissao"]:
                st.info(
                    f"{len(dados['shipment_ids_fora_emissao'])} pedido(s) sem Status "
                    "EMISSAO (status diferente ou não preenchido) não serão enviados."
                )
            if dados["tabela"].empty:
                warning(
                    "Nenhum pedido está com Status EMISSAO; a fila de envio ficou vazia."
                )
            else:
                success(
                    f"Fila montada: {len(dados['tabela'])} pedido(s) e "
                    f"{dados['total_etiquetas']} etiqueta(s)."
                )
        except CredenciaisFractionInvalidasError as erro_credenciais:
            warning(str(erro_credenciais))
            return
        except (UnicodeDecodeError, ValueError) as erro_dados:
            log(f"Erro nos arquivos: {type(erro_dados).__name__}: {erro_dados}")
            error(f"Não foi possível montar a fila: {erro_dados}")
            return
        except Exception as erro_execucao:
            log(f"Erro: {type(erro_execucao).__name__}: {erro_execucao}")
            error(f"A montagem da fila foi interrompida: {erro_execucao}")
            return

    resultado_atual = st.session_state.get(CHAVE_RESULTADO)
    if not resultado_atual or resultado_atual.get("identificador") != identificador:
        renderizar_logs(st.session_state.get(CHAVE_LOGS, []))
        return

    tabela: pd.DataFrame = resultado_atual["tabela"]
    if tabela.empty:
        sem_match = resultado_atual.get("shipment_ids_sem_match", [])
        if sem_match:
            with st.expander("ShipmentIds sem correspondência"):
                st.code("\n".join(sem_match), language=None, wrap_lines=True)
        renderizar_logs(st.session_state.get(CHAVE_LOGS, []))
        return

    configuracao_colunas_fila = {
        "CTE": st.column_config.TextColumn("CTE"),
        "ShipmentId": st.column_config.TextColumn("ShipmentId"),
        "Etiquetas do pedido": st.column_config.TextColumn(
            "Etiquetas do pedido", width="large"
        ),
        "Volumes Sistema": st.column_config.NumberColumn("Volumes Sistema"),
        "Volumes Etiquetas": st.column_config.NumberColumn("Volumes Etiquetas"),
        "Etiquetas faltantes": st.column_config.TextColumn(
            "Etiquetas faltantes", width="large"
        ),
        "CTEs para dar entrada": st.column_config.TextColumn(
            "CTEs para dar entrada", width="large"
        ),
        "Destino": st.column_config.TextColumn("Destino"),
        "Manifesto": st.column_config.TextColumn("Manifesto"),
        "Rota": st.column_config.TextColumn("Rota"),
        "Origem": st.column_config.TextColumn("Origem"),
        "Status": st.column_config.TextColumn("Status"),
    }
    with st.expander("Fila completa para conferência", expanded=False):
        st.dataframe(
            tabela,
            hide_index=True,
            width="stretch",
            column_config=configuracao_colunas_fila,
        )

    divergencias = tabela[tabela.apply(_volume_divergente, axis=1)]
    sem_volume = tabela[tabela["Volumes Sistema"].isna()]
    if not divergencias.empty:
        warning(
            f"{len(divergencias)} pedido(s) têm divergência entre Volume e "
            "etiquetas. Confirme os pedidos que devem entrar na fila de envio."
        )
        with st.form(
            key=f"gerar_romaneios_emissao_confirmar_{identificador[:16]}"
        ):
            selecionados: list[str] = []
            for _, linha in divergencias.iterrows():
                shipment_id = str(linha["ShipmentId"])
                etiquetas_faltantes = str(linha["Etiquetas faltantes"] or "").strip()
                quantidade_faltantes = (
                    len(etiquetas_faltantes.splitlines()) if etiquetas_faltantes else 0
                )
                autorizar = st.checkbox(
                    f"Autorizar ShipmentId {shipment_id}: Volume na Performance "
                    f"{int(linha['Volumes Sistema'])}; etiquetas bipadas "
                    f"{int(linha['Volumes Etiquetas'])}; "
                    f"{quantidade_faltantes} etiqueta(s) faltante(s).",
                    key=(
                        f"gerar_romaneios_emissao_confirmar_{identificador[:16]}_"
                        f"{shipment_id}"
                    ),
                )
                if autorizar:
                    selecionados.append(shipment_id)

            confirmar = st.form_submit_button(
                "Confirmar pedidos divergentes",
                type="primary",
                width=LARGURA_BOTAO,
            )

        if confirmar:
            resultado_atual["confirmacoes"] = selecionados
            st.session_state[CHAVE_RESULTADO] = resultado_atual
            success(
                f"Confirmação registrada: {len(selecionados)} de "
                f"{len(divergencias)} pedido(s) divergente(s) liberado(s)."
            )

    if not sem_volume.empty:
        warning(
            f"{len(sem_volume)} pedido(s) não têm Volume na Performance e "
            "não serão enviados."
        )

    tabela_envio = tabela.copy()
    confirmacoes = set(resultado_atual.get("confirmacoes", []))
    for indice, linha in tabela_envio.iterrows():
        if pd.isna(linha["Volumes Sistema"]):
            tabela_envio.at[indice, "CTEs para dar entrada"] = ""
        elif _volume_divergente(linha) and str(linha["ShipmentId"]) not in confirmacoes:
            tabela_envio.at[indice, "CTEs para dar entrada"] = ""

    tem_ctes_envio = tabela_envio["CTEs para dar entrada"].fillna("").map(
        lambda valor: any(
            linha.strip() and linha.strip().lower() != "nan"
            for linha in str(valor).splitlines()
        )
    )
    rota_linha = tabela_envio["Rota"].fillna("").astype(str).str.strip()
    rota_original = tabela["Rota"].fillna("").astype(str).str.strip()
    sem_rota_ou_ambigua = tabela.loc[
        rota_original.isin({"", "Sem rota", "Rota ambígua"})
    ].copy()
    grupos, _ = mapear_rotas_para_grupos(tabela_envio)
    grupos_validos, destinos_pendentes = associar_destinos_fraction(
        grupos,
        carregar_destinos_fraction(),
    )

    rotas_prontas = {grupo["ROTA"] for grupo in grupos_validos}
    prontos_para_envio = tabela_envio.loc[
        tem_ctes_envio & rota_linha.isin(rotas_prontas)
    ].copy()
    st.markdown("#### Prontos para envio")
    if prontos_para_envio.empty:
        st.info("Nenhum pedido está pronto para envio.")
    else:
        st.dataframe(
            prontos_para_envio,
            hide_index=True,
            width="stretch",
            column_config=configuracao_colunas_fila,
        )

    st.markdown("#### Sem rota ou com mais de uma rota")
    if sem_rota_ou_ambigua.empty:
        st.info("Não há pedidos sem rota ou com rota ambígua.")
    else:
        st.dataframe(
            sem_rota_ou_ambigua,
            hide_index=True,
            width="stretch",
            column_config=configuracao_colunas_fila,
        )
        warning(
            f"{len(sem_rota_ou_ambigua)} pedido(s) não têm rota única e ficarão "
            "fora do envio."
        )

    rotas_sem_unidade = {
        grupo["ROTA"]
        for grupo in grupos
        if grupo["ROTA"] not in rotas_prontas
    }
    if destinos_pendentes:
        warning(
            f"{len(destinos_pendentes)} rota(s) não têm unidade única no catálogo "
            "do Fraction e ficarão fora do envio."
        )
    destinos_ambiguos = set(
        sem_rota_ou_ambigua.loc[
            sem_rota_ou_ambigua["Rota"].eq("Rota ambígua"),
            "Destino",
        ].dropna().astype(str)
    )
    if rotas_sem_unidade or destinos_ambiguos:
        tabela_mapa_rotas = _montar_tabela_rotas_json(
            rotas_sem_unidade,
            destinos_ambiguos,
        )
        if not tabela_mapa_rotas.empty:
            st.markdown("#### Rotas do JSON: origem e destino(s)")
            st.dataframe(
                tabela_mapa_rotas,
                hide_index=True,
                width="stretch",
                column_config={
                    "Origem": st.column_config.TextColumn("Origem"),
                    "Destino(s)": st.column_config.TextColumn(
                        "Destino(s)", width="large"
                    ),
                },
            )
    quantidade_ctes = sum(
        len(grupo["LISTA_CTES"]) for grupo in grupos_validos
    )
    st.caption(
        f"Prontos para envio: {quantidade_ctes} CTE(s) em "
        f"{len(grupos_validos)} rota(s)."
    )

    faltantes = avisar_variaveis_faltantes("gerar_romaneios")
    processado = bool(resultado_atual.get("enviado"))
    enviar = st.button(
        "Gerar romaneios no Fraction",
        type="primary",
        key="gerar_romaneios_emissao_enviar",
        width=LARGURA_BOTAO,
        disabled=(
            quantidade_ctes == 0
            or bool(faltantes)
            or processado
        ),
    )

    if processado:
        success(resultado_atual["resumo_envio"])

    if enviar:
        st.session_state[CHAVE_LOGS] = []
        _, log_visual = criar_painel_execucao(CHAVE_LOGS)
        log, _ = criar_logger_execucao(
            "gerar_romaneios",
            log_visual,
            rotulo="processar_emissao",
        )
        nome_fontes = (
            f"{arquivo_performance.name} + {arquivo_etiquetas.name}"
        )
        try:
            log("Usuário confirmou a prévia e iniciou a emissão dos romaneios.")
            with st.spinner("Gerando romaneios de Emissão no Fraction..."):
                resultado = executar_manifestos_romaneios(
                    nome_fontes,
                    tabela_previa=tabela_envio,
                    log=log,
                )
            mensagem = (
                f"Romaneios de Emissão enviados: "
                f"{resultado['grupos_manifesto']} grupo(s), "
                f"{resultado['ctes_manifesto']} CTE(s) em "
                f"{resultado['lotes_manifesto']} lote(s). "
                f"PDFs salvos: {len(resultado['arquivos_pdf'])}."
            )
            resultado_atual["enviado"] = True
            resultado_atual["resumo_envio"] = mensagem
            resultado_atual["arquivos_pdf"] = list(resultado["arquivos_pdf"])
            resultado_atual["arquivo_lote_impressao"] = None
            resultado_atual["impressao_solicitada"] = False
            st.session_state[CHAVE_RESULTADO] = resultado_atual
            log(mensagem)
            success(mensagem)
        except CredenciaisFractionInvalidasError as erro_credenciais:
            warning(str(erro_credenciais))
        except Exception as erro_execucao:
            log(f"Erro: {type(erro_execucao).__name__}: {erro_execucao}")
            error(f"A geração dos romaneios de Emissão foi interrompida: {erro_execucao}")

    arquivos_pdf = resultado_atual.get("arquivos_pdf", [])
    if resultado_atual.get("enviado") and arquivos_pdf:
        st.markdown("#### Impressão dos romaneios")
        st.caption(
            f"{len(arquivos_pdf)} PDF(s) já foram baixados e salvos. A impressão "
            "envia um único PDF combinado para a impressora padrão do Windows."
        )
        with st.expander("PDFs salvos", expanded=False):
            st.code("\n".join(arquivos_pdf), language=None, wrap_lines=True)

        if resultado_atual.get("impressao_solicitada"):
            success("O lote foi enviado ao aplicativo PDF padrão para impressão.")
            if resultado_atual.get("arquivo_lote_impressao"):
                st.caption(
                    f"Arquivo combinado: {resultado_atual['arquivo_lote_impressao']}"
                )
        else:
            with st.form(
                key=f"gerar_romaneios_emissao_imprimir_{identificador[:16]}"
            ):
                st.info(
                    "Os PDFs individuais continuarão salvos. O lote combinado será "
                    "enviado à impressora padrão somente após sua confirmação."
                )
                confirmar_impressao = st.checkbox(
                    f"Confirmo imprimir os {len(arquivos_pdf)} romaneio(s) do lote."
                )
                imprimir = st.form_submit_button(
                    "Imprimir todos os PDFs de uma vez",
                    type="primary",
                    width=LARGURA_BOTAO,
                )

            if imprimir:
                if not confirmar_impressao:
                    warning("Marque a confirmação para enviar o lote à impressora.")
                else:
                    _, log_visual = criar_painel_execucao(
                        CHAVE_LOGS,
                        limpar=False,
                    )
                    log, _ = criar_logger_execucao(
                        "gerar_romaneios",
                        log_visual,
                        rotulo="imprimir_emissao",
                    )
                    try:
                        with st.spinner("Validando e enviando o lote para impressão..."):
                            caminho_lote = resultado_atual.get(
                                "arquivo_lote_impressao"
                            )
                            if not caminho_lote:
                                caminho_lote = str(
                                    preparar_lote_impressao_romaneios(
                                        arquivos_pdf,
                                        log=log,
                                    )
                                )
                                resultado_atual["arquivo_lote_impressao"] = caminho_lote
                                st.session_state[CHAVE_RESULTADO] = resultado_atual
                            enviar_pdf_para_impressora_padrao(
                                caminho_lote,
                                log=log,
                            )
                        resultado_atual["impressao_solicitada"] = True
                        st.session_state[CHAVE_RESULTADO] = resultado_atual
                        success(
                            "Lote enviado ao aplicativo PDF padrão. Confira a fila "
                            "da impressora para acompanhar a impressão."
                        )
                    except Exception as erro_impressao:
                        log(
                            f"Falha na impressão: {type(erro_impressao).__name__}: "
                            f"{erro_impressao}"
                        )
                        error(
                            f"Não foi possível enviar o lote para impressão: "
                            f"{erro_impressao}"
                        )

    renderizar_logs(st.session_state.get(CHAVE_LOGS, []))
