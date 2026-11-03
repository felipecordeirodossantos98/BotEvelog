from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from io import BytesIO
from pathlib import Path, PurePosixPath
import re
import tempfile
import time
import unicodedata
from typing import Callable
from zipfile import BadZipFile, ZipFile

import pandas as pd
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from automacoes.baixar_relatorios_performance import executar_extracao
from automacoes.dar_entrada_pedidos import (
    COLUNAS_RESULTADO,
    PADRAO_ETIQUETA,
    agrupar_etiquetas_importadas,
)
from servicos.fraction import (
    carregar_codigos_preferenciais_unidades,
    carregar_regras_rotas_com_destino_e_triagem,
    consultar_ctes_por_etiquetas_fraction,
)
from utils.config import (
    ARQUIVO_ETIQUETAS_ROMANEIOS_EMISSAO,
    HEADLESS,
    SLOW_MO_MS,
    URL_DRIVE,
)
from utils.estado import RegistroTexto


ALIAS_CTE_RELATORIO = {
    "CTE",
    "CODIGO",
    "CODIGOCTE",
    "REMESSA",
    "CONHECIMENTO",
    "NUMEROCONHECIMENTO",
}
ALIAS_SHIPMENT_RELATORIO = {
    "SHIPMENTID",
    "DOCRECEBEDOR",
    "IDSHIPMENT",
}
ALIAS_DESTINO_RELATORIO = {
    "DESTINO",
    "DESTINACAO",
    "DESTINOFINAL",
}
ALIAS_STATUS_RELATORIO = {
    "STATUS",
    "STATUSPEDIDO",
    "SITUACAO",
    "SITUACAOPEDIDO",
}
ALIAS_UF_RELATORIO = {"UF", "ESTADO"}
ALIAS_CIDADE_RELATORIO = {"CIDADE", "MUNICIPIO"}
NOMES_UF = {
    "ACRE": "AC",
    "ALAGOAS": "AL",
    "AMAPA": "AP",
    "AMAZONAS": "AM",
    "BAHIA": "BA",
    "CEARA": "CE",
    "DISTRITO FEDERAL": "DF",
    "ESPIRITO SANTO": "ES",
    "GOIAS": "GO",
    "MARANHAO": "MA",
    "MATO GROSSO": "MT",
    "MATO GROSSO DO SUL": "MS",
    "MINAS GERAIS": "MG",
    "PARA": "PA",
    "PARAIBA": "PB",
    "PARANA": "PR",
    "PERNAMBUCO": "PE",
    "PIAUI": "PI",
    "RIO DE JANEIRO": "RJ",
    "RIO GRANDE DO NORTE": "RN",
    "RIO GRANDE DO SUL": "RS",
    "RONDONIA": "RO",
    "RORAIMA": "RR",
    "SANTA CATARINA": "SC",
    "SAO PAULO": "SP",
    "SERGIPE": "SE",
    "TOCANTINS": "TO",
}
TIMEOUT_DOWNLOAD_DRIVE_MS = 300_000
MAX_TENTATIVAS_DOWNLOAD_DRIVE = 3
REGISTRO_ETIQUETAS_ROMANEIOS_EMISSAO = RegistroTexto(
    ARQUIVO_ETIQUETAS_ROMANEIOS_EMISSAO
)


def preparar_estado_etiquetas_romaneio() -> Path:
    """Cria a pasta e o arquivo de estado sem alterar etiquetas registradas."""
    ARQUIVO_ETIQUETAS_ROMANEIOS_EMISSAO.parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    ARQUIVO_ETIQUETAS_ROMANEIOS_EMISSAO.touch(exist_ok=True)
    return ARQUIVO_ETIQUETAS_ROMANEIOS_EMISSAO


def registrar_etiquetas_romaneio(
    etiquetas: list[str],
    *,
    contexto: str,
    log: Callable[[str], None] | None = None,
) -> list[str]:
    """Persiste etiquetas de um romaneio confirmado para evitar reenvio."""
    novas = REGISTRO_ETIQUETAS_ROMANEIOS_EMISSAO.adicionar_varios(etiquetas)
    _log_performance_emissao(
        log,
        f"Estado de etiquetas atualizado para {contexto}: "
        f"{len(novas)} nova(s) etiqueta(s) registrada(s), "
        f"{len(etiquetas) - len(novas)} já constavam no estado. "
        f"Arquivo: {ARQUIVO_ETIQUETAS_ROMANEIOS_EMISSAO}.",
    )
    for etiqueta in novas:
        _log_performance_emissao(log, f"Etiqueta persistida: {etiqueta}.")
    return novas


def _filtrar_etiquetas_ja_registradas(
    pedidos: list[dict[str, object]],
    *,
    log: Callable[[str], None] | None = None,
) -> tuple[list[dict[str, object]], int]:
    pedidos_novos: list[dict[str, object]] = []
    total_ignoradas = 0
    _log_performance_emissao(
        log,
        f"Estado local: {len(REGISTRO_ETIQUETAS_ROMANEIOS_EMISSAO)} etiqueta(s) "
        f"já registradas em {ARQUIVO_ETIQUETAS_ROMANEIOS_EMISSAO}.",
    )

    for pedido in pedidos:
        shipment_id = str(pedido["ShipmentId"])
        mapa_paletes = pedido.get("ETIQUETAS_POR_PALETE")
        if not isinstance(mapa_paletes, dict) or not mapa_paletes:
            mapa_paletes = {"Sem palete": pedido["ETIQUETAS"]}

        etiquetas_frescas_por_palete: dict[str, list[str]] = {}
        for palete, etiquetas in mapa_paletes.items():
            frescas = []
            registradas = []
            for etiqueta in etiquetas:
                if REGISTRO_ETIQUETAS_ROMANEIOS_EMISSAO.contem(etiqueta):
                    registradas.append(etiqueta)
                else:
                    frescas.append(etiqueta)
            total_ignoradas += len(registradas)
            if registradas:
                _log_performance_emissao(
                    log,
                    f"Ignorando {len(registradas)} etiqueta(s) já tratada(s) "
                    f"em outra execução: Palete '{palete}', ShipmentId "
                    f"{shipment_id}: {', '.join(registradas)}.",
                )
            if frescas:
                etiquetas_frescas_por_palete[str(palete)] = frescas

        etiquetas_frescas = [
            etiqueta
            for etiquetas in etiquetas_frescas_por_palete.values()
            for etiqueta in etiquetas
        ]
        if not etiquetas_frescas:
            _log_performance_emissao(
                log,
                f"ShipmentId {shipment_id} não tem etiqueta nova; removido "
                "da consulta e da fila desta execução.",
            )
            continue

        pedido_novo = dict(pedido)
        pedido_novo["ETIQUETAS"] = etiquetas_frescas
        pedido_novo["VOLUMES_ETIQUETAS"] = len(etiquetas_frescas)
        pedido_novo["ETIQUETAS_POR_PALETE"] = etiquetas_frescas_por_palete
        pedidos_novos.append(pedido_novo)

    return pedidos_novos, total_ignoradas


