from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from pathlib import Path
import tempfile
import unicodedata

import pandas as pd
import streamlit as st

from servicos.fraction import (
    CredenciaisFractionInvalidasError,
    associar_destinos_fraction,
    carregar_codigos_preferenciais_unidades,
    carregar_destinos_fraction,
    carregar_regras_rotas_com_destino_e_triagem,
    executar_manifestos_romaneios,
    enviar_pdf_para_impressora_padrao,
    mapear_rotas_para_grupos,
    preparar_lote_impressao_romaneios,
)
from automacoes.gerar_romaneios_emissao import (
    COLUNAS_RESULTADO,
    baixar_etiquetas_drive,
    carregar_codigos_preferenciais_unidades_emissao,
    consultar_status_performance_ctes,
    montar_fila_emissao,
    preparar_estado_etiquetas_romaneio,
    recalcular_rotas_fila_emissao,
    registrar_etiquetas_romaneio,
    selecionar_txt_novos_do_zip,
)
from automacoes.dar_entrada_pedidos import (
    PADRAO_ETIQUETA,
)
from servicos.fila_romaneios_emissao import FilaRomaneiosEmissao
from utils.interface import (
    LARGURA_BOTAO,
    avisar_variaveis_faltantes,
    criar_painel_execucao,
    error,
    renderizar_logs,
    success,
    warning,
)
from utils.config import (
    ARQUIVO_FILA_ROMANEIOS_EMISSAO,
    ARQUIVO_ROTAS_ROMANEIOS,
    RESULTADOS_ROMANEIOS,
)
from utils.logs import criar_logger_execucao


CHAVE_RESULTADO = "visual_gerar_romaneios_emissao_resultado"
CHAVE_LOGS = "visual_gerar_romaneios_emissao_logs"
CHAVE_LOG_PAINEL_RENDERIZADO = "visual_gerar_romaneios_emissao_log_painel_renderizado"
CHAVE_MENSAGEM_SINCRONIZACAO = "visual_gerar_romaneios_emissao_sincronizacao"


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
    (
        rotas_por_uf,
        rotas_por_cidade,
        rotas_por_destino,
        triagens_por_uf,
        triagens_por_cidade,
        triagens_por_destino,
    ) = carregar_regras_rotas_com_destino_e_triagem(ARQUIVO_ROTAS_ROMANEIOS)
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
                "Destino": "",
                "Triagem": triagens_por_uf.get(uf, ""),
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
                "Destino": "",
                "Triagem": triagens_por_cidade.get(cidade, ""),
                "Código preferencial": codigos_preferenciais.get(chave_unidade, ""),
            }
        )

    for destino, unidade in rotas_por_destino.items():
        chave_unidade = _normalizar_rota(unidade)
        if chave_unidade not in relevantes:
            continue
        linhas.append(
            {
                "Unidade": unidade,
                "UF": "",
                "Cidade": "",
                "Destino": destino,
                "Triagem": triagens_por_destino.get(destino, ""),
                "Código preferencial": codigos_preferenciais.get(chave_unidade, ""),
            }
        )

    linhas.sort(
        key=lambda linha: (
            _normalizar_rota(linha["Unidade"]),
            linha["UF"],
            _normalizar_rota(linha["Cidade"]),
            _normalizar_rota(linha["Destino"]),
        )
    )
    return pd.DataFrame(
        linhas,
        columns=[
            "Unidade",
            "UF",
            "Cidade",
            "Destino",
            "Triagem",
            "Código preferencial",
        ],
    )


def _volume_divergente(linha: pd.Series) -> bool:
    volume_sistema = linha["Volumes Sistema"]
    if pd.isna(volume_sistema):
        return False
    return (
        int(volume_sistema) != int(linha["Volumes Etiquetas"])
        or bool(str(linha["Etiquetas faltantes"] or "").strip())
    )


