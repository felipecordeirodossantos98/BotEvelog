from __future__ import annotations

from hashlib import sha256
from io import BytesIO
from pathlib import Path
import tempfile

import pandas as pd
import streamlit as st

from automacoes.dar_entrada_pedidos import (
    AdicaoRecebimentoIndeterminadaError,
    adicionar_ctes_ao_recebimento_fraction,
    agrupar_etiquetas_importadas,
    baixar_bases_por_romaneios,
    coletar_ctes_liberados_para_entrada,
    montar_base_pedidos,
    normalizar_numeros_romaneio,
    salvar_base_entrada_pedidos,
)
from automacoes.gerar_romaneios import (
    baixar_etiquetas_drive,
    preparar_tabela_entrada_para_fila,
    selecionar_txt_novos_do_zip,
)
from servicos.fraction import CredenciaisFractionInvalidasError
from servicos.fila_romaneios_emissao import FilaRomaneiosEmissao
from servicos.romaneios_finalizados import sincronizar_shipments_finalizados
from utils.interface import (
    LARGURA_BOTAO,
    LARGURA_COMPONENTE,
    avisar_variaveis_faltantes,
    error,
    success,
    warning,
)
from utils.config import (
    ARQUIVO_FILA_ROMANEIOS_EMISSAO,
    ARQUIVO_FILA_ROMANEIOS_ENTRADA,
)
from utils.logs import criar_logger_execucao


CHAVE_RESULTADO = "visual_dar_entrada_pedidos_resultado"
CHAVE_CONFIRMACOES_DIVERGENCIAS = "visual_dar_entrada_pedidos_confirmacoes"
CHAVE_ADICAO_RECEBIMENTO = "visual_dar_entrada_pedidos_processados_v2"
CHAVE_ETIQUETAS_DRIVE = "visual_dar_entrada_pedidos_etiquetas_drive"


def _filtrar_etiquetas_ja_na_fila(
    pedidos: list[dict[str, object]],
    etiquetas_conhecidas: set[str],
    *,
    log,
) -> list[dict[str, object]]:
    conhecidas = {str(etiqueta).strip() for etiqueta in etiquetas_conhecidas}
    filtrados: list[dict[str, object]] = []
    removidas = 0

    for pedido in pedidos:
        por_palete = pedido.get("ETIQUETAS_POR_PALETE")
        if not isinstance(por_palete, dict):
            por_palete = {"Sem palete": pedido.get("ETIQUETAS", [])}

        novas_por_palete = {
            str(palete): [
                str(etiqueta).strip()
                for etiqueta in etiquetas
                if str(etiqueta).strip()
                and str(etiqueta).strip() not in conhecidas
            ]
            for palete, etiquetas in por_palete.items()
        }
        novas_por_palete = {
            palete: etiquetas
            for palete, etiquetas in novas_por_palete.items()
            if etiquetas
        }
        etiquetas_novas = [
            etiqueta
            for etiquetas in novas_por_palete.values()
            for etiqueta in etiquetas
        ]
        etiquetas_anteriores = len(pedido.get("ETIQUETAS", []))
        removidas += max(etiquetas_anteriores - len(etiquetas_novas), 0)
        if not etiquetas_novas:
            continue

        pedido_atualizado = dict(pedido)
        pedido_atualizado["ETIQUETAS"] = etiquetas_novas
        pedido_atualizado["VOLUMES_ETIQUETAS"] = len(etiquetas_novas)
        pedido_atualizado["ETIQUETAS_POR_PALETE"] = novas_por_palete
        ruas = pedido.get("RUA_POR_PALETE", {})
        pedido_atualizado["RUA_POR_PALETE"] = {
            palete: ruas.get(palete, "")
            for palete in novas_por_palete
        }
        filtrados.append(pedido_atualizado)

    if removidas:
        log(
            f"Ignoradas {removidas} etiqueta(s) já presentes na fila Entrada; "
            f"restaram {sum(len(pedido['ETIQUETAS']) for pedido in filtrados)} "
            "etiqueta(s) nova(s) para conferência."
        )
    return filtrados