def _normalizar_cabecalho(valor: object) -> str:
    texto = unicodedata.normalize("NFKD", str(valor).strip())
    texto = "".join(
        caractere
        for caractere in texto
        if not unicodedata.combining(caractere)
    )
    return "".join(caractere for caractere in texto.upper() if caractere.isalnum())


def _normalizar_rota(valor: object) -> str:
    texto = unicodedata.normalize("NFKD", str(valor).strip())
    texto = "".join(
        caractere
        for caractere in texto
        if not unicodedata.combining(caractere)
    )
    return " ".join(texto.upper().split())


def _normalizar_uf(valor: object) -> str:
    texto = _normalizar_valor(valor)
    if not texto:
        return ""
    uf = _normalizar_rota(texto)
    uf_sem_acento = "".join(
        caractere
        for caractere in unicodedata.normalize("NFKD", uf)
        if not unicodedata.combining(caractere)
    )
    return NOMES_UF.get(uf_sem_acento, uf_sem_acento)


def _normalizar_valor(valor: object) -> str:
    if valor is None or pd.isna(valor):
        return ""
    texto = str(valor).strip()
    if re.fullmatch(r"\d+\.0", texto):
        texto = texto[:-2]
    return " ".join(texto.split())


def _normalizar_shipment_id(valor: object) -> str:
    shipment_id = _normalizar_valor(valor)
    if shipment_id.isdigit() and len(shipment_id) < 14:
        return shipment_id.zfill(14)
    return shipment_id


def carregar_codigos_preferenciais_unidades_emissao() -> dict[str, str]:
    """Retorna o código preferencial da unidade quando o nome não é único."""
    return carregar_codigos_preferenciais_unidades()


def _adicionar_rota(tabela: pd.DataFrame) -> pd.DataFrame:
    (
        rotas_por_uf,
        rotas_por_cidade,
        rotas_por_destino,
        triagens_por_uf,
        triagens_por_cidade,
        triagens_por_destino,
    ) = carregar_regras_rotas_com_destino_e_triagem()
    resultado = tabela.copy()

    def resolver(linha: pd.Series) -> tuple[str, str]:
        uf = _normalizar_uf(linha.get("UF", ""))
        cidade = _normalizar_rota(_normalizar_valor(linha.get("Cidade", "")))
        rota_cidade = rotas_por_cidade.get(cidade, "")
        if rota_cidade:
            return rota_cidade, triagens_por_cidade.get(cidade, "")
        rota_uf = rotas_por_uf.get(uf, "")
        if rota_uf:
            return rota_uf, triagens_por_uf.get(uf, "")
        destino = _normalizar_rota(_normalizar_valor(linha.get("Destino", "")))
        rota_destino = rotas_por_destino.get(destino, "")
        if rota_destino:
            return rota_destino, triagens_por_destino.get(destino, "")
        return "Sem rota", ""

    resolvidos = resultado.apply(resolver, axis=1)
    resultado["Rota"] = resolvidos.map(lambda item: item[0])
    resultado["Triagem"] = resolvidos.map(lambda item: item[1])
    return resultado


def _completar_rotas_por_rua_triagem(
    tabela: pd.DataFrame,
    *,
    log: Callable[[str], None] | None = None,
) -> pd.DataFrame:
    """Completa a unidade somente dentro da mesma rua e triagem literal."""
    resultado = tabela.copy()
    if resultado.empty or "Rua" not in resultado or "Rota" not in resultado:
        return resultado

    rotas_sem_valor = {"", "SEM ROTA"}
    rotas_nao_inferiveis = rotas_sem_valor | {"ROTA AMBIGUA"}
    colunas_grupo = ["Rua"]
    if "Triagem" in resultado:
        colunas_grupo.append("Triagem")
    grupos = resultado.groupby(colunas_grupo, sort=False, dropna=False).groups
    for identificadores, indices in grupos.items():
        if not isinstance(identificadores, tuple):
            identificadores = (identificadores,)
        rua = str(identificadores[0])
        triagem = str(identificadores[1]) if len(identificadores) > 1 else ""
        rotas_conhecidas: dict[str, str] = {}
        for valor in resultado.loc[indices, "Rota"]:
            rota = _normalizar_valor(valor)
            chave = _normalizar_rota(rota)
            if chave and chave not in rotas_nao_inferiveis:
                rotas_conhecidas.setdefault(chave, rota)

        faltantes = resultado.loc[indices, "Rota"].map(
            lambda valor: _normalizar_rota(_normalizar_valor(valor))
            in rotas_sem_valor
        )
        if len(rotas_conhecidas) == 1 and faltantes.any():
            rota = next(iter(rotas_conhecidas.values()))
            indices_faltantes = resultado.loc[indices].index[faltantes]
            resultado.loc[indices_faltantes, "Rota"] = rota
            _log_performance_emissao(
                log,
                f"{rua} / {triagem or 'sem triagem'}: unidade '{rota}' aplicada a "
                f"{len(indices_faltantes)} pedido(s) sem rota, usando os "
                "pedidos da mesma rua e triagem.",
            )
        elif len(rotas_conhecidas) > 1 and faltantes.any():
            _log_performance_emissao(
                log,
                f"{rua} / {triagem or 'sem triagem'} tem mais de uma unidade "
                "conhecida; "
                f"{int(faltantes.sum())} pedido(s) sem rota não foram inferidos. "
                "Unidades encontradas: "
                + ", ".join(rotas_conhecidas.values()),
            )

    return resultado


def recalcular_rotas_fila_emissao(
    tabela: pd.DataFrame,
    *,
    log: Callable[[str], None] | None = None,
) -> pd.DataFrame:
    """Resolve novamente rota e triagem usando a versão atual do JSON de rotas."""
    if tabela.empty:
        return tabela.copy()
    resultado = _adicionar_rota(tabela)
    return _completar_rotas_por_rua_triagem(resultado, log=log)


def _localizar_coluna_normalizada(
    colunas: list[object],
    aliases: set[str],
) -> object | None:
    for coluna in colunas:
        if _normalizar_cabecalho(coluna) in aliases:
            return coluna
    return None


def _registrar_texto_unico(
    mapa: dict[str, str],
    ambiguos: set[str],
    chave: str,
    valor: str,
) -> None:
    if not chave or not valor or chave in ambiguos:
        return
    existente = mapa.get(chave)
    if existente is not None and _normalizar_rota(existente) != _normalizar_rota(valor):
        mapa.pop(chave, None)
        ambiguos.add(chave)
        return
    mapa[chave] = valor


