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
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile

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
    carregar_regras_triagem_rotas,
    consultar_ctes_por_etiquetas_fraction,
    consultar_shipments_no_fraction,
    resolver_rota_por_localidade,
)
from utils.config import (
    ARQUIVO_ETIQUETAS_ROMANEIOS_ENTRADA,
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
REGISTRO_ETIQUETAS_ROMANEIOS_ENTRADA = RegistroTexto(
    ARQUIVO_ETIQUETAS_ROMANEIOS_ENTRADA
)


def _estado_etiquetas_romaneio(
    status_fila: str,
) -> tuple[Path, RegistroTexto]:
    status_normalizado = _normalizar_cabecalho(status_fila)
    if status_normalizado == "ENTRADA":
        return (
            ARQUIVO_ETIQUETAS_ROMANEIOS_ENTRADA,
            REGISTRO_ETIQUETAS_ROMANEIOS_ENTRADA,
        )
    return (
        ARQUIVO_ETIQUETAS_ROMANEIOS_EMISSAO,
        REGISTRO_ETIQUETAS_ROMANEIOS_EMISSAO,
    )


def preparar_estado_etiquetas_romaneio(status_fila: str = "EMISSAO") -> Path:
    """Cria a pasta e o arquivo de estado sem alterar etiquetas registradas."""
    caminho, _ = _estado_etiquetas_romaneio(status_fila)
    caminho.parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    caminho.touch(exist_ok=True)
    return caminho


def registrar_etiquetas_romaneio(
    etiquetas: list[str],
    *,
    contexto: str,
    status_fila: str = "EMISSAO",
    log: Callable[[str], None] | None = None,
    detalhar_etiquetas: bool = True,
) -> list[str]:
    """Persiste etiquetas de um romaneio confirmado para evitar reenvio."""
    caminho, registro = _estado_etiquetas_romaneio(status_fila)
    novas = registro.adicionar_varios(etiquetas)
    _log_performance_emissao(
        log,
        f"Estado de etiquetas atualizado para {contexto}: "
        f"{len(novas)} nova(s) etiqueta(s) registrada(s), "
        f"{len(etiquetas) - len(novas)} já constavam no estado. "
        f"Arquivo: {caminho}.",
    )
    if detalhar_etiquetas:
        for etiqueta in novas:
            _log_performance_emissao(log, f"Etiqueta persistida: {etiqueta}.")
    return novas


def _filtrar_etiquetas_ja_registradas(
    pedidos: list[dict[str, object]],
    *,
    status_fila: str = "EMISSAO",
    etiquetas_ja_conhecidas: set[str] | None = None,
    log: Callable[[str], None] | None = None,
) -> tuple[list[dict[str, object]], int]:
    pedidos_novos: list[dict[str, object]] = []
    total_ignoradas = 0
    etiquetas_ja_conhecidas = etiquetas_ja_conhecidas or set()
    caminho_estado, registro = _estado_etiquetas_romaneio(status_fila)
    _log_performance_emissao(
        log,
        f"Estado local {status_fila}: {len(registro)} etiqueta(s) já registradas "
        f"em {caminho_estado}.",
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
                if (
                    registro.contem(etiqueta)
                    or etiqueta in etiquetas_ja_conhecidas
                ):
                    registradas.append(etiqueta)
                else:
                    frescas.append(etiqueta)
            total_ignoradas += len(registradas)
            if registradas:
                _log_performance_emissao(
                    log,
                    f"Ignorando {len(registradas)} etiqueta(s) já conhecidas "
                    f"localmente: Palete '{palete}', ShipmentId "
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


def _adicionar_rota(
    tabela: pd.DataFrame,
    *,
    log: Callable[[str], None] | None = None,
) -> pd.DataFrame:
    regras_rotas = carregar_regras_triagem_rotas()
    resultado = tabela.copy()

    def resolver(linha: pd.Series) -> tuple[str, str]:
        uf = _normalizar_uf(linha.get("UF", ""))
        cidade = _normalizar_rota(_normalizar_valor(linha.get("Cidade", "")))
        destino = _normalizar_rota(_normalizar_valor(linha.get("Destino", "")))
        origem_palete = _normalizar_valor(linha.get("Palete de origem", ""))
        encontrado_palete = re.search(
            r"(?:^|[·•])\s*Palete\s+(.+?)\s*$",
            origem_palete,
            flags=re.IGNORECASE,
        )
        triagem_persistida = _normalizar_valor(linha.get("Triagem", ""))
        triagem_palete = triagem_persistida or (
            encontrado_palete.group(1).strip() if encontrado_palete else ""
        )
        rota, triagem = resolver_rota_por_localidade(
            regras_rotas,
            uf=uf,
            cidade=cidade,
            destino=destino,
            triagem=triagem_palete,
        )
        if triagem_palete and _normalizar_rota(rota) == "SEM ROTA":
            rota_atualizada, triagem_atualizada = resolver_rota_por_localidade(
                regras_rotas,
                uf=uf,
                cidade=cidade,
                destino=destino,
            )
            if (
                _normalizar_rota(rota_atualizada) not in {"SEM ROTA", "ROTA AMBIGUA"}
                and triagem_atualizada
            ):
                _log_performance_emissao(
                    log,
                    f"Triagem antiga '{triagem_palete}' não consta mais na base de "
                    f"rotas; pedido {linha.get('ShipmentId', '')} atualizado para "
                    f"'{triagem_atualizada}' pela cidade/destino.",
                )
                rota, triagem = rota_atualizada, triagem_atualizada
        if (
            not triagem_palete
            and not triagem
            and _normalizar_rota(rota) != "SEM ROTA"
        ):
            return "Rota ambígua", ""
        return rota, triagem

    resolvidos = resultado.apply(resolver, axis=1)
    resultado["Rota"] = resolvidos.map(lambda item: item[0])
    resultado["Triagem"] = resolvidos.map(lambda item: item[1])
    return resultado


def preparar_tabela_entrada_para_fila(
    tabela: pd.DataFrame,
    ctes_recebidos: list[str] | set[str],
    *,
    log: Callable[[str], None] | None = None,
) -> pd.DataFrame:
    """Prepara somente os pedidos recebidos para a fila de romaneios Entrada."""
    recebidos = {
        _normalizar_valor(cte)
        for cte in ctes_recebidos
        if _normalizar_valor(cte)
    }
    linhas: list[dict[str, object]] = []

    for _, linha in tabela.iterrows():
        cte = _normalizar_valor(linha.get("CTE", ""))
        shipment_id = _normalizar_shipment_id(linha.get("ShipmentId", ""))
        etiquetas = [
            valor.strip()
            for valor in str(linha.get("Etiquetas do pedido", "")).splitlines()
            if valor.strip() and valor.strip().lower() != "nan"
        ]
        ctes_entrada = [
            _normalizar_valor(valor)
            for valor in str(linha.get("CTEs para dar entrada", "")).splitlines()
            if _normalizar_valor(valor)
        ]
        if not cte or not shipment_id or not etiquetas or not ctes_entrada:
            continue

        if len(ctes_entrada) != len(etiquetas):
            if log is not None:
                log(
                    f"Pedido {shipment_id} não entrou na fila de romaneios: "
                    f"{len(etiquetas)} etiqueta(s) para {len(ctes_entrada)} "
                    "CTE(s) associados."
                )
            continue

        etiquetas_ctes_recebidos = [
            (etiqueta, cte_entrada)
            for etiqueta, cte_entrada in zip(etiquetas, ctes_entrada)
            if cte_entrada in recebidos
        ]
        if not etiquetas_ctes_recebidos:
            continue

        destino = _normalizar_valor(linha.get("Destino", ""))
        ctes_para_romaneio = list(dict.fromkeys(
            cte_entrada for _, cte_entrada in etiquetas_ctes_recebidos
        ))
        linhas.append(
            {
                "Rua": "",
                "Palete de origem": "Dar entrada em pedidos",
                "CTE": cte,
                "ShipmentId": shipment_id,
                "Etiquetas do pedido": "\n".join(
                    etiqueta for etiqueta, _ in etiquetas_ctes_recebidos
                ),
                "Volumes Sistema": None,
                "Volumes Etiquetas": len(etiquetas_ctes_recebidos),
                "Etiquetas faltantes": "",
                "CTEs para dar entrada": "\n".join(ctes_para_romaneio),
                "CTEs para romaneio": "\n".join(ctes_para_romaneio),
                "CTEs por etiqueta": "\n".join(
                    cte_entrada for _, cte_entrada in etiquetas_ctes_recebidos
                ),
                "UF": _normalizar_uf(linha.get("UF", "")),
                "Cidade": _normalizar_valor(linha.get("Cidade", "")),
                "Destino": destino,
                "Manifesto": _normalizar_valor(linha.get("Manifesto", "")),
                "Triagem": "",
                "Rota": "",
                "Origem": "Dar entrada em pedidos",
                "Status": "ENTRADA",
            }
        )

    colunas = [
        "Rua",
        "Palete de origem",
        "CTE",
        "ShipmentId",
        "Etiquetas do pedido",
        "Volumes Sistema",
        "Volumes Etiquetas",
        "Etiquetas faltantes",
        "CTEs para dar entrada",
        "CTEs para romaneio",
        "CTEs por etiqueta",
        "UF",
        "Cidade",
        "Destino",
        "Manifesto",
        "Triagem",
        "Rota",
        "Origem",
        "Status",
    ]
    preparada = pd.DataFrame(linhas, columns=colunas)
    if preparada.empty:
        if log is not None:
            log("Nenhum pedido confirmado foi preparado para a fila Entrada.")
        return preparada

    try:
        preparada = _adicionar_rota(preparada, log=log)
    except (FileNotFoundError, OSError, ValueError) as erro_rota:
        preparada["Rota"] = "Sem rota"
        preparada["Triagem"] = ""
        _log_performance_emissao(
            log,
            "A entrada foi mantida na fila, mas não foi possível resolver as rotas "
            f"agora: {type(erro_rota).__name__}: {erro_rota}. "
            "A configuração será reavaliada ao abrir a fila Entrada.",
        )
    if log is not None:
        ctes_romaneio = {
            cte.strip()
            for valor in preparada["CTEs para romaneio"].fillna("").astype(str)
            for cte in valor.splitlines()
            if cte.strip()
        }
        log(
            f"Fila Entrada preparada: {len(preparada)} pedido(s), "
            f"{sum(len(str(valor).splitlines()) for valor in preparada['Etiquetas do pedido'])} "
            f"etiqueta(s) e {len(ctes_romaneio)} CTE(s) para romaneio, "
            "agrupamento posterior por triagem."
        )
    return preparada


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
    resultado = _adicionar_rota(tabela, log=log)
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


def consultar_dados_performance_ctes(
    ctes: list[str],
    *,
    log: Callable[[str], None] | None = None,
    pasta_temporaria: Path | None = None,
) -> dict[str, dict[str, str]]:
    """Consulta Destino, Status, UF e Cidade atuais dos CTEs na Performance."""
    return _consultar_dados_performance_ctes(
        ctes,
        log=log,
        pasta_temporaria=pasta_temporaria,
    )


def montar_fila_emissao(
    conteudo_etiquetas: bytes,
    *,
    nome_arquivo_etiquetas: str = "etiquetas.txt",
    status_fila: str = "EMISSAO",
    etiquetas_ja_conhecidas: set[str] | None = None,
    log: Callable[[str], None] | None = None,
    pasta_temporaria: Path | None = None,
) -> dict[str, object]:
    """Cruza etiquetas no Fraction, seus CTEs e a Performance por CTE."""
    preparar_estado_etiquetas_romaneio(status_fila)
    status_alvo = _normalizar_cabecalho(status_fila)
    _log_performance_emissao(
        log,
        f"Arquivo de estado de etiquetas ({status_alvo}): "
        f"{preparar_estado_etiquetas_romaneio(status_alvo)}.",
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
    paletes_origem: set[str] = set()
    ruas_origem: set[str] = set()
    for pedido in pedidos:
        for palete, etiquetas_palete in pedido.get("ETIQUETAS_POR_PALETE", {}).items():
            rua = str(pedido.get("RUA_POR_PALETE", {}).get(palete, "")).strip()
            if not rua:
                raise ValueError(
                    f"Não identifiquei a rua do arquivo que contém o palete "
                    f"'{palete}'. Inclua 'Rua 1' (ou o número correspondente) "
                    "no nome/caminho do TXT dentro do ZIP ou no cabeçalho do TXT."
                )
            paletes_origem.add(str(palete))
            ruas_origem.add(rua)
    if paletes_origem:
        _log_performance_emissao(
            log,
            f"Origem validada: {len(pedidos)} ShipmentId(s), "
            f"{total_etiquetas_importadas} etiqueta(s) em "
            f"{len(paletes_origem)} palete(s) de {len(ruas_origem)} rua(s).",
        )

    pedidos, etiquetas_ja_registradas = _filtrar_etiquetas_ja_registradas(
        pedidos,
        status_fila=status_alvo,
        etiquetas_ja_conhecidas=etiquetas_ja_conhecidas,
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
            "shipment_ids_fora_fila": [],
            "etiquetas_ja_registradas": etiquetas_ja_registradas,
            "etiquetas_status_fora_fila": 0,
            "shipment_ids_etiquetas": 0,
            "total_etiquetas": 0,
        }

    etiquetas_por_shipment_id: dict[str, list[str]] = {}
    for etiqueta in etiquetas_novas:
        partes_etiqueta = PADRAO_ETIQUETA.fullmatch(etiqueta)
        if partes_etiqueta is None:
            continue
        etiquetas_por_shipment_id.setdefault(
            partes_etiqueta.group(1),
            [],
        ).append(etiqueta)

    shipment_ids = list(etiquetas_por_shipment_id)
    _log_performance_emissao(
        log,
        f"Consultando {len(shipment_ids)} ShipmentId(s) únicos no Envio de remessa "
        "do Fraction, usando os 14 primeiros dígitos das etiquetas.",
    )
    cte_por_shipment = consultar_ctes_por_etiquetas_fraction(
        shipment_ids,
        log=log,
        por_shipment_id=True,
    )
    cte_por_shipment = {
        str(shipment_id).strip(): _normalizar_valor(cte)
        for shipment_id, cte in cte_por_shipment.items()
        if _normalizar_valor(cte)
    }

    shipment_ids_sem_cte = [
        shipment_id
        for shipment_id in shipment_ids
        if shipment_id not in cte_por_shipment
    ]
    if shipment_ids_sem_cte:
        _log_performance_emissao(
            log,
            f"Tentando recuperar em lote as etiquetas de "
            f"{len(shipment_ids_sem_cte)} ShipmentId(s) sem Remessa/CTE.",
        )
        etiquetas_para_reconsulta = [
            etiqueta
            for shipment_id in shipment_ids_sem_cte
            for etiqueta in etiquetas_por_shipment_id.get(shipment_id, [])
        ]
        ctes_por_etiqueta_recuperados: dict[str, str] = {}
        try:
            ctes_por_etiqueta_recuperados = (
                consultar_ctes_por_etiquetas_fraction(
                    etiquetas_para_reconsulta,
                    log=log,
                    por_shipment_id=False,
                )
            )
        except Exception as erro_consulta_etiquetas:
            _log_performance_emissao(
                log,
                "Falha na reconsulta em lote das etiquetas sem CTE: "
                f"{type(erro_consulta_etiquetas).__name__}: "
                f"{erro_consulta_etiquetas}.",
            )

        shipments_recuperados_em_lote = 0
        shipments_ambiguos_em_lote = 0
        for shipment_id in shipment_ids_sem_cte:
            ctes_encontrados = list(dict.fromkeys(
                _normalizar_valor(
                    ctes_por_etiqueta_recuperados.get(etiqueta, "")
                )
                for etiqueta in etiquetas_por_shipment_id.get(shipment_id, [])
                if _normalizar_valor(
                    ctes_por_etiqueta_recuperados.get(etiqueta, "")
                )
            ))
            if len(ctes_encontrados) == 1:
                cte_por_shipment[shipment_id] = ctes_encontrados[0]
                shipments_recuperados_em_lote += 1
                _log_performance_emissao(
                    log,
                    f"Reconsulta em lote: ShipmentId {shipment_id} associado "
                    f"ao CTE {ctes_encontrados[0]}.",
                )
            elif len(ctes_encontrados) > 1:
                shipments_ambiguos_em_lote += 1
                _log_performance_emissao(
                    log,
                    f"Reconsulta em lote: ShipmentId {shipment_id} retornou "
                    "mais de um CTE e será ignorado por ambiguidade.",
                )
        _log_performance_emissao(
            log,
            f"Reconsulta em lote recuperou {shipments_recuperados_em_lote}/"
            f"{len(shipment_ids_sem_cte)} ShipmentId(s); "
            f"{shipments_ambiguos_em_lote} associação(ões) ambígua(s).",
        )

        shipment_ids_a_consultar = [
            shipment_id
            for shipment_id in shipment_ids_sem_cte
            if shipment_id not in cte_por_shipment
        ]
        if shipment_ids_a_consultar:
            _log_performance_emissao(
                log,
                f"A reconsulta em lote ainda deixou "
                f"{len(shipment_ids_a_consultar)} ShipmentId(s) sem CTE; "
                "acionando a consulta por ShipmentId e aproveitando somente o CTE.",
            )
            try:
                dados_shipments = consultar_shipments_no_fraction(
                    shipment_ids_a_consultar,
                    log=lambda mensagem: _log_performance_emissao(log, mensagem),
                )
            except Exception as erro_consulta_shipments:
                _log_performance_emissao(
                    log,
                    "Falha na consulta alternativa por ShipmentId: "
                    f"{type(erro_consulta_shipments).__name__}: "
                    f"{erro_consulta_shipments}.",
                )
                dados_shipments = {}

            shipments_recuperados_por_consulta = 0
            for shipment_id in shipment_ids_a_consultar:
                dados = dados_shipments.get(shipment_id, {})
                cte = _normalizar_valor(dados.get("CTE", ""))
                if not cte:
                    continue
                cte_por_shipment[shipment_id] = cte
                shipments_recuperados_por_consulta += 1
                _log_performance_emissao(
                    log,
                    f"Consulta por ShipmentId recuperou o CTE {cte} para "
                    f"{shipment_id}; os volumes desta consulta foram descartados.",
                )
            _log_performance_emissao(
                log,
                f"Consulta por ShipmentId recuperou CTE para "
                f"{shipments_recuperados_por_consulta}/"
                f"{len(shipment_ids_a_consultar)} pedido(s).",
            )

    cte_por_etiqueta = {
        etiqueta: cte_por_shipment[shipment_id]
        for shipment_id, etiquetas_do_shipment in etiquetas_por_shipment_id.items()
        if shipment_id in cte_por_shipment
        for etiqueta in etiquetas_do_shipment
    }
    etiquetas_sem_cte = [
        etiqueta for etiqueta in etiquetas_novas if etiqueta not in cte_por_etiqueta
    ]
    if etiquetas_sem_cte:
        etiquetas_ausentes = set(etiquetas_sem_cte)
        shipments_sem_cte = {
            match.group(1)
            for etiqueta in etiquetas_sem_cte
            if (match := PADRAO_ETIQUETA.fullmatch(etiqueta)) is not None
        }
        _log_performance_emissao(
            log,
            f"O Fraction não retornou Remessa/CTE para {len(etiquetas_sem_cte)} "
            f"etiqueta(s) de {len(shipments_sem_cte)} ShipmentId(s), nem na "
            "consulta em lote nem na tentativa isolada; elas serão ignoradas e as "
            "correspondências encontradas seguirão.",
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
            "shipment_ids_fora_fila": [],
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
    total_linhas_previstas = sum(
        len(pedido.get("ETIQUETAS_POR_PALETE", {})) for pedido in pedidos
    )
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
            if len(linhas) % 100 == 0 or len(linhas) == total_linhas_previstas:
                _log_performance_emissao(
                    log,
                    f"Montagem da fila: {len(linhas)}/{total_linhas_previstas} "
                    "pedido(s) preparados.",
                )

    tabela_completa = pd.DataFrame(linhas)
    tabela_completa = _adicionar_rota(tabela_completa)
    tabela_completa = _completar_rotas_por_rua_triagem(tabela_completa, log=log)
    status_alvo_boleano = tabela_completa["Status"].map(
        lambda valor: _normalizar_cabecalho(valor) == status_alvo
    )
    etiquetas_fora_fila: list[str] = []
    quantidade_pedidos_fora_fila = int((~status_alvo_boleano).sum())
    pedidos_alvo_classificados = 0
    for indice, (_, linha) in enumerate(tabela_completa.iterrows()):
        vai_para_fila = bool(status_alvo_boleano.iloc[indice])
        pedidos_alvo_classificados += int(vai_para_fila)
        etiquetas = [
            etiqueta.strip()
            for etiqueta in str(linha["Etiquetas do pedido"]).splitlines()
            if etiqueta.strip()
        ]
        if not vai_para_fila and etiquetas:
            etiquetas_fora_fila.extend(etiquetas)

        quantidade_classificada = indice + 1
        if (
            quantidade_classificada % 100 == 0
            or quantidade_classificada == len(tabela_completa)
        ):
            _log_performance_emissao(
                log,
                f"Classificação por Status: {quantidade_classificada}/"
                f"{len(tabela_completa)} pedido(s); "
                f"{pedidos_alvo_classificados} "
                f"com Status final {status_alvo}.",
            )

    _log_performance_emissao(
        log,
        f"Classificação concluída: {int(status_alvo_boleano.sum())} pedido(s) "
        f"{status_alvo} e {quantidade_pedidos_fora_fila} fora da fila.",
    )
    etiquetas_status_fora_fila = 0
    if status_alvo == "EMISSAO":
        novas_etiquetas_fora_fila = registrar_etiquetas_romaneio(
            etiquetas_fora_fila,
            contexto="pedidos fora da fila por Status final diferente de EMISSAO",
            status_fila=status_alvo,
            log=log,
            detalhar_etiquetas=False,
        )
        etiquetas_status_fora_fila = len(novas_etiquetas_fora_fila)

    tabela_completa = tabela_completa.reset_index(drop=True)
    tabela = tabela_completa.loc[status_alvo_boleano].reset_index(drop=True)
    shipment_ids_fora_fila = list(dict.fromkeys(
        tabela_completa.loc[~status_alvo_boleano, "ShipmentId"].astype(str).tolist()
    ))
    if log is not None:
        log(
            f"Etiquetas dos TXT: {total_etiquetas_importadas} lida(s), "
            f"{etiquetas_ja_registradas} já estavam registradas, "
            f"{len(etiquetas_novas) - len(etiquetas_sem_cte)} com Remessa/CTE, "
            f"{len(etiquetas_sem_cte)} sem retorno e ignorada(s), "
            f"{etiquetas_status_fora_fila} registrada(s) por status fora da fila; "
            f"{len(tabela)} linha(s) {status_alvo} mantida(s) em "
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
                f"Status na Performance e não entraram na fila {status_alvo}."
            )

    return {
        "tabela": tabela,
        "tabela_completa": tabela_completa,
        "etiquetas_sem_retorno_fraction": etiquetas_sem_cte,
        "shipment_ids_sem_retorno_fraction": list(dict.fromkeys(shipment_ids_sem_retorno)),
        "shipment_ids_sem_destino_performance": list(dict.fromkeys(shipment_ids_sem_destino)),
        "shipment_ids_sem_status_performance": list(dict.fromkeys(shipment_ids_sem_status)),
        "shipment_ids_fora_emissao": (
            shipment_ids_fora_fila if status_alvo == "EMISSAO" else []
        ),
        "shipment_ids_fora_fila": shipment_ids_fora_fila,
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
            try:
                if controle.is_visible() and controle.is_enabled():
                    return controle
            except Exception:
                # O Drive redesenha os menus durante a seleção; descarte
                # controles que ficaram obsoletos e continue procurando.
                continue

    # Em alguns layouts do Drive, os botões da barra de ações têm apenas
    # aria-label/title/data-tooltip e não são expostos com nome acessível.
    controles_rotulados = page.locator(
        "[aria-label], [title], [data-tooltip]"
    )
    for indice in range(controles_rotulados.count()):
        controle = controles_rotulados.nth(indice)
        try:
            rotulos = " ".join(
                str(controle.get_attribute(atributo) or "")
                for atributo in ("aria-label", "title", "data-tooltip")
            )
            if (
                padrao.search(rotulos)
                and controle.is_visible()
                and controle.is_enabled()
            ):
                return controle
        except Exception:
            continue
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
    nome_linha_selecionada: str | None = None,
):
    # Procura primeiro dentro da barra que aparece após a seleção. Isso evita
    # acionar um Download da pasta inteira, fora da seleção de arquivos.
    barras = page.get_by_role("toolbar")
    for indice in range(barras.count()):
        barra = barras.nth(indice)
        try:
            if not barra.is_visible():
                continue
            acao_download = _localizar_acao_drive(barra, padrao_download)
            if acao_download is not None:
                return acao_download
            mais_acoes = _localizar_acao_drive(barra, padrao_mais_acoes)
            if mais_acoes is not None:
                mais_acoes.click(timeout=10_000)
                page.wait_for_timeout(300)
                menus = page.get_by_role("menu")
                for indice_menu in range(menus.count()):
                    menu = menus.nth(indice_menu)
                    if menu.is_visible():
                        acao_download = _localizar_acao_drive(menu, padrao_download)
                        if acao_download is not None:
                            return acao_download
                page.keyboard.press("Escape")
        except Exception:
            continue

    def localizar_download_no_menu():
        menus = page.get_by_role("menu")
        for indice_menu in range(menus.count()):
            menu = menus.nth(indice_menu)
            try:
                if menu.is_visible():
                    acao = _localizar_acao_drive(menu, padrao_download)
                    if acao is not None:
                        return acao
            except Exception:
                continue
        itens = page.get_by_role("menuitem", name=padrao_download)
        for indice_item in range(itens.count()):
            item = itens.nth(indice_item)
            try:
                if item.is_visible() and item.is_enabled():
                    return item
            except Exception:
                continue
        return None

    # Se não houver botão na barra, abre o menu do conjunto selecionado.
    try:
        page.keyboard.press("Shift+F10")
        page.wait_for_timeout(300)
        acao_download = localizar_download_no_menu()
        if acao_download is not None:
            return acao_download
        page.keyboard.press("Escape")
    except Exception:
        pass

    if nome_linha_selecionada:
        linha_selecionada, _ = _localizar_linha_arquivo_drive(
            page,
            nome_linha_selecionada,
        )
        if linha_selecionada is not None:
            try:
                linha_selecionada.click(
                    button="right",
                    timeout=5_000,
                    force=True,
                )
                page.wait_for_timeout(300)
                return localizar_download_no_menu()
            except Exception:
                pass
    return None


def _localizar_download_pasta_drive(
    page,
    padrao_download: re.Pattern,
    padrao_mais_acoes: re.Pattern,
):
    """Localiza a ação de baixar a pasta inteira sem selecionar linhas."""
    barras = page.get_by_role("toolbar")
    for indice in range(barras.count()):
        barra = barras.nth(indice)
        try:
            if not barra.is_visible():
                continue
            acao_download = _localizar_acao_drive(barra, padrao_download)
            if acao_download is not None:
                return acao_download
            mais_acoes = _localizar_acao_drive(barra, padrao_mais_acoes)
            if mais_acoes is not None:
                mais_acoes.click(timeout=10_000)
                page.wait_for_timeout(300)
                menus = page.get_by_role("menu")
                for indice_menu in range(menus.count()):
                    menu = menus.nth(indice_menu)
                    if menu.is_visible():
                        acao_download = _localizar_acao_drive(
                            menu,
                            padrao_download,
                        )
                        if acao_download is not None:
                            return acao_download
                page.keyboard.press("Escape")
        except Exception:
            continue

    # Link compartilhado e layouts compactos podem expor o comando fora de uma
    # toolbar acessível. Sem uma linha em hover, o Download visível é da pasta.
    acao_download = _localizar_acao_drive(page, padrao_download)
    if acao_download is not None:
        return acao_download

    mais_acoes = _localizar_acao_drive(page, padrao_mais_acoes)
    if mais_acoes is not None:
        mais_acoes.click(timeout=10_000)
        page.wait_for_timeout(300)
        menus = page.get_by_role("menu")
        for indice_menu in range(menus.count()):
            menu = menus.nth(indice_menu)
            try:
                if menu.is_visible():
                    acao_download = _localizar_acao_drive(menu, padrao_download)
                    if acao_download is not None:
                        return acao_download
            except Exception:
                continue
    return None


def _resumir_acoes_drive_visiveis(page, limite: int = 30) -> list[str]:
    """Retorna rótulos de controles visíveis para registrar falhas de UI do Drive."""
    rotulos: list[str] = []
    controles = page.locator(
        'button, [role="button"], [role="menuitem"], a[href], '
        "[aria-label], [title], [data-tooltip]"
    )
    for indice in range(min(controles.count(), 250)):
        controle = controles.nth(indice)
        try:
            if not controle.is_visible():
                continue
            partes = [
                str(controle.get_attribute(atributo) or "").strip()
                for atributo in ("aria-label", "title", "data-tooltip")
            ]
            texto = " ".join(partes + [controle.inner_text().strip()]).strip()
            texto = " ".join(texto.split())
            if texto and texto not in rotulos:
                rotulos.append(texto[:120])
                if len(rotulos) >= limite:
                    break
        except Exception:
            continue
    return rotulos


def _localizar_linha_arquivo_drive(page, nome_arquivo: str):
    """Reconsulta as linhas visíveis pelo nome para tolerar redesenhos do Drive."""
    grades = page.get_by_role("grid")
    for indice_grade in range(grades.count()):
        grade = grades.nth(indice_grade)
        try:
            if not grade.is_visible():
                continue
            linhas = grade.get_by_role("row")
            for indice_linha in range(linhas.count()):
                linha = linhas.nth(indice_linha)
                celulas = linha.get_by_role("gridcell")
                if not celulas.count():
                    continue
                primeiro_texto = celulas.first.inner_text().strip()
                if primeiro_texto.splitlines()[0].strip() == nome_arquivo:
                    return linha, celulas
        except Exception:
            # Uma re-renderização entre count() e inner_text() é normal no Drive.
            continue
    return None, None


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


def _nome_arquivo_contem_termo(nome_arquivo: str, termo: str) -> bool:
    normalizado = _normalizar_nome_arquivo(nome_arquivo)
    termo_normalizado = _normalizar_nome_arquivo(termo)
    return bool(termo_normalizado) and termo_normalizado in normalizado


def _etiquetas_validas_do_txt(conteudo: bytes) -> set[str]:
    """Extrai códigos de etiqueta para detectar TXT importados incompletos."""
    texto = conteudo.decode("utf-8-sig", errors="ignore")
    etiquetas: set[str] = set()
    for linha in texto.splitlines():
        etiqueta = linha.strip()
        correspondencia = PADRAO_ETIQUETA.fullmatch(etiqueta)
        if correspondencia is not None and correspondencia.group(2) != "000":
            etiquetas.add(etiqueta)
    return etiquetas


def _normalizar_nome_arquivo(nome_arquivo: str) -> str:
    nome = PurePosixPath(str(nome_arquivo).replace("\\", "/")).name
    normalizado = unicodedata.normalize("NFKD", nome)
    return "".join(
        caractere
        for caractere in normalizado
        if not unicodedata.combining(caractere)
    ).casefold()


def baixar_etiquetas_drive(
    *,
    pasta_destino: Path | None = None,
    termo_nome: str = "Transferência",
    log=None,
) -> Path | None:
    """Baixa todos os TXT do fluxo; a deduplicação é feita localmente depois."""
    if not URL_DRIVE:
        raise RuntimeError("Configure URL_DRIVE no arquivo bot/.env.")
    if not str(termo_nome).strip():
        raise ValueError("Informe o termo que deve aparecer no nome dos TXT.")

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

    _log_performance_emissao(
        log,
        f"Sincronização em lote ativa: vou baixar todos os TXT com "
        f"'{termo_nome}' no nome; os já salvos serão descartados localmente.",
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
                grades = page.get_by_role("grid")
                pasta_aberta = False
                for tentativa_abertura in range(1, 3):
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
                    try:
                        grades.first.wait_for(state="visible", timeout=30_000)
                        pasta_aberta = True
                        break
                    except PlaywrightTimeoutError:
                        if tentativa_abertura < 2:
                            _log_performance_emissao(
                                log,
                                "A lista da pasta não apareceu após fechar a prévia; "
                                "reabrindo o link do Drive nesta sessão.",
                            )
                if not pasta_aberta:
                    raise RuntimeError(
                        "O link abriu, mas a pasta do Google Drive não exibiu "
                        "a lista de arquivos após duas tentativas de abertura."
                    )

                _log_performance_emissao(
                    log,
                    "Pasta do Drive carregada; baixando o conteúdo completo em "
                    "um único ZIP, sem selecionar as linhas uma a uma.",
                )
                acao_download = _localizar_download_pasta_drive(
                    page,
                    padrao_download,
                    padrao_mais_acoes,
                )
                selecao_total_acionada = False
                nome_primeira_linha: str | None = None
                if acao_download is None:
                    grade_visivel = next(
                        (
                            grades.nth(indice)
                            for indice in range(grades.count())
                            if grades.nth(indice).is_visible()
                        ),
                        None,
                    )
                    if grade_visivel is None:
                        raise RuntimeError(
                            "A lista do Drive sumiu antes de acionar o download."
                        )
                    linhas = grade_visivel.get_by_role("row")
                    for indice_linha in range(linhas.count()):
                        linha = linhas.nth(indice_linha)
                        celulas = linha.get_by_role("gridcell")
                        if not celulas.count():
                            continue
                        texto_linha = celulas.first.inner_text().strip()
                        if not texto_linha:
                            continue
                        nome_primeira_linha = texto_linha.splitlines()[0].strip()
                        try:
                            linha.hover(timeout=3_000)
                        except PlaywrightTimeoutError:
                            pass
                        _selecionar_linha_arquivo_drive(
                            linha,
                            celulas,
                            ja_selecionados=0,
                        )
                        break
                    if not nome_primeira_linha:
                        raise RuntimeError(
                            "A pasta do Google Drive está vazia; não há arquivos para baixar."
                        )
                    page.keyboard.press("Control+A")
                    page.wait_for_timeout(400)
                    selecao_total_acionada = True
                    _log_performance_emissao(
                        log,
                        "O botão da pasta não apareceu; usei Ctrl+A uma única vez "
                        "para selecionar todos os itens.",
                    )
                    acao_download = _localizar_download_drive_apos_selecao(
                        page,
                        padrao_download,
                        padrao_mais_acoes,
                        nome_linha_selecionada=nome_primeira_linha,
                    )
                if acao_download is None:
                    acoes_visiveis = _resumir_acoes_drive_visiveis(page)
                    _log_performance_emissao(
                        log,
                        "Ações visíveis no Drive ao baixar a pasta: "
                        + (" | ".join(acoes_visiveis) if acoes_visiveis else "nenhuma"),
                    )
                    raise RuntimeError(
                        "Não localizei a ação para baixar o conteúdo completo da pasta."
                    )

                _log_performance_emissao(
                    log,
                    "Ação de download da pasta localizada; solicitando um único ZIP. "
                    "A filtragem de TXT e arquivos já salvos será local.",
                )
                try:
                    with page.expect_download(
                        timeout=TIMEOUT_DOWNLOAD_DRIVE_MS
                    ) as informacao_download:
                        acao_download.click(timeout=20_000)
                    download = informacao_download.value
                except PlaywrightTimeoutError:
                    page.keyboard.press("Escape")
                    if selecao_total_acionada:
                        acao_download = _localizar_download_drive_apos_selecao(
                            page,
                            padrao_download,
                            padrao_mais_acoes,
                            nome_linha_selecionada=nome_primeira_linha,
                        )
                    else:
                        acao_download = _localizar_download_pasta_drive(
                            page,
                            padrao_download,
                            padrao_mais_acoes,
                        )
                    if acao_download is None:
                        raise RuntimeError(
                            "A ação de download da pasta desapareceu antes de iniciar."
                        )
                    _log_performance_emissao(
                        log,
                        "O clique normal não iniciou o download; repetindo a ação "
                        "da pasta com clique forçado.",
                    )
                    with page.expect_download(
                        timeout=TIMEOUT_DOWNLOAD_DRIVE_MS
                    ) as informacao_download:
                        acao_download.click(timeout=20_000, force=True)
                    download = informacao_download.value

                nome_download = str(download.suggested_filename or "").strip()
                with tempfile.TemporaryDirectory(
                    prefix="botevelog_drive_lote_",
                    dir=pasta_destino,
                ) as diretorio_download:
                    if nome_download.casefold().endswith(".zip"):
                        caminho_zip_drive = Path(diretorio_download) / "drive.zip"
                        download.save_as(caminho_zip_drive)
                    else:
                        if not nome_download.casefold().endswith(".txt"):
                            raise ValueError(
                                "O Drive retornou um formato inesperado ao baixar a "
                                f"pasta: '{nome_download}'."
                            )
                        caminho_txt_drive = Path(diretorio_download) / "drive.txt"
                        download.save_as(caminho_txt_drive)
                        nome_base_txt = PurePosixPath(
                            nome_download.replace("\\", "/")
                        ).name
                        caminho_zip_drive = Path(diretorio_download) / "drive.zip"
                        with ZipFile(
                            caminho_zip_drive,
                            mode="w",
                            compression=ZIP_DEFLATED,
                        ) as zip_drive:
                            zip_drive.write(caminho_txt_drive, arcname=nome_base_txt)

                    quantidade_txt_drive = 0
                    quantidade_txt_fluxo = 0
                    try:
                        with ZipFile(caminho_zip_drive) as zip_drive:
                            with ZipFile(
                                caminho_zip,
                                mode="w",
                                compression=ZIP_DEFLATED,
                            ) as zip_filtrado:
                                for membro in zip_drive.infolist():
                                    if membro.is_dir() or PurePosixPath(
                                        membro.filename.replace("\\", "/")
                                    ).suffix.casefold() != ".txt":
                                        continue
                                    quantidade_txt_drive += 1
                                    if not _nome_arquivo_contem_termo(
                                        membro.filename,
                                        termo_nome,
                                    ):
                                        continue
                                    nome_base = PurePosixPath(
                                        membro.filename.replace("\\", "/")
                                    ).name
                                    if not nome_base:
                                        continue
                                    zip_filtrado.writestr(
                                        nome_base,
                                        zip_drive.read(membro),
                                    )
                                    quantidade_txt_fluxo += 1
                    except BadZipFile as erro:
                        raise ValueError(
                            "O Google Drive não retornou um ZIP válido para a pasta."
                        ) from erro

                if quantidade_txt_fluxo == 0:
                    caminho_zip.unlink(missing_ok=True)
                    _log_performance_emissao(
                        log,
                        f"O ZIP completo contém {quantidade_txt_drive} TXT(s), "
                        f"mas nenhum nome contém '{termo_nome}'.",
                    )
                    return None

                try:
                    with ZipFile(caminho_zip) as arquivo_zip:
                        membros_txt = [
                            membro
                            for membro in arquivo_zip.infolist()
                            if not membro.is_dir()
                            and PurePosixPath(membro.filename).suffix.casefold()
                            == ".txt"
                        ]
                        if len(membros_txt) != quantidade_txt_fluxo:
                            raise ValueError(
                                "O ZIP filtrado localmente ficou incompleto: "
                                f"{len(membros_txt)} de {quantidade_txt_fluxo} TXT(s)."
                            )
                        if not membros_txt:
                            raise ValueError(
                                "A pasta do Drive não contém TXT do fluxo selecionado."
                            )
                except BadZipFile as erro:
                    raise ValueError(
                        "O ZIP local filtrado com os TXT do Drive ficou inválido."
                    ) from erro

                _log_performance_emissao(
                    log,
                    f"Download completo recebido como '{nome_download}': "
                    f"{quantidade_txt_drive} TXT(s) na pasta; "
                    f"{quantidade_txt_fluxo} TXT(s) com '{termo_nome}' filtrado(s) "
                    f"localmente em {caminho_zip.name}.",
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
    etiquetas_ja_conhecidas: set[str] | None = None,
    termo_nome: str = "Transferência",
    log: Callable[[str], None] | None = None,
) -> tuple[bytes, list[dict[str, str]]]:
    """Seleciona TXT novos e reavalia arquivos importados com etiqueta pendente."""
    buffer_novos = BytesIO()
    novos: list[dict[str, str]] = []
    vistos_nesta_sincronizacao: set[str] = set()
    arquivos_reavaliados = 0
    etiquetas_pendentes_reavaliadas = 0
    etiquetas_conhecidas = {
        str(etiqueta).strip()
        for etiqueta in (etiquetas_ja_conhecidas or set())
        if str(etiqueta).strip()
    }

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

            membros_correspondentes = []
            for membro in membros_txt:
                if _nome_arquivo_contem_termo(membro.filename, termo_nome):
                    membros_correspondentes.append(membro)
                else:
                    _log_performance_emissao(
                        log,
                        f"TXT ignorado por não conter '{termo_nome}' no nome: "
                        f"{PurePosixPath(membro.filename).as_posix()}.",
                    )

            if not membros_correspondentes:
                _log_performance_emissao(
                    log,
                    f"Nenhum TXT com '{termo_nome}' no nome foi encontrado no ZIP.",
                )
                return buffer_novos.getvalue(), novos

            with ZipFile(buffer_novos, mode="w") as zip_novos:
                for membro in membros_correspondentes:
                    conteudo = arquivo_zip.read(membro)
                    digest = sha256(conteudo).hexdigest()
                    caminho_normalizado = PurePosixPath(membro.filename).as_posix()
                    chave = sha256(
                        caminho_normalizado.encode("utf-8") + b"\0" + digest.encode("ascii")
                    ).hexdigest()
                    chave_estavel = sha256(
                        _normalizar_nome_arquivo(caminho_normalizado).encode("utf-8")
                        + b"\0"
                        + digest.encode("ascii")
                    ).hexdigest()
                    chave_nome_conteudo = sha256(
                        _normalizar_nome_arquivo(PurePosixPath(caminho_normalizado).name).encode("utf-8")
                        + b"\0"
                        + digest.encode("ascii")
                    ).hexdigest()
                    if (
                        chave in vistos_nesta_sincronizacao
                        or chave_estavel in vistos_nesta_sincronizacao
                        or chave_nome_conteudo in vistos_nesta_sincronizacao
                    ):
                        _log_performance_emissao(
                            log,
                            f"TXT duplicado dentro do ZIP; ignorando: "
                            f"{caminho_normalizado}.",
                        )
                        continue

                    ja_importado = (
                        chave in arquivos_importados
                        or chave_estavel in arquivos_importados
                        or chave_nome_conteudo in arquivos_importados
                    )
                    reavaliado = False
                    if ja_importado:
                        if etiquetas_ja_conhecidas is None:
                            _log_performance_emissao(
                                log,
                                f"TXT já sincronizado; ignorando: "
                                f"{caminho_normalizado}.",
                            )
                            continue
                        etiquetas_pendentes = (
                            _etiquetas_validas_do_txt(conteudo)
                            - etiquetas_conhecidas
                        )
                        if not etiquetas_pendentes:
                            _log_performance_emissao(
                                log,
                                f"TXT já sincronizado e sem etiquetas pendentes; "
                                f"ignorando: {caminho_normalizado}.",
                            )
                            continue
                        reavaliado = True
                        arquivos_reavaliados += 1
                        etiquetas_pendentes_reavaliadas += len(etiquetas_pendentes)
                        _log_performance_emissao(
                            log,
                            f"TXT importado reavaliado: {caminho_normalizado}; "
                            f"{len(etiquetas_pendentes)} etiqueta(s) ainda não "
                            "constam na fila nem no histórico.",
                        )

                    zip_novos.writestr(membro, conteudo)
                    vistos_nesta_sincronizacao.add(chave)
                    vistos_nesta_sincronizacao.add(chave_estavel)
                    vistos_nesta_sincronizacao.add(chave_nome_conteudo)
                    novos.append(
                        {
                            "chave": chave,
                            "nome": PurePosixPath(caminho_normalizado).name,
                            "caminho_no_zip": caminho_normalizado,
                            "sha256": digest,
                        }
                    )
                    if not reavaliado:
                        _log_performance_emissao(
                            log,
                            f"TXT novo detectado: {caminho_normalizado} "
                            f"({len(conteudo)} bytes; SHA-256 {digest}).",
                        )
    except BadZipFile as erro:
        raise ValueError("O conteúdo baixado do Drive não é um ZIP válido.") from erro

    if log is not None:
        log(
            f"Sincronização do ZIP: {len(novos) - arquivos_reavaliados} TXT(s) "
            f"novo(s), {arquivos_reavaliados} TXT(s) reavaliado(s) por "
            f"{etiquetas_pendentes_reavaliadas} etiqueta(s) sem registro; "
            f"{len(membros_correspondentes)} TXT(s) com '{termo_nome}' encontrado(s)."
        )
    return buffer_novos.getvalue(), novos
