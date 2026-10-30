from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO
import json
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
    consultar_shipments_no_fraction,
    gerar_ctes_para_entrada,
    gerar_etiquetas_faltantes,
)
from servicos.fraction import (
    carregar_codigos_preferenciais_unidades,
    carregar_regras_rotas_com_destino_e_triagem,
)
from utils.config import (
    ARQUIVO_ETIQUETAS_ROMANEIOS_EMISSAO,
    FRACTION_PASSWORD_PERFORMANCE,
    FRACTION_USER_PERFORMANCE,
    HEADLESS,
    SLOW_MO_MS,
    URL_DRIVE,
    URL_FRACTION,
)
from utils.estado import RegistroTexto


CABECALHOS_OBRIGATORIOS = {
    "SHIPMENTID": "ShipmentId",
    "CODIGO": "Codigo",
    "DESTINO": "Destino",
    "VOLUME": "Volume",
}
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
SELETOR_DATA_INICIAL_PERFORMANCE = '[id="formPerfEntFiltro:dtini_input"]'
SELETOR_DATA_FINAL_PERFORMANCE = '[id="formPerfEntFiltro:dtfim_input"]'
TIMEOUT_DOWNLOAD_PERFORMANCE_MS = 600_000
TIMEOUT_DOWNLOAD_DRIVE_MS = 300_000
MAX_TENTATIVAS_DOWNLOAD_DRIVE = 3
LIMITE_DIAS_PERFORMANCE_EMISSAO = 31
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


def _normalizar_volume(valor: object, *, shipment_id: str) -> int | None:
    texto = _normalizar_valor(valor)
    if not texto:
        return None

    # O Fraction também exporta volumes no formato 1/N.
    fracao_total = re.fullmatch(r"0*1\s*/\s*0*(\d+)\s*", texto)
    if fracao_total:
        volume = int(fracao_total.group(1))
    else:
        try:
            numero = Decimal(texto.replace(",", "."))
        except InvalidOperation as erro:
            raise ValueError(
                f"Volume inválido para o ShipmentId {shipment_id}: {texto!r}."
            ) from erro

        if not numero.is_finite() or numero != numero.to_integral_value():
            raise ValueError(
                f"Volume precisa ser um número inteiro para o ShipmentId "
                f"{shipment_id}: {texto!r}."
            )
        volume = int(numero)

    if volume <= 0:
        raise ValueError(
            f"Volume precisa ser maior que zero para o ShipmentId {shipment_id}."
        )
    return volume


def _ler_base_performance(conteudo: bytes) -> tuple[pd.DataFrame, str, int]:
    if not conteudo:
        raise ValueError("A planilha Performance está vazia.")

    correspondencias: list[tuple[str, int]] = []

    try:
        with pd.ExcelFile(BytesIO(conteudo), engine="openpyxl") as planilha:
            for nome_aba in planilha.sheet_names:
                primeiras_linhas = pd.read_excel(
                    planilha,
                    sheet_name=nome_aba,
                    header=None,
                    nrows=20,
                    dtype=object,
                )

                for indice, linha in primeiras_linhas.iterrows():
                    cabecalhos = {
                        _normalizar_cabecalho(valor)
                        for valor in linha.tolist()
                        if not pd.isna(valor)
                    }
                    if set(CABECALHOS_OBRIGATORIOS).issubset(cabecalhos):
                        correspondencias.append((nome_aba, int(indice)))
                        break

            if not correspondencias:
                raise ValueError(
                    "Não encontrei uma tabela na planilha Performance com as "
                    "colunas ShipmentId, Codigo, Destino e Volume nas primeiras "
                    "20 linhas de nenhuma aba."
                )
            if len(correspondencias) > 1:
                abas = ", ".join(nome for nome, _ in correspondencias)
                raise ValueError(
                    "Encontrei mais de uma aba com as colunas necessárias na "
                    f"planilha Performance: {abas}. Deixe apenas uma aba compatível."
                )

            nome_aba, indice_cabecalho = correspondencias[0]
            base = pd.read_excel(
                planilha,
                sheet_name=nome_aba,
                header=indice_cabecalho,
                dtype=object,
            )
    except ValueError:
        raise
    except Exception as erro:
        raise ValueError(
            f"Não foi possível ler a planilha Performance: {erro}"
        ) from erro

    colunas_por_chave: dict[str, list[object]] = {}
    colunas_status: list[object] = []
    for coluna in base.columns:
        chave = _normalizar_cabecalho(coluna)
        if chave in CABECALHOS_OBRIGATORIOS:
            colunas_por_chave.setdefault(chave, []).append(coluna)
        elif chave == "STATUS":
            colunas_status.append(coluna)

    duplicadas = [
        CABECALHOS_OBRIGATORIOS[chave]
        for chave, colunas in colunas_por_chave.items()
        if len(colunas) > 1
    ]
    if duplicadas:
        raise ValueError(
            "A planilha Performance tem cabeçalhos duplicados: "
            + ", ".join(duplicadas)
            + "."
        )
    if len(colunas_status) > 1:
        raise ValueError(
            "A planilha Performance tem mais de uma coluna Status."
        )

    faltantes = [
        nome
        for chave, nome in CABECALHOS_OBRIGATORIOS.items()
        if chave not in colunas_por_chave
    ]
    if faltantes:
        raise ValueError(
            "Coluna(s) obrigatória(s) ausente(s) na planilha Performance: "
            + ", ".join(faltantes)
            + "."
        )

    coluna_uf = _localizar_coluna_normalizada(
        list(base.columns),
        ALIAS_UF_RELATORIO,
    )
    coluna_cidade = _localizar_coluna_normalizada(
        list(base.columns),
        ALIAS_CIDADE_RELATORIO,
    )
    colunas = {
        nome: colunas_por_chave[chave][0]
        for chave, nome in CABECALHOS_OBRIGATORIOS.items()
    }
    dados = pd.DataFrame(
        {
            "ShipmentId": base[colunas["ShipmentId"]].map(_normalizar_shipment_id),
            "Codigo": base[colunas["Codigo"]].map(_normalizar_valor),
            "Destino": base[colunas["Destino"]].map(_normalizar_valor),
            "Volume": base[colunas["Volume"]],
            "Status": (
                base[colunas_status[0]].map(_normalizar_valor)
                if colunas_status
                else ""
            ),
            "UF": (
                base[coluna_uf].map(_normalizar_uf)
                if coluna_uf is not None
                else ""
            ),
            "Cidade": (
                base[coluna_cidade].map(_normalizar_valor)
                if coluna_cidade is not None
                else ""
            ),
        }
    )
    dados = dados[dados["ShipmentId"] != ""].reset_index(drop=True)
    return dados, nome_aba, indice_cabecalho + 1