def _ler_destinos_relatorio_performance(
    caminho: str,
) -> tuple[
    dict[str, str],
    dict[str, str],
    dict[str, str],
    dict[str, str],
    dict[str, str],
    dict[str, str],
    dict[str, str],
    dict[str, str],
]:
    """Indexa Destino, Status, UF e Cidade por CTE e ShipmentId."""
    partes: list[pd.DataFrame] = []
    with pd.ExcelFile(caminho, engine="openpyxl") as planilha:
        for nome_aba in planilha.sheet_names:
            primeiras_linhas = pd.read_excel(
                planilha,
                sheet_name=nome_aba,
                header=None,
                nrows=30,
                dtype=object,
            )
            for indice, linha in primeiras_linhas.iterrows():
                cabecalhos = [
                    valor for valor in linha.tolist() if not pd.isna(valor)
                ]
                coluna_destino = _localizar_coluna_normalizada(
                    cabecalhos,
                    ALIAS_DESTINO_RELATORIO,
                )
                coluna_cte = _localizar_coluna_normalizada(
                    cabecalhos,
                    ALIAS_CTE_RELATORIO,
                )
                coluna_shipment = _localizar_coluna_normalizada(
                    cabecalhos,
                    ALIAS_SHIPMENT_RELATORIO,
                )
                if coluna_destino is None or (
                    coluna_cte is None and coluna_shipment is None
                ):
                    continue

                dados = pd.read_excel(
                    planilha,
                    sheet_name=nome_aba,
                    header=int(indice),
                    dtype=object,
                )
                partes.append(dados)
                break

    if not partes:
        raise ValueError(
            "O relatório de Performance gerado não contém colunas de CTE/ShipmentId "
            "e Destino."
        )

    dados = pd.concat(partes, ignore_index=True)
    coluna_destino = _localizar_coluna_normalizada(
        list(dados.columns),
        ALIAS_DESTINO_RELATORIO,
    )
    coluna_cte = _localizar_coluna_normalizada(
        list(dados.columns),
        ALIAS_CTE_RELATORIO,
    )
    coluna_shipment = _localizar_coluna_normalizada(
        list(dados.columns),
        ALIAS_SHIPMENT_RELATORIO,
    )
    coluna_status = _localizar_coluna_normalizada(
        list(dados.columns),
        ALIAS_STATUS_RELATORIO,
    )
    coluna_uf = _localizar_coluna_normalizada(
        list(dados.columns),
        ALIAS_UF_RELATORIO,
    )
    coluna_cidade = _localizar_coluna_normalizada(
        list(dados.columns),
        ALIAS_CIDADE_RELATORIO,
    )

    destinos_por_cte: dict[str, str] = {}
    destinos_por_shipment: dict[str, str] = {}
    status_por_cte: dict[str, str] = {}
    status_por_shipment: dict[str, str] = {}
    uf_por_cte: dict[str, str] = {}
    uf_por_shipment: dict[str, str] = {}
    cidade_por_cte: dict[str, str] = {}
    cidade_por_shipment: dict[str, str] = {}
    ctes_ambiguos: set[str] = set()
    shipments_ambiguos: set[str] = set()
    ctes_status_ambiguos: set[str] = set()
    shipments_status_ambiguos: set[str] = set()
    ctes_uf_ambiguos: set[str] = set()
    shipments_uf_ambiguos: set[str] = set()
    ctes_cidade_ambiguos: set[str] = set()
    shipments_cidade_ambiguos: set[str] = set()
    for _, linha in dados.iterrows():
        destino = _normalizar_valor(linha[coluna_destino])
        cte = _normalizar_valor(linha[coluna_cte]) if coluna_cte is not None else ""
        status = (
            _normalizar_valor(linha[coluna_status])
            if coluna_status is not None
            else ""
        )
        uf = (
            _normalizar_uf(linha[coluna_uf])
            if coluna_uf is not None
            else ""
        )
        cidade = (
            _normalizar_valor(linha[coluna_cidade])
            if coluna_cidade is not None
            else ""
        )
        shipment_id = (
            _normalizar_shipment_id(linha[coluna_shipment])
            if coluna_shipment is not None
            else ""
        )
        _registrar_texto_unico(
            destinos_por_cte,
            ctes_ambiguos,
            cte,
            destino,
        )
        _registrar_texto_unico(
            destinos_por_shipment,
            shipments_ambiguos,
            shipment_id,
            destino,
        )
        _registrar_texto_unico(
            status_por_cte,
            ctes_status_ambiguos,
            cte,
            status,
        )
        _registrar_texto_unico(
            status_por_shipment,
            shipments_status_ambiguos,
            shipment_id,
            status,
        )
        _registrar_texto_unico(
            uf_por_cte,
            ctes_uf_ambiguos,
            cte,
            uf,
        )
        _registrar_texto_unico(
            uf_por_shipment,
            shipments_uf_ambiguos,
            shipment_id,
            uf,
        )
        _registrar_texto_unico(
            cidade_por_cte,
            ctes_cidade_ambiguos,
            cte,
            cidade,
        )
        _registrar_texto_unico(
            cidade_por_shipment,
            shipments_cidade_ambiguos,
            shipment_id,
            cidade,
        )

    return (
        destinos_por_cte,
        destinos_por_shipment,
        status_por_cte,
        status_por_shipment,
        uf_por_cte,
        uf_por_shipment,
        cidade_por_cte,
        cidade_por_shipment,
    )


