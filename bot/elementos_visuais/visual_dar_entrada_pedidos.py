from __future__ import annotations

from hashlib import sha256
from io import BytesIO

import pandas as pd
import streamlit as st

from automacoes.dar_entrada_pedidos import (
    AdicaoRecebimentoIndeterminadaError,
    adicionar_ctes_ao_recebimento_fraction,
    agrupar_etiquetas_importadas,
    baixar_bases_por_romaneios,
    completar_pedidos_fora_romaneios,
    coletar_ctes_liberados_para_entrada,
    consultar_shipments_no_fraction,
    montar_base_pedidos,
    normalizar_numeros_romaneio,
    salvar_base_entrada_pedidos,
)
from servicos.fraction import CredenciaisFractionInvalidasError
from utils.interface import (
    LARGURA_BOTAO,
    avisar_variaveis_faltantes,
    criar_painel_execucao,
    error,
    success,
    warning,
)
from utils.logs import criar_logger_execucao


CHAVE_RESULTADO = "visual_dar_entrada_pedidos_resultado"
CHAVE_LOGS = "visual_dar_entrada_pedidos_logs"
CHAVE_CONFIRMACOES_DIVERGENCIAS = "visual_dar_entrada_pedidos_confirmacoes"
CHAVE_ADICAO_RECEBIMENTO = "visual_dar_entrada_pedidos_processados_v2"