def _montar_tabela_fila_persistida(
    fila: FilaRomaneiosEmissao,
) -> pd.DataFrame:
    etiquetas_abertas = fila.carregar_etiquetas_abertas()
    colunas = (
        ["Rua", "Palete de origem"]
        + COLUNAS_RESULTADO[:-2]
        + ["UF", "Cidade"]
        + COLUNAS_RESULTADO[-2:]
        + ["CTEs por etiqueta", "Triagem", "Rota", "Origem", "Status"]
    )
    if etiquetas_abertas.empty:
        return pd.DataFrame(columns=colunas)

    linhas = []
    colunas_grupo = ["rua", "triagem", "rota", "shipment_id"]
    for _, grupo in etiquetas_abertas.groupby(
        colunas_grupo,
        sort=False,
        dropna=False,
    ):
        def primeira_preenchida(coluna: str):
            for valor in grupo[coluna].tolist():
                if pd.notna(valor) and str(valor).strip():
                    return valor
            return ""

        etiquetas = sorted(
            grupo["etiqueta"].astype(str).tolist(),
            key=lambda etiqueta: (
                int(PADRAO_ETIQUETA.fullmatch(etiqueta).group(2))
                if PADRAO_ETIQUETA.fullmatch(etiqueta)
                else 0
            ),
        )
        cte_por_etiqueta = {
            str(linha["etiqueta"]): str(linha["cte"] or "").strip()
            for _, linha in grupo.iterrows()
        }
        ctes_por_etiqueta = [cte_por_etiqueta.get(etiqueta, "") for etiqueta in etiquetas]
        ctes_do_pedido = list(dict.fromkeys(cte for cte in ctes_por_etiqueta if cte))
        volume_sistema = None
        shipment_id = str(primeira_preenchida("shipment_id"))
        cte = ctes_do_pedido[0] if ctes_do_pedido else ""
        linhas.append(
            {
                "Rua": str(primeira_preenchida("rua") or ""),
                "Palete de origem": "; ".join(
                    dict.fromkeys(
                        valor.strip()
                        for valor in grupo["palete_origem"].astype(str)
                        if valor.strip()
                    )
                ),
                "CTE": cte,
                "ShipmentId": shipment_id,
                "Etiquetas do pedido": "\n".join(etiquetas),
                "Volumes Sistema": volume_sistema,
                "Volumes Etiquetas": len(etiquetas),
                "Etiquetas faltantes": "",
                "CTEs para dar entrada": "\n".join(ctes_do_pedido),
                "CTEs por etiqueta": "\n".join(ctes_por_etiqueta),
                "UF": str(primeira_preenchida("uf") or ""),
                "Cidade": str(primeira_preenchida("cidade") or ""),
                "Destino": str(primeira_preenchida("destino") or ""),
                "Manifesto": "",
                "Triagem": str(primeira_preenchida("triagem") or ""),
                "Rota": str(primeira_preenchida("rota") or ""),
                "Origem": str(primeira_preenchida("origem") or ""),
                "Status": str(primeira_preenchida("status") or ""),
            }
        )

    return pd.DataFrame(linhas, columns=colunas)