def _consultar_dados_performance_ctes(
    ctes: list[str],
    *,
    log: Callable[[str], None] | None,
    pasta_temporaria: Path | None = None,
) -> dict[str, dict[str, str]]:
    """Consulta Destino/Status dos CTEs na Performance, em lotes de até 1.000."""
    ctes_unicos = list(dict.fromkeys(
        _normalizar_valor(cte) for cte in ctes if _normalizar_valor(cte)
    ))
    if not ctes_unicos:
        return {}

    planilha_ctes = BytesIO()
    pd.DataFrame({"REMESSA": ctes_unicos}).to_excel(
        planilha_ctes, index=False, engine="openpyxl"
    )
    planilha_ctes.seek(0)
    registrar = log if log is not None else (lambda _mensagem: None)
    quantidade_lotes = (len(ctes_unicos) + 999) // 1000
    registrar(
        f"Consultando {len(ctes_unicos)} CTE(s) na Performance em "
        f"{quantidade_lotes} lote(s) de até 1.000."
    )

    resultado = executar_extracao(
        planilha_ctes,
        log=registrar,
        pasta_destino=pasta_temporaria,
    )
    caminho_relatorio = resultado.get("arquivo")
    if not caminho_relatorio:
        raise RuntimeError(
            "A consulta de Performance por CTE não gerou uma planilha consolidada."
        )
    registrar(f"Performance por CTE salva temporariamente em {caminho_relatorio}.")
    (
        destinos_por_cte,
        _destinos_por_shipment,
        status_por_cte,
        _status_por_shipment,
        uf_por_cte,
        _uf_por_shipment,
        cidade_por_cte,
        _cidade_por_shipment,
    ) = _ler_destinos_relatorio_performance(str(caminho_relatorio))

    dados = {}
    for cte in ctes_unicos:
        destino = destinos_por_cte.get(cte, "")
        status = status_por_cte.get(cte, "")
        uf = uf_por_cte.get(cte, "")
        cidade = cidade_por_cte.get(cte, "")
        if destino or status or uf or cidade:
            dados[cte] = {
                "Destino": destino,
                "Status": status,
                "UF": uf,
                "Cidade": cidade,
            }
    registrar(
        f"Performance retornou Destino para "
        f"{sum(bool(item.get('Destino')) for item in dados.values())}/"
        f"{len(ctes_unicos)} CTE(s) e Status para "
        f"{sum(bool(item.get('Status')) for item in dados.values())}/"
        f"{len(ctes_unicos)} CTE(s)."
    )
    return dados


def consultar_status_performance_ctes(
    ctes: list[str],
    *,
    log: Callable[[str], None] | None = None,
    pasta_temporaria: Path | None = None,
) -> dict[str, str]:
    """Consulta o status atual de CTEs na Performance do Fraction."""
    dados = _consultar_dados_performance_ctes(
        ctes,
        log=log,
        pasta_temporaria=pasta_temporaria,
    )
    return {
        cte: status
        for cte, item in dados.items()
        if (status := _normalizar_valor(item.get("Status", "")))
    }