def renderizar_dar_entrada_pedidos() -> None:
    st.subheader("Dar entrada em pedidos")

    faltantes = avisar_variaveis_faltantes("dar_entrada_pedidos")
    numeros_romaneio_texto = st.text_area(
        "Números dos romaneios",
        placeholder="Informe um ou mais números, um por linha ou separados por vírgula",
        height=110,
        key="dar_entrada_pedidos_numeros_romaneio",
    )
    numeros_romaneio: list[str] = []
    erro_romaneios = None
    if numeros_romaneio_texto.strip():
        try:
            numeros_romaneio = normalizar_numeros_romaneio(numeros_romaneio_texto)
            st.caption(f"{len(numeros_romaneio)} romaneio(s) válido(s).")
        except ValueError as erro:
            erro_romaneios = str(erro)
            warning(erro_romaneios)

    arquivo_etiquetas = st.file_uploader(
        "Etiquetas bipadas (TXT ou ZIP de TXT)",
        type=["txt", "zip"],
        key="dar_entrada_pedidos_txt",
        help=(
            "Aceita TXT simples ou estruturado do Coletor Evelog. O ZIP pode conter "
            "vários TXT; o programa extrai e reúne somente as etiquetas de todos eles."
        ),
        width="stretch",
    )

    arquivo_pronto = arquivo_etiquetas is not None
    if not arquivo_pronto:
        st.info("Selecione um TXT ou ZIP com as etiquetas bipadas.")
    conteudo_etiquetas = arquivo_etiquetas.getvalue() if arquivo_etiquetas else b""
    identificador_arquivos = None
    if arquivo_pronto and numeros_romaneio and not erro_romaneios:
        identificador_arquivos = sha256(
            "\n".join(numeros_romaneio).encode("utf-8")
            + b"\0"
            + arquivo_etiquetas.name.encode("utf-8")
            + b"\0"
            + conteudo_etiquetas
        ).hexdigest()

    iniciar = st.button(
        "Montar base para dar entrada",
        type="primary",
        key="dar_entrada_pedidos_montar",
        width=LARGURA_BOTAO,
        disabled=(
            bool(faltantes)
            or not numeros_romaneio
            or bool(erro_romaneios)
            or not arquivo_pronto
        ),
    )
    if iniciar:
        st.session_state.pop(CHAVE_RESULTADO, None)
        st.session_state.pop(CHAVE_CONFIRMACOES_DIVERGENCIAS, None)
        _, log_visual = criar_painel_execucao(CHAVE_LOGS)
        log, _ = criar_logger_execucao(
            "dar_entrada_pedidos",
            log_visual,
            rotulo="dar_entrada_pedidos",
        )
        try:
            log(
                f"Iniciando consulta para {len(numeros_romaneio)} romaneio(s): "
                + ", ".join(numeros_romaneio)
            )
            pedidos = agrupar_etiquetas_importadas(
                conteudo_etiquetas,
                arquivo_etiquetas.name,
                log=log,
            )
            total_etiquetas = sum(
                int(pedido["VOLUMES_ETIQUETAS"]) for pedido in pedidos
            )
            log(
                f"Etiquetas reunidas: {total_etiquetas} etiqueta(s) em "
                f"{len(pedidos)} ShipmentId(s)."
            )
            with st.spinner(
                "Consultando o Fraction e montando a conferência..."
            ):
                bases = baixar_bases_por_romaneios(
                    numeros_romaneio,
                    log=log,
                )
                resultado = montar_base_pedidos(
                    bases["unitizado"],
                    bases["inconsistencia"],
                    pedidos,
                    log=log,
                )
                pedidos_fora_romaneios = resultado["pedidos_fora_romaneios"]
                if not pedidos_fora_romaneios.empty:
                    shipment_ids_fora = pedidos_fora_romaneios[
                        "ShipmentId"
                    ].astype(str).tolist()
                    log(
                        f"Iniciando consulta individual no Fraction para "
                        f"{len(shipment_ids_fora)} ShipmentId(s) fora dos romaneios."
                    )
                    consultas_fraction = consultar_shipments_no_fraction(
                        shipment_ids_fora,
                        log=log,
                    )
                    resultado["pedidos_fora_romaneios"] = (
                        completar_pedidos_fora_romaneios(
                            pedidos_fora_romaneios,
                            consultas_fraction,
                            log=log,
                        )
                    )
            st.session_state[CHAVE_RESULTADO] = {
                "arquivos": identificador_arquivos,
                "romaneios": numeros_romaneio,
                **resultado,
            }
            success(
                f"Consulta montada: {len(resultado['tabela'])} CTE(s) "
                f"e {resultado['divergencias']} divergência(s) de volume."
            )
        except CredenciaisFractionInvalidasError as erro_credenciais:
            warning(str(erro_credenciais))
            return
        except (UnicodeDecodeError, ValueError) as erro_dados:
            log(f"Erro nos arquivos: {type(erro_dados).__name__}: {erro_dados}")
            error(f"Não foi possível montar a consulta: {erro_dados}")
            return
        except Exception as erro_execucao:
            log(f"Erro: {type(erro_execucao).__name__}: {erro_execucao}")
            error(f"A consulta por etiqueta foi interrompida: {erro_execucao}")
            return

    resultado_atual = st.session_state.get(CHAVE_RESULTADO)
    if (
        not resultado_atual
        or not identificador_arquivos
        or resultado_atual.get("arquivos") != identificador_arquivos
    ):
        return

    tabela: pd.DataFrame = resultado_atual["tabela"]
    st.markdown("#### Conferência dos pedidos e volumes")
    st.dataframe(
        tabela,
        hide_index=True,
        width="stretch",
        column_config={
            "CTE": st.column_config.TextColumn("CTE"),
            "Manifesto": st.column_config.TextColumn("Manifesto"),
            "Destino": st.column_config.TextColumn("Destino"),
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
        },
    )

    pedidos_fora_romaneios: pd.DataFrame = resultado_atual.get(
        "pedidos_fora_romaneios",
        pd.DataFrame(
            columns=[
                "CTE",
                "ShipmentId",
                "Etiquetas do pedido",
                "Volumes Fraction",
                "Volumes Etiquetas",
                "Etiquetas faltantes",
                "CTEs para dar entrada",
                "Situação",
            ]
        ),
    )
    divergencias_df = tabela[
        tabela["Volumes Sistema"].notna()
        & (
            (tabela["Volumes Sistema"] != tabela["Volumes Etiquetas"])
            | tabela["Etiquetas faltantes"].fillna("").astype(str).str.strip().ne("")
        )
    ]
    sem_volume = tabela[tabela["Volumes Sistema"].isna()]
    divergencias_fora_df = pedidos_fora_romaneios[
        (pedidos_fora_romaneios["Situação"] == "Fora dos romaneios consultados")
        & pedidos_fora_romaneios["Volumes Fraction"].notna()
        & (
            (pedidos_fora_romaneios["Volumes Fraction"] != pedidos_fora_romaneios["Volumes Etiquetas"])
            | pedidos_fora_romaneios["Etiquetas faltantes"]
            .fillna("")
            .astype(str)
            .str.strip()
            .ne("")
        )
    ]
    sem_volume_fora = pedidos_fora_romaneios[
        (pedidos_fora_romaneios["Situação"] == "Fora dos romaneios consultados")
        & pedidos_fora_romaneios["Volumes Fraction"].isna()
    ]
    total_divergencias = len(divergencias_df) + len(divergencias_fora_df)
    if total_divergencias:
        warning("Inconsistência de volumes.")
        st.caption(
            "A conferência considera somente as etiquetas bipadas para formar os CTEs. "
            "Etiquetas faltantes ficam fora desta entrada."
        )
    total_sem_volume = len(sem_volume) + len(sem_volume_fora)
    if total_sem_volume:
        warning(
            f"{total_sem_volume} CTE(s) sem volume confirmado no Fraction; "
            "eles não serão liberados para entrada."
        )

    confirmacao_atual = st.session_state.get(
        CHAVE_CONFIRMACOES_DIVERGENCIAS, {}
    )
    confirmacao_registrada = confirmacao_atual.get("arquivos") == identificador_arquivos
    ctes_confirmados = (
        set(confirmacao_atual.get("ctes", [])) if confirmacao_registrada else set()
    )

    if total_divergencias:
        with st.form(key=f"dar_entrada_pedidos_confirmar_{identificador_arquivos[:16]}"):
            selecionados_no_formulario: list[str] = []
            for _, linha in divergencias_df.iterrows():
                cte = str(linha["CTE"])
                etiquetas_faltantes = str(linha["Etiquetas faltantes"]).strip()
                quantidade_faltantes = (
                    len(etiquetas_faltantes.splitlines()) if etiquetas_faltantes else 0
                )
                autorizar = st.checkbox(
                    f"Autorizar CTE {cte}: total de volumes: "
                    f"{int(linha['Volumes Sistema'])}; bipados: "
                    f"{int(linha['Volumes Etiquetas'])}; "
                    f"{quantidade_faltantes} etiqueta(s) faltante(s).",
                    key=f"dar_entrada_pedidos_autorizar_{identificador_arquivos[:16]}_{cte}",
                )
                if autorizar:
                    selecionados_no_formulario.append(cte)

            for _, linha in divergencias_fora_df.iterrows():
                cte = str(linha["CTE"])
                shipment_id = str(linha["ShipmentId"])
                etiquetas_faltantes = str(linha["Etiquetas faltantes"]).strip()
                quantidade_faltantes = (
                    len(etiquetas_faltantes.splitlines()) if etiquetas_faltantes else 0
                )
                token_confirmacao = f"SHIPMENT:{shipment_id}"
                autorizar = st.checkbox(
                    f"Autorizar CTE {cte} (ShipmentId {shipment_id}): "
                    f"total de volumes: {int(linha['Volumes Fraction'])}; "
                    f"bipados: {int(linha['Volumes Etiquetas'])}; "
                    f"{quantidade_faltantes} etiqueta(s) faltante(s).",
                    key=(
                        f"dar_entrada_pedidos_autorizar_{identificador_arquivos[:16]}_"
                        f"fora_{shipment_id}"
                    ),
                )
                if autorizar:
                    selecionados_no_formulario.append(token_confirmacao)

            confirmar_selecao = st.form_submit_button(
                "Confirmar seleção para entrada",
                type="primary",
                width=LARGURA_BOTAO,
            )

        if confirmar_selecao:
            ctes_confirmados = set(selecionados_no_formulario)
            confirmacao_registrada = True
            st.session_state[CHAVE_CONFIRMACOES_DIVERGENCIAS] = {
                "arquivos": identificador_arquivos,
                "ctes": sorted(ctes_confirmados),
            }
            success(
                f"Confirmação registrada: {len(ctes_confirmados)} de "
                f"{total_divergencias} CTE(s) divergente(s) liberado(s)."
            )

    tabela_para_entrada = tabela.copy()
    situacoes: list[str] = []
    for indice, linha in tabela_para_entrada.iterrows():
        cte = str(linha["CTE"]).strip()
        volumes_sistema = linha["Volumes Sistema"]
        if pd.isna(volumes_sistema):
            tabela_para_entrada.at[indice, "CTEs para dar entrada"] = ""
            situacoes.append("VOLUME DO SISTEMA NÃO LOCALIZADO")
            continue

        divergente = (
            int(volumes_sistema) != int(linha["Volumes Etiquetas"])
            or bool(str(linha["Etiquetas faltantes"]).strip())
        )
        if not divergente:
            situacoes.append("LIBERADO - SEM DIVERGÊNCIA")
        elif not confirmacao_registrada:
            tabela_para_entrada.at[indice, "CTEs para dar entrada"] = ""
            situacoes.append("AGUARDANDO CONFIRMAÇÃO")
        elif cte in ctes_confirmados:
            situacoes.append("CONFIRMADO")
        else:
            tabela_para_entrada.at[indice, "CTEs para dar entrada"] = ""
            situacoes.append("NÃO AUTORIZADO")

    tabela_para_entrada["Situação da entrada"] = situacoes
    resultado_atual["tabela_para_entrada"] = tabela_para_entrada
    pedidos_fora_para_entrada = pedidos_fora_romaneios.copy()
    situacoes_fora: list[str] = []
    for indice, linha in pedidos_fora_para_entrada.iterrows():
        situacao_consulta = str(linha["Situação"])
        if situacao_consulta != "Fora dos romaneios consultados":
            pedidos_fora_para_entrada.at[indice, "CTEs para dar entrada"] = ""
            situacoes_fora.append(situacao_consulta)
            continue

        cte = str(linha["CTE"]).strip()
        volumes_sistema = linha["Volumes Fraction"]
        if pd.isna(volumes_sistema):
            pedidos_fora_para_entrada.at[indice, "CTEs para dar entrada"] = ""
            situacoes_fora.append("VOLUME DO SISTEMA NÃO LOCALIZADO")
            continue

        divergente = (
            int(volumes_sistema) != int(linha["Volumes Etiquetas"])
            or bool(str(linha["Etiquetas faltantes"]).strip())
        )
        if not divergente:
            situacoes_fora.append("LIBERADO - SEM DIVERGÊNCIA")
        elif not confirmacao_registrada:
            pedidos_fora_para_entrada.at[indice, "CTEs para dar entrada"] = ""
            situacoes_fora.append("AGUARDANDO CONFIRMAÇÃO")
        elif f"SHIPMENT:{linha['ShipmentId']}" in ctes_confirmados:
            situacoes_fora.append("CONFIRMADO")
        else:
            pedidos_fora_para_entrada.at[indice, "CTEs para dar entrada"] = ""
            situacoes_fora.append("NÃO AUTORIZADO")

    pedidos_fora_para_entrada["Situação da entrada"] = situacoes_fora
    resultado_atual["pedidos_fora_romaneios_para_entrada"] = pedidos_fora_para_entrada

    situacoes_liberadas = ["LIBERADO - SEM DIVERGÊNCIA", "CONFIRMADO"]
    situacoes_pendentes = [
        "AGUARDANDO CONFIRMAÇÃO",
        "NÃO AUTORIZADO",
        "VOLUME DO SISTEMA NÃO LOCALIZADO",
        "Falha na consulta Fraction",
        "Fraction indisponível",
        "Consulta interrompida: Fraction indisponível",
        "Não encontrado no Fraction",
    ]
    liberados = tabela_para_entrada["Situação da entrada"].isin(
        situacoes_liberadas
    ).sum() + pedidos_fora_para_entrada["Situação da entrada"].isin(
        situacoes_liberadas
    ).sum()
    pendentes = tabela_para_entrada["Situação da entrada"].isin(
        situacoes_pendentes
    ).sum() + pedidos_fora_para_entrada["Situação da entrada"].isin(
        situacoes_pendentes
    ).sum()
    st.caption(
        f"Próxima etapa: {liberados} CTE(s) liberado(s); "
        f"{pendentes} CTE(s) fora da entrada por confirmação ou volume pendente."
    )

    st.markdown("#### Pedidos fora dos romaneios consultados")
    if pedidos_fora_para_entrada.empty:
        st.caption("Todos os ShipmentIds do TXT foram encontrados nos Unitizados consultados.")
    else:
        st.dataframe(
            pedidos_fora_para_entrada,
            hide_index=True,
            width="stretch",
            column_config={
                "CTE": st.column_config.TextColumn("CTE"),
                "ShipmentId": st.column_config.TextColumn("ShipmentId"),
                "Etiquetas do pedido": st.column_config.TextColumn(
                    "Etiquetas do pedido", width="large"
                ),
                "Volumes Fraction": st.column_config.NumberColumn("Volumes Fraction"),
                "Volumes Etiquetas": st.column_config.NumberColumn("Volumes Etiquetas"),
                "Etiquetas faltantes": st.column_config.TextColumn(
                    "Etiquetas faltantes", width="large"
                ),
                "CTEs para dar entrada": st.column_config.TextColumn(
                    "CTEs para dar entrada", width="large"
                ),
                "Situação": st.column_config.TextColumn("Situação"),
                "Situação da entrada": st.column_config.TextColumn(
                    "Situação da entrada"
                ),
            },
        )

    ctes_liberados = coletar_ctes_liberados_para_entrada(
        tabela_para_entrada,
        pedidos_fora_para_entrada,
    )
    registro_adicoes = st.session_state.get(CHAVE_ADICAO_RECEBIMENTO, {})
    if registro_adicoes.get("arquivos") == identificador_arquivos:
        ctes_adicionados = set(registro_adicoes.get("adicionados", []))
        ctes_incerto = set(registro_adicoes.get("incertos", []))
    else:
        ctes_adicionados = set()
        ctes_incerto = set()
    ctes_ja_tentados = ctes_adicionados | ctes_incerto
    ctes_para_adicionar = [
        cte for cte in ctes_liberados if cte not in ctes_ja_tentados
    ]

    st.caption(
        f"{len(ctes_liberados)} CTE(s) liberado(s) para recebimento. "
        "Serão enviados em lotes de até 1.000, um código por linha; ao final, "
        "a lista será processada no Fraction."
    )
    if ctes_incerto:
        warning(
            f"A entrada de {len(ctes_incerto)} CTE(s) pode ter sido recebida pelo Fraction. "
            "Confira o resultado no sistema antes de tentar novamente."
        )
    elif ctes_liberados and not ctes_para_adicionar:
        st.info("Todos os CTEs liberados desta base já foram processados no recebimento.")
    elif not ctes_liberados:
        st.info("Não há CTEs liberados para adicionar. Confira as divergências e volumes pendentes.")

    adicionar = st.button(
        "Dar entrada nos pedidos",
        type="primary",
        key=f"dar_entrada_adicionar_{identificador_arquivos[:16]}",
        width=LARGURA_BOTAO,
        disabled=not ctes_para_adicionar,
    )
    if adicionar:
        _, log_visual = criar_painel_execucao(CHAVE_LOGS)
        log, _ = criar_logger_execucao(
            "dar_entrada_pedidos",
            log_visual,
            rotulo="recebimento_remessa",
        )
        log(
            f"Iniciando entrada de {len(ctes_para_adicionar)} CTE(s) no "
            "Recebimento Remessa."
        )
        try:
            with st.spinner("Dando entrada nos pedidos no Fraction..."):
                resultado_adicao = adicionar_ctes_ao_recebimento_fraction(
                    ctes_para_adicionar,
                    log=log,
                )
            st.session_state[CHAVE_ADICAO_RECEBIMENTO] = {
                "arquivos": identificador_arquivos,
                "adicionados": sorted(
                    ctes_adicionados | set(resultado_adicao["ctes"])
                ),
                "incertos": sorted(ctes_incerto),
            }
            try:
                caminho_base = salvar_base_entrada_pedidos(
                    tabela_para_entrada,
                    pedidos_fora_para_entrada,
                    sorted(ctes_adicionados | set(resultado_adicao["ctes"])),
                    log=log,
                )
                success(
                    f"Entrada concluída: {resultado_adicao['quantidade']} CTE(s) "
                    f"em {resultado_adicao['lotes']} lote(s). Base completa salva em "
                    f"{caminho_base}."
                )
            except Exception as erro_salvamento:
                log(
                    "A entrada foi concluída no Fraction, mas não foi possível "
                    f"salvar a planilha: {type(erro_salvamento).__name__}: "
                    f"{erro_salvamento}"
                )
                warning(
                    "A entrada foi concluída no Fraction, mas a planilha não foi "
                    f"salva: {erro_salvamento}"
                )
        except AdicaoRecebimentoIndeterminadaError as erro_indeterminado:
            st.session_state[CHAVE_ADICAO_RECEBIMENTO] = {
                "arquivos": identificador_arquivos,
                "adicionados": sorted(ctes_adicionados),
                "incertos": sorted(ctes_incerto | set(ctes_para_adicionar)),
            }
            warning(str(erro_indeterminado))
        except CredenciaisFractionInvalidasError as erro_credenciais:
            warning(str(erro_credenciais))
        except Exception as erro_execucao:
            log(f"Erro: {type(erro_execucao).__name__}: {erro_execucao}")
            error(f"A entrada dos CTEs foi interrompida: {erro_execucao}")

    avisos = resultado_atual.get("avisos", {})
    avisos_com_conteudo = {
        nome: valores
        for nome, valores in avisos.items()
        if valores and nome != "ShipmentIds do TXT sem linha no Unitizado"
    }
    if avisos_com_conteudo:
        with st.expander("Detalhes de dados não associados"):
            for nome, valores in avisos_com_conteudo.items():
                st.markdown(f"**{nome}**")
                if isinstance(valores, dict):
                    st.code(
                        "\n".join(f"{chave}: {valor}" for chave, valor in valores.items()),
                        language=None,
                        wrap_lines=True,
                    )
                else:
                    st.code("\n".join(map(str, valores)), language=None, wrap_lines=True)

    arquivo_excel = BytesIO()
    with pd.ExcelWriter(arquivo_excel, engine="openpyxl") as escritor:
        tabela_para_entrada.to_excel(
            escritor,
            index=False,
            sheet_name="Conferencia",
        )
        pedidos_fora_para_entrada.to_excel(
            escritor,
            index=False,
            sheet_name="Fora dos romaneios",
        )
    st.download_button(
        "Baixar conferência em Excel",
        data=arquivo_excel.getvalue(),
        file_name="dar_entrada_em_pedidos.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key="dar_entrada_pedidos_baixar_conferencia",
    )