def _renderizar_sincronizacao_drive() -> None:
    faltantes = avisar_variaveis_faltantes("gerar_romaneios")
    faltantes_drive = avisar_variaveis_faltantes("importar_etiquetas_drive")
    st.caption(
        "Sincronize os TXT do Drive. O Fraction associa cada etiqueta à Remessa/CTE; "
        "em seguida, a Performance consulta os CTEs em lotes de até 1.000 para obter "
        "Destino e Status. O volume do sistema permanece em branco nesta etapa."
    )
    sincronizar = st.button(
        "Sincronizar Drive",
        type="primary",
        key="gerar_romaneios_emissao_sincronizar",
        width=LARGURA_BOTAO,
        disabled=bool(faltantes or faltantes_drive),
    )
    if not sincronizar:
        return

    st.session_state[CHAVE_LOGS] = []
    _, log_visual = criar_painel_execucao(
        CHAVE_LOGS,
        limpar=False,
        max_linhas=None,
        expandido=True,
    )
    st.session_state[CHAVE_LOG_PAINEL_RENDERIZADO] = True
    log, _ = criar_logger_execucao(
        "gerar_romaneios",
        log_visual,
        rotulo="processar_emissao",
    )
    try:
        st.session_state.pop(CHAVE_MENSAGEM_SINCRONIZACAO, None)
        caminho_estado = preparar_estado_etiquetas_romaneio()
        fila = FilaRomaneiosEmissao(ARQUIVO_FILA_ROMANEIOS_EMISSAO)
        log(f"Arquivo de etiquetas já processadas: {caminho_estado}.")
        log(f"Banco local da fila incremental: {ARQUIVO_FILA_ROMANEIOS_EMISSAO}.")
        with tempfile.TemporaryDirectory(prefix="botevelog_emissao_") as diretorio_temporario:
            pasta_temporaria = Path(diretorio_temporario)
            with st.spinner("Baixando os TXT novos do Drive..."):
                caminho_zip = baixar_etiquetas_drive(
                    pasta_destino=pasta_temporaria,
                    arquivos_conhecidos=fila.nomes_arquivos_importados(),
                    log=log,
                )
            if caminho_zip is None:
                st.session_state[CHAVE_MENSAGEM_SINCRONIZACAO] = (
                    "Drive sincronizado: nenhum TXT novo elegível foi baixado. "
                    "Só são importados arquivos com 'Transferência' no nome; "
                    "as filas abertas foram mantidas."
                )
                success(
                    "Não há arquivos novos elegíveis; as filas abertas "
                    "continuam disponíveis."
                )
                return

            conteudo_zip = caminho_zip.read_bytes()
            conteudo_etiquetas, arquivos_novos = selecionar_txt_novos_do_zip(
                conteudo_zip,
                fila.arquivos_importados(),
                log=log,
            )
            if not arquivos_novos:
                st.session_state[CHAVE_MENSAGEM_SINCRONIZACAO] = (
                    "Drive sincronizado: nenhum TXT novo elegível foi encontrado. "
                    "Só são importados arquivos com 'Transferência' no nome; "
                    "as filas abertas foram mantidas."
                )
                success(
                    "Não há arquivos novos elegíveis; as filas abertas "
                    "continuam disponíveis."
                )
                return

            with st.spinner(
                "Consultando etiquetas no Fraction e montando a fila pela Performance..."
            ):
                dados = montar_fila_emissao(
                    conteudo_etiquetas,
                    nome_arquivo_etiquetas="txt_novos_drive.zip",
                    pasta_temporaria=pasta_temporaria,
                    log=log,
                )
                contagem = fila.importar_fila(
                    dados["tabela"],
                    arquivos_novos,
                    log=log,
                )

            st.session_state[CHAVE_MENSAGEM_SINCRONIZACAO] = (
                f"Sincronização concluída: {contagem['novas']} etiqueta(s) nova(s) "
                f"adicionada(s); {contagem['repetidas']} já estavam na fila ou no histórico."
            )
            etiquetas_sem_cte = dados.get("etiquetas_sem_retorno_fraction", [])
            if etiquetas_sem_cte:
                amostra_etiquetas = ", ".join(etiquetas_sem_cte[:5])
                mais_etiquetas = "…" if len(etiquetas_sem_cte) > 5 else ""
                warning(
                    f"{len(etiquetas_sem_cte)} etiqueta(s) não retornaram "
                    "Remessa/CTE no Fraction e foram ignoradas. As demais "
                    f"foram processadas. Amostra: {amostra_etiquetas}{mais_etiquetas}"
                )
            if dados["shipment_ids_sem_retorno_fraction"]:
                warning(
                    f"{len(dados['shipment_ids_sem_retorno_fraction'])} pedido(s) "
                    "não retornaram dados de Performance; permanecerão fora da fila."
                )
            if dados["shipment_ids_sem_destino_performance"]:
                warning(
                    f"{len(dados['shipment_ids_sem_destino_performance'])} pedido(s) "
                    "ficaram sem Destino na Performance; confira a rota antes de gerar."
                )
            if dados["shipment_ids_sem_status_performance"]:
                warning(
                    f"{len(dados['shipment_ids_sem_status_performance'])} pedido(s) "
                    "não têm Status EMISSAO e foram mantidos fora da fila."
                )
            if not dados["tabela"].empty:
                success(
                    f"Fila atualizada: {contagem['novas']} etiqueta(s) nova(s) "
                    "adicionada(s)."
                )
            else:
                warning(
                    "Os TXT novos foram processados, mas não havia etiquetas elegíveis "
                    "para entrar na fila."
                )
    except CredenciaisFractionInvalidasError as erro_credenciais:
        warning(str(erro_credenciais))
    except Exception as erro:
        log(f"Falha ao processar a fila: {type(erro).__name__}: {erro}")
        error(f"Não foi possível processar a fila: {erro}")