def montar_fila_emissao(
    conteudo_etiquetas: bytes,
    *,
    nome_arquivo_etiquetas: str = "etiquetas.txt",
    log: Callable[[str], None] | None = None,
    pasta_temporaria: Path | None = None,
) -> dict[str, object]:
    """Cruza etiquetas no Fraction, seus CTEs e a Performance por CTE."""
    preparar_estado_etiquetas_romaneio()
    _log_performance_emissao(
        log,
        f"Arquivo de estado de etiquetas: {ARQUIVO_ETIQUETAS_ROMANEIOS_EMISSAO}.",
    )
    pedidos = agrupar_etiquetas_importadas(
        conteudo_etiquetas,
        nome_arquivo_etiquetas,
        log=log,
    )
    total_etiquetas_importadas = sum(len(pedido["ETIQUETAS"]) for pedido in pedidos)
    _log_performance_emissao(
        log,
        f"TXT lido: {len(pedidos)} ShipmentId(s) e "
        f"{total_etiquetas_importadas} etiqueta(s).",
    )
    for pedido in pedidos:
        shipment_id = str(pedido["ShipmentId"])
        for palete, etiquetas_palete in pedido.get("ETIQUETAS_POR_PALETE", {}).items():
            rua = str(pedido.get("RUA_POR_PALETE", {}).get(palete, "")).strip()
            if not rua:
                raise ValueError(
                    f"Não identifiquei a rua do arquivo que contém o palete "
                    f"'{palete}'. Inclua 'Rua 1' (ou o número correspondente) "
                    "no nome/caminho do TXT dentro do ZIP ou no cabeçalho do TXT."
                )
            _log_performance_emissao(
                log,
                f"TXT lido — {rua}, palete '{palete}', ShipmentId {shipment_id}, "
                f"{len(etiquetas_palete)} etiqueta(s): {', '.join(etiquetas_palete)}.",
            )

    pedidos, etiquetas_ja_registradas = _filtrar_etiquetas_ja_registradas(
        pedidos,
        log=log,
    )
    etiquetas_novas = list(dict.fromkeys(
        etiqueta
        for pedido in pedidos
        for etiquetas_palete in pedido.get("ETIQUETAS_POR_PALETE", {}).values()
        for etiqueta in etiquetas_palete
    ))
    _log_performance_emissao(
        log,
        f"Após deduplicação local: {len(etiquetas_novas)} etiqueta(s) nova(s) "
        f"em {len(pedidos)} ShipmentId(s); "
        f"{etiquetas_ja_registradas} já estavam registradas.",
    )

    colunas_fila = ["Rua", "Palete de origem"] + COLUNAS_RESULTADO + [
        "UF", "Cidade", "Origem", "Status",
    ]
    if not etiquetas_novas:
        vazio = pd.DataFrame(columns=colunas_fila)
        return {
            "tabela": vazio,
            "tabela_completa": vazio.copy(),
            "etiquetas_sem_retorno_fraction": [],
            "shipment_ids_sem_retorno_fraction": [],
            "shipment_ids_sem_destino_performance": [],
            "shipment_ids_sem_status_performance": [],
            "shipment_ids_fora_emissao": [],
            "etiquetas_ja_registradas": etiquetas_ja_registradas,
            "etiquetas_status_fora_fila": 0,
            "shipment_ids_etiquetas": 0,
            "total_etiquetas": 0,
        }

    _log_performance_emissao(
        log,
        f"Consultando {len(etiquetas_novas)} etiqueta(s) no Envio de remessa "
        "do Fraction para obter as Remessas/CTEs.",
    )
    cte_por_etiqueta = consultar_ctes_por_etiquetas_fraction(
        etiquetas_novas,
        log=log,
    )
    cte_por_etiqueta = {
        str(etiqueta).strip(): _normalizar_valor(cte)
        for etiqueta, cte in cte_por_etiqueta.items()
        if _normalizar_valor(cte)
    }
    etiquetas_sem_cte = [
        etiqueta for etiqueta in etiquetas_novas if etiqueta not in cte_por_etiqueta
    ]
    if etiquetas_sem_cte:
        etiquetas_ausentes = set(etiquetas_sem_cte)
        _log_performance_emissao(
            log,
            f"O Fraction não retornou Remessa/CTE para {len(etiquetas_sem_cte)} "
            "etiqueta(s); elas serão ignoradas e as correspondências encontradas "
            "seguirão para a consulta.",
        )
        pedidos_com_correspondencia: list[dict[str, object]] = []
        for pedido in pedidos:
            etiquetas_por_palete = pedido.get("ETIQUETAS_POR_PALETE", {})
            if not isinstance(etiquetas_por_palete, dict):
                continue
            etiquetas_validas_por_palete: dict[str, list[str]] = {}
            for palete, etiquetas_palete in etiquetas_por_palete.items():
                validas = [
                    etiqueta
                    for etiqueta in etiquetas_palete
                    if etiqueta in cte_por_etiqueta
                ]
                etiquetas_validas_por_palete[str(palete)] = validas
                for etiqueta in etiquetas_palete:
                    if etiqueta in etiquetas_ausentes:
                        _log_performance_emissao(
                            log,
                            f"Etiqueta ignorada sem Remessa/CTE: {etiqueta}; "
                            f"ShipmentId {pedido['ShipmentId']}, palete '{palete}'.",
                        )
            etiquetas_validas_por_palete = {
                palete: etiquetas
                for palete, etiquetas in etiquetas_validas_por_palete.items()
                if etiquetas
            }
            etiquetas_validas = [
                etiqueta
                for etiquetas_palete in etiquetas_validas_por_palete.values()
                for etiqueta in etiquetas_palete
            ]
            if not etiquetas_validas:
                continue
            pedido_com_correspondencia = dict(pedido)
            pedido_com_correspondencia["ETIQUETAS"] = etiquetas_validas
            pedido_com_correspondencia["VOLUMES_ETIQUETAS"] = len(etiquetas_validas)
            pedido_com_correspondencia["ETIQUETAS_POR_PALETE"] = (
                etiquetas_validas_por_palete
            )
            pedidos_com_correspondencia.append(pedido_com_correspondencia)
        pedidos = pedidos_com_correspondencia

    if not cte_por_etiqueta:
        _log_performance_emissao(
            log,
            "Nenhuma etiqueta retornou Remessa/CTE do Fraction; não há dados "
            "para consultar na Performance.",
        )
        vazio = pd.DataFrame(columns=colunas_fila)
        return {
            "tabela": vazio,
            "tabela_completa": vazio.copy(),
            "etiquetas_sem_retorno_fraction": etiquetas_sem_cte,
            "shipment_ids_sem_retorno_fraction": [],
            "shipment_ids_sem_destino_performance": [],
            "shipment_ids_sem_status_performance": [],
            "shipment_ids_fora_emissao": [],
            "etiquetas_ja_registradas": etiquetas_ja_registradas,
            "etiquetas_status_fora_fila": 0,
            "shipment_ids_etiquetas": 0,
            "total_etiquetas": 0,
        }

    _log_performance_emissao(
        log,
        f"Prosseguindo com {len(cte_por_etiqueta)} etiqueta(s) associada(s) "
        f"a {len(set(cte_por_etiqueta.values()))} Remessa/CTE(s).",
    )

    ctes_unicos = list(dict.fromkeys(cte_por_etiqueta.values()))
    desempenho_por_cte = _consultar_dados_performance_ctes(
        ctes_unicos,
        log=log,
        pasta_temporaria=pasta_temporaria,
    )
    linhas: list[dict[str, object]] = []
    shipment_ids_sem_destino: list[str] = []
    shipment_ids_sem_status: list[str] = []
    shipment_ids_sem_retorno: list[str] = []

    for pedido in pedidos:
        shipment_id = str(pedido["ShipmentId"])
        for palete, etiquetas_brutas in pedido.get("ETIQUETAS_POR_PALETE", {}).items():
            rua = str(pedido.get("RUA_POR_PALETE", {}).get(palete, "")).strip()
            etiquetas = sorted(
                dict.fromkeys(etiquetas_brutas),
                key=lambda etiqueta: int(PADRAO_ETIQUETA.fullmatch(etiqueta).group(2)),
            )
            ctes_por_etiqueta = [cte_por_etiqueta[etiqueta] for etiqueta in etiquetas]
            ctes_pedido = list(dict.fromkeys(ctes_por_etiqueta))
            dados_performance = [
                desempenho_por_cte.get(cte, {}) for cte in ctes_pedido
            ]

            def valor_unico(campo: str) -> str:
                valores = list(dict.fromkeys(
                    _normalizar_valor(item.get(campo, ""))
                    for item in dados_performance
                    if _normalizar_valor(item.get(campo, ""))
                ))
                if len(valores) > 1:
                    raise ValueError(
                        f"ShipmentId {shipment_id} tem valores de {campo} "
                        f"divergentes entre as Remessas consultadas: {valores}."
                    )
                return valores[0] if valores else ""

            destino = valor_unico("Destino")
            status = valor_unico("Status")
            uf = valor_unico("UF")
            cidade = valor_unico("Cidade")
            if not any(desempenho_por_cte.get(cte) for cte in ctes_pedido):
                shipment_ids_sem_retorno.append(shipment_id)
            if not destino:
                shipment_ids_sem_destino.append(shipment_id)
            if not status:
                shipment_ids_sem_status.append(shipment_id)

            linha = {
                "Rua": rua,
                "Palete de origem": str(palete),
                "CTE": ctes_pedido[0] if ctes_pedido else "",
                "ShipmentId": shipment_id,
                "Etiquetas do pedido": "\n".join(etiquetas),
                "Volumes Sistema": None,
                "Volumes Etiquetas": len(etiquetas),
                "Etiquetas faltantes": "",
                "CTEs para dar entrada": "\n".join(ctes_pedido),
                "Destino": destino,
                "Manifesto": "",
                "CTEs por etiqueta": "\n".join(ctes_por_etiqueta),
                "UF": uf,
                "Cidade": cidade,
                "Origem": "Consulta por etiqueta",
                "Status": status,
            }
            linhas.append(linha)
            _log_performance_emissao(
                log,
                f"Linha montada — {rua}, palete '{palete}', ShipmentId "
                f"{shipment_id}, {len(etiquetas)} etiqueta(s), "
                f"{len(ctes_pedido)} Remessa/CTE(s), Destino "
                f"'{destino or 'SEM DESTINO'}', Status "
                f"'{status or 'SEM STATUS'}'.",
            )

    tabela_completa = pd.DataFrame(linhas)
    tabela_completa = _adicionar_rota(tabela_completa)
    tabela_completa = _completar_rotas_por_rua_triagem(tabela_completa, log=log)
    status_final_emissao = tabela_completa["Status"].map(
        lambda valor: _normalizar_cabecalho(valor) == "EMISSAO"
    )
    etiquetas_status_fora_fila = 0
    for indice, linha in tabela_completa.iterrows():
        vai_para_fila = bool(status_final_emissao.loc[indice])
        etiquetas = [
            etiqueta.strip()
            for etiqueta in str(linha["Etiquetas do pedido"]).splitlines()
            if etiqueta.strip()
        ]
        _log_performance_emissao(
            log,
            f"Classificação — {linha['Rua']} / triagem "
            f"'{linha['Triagem'] or 'SEM TRIAGEM'}', palete "
            f"'{linha['Palete de origem']}', ShipmentId {linha['ShipmentId']}, "
            f"CTE '{linha['CTE']}', Status '{linha['Status'] or 'SEM STATUS'}': "
            f"{'INCLUÍDO NA FILA EMISSAO' if vai_para_fila else 'FORA DA FILA'}; "
            f"Rota '{linha['Rota']}', Destino '{linha['Destino']}'.",
        )
        if not vai_para_fila and etiquetas:
            novas = registrar_etiquetas_romaneio(
                etiquetas,
                contexto=(
                    f"pedido fora da fila — {linha['Rua']} / triagem "
                    f"'{linha['Triagem']}', palete '{linha['Palete de origem']}', "
                    f"ShipmentId {linha['ShipmentId']}, status "
                    f"'{linha['Status'] or 'ausente'}'"
                ),
                log=log,
            )
            etiquetas_status_fora_fila += len(novas)

    tabela_completa = tabela_completa.reset_index(drop=True)
    tabela = tabela_completa.loc[status_final_emissao].reset_index(drop=True)
    shipment_ids_fora_emissao = list(dict.fromkeys(
        tabela_completa.loc[~status_final_emissao, "ShipmentId"].astype(str).tolist()
    ))
    if log is not None:
        log(
            f"Etiquetas dos TXT: {total_etiquetas_importadas} lida(s), "
            f"{etiquetas_ja_registradas} já estavam registradas, "
            f"{len(etiquetas_novas) - len(etiquetas_sem_cte)} com Remessa/CTE, "
            f"{len(etiquetas_sem_cte)} sem retorno e ignorada(s), "
            f"{etiquetas_status_fora_fila} registrada(s) por status fora da fila; "
            f"{len(tabela)} linha(s) EMISSAO mantida(s) em "
            f"{tabela_completa[['Rua', 'Palete de origem']].drop_duplicates().shape[0]} "
            "grupo(s) rua/palete."
        )
        if shipment_ids_sem_retorno:
            log(
                f"{len(set(shipment_ids_sem_retorno))} ShipmentId(s) não retornaram "
                "dados na Performance consultada por CTE."
            )
        if shipment_ids_sem_destino:
            log(
                f"{len(set(shipment_ids_sem_destino))} ShipmentId(s) ficaram sem "
                "Destino na Performance."
            )
        if shipment_ids_sem_status:
            log(
                f"{len(set(shipment_ids_sem_status))} ShipmentId(s) ficaram sem "
                "Status na Performance e não entraram na fila EMISSAO."
            )

    return {
        "tabela": tabela,
        "tabela_completa": tabela_completa,
        "etiquetas_sem_retorno_fraction": etiquetas_sem_cte,
        "shipment_ids_sem_retorno_fraction": list(dict.fromkeys(shipment_ids_sem_retorno)),
        "shipment_ids_sem_destino_performance": list(dict.fromkeys(shipment_ids_sem_destino)),
        "shipment_ids_sem_status_performance": list(dict.fromkeys(shipment_ids_sem_status)),
        "shipment_ids_fora_emissao": shipment_ids_fora_emissao,
        "etiquetas_ja_registradas": etiquetas_ja_registradas,
        "etiquetas_status_fora_fila": etiquetas_status_fora_fila,
        "shipment_ids_etiquetas": len(tabela),
        "total_etiquetas": int(tabela["Volumes Etiquetas"].sum()) if not tabela.empty else 0,
    }