def ler_base_performance(conteudo: bytes) -> tuple[pd.DataFrame, str, int]:
    """Lê e normaliza a tabela Performance para os fluxos de Emissão."""
    return _ler_base_performance(conteudo)


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


def _consultar_dados_performance_shipmentids(
    ctes_por_shipment: dict[str, str],
    *,
    log: Callable[[str], None] | None,
    pasta_temporaria: Path | None = None,
) -> dict[str, dict[str, str]]:
    """Gera relatório para todos os CTEs da fila e associa Destino e Status."""
    ctes_por_shipment = {
        shipment_id: _normalizar_valor(cte)
        for shipment_id, cte in ctes_por_shipment.items()
    }
    ctes = list(dict.fromkeys(cte for cte in ctes_por_shipment.values() if cte))
    if not ctes:
        if log is not None:
            log("Nenhum CTE foi encontrado para gerar o relatório de Performance complementar.")
        return {}

    escrever_relatorio = BytesIO()
    pd.DataFrame({"REMESSA": ctes}).to_excel(
        escrever_relatorio,
        index=False,
        engine="openpyxl",
    )
    escrever_relatorio.seek(0)
    registrar_log = log if log is not None else (lambda _mensagem: None)
    registrar_log(
        f"Gerando relatório de Performance complementar para {len(ctes)} CTE(s) "
        f"de {sum(bool(cte) for cte in ctes_por_shipment.values())} pedido(s) da fila."
    )

    try:
        resultado = executar_extracao(
            escrever_relatorio,
            log=registrar_log,
            pasta_destino=pasta_temporaria,
        )
        caminho_relatorio = resultado.get("arquivo")
        if not caminho_relatorio:
            registrar_log(
                "A consulta complementar não gerou arquivo de Performance; "
                "os destinos desses pedidos ficarão pendentes."
            )
            return {}

        registrar_log(f"Relatório complementar gerado: {caminho_relatorio}.")
        (
            destinos_por_cte,
            destinos_por_shipment,
            status_por_cte,
            status_por_shipment,
            uf_por_cte,
            uf_por_shipment,
            cidade_por_cte,
            cidade_por_shipment,
        ) = _ler_destinos_relatorio_performance(str(caminho_relatorio))
    except Exception as erro:
        registrar_log(
            "Não foi possível gerar ou ler o relatório de Performance complementar: "
            f"{type(erro).__name__}: {erro}"
        )
        return {}

    dados_por_shipment: dict[str, dict[str, str]] = {}
    for shipment_id, cte in ctes_por_shipment.items():
        destino = destinos_por_shipment.get(shipment_id, "") or (
            destinos_por_cte.get(cte, "") if cte else ""
        )
        status = status_por_shipment.get(shipment_id, "") or (
            status_por_cte.get(cte, "") if cte else ""
        )
        uf = uf_por_shipment.get(shipment_id, "") or (
            uf_por_cte.get(cte, "") if cte else ""
        )
        cidade = cidade_por_shipment.get(shipment_id, "") or (
            cidade_por_cte.get(cte, "") if cte else ""
        )
        if destino or status or uf or cidade:
            dados_por_shipment[shipment_id] = {
                "Destino": destino,
                "Status": status,
                "UF": uf,
                "Cidade": cidade,
            }

    registrar_log(
        f"Destino localizado no relatório de Performance para "
        f"{sum(bool(item['Destino']) for item in dados_por_shipment.values())}/"
        f"{len(ctes_por_shipment)} ShipmentId(s) consultados."
    )
    registrar_log(
        f"Status localizado no relatório de Performance para "
        f"{sum(bool(item['Status']) for item in dados_por_shipment.values())}/"
        f"{len(ctes_por_shipment)} ShipmentId(s) consultados."
    )
    return dados_por_shipment


