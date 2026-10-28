from __future__ import annotations

from datetime import date
from hashlib import sha256
from pathlib import Path
import tempfile
import unicodedata

import pandas as pd
import streamlit as st

from automacoes.gerar_romaneios import (
    associar_destinos_fraction,
    carregar_codigos_preferenciais_unidades,
    carregar_destinos_fraction,
    carregar_regras_rotas_localidade,
    executar_manifestos_romaneios,
    enviar_pdf_para_impressora_padrao,
    mapear_rotas_para_grupos,
    preparar_lote_impressao_romaneios,
)
from automacoes.gerar_romaneios_emissao import (
    LIMITE_DIAS_PERFORMANCE_EMISSAO,
    baixar_etiquetas_drive,
    carregar_codigos_preferenciais_unidades_emissao,
    extrair_performance_intervalo,
    montar_fila_emissao,
)
from servicos.fraction import CredenciaisFractionInvalidasError
from utils.interface import (
    LARGURA_BOTAO,
    avisar_variaveis_faltantes,
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
    unidades_relevantes: set[str],
) -> pd.DataFrame:
    rotas_por_uf, rotas_por_cidade = carregar_regras_rotas_localidade(
        ARQUIVO_ROTAS_ROMANEIOS
    )
    codigos_preferenciais = carregar_codigos_preferenciais_unidades()
    relevantes = {_normalizar_rota(unidade) for unidade in unidades_relevantes}
    linhas = []

    for uf, unidade in rotas_por_uf.items():
        chave_unidade = _normalizar_rota(unidade)
        if chave_unidade not in relevantes:
            continue
        linhas.append(
            {
                "Unidade": unidade,
                "UF": uf,
                "Cidade": "Todas",
                "Código preferencial": codigos_preferenciais.get(chave_unidade, ""),
            }
        )

    for cidade, unidade in rotas_por_cidade.items():
        chave_unidade = _normalizar_rota(unidade)
        if chave_unidade not in relevantes:
            continue
        linhas.append(
            {
                "Unidade": unidade,
                "UF": "",
                "Cidade": cidade,
                "Código preferencial": codigos_preferenciais.get(chave_unidade, ""),
            }
        )

    linhas.sort(
        key=lambda linha: (
            _normalizar_rota(linha["Unidade"]),
            linha["UF"],
            _normalizar_rota(linha["Cidade"]),
        )
    )
    return pd.DataFrame(
        linhas,
        columns=["Unidade", "UF", "Cidade", "Código preferencial"],
    )


def _volume_divergente(linha: pd.Series) -> bool:
    volume_sistema = linha["Volumes Sistema"]
    if pd.isna(volume_sistema):
        return False
    return (
        int(volume_sistema) != int(linha["Volumes Etiquetas"])
        or bool(str(linha["Etiquetas faltantes"] or "").strip())
    )


def _normalizar_periodo_emissao(valor):
    if isinstance(valor, tuple):
        if not valor:
            return None, None
        if len(valor) == 1:
            return valor[0], valor[0]
        return valor[0], valor[1]
    return valor, valor