def _log_performance_emissao(log, mensagem: str) -> None:
    if log is None:
        print(mensagem)
    else:
        log(mensagem)


def _localizar_acao_drive(page, padrao: re.Pattern):
    for papel in ("button", "menuitem", "link"):
        controles = page.get_by_role(papel, name=padrao)
        for indice in range(controles.count()):
            controle = controles.nth(indice)
            if controle.is_visible() and controle.is_enabled():
                return controle
    return None


def _fechar_previa_txt_drive(page, *, log=None) -> None:
    dialogo_previa = page.get_by_role(
        "dialog",
        name=re.compile(r"^(texto|text),", re.IGNORECASE),
    )
    for _ in range(2):
        dialogo_visivel = next(
            (
                dialogo_previa.nth(indice)
                for indice in range(dialogo_previa.count())
                if dialogo_previa.nth(indice).is_visible()
            ),
            None,
        )
        if dialogo_visivel is None:
            return

        _log_performance_emissao(
            log,
            "Prévia de TXT aberta no Drive; fechando antes de selecionar os arquivos.",
        )
        page.keyboard.press("Escape")
        try:
            dialogo_visivel.wait_for(state="hidden", timeout=5_000)
            return
        except PlaywrightTimeoutError:
            botao_fechar = dialogo_visivel.get_by_role(
                "button",
                name=re.compile(r"close|fechar|voltar", re.IGNORECASE),
            )
            if botao_fechar.count() and botao_fechar.first.is_visible():
                botao_fechar.first.click(timeout=5_000, force=True)
                dialogo_visivel.wait_for(state="hidden", timeout=5_000)
                return

    raise RuntimeError(
        "A prévia de um TXT permaneceu aberta no Google Drive e bloqueou a lista."
    )


def _localizar_download_drive_apos_selecao(
    page,
    padrao_download: re.Pattern,
    padrao_mais_acoes: re.Pattern,
):
    acao_download = _localizar_acao_drive(page, padrao_download)
    if acao_download is not None:
        return acao_download

    mais_acoes = _localizar_acao_drive(page, padrao_mais_acoes)
    if mais_acoes is not None:
        mais_acoes.click(timeout=10_000)
        page.wait_for_timeout(300)
        _fechar_previa_txt_drive(page)
        return _localizar_acao_drive(page, padrao_download)
    return None


def _selecionar_linha_arquivo_drive(linha, celulas, *, ja_selecionados: int) -> str:
    """Seleciona uma linha do Drive, inclusive quando a caixa não é exposta."""
    seletores = linha.get_by_role("checkbox")
    if not seletores.count():
        # O Drive nem sempre publica o controle de seleção como role acessível;
        # dependendo da versão/layout ele só aparece visualmente ao passar o mouse.
        seletores = linha.locator(
            '[role="checkbox"], input[type="checkbox"], '
            '[aria-label*="Select"], [aria-label*="Selecionar"]'
        )

    for indice in range(seletores.count()):
        seletor = seletores.nth(indice)
        if not seletor.is_visible() or not seletor.is_enabled():
            continue
        marcado = (
            seletor.get_attribute("aria-checked") == "true"
            or (seletor.get_attribute("type") == "checkbox" and seletor.is_checked())
        )
        if not marcado:
            seletor.click(timeout=5_000, force=True)
        return "caixa de seleção"

    # Na lista do Drive, um clique simples seleciona o arquivo; Ctrl+clique
    # preserva as seleções anteriores ao acumular vários TXT.
    alvo = celulas.first if celulas.count() else linha
    modificadores = ["Control"] if ja_selecionados else []
    alvo.click(timeout=5_000, modifiers=modificadores, force=True)
    return "clique na linha"