def montar_fila_emissao(
    conteudo_performance: bytes,
    conteudo_etiquetas: bytes,
    *,
    nome_arquivo_etiquetas: str = "etiquetas.txt",
    log: Callable[[str], None] | None = None,
    pasta_temporaria: Path | None = None,
) -> dict[str, object]:
    """Cruza etiquetas e Performance e monta a fila de envio dos romaneios."""
    preparar_estado_etiquetas_romaneio()
    _log_performance_emissao(
        log,
        f"Arquivo de estado de etiquetas: "
        f"{ARQUIVO_ETIQUETAS_ROMANEIOS_EMISSAO}.",
    )
    base, nome_aba, linha_cabecalho = _ler_base_performance(conteudo_performance)
    pedidos = agrupar_etiquetas_importadas(
        conteudo_etiquetas,
        nome_arquivo_etiquetas,
        log=log,
    )
    total_etiquetas_importadas = sum(
        len(pedido["ETIQUETAS"]) for pedido in pedidos
    )
    total_paletes_importados = len(
        {
            (str(pedido.get("RUA_POR_PALETE", {}).get(palete, "")), str(palete))
            for pedido in pedidos
            for palete in pedido.get("ETIQUETAS_POR_PALETE", {})
        }
    )
    _log_performance_emissao(
        log,
        f"Montagem detalhada da fila iniciada: {len(pedidos)} ShipmentId(s), "
        f"{total_etiquetas_importadas} etiqueta(s), "
        f"{total_paletes_importados} grupo(s) de rua/palete.",
    )
    for pedido in pedidos:
        shipment_id = str(pedido["ShipmentId"])
        mapa_paletes = pedido.get("ETIQUETAS_POR_PALETE") or {
            "Sem palete": pedido["ETIQUETAS"]
        }
        ruas_por_palete = pedido.get("RUA_POR_PALETE", {})
        for palete, etiquetas in mapa_paletes.items():
            rua = str(ruas_por_palete.get(palete, "")).strip()
            if not rua:
                raise ValueError(
                    f"Não identifiquei a rua do arquivo que contém o palete "
                    f"'{palete}'. Inclua 'Rua 1' (ou o número correspondente) "
                    "no nome/caminho do TXT dentro do ZIP ou no cabeçalho do TXT."
                )
            _log_performance_emissao(
                log,
                f"TXT lido — {rua}, palete '{palete}', ShipmentId {shipment_id}, "
                f"{len(etiquetas)} etiqueta(s): {', '.join(etiquetas)}.",
            )

    pedidos, etiquetas_ja_registradas = _filtrar_etiquetas_ja_registradas(
        pedidos,
        log=log,
    )
    total_etiquetas_novas = sum(len(pedido["ETIQUETAS"]) for pedido in pedidos)
    _log_performance_emissao(
        log,
        f"Após consultar o estado, restaram {len(pedidos)} ShipmentId(s) e "
        f"{total_etiquetas_novas} etiqueta(s) novas; "
        f"{etiquetas_ja_registradas} etiqueta(s) foram ignoradas por já terem "
        "sido capturadas em outra execução.",
    )

    registros_por_shipment: dict[str, list[dict[str, object]]] = {}
    for _, linha in base.iterrows():
        shipment_id = str(linha["ShipmentId"])
        registros_por_shipment.setdefault(shipment_id, []).append(
            {
                "Codigo": str(linha["Codigo"]),
                "Destino": str(linha["Destino"]),
                "Volume": linha["Volume"],
                "Status": str(linha["Status"]),
                "UF": str(linha["UF"]),
                "Cidade": str(linha["Cidade"]),
            }
        )

    registros_unicos_por_shipment: dict[str, dict[str, object]] = {}
    shipment_ids_sem_match: list[str] = []
    for pedido in pedidos:
        shipment_id = str(pedido["ShipmentId"])
        _log_performance_emissao(
            log,
            f"Associando ShipmentId {shipment_id} à Performance; "
            f"{len(pedido['ETIQUETAS'])} etiqueta(s) novas.",
        )
        candidatos = registros_por_shipment.get(shipment_id, [])
        if not candidatos:
            _log_performance_emissao(
                log,
                f"ShipmentId {shipment_id}: sem correspondência direta na "
                "Performance; será consultado no Fraction.",
            )
            if shipment_id not in shipment_ids_sem_match:
                shipment_ids_sem_match.append(shipment_id)
            continue

        registros_unicos = {
            (
                str(registro["Codigo"]),
                str(registro["Destino"]),
                _normalizar_valor(registro["Volume"]),
                _normalizar_uf(registro.get("UF", "")),
                _normalizar_rota(registro.get("Cidade", "")),
            ): registro
            for registro in candidatos
        }
        if len(registros_unicos) > 1:
            raise ValueError(
                f"ShipmentId {shipment_id} aparece com mais de um Codigo, "
                "Destino, Volume, UF ou Cidade na planilha Performance. "
                "Corrija a base antes de montar a fila."
            )

        registro = next(iter(registros_unicos.values()))
        if not str(registro["Codigo"]).strip() or not str(registro["Destino"]).strip():
            raise ValueError(
                f"A linha correspondente ao ShipmentId {shipment_id} precisa "
                "ter Codigo e Destino preenchidos na planilha Performance."
            )
        registros_unicos_por_shipment[shipment_id] = registro
        _log_performance_emissao(
            log,
            f"ShipmentId {shipment_id}: correspondência única na Performance — "
            f"CTE {registro['Codigo']}, Destino '{registro['Destino']}', "
            f"Volume '{_normalizar_valor(registro['Volume'])}', "
            f"Status '{_normalizar_valor(registro['Status'])}'.",
        )

    resultados_consulta: dict[str, dict[str, object]] = {}
    if shipment_ids_sem_match:
        log_consulta = log if log is not None else (lambda _mensagem: None)
        resultados_consulta = consultar_shipments_no_fraction(
            shipment_ids_sem_match,
            log=log_consulta,
        )

    linhas_por_rua_palete_shipment: dict[tuple[str, str, str], dict[str, object]] = {}
    linhas_por_shipment: dict[str, list[dict[str, object]]] = {}
    for pedido in pedidos:
        shipment_id = str(pedido["ShipmentId"])
        etiquetas_por_palete = pedido.get("ETIQUETAS_POR_PALETE")
        if not isinstance(etiquetas_por_palete, dict) or not etiquetas_por_palete:
            etiquetas_por_palete = {"Sem palete": pedido["ETIQUETAS"]}
        ruas_por_palete = pedido.get("RUA_POR_PALETE", {})

        registro = registros_unicos_por_shipment.get(shipment_id)
        if registro is not None:
            codigo = str(registro["Codigo"]).strip()
            destino = str(registro["Destino"]).strip()
            volume_sistema = _normalizar_volume(
                registro["Volume"],
                shipment_id=shipment_id,
            )
            uf = str(registro["UF"])
            cidade = str(registro["Cidade"])
            status = _normalizar_valor(registro["Status"])
            origem = "Base"
        else:
            consulta = resultados_consulta.get(shipment_id, {})
            codigo = _normalizar_valor(consulta.get("CTE"))
            volume_sistema_bruto = consulta.get("Volumes Sistema")
            volume_sistema = (
                _normalizar_volume(volume_sistema_bruto, shipment_id=shipment_id)
                if volume_sistema_bruto is not None
                and not pd.isna(volume_sistema_bruto)
                else None
            )
            destino = ""
            uf = ""
            cidade = ""
            status = ""
            origem = "Consulta"

        for palete, etiquetas_brutas in etiquetas_por_palete.items():
            rua = str(ruas_por_palete.get(palete, "")).strip()
            if not rua:
                raise ValueError(
                    f"Não identifiquei a rua do arquivo que contém o palete "
                    f"'{palete}'. Inclua 'Rua 1' (ou o número correspondente) "
                    "no nome/caminho do TXT dentro do ZIP ou no cabeçalho do TXT."
                )
            etiquetas = sorted(
                list(etiquetas_brutas),
                key=lambda etiqueta: int(
                    PADRAO_ETIQUETA.fullmatch(etiqueta).group(2)
                ),
            )
            ctes_para_entrada = (
                gerar_ctes_para_entrada(codigo, etiquetas, volume_sistema)
                if codigo
                else ""
            )
            linha_palete = {
                "Rua": rua,
                "Palete de origem": str(palete),
                "CTE": codigo,
                "ShipmentId": shipment_id,
                "Etiquetas do pedido": "\n".join(etiquetas),
                "Volumes Sistema": volume_sistema,
                "Volumes Etiquetas": len(etiquetas),
                "Etiquetas faltantes": gerar_etiquetas_faltantes(
                    shipment_id,
                    etiquetas,
                    volume_sistema,
                ),
                "CTEs para dar entrada": ctes_para_entrada,
                "UF": uf,
                "Cidade": cidade,
                "Destino": destino,
                "Manifesto": "",
                "Origem": origem,
                "Status": status,
            }
            linhas_por_rua_palete_shipment[(rua, str(palete), shipment_id)] = linha_palete
            linhas_por_shipment.setdefault(shipment_id, []).append(linha_palete)
            _log_performance_emissao(
                log,
                f"Linha montada — {rua}, palete '{palete}', ShipmentId {shipment_id}, "
                f"CTE '{codigo}', {len(etiquetas)} etiqueta(s), "
                f"{len(ctes_para_entrada.splitlines())} CTE(s) para envio, "
                f"Status '{status or 'SEM STATUS'}', Destino '{destino or 'SEM DESTINO'}'. "
                f"Etiquetas: {', '.join(etiquetas)}.",
            )

    shipment_ids_fila = list(
        dict.fromkeys(
            shipment_id
            for _, _, shipment_id in linhas_por_rua_palete_shipment
        )
    )
    ctes_por_shipment = {
        shipment_id: _normalizar_valor(
            registros_unicos_por_shipment.get(shipment_id, {}).get("Codigo")
            or resultados_consulta.get(shipment_id, {}).get("CTE")
        )
        for shipment_id in shipment_ids_fila
    }
    dados_performance_consultados = _consultar_dados_performance_shipmentids(
        ctes_por_shipment,
        log=log,
        pasta_temporaria=pasta_temporaria,
    )
    for shipment_id in shipment_ids_fila:
        dados_performance = dados_performance_consultados.get(
            shipment_id,
            {},
        )
        for linha_fila in linhas_por_shipment.get(shipment_id, []):
            if dados_performance.get("Destino"):
                linha_fila["Destino"] = dados_performance["Destino"]
            if dados_performance.get("Status"):
                linha_fila["Status"] = dados_performance["Status"]
            if dados_performance.get("UF"):
                linha_fila["UF"] = dados_performance["UF"]
            if dados_performance.get("Cidade"):
                linha_fila["Cidade"] = dados_performance["Cidade"]

    linhas = list(linhas_por_rua_palete_shipment.values())

    colunas_fila = ["Rua", "Palete de origem"] + COLUNAS_RESULTADO + [
        "UF",
        "Cidade",
        "Origem",
        "Status",
    ]
    tabela_completa = pd.DataFrame(linhas, columns=colunas_fila)
    tabela_completa = _adicionar_rota(tabela_completa)
    tabela_completa = _completar_rotas_por_rua_triagem(
        tabela_completa,
        log=log,
    )
    colunas_base_completa = ["Rua", "Palete de origem"] + [
        coluna
        for coluna in COLUNAS_RESULTADO
        if coluna not in {"Destino", "Manifesto"}
    ] + [
        "UF",
        "Cidade",
        "Destino",
        "Triagem",
        "Rota",
        "Manifesto",
        "Origem",
        "Status",
    ]
    tabela_completa = tabela_completa[colunas_base_completa]
    status_final_emissao = tabela_completa["Status"].map(
        lambda valor: _normalizar_cabecalho(valor) == "EMISSAO"
    )
    etiquetas_status_fora_fila = 0
    for indice, linha in tabela_completa.iterrows():
        etiquetas = [
            etiqueta.strip()
            for etiqueta in str(linha["Etiquetas do pedido"]).splitlines()
            if etiqueta.strip()
        ]
        status = _normalizar_valor(linha["Status"])
        vai_para_fila = bool(status_final_emissao.loc[indice])
        _log_performance_emissao(
            log,
            f"Classificação — {linha['Rua']} / triagem "
            f"'{linha['Triagem'] or 'SEM TRIAGEM'}', palete "
            f"'{linha['Palete de origem']}', ShipmentId "
            f"{linha['ShipmentId']}, CTE '{linha['CTE']}', Status "
            f"'{status or 'SEM STATUS'}': "
            f"{'INCLUÍDO NA FILA EMISSAO' if vai_para_fila else 'FORA DA FILA'}; "
            f"Rota '{linha['Rota']}', Destino '{linha['Destino']}', "
            f"{len(etiquetas)} etiqueta(s): {', '.join(etiquetas)}.",
        )
        if not vai_para_fila and etiquetas:
            motivo = (
                f"Status '{status}' diferente de EMISSAO"
                if status
                else "Status ausente"
            )
            novas = registrar_etiquetas_romaneio(
                etiquetas,
                contexto=(
                    f"pedido fora da fila — {linha['Rua']} / triagem "
                    f"'{linha['Triagem']}', palete '{linha['Palete de origem']}', "
                    f"ShipmentId {linha['ShipmentId']}, motivo: {motivo}"
                ),
                log=log,
            )
            etiquetas_status_fora_fila += len(novas)
    tabela_base_completa = tabela_completa.reset_index(drop=True)
    colunas_fila_emissao = ["Rua", "Palete de origem"] + COLUNAS_RESULTADO + [
        "Triagem",
        "Rota",
        "Origem",
        "Status",
    ]
    tabela = tabela_completa.loc[
        status_final_emissao,
        colunas_fila_emissao,
    ].reset_index(drop=True)
    shipment_ids_fora_emissao = list(dict.fromkeys(tabela_completa.loc[
        ~status_final_emissao,
        "ShipmentId",
    ].astype(str).tolist()))
    shipment_ids_sem_status = list(dict.fromkeys(tabela_completa.loc[
        tabela_completa["Status"].fillna("").astype(str).str.strip().eq(""),
        "ShipmentId",
    ].astype(str).tolist()))

    if log is not None:
        log(
            f"Performance: {len(base)} linha(s) utilizável(is) na aba "
            f"{nome_aba}; cabeçalho na linha {linha_cabecalho}."
        )
        log(
            f"Etiquetas: {total_etiquetas_importadas} importada(s), "
            f"{etiquetas_ja_registradas} já registrada(s) em execuções anteriores, "
            f"{etiquetas_status_fora_fila} registrada(s) agora por status fora da fila; "
            f"{sum(len(pedido['ETIQUETAS']) for pedido in pedidos)} "
            f"em {len(pedidos)} ShipmentId(s) e "
            f"{tabela_completa[['Rua', 'Palete de origem']].drop_duplicates().shape[0]} "
            "grupo(s) de rua/palete; linhas com Status "
            f"final EMISSAO mantidas após atualização: {len(tabela)}."
        )
        if shipment_ids_fora_emissao:
            log(
                f"{len(shipment_ids_fora_emissao)} pedido(s) removidos da fila por "
                "estarem sem Status EMISSAO."
            )
        if shipment_ids_sem_match:
            log(
                f"{len(shipment_ids_sem_match)} ShipmentId(s) ausentes da "
                "Performance foram consultados individualmente no Fraction."
            )
    sem_retorno = list(dict.fromkeys([
        shipment_id
        for shipment_id in shipment_ids_sem_match
        if not _normalizar_valor(
            resultados_consulta.get(shipment_id, {}).get("CTE")
        )
    ]))
    sem_destino = list(dict.fromkeys([
        shipment_id
        for shipment_id in shipment_ids_sem_match
        if not _normalizar_valor(
            next(
                (
                    linha.get("Destino")
                    for linha in linhas_por_shipment.get(shipment_id, [])
                ),
                "",
            )
        )
    ]))
    if log is not None:
        if sem_retorno:
            log(
                f"{len(sem_retorno)} ShipmentId(s) sem CTE encontrado na consulta "
                "do Fraction."
            )
        if sem_destino:
            log(
                f"{len(sem_destino)} ShipmentId(s) ficaram sem Destino no relatório "
                "de Performance complementar."
            )
        if shipment_ids_sem_status:
            log(
                f"{len(shipment_ids_sem_status)} pedido(s) ficaram sem Status nas "
                "planilhas de Performance e foram removidos da fila."
            )

    return {
        "tabela": tabela,
        "tabela_completa": tabela_base_completa,
        "shipment_ids_sem_match": shipment_ids_sem_match,
        "shipment_ids_sem_retorno_fraction": sem_retorno,
        "shipment_ids_sem_destino_performance": sem_destino,
        "shipment_ids_sem_status_performance": shipment_ids_sem_status,
        "shipment_ids_fora_emissao": shipment_ids_fora_emissao,
        "etiquetas_ja_registradas": etiquetas_ja_registradas,
        "etiquetas_status_fora_fila": etiquetas_status_fora_fila,
        "shipment_ids_etiquetas": len(tabela),
        "total_etiquetas": int(tabela["Volumes Etiquetas"].sum()),
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


def baixar_etiquetas_drive(
    *,
    pasta_destino: Path | None = None,
    log=None,
) -> Path:
    """Baixa a pasta compartilhada do Drive como um ZIP."""
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

                acao_download = _localizar_acao_drive(page, padrao_download)
                if acao_download is None:
                    grades = page.get_by_role("grid")
                    try:
                        grades.first.wait_for(state="visible", timeout=60_000)
                    except PlaywrightTimeoutError as erro:
                        raise RuntimeError(
                            "O link abriu, mas a pasta do Google Drive não exibiu "
                            "a lista de arquivos. Confirme a permissão de leitura."
                        ) from erro

                    for indice in range(grades.count()):
                        grade = grades.nth(indice)
                        if not grade.is_visible():
                            continue
                        _fechar_previa_txt_drive(page, log=log)
                        try:
                            grade.focus(timeout=5_000)
                        except PlaywrightTimeoutError:
                            _log_performance_emissao(
                                log,
                                "A grade não aceitou foco direto; tentando selecionar "
                                "os arquivos pelo teclado.",
                            )
                        page.keyboard.press("Control+A")
                        page.wait_for_timeout(500)
                        acao_download = _localizar_download_drive_apos_selecao(
                            page,
                            padrao_download,
                            padrao_mais_acoes,
                        )
                        if acao_download is not None:
                            break

                        linhas = grade.get_by_role("row")
                        for indice_linha in range(min(linhas.count(), 5)):
                            linha = linhas.nth(indice_linha)
                            try:
                                linha.hover(timeout=3_000)
                            except PlaywrightTimeoutError:
                                continue
                            seletores = linha.get_by_role("checkbox")
                            if not seletores.count() or not seletores.first.is_visible():
                                continue
                            seletores.first.click(timeout=5_000)
                            page.keyboard.press("Control+A")
                            page.wait_for_timeout(500)
                            acao_download = _localizar_download_drive_apos_selecao(
                                page,
                                padrao_download,
                                padrao_mais_acoes,
                            )
                            if acao_download is not None:
                                break
                        if acao_download is not None:
                            break

                if acao_download is None:
                    raise RuntimeError(
                        "A pasta abriu, mas não localizei a ação Download após "
                        "tentar selecionar os arquivos pelo teclado e pela caixa "
                        "de seleção."
                    )

                _log_performance_emissao(
                    log,
                    "Ação Download localizada; aguardando o ZIP da pasta.",
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

                download.save_as(caminho_zip)
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
                    f"Pasta do Drive baixada como {caminho_zip.name}; "
                    f"nome original: {download.suggested_filename}.",
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


def _login_performance_emissao(page, token: str, *, log=None) -> None:
    if not URL_FRACTION:
        raise RuntimeError("URL_FRACTION não foi configurada no .env.")
    if not FRACTION_USER_PERFORMANCE or not FRACTION_PASSWORD_PERFORMANCE:
        raise RuntimeError(
            "Configure FRACTION_USER_PERFORMANCE e "
            "FRACTION_PASSWORD_PERFORMANCE no .env."
        )
    if not token.strip():
        raise ValueError("Informe o Token MFA para consultar o Fraction.")

    _log_performance_emissao(log, "Abrindo o Fraction para a extração da Performance.")
    page.goto(URL_FRACTION, wait_until="domcontentloaded")
    page.get_by_role("textbox", name="Usuário").fill(
        FRACTION_USER_PERFORMANCE
    )
    page.get_by_role("textbox", name="Senha").fill(
        FRACTION_PASSWORD_PERFORMANCE
    )
    page.get_by_role("button", name="Login").click()

    campo_token = page.get_by_role("textbox", name="Digite o código MFA:")
    campo_token.wait_for(state="visible", timeout=60_000)
    campo_token.fill(token.strip())
    page.get_by_role("button", name="Validar").click()
    page.get_by_role("link", name="Gerencial").wait_for(
        state="visible",
        timeout=120_000,
    )
    _log_performance_emissao(log, "Login do Fraction concluído com o Token MFA.")


def _abrir_tela_performance_emissao(page, *, log=None) -> None:
    _log_performance_emissao(log, "Abrindo Gerencial > Performance.")
    page.get_by_role("link", name="Gerencial").click()
    page.get_by_role("link", name="Performance", exact=True).click()
    page.locator(SELETOR_DATA_INICIAL_PERFORMANCE).wait_for(
        state="visible",
        timeout=120_000,
    )
    page.locator(SELETOR_DATA_FINAL_PERFORMANCE).wait_for(
        state="visible",
        timeout=120_000,
    )


def _preencher_data_performance_emissao(page, seletor: str, valor: date) -> None:
    campo = page.locator(seletor)
    texto = valor.strftime("%d/%m/%Y")
    campo.click()
    meses = page.get_by_label("select month")
    for indice in range(meses.count()):
        mes_visivel = meses.nth(indice)
        if mes_visivel.is_visible():
            mes_visivel.select_option(str(valor.month - 1))
            break
    anos = page.get_by_label("select year")
    for indice in range(anos.count()):
        ano_visivel = anos.nth(indice)
        if ano_visivel.is_visible():
            ano_visivel.select_option(str(valor.year))
            break
    dias = page.get_by_role(
        "link",
        name=str(valor.day),
        exact=True,
    )
    dia_selecionado = False
    for indice in range(dias.count()):
        dia_visivel = dias.nth(indice)
        if dia_visivel.is_visible():
            dia_visivel.click()
            dia_selecionado = True
            break
    if not dia_selecionado:
        dias.first.wait_for(state="visible", timeout=15_000)
        dias.first.click()
    if campo.input_value().strip() != texto:
        raise RuntimeError(
            f"O Fraction não manteve a data {texto} no campo {seletor}."
        )


def _extrair_planilhas_performance_zip(
    conteudo_zip: bytes,
    data_inicial: date,
    data_final: date,
    pasta_planilhas: Path,
) -> list[tuple[pd.DataFrame, Path, str, int]]:
    try:
        with ZipFile(BytesIO(conteudo_zip)) as arquivo_zip:
            membros = [
                membro
                for membro in arquivo_zip.infolist()
                if not membro.is_dir()
                and PurePosixPath(membro.filename).suffix.casefold() == ".xlsx"
                and "__macosx" not in {
                    parte.casefold()
                    for parte in PurePosixPath(membro.filename).parts
                }
            ]
            if not membros:
                raise ValueError(
                    "O ZIP do Fraction não contém uma planilha .xlsx."
                )

            partes: list[tuple[pd.DataFrame, Path, str, int]] = []
            for indice, membro in enumerate(membros, start=1):
                conteudo_planilha = arquivo_zip.read(membro)
                caminho_planilha = pasta_planilhas / (
                    f"Performance_{data_inicial:%Y-%m-%d}_"
                    f"a_{data_final:%Y-%m-%d}_{indice:02d}.xlsx"
                )
                caminho_planilha.write_bytes(conteudo_planilha)
                tabela, nome_aba, linha_cabecalho = ler_base_performance(
                    conteudo_planilha
                )
                tabela.insert(
                    0,
                    "Período da consulta",
                    f"{data_inicial.isoformat()} a {data_final.isoformat()}",
                )
                tabela = _adicionar_rota(tabela)
                partes.append(
                    (
                        tabela,
                        caminho_planilha,
                        nome_aba,
                        linha_cabecalho,
                    )
                )
    except BadZipFile as erro:
        raise ValueError(
            "O ZIP da Performance do período "
            f"{data_inicial:%d/%m/%Y} a {data_final:%d/%m/%Y} está inválido."
        ) from erro

    return partes


def _baixar_performance_periodo(
    page,
    pasta_zips: Path,
    *,
    log=None,
) -> Path:
    total = page.get_by_role(
        "gridcell",
        name="TOTAL DO PERIODO",
        exact=True,
    )
    total.wait_for(state="visible", timeout=240_000)
    linha_total = total.locator("xpath=ancestor::tr[1]")
    celulas = linha_total.get_by_role("gridcell")
    if celulas.count() < 2:
        raise RuntimeError(
            "A linha TOTAL DO PERIODO foi localizada, mas não contém a célula "
            "dinâmica de quantidade esperada."
        )

    total.click()
    celula_quantidade = linha_total.get_by_role("gridcell").nth(1)
    quantidade = celula_quantidade.inner_text().strip()
    if not quantidade:
        raise RuntimeError(
            "A quantidade de pedidos da linha TOTAL DO PERIODO está vazia."
        )
    _log_performance_emissao(
        log,
        "Selecionando TOTAL DO PERIODO; quantidade retornada pelo Fraction: "
        f"{quantidade}.",
    )
    caminho_zip = pasta_zips / "Performance_Periodo.zip"
    try:
        with page.expect_download(
            timeout=TIMEOUT_DOWNLOAD_PERFORMANCE_MS
        ) as informacao_download:
            linha_total.get_by_role("gridcell").nth(1).click()
        download = informacao_download.value
    except PlaywrightTimeoutError as erro:
        raise TimeoutError(
            "O Fraction mostrou a quantidade de pedidos no TOTAL DO PERIODO, "
            "mas o clique nessa quantidade não iniciou o download ZIP."
        ) from erro

    download.save_as(caminho_zip)
    _log_performance_emissao(
        log,
        f"ZIP baixado e salvo como {caminho_zip.name}; "
        f"nome original: {download.suggested_filename}.",
    )
    return caminho_zip


def extrair_performance_intervalo(
    data_inicial: date,
    data_final: date,
    token: str,
    *,
    pasta_temporaria: Path | None = None,
    log=None,
) -> dict[str, object]:
    """Baixa e processa uma única Performance para o período selecionado."""
    if data_final < data_inicial:
        raise ValueError("A data final precisa ser igual ou posterior à inicial.")
    quantidade_dias = (data_final - data_inicial).days + 1
    if quantidade_dias > LIMITE_DIAS_PERFORMANCE_EMISSAO:
        raise ValueError(
            "O período da Performance pode ter no máximo "
            f"{LIMITE_DIAS_PERFORMANCE_EMISSAO} dias, contando as datas inicial "
            "e final."
        )
    if not token.strip():
        raise ValueError("Informe o Token MFA para consultar o Fraction.")

    identificador = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
    raiz_temporaria = (
        Path(pasta_temporaria)
        if pasta_temporaria is not None
        else Path(tempfile.mkdtemp(prefix="botevelog_performance_emissao_"))
    )
    pasta_execucao = raiz_temporaria / (
        f"extracao_{data_inicial:%Y-%m-%d}_{data_final:%Y-%m-%d}_{identificador}"
    )
    pasta_zips = pasta_execucao / "zips"
    pasta_planilhas = pasta_execucao / "planilhas"
    pasta_zips.mkdir(parents=True, exist_ok=True)
    pasta_planilhas.mkdir(parents=True, exist_ok=True)

    _log_performance_emissao(
        log,
        "Extração isolada da Performance em uma única consulta, "
        f"de {data_inicial:%d/%m/%Y} a {data_final:%d/%m/%Y}.",
    )

    tabelas: list[pd.DataFrame] = []
    arquivos_zip: list[str] = []
    planilhas_extraidas: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=HEADLESS,
            slow_mo=SLOW_MO_MS,
        )
        contexto = browser.new_context(accept_downloads=True)
        page = contexto.new_page()
        page.on(
            "pageerror",
            lambda erro: _log_performance_emissao(
                log,
                f"Erro JavaScript no Fraction: {erro}",
            ),
        )

        try:
            _login_performance_emissao(page, token, log=log)
            _abrir_tela_performance_emissao(page, log=log)
            _preencher_data_performance_emissao(
                page,
                SELETOR_DATA_INICIAL_PERFORMANCE,
                data_inicial,
            )
            _preencher_data_performance_emissao(
                page,
                SELETOR_DATA_FINAL_PERFORMANCE,
                data_final,
            )
            _log_performance_emissao(
                log,
                "Processando o relatório do período selecionado.",
            )
            page.get_by_role("button", name="Processar").click(
                timeout=120_000
            )
            caminho_zip = _baixar_performance_periodo(
                page,
                pasta_zips,
                log=log,
            )
            arquivos_zip.append(str(caminho_zip))

            partes_periodo = _extrair_planilhas_performance_zip(
                caminho_zip.read_bytes(),
                data_inicial,
                data_final,
                pasta_planilhas,
            )
            for (
                tabela_periodo,
                caminho_planilha,
                nome_aba,
                linha_cabecalho,
            ) in partes_periodo:
                tabelas.append(tabela_periodo)
                planilhas_extraidas.append(str(caminho_planilha))
                _log_performance_emissao(
                    log,
                    f"Planilha do período extraída como {caminho_planilha.name}; "
                    f"aba {nome_aba}, cabeçalho na linha {linha_cabecalho}, "
                    f"{len(tabela_periodo)} linha(s).",
                )

        finally:
            _log_performance_emissao(
                log,
                "Encerrando o navegador da extração de Performance.",
            )
            contexto.close()
            browser.close()

    if not tabelas:
        raise ValueError("Nenhuma planilha foi extraída para o período.")

    tabela_consolidada = pd.concat(tabelas, ignore_index=True)
    caminho_consolidado = pasta_execucao / (
        f"Performance_Emissao_{data_inicial:%Y-%m-%d}_"
        f"{data_final:%Y-%m-%d}.xlsx"
    )
    colunas_rota = ["UF", "Cidade", "Destino", "Rota"]
    with pd.ExcelWriter(caminho_consolidado, engine="openpyxl") as escritor:
        tabela_consolidada.to_excel(
            escritor,
            index=False,
            sheet_name="Performance",
        )
        tabela_consolidada[colunas_rota].to_excel(
            escritor,
            index=False,
            sheet_name="Rotas",
        )
    _log_performance_emissao(
        log,
        f"Consolidação salva em {caminho_consolidado}: "
        f"{len(tabela_consolidada)} linha(s) e abas Performance/Rotas, "
        f"{len(tabela_consolidada.columns)} coluna(s).",
    )

    return {
        "tabela": tabela_consolidada,
        "arquivo_consolidado": str(caminho_consolidado),
        "arquivos_zip": arquivos_zip,
        "planilhas_extraidas": planilhas_extraidas,
        "pasta_execucao": str(pasta_execucao),
    }