def renderizar_gerar_romaneios_emissao() -> None:
    st.subheader("Fila de romaneios de Emissão")
    st.caption(
        "As filas são atualizadas pelos TXT novos do Drive e armazenadas por rua e "
        "triagem. Cada etiqueta é registrada uma única vez; filas concluídas permanecem "
        "no histórico."
    )

    _renderizar_sincronizacao_drive()
    fila = FilaRomaneiosEmissao(ARQUIVO_FILA_ROMANEIOS_EMISSAO)
    tabela = _montar_tabela_fila_persistida(fila)
    if not tabela.empty:
        try:
            tabela_recalculada = recalcular_rotas_fila_emissao(tabela)
            etiquetas_atualizadas = fila.atualizar_rotas_abertas(tabela_recalculada)
            if etiquetas_atualizadas:
                tabela = _montar_tabela_fila_persistida(fila)
                st.caption(
                    f"Rotas atualizadas pela configuração atual para "
                    f"{etiquetas_atualizadas} etiqueta(s) da fila aberta."
                )
        except (FileNotFoundError, OSError, ValueError) as erro:
            warning(
                "Não foi possível recalcular as rotas das filas abertas; "
                f"os dados salvos foram mantidos. Detalhe: {erro}"
            )

    etiquetas_abertas_db = fila.carregar_etiquetas_abertas()
    ctes_abertos = list(dict.fromkeys(
        str(cte).strip()
        for cte in etiquetas_abertas_db.get("cte", pd.Series(dtype=str)).tolist()
        if pd.notna(cte) and str(cte).strip()
    ))
    faltantes_config_status = (
        avisar_variaveis_faltantes("baixar_relatorios_performance")
        if ctes_abertos
        else []
    )
    st.caption(
        "O status é consultado novamente na Performance do Fraction para os CTEs "
        "que ainda estão abertos."
    )
    atualizar_status = st.button(
        "Atualizar status no Fraction",
        key="gerar_romaneios_emissao_atualizar_status",
        width=LARGURA_BOTAO,
        disabled=not ctes_abertos or bool(faltantes_config_status),
    )
    if atualizar_status:
        st.session_state[CHAVE_LOGS] = []
        log, caminho_log = criar_logger_execucao(
            "gerar_romaneios",
            st.session_state[CHAVE_LOGS].append,
            rotulo="atualizar_status_emissao",
        )
        log(f"Arquivo de log: {caminho_log}.")
        try:
            with st.spinner(
                f"Atualizando o status de {len(ctes_abertos)} CTE(s) no Fraction..."
            ):
                with tempfile.TemporaryDirectory(
                    prefix="botevelog_status_emissao_"
                ) as diretorio_temporario:
                    status_por_cte = consultar_status_performance_ctes(
                        ctes_abertos,
                        log=log,
                        pasta_temporaria=Path(diretorio_temporario),
                    )
            ctes_sem_status = [
                cte for cte in ctes_abertos if cte not in status_por_cte
            ]
            etiquetas_atualizadas = fila.atualizar_status_abertas(status_por_cte)
            log(
                f"Status atualizado para {etiquetas_atualizadas} etiqueta(s) "
                f"aberta(s), em {len(status_por_cte)} CTE(s)."
            )
            if ctes_sem_status:
                log(
                    f"A Performance não retornou Status para {len(ctes_sem_status)} "
                    "CTE(s); os status anteriores foram mantidos."
                )
                warning(
                    f"A Performance não retornou Status para {len(ctes_sem_status)} "
                    "CTE(s); os valores anteriores foram mantidos."
                )
            else:
                success(
                    f"Status consultado para {len(status_por_cte)} CTE(s); "
                    f"{etiquetas_atualizadas} etiqueta(s) aberta(s) tiveram alteração."
                )
            tabela = _montar_tabela_fila_persistida(fila)
        except Exception as erro:
            log(f"Falha ao atualizar status: {type(erro).__name__}: {erro}")
            error(f"Não foi possível atualizar os status no Fraction: {erro}")

    etiquetas_ativas = "\n".join(
        tabela.get("Etiquetas do pedido", pd.Series(dtype=str)).astype(str)
    )
    identificador = sha256(etiquetas_ativas.encode("utf-8")).hexdigest()
    resultado_atual = st.session_state.get(CHAVE_RESULTADO)
    if not isinstance(resultado_atual, dict):
        resultado_atual = {}
        st.session_state[CHAVE_RESULTADO] = resultado_atual
    mensagem_sincronizacao = st.session_state.get(CHAVE_MENSAGEM_SINCRONIZACAO)
    if mensagem_sincronizacao:
        st.info(mensagem_sincronizacao)
    if resultado_atual.get("resumo_envio"):
        success(resultado_atual["resumo_envio"])

    configuracao_colunas_fila = {
        "Rua": st.column_config.TextColumn("Rua"),
        "Palete de origem": st.column_config.TextColumn("Palete de origem"),
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
        "Triagem": st.column_config.TextColumn("Triagem", width="large"),
        "Manifesto": st.column_config.TextColumn("Manifesto"),
        "Rota": st.column_config.TextColumn("Rota"),
        "Origem": st.column_config.TextColumn("Origem"),
        "Status": st.column_config.TextColumn("Status"),
    }
    resumo_abertos = (
        tabela.groupby(["Rua", "Triagem", "Rota"], dropna=False)
        .agg(
            Pedidos=("ShipmentId", "nunique"),
            Etiquetas=("Volumes Etiquetas", "sum"),
        )
        .reset_index()
        if not tabela.empty
        else pd.DataFrame(columns=["Rua", "Triagem", "Rota", "Pedidos", "Etiquetas"])
    )
    st.markdown("#### Dashboard de filas abertas")
    if resumo_abertos.empty:
        st.info("Ainda não há etiquetas pendentes. Sincronize o Drive após enviar um TXT.")
    else:
        st.dataframe(resumo_abertos, hide_index=True, width="stretch")
    with st.expander("Detalhes dos pedidos em filas abertas", expanded=False):
        if tabela.empty:
            st.info("Sem pedidos pendentes.")
        else:
            st.dataframe(
                tabela.drop(columns=["CTEs por etiqueta"], errors="ignore"),
                hide_index=True,
                width="stretch",
                column_config=configuracao_colunas_fila,
            )

    historico = fila.carregar_historico()
    with st.expander("Romaneios concluídos", expanded=False):
        if historico.empty:
            st.info("Nenhum romaneio foi concluído por esta fila ainda.")
        else:
            st.dataframe(historico, hide_index=True, width="stretch")

    divergencias = tabela[tabela.apply(_volume_divergente, axis=1)]
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
                rua = str(linha.get("Rua", "Sem rua"))
                triagem = str(linha.get("Triagem", "Sem triagem"))
                palete = str(linha.get("Palete de origem", "Sem palete"))
                etiquetas_token = str(linha.get("Etiquetas do pedido", ""))
                token_confirmacao = (
                    f"{rua}\0{palete}\0{shipment_id}\0{etiquetas_token}"
                )
                chave_checkbox = sha256(token_confirmacao.encode("utf-8")).hexdigest()[:16]
                etiquetas_faltantes = str(linha["Etiquetas faltantes"] or "").strip()
                quantidade_faltantes = (
                    len(etiquetas_faltantes.splitlines()) if etiquetas_faltantes else 0
                )
                autorizar = st.checkbox(
                    f"Autorizar {rua} / {triagem} — ShipmentId {shipment_id}: "
                    f"Volume na Performance "
                    f"{int(linha['Volumes Sistema'])}; etiquetas bipadas "
                    f"{int(linha['Volumes Etiquetas'])}; "
                    f"{quantidade_faltantes} etiqueta(s) faltante(s).",
                    key=(
                        f"gerar_romaneios_emissao_confirmar_{identificador[:16]}_"
                        f"{chave_checkbox}"
                    ),
                )
                if autorizar:
                    selecionados.append(token_confirmacao)

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

    tabela_envio = tabela.copy()
    confirmacoes = set(resultado_atual.get("confirmacoes", []))
    for indice, linha in tabela_envio.iterrows():
        token_confirmacao = (
            f"{linha.get('Rua', 'Sem rua')}\0"
            f"{linha.get('Palete de origem', 'Sem palete')}\0"
            f"{linha['ShipmentId']}\0"
            f"{linha.get('Etiquetas do pedido', '')}"
        )
        if _volume_divergente(linha) and token_confirmacao not in confirmacoes:
            tabela_envio.at[indice, "CTEs para dar entrada"] = ""

    status_emissao = tabela_envio["Status"].fillna("").map(
        lambda valor: _normalizar_rota(valor) == "EMISSAO"
    )
    pedidos_fora_emissao = tabela_envio.loc[~status_emissao]
    if not pedidos_fora_emissao.empty:
        warning(
            f"{len(pedidos_fora_emissao)} pedido(s) com status diferente de "
            "EMISSAO estão bloqueados para geração de romaneio."
        )

    tem_ctes_envio = tabela_envio["CTEs para dar entrada"].fillna("").map(
        lambda valor: any(
            linha.strip() and linha.strip().lower() != "nan"
            for linha in str(valor).splitlines()
        )
    )
    tem_ctes_envio &= status_emissao
    rota_original = tabela["Rota"].fillna("").astype(str).str.strip()
    sem_rota_ou_ambigua = tabela.loc[
        rota_original.isin({"", "Sem rota", "Rota ambígua"})
    ].copy()
    grupos, grupos_ignorados = mapear_rotas_para_grupos(
        tabela_envio.loc[status_emissao],
        separar_por_rua_triagem=True,
    )
    codigos_preferenciais = carregar_codigos_preferenciais_unidades_emissao()
    grupos_validos, destinos_pendentes = associar_destinos_fraction(
        grupos,
        carregar_destinos_fraction(),
        codigos_preferenciais=codigos_preferenciais,
    )

    chaves_grupos_prontos = {
        (
            _normalizar_rota(grupo.get("RUA", "")),
            _normalizar_rota(grupo.get("TRIAGEM", "")),
            _normalizar_rota(grupo["ROTA"]),
        )
        for grupo in grupos_validos
    }
    rotas_prontas = {grupo["ROTA"] for grupo in grupos_validos}

    def linha_em_grupo_pronto(linha: pd.Series) -> bool:
        chave = (
            _normalizar_rota(linha.get("Rua", "")),
            _normalizar_rota(linha.get("Triagem", "")),
            _normalizar_rota(linha.get("Rota", "")),
        )
        return chave in chaves_grupos_prontos

    prontos_para_envio = tabela_envio.loc[
        tem_ctes_envio
        & tabela_envio.apply(linha_em_grupo_pronto, axis=1)
    ].copy()
    st.markdown("#### Prontos para envio")
    if prontos_para_envio.empty:
        st.info("Nenhum pedido está pronto para envio.")
    else:
        st.dataframe(
            prontos_para_envio.drop(columns=["CTEs por etiqueta"], errors="ignore"),
            hide_index=True,
            width="stretch",
            column_config=configuracao_colunas_fila,
        )

    st.markdown("#### Sem rota ou com mais de uma rota")
    if sem_rota_ou_ambigua.empty:
        st.info("Não há pedidos sem rota ou com rota ambígua.")
    else:
        st.dataframe(
            sem_rota_ou_ambigua.drop(columns=["CTEs por etiqueta"], errors="ignore"),
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
            f"{len(destinos_pendentes)} grupo(s) não têm unidade única no catálogo "
            "do Fraction e ficarão fora do envio."
        )
    if grupos_ignorados:
        warning(
            f"{len(grupos_ignorados)} CTE(s) não entraram em romaneios porque "
            "faltou unidade, Rua, triagem ou CTE válido."
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
                    "Destino": st.column_config.TextColumn("Destino"),
                    "Triagem": st.column_config.TextColumn("Triagem", width="large"),
                    "Código preferencial": st.column_config.TextColumn(
                        "Código preferencial"
                    ),
                },
            )
    quantidade_ctes = sum(len(grupo["LISTA_CTES"]) for grupo in grupos_validos)
    st.caption(
        f"Disponíveis: {quantidade_ctes} CTE(s) em "
        f"{len(grupos_validos)} grupo(s) com unidade Fraction resolvida."
    )

    selecao_grupo = None
    tabela_grupo = tabela_envio.iloc[0:0].copy()
    if grupos_validos:
        grupo_por_opcao: dict[str, dict] = {}
        for grupo in grupos_validos:
            chave_grupo = (
                str(grupo.get("RUA", "")),
                str(grupo.get("TRIAGEM", "")),
                str(grupo.get("ROTA", "")),
            )
            ctes_grupo = len(grupo["LISTA_CTES"])
            opcao = (
                f"{chave_grupo[0]} · {chave_grupo[1]} · {chave_grupo[2]} "
                f"({ctes_grupo} CTE(s))"
            )
            grupo_por_opcao[opcao] = grupo
        opcao_selecionada = st.selectbox(
            "Fila que será fechada e enviada ao Fraction",
            list(grupo_por_opcao),
            key="gerar_romaneios_emissao_grupo_selecionado",
        )
        selecao_grupo = grupo_por_opcao[opcao_selecionada]
        chave_selecionada = (
            str(selecao_grupo.get("RUA", "")),
            str(selecao_grupo.get("TRIAGEM", "")),
            str(selecao_grupo.get("ROTA", "")),
        )
        tabela_grupo = tabela_envio.loc[
            tabela_envio.apply(
                lambda linha: (
                    str(linha.get("Rua", "")),
                    str(linha.get("Triagem", "")),
                    str(linha.get("Rota", "")),
                ) == chave_selecionada,
                axis=1,
            )
        ].copy()
        st.caption(
            f"Selecionado: {chave_selecionada[0]} / {chave_selecionada[1]} / "
            f"{chave_selecionada[2]} — {len(selecao_grupo['LISTA_CTES'])} CTE(s). "
            "As outras filas permanecem abertas."
        )

    faltantes = avisar_variaveis_faltantes("gerar_romaneios")
    enviar = st.button(
        "Gerar romaneio desta rua / triagem",
        type="primary",
        key="gerar_romaneios_emissao_enviar",
        width=LARGURA_BOTAO,
        disabled=(selecao_grupo is None or bool(faltantes)),
    )

    if enviar and selecao_grupo is not None:
        log, _ = criar_logger_execucao(
            "gerar_romaneios",
            st.session_state[CHAVE_LOGS].append,
            rotulo="processar_emissao",
        )
        try:
            rua_selecionada = str(selecao_grupo.get("RUA") or "")
            triagem_selecionada = str(selecao_grupo.get("TRIAGEM") or "")
            rota_selecionada = str(selecao_grupo.get("ROTA") or "")
            ctes_selecionados = {
                str(cte).strip()
                for cte in selecao_grupo.get("LISTA_CTES", [])
                if str(cte).strip()
            }
            tabela_grupo = tabela_grupo.loc[
                tabela_grupo["CTEs para dar entrada"].map(
                    lambda valor: bool(
                        ctes_selecionados.intersection(
                            item.strip()
                            for item in str(valor).splitlines()
                            if item.strip()
                        )
                    )
                )
            ].copy()
            log(
                f"Funcionário selecionou {rua_selecionada} / "
                f"{triagem_selecionada} / {rota_selecionada}; "
                f"{len(ctes_selecionados)} CTE(s) serão enviados."
            )

            def registrar_grupo_concluido(grupo: dict, caminho_pdf: Path) -> None:
                etiquetas_grupo = [
                    etiqueta.strip()
                    for valor in tabela_grupo["Etiquetas do pedido"]
                    for etiqueta in str(valor).splitlines()
                    if etiqueta.strip()
                ]
                etiquetas_grupo = list(dict.fromkeys(etiquetas_grupo))
                if not etiquetas_grupo:
                    raise RuntimeError(
                        "O PDF foi salvo, mas não foi possível associar etiquetas "
                        "à fila selecionada. A fila não será fechada."
                    )
                quantidade_fechada = fila.fechar_romaneio(
                    rua=rua_selecionada,
                    triagem=triagem_selecionada,
                    rota=rota_selecionada,
                    etiquetas=etiquetas_grupo,
                    caminho_pdf=caminho_pdf,
                )
                registrar_etiquetas_romaneio(
                    etiquetas_grupo,
                    contexto=(
                        f"romaneio gerado — {rua_selecionada} / "
                        f"{triagem_selecionada}, rota '{rota_selecionada}', "
                        f"PDF '{caminho_pdf}'"
                    ),
                    log=log,
                )
                log(
                    f"Fila fechada no SQLite: {quantidade_fechada} etiqueta(s) "
                    f"em {rua_selecionada} / {triagem_selecionada}."
                )

            pasta_execucao_pdf = RESULTADOS_ROMANEIOS / (
                f"Execucao_Emissao_{datetime.now():%Y-%m-%d_%H-%M-%S-%f}"
            )
            with st.spinner(
                f"Gerando romaneio de {rua_selecionada} / {triagem_selecionada}..."
            ):
                processamento = executar_manifestos_romaneios(
                    f"Fila {rua_selecionada} / {triagem_selecionada}",
                    tabela_previa=tabela_grupo,
                    codigos_preferenciais=codigos_preferenciais,
                    separar_por_rua_triagem=True,
                    pasta_destino_pdf=pasta_execucao_pdf,
                    ao_concluir_grupo=registrar_grupo_concluido,
                    log=log,
                )
            mensagem = (
                f"Romaneio concluído para {rua_selecionada} / {triagem_selecionada}: "
                f"{processamento['ctes_manifesto']} CTE(s), "
                f"{len(processamento['arquivos_pdf'])} PDF(s). "
                f"A fila foi encerrada e as próximas etiquetas abrirão um novo lote."
            )
            resultado_atual.update(
                {
                    "enviado": True,
                    "resumo_envio": mensagem,
                    "arquivos_pdf": list(processamento["arquivos_pdf"]),
                    "pasta_pdf": processamento["pasta_pdf"],
                    "arquivo_lote_impressao": None,
                    "impressao_solicitada": False,
                }
            )
            st.session_state[CHAVE_RESULTADO] = resultado_atual
            log(mensagem)
            st.rerun()
        except CredenciaisFractionInvalidasError as erro_credenciais:
            warning(str(erro_credenciais))
        except Exception as erro_execucao:
            log(f"Erro: {type(erro_execucao).__name__}: {erro_execucao}")
            error(f"A geração do romaneio foi interrompida: {erro_execucao}")

    arquivos_pdf = resultado_atual.get("arquivos_pdf", [])
    if resultado_atual.get("enviado") and arquivos_pdf:
        st.markdown("#### Impressão dos romaneios")
        st.caption(
            f"{len(arquivos_pdf)} PDF(s) já foram baixados e salvos. A impressão "
            "envia todas as páginas em um único lote para a impressora padrão."
        )
        if resultado_atual.get("pasta_pdf"):
            st.caption(f"Pasta desta execução: {resultado_atual['pasta_pdf']}")
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

    if not st.session_state.pop(CHAVE_LOG_PAINEL_RENDERIZADO, False):
        renderizar_logs(
            st.session_state.get(CHAVE_LOGS, []),
            max_linhas=None,
            expandido=True,
        )