def _nome_arquivo_tem_transferencia(nome_arquivo: str) -> bool:
    nome = PurePosixPath(str(nome_arquivo)).name
    normalizado = unicodedata.normalize("NFKD", nome)
    normalizado = "".join(
        caractere
        for caractere in normalizado
        if not unicodedata.combining(caractere)
    ).casefold()
    return "transferencia" in normalizado


def baixar_etiquetas_drive(
    *,
    pasta_destino: Path | None = None,
    arquivos_conhecidos: set[str] | None = None,
    log=None,
) -> Path | None:
    """Baixa apenas TXT ainda não registrados, agrupando-os em um ZIP."""
    if not URL_DRIVE:
        raise RuntimeError("Configure URL_DRIVE no arquivo bot/.env.")

    pasta_destino = (
        Path(pasta_destino)
        if pasta_destino is not None
        else Path(tempfile.mkdtemp(prefix="botevelog_etiquetas_emissao_"))
    )
    pasta_destino.mkdir(parents=True, exist_ok=True)
    identificador = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
    caminho_zip = pasta_destino / f"etiquetas_drive_{identificador}.zip"
    padrao_download = re.compile(r"download|baixar", re.IGNORECASE)
    padrao_mais_acoes = re.compile(
        r"more actions|mais ações|mais opcoes|mais opções",
        re.IGNORECASE,
    )

    with sync_playwright() as playwright:
        ultimo_erro: Exception | None = None
        for tentativa in range(1, MAX_TENTATIVAS_DOWNLOAD_DRIVE + 1):
            browser = None
            contexto = None
            try:
                _log_performance_emissao(
                    log,
                    f"Abrindo o Google Drive para baixar etiquetas "
                    f"(tentativa {tentativa}/{MAX_TENTATIVAS_DOWNLOAD_DRIVE}).",
                )
                browser = playwright.chromium.launch(
                    headless=HEADLESS,
                    slow_mo=SLOW_MO_MS,
                )
                contexto = browser.new_context(accept_downloads=True)
                page = contexto.new_page()
                page.goto(
                    URL_DRIVE,
                    wait_until="domcontentloaded",
                    timeout=90_000,
                )
                page.wait_for_timeout(1_500)
                _fechar_previa_txt_drive(page, log=log)
                texto_pagina = page.locator("body").inner_text().casefold()
                if any(
                    frase in texto_pagina
                    for frase in (
                        "you need access",
                        "request access",
                        "solicitar acesso",
                        "pedir acesso",
                    )
                ):
                    raise PermissionError(
                        "O link do Google Drive não permite acesso à pasta. "
                        "Compartilhe-a com acesso pelo link e permissão de leitura."
                    )

                grades = page.get_by_role("grid")
                try:
                    grades.first.wait_for(state="visible", timeout=60_000)
                except PlaywrightTimeoutError as erro:
                    raise RuntimeError(
                        "O link abriu, mas a pasta do Google Drive não exibiu "
                        "a lista de arquivos. Confirme a permissão de leitura."
                    ) from erro

                nomes_conhecidos = {
                    str(nome).strip().casefold()
                    for nome in (arquivos_conhecidos or set())
                }
                quantidade_selecionada = 0
                quantidade_txt_drive = 0
                quantidade_txt_transferencia = 0
                quantidade_txt_novo = 0
                for indice_grade in range(grades.count()):
                    grade = grades.nth(indice_grade)
                    if not grade.is_visible():
                        continue
                    linhas = grade.get_by_role("row")
                    for indice_linha in range(linhas.count()):
                        linha = linhas.nth(indice_linha)
                        celulas = linha.get_by_role("gridcell")
                        if not celulas.count():
                            continue
                        texto_celula = celulas.first.inner_text().strip()
                        if not texto_celula:
                            continue
                        nome_arquivo = texto_celula.splitlines()[0].strip()
                        if PurePosixPath(nome_arquivo).suffix.casefold() != ".txt":
                            continue
                        quantidade_txt_drive += 1
                        if not _nome_arquivo_tem_transferencia(nome_arquivo):
                            _log_performance_emissao(
                                log,
                                f"Ignorando TXT sem 'Transferência' no nome: "
                                f"{nome_arquivo}.",
                            )
                            continue
                        quantidade_txt_transferencia += 1
                        if nome_arquivo.casefold() in nomes_conhecidos:
                            _log_performance_emissao(
                                log,
                                f"Arquivo já registrado no estado; não será baixado: "
                                f"{nome_arquivo}.",
                            )
                            continue
                        quantidade_txt_novo += 1
                        try:
                            linha.hover(timeout=3_000)
                        except PlaywrightTimeoutError:
                            _log_performance_emissao(
                                log,
                                f"Não consegui destacar a linha do Drive: {nome_arquivo}.",
                            )
                            continue
                        metodo_selecao = _selecionar_linha_arquivo_drive(
                            linha,
                            celulas,
                            ja_selecionados=quantidade_selecionada,
                        )
                        quantidade_selecionada += 1
                        _log_performance_emissao(
                            log,
                            f"Selecionado para sincronização: {nome_arquivo} "
                            f"({metodo_selecao}).",
                        )

                if quantidade_selecionada == 0:
                    if quantidade_txt_drive == 0:
                        raise RuntimeError(
                            "A pasta do Google Drive não contém arquivos TXT visíveis."
                        )
                    if quantidade_txt_transferencia == 0:
                        _log_performance_emissao(
                            log,
                            "Nenhum TXT com 'Transferência' no nome foi encontrado; "
                            "não foi feito download.",
                        )
                        return None
                    if quantidade_txt_novo == 0:
                        _log_performance_emissao(
                            log,
                            "Nenhum TXT novo no Drive; não foi feito download.",
                        )
                        return None
                    raise RuntimeError(
                        f"Encontrei {quantidade_txt_novo} TXT(s) novo(s), mas não "
                        "consegui selecionar nenhum no Drive."
                    )
                if quantidade_selecionada != quantidade_txt_novo:
                    raise RuntimeError(
                        f"Foram selecionados {quantidade_selecionada} de "
                        f"{quantidade_txt_novo} TXT(s) novo(s). A sincronização foi "
                        "interrompida para não importar somente parte dos arquivos."
                    )

                _fechar_previa_txt_drive(page, log=log)
                acao_download = _localizar_download_drive_apos_selecao(
                    page,
                    padrao_download,
                    padrao_mais_acoes,
                )

                if acao_download is None:
                    raise RuntimeError(
                        "TXT novos selecionados, mas não localizei a ação Download."
                    )

                _log_performance_emissao(
                    log,
                    f"Ação Download localizada; aguardando "
                    f"{quantidade_selecionada} TXT(s) novo(s).",
                )
                try:
                    with page.expect_download(
                        timeout=TIMEOUT_DOWNLOAD_DRIVE_MS
                    ) as informacao_download:
                        acao_download.click(timeout=20_000)
                    download = informacao_download.value
                except PlaywrightTimeoutError:
                    _fechar_previa_txt_drive(page, log=log)
                    acao_download = _localizar_acao_drive(
                        page,
                        padrao_download,
                    )
                    if acao_download is None:
                        raise RuntimeError(
                            "A ação Download desapareceu antes de iniciar o ZIP."
                        )
                    _log_performance_emissao(
                        log,
                        "O clique normal foi bloqueado; repetindo a ação Download "
                        "com verificação de elemento e clique de contingência.",
                    )
                    with page.expect_download(
                        timeout=TIMEOUT_DOWNLOAD_DRIVE_MS
                    ) as informacao_download:
                        acao_download.click(timeout=20_000, force=True)
                    download = informacao_download.value

                nome_download = str(download.suggested_filename or "").strip()
                if nome_download.casefold().endswith(".zip"):
                    download.save_as(caminho_zip)
                elif nome_download.casefold().endswith(".txt") and quantidade_selecionada == 1:
                    caminho_txt = pasta_destino / f"download_drive_{identificador}.txt"
                    download.save_as(caminho_txt)
                    conteudo_txt = caminho_txt.read_bytes()
                    with ZipFile(caminho_zip, mode="w") as arquivo_zip:
                        arquivo_zip.writestr(nome_download, conteudo_txt)
                    caminho_txt.unlink(missing_ok=True)
                else:
                    raise ValueError(
                        "O Drive retornou um download individual inesperado: "
                        f"'{nome_download}'."
                    )
                try:
                    with ZipFile(caminho_zip) as arquivo_zip:
                        if not any(
                            not membro.is_dir()
                            and PurePosixPath(membro.filename).suffix.casefold() == ".txt"
                            for membro in arquivo_zip.infolist()
                        ):
                            raise ValueError(
                                "O ZIP baixado do Drive não contém arquivos TXT."
                            )
                except BadZipFile as erro:
                    raise ValueError(
                        "O Google Drive não retornou um ZIP válido para a pasta."
                    ) from erro

                _log_performance_emissao(
                    log,
                    f"{quantidade_selecionada} TXT(s) novo(s) baixado(s) em "
                    f"{caminho_zip.name}; nome retornado pelo Drive: {nome_download}.",
                )
                return caminho_zip
            except PermissionError:
                raise
            except Exception as erro:
                ultimo_erro = erro
                caminho_zip.unlink(missing_ok=True)
                _log_performance_emissao(
                    log,
                    f"Falha na tentativa {tentativa}/"
                    f"{MAX_TENTATIVAS_DOWNLOAD_DRIVE}: "
                    f"{type(erro).__name__}: {erro}",
                )
                if tentativa < MAX_TENTATIVAS_DOWNLOAD_DRIVE:
                    _log_performance_emissao(
                        log,
                        "Abrindo uma sessão nova do navegador para tentar novamente.",
                    )
                    time.sleep(min(2 * tentativa, 5))
            finally:
                if contexto is not None:
                    try:
                        contexto.close()
                    except Exception:
                        pass
                if browser is not None:
                    try:
                        browser.close()
                    except Exception:
                        pass

    raise RuntimeError(
        f"Não foi possível baixar as etiquetas do Google Drive após "
        f"{MAX_TENTATIVAS_DOWNLOAD_DRIVE} tentativas: {ultimo_erro}"
    ) from ultimo_erro