def renderizar_dar_entrada_pedidos() -> None:
    shipments_finalizados = sincronizar_shipments_finalizados(
        [ARQUIVO_FILA_ROMANEIOS_EMISSAO, ARQUIVO_FILA_ROMANEIOS_ENTRADA]
    )
    st.subheader("Dar entrada em pedidos")

    faltantes = avisar_variaveis_faltantes("dar_entrada_pedidos")
    numeros_romaneio_texto = st.text_area(
        "Números dos romaneios",
        placeholder="Informe um ou mais números, um por linha ou separados por vírgula",
        height=110,
        width=LARGURA_COMPONENTE,
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

    buscar_drive = st.button(
        "Sincronizar etiquetas do Drive",
        key="dar_entrada_pedidos_buscar_etiquetas_drive",
        width=LARGURA_BOTAO,
    )
    if buscar_drive:
        faltantes_drive = avisar_variaveis_faltantes("importar_etiquetas_drive")
        if not faltantes_drive:
            st.session_state.pop(CHAVE_ETIQUETAS_DRIVE, None)
            st.session_state.pop(CHAVE_RESULTADO, None)
            st.session_state.pop(CHAVE_CONFIRMACOES_DIVERGENCIAS, None)
            log, _ = criar_logger_execucao(
                "dar_entrada_pedidos",
                rotulo="buscar_etiquetas_drive_entrada",
            )
            try:
                with tempfile.TemporaryDirectory(
                    prefix="botevelog_entrada_drive_"
                ) as diretorio_temporario:
                    with st.spinner("Buscando arquivos de Entrada no Drive..."):
                        caminho_zip = baixar_etiquetas_drive(
                            pasta_destino=Path(diretorio_temporario),
                            termo_nome="Entrada",
                            log=log,
                        )
                    if caminho_zip is None:
                        warning(
                            "Não encontrei TXT com 'Entrada' no nome dos arquivos do Drive."
                        )
                    else:
                        fila_entrada = FilaRomaneiosEmissao(
                            ARQUIVO_FILA_ROMANEIOS_ENTRADA
                        )
                        conteudo_novo, arquivos_novos = selecionar_txt_novos_do_zip(
                            caminho_zip.read_bytes(),
                            fila_entrada.arquivos_dar_entrada_importados(),
                            etiquetas_ja_conhecidas=(
                                fila_entrada.etiquetas_conhecidas()
                            ),
                            termo_nome="Entrada",
                            log=log,
                        )
                        if not arquivos_novos:
                            warning("Não há bases novas para sincronizar.")
                        else:
                            st.session_state[CHAVE_ETIQUETAS_DRIVE] = {
                                "nome": caminho_zip.name,
                                "conteudo": conteudo_novo,
                                "arquivos": arquivos_novos,
                            }
                            st.session_state.pop(CHAVE_RESULTADO, None)
                            st.session_state.pop(
                                CHAVE_CONFIRMACOES_DIVERGENCIAS,
                                None,
                            )
                            log(
                                f"Selecionados localmente {len(arquivos_novos)} "
                                "TXT(s) novo(s) com 'Entrada' no nome."
                            )
                            success(
                                f"{len(arquivos_novos)} TXT(s) novo(s) carregado(s). "
                                "Confira os romaneios e monte a base para dar entrada."
                            )
            except Exception as erro_drive:
                log(
                    f"Falha ao buscar etiquetas no Drive: "
                    f"{type(erro_drive).__name__}: {erro_drive}"
                )
                error(f"Não foi possível buscar etiquetas no Drive: {erro_drive}")

    arquivo_etiquetas_drive = st.session_state.get(CHAVE_ETIQUETAS_DRIVE)
    if arquivo_etiquetas_drive:
        nome_arquivo_etiquetas = str(arquivo_etiquetas_drive["nome"])
        conteudo_etiquetas = bytes(arquivo_etiquetas_drive["conteudo"])
        st.caption(f"Etiquetas carregadas do Drive: {nome_arquivo_etiquetas}")
    else:
        nome_arquivo_etiquetas = ""
        conteudo_etiquetas = b""

    arquivo_pronto = bool(conteudo_etiquetas)
    if not arquivo_pronto:
        st.info("Busque as etiquetas no Drive para continuar.")
    identificador_arquivos = None
    if arquivo_pronto and numeros_romaneio and not erro_romaneios:
        identificador_arquivos = sha256(
            "\n".join(numeros_romaneio).encode("utf-8")
            + b"\0"
            + nome_arquivo_etiquetas.encode("utf-8")
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
        log, _ = criar_logger_execucao(
            "dar_entrada_pedidos",
            rotulo="dar_entrada_pedidos",
        )
        try:
            log(
                f"Iniciando consulta para {len(numeros_romaneio)} romaneio(s): "
                + ", ".join(numeros_romaneio)
            )
            log(
                f"Registro compartilhado: {len(shipments_finalizados)} "
                "ShipmentId(s) já finalizados serão mantidos fora da fila de romaneios."
            )
            pedidos = agrupar_etiquetas_importadas(
                conteudo_etiquetas,
                nome_arquivo_etiquetas,
                log=log,
            )
            fila_entrada = FilaRomaneiosEmissao(
                ARQUIVO_FILA_ROMANEIOS_ENTRADA
            )
            pedidos = _filtrar_etiquetas_ja_na_fila(
                pedidos,
                fila_entrada.etiquetas_conhecidas(),
                log=log,
            )
            if not pedidos:
                warning(
                    "Todas as etiquetas deste arquivo já constam na fila de Entrada."
                )
                return
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
                    log(
                        f"{len(pedidos_fora_romaneios)} ShipmentId(s) do TXT não "
                        "constam nos Unitizados consultados; permanecerão fora da entrada."
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
    total_divergencias = len(divergencias_df)
    if total_divergencias:
        warning("Inconsistência de volumes.")
        st.caption(
            "A conferência considera somente as etiquetas bipadas para formar os CTEs. "
            "Etiquetas faltantes ficam fora desta entrada."
        )
    total_sem_volume = len(sem_volume)
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
    pedidos_fora_para_entrada["CTEs para dar entrada"] = ""
    pedidos_fora_para_entrada["Situação da entrada"] = (
        "FORA DOS ROMANEIOS CONSULTADOS — NÃO SERÁ INCLUÍDO"
    )
    resultado_atual["pedidos_fora_romaneios_para_entrada"] = pedidos_fora_para_entrada

    situacoes_liberadas = ["LIBERADO - SEM DIVERGÊNCIA", "CONFIRMADO"]
    liberados = tabela_para_entrada["Situação da entrada"].isin(
        situacoes_liberadas
    ).sum()
    pendentes = tabela_para_entrada["Situação da entrada"].isin(
        ["AGUARDANDO CONFIRMAÇÃO", "NÃO AUTORIZADO", "VOLUME DO SISTEMA NÃO LOCALIZADO"]
    ).sum()
    st.caption(
        f"Próxima etapa: {liberados} CTE(s) liberado(s); "
        f"{pendentes} CTE(s) fora da entrada por confirmação ou volume pendente."
    )

    if not pedidos_fora_para_entrada.empty:
        st.markdown("#### Pedidos fora dos romaneios consultados")
        tabela_fora_romaneios_exibicao = pedidos_fora_para_entrada[
            ["ShipmentId", "Etiquetas do pedido"]
        ]
        st.dataframe(
            tabela_fora_romaneios_exibicao,
            hide_index=True,
            width="stretch",
            column_config={
                "ShipmentId": st.column_config.TextColumn("ShipmentId"),
                "Etiquetas do pedido": st.column_config.TextColumn(
                    "Etiquetas do pedido", width="large"
                ),
            },
        )

    ctes_liberados = coletar_ctes_liberados_para_entrada(tabela_para_entrada)
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
        log, _ = criar_logger_execucao(
            "dar_entrada_pedidos",
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
            ctes_confirmados = ctes_adicionados | set(resultado_adicao["ctes"])

            try:
                tabela_fila_entrada = preparar_tabela_entrada_para_fila(
                    tabela_para_entrada,
                    ctes_confirmados,
                    log=log,
                )
                if tabela_fila_entrada.empty:
                    log(
                        "Nenhum pedido recebido foi incluído na fila de romaneios "
                        "Entrada."
                    )
                else:
                    fila_entrada = FilaRomaneiosEmissao(
                        ARQUIVO_FILA_ROMANEIOS_ENTRADA
                    )
                    resultado_fila = fila_entrada.importar_fila(
                        tabela_fila_entrada,
                        [],
                        shipments_finalizados=shipments_finalizados,
                        log=log,
                    )
                    ctes_romaneio = {
                        cte.strip()
                        for valor in tabela_fila_entrada[
                            "CTEs para romaneio"
                        ].fillna("").astype(str)
                        for cte in valor.splitlines()
                        if cte.strip()
                    }
                    log(
                        f"Fila Entrada atualizada: {resultado_fila['novas']} "
                        f"etiqueta(s) adicionada(s), {len(ctes_romaneio)} "
                        "CTE(s) para romaneio, "
                        f"{resultado_fila['repetidas']} já existente(s)."
                    )
            except Exception as erro_fila:
                log(
                    "A entrada foi concluída no Fraction, mas não foi possível "
                    "atualizar a fila de romaneios Entrada: "
                    f"{type(erro_fila).__name__}: {erro_fila}"
                )
                warning(
                    "A entrada foi concluída, mas não foi possível atualizar a "
                    f"fila de romaneios Entrada: {erro_fila}"
                )

            arquivos_drive_entrada = list(
                (arquivo_etiquetas_drive or {}).get("arquivos", [])
            )
            if (
                arquivos_drive_entrada
                and ctes_liberados
                and pendentes == 0
                and set(ctes_liberados).issubset(ctes_confirmados)
            ):
                fila_entrada = FilaRomaneiosEmissao(
                    ARQUIVO_FILA_ROMANEIOS_ENTRADA
                )
                registrados = fila_entrada.registrar_arquivos_dar_entrada(
                    arquivos_drive_entrada
                )
                log(
                    f"{registrados} TXT(s) de Entrada registrados como processados "
                    "após confirmação de todos os CTEs no Fraction."
                )
            elif arquivos_drive_entrada:
                log(
                    "TXT de Entrada mantidos como pendentes de sincronização: "
                    "a conferência ainda tem CTE(s) aguardando confirmação, "
                    "volume ou processamento."
                )
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