def _renderizar_extracao_performance() -> tuple[str, str] | None:
    faltantes = avisar_variaveis_faltantes("extrair_performance_emissao")
    faltantes_drive = avisar_variaveis_faltantes("importar_etiquetas_drive")

    hoje = date.today()
    periodo_selecionado = st.date_input(
        "Período de extração",
        value=(hoje, hoje),
        min_value=date(2000, 1, 1),
        max_value=hoje,
        key="gerar_romaneios_emissao_performance_periodo_extracao",
        format="DD/MM/YYYY",
    )
    data_inicial, data_final = _normalizar_periodo_emissao(
        periodo_selecionado
    )

    periodo_valido = data_inicial is not None and data_final is not None
    quantidade_dias = (
        (data_final - data_inicial).days + 1
        if periodo_valido
        else 0
    )
    if not periodo_valido:
        warning("Selecione as datas inicial e final do período de extração.")
    elif data_final < data_inicial:
        warning("A data final precisa ser igual ou posterior à data inicial.")
    elif quantidade_dias > LIMITE_DIAS_PERFORMANCE_EMISSAO:
        warning(
            "O período máximo permitido é de "
            f"{LIMITE_DIAS_PERFORMANCE_EMISSAO} dias, contando as datas inicial "
            "e final."
        )
    else:
        st.caption(
            "Será feita uma extração para o intervalo de "
            f"{data_inicial:%d/%m/%Y} a {data_final:%d/%m/%Y}. "
            f"Limite: {LIMITE_DIAS_PERFORMANCE_EMISSAO} dias."
        )

    token = st.text_input(
        "Token MFA do Fraction",
        type="password",
        max_chars=6,
        placeholder="Digite o código MFA",
        key="gerar_romaneios_emissao_performance_token_mfa",
    )
    processar = st.button(
        "Processar",
        type="primary",
        key="gerar_romaneios_emissao_processar",
        width=LARGURA_BOTAO,
        disabled=(
            bool(faltantes or faltantes_drive)
            or not periodo_valido
            or data_final < data_inicial
            or quantidade_dias > LIMITE_DIAS_PERFORMANCE_EMISSAO
        ),
    )

    periodo = (
        (data_inicial.isoformat(), data_final.isoformat())
        if periodo_valido
        else None
    )
    if processar:
        if not token.strip():
            error("Informe o Token MFA para processar a fila.")
        else:
            st.session_state.pop(CHAVE_RESULTADO, None)
            st.session_state[CHAVE_LOGS] = []
            log, _ = criar_logger_execucao(
                "gerar_romaneios",
                st.session_state[CHAVE_LOGS].append,
                rotulo="processar_emissao",
            )
            try:
                with tempfile.TemporaryDirectory(
                    prefix="botevelog_emissao_"
                ) as diretorio_temporario:
                    pasta_temporaria = Path(diretorio_temporario)
                    with st.spinner("Extraindo a Performance do período..."):
                        extracao = extrair_performance_intervalo(
                            data_inicial,
                            data_final,
                            token,
                            pasta_temporaria=pasta_temporaria,
                            log=log,
                        )
                    conteudo_performance = Path(
                        extracao["arquivo_consolidado"]
                    ).read_bytes()
                    nome_arquivo_performance = Path(
                        extracao["arquivo_consolidado"]
                    ).name

                    with st.spinner("Baixando as etiquetas do Drive..."):
                        caminho_zip = baixar_etiquetas_drive(
                            pasta_destino=pasta_temporaria,
                            log=log,
                        )
                    conteudo_etiquetas = caminho_zip.read_bytes()
                    nome_arquivo_etiquetas = caminho_zip.name

                    identificador = sha256(
                        conteudo_performance
                        + b"\0"
                        + nome_arquivo_etiquetas.encode("utf-8")
                        + b"\0"
                        + conteudo_etiquetas
                        + b"\0"
                        + "|".join(periodo or ()).encode("utf-8")
                    ).hexdigest()

                    with st.spinner("Montando a fila para conferência..."):
                        dados = montar_fila_emissao(
                            conteudo_performance,
                            conteudo_etiquetas,
                            nome_arquivo_etiquetas=nome_arquivo_etiquetas,
                            pasta_temporaria=pasta_temporaria,
                            log=log,
                        )

                st.session_state[CHAVE_RESULTADO] = {
                    "periodo": periodo,
                    "identificador": identificador,
                    "arquivo_performance_nome": nome_arquivo_performance,
                    "nome_arquivo_etiquetas": nome_arquivo_etiquetas,
                    **dados,
                    "confirmacoes": [],
                    "enviado": False,
                    "resumo_envio": None,
                }
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
                if dados["tabela"].empty:
                    warning(
                        "Nenhum pedido está com Status EMISSAO; a fila de envio ficou vazia."
                    )
                else:
                    success(f"Fila montada: {len(dados['tabela'])} pedido(s).")
            except CredenciaisFractionInvalidasError as erro_credenciais:
                warning(str(erro_credenciais))
            except Exception as erro:
                log(f"Falha ao processar a fila: {type(erro).__name__}: {erro}")
                error(f"Não foi possível processar a fila: {erro}")

    return periodo


