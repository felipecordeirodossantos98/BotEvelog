from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from pathlib import Path
import re
import tempfile
import unicodedata

import pandas as pd
import streamlit as st

from servicos.fraction import (
    CredenciaisFractionInvalidasError,
    associar_destinos_fraction,
    carregar_codigos_preferenciais_unidades,
    carregar_destinos_fraction,
    carregar_regras_triagem_rotas,
    executar_manifestos_romaneios,
    listar_triagens_por_localidade,
    enviar_pdf_para_impressora_padrao,
    mapear_rotas_para_grupos,
    preparar_lote_impressao_romaneios,
)
from automacoes.gerar_romaneios import (
    COLUNAS_RESULTADO,
    baixar_etiquetas_drive,
    carregar_codigos_preferenciais_unidades_emissao,
    consultar_dados_performance_ctes,
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
from servicos.romaneios_finalizados import (
    registrar_shipments_finalizados,
    sincronizar_shipments_finalizados,
)
from utils.interface import (
    LARGURA_BOTAO,
    avisar_variaveis_faltantes,
    error,
    success,
    warning,
)
from utils.config import (
    ARQUIVO_FILA_ROMANEIOS_EMISSAO,
    ARQUIVO_FILA_ROMANEIOS_ENTRADA,
    ARQUIVO_ROTAS_ROMANEIOS,
    RESULTADOS_ROMANEIOS,
)
from utils.logs import criar_logger_execucao


CHAVE_RESULTADO = "visual_gerar_romaneios_resultado"
COLUNAS_OCULTAS_DF_ROMANEIOS = [
    "Volumes Sistema",
    "Volumes Etiquetas",
    "Etiquetas faltantes",
    "CTEs para dar entrada",
    "Manifesto",
    "Origem",
]


def _preparar_df_exibicao_romaneios(tabela: pd.DataFrame) -> pd.DataFrame:
    colunas_ocultas = ["CTEs por etiqueta", *COLUNAS_OCULTAS_DF_ROMANEIOS]
    if "Status" not in tabela.columns or not tabela["Status"].fillna("").map(
        lambda valor: _normalizar_rota(valor) == "ENTRADA"
    ).any():
        colunas_ocultas.append("CTEs para romaneio")
    return tabela.drop(
        columns=colunas_ocultas,
        errors="ignore",
    )


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
    regras = carregar_regras_triagem_rotas(ARQUIVO_ROTAS_ROMANEIOS)
    codigos_preferenciais = carregar_codigos_preferenciais_unidades()
    relevantes = {_normalizar_rota(unidade) for unidade in unidades_relevantes}
    linhas = []

    for regra in regras:
        unidade = str(regra["unidade"])
        chave_unidade = _normalizar_rota(unidade)
        if chave_unidade not in relevantes:
            continue
        linhas.append(
            {
                "Unidade": unidade,
                "UF": ", ".join(regra["ufs"]),
                "Cidade": ", ".join(regra["cidades"]),
                "Destino": ", ".join(regra["destinos"]),
                "Triagem": str(regra["triagem"]),
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
            "Triagem",
            "UF",
            "Cidade",
            "Destino",
            "Unidade",
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
        + COLUNAS_RESULTADO[:3]
        + ["CTEs para romaneio"]
        + COLUNAS_RESULTADO[3:-2]
        + ["UF", "Cidade"]
        + COLUNAS_RESULTADO[-2:]
        + [
            "CTEs por etiqueta",
            "Triagem",
            "Rota",
            "Origem",
            "Status",
        ]
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
        status_pedido = str(primeira_preenchida("status") or "")
        ctes_base = [
            str(valor).strip()
            for valor in grupo.get("cte_base", pd.Series(dtype=str)).tolist()
            if pd.notna(valor) and str(valor).strip()
        ]
        cte = ctes_base[0] if ctes_base else (
            ctes_do_pedido[0] if ctes_do_pedido else ""
        )
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
                "CTEs para romaneio": (
                    "\n".join(ctes_do_pedido)
                    if _normalizar_rota(status_pedido) == "ENTRADA"
                    else ""
                ),
                "UF": str(primeira_preenchida("uf") or ""),
                "Cidade": str(primeira_preenchida("cidade") or ""),
                "Destino": str(primeira_preenchida("destino") or ""),
                "Manifesto": "",
                "Triagem": str(primeira_preenchida("triagem") or ""),
                "Rota": str(primeira_preenchida("rota") or ""),
                "Origem": str(primeira_preenchida("origem") or ""),
                "Status": status_pedido,
            }
        )

    return pd.DataFrame(linhas, columns=colunas)


def _recalcular_rotas_fila_aberta(
    fila: FilaRomaneiosEmissao,
    status_fila: str,
    *,
    log,
) -> int:
    tabela = _montar_tabela_fila_persistida(fila)
    if tabela.empty or "Status" not in tabela.columns:
        log(f"Fila {status_fila} sem pedidos abertos para recalcular rotas.")
        return 0
    tabela = tabela.loc[
        tabela["Status"].fillna("").map(
            lambda valor: _normalizar_rota(valor) == status_fila
        )
    ].copy()
    if tabela.empty:
        log(f"Fila {status_fila} sem pedidos abertos para recalcular rotas.")
        return 0

    recalculada = recalcular_rotas_fila_emissao(tabela, log=log)
    atualizadas = fila.atualizar_rotas_abertas(recalculada)
    log(
        f"Rotas da fila {status_fila} reavaliadas pela configuração atual; "
        f"{atualizadas} etiqueta(s) tiveram rota/triagem atualizadas."
    )
    return atualizadas


def _montar_df_ctes_volume_fila(tabela: pd.DataFrame) -> pd.DataFrame:
    linhas = []
    for _, pedido in tabela.iterrows():
        cte_base = str(pedido.get("CTE", "") or "").strip()
        uf = str(pedido.get("UF", "") or "").strip()
        cidade = str(pedido.get("Cidade", "") or "").strip()
        ctes_volume = [
            cte.strip()
            for cte in str(pedido.get("CTEs para romaneio", "") or "").splitlines()
            if cte.strip()
        ]
        for cte_volume in ctes_volume:
            linhas.append(
                {
                    "CTE": cte_base,
                    "CTE Volume": cte_volume,
                    "UF": uf,
                    "Cidade": cidade,
                }
            )
    return pd.DataFrame(linhas, columns=["CTE", "CTE Volume", "UF", "Cidade"])


def _normalizar_cte_base_volume(valor: object) -> str:
    if pd.isna(valor):
        return ""
    cte = str(valor).strip()
    return cte[:-3] if cte.isdigit() and len(cte) == 17 else cte


def _resumir_ctes_e_volumes(ctes: list[str]) -> tuple[int, int]:
    ctes_unicos = list(
        dict.fromkeys(str(cte).strip() for cte in ctes if str(cte).strip())
    )
    ctes_base = {
        base
        for cte in ctes_unicos
        if (base := _normalizar_cte_base_volume(cte))
    }
    return len(ctes_base), len(ctes_unicos)


def _extrair_ctes_ignorados(grupos_ignorados: list[str]) -> list[str]:
    ctes = []
    for mensagem in grupos_ignorados:
        correspondencia = re.match(
            r"^CTE\(s\)\s+(.+?)(?:\s+em\s+|:\s)",
            mensagem,
        )
        if correspondencia:
            ctes.extend(
                cte.strip()
                for cte in correspondencia.group(1).split(",")
                if cte.strip()
            )
    return list(dict.fromkeys(ctes))


def _renderizar_atualizacao_fila(
    status_fila: str,
    arquivo_fila: Path,
    termo_nome: str,
    sufixo_chave: str,
    sincronizacao_disponivel: bool,
) -> None:
    atualizar = st.button(
        "Atualizar filas",
        type="primary",
        key=f"gerar_romaneios_{sufixo_chave}_atualizar_filas",
        width=LARGURA_BOTAO,
        disabled=not sincronizacao_disponivel,
    )
    if not atualizar:
        return

    log, _ = criar_logger_execucao(
        "gerar_romaneios",
        rotulo=f"atualizar_fila_{sufixo_chave}",
    )
    fila = FilaRomaneiosEmissao(arquivo_fila)
    shipments_finalizados = sincronizar_shipments_finalizados(
        [ARQUIVO_FILA_ROMANEIOS_EMISSAO, ARQUIVO_FILA_ROMANEIOS_ENTRADA]
    )
    log(
        f"Registro compartilhado: {len(shipments_finalizados)} ShipmentId(s) "
        "já finalizados pela automação."
    )
    finalizados_por_status = 0
    if status_fila == "EMISSAO":
        abertas_existentes = fila.carregar_etiquetas_abertas()
        shipments_ja_fora_emissao = {
            str(linha.get("shipment_id", "")).strip()
            for linha in abertas_existentes.to_dict(orient="records")
            if str(linha.get("shipment_id", "")).strip()
            and _normalizar_rota(linha.get("status", ""))
            not in {"", "EMISSAO"}
        }
        if shipments_ja_fora_emissao:
            etiquetas_finalizadas = fila.finalizar_shipments(
                shipments_ja_fora_emissao
            )
            registrar_shipments_finalizados(shipments_ja_fora_emissao)
            finalizados_por_status = len(shipments_ja_fora_emissao)
            shipments_finalizados = sincronizar_shipments_finalizados(
                [
                    ARQUIVO_FILA_ROMANEIOS_EMISSAO,
                    ARQUIVO_FILA_ROMANEIOS_ENTRADA,
                ]
            )
            log(
                f"{finalizados_por_status} ShipmentId(s) com status diferente de "
                f"EMISSAO foram finalizados antes da consulta; "
                f"{etiquetas_finalizadas} etiqueta(s) saíram das filas abertas."
            )
    novas_etiquetas = 0
    etiquetas_repetidas = 0
    ctes_com_status = 0
    etiquetas_com_status_atualizado = 0
    etiquetas_com_rota_atualizada = 0
    erros: list[str] = []
    avisos: list[str] = []

    try:
        caminho_estado = preparar_estado_etiquetas_romaneio(status_fila)
        log(f"Arquivo de etiquetas já processadas: {caminho_estado}.")
        log(f"Banco local da fila incremental ({status_fila}): {arquivo_fila}.")
        etiquetas_ja_conhecidas = {
            etiqueta.strip()
            for etiqueta in caminho_estado.read_text(encoding="utf-8-sig").splitlines()
            if etiqueta.strip()
        }
        etiquetas_ja_conhecidas.update(fila.etiquetas_conhecidas())
        with tempfile.TemporaryDirectory(
            prefix=f"botevelog_{sufixo_chave}_"
        ) as diretorio_temporario:
            pasta_temporaria = Path(diretorio_temporario)

            etiquetas_abertas = fila.carregar_etiquetas_abertas()
            ctes_abertos = list(dict.fromkeys(
                str(cte).strip()
                for cte in etiquetas_abertas.get("cte", pd.Series(dtype=str)).tolist()
                if pd.notna(cte) and str(cte).strip()
            ))
            if ctes_abertos:
                try:
                    with st.spinner(
                        f"Atualizando Status de {len(ctes_abertos)} CTE(s)..."
                    ):
                        status_por_cte = consultar_status_performance_ctes(
                            ctes_abertos,
                            log=log,
                            pasta_temporaria=pasta_temporaria,
                        )
                    etiquetas_com_status_atualizado = fila.atualizar_status_abertas(
                        status_por_cte
                    )
                    ctes_com_status = len(status_por_cte)
                    if status_fila == "EMISSAO":
                        shipments_por_cte: dict[str, set[str]] = {}
                        for linha in etiquetas_abertas.to_dict(orient="records"):
                            shipment_id = str(linha.get("shipment_id", "")).strip()
                            if not shipment_id:
                                continue
                            for campo_cte in ("cte_base", "cte"):
                                cte = str(linha.get(campo_cte, "")).strip()
                                if cte:
                                    shipments_por_cte.setdefault(cte, set()).add(
                                        shipment_id
                                    )
                        shipments_fora_emissao = {
                            shipment_id
                            for cte, status in status_por_cte.items()
                            if str(cte).strip()
                            and _normalizar_rota(status) != "EMISSAO"
                            for shipment_id in shipments_por_cte.get(
                                str(cte).strip(), set()
                            )
                        }
                        if shipments_fora_emissao:
                            etiquetas_finalizadas = fila.finalizar_shipments(
                                shipments_fora_emissao
                            )
                            registrar_shipments_finalizados(shipments_fora_emissao)
                            finalizados_por_status += len(shipments_fora_emissao)
                            shipments_finalizados = sincronizar_shipments_finalizados(
                                [
                                    ARQUIVO_FILA_ROMANEIOS_EMISSAO,
                                    ARQUIVO_FILA_ROMANEIOS_ENTRADA,
                                ]
                            )
                            log(
                                f"{len(shipments_fora_emissao)} ShipmentId(s) "
                                "finalizados porque a Performance retornou status "
                                "diferente de EMISSAO; "
                                f"{etiquetas_finalizadas} etiqueta(s) saíram das "
                                "filas abertas."
                            )
                    ctes_sem_status = [
                        cte for cte in ctes_abertos if cte not in status_por_cte
                    ]
                    log(
                        f"Status atualizado para {etiquetas_com_status_atualizado} "
                        f"etiqueta(s) aberta(s), em {ctes_com_status} CTE(s)."
                    )
                    if ctes_sem_status:
                        avisos.append(
                            f"A Performance não retornou Status para "
                            f"{len(ctes_sem_status)} CTE(s); os valores anteriores "
                            "foram mantidos."
                        )
                        log(avisos[-1])
                except Exception as erro_status:
                    mensagem = (
                        f"Falha ao atualizar Status: {type(erro_status).__name__}: "
                        f"{erro_status}"
                    )
                    log(mensagem)
                    erros.append(mensagem)
            else:
                log(
                    f"Não há CTEs em filas abertas de {status_fila} para "
                    "atualizar o Status."
                )

            try:
                with st.spinner("Sincronizando arquivos novos..."):
                    caminho_zip = baixar_etiquetas_drive(
                        pasta_destino=pasta_temporaria,
                        termo_nome=termo_nome,
                        log=log,
                    )
                if caminho_zip is None:
                    log("Nenhum TXT novo foi encontrado no Drive.")
                else:
                    conteudo_zip = caminho_zip.read_bytes()
                    conteudo_etiquetas, arquivos_novos = selecionar_txt_novos_do_zip(
                        conteudo_zip,
                        fila.arquivos_importados(),
                        etiquetas_ja_conhecidas=etiquetas_ja_conhecidas,
                        termo_nome=termo_nome,
                        log=log,
                    )
                    if not arquivos_novos:
                        log("O ZIP baixado não contém TXT novo para importar.")
                    else:
                        with st.spinner("Importando etiquetas e atualizando a fila..."):
                            dados = montar_fila_emissao(
                                conteudo_etiquetas,
                                nome_arquivo_etiquetas="txt_novos_drive.zip",
                                status_fila=status_fila,
                                etiquetas_ja_conhecidas=etiquetas_ja_conhecidas,
                                pasta_temporaria=pasta_temporaria,
                                log=log,
                            )
                            contagem = fila.importar_fila(
                                dados["tabela"],
                                arquivos_novos,
                                shipments_finalizados=shipments_finalizados,
                                log=log,
                            )
                        novas_etiquetas = contagem["novas"]
                        etiquetas_repetidas = contagem["repetidas"]
                        if contagem.get("conflitos", 0):
                            quantidade_conflitos = int(contagem["conflitos"])
                            pedidos_isolados = int(
                                contagem.get("pedidos_isolados", 0)
                            )
                            avisos.append(
                                f"{quantidade_conflitos} etiqueta(s) foram isoladas "
                                "por aparecerem em mais de uma rua/triagem; "
                                f"{pedidos_isolados} pedido(s) completo(s) ficaram "
                                "fora das filas abertas para revisão manual."
                            )

                        etiquetas_sem_cte = dados.get(
                            "etiquetas_sem_retorno_fraction", []
                        )
                        if etiquetas_sem_cte:
                            amostra_etiquetas = ", ".join(etiquetas_sem_cte[:5])
                            mais_etiquetas = "…" if len(etiquetas_sem_cte) > 5 else ""
                            avisos.append(
                                f"{len(etiquetas_sem_cte)} etiqueta(s) não retornaram "
                                "Remessa/CTE no Fraction e foram ignoradas. Amostra: "
                                f"{amostra_etiquetas}{mais_etiquetas}"
                            )
                        if dados["shipment_ids_sem_retorno_fraction"]:
                            avisos.append(
                                f"{len(dados['shipment_ids_sem_retorno_fraction'])} "
                                "pedido(s) não retornaram dados de Performance e "
                                "ficaram fora da fila."
                            )
                        if dados["shipment_ids_sem_destino_performance"]:
                            avisos.append(
                                f"{len(dados['shipment_ids_sem_destino_performance'])} "
                                "pedido(s) ficaram sem Destino na Performance; "
                                "confira a rota antes de gerar."
                            )
                        if dados["shipment_ids_sem_status_performance"]:
                            avisos.append(
                                f"{len(dados['shipment_ids_sem_status_performance'])} "
                                f"pedido(s) sem Status na Performance ficaram fora "
                                f"da fila {status_fila}."
                            )
                        if dados["tabela"].empty and not avisos:
                            avisos.append(
                                "Os TXT novos foram processados, mas não havia "
                                "etiquetas elegíveis para entrar na fila."
                            )
            except Exception as erro_drive:
                mensagem = (
                    f"Falha ao sincronizar os arquivos: {type(erro_drive).__name__}: "
                    f"{erro_drive}"
                )
                log(mensagem)
                erros.append(mensagem)

        try:
            etiquetas_com_rota_atualizada = _recalcular_rotas_fila_aberta(
                fila,
                status_fila,
                log=log,
            )
        except (FileNotFoundError, OSError, ValueError) as erro_rotas:
            mensagem = (
                "Não foi possível recalcular as rotas pela configuração atual: "
                f"{type(erro_rotas).__name__}: {erro_rotas}"
            )
            log(mensagem)
            avisos.append(mensagem)

        resumo = (
            f"Atualização concluída: {novas_etiquetas} etiqueta(s) nova(s), "
            f"{etiquetas_repetidas} já conhecida(s)."
        )
        resumo += (
            f" Rotas revisadas; {etiquetas_com_rota_atualizada} etiqueta(s) "
            "tiveram rota/triagem atualizadas."
        )
        if ctes_abertos:
            resumo += (
                f" Status retornado para {ctes_com_status}/{len(ctes_abertos)} "
                f"CTE(s) aberto(s); {etiquetas_com_status_atualizado} etiqueta(s) "
                "tiveram Status atualizado. "
                f"{finalizados_por_status} pedido(s) finalizados por status "
                "diferente de EMISSAO."
            )
        else:
            resumo += (
                f" Não havia CTEs em filas abertas de {status_fila} para consultar."
            )
        if erros:
            error(f"Atualização parcial. {resumo} {'; '.join(erros)}")
        else:
            success(resumo)
        for aviso in avisos:
            warning(aviso)
    except Exception as erro:
        log(f"Falha ao atualizar as filas: {type(erro).__name__}: {erro}")
        error(f"Não foi possível atualizar as filas: {erro}")


def renderizar_gerar_romaneios() -> None:
    sincronizar_shipments_finalizados(
        [ARQUIVO_FILA_ROMANEIOS_EMISSAO, ARQUIVO_FILA_ROMANEIOS_ENTRADA]
    )
    st.subheader("Gerar romaneios")
    faltantes = avisar_variaveis_faltantes("gerar_romaneios")
    geracao_disponivel = not bool(faltantes)
    tab_emissao, tab_entrada = st.tabs(["Emissão", "Entrada"])

    with tab_emissao:
        faltantes_drive = avisar_variaveis_faltantes("importar_etiquetas_drive")
        sincronizacao_disponivel = (
            geracao_disponivel and not bool(faltantes_drive)
        )
        _renderizar_fila_romaneios(
            "EMISSAO",
            ARQUIVO_FILA_ROMANEIOS_EMISSAO,
            "Transferência",
            "emissao",
            sincronizacao_disponivel,
            geracao_disponivel,
        )

    with tab_entrada:
        _renderizar_fila_romaneios(
            "ENTRADA",
            ARQUIVO_FILA_ROMANEIOS_ENTRADA,
            "Entrada",
            "entrada",
            False,
            geracao_disponivel,
            mostrar_sincronizacao=False,
        )


def _renderizar_atualizacao_fila_entrada(arquivo_fila: Path) -> None:
    atualizar = st.button(
        "Atualizar fila",
        type="primary",
        key="gerar_romaneios_entrada_atualizar_fila",
        width=LARGURA_BOTAO,
    )
    if not atualizar:
        return

    log, _ = criar_logger_execucao(
        "gerar_romaneios",
        rotulo="atualizar_fila_entrada",
    )
    fila = FilaRomaneiosEmissao(arquivo_fila)
    etiquetas_abertas = fila.carregar_etiquetas_abertas()
    ctes_abertos = list(dict.fromkeys(
        (
            str(linha.get("cte_base")).strip()
            if pd.notna(linha.get("cte_base"))
            and str(linha.get("cte_base")).strip()
            else str(linha.get("cte", "")).strip()
        )
        for linha in etiquetas_abertas.to_dict(orient="records")
        if (
            pd.notna(linha.get("cte_base"))
            and str(linha.get("cte_base")).strip()
        )
        or (pd.notna(linha.get("cte")) and str(linha.get("cte")).strip())
    ))
    if not ctes_abertos:
        log("Não há CTEs abertos na fila Entrada para atualizar.")
        st.info("Não há pedidos abertos para atualizar na fila Entrada.")
        return

    log(
        f"Atualizando UF, Cidade, Destino e Status de {len(ctes_abertos)} "
        "CTE(s) da fila Entrada pela Performance."
    )
    linhas_abertas = etiquetas_abertas.to_dict(orient="records")
    try:
        with tempfile.TemporaryDirectory(
            prefix="botevelog_performance_fila_entrada_"
        ) as diretorio_temporario:
            with st.spinner(
                f"Atualizando dados de {len(ctes_abertos)} CTE(s) pela Performance..."
            ):
                dados_por_cte = consultar_dados_performance_ctes(
                    ctes_abertos,
                    log=log,
                    pasta_temporaria=Path(diretorio_temporario),
                )
                ctes_transferidos = {
                    str(cte).strip()
                    for cte, dados in dados_por_cte.items()
                    if _normalizar_rota(dados.get("Status", ""))
                    == "TRANSFERENCIA"
                }
                shipments_transferidos = set()
                for linha in linhas_abertas:
                    status_atual = _normalizar_rota(linha.get("status", ""))
                    cte_base = linha.get("cte_base", "")
                    cte = linha.get("cte", "")
                    if pd.isna(cte_base) or not str(cte_base).strip():
                        cte_base = cte
                    shipment_id = str(linha.get("shipment_id", "")).strip()
                    if (
                        status_atual in {"ENTRADA", "TRANSFERENCIA"}
                        and str(cte_base).strip() in ctes_transferidos
                        and shipment_id
                    ):
                        shipments_transferidos.add(shipment_id)
                etiquetas_atualizadas = fila.atualizar_dados_performance_abertas(
                    dados_por_cte
                )
    except Exception as erro:
        log(
            f"Falha ao atualizar a fila Entrada pela Performance: "
            f"{type(erro).__name__}: {erro}"
        )
        error(f"Não foi possível atualizar a fila Entrada: {erro}")
        return

    shipments_finalizados = 0
    etiquetas_finalizadas = 0
    if shipments_transferidos:
        # Finaliza primeiro no banco local. Se a sincronização compartilhada
        # falhar, a próxima abertura da fila migra esse estado para o registro
        # global antes de importar ou atualizar pedidos.
        etiquetas_finalizadas = fila.finalizar_shipments(shipments_transferidos)
        registrar_shipments_finalizados(shipments_transferidos)
        shipments_finalizados = len(shipments_transferidos)
        sincronizar_shipments_finalizados(
            [ARQUIVO_FILA_ROMANEIOS_EMISSAO, ARQUIVO_FILA_ROMANEIOS_ENTRADA]
        )
        log(
            f"{shipments_finalizados} ShipmentId(s) finalizados na automação: "
            "status ENTRADA → TRANSFERENCIA confirmado na Performance. "
            f"{etiquetas_finalizadas} etiqueta(s) foram retiradas das filas abertas."
        )

    etiquetas_com_rota_atualizada = 0
    erro_recalculo_rotas = ""
    try:
        etiquetas_com_rota_atualizada = _recalcular_rotas_fila_aberta(
            fila,
            "ENTRADA",
            log=log,
        )
    except (FileNotFoundError, OSError, ValueError) as erro_rotas:
        erro_recalculo_rotas = (
            "Não foi possível recalcular as rotas pela configuração atual: "
            f"{type(erro_rotas).__name__}: {erro_rotas}"
        )
        log(erro_recalculo_rotas)

    ctes_sem_retorno = [cte for cte in ctes_abertos if cte not in dados_por_cte]
    ctes_sem_status = [
        cte
        for cte, dados in dados_por_cte.items()
        if not str(dados.get("Status", "")).strip()
    ]
    ctes_sem_localidade = [
        cte
        for cte, dados in dados_por_cte.items()
        if not str(dados.get("UF", "")).strip()
        or not str(dados.get("Cidade", "")).strip()
    ]
    ctes_status_entrada = [
        cte
        for cte, dados in dados_por_cte.items()
        if _normalizar_rota(dados.get("Status", "")) == "ENTRADA"
    ]
    ctes_outro_status = [
        cte
        for cte, dados in dados_por_cte.items()
        if _normalizar_rota(dados.get("Status", ""))
        not in {"", "ENTRADA", "TRANSFERENCIA"}
    ]
    status_fora_da_fila = sorted(
        {
            str(dados.get("Status", "")).strip()
            for dados in dados_por_cte.values()
            if _normalizar_rota(dados.get("Status", ""))
            not in {"", "ENTRADA", "TRANSFERENCIA"}
        },
        key=_normalizar_rota,
    )
    resumo_status_fila = (
        f"{len(ctes_status_entrada)} CTE(s) com status ENTRADA permanecem na aba; "
        f"{len(ctes_outro_status)} com outro status ficam ocultos"
        + (
            f" ({', '.join(status_fora_da_fila)})"
            if status_fora_da_fila
            else ""
        )
        + "."
    )
    log(
        f"Fila Entrada atualizada: {len(dados_por_cte)}/{len(ctes_abertos)} "
        f"CTE(s) retornaram dados; {etiquetas_atualizadas} etiqueta(s) "
        "tiveram dados alterados no SQLite. "
        f"{resumo_status_fila}"
    )
    if ctes_sem_retorno or ctes_sem_status or ctes_sem_localidade:
        warning(
            "A Performance não retornou todos os dados da fila. "
            f"Sem correspondência: {len(ctes_sem_retorno)} CTE(s); "
            f"sem Status: {len(ctes_sem_status)}; "
            f"sem UF ou Cidade: {len(ctes_sem_localidade)}. "
            "Os valores anteriores foram mantidos quando não houve retorno. "
            f"{resumo_status_fila} "
            f"Finalizados por transferência: {shipments_finalizados} pedido(s). "
            f"Rotas revisadas; {etiquetas_com_rota_atualizada} etiqueta(s) "
            "tiveram rota/triagem atualizadas."
        )
    else:
        success(
            f"Performance consultada para {len(ctes_abertos)} CTE(s); "
            f"{etiquetas_atualizadas} etiqueta(s) tiveram dados alterados. "
            f"{resumo_status_fila} "
            f"Finalizados por transferência: {shipments_finalizados} pedido(s). "
            f"Rotas revisadas; {etiquetas_com_rota_atualizada} etiqueta(s) "
            "tiveram rota/triagem atualizadas."
        )
    if erro_recalculo_rotas:
        warning(erro_recalculo_rotas)


def _renderizar_fila_romaneios(
    status_fila: str,
    arquivo_fila: Path,
    termo_nome: str,
    sufixo_chave: str,
    sincronizacao_disponivel: bool,
    geracao_disponivel: bool,
    *,
    mostrar_sincronizacao: bool = True,
) -> None:
    chave_resultado = f"{CHAVE_RESULTADO}_{sufixo_chave}"
    chave_widget = f"gerar_romaneios_{sufixo_chave}"

    if mostrar_sincronizacao:
        _renderizar_atualizacao_fila(
            status_fila,
            arquivo_fila,
            termo_nome,
            sufixo_chave,
            sincronizacao_disponivel,
        )
    else:
        _renderizar_atualizacao_fila_entrada(arquivo_fila)
        st.caption(
            "Esta fila é preenchida automaticamente após a entrada dos pedidos."
        )
    fila = FilaRomaneiosEmissao(arquivo_fila)
    tabela = _montar_tabela_fila_persistida(fila)
    if not tabela.empty and "Status" in tabela.columns:
        tabela = tabela.loc[
            tabela["Status"].fillna("").map(
                lambda valor: _normalizar_rota(valor) == status_fila
            )
        ].copy()
    if not tabela.empty:
        try:
            tabela_recalculada = recalcular_rotas_fila_emissao(tabela)
            etiquetas_atualizadas = fila.atualizar_rotas_abertas(tabela_recalculada)
            if etiquetas_atualizadas:
                tabela = _montar_tabela_fila_persistida(fila)
                if not tabela.empty and "Status" in tabela.columns:
                    tabela = tabela.loc[
                        tabela["Status"].fillna("").map(
                            lambda valor: _normalizar_rota(valor) == status_fila
                        )
                    ].copy()
                st.caption(
                    f"Rotas atualizadas pela configuração atual para "
                    f"{etiquetas_atualizadas} etiqueta(s) da fila aberta."
                )
        except (FileNotFoundError, OSError, ValueError) as erro:
            warning(
                "Não foi possível recalcular as rotas das filas abertas; "
                f"os dados salvos foram mantidos. Detalhe: {erro}"
            )

    etiquetas_conflito = fila.carregar_etiquetas_conflito()
    if not etiquetas_conflito.empty and "Status" in etiquetas_conflito.columns:
        etiquetas_conflito = etiquetas_conflito.loc[
            etiquetas_conflito["Status"].fillna("").map(
                lambda valor: _normalizar_rota(valor) == status_fila
            )
        ].copy()
    tabela_status_aberto = tabela.loc[
        tabela["Status"].fillna("").map(
            lambda valor: _normalizar_rota(valor) == status_fila
        )
    ]
    if status_fila == "ENTRADA":
        df_ctes_volume = _montar_df_ctes_volume_fila(tabela_status_aberto)
        if not df_ctes_volume.empty:
            st.markdown("#### CTEs da fila Entrada")
            st.dataframe(df_ctes_volume, hide_index=True, width="stretch")
            csv_ctes_volume = df_ctes_volume.to_csv(
                index=False,
                sep=";",
            ).encode("utf-8-sig")
            st.download_button(
                "Baixar CSV dos CTEs da fila",
                data=csv_ctes_volume,
                file_name=f"ctes_fila_entrada_{datetime.now():%Y%m%d_%H%M%S}.csv",
                mime="text/csv",
                key=f"{chave_widget}_baixar_ctes_volume",
                width=LARGURA_BOTAO,
            )
    ruas_disponiveis = sorted(
        {
            str(rua).strip()
            for rua in tabela_status_aberto["Rua"].fillna("").astype(str)
            if rua.strip()
        },
        key=_normalizar_rota,
    )
    opcao_conflitos = "Etiquetas em mais de uma rua"
    if status_fila == "ENTRADA":
        rua_selecionada_filtro = ""
    else:
        opcoes_filtro = list(ruas_disponiveis)
        if not etiquetas_conflito.empty:
            opcoes_filtro.append(opcao_conflitos)
        if not opcoes_filtro:
            opcoes_filtro = ["Nenhuma rua disponível"]
        coluna_filtro_rua, _ = st.columns([1, 5])
        with coluna_filtro_rua:
            rua_selecionada_filtro = st.selectbox(
                "Filtrar por rua",
                opcoes_filtro,
                key=f"{chave_widget}_filtro_rua",
            )
    if status_fila != "ENTRADA" and rua_selecionada_filtro == opcao_conflitos:
        tabela = tabela.iloc[0:0].copy()
    elif status_fila == "ENTRADA":
        tabela = tabela_status_aberto.copy()
    else:
        tabela = tabela.loc[
            tabela["Rua"].fillna("").astype(str) == rua_selecionada_filtro
        ].copy()

    if status_fila == "ENTRADA" and not etiquetas_conflito.empty:
        st.markdown("#### Etiquetas isoladas por triagem")
        st.dataframe(
            etiquetas_conflito,
            hide_index=True,
            width="stretch",
        )

    if status_fila != "ENTRADA" and rua_selecionada_filtro == opcao_conflitos:
        pedidos_conflito = etiquetas_conflito["ShipmentId"].nunique()
        warning(
            f"{etiquetas_conflito['Etiqueta'].nunique()} etiqueta(s) estão "
            "isoladas por aparecerem em mais de uma rua/triagem; "
            f"{pedidos_conflito} pedido(s) completo(s) foram retirados das filas "
            "abertas para revisão manual."
        )

        def valores_unicos(valores: pd.Series) -> str:
            unicos = dict.fromkeys(
                str(valor).strip()
                for valor in valores
                if pd.notna(valor) and str(valor).strip()
            )
            return ", ".join(unicos)

        tabela_conflitos = (
            etiquetas_conflito.groupby(
                ["Etiqueta", "ShipmentId"],
                as_index=False,
                sort=True,
            )
            .agg(
                **{
                    coluna: (coluna, valores_unicos)
                    for coluna in (
                        "Rua",
                        "Triagem",
                        "Palete de origem",
                        "Rota",
                        "Destino",
                        "Detectado em",
                    )
                    if coluna in etiquetas_conflito.columns
                }
            )
            .rename(columns={"Rua": "Ruas"})
        )
        st.dataframe(
            tabela_conflitos,
            hide_index=True,
            width="stretch",
        )
        shipment_ids_conflito = sorted(
            etiquetas_conflito["ShipmentId"]
            .dropna()
            .astype(str)
            .str.strip()
            .loc[lambda valores: valores.ne("")]
            .unique()
        )
        if ruas_disponiveis and shipment_ids_conflito:
            coluna_controles_conflito, _ = st.columns([1, 5])
            with coluna_controles_conflito:
                shipment_selecionado = st.selectbox(
                    "Selecione um pedido (ShipmentId)",
                    shipment_ids_conflito,
                    key=f"{chave_widget}_shipment_conflito_selecionado",
                )
                ocorrencias_pedido = etiquetas_conflito.loc[
                    etiquetas_conflito["ShipmentId"].astype(str).str.strip()
                    == shipment_selecionado
                ]
                ruas_deste_pedido = sorted(
                    {
                        str(rua).strip()
                        for rua in ocorrencias_pedido["Rua"].dropna()
                        if str(rua).strip()
                    },
                    key=_normalizar_rota,
                )
                if ruas_deste_pedido:
                    rua_destino_conflitos = st.radio(
                        "Selecione a rua correta deste pedido",
                        ruas_deste_pedido,
                        index=None,
                        horizontal=True,
                        key=f"{chave_widget}_rua_conflito_{shipment_selecionado}",
                    )
                    mover_conflito = st.button(
                        "Confirmar rua deste pedido",
                        type="primary",
                        disabled=not rua_destino_conflitos,
                        key=f"{chave_widget}_confirmar_rua_{shipment_selecionado}",
                    )
                else:
                    rua_destino_conflitos = ""
                    mover_conflito = False
            if mover_conflito:
                try:
                    resultado_movimentacao = fila.resolver_conflitos_para_rua(
                        rua_destino_conflitos,
                        [shipment_selecionado],
                    )
                except Exception as erro:
                    error(f"Não foi possível mover os pedidos isolados: {erro}")
                else:
                    success(
                        f"{resultado_movimentacao['etiquetas']} etiqueta(s) de "
                        f"{resultado_movimentacao['pedidos']} pedido(s) foram "
                        f"colocadas em {rua_destino_conflitos} e voltaram para a fila."
                    )
                    st.rerun()
        return

    etiquetas_ativas = "\n".join(
        tabela.get("Etiquetas do pedido", pd.Series(dtype=str)).astype(str)
    )
    identificador = sha256(etiquetas_ativas.encode("utf-8")).hexdigest()
    resultado_atual = st.session_state.get(chave_resultado)
    if not isinstance(resultado_atual, dict):
        resultado_atual = {}
        st.session_state[chave_resultado] = resultado_atual
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
        "CTEs para romaneio": st.column_config.TextColumn(
            "CTEs para romaneio", width="large"
        ),
        "UF": st.column_config.TextColumn("UF"),
        "Cidade": st.column_config.TextColumn("Cidade"),
        "Destino": st.column_config.TextColumn("Destino"),
        "Triagem": st.column_config.TextColumn("Triagem", width="large"),
        "Rota": st.column_config.TextColumn("Rota"),
        "Status": st.column_config.TextColumn("Status"),
    }
    tabela_dashboard = tabela.loc[
        tabela["Status"].fillna("").map(
            lambda valor: _normalizar_rota(valor) == status_fila
        )
    ].copy()
    if status_fila == "ENTRADA":
        tabela_dashboard["_CTE base"] = tabela_dashboard["CTE"].map(
            _normalizar_cte_base_volume
        )
        tabela_dashboard["_Triagem para resumo"] = tabela_dashboard[
            "Triagem"
        ].fillna("").astype(str)
        tabela_dashboard["_Rota para resumo"] = tabela_dashboard[
            "Rota"
        ].fillna("").astype(str)
        eh_teca_matriz = tabela_dashboard["Rota"].fillna("").map(
            lambda valor: _normalizar_rota(valor) == "TC TECA MATRIZ"
        )
        tabela_dashboard.loc[eh_teca_matriz, "_Triagem para resumo"] = (
            "__FILA_TC_TECA_MATRIZ__"
        )
        tabela_dashboard.loc[eh_teca_matriz, "_Rota para resumo"] = (
            "TC TECA MATRIZ"
        )
        tabela_dashboard["_Volumes para romaneio"] = tabela_dashboard[
            "CTEs para romaneio"
        ].map(
            lambda valor: len(
                {
                    cte.strip()
                    for cte in str(valor).splitlines()
                    if cte.strip()
                }
            )
        )
        resumo_abertos = (
            tabela_dashboard.groupby(
                ["_Triagem para resumo", "_Rota para resumo"], dropna=False
            )
            .agg(
                Triagem=(
                    "Triagem",
                    lambda valores: ", ".join(
                        dict.fromkeys(
                            str(valor).strip()
                            for valor in valores
                            if str(valor).strip()
                        )
                    ),
                ),
                Rota=("_Rota para resumo", "first"),
                Pedidos=("ShipmentId", "nunique"),
                CTEs=("_CTE base", "nunique"),
                Volumes=("_Volumes para romaneio", "sum"),
            )
            .reset_index()
            .drop(columns=["_Triagem para resumo", "_Rota para resumo"])
            [["Triagem", "Rota", "Pedidos", "CTEs", "Volumes"]]
            if not tabela_dashboard.empty
            else pd.DataFrame(columns=["Triagem", "Rota", "Pedidos", "CTEs", "Volumes"])
        )
    else:
        colunas_resumo = ["Rua", "Triagem", "Rota"]
        resumo_abertos = (
            tabela_dashboard.groupby(colunas_resumo, dropna=False)
            .agg(Pedidos=("ShipmentId", "nunique"))
            .reset_index()
            if not tabela_dashboard.empty
            else pd.DataFrame(columns=[*colunas_resumo, "Pedidos"])
        )
    if not resumo_abertos.empty:
        st.markdown(
            "#### Pedidos disponíveis por triagem"
            if status_fila == "ENTRADA"
            else "#### Resumo das filas abertas"
        )
        st.dataframe(resumo_abertos, hide_index=True, width="stretch")
    divergencias = tabela[tabela.apply(_volume_divergente, axis=1)]
    if not divergencias.empty:
        warning(
            f"{len(divergencias)} pedido(s) têm divergência entre Volume e "
            "etiquetas. Confirme os pedidos que devem entrar na fila de envio."
        )
        with st.form(
            key=f"{chave_widget}_confirmar_{identificador[:16]}"
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
                        f"{chave_widget}_confirmar_{identificador[:16]}_"
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
            st.session_state[chave_resultado] = resultado_atual
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

    status_alvo = tabela_envio["Status"].fillna("").map(
        lambda valor: _normalizar_rota(valor) == status_fila
    )

    rota_original = tabela["Rota"].fillna("").astype(str).str.strip()
    sem_rota_ou_ambigua = tabela.loc[
        rota_original.isin({"", "Sem rota", "Rota ambígua"})
    ].copy()
    grupos, grupos_ignorados = mapear_rotas_para_grupos(
        tabela_envio.loc[status_alvo],
        separar_por_rua_triagem=status_fila != "ENTRADA",
        separar_por_triagem=status_fila == "ENTRADA",
        agrupar_triagens_por_rota=(
            {"TC TECA MATRIZ"} if status_fila == "ENTRADA" else set()
        ),
    )
    codigos_preferenciais = carregar_codigos_preferenciais_unidades_emissao()
    grupos_validos, destinos_pendentes = associar_destinos_fraction(
        grupos,
        carregar_destinos_fraction(),
        codigos_preferenciais=codigos_preferenciais,
    )

    rotas_prontas = {grupo["ROTA"] for grupo in grupos_validos}

    if not sem_rota_ou_ambigua.empty:
        st.markdown("#### Sem rota ou com mais de uma rota")
        tabela_sem_rota = _preparar_df_exibicao_romaneios(
            sem_rota_ou_ambigua
        )
        regras_rotas = carregar_regras_triagem_rotas(ARQUIVO_ROTAS_ROMANEIOS)
        for indice, linha in tabela_sem_rota.iterrows():
            if _normalizar_rota(linha.get("Rota", "")) != "ROTA AMBIGUA":
                continue
            triagens = listar_triagens_por_localidade(
                regras_rotas,
                uf=linha.get("UF", ""),
                cidade=linha.get("Cidade", ""),
                destino=linha.get("Destino", ""),
                triagem=linha.get("Triagem", ""),
            )
            if triagens:
                tabela_sem_rota.at[indice, "Triagem"] = " • ".join(triagens)
        if status_fila == "ENTRADA":
            tabela_sem_rota = tabela_sem_rota.drop(
                columns=["Rua", "Palete de origem"],
                errors="ignore",
            )
        st.dataframe(
            tabela_sem_rota,
            hide_index=True,
            width="stretch",
            column_config=configuracao_colunas_fila,
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
        motivo_ignorado = (
            "faltou unidade, triagem ou CTE válido."
            if status_fila == "ENTRADA"
            else "faltou unidade, Rua, triagem ou CTE válido."
        )
        ctes_ignorados = _extrair_ctes_ignorados(grupos_ignorados)
        quantidade_ctes, quantidade_volumes = _resumir_ctes_e_volumes(
            ctes_ignorados
        )
        if not ctes_ignorados:
            quantidade_ctes = len(grupos_ignorados)
            quantidade_volumes = len(grupos_ignorados)
        warning(
            f"{quantidade_ctes} CTE(s), totalizando {quantidade_volumes} "
            f"volume(s), não entraram em romaneios porque {motivo_ignorado}"
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
                    "Triagem": st.column_config.TextColumn("Triagem", width="small"),
                    "Unidade": st.column_config.TextColumn("Unidade"),
                    "UF": st.column_config.TextColumn("UF"),
                    "Cidade": st.column_config.TextColumn("Cidade"),
                    "Destino": st.column_config.TextColumn("Destino"),
                    "Código preferencial": st.column_config.TextColumn(
                        "Código preferencial"
                    ),
                },
            )
    selecao_grupo = None
    tabela_grupo = tabela_envio.iloc[0:0].copy()
    if grupos_validos:
        grupo_por_opcao: dict[str, dict] = {}
        for grupo in grupos_validos:
            if status_fila == "ENTRADA":
                chave_grupo = (
                    str(grupo.get("TRIAGEM", "")),
                    str(grupo.get("ROTA", "")),
                )
            else:
                chave_grupo = (
                    str(grupo.get("RUA", "")),
                    str(grupo.get("TRIAGEM", "")),
                    str(grupo.get("ROTA", "")),
                )
            quantidade_ctes, quantidade_volumes = _resumir_ctes_e_volumes(
                grupo["LISTA_CTES"]
            )
            if status_fila == "ENTRADA":
                opcao = (
                    f"Triagem {chave_grupo[0]} · {chave_grupo[1]} "
                    f"({quantidade_ctes} CTE(s), {quantidade_volumes} volume(s))"
                )
            else:
                opcao = (
                    f"{chave_grupo[0]} · {chave_grupo[1]} · {chave_grupo[2]} "
                    f"({quantidade_ctes} CTE(s), {quantidade_volumes} volume(s))"
                )
            grupo_por_opcao[opcao] = grupo
        coluna_seletor_fila, _ = st.columns([1, 1])
        with coluna_seletor_fila:
            opcao_selecionada = st.selectbox(
                "Fila por triagem"
                if status_fila == "ENTRADA"
                else "Fila de romaneio",
                list(grupo_por_opcao),
                key=f"{chave_widget}_grupo_selecionado",
            )
        selecao_grupo = grupo_por_opcao[opcao_selecionada]
        if status_fila == "ENTRADA":
            triagens_selecionadas = set(
                _normalizar_rota(triagem)
                for triagem in (
                    selecao_grupo.get("TRIAGENS")
                    or [str(selecao_grupo.get("TRIAGEM", ""))]
                )
            )
            rota_selecionada = _normalizar_rota(
                selecao_grupo.get("ROTA", "")
            )
        else:
            chave_selecionada = (
                str(selecao_grupo.get("RUA", "")),
                str(selecao_grupo.get("TRIAGEM", "")),
                str(selecao_grupo.get("ROTA", "")),
            )
        def pertence_ao_grupo(linha: pd.Series) -> bool:
            if status_fila == "ENTRADA":
                return (
                    _normalizar_rota(linha.get("Triagem", ""))
                    in triagens_selecionadas
                    and _normalizar_rota(linha.get("Rota", ""))
                    == rota_selecionada
                )
            return (
                str(linha.get("Rua", "")),
                str(linha.get("Triagem", "")),
                str(linha.get("Rota", "")),
            ) == chave_selecionada

        tabela_grupo = tabela_envio.loc[
            tabela_envio.apply(pertence_ao_grupo, axis=1)
        ].copy()

        if status_fila == "ENTRADA" and not tabela_grupo.empty:
            st.markdown("#### Pedidos da triagem selecionada")
            st.dataframe(
                _preparar_df_exibicao_romaneios(tabela_grupo),
                hide_index=True,
                width="stretch",
                column_config=configuracao_colunas_fila,
            )

    enviar = st.button(
        "Gerar romaneio",
        type="primary",
        key=f"{chave_widget}_enviar",
        width=LARGURA_BOTAO,
        disabled=(selecao_grupo is None or not geracao_disponivel),
    )

    if enviar and selecao_grupo is not None:
        log, _ = criar_logger_execucao(
            "gerar_romaneios",
            rotulo=f"processar_{sufixo_chave}",
        )
        try:
            rua_selecionada = str(selecao_grupo.get("RUA") or "")
            triagem_selecionada = str(selecao_grupo.get("TRIAGEM") or "")
            rota_selecionada = str(selecao_grupo.get("ROTA") or "")
            coluna_ctes_romaneio = (
                "CTEs para romaneio"
                if status_fila == "ENTRADA"
                else "CTEs para dar entrada"
            )
            ctes_selecionados = {
                str(cte).strip()
                for cte in selecao_grupo.get("LISTA_CTES", [])
                if str(cte).strip()
            }
            tabela_grupo = tabela_grupo.loc[
                tabela_grupo[coluna_ctes_romaneio].map(
                    lambda valor: bool(
                        ctes_selecionados.intersection(
                            item.strip()
                            for item in str(valor).splitlines()
                            if item.strip()
                        )
                    )
                )
            ].copy()
            if status_fila == "ENTRADA":
                log(
                    f"Funcionário selecionou triagem {triagem_selecionada} / "
                    f"{rota_selecionada}; {len(ctes_selecionados)} CTE(s) "
                    "serão enviados."
                )
            else:
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
                    triagens=selecao_grupo.get("TRIAGENS"),
                )
                shipments_grupo = list(dict.fromkeys(
                    str(shipment_id).strip()
                    for shipment_id in tabela_grupo["ShipmentId"].tolist()
                    if pd.notna(shipment_id) and str(shipment_id).strip()
                ))
                novos_shipments_finalizados = registrar_shipments_finalizados(
                    shipments_grupo
                )
                log(
                    f"{len(novos_shipments_finalizados)} ShipmentId(s) registrados "
                    "como finalizados globalmente para impedir reentrada em filas."
                )
                registrar_etiquetas_romaneio(
                    etiquetas_grupo,
                    contexto=(
                        f"romaneio gerado — "
                        + (
                            f"triagem {triagem_selecionada}"
                            if status_fila == "ENTRADA"
                            else f"{rua_selecionada} / {triagem_selecionada}"
                        )
                        + f", rota '{rota_selecionada}', "
                        f"PDF '{caminho_pdf}'"
                    ),
                    status_fila=status_fila,
                    log=log,
                )
                identificacao_fila = (
                    f"triagem {triagem_selecionada}"
                    if status_fila == "ENTRADA"
                    else f"{rua_selecionada} / {triagem_selecionada}"
                )
                log(
                    f"Fila fechada no SQLite: {quantidade_fechada} etiqueta(s) "
                    f"em {identificacao_fila}."
                )

            pasta_execucao_pdf = RESULTADOS_ROMANEIOS / (
                f"Execucao_{status_fila.title()}_"
                f"{datetime.now():%Y-%m-%d_%H-%M-%S-%f}"
            )
            tabela_execucao = tabela_grupo
            if status_fila == "ENTRADA" and selecao_grupo.get("TRIAGENS"):
                tabela_execucao = tabela_grupo.copy()
                tabela_execucao["Triagem"] = triagem_selecionada
            with st.spinner(
                (
                    f"Gerando romaneio da triagem {triagem_selecionada}..."
                    if status_fila == "ENTRADA"
                    else f"Gerando romaneio de {rua_selecionada} / "
                    f"{triagem_selecionada}..."
                )
            ):
                processamento = executar_manifestos_romaneios(
                    (
                        f"Fila Entrada — triagem {triagem_selecionada}"
                        if status_fila == "ENTRADA"
                        else f"Fila {rua_selecionada} / {triagem_selecionada}"
                    ),
                    tabela_previa=tabela_execucao,
                    codigos_preferenciais=codigos_preferenciais,
                    separar_por_rua_triagem=status_fila != "ENTRADA",
                    separar_por_triagem=status_fila == "ENTRADA",
                    pasta_destino_pdf=pasta_execucao_pdf,
                    ao_concluir_grupo=registrar_grupo_concluido,
                    log=log,
                )
            if status_fila == "ENTRADA":
                mensagem = (
                    f"Romaneio concluído para triagem {triagem_selecionada} / "
                    f"{rota_selecionada}: {processamento['ctes_manifesto']} CTE(s), "
                    f"{len(processamento['arquivos_pdf'])} PDF(s). "
                    "Os pedidos desta triagem foram retirados da fila aberta."
                )
            else:
                mensagem = (
                    f"Romaneio concluído para {rua_selecionada} / "
                    f"{triagem_selecionada}: "
                    f"{processamento['ctes_manifesto']} CTE(s), "
                    f"{len(processamento['arquivos_pdf'])} PDF(s). "
                    "A fila foi encerrada e as próximas etiquetas abrirão um novo lote."
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
            st.session_state[chave_resultado] = resultado_atual
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
        if resultado_atual.get("arquivo_lote_impressao"):
            st.caption(
                "PDF combinado salvo em: "
                f"{resultado_atual['arquivo_lote_impressao']}"
            )
        with st.expander("PDFs salvos", expanded=False):
            st.code("\n".join(arquivos_pdf), language=None, wrap_lines=True)

        if resultado_atual.get("impressao_solicitada"):
            success("O lote foi enviado pelo SumatraPDF à fila da impressora.")
        else:
            with st.form(
                key=f"{chave_widget}_imprimir_{identificador[:16]}"
            ):
                st.info(
                    "Os PDFs individuais continuarão salvos. O lote combinado será "
                    "enviado à impressora padrão somente após sua confirmação."
                )
                confirmar_impressao = st.checkbox(
                    f"Confirmo imprimir os {len(arquivos_pdf)} romaneio(s) do lote.",
                    key=(
                        f"{chave_widget}_confirmar_impressao_"
                        f"{identificador[:16]}"
                    ),
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
                        rotulo=f"imprimir_{sufixo_chave}",
                    )
                    try:
                        with st.spinner("Validando e enviando o lote para impressão..."):
                            pasta_lote = Path(
                                resultado_atual.get("pasta_pdf")
                                or RESULTADOS_ROMANEIOS
                            )
                            caminho_lote = preparar_lote_impressao_romaneios(
                                arquivos_pdf,
                                pasta_destino=pasta_lote,
                                log=log,
                            )
                            resultado_atual["arquivo_lote_impressao"] = str(
                                caminho_lote
                            )
                            st.session_state[chave_resultado] = resultado_atual
                            st.info(f"PDF combinado salvo em: {caminho_lote}")
                            enviar_pdf_para_impressora_padrao(
                                caminho_lote,
                                log=log,
                            )
                        resultado_atual["impressao_solicitada"] = True
                        st.session_state[chave_resultado] = resultado_atual
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
