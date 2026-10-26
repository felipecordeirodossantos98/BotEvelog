from __future__ import annotations

from decimal import Decimal, InvalidOperation
from io import BytesIO
import re
import unicodedata
from typing import Callable

import pandas as pd

from automacoes.baixar_relatorios_performance import executar_extracao
from automacoes.dar_entrada_pedidos import (
    COLUNAS_RESULTADO,
    PADRAO_ETIQUETA,
    agrupar_etiquetas_importadas,
    consultar_shipments_no_fraction,
    gerar_ctes_para_entrada,
    gerar_etiquetas_faltantes,
)
from automacoes.gerar_romaneios import carregar_rotas_destino_origem


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
        }
    )
    dados = dados[dados["ShipmentId"] != ""].reset_index(drop=True)
    return dados, nome_aba, indice_cabecalho + 1


def _adicionar_rota(tabela: pd.DataFrame) -> pd.DataFrame:
    mapa_rotas, destinos_ambiguos = carregar_rotas_destino_origem()
    resultado = tabela.copy()

    def resolver(destino: object) -> str:
        chave = _normalizar_rota(destino)
        if chave in destinos_ambiguos:
            return "Rota ambígua"
        return mapa_rotas.get(chave, "Sem rota")

    resultado["Rota"] = resultado["Destino"].map(resolver)
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
) -> tuple[dict[str, str], dict[str, str], dict[str, str], dict[str, str]]:
    """Indexa Destino e Status do relatório por CTE e ShipmentId."""
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

    destinos_por_cte: dict[str, str] = {}
    destinos_por_shipment: dict[str, str] = {}
    status_por_cte: dict[str, str] = {}
    status_por_shipment: dict[str, str] = {}
    ctes_ambiguos: set[str] = set()
    shipments_ambiguos: set[str] = set()
    ctes_status_ambiguos: set[str] = set()
    shipments_status_ambiguos: set[str] = set()
    for _, linha in dados.iterrows():
        destino = _normalizar_valor(linha[coluna_destino])
        cte = _normalizar_valor(linha[coluna_cte]) if coluna_cte is not None else ""
        status = (
            _normalizar_valor(linha[coluna_status])
            if coluna_status is not None
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

    return (
        destinos_por_cte,
        destinos_por_shipment,
        status_por_cte,
        status_por_shipment,
    )


def _consultar_dados_performance_shipmentids(
    ctes_por_shipment: dict[str, str],
    *,
    log: Callable[[str], None] | None,
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
        resultado = executar_extracao(escrever_relatorio, log=registrar_log)
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
        if destino or status:
            dados_por_shipment[shipment_id] = {
                "Destino": destino,
                "Status": status,
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
) -> dict[str, object]:
    """Cruza etiquetas e Performance e monta a fila de envio dos romaneios."""
    base, nome_aba, linha_cabecalho = _ler_base_performance(conteudo_performance)
    pedidos = agrupar_etiquetas_importadas(
        conteudo_etiquetas,
        nome_arquivo_etiquetas,
        log=log,
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
            }
        )

    linhas: list[dict[str, object]] = []
    shipment_ids_sem_match: list[str] = []
    linhas_por_shipment: dict[str, dict[str, object]] = {}
    resultados_consulta: dict[str, dict[str, object]] = {}
    dados_performance_consultados: dict[str, dict[str, str]] = {}

    for pedido in pedidos:
        shipment_id = str(pedido["ShipmentId"])
        etiquetas = sorted(
            pedido["ETIQUETAS"],
            key=lambda etiqueta: int(PADRAO_ETIQUETA.fullmatch(etiqueta).group(2)),
        )
        candidatos = registros_por_shipment.get(shipment_id, [])
        if not candidatos:
            shipment_ids_sem_match.append(shipment_id)
            continue

        registros_unicos = {
            (
                str(registro["Codigo"]),
                str(registro["Destino"]),
                _normalizar_valor(registro["Volume"]),
            ): registro
            for registro in candidatos
        }
        if len(registros_unicos) > 1:
            raise ValueError(
                f"ShipmentId {shipment_id} aparece com mais de um Codigo, "
                "Destino ou Volume na planilha Performance. Corrija a base "
                "antes de montar a fila."
            )

        registro = next(iter(registros_unicos.values()))
        codigo = str(registro["Codigo"]).strip()
        destino = str(registro["Destino"]).strip()
        if not codigo or not destino:
            raise ValueError(
                f"A linha correspondente ao ShipmentId {shipment_id} precisa "
                "ter Codigo e Destino preenchidos na planilha Performance."
            )

        volume_sistema = _normalizar_volume(
            registro["Volume"],
            shipment_id=shipment_id,
        )
        linhas_por_shipment[shipment_id] = {
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
            "CTEs para dar entrada": gerar_ctes_para_entrada(
                codigo,
                etiquetas,
                volume_sistema,
            ),
            "Destino": destino,
            "Manifesto": "",
            "Origem": "Base",
            "Status": _normalizar_valor(registro["Status"]),
        }

    if shipment_ids_sem_match:
        log_consulta = log if log is not None else (lambda _mensagem: None)
        resultados_consulta = consultar_shipments_no_fraction(
            shipment_ids_sem_match,
            log=log_consulta,
        )
        for pedido in pedidos:
            shipment_id = str(pedido["ShipmentId"])
            if shipment_id not in shipment_ids_sem_match:
                continue

            etiquetas = sorted(
                pedido["ETIQUETAS"],
                key=lambda etiqueta: int(
                    PADRAO_ETIQUETA.fullmatch(etiqueta).group(2)
                ),
            )
            consulta = resultados_consulta.get(shipment_id, {})
            codigo = _normalizar_valor(consulta.get("CTE"))
            volume_sistema_bruto = consulta.get("Volumes Sistema")
            volume_sistema = (
                _normalizar_volume(volume_sistema_bruto, shipment_id=shipment_id)
                if volume_sistema_bruto is not None
                and not pd.isna(volume_sistema_bruto)
                else None
            )
            ctes_para_entrada = (
                gerar_ctes_para_entrada(codigo, etiquetas, volume_sistema)
                if codigo
                else ""
            )
            linhas_por_shipment[shipment_id] = {
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
                "Destino": "",
                "Manifesto": "",
                "Origem": "Consulta",
                "Status": "",
            }


    shipment_ids_fila = [
        str(pedido["ShipmentId"])
        for pedido in pedidos
        if str(pedido["ShipmentId"]) in linhas_por_shipment
    ]
    ctes_por_shipment = {
        shipment_id: _normalizar_valor(
            linhas_por_shipment[shipment_id].get("CTE")
        )
        for shipment_id in shipment_ids_fila
    }
    dados_performance_consultados = _consultar_dados_performance_shipmentids(
        ctes_por_shipment,
        log=log,
    )
    for shipment_id in shipment_ids_fila:
        dados_performance = dados_performance_consultados.get(
            shipment_id,
            {},
        )
        linha_fila = linhas_por_shipment[shipment_id]
        if dados_performance.get("Destino"):
            linha_fila["Destino"] = dados_performance["Destino"]
        if dados_performance.get("Status"):
            linha_fila["Status"] = dados_performance["Status"]

    linhas = [
        linhas_por_shipment[str(pedido["ShipmentId"])]
        for pedido in pedidos
        if str(pedido["ShipmentId"]) in linhas_por_shipment
    ]

    colunas_fila = COLUNAS_RESULTADO + ["Origem", "Status"]
    tabela_completa = pd.DataFrame(linhas, columns=colunas_fila)
    tabela_completa = _adicionar_rota(tabela_completa)
    tabela_completa = tabela_completa[
        COLUNAS_RESULTADO + ["Rota", "Origem", "Status"]
    ]
    status_final_emissao = tabela_completa["Status"].map(
        lambda valor: _normalizar_cabecalho(valor) == "EMISSAO"
    )
    tabela = tabela_completa.loc[status_final_emissao].reset_index(drop=True)
    shipment_ids_fora_emissao = tabela_completa.loc[
        ~status_final_emissao,
        "ShipmentId",
    ].astype(str).tolist()
    shipment_ids_sem_status = tabela_completa.loc[
        tabela_completa["Status"].fillna("").astype(str).str.strip().eq(""),
        "ShipmentId",
    ].astype(str).tolist()

    if log is not None:
        log(
            f"Performance: {len(base)} linha(s) utilizável(is) na aba "
            f"{nome_aba}; cabeçalho na linha {linha_cabecalho}."
        )
        log(
            f"Etiquetas: {sum(len(pedido['ETIQUETAS']) for pedido in pedidos)} "
            f"em {len(pedidos)} ShipmentId(s); pedidos com Status final EMISSAO "
            f"mantidos após atualização: {len(tabela)}."
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
    sem_retorno = [
        shipment_id
        for shipment_id in shipment_ids_sem_match
        if not _normalizar_valor(
            resultados_consulta.get(shipment_id, {}).get("CTE")
        )
    ]
    sem_destino = [
        shipment_id
        for shipment_id in shipment_ids_sem_match
        if not _normalizar_valor(
            linhas_por_shipment.get(shipment_id, {}).get("Destino")
        )
    ]
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
        "shipment_ids_sem_match": shipment_ids_sem_match,
        "shipment_ids_sem_retorno_fraction": sem_retorno,
        "shipment_ids_sem_destino_performance": sem_destino,
        "shipment_ids_sem_status_performance": shipment_ids_sem_status,
        "shipment_ids_fora_emissao": shipment_ids_fora_emissao,
        "shipment_ids_etiquetas": len(tabela),
        "total_etiquetas": int(tabela["Volumes Etiquetas"].sum()),
    }