def selecionar_txt_novos_do_zip(
    conteudo_zip: bytes,
    arquivos_importados: set[str],
    *,
    log: Callable[[str], None] | None = None,
) -> tuple[bytes, list[dict[str, str]]]:
    """Retorna um ZIP só com TXT ainda não importados no estado local."""
    buffer_novos = BytesIO()
    novos: list[dict[str, str]] = []
    vistos_nesta_sincronizacao: set[str] = set()

    try:
        with ZipFile(BytesIO(conteudo_zip)) as arquivo_zip:
            membros_txt = [
                membro
                for membro in arquivo_zip.infolist()
                if not membro.is_dir()
                and PurePosixPath(membro.filename).suffix.casefold() == ".txt"
            ]
            if not membros_txt:
                raise ValueError("O ZIP do Google Drive não contém arquivos TXT.")

            membros_transferencia = []
            for membro in membros_txt:
                if _nome_arquivo_tem_transferencia(membro.filename):
                    membros_transferencia.append(membro)
                else:
                    _log_performance_emissao(
                        log,
                        "TXT ignorado por não conter 'Transferência' no nome: "
                        f"{PurePosixPath(membro.filename).as_posix()}.",
                    )

            if not membros_transferencia:
                _log_performance_emissao(
                    log,
                    "Nenhum TXT com 'Transferência' no nome foi encontrado no ZIP.",
                )
                return buffer_novos.getvalue(), novos

            with ZipFile(buffer_novos, mode="w") as zip_novos:
                for membro in membros_transferencia:
                    conteudo = arquivo_zip.read(membro)
                    digest = sha256(conteudo).hexdigest()
                    caminho_normalizado = PurePosixPath(membro.filename).as_posix()
                    chave = sha256(
                        caminho_normalizado.encode("utf-8") + b"\0" + digest.encode("ascii")
                    ).hexdigest()
                    if chave in arquivos_importados or chave in vistos_nesta_sincronizacao:
                        _log_performance_emissao(
                            log,
                            f"TXT já sincronizado; ignorando: {caminho_normalizado}.",
                        )
                        continue

                    zip_novos.writestr(membro, conteudo)
                    vistos_nesta_sincronizacao.add(chave)
                    novos.append(
                        {
                            "chave": chave,
                            "nome": PurePosixPath(caminho_normalizado).name,
                            "caminho_no_zip": caminho_normalizado,
                            "sha256": digest,
                        }
                    )
                    _log_performance_emissao(
                        log,
                        f"TXT novo detectado: {caminho_normalizado} "
                        f"({len(conteudo)} bytes; SHA-256 {digest}).",
                    )
    except BadZipFile as erro:
        raise ValueError("O conteúdo baixado do Drive não é um ZIP válido.") from erro

    if log is not None:
        log(
            f"Sincronização do ZIP: {len(novos)} TXT(s) novo(s) de "
            f"{len(membros_transferencia)} TXT(s) com 'Transferência' encontrado(s)."
        )
    return buffer_novos.getvalue(), novos