def renderizar_gerar_romaneios_emissao() -> None:
    st.subheader("Gerar romaneios de Emissão")

    periodo = _renderizar_extracao_performance()
    resultado_atual = st.session_state.get(CHAVE_RESULTADO)
    if resultado_atual and resultado_atual.get("periodo") != periodo:
        st.session_state.pop(CHAVE_RESULTADO, None)
        resultado_atual = None
    if not resultado_atual:
        renderizar_logs(st.session_state.get(CHAVE_LOGS, []))
        return

    identificador = resultado_atual["identificador"]
    tabela: pd.DataFrame = resultado_atual["tabela"]
    tabela_completa: pd.DataFrame = resultado_atual.get(
        "tabela_completa",
        tabela,
    )
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
        "UF": st.column_config.TextColumn("UF"),
        "Cidade": st.column_config.TextColumn("Cidade"),
        "Destino": st.column_config.TextColumn("Destino"),
        "Manifesto": st.column_config.TextColumn("Manifesto"),
        "Rota": st.column_config.TextColumn("Rota"),
        "Origem": st.column_config.TextColumn("Origem"),
        "Status": st.column_config.TextColumn("Status"),
    }
    st.markdown("#### Todos os pedidos")
    st.dataframe(
        tabela_completa,
        hide_index=True,
        width="stretch",
        column_config=configuracao_colunas_fila,
    )

    if tabela.empty:
        st.info("Nenhum pedido da base completa ficou na fila EMISSAO.")
        sem_match = resultado_atual.get("shipment_ids_sem_match", [])
        if sem_match:
            with st.expander("ShipmentIds sem correspondência"):
                st.code("\n".join(sem_match), language=None, wrap_lines=True)
        renderizar_logs(st.session_state.get(CHAVE_LOGS, []))
        return

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
    codigos_preferenciais = carregar_codigos_preferenciais_unidades_emissao()
    grupos_validos, destinos_pendentes = associar_destinos_fraction(
        grupos,
        carregar_destinos_fraction(),
        codigos_preferenciais=codigos_preferenciais,
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
    if rotas_sem_unidade:
        tabela_mapa_rotas = _montar_tabela_rotas_json(rotas_sem_unidade)
        if not tabela_mapa_rotas.empty:
            st.markdown("#### Regras de rota no JSON")
            st.dataframe(
                tabela_mapa_rotas,
                hide_index=True,
                width="stretch",
                column_config={
                    "Unidade": st.column_config.TextColumn("Unidade"),
                    "UF": st.column_config.TextColumn("UF"),
                    "Cidade": st.column_config.TextColumn("Cidade"),
                    "Código preferencial": st.column_config.TextColumn(
                        "Código preferencial"
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
        log, _ = criar_logger_execucao(
            "gerar_romaneios",
            st.session_state[CHAVE_LOGS].append,
            rotulo="processar_emissao",
        )
        nome_fontes = (
            f"{resultado_atual['arquivo_performance_nome']} + "
            f"{resultado_atual['nome_arquivo_etiquetas']}"
        )
        try:
            log("Usuário confirmou a prévia e iniciou a emissão dos romaneios.")
            with st.spinner("Gerando romaneios de Emissão no Fraction..."):
                resultado = executar_manifestos_romaneios(
                    nome_fontes,
                    tabela_previa=tabela_envio,
                    codigos_preferenciais=codigos_preferenciais,
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
            "envia todas as páginas em um único lote para a impressora padrão."
        )
        with st.expander("PDFs salvos", expanded=False):
            st.code("\n".join(arquivos_pdf), language=None, wrap_lines=True)

        if resultado_atual.get("impressao_solicitada"):
            success("O lote foi enviado pelo SumatraPDF à fila da impressora.")
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
                    log, _ = criar_logger_execucao(
                        "gerar_romaneios",
                        st.session_state[CHAVE_LOGS].append,
                        rotulo="imprimir_emissao",
                    )
                    try:
                        with st.spinner("Validando e enviando o lote para impressão..."):
                            with tempfile.TemporaryDirectory(
                                prefix="botevelog_impressao_"
                            ) as diretorio_temporario:
                                caminho_lote = preparar_lote_impressao_romaneios(
                                    arquivos_pdf,
                                    pasta_temporaria=Path(diretorio_temporario),
                                    log=log,
                                )
                                enviar_pdf_para_impressora_padrao(
                                    caminho_lote,
                                    log=log,
                                )
                        resultado_atual["arquivo_lote_impressao"] = None
                        resultado_atual["impressao_solicitada"] = True
                        st.session_state[CHAVE_RESULTADO] = resultado_atual
                        success(
                            "Lote enviado pelo SumatraPDF. Confira a fila "
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
