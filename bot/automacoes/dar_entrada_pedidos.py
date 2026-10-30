from __future__ import annotations

from collections import OrderedDict, defaultdict
from datetime import datetime
from io import BytesIO
from pathlib import Path, PurePosixPath
import re
import unicodedata
from typing import Callable
from zipfile import BadZipFile, ZipFile

import pandas as pd
from playwright.sync_api import (
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from servicos.fraction import (
    CredenciaisFractionInvalidasError,
    executar_com_retentativas_fraction,
    fechar_recurso_playwright_seguro,
    login_fraction,
    recuperar_sessao_fraction,
)
from utils.config import (
    FRACTION_PASSWORD,
    FRACTION_USER,
    HEADLESS,
    RESULTADOS_ENTRADA_PEDIDOS,
    SLOW_MO_MS,
    URL_FRACTION,
)


COLUNAS_RESULTADO = [
    "CTE",
    "ShipmentId",
    "Etiquetas do pedido",
    "Volumes Sistema",
    "Volumes Etiquetas",
    "Etiquetas faltantes",
    "CTEs para dar entrada",
    "Destino",
    "Manifesto",
]
PADRAO_ETIQUETA = re.compile(r"^(\d{14})\$(\d{3})(\S*)\s*$")
PADRAO_BLOCO_SHIPMENT_TXT = re.compile(
    r"^Shipment\s+ID\s*:\s*(\d{14})"
    r"(?:\s*\(\s*(\d+)\s+etiqueta\(s\)\s*\))?\s*$",
    flags=re.IGNORECASE,
)
PADRAO_BLOCO_PALETE_TXT = re.compile(
    r"^PAL[EÉ]TE\s+(.+?)\s*[·•]\s*(\d+)\s+etiqueta\(s\)"
    r"\s*[·•]\s*(\d+)\s+Shipment\s+ID\(s\)\s*$",
    flags=re.IGNORECASE,
)
PADRAO_IDENTIFICADOR_RUA = re.compile(
    r"(?<![A-Z0-9])(?:RUA|CORREDOR)\s*[:#._-]?\s*0*(\d+[A-Z]?)"
    r"(?![A-Z0-9])",
    flags=re.IGNORECASE,
)
PADRAO_VOLUME_TOTAL = re.compile(r"^0*1\s*/\s*0*(\d+)\s*$")
PADRAO_NUMEROS_ROMANEIO = re.compile(r"\s*[,;\n\r]+\s*|\s+")
MAX_TENTATIVAS_CONSULTA_ROMANEIOS = 3
MAX_TENTATIVAS_CONSULTA_SHIPMENT = 5
MAX_TENTATIVAS_ABRIR_SESSAO_SHIPMENT = 3
MAX_TENTATIVAS_PREPARACAO_RECEBIMENTO = 5
MAX_CTES_POR_LOTE_RECEBIMENTO = 1000
COLUNAS_PEDIDOS_FORA_ROMANEIOS = [
    "CTE",
    "ShipmentId",
    "Etiquetas do pedido",
    "Volumes Fraction",
    "Volumes Etiquetas",
    "Etiquetas faltantes",
    "CTEs para dar entrada",
    "Situação",
]


class AdicaoRecebimentoIndeterminadaError(RuntimeError):
    """O Fraction pode ter recebido uma ação de entrada sem confirmação final."""


def normalizar_numeros_romaneio(texto: str) -> list[str]:
    """Valida e remove duplicidades mantendo a ordem dos romaneios digitados."""
    numeros = [item for item in PADRAO_NUMEROS_ROMANEIO.split(texto.strip()) if item]
    invalidos = [numero for numero in numeros if not numero.isdigit()]
    if invalidos:
        raise ValueError(
            "Informe somente números de romaneio, separados por espaço, vírgula, "
            "ponto e vírgula ou quebra de linha. Inválido(s): "
            + ", ".join(invalidos[:10])
        )
    if not numeros:
        raise ValueError("Informe ao menos um número de romaneio.")

    unicos: list[str] = []
    vistos: set[str] = set()
    for numero in numeros:
        if numero not in vistos:
            vistos.add(numero)
            unicos.append(numero)
    return unicos


def _aguardar_carregamento_fraction(page, log: Callable[[str], None]) -> None:
    """Espera o Ajax e os indicadores de carregamento do Fraction terminarem."""
    page.wait_for_timeout(1000)
    page.wait_for_function(
        """() => {
            const estaVisivel = (elemento) =>
                elemento.getClientRects().length > 0 &&
                getComputedStyle(elemento).visibility !== "hidden" &&
                getComputedStyle(elemento).display !== "none";
            const carregando = Array.from(document.querySelectorAll("body *"))
                .some((elemento) =>
                    elemento.children.length === 0 &&
                    elemento.innerText?.trim().includes("Carregando") &&
                    estaVisivel(elemento)
                );
            const jqueryAtivo = window.jQuery && window.jQuery.active > 0;
            const fila = window.PrimeFaces?.ajax?.Queue;
            const filaVazia = !fila || typeof fila.isEmpty !== "function" || fila.isEmpty();
            const ocupado = Array.from(
                document.querySelectorAll('[aria-busy="true"]')
            ).some(estaVisivel);
            return !carregando && !jqueryAtivo && filaVazia && !ocupado;
        }""",
        timeout=180_000,
        polling=500,
    )
    log("A atualização do Fraction terminou.")


def _aguardar_spinner_recebimento(
    page,
    log: Callable[[str], None],
    *,
    contexto: str,
) -> None:
    """Só retorna quando o spinner Carregando apareceu e depois desapareceu."""
    spinner = page.get_by_text("Carregando").first
    log(f"Aguardando 'Carregando' aparecer após {contexto}.")
    try:
        spinner.wait_for(state="visible", timeout=120_000)
    except PlaywrightTimeoutError as erro:
        raise PlaywrightTimeoutError(
            f"O Fraction não exibiu 'Carregando' após {contexto}; "
            "a atualização não pode ser confirmada."
        ) from erro

    log("Spinner 'Carregando' detectado; aguardando desaparecer.")
    spinner.wait_for(state="hidden", timeout=180_000)
    log(f"Spinner 'Carregando' desapareceu após {contexto}.")


def _exportar_excel_fraction(
    page,
    seletor: str,
    descricao: str,
    log: Callable[[str], None],
) -> tuple[bytes, str]:
    exportador = page.locator(seletor)
    exportador.wait_for(state="visible", timeout=60_000)
    page.wait_for_function(
        """seletor => {
            const elemento = document.querySelector(seletor);
            return elemento && !elemento.disabled &&
                elemento.getAttribute("aria-disabled") !== "true";
        }""",
        arg=seletor,
        timeout=60_000,
    )
    log(f"Exportando {descricao} para Excel.")
    with page.expect_download(timeout=240_000) as download_info:
        exportador.click()
    download = download_info.value
    caminho_download = download.path()
    if not caminho_download:
        raise RuntimeError(f"O Fraction não disponibilizou {descricao}.")
    conteudo = Path(caminho_download).read_bytes()
    if not conteudo:
        raise RuntimeError(f"O arquivo de {descricao} exportado pelo Fraction está vazio.")
    nome_arquivo = download.suggested_filename
    log(f"{descricao} exportado: {nome_arquivo} ({len(conteudo)} bytes).")
    return conteudo, nome_arquivo


def _consolidar_unitizados(conteudos: list[tuple[str, bytes]], log) -> bytes:
    cabecalho_referencia: list[str] | None = None
    partes: list[pd.DataFrame] = []
    colunas_obrigatorias = {
        _normalizar_cabecalho("CTE"),
        _normalizar_cabecalho("Doc Recebedor"),
        _normalizar_cabecalho("Destino"),
    }

    for numero_romaneio, conteudo in conteudos:
        bruto = pd.read_excel(BytesIO(conteudo), header=None, dtype=object)
        indice_cabecalho = None
        for indice, linha in bruto.head(20).iterrows():
            valores = {
                _normalizar_cabecalho(valor)
                for valor in linha.tolist()
                if not pd.isna(valor)
            }
            if colunas_obrigatorias.issubset(valores):
                indice_cabecalho = indice
                break
        if indice_cabecalho is None:
            raise ValueError(
                f"O Unitizado do romaneio {numero_romaneio} não contém "
                "as colunas CTE, Doc Recebedor e Destino."
            )

        cabecalho = [
            "" if pd.isna(valor) else str(valor).strip()
            for valor in bruto.iloc[indice_cabecalho].tolist()
        ]
        if cabecalho_referencia is None:
            cabecalho_referencia = cabecalho
        elif [
            _normalizar_cabecalho(coluna) for coluna in cabecalho
        ] != [_normalizar_cabecalho(coluna) for coluna in cabecalho_referencia]:
            raise ValueError(
                f"O layout do Unitizado do romaneio {numero_romaneio} "
                "é diferente dos demais romaneios."
            )

        dados = bruto.iloc[indice_cabecalho + 1 :].copy()
        dados.columns = cabecalho
        dados.dropna(how="all", inplace=True)
        coluna_manifesto = _localizar_coluna(dados, "Manifesto")
        if coluna_manifesto is None:
            dados["Manifesto"] = str(numero_romaneio)
        else:
            dados[coluna_manifesto] = str(numero_romaneio)
        partes.append(dados)
        log(
            f"Romaneio {numero_romaneio}: "
            f"{len(dados)} linha(s) Unitizado(s) exportada(s)."
        )

    if not partes or cabecalho_referencia is None:
        raise ValueError("Nenhuma base Unitizado foi exportada do Fraction.")

    unificado = pd.concat(partes, ignore_index=True)
    saida = BytesIO()
    unificado.to_excel(saida, index=False, engine="openpyxl")
    log(
        f"Bases Unitizado consolidadas: {len(unificado)} linha(s) "
        f"de {len(conteudos)} romaneio(s)."
    )
    return saida.getvalue()


def baixar_bases_por_romaneios(
    numeros_romaneio: list[str],
    *,
    log: Callable[[str], None] = print,
) -> dict[str, bytes]:
    """Baixa Inconsistência e Unitizado no Fraction para os romaneios indicados."""
    if not numeros_romaneio:
        raise ValueError("Informe ao menos um número de romaneio.")
    if not URL_FRACTION or not FRACTION_USER or not FRACTION_PASSWORD:
        raise ValueError(
            "URL_FRACTION, FRACTION_USER e FRACTION_PASSWORD precisam estar configurados."
        )

    ultimo_erro: Exception | None = None
    with sync_playwright() as playwright:
        for tentativa in range(1, MAX_TENTATIVAS_CONSULTA_ROMANEIOS + 1):
            browser = None
            try:
                log(
                    f"Consulta de romaneios: tentativa {tentativa}/"
                    f"{MAX_TENTATIVAS_CONSULTA_ROMANEIOS}."
                )
                browser = playwright.chromium.launch(
                    headless=HEADLESS,
                    slow_mo=SLOW_MO_MS,
                )
                context = browser.new_context(accept_downloads=True)
                page = context.new_page()
                page.on(
                    "pageerror",
                    lambda erro: log(f"Erro JavaScript no Fraction: {erro}"),
                )

                log("Entrando no Fraction com o usuário operacional.")
                login_fraction(page, FRACTION_USER, FRACTION_PASSWORD, log)
                log("Abrindo Operacional > Manifesto > Conf. Manifesto para Inconsistência.")
                page.get_by_role("link", name="Operacional").click()
                page.get_by_role("link", name="Manifesto", exact=True).click()
                page.get_by_role("link", name="Conf. Manifesto").click()

                campo_romaneios = page.locator(
                    '[id="form_conferencia_manifesto:txtArea"]'
                )
                campo_romaneios.wait_for(state="visible", timeout=60_000)
                campo_romaneios.click()
                campo_romaneios.fill("\n".join(numeros_romaneio))
                log(
                    f"{len(numeros_romaneio)} romaneio(s) preenchido(s) na conferência."
                )

                page.get_by_role("button", name="Adicionar").click()
                log("Romaneios adicionados; aguardando a conferência atualizar.")
                _aguardar_carregamento_fraction(page, log)
                inconsistencia, nome_inconsistencia = _exportar_excel_fraction(
                    page,
                    '[id="form_conferencia_manifesto:id_exportar_excel"]',
                    "base InconsistenciaManifesto",
                    log,
                )

                unitizados: list[tuple[str, bytes]] = []
                for indice, numero_romaneio in enumerate(numeros_romaneio, start=1):
                    log(
                        f"Buscando Unitizado do romaneio {numero_romaneio} "
                        f"({indice}/{len(numeros_romaneio)})."
                    )
                    page.get_by_role("link", name="Consultas").click()
                    page.get_by_role("link", name="Pesquisar").click()
                    page.get_by_text("Manifesto ou Unitizado").click()
                    campo_pesquisa = page.locator('[id="frmPesquisa:cte"]')
                    campo_pesquisa.wait_for(state="visible", timeout=60_000)
                    campo_pesquisa.click()
                    campo_pesquisa.fill(numero_romaneio)
                    page.get_by_role("button", name="Processar").click()
                    log(f"Pesquisa do romaneio {numero_romaneio} enviada.")
                    _aguardar_carregamento_fraction(page, log)
                    unitizado, _ = _exportar_excel_fraction(
                        page,
                        '[id="formLstUnitizado:id_exportar_excel"]',
                        f"Unitizado do romaneio {numero_romaneio}",
                        log,
                    )
                    unitizados.append((numero_romaneio, unitizado))

                unitizado_consolidado = _consolidar_unitizados(unitizados, log)
                log(
                    f"Arquivos-fonte: {nome_inconsistencia} e "
                    f"{len(unitizados)} Unitizado(s) individual(is)."
                )
                return {
                    "unitizado": unitizado_consolidado,
                    "inconsistencia": inconsistencia,
                }
            except CredenciaisFractionInvalidasError:
                raise
            except (
                PlaywrightTimeoutError,
                PlaywrightError,
                OSError,
                RuntimeError,
            ) as erro:
                ultimo_erro = erro
                log(
                    f"Falha na tentativa {tentativa}: "
                    f"{type(erro).__name__}: {erro}"
                )
                if tentativa == MAX_TENTATIVAS_CONSULTA_ROMANEIOS:
                    break
                log("Será feito novo login e o caminho da conferência será reiniciado.")
            finally:
                if browser is not None:
                    try:
                        browser.close()
                    except PlaywrightError:
                        pass

    raise RuntimeError(
        f"Não foi possível exportar a Conferência de Manifesto após "
        f"{MAX_TENTATIVAS_CONSULTA_ROMANEIOS} tentativas. Último erro: {ultimo_erro}"
    ) from ultimo_erro


def _normalizar_codigo(valor: object) -> str:
    if pd.isna(valor):
        return ""

    texto = str(valor).strip()
    if re.fullmatch(r"\d+\.0", texto):
        texto = texto[:-2]
    return texto


def _normalizar_shipment_id(valor: object) -> str:
    shipment_id = _normalizar_codigo(valor)
    if shipment_id.isdigit() and len(shipment_id) < 14:
        return shipment_id.zfill(14)
    return shipment_id


def _capturar_valor_rotulo_fraction(page, rotulo: str) -> str:
    """Lê o valor numérico associado a um rótulo do resultado de pesquisa."""
    marcador = page.get_by_text(rotulo, exact=False).first
    marcador.wait_for(state="visible", timeout=30_000)
    padrao = re.compile(
        rf"{re.escape(rotulo.rstrip(':'))}\s*:\s*(\d+)",
        flags=re.IGNORECASE,
    )

    for nivel in range(7):
        elemento = marcador if nivel == 0 else marcador.locator("xpath=" + "/.." * nivel)
        try:
            texto = elemento.inner_text().strip()
        except PlaywrightError:
            continue
        encontrado = padrao.search(texto)
        if encontrado:
            return encontrado.group(1)

    raise RuntimeError(f"O resultado do Fraction não trouxe um valor para '{rotulo}'.")


def _pedido_nao_encontrado_fraction(page) -> bool:
    try:
        texto = _normalizar_cabecalho(page.locator("body").inner_text())
    except PlaywrightError:
        return False
    return any(
        frase in texto
        for frase in (
            "NENHUM REGISTRO",
            "NENHUM RESULTADO",
            "NAO ENCONTRADO",
            "NAO LOCALIZADO",
            "NAO FORAM ENCONTRADOS",
            "NAO EXISTE",
            "SEM REGISTRO",
        )
    )


def _consultar_shipment_id_fraction(
    page,
    shipment_id: str,
    log: Callable[[str], None],
) -> dict[str, object]:
    """Consulta um ShipmentId e captura Conhecimento e total de Volumes."""
    page.get_by_role("link", name="Consultas").click()
    page.get_by_role("link", name="Pesquisar").click()
    page.get_by_text(
        re.compile(r"Shipp?ment\s*ID", flags=re.IGNORECASE)
    ).first.click()
    _aguardar_carregamento_fraction(page, log)

    campo_pesquisa = page.locator('[id="frmPesquisa:cte"]')
    campo_pesquisa.wait_for(state="visible", timeout=60_000)
    campo_pesquisa.fill(shipment_id)
    page.get_by_role("button", name="Processar").click()
    log(f"ShipmentId {shipment_id}: pesquisa enviada.")
    _aguardar_carregamento_fraction(page, log)

    try:
        cte = _capturar_valor_rotulo_fraction(page, "Conhecimento:")
        volumes = int(_capturar_valor_rotulo_fraction(page, "Volumes:"))
    except PlaywrightTimeoutError:
        if _pedido_nao_encontrado_fraction(page):
            log(f"ShipmentId {shipment_id}: pedido não encontrado no Fraction.")
            return {
                "CTE": "",
                "Volumes Sistema": None,
                "Situação consulta": "Não encontrado no Fraction",
            }
        raise

    if volumes < 1:
        raise ValueError(
            f"ShipmentId {shipment_id}: o Fraction retornou um total de volumes inválido ({volumes})."
        )
    log(f"ShipmentId {shipment_id}: CTE {cte}, {volumes} volume(s) no Fraction.")
    return {
        "CTE": cte,
        "Volumes Sistema": volumes,
        "Situação consulta": "Consultado",
    }


def consultar_shipments_no_fraction(
    shipment_ids: list[str],
    *,
    log: Callable[[str], None] = print,
) -> dict[str, dict[str, object]]:
    """Consulta ShipmentIds individualmente com as recuperações do Fraction."""
    shipment_ids_unicos = list(
        dict.fromkeys(_normalizar_shipment_id(item) for item in shipment_ids)
    )
    if not shipment_ids_unicos:
        return {}
    if not URL_FRACTION or not FRACTION_USER or not FRACTION_PASSWORD:
        raise ValueError(
            "URL_FRACTION, FRACTION_USER e FRACTION_PASSWORD precisam estar configurados."
        )

    resultados: dict[str, dict[str, object]] = {}
    with sync_playwright() as playwright:
        browser = None
        context = None
        page = None

        def fechar_sessao() -> None:
            nonlocal browser, context, page
            fechar_recurso_playwright_seguro(context, log)
            fechar_recurso_playwright_seguro(browser, log)
            browser = None
            context = None
            page = None

        def abrir_sessao() -> None:
            nonlocal browser, context, page
            fechar_sessao()
            ultimo_erro: Exception | None = None
            for tentativa in range(1, MAX_TENTATIVAS_ABRIR_SESSAO_SHIPMENT + 1):
                try:
                    log(
                        "Abrindo sessão do Fraction para consulta individual "
                        f"({tentativa}/{MAX_TENTATIVAS_ABRIR_SESSAO_SHIPMENT})."
                    )
                    browser = playwright.chromium.launch(
                        headless=HEADLESS,
                        slow_mo=SLOW_MO_MS,
                    )
                    context = browser.new_context(accept_downloads=True)
                    page = context.new_page()
                    page.on(
                        "pageerror",
                        lambda erro: log(f"Erro JavaScript no Fraction: {erro}"),
                    )
                    login_fraction(page, FRACTION_USER, FRACTION_PASSWORD, log)
                    return
                except CredenciaisFractionInvalidasError:
                    fechar_sessao()
                    raise
                except Exception as erro:
                    ultimo_erro = erro
                    log(
                        "Falha ao abrir a sessão do Fraction: "
                        f"{type(erro).__name__}: {erro}"
                    )
                    fechar_sessao()
                    if tentativa == MAX_TENTATIVAS_ABRIR_SESSAO_SHIPMENT:
                        break
            raise RuntimeError(
                "Não foi possível abrir uma sessão do Fraction para as consultas "
                f"individuais após {MAX_TENTATIVAS_ABRIR_SESSAO_SHIPMENT} tentativas."
            ) from ultimo_erro

        def recuperar_sessao() -> None:
            if page is None:
                abrir_sessao()
                return
            recuperar_sessao_fraction(
                page,
                log,
                login_callback=lambda: login_fraction(
                    page,
                    FRACTION_USER,
                    FRACTION_PASSWORD,
                    log,
                ),
            )

        try:
            try:
                abrir_sessao()
            except CredenciaisFractionInvalidasError:
                raise
            except Exception as erro:
                log(
                    "Consultas individuais não iniciadas: "
                    f"{type(erro).__name__}: {erro}"
                )
                return {
                    shipment_id: {
                        "CTE": "",
                        "Volumes Sistema": None,
                        "Situação consulta": "Fraction indisponível",
                    }
                    for shipment_id in shipment_ids_unicos
                }

            for indice, shipment_id in enumerate(shipment_ids_unicos, start=1):
                log(
                    f"Consultando ShipmentId {shipment_id} "
                    f"({indice}/{len(shipment_ids_unicos)})."
                )

                def consultar():
                    return _consultar_shipment_id_fraction(page, shipment_id, log)

                try:
                    resultado = executar_com_retentativas_fraction(
                        consultar,
                        recuperar_sessao,
                        log,
                        descricao=f"ShipmentId {shipment_id}",
                        tentativas=MAX_TENTATIVAS_CONSULTA_SHIPMENT,
                    )
                except CredenciaisFractionInvalidasError:
                    raise
                except Exception as erro:
                    log(
                        f"ShipmentId {shipment_id}: {type(erro).__name__}: {erro}. "
                        "Retentativas com refresh "
                        "esgotadas ou consulta interrompida. Abrindo Chromium novo "
                        "para uma tentativa final."
                    )
                    try:
                        abrir_sessao()
                        resultado = consultar()
                    except CredenciaisFractionInvalidasError:
                        raise
                    except Exception as erro_final:
                        log(
                            f"ShipmentId {shipment_id}: falha definitiva: "
                            f"{type(erro_final).__name__}: {erro_final}"
                        )
                        resultados[shipment_id] = {
                            "CTE": "",
                            "Volumes Sistema": None,
                            "Situação consulta": "Falha na consulta Fraction",
                        }
                        if indice < len(shipment_ids_unicos):
                            try:
                                abrir_sessao()
                            except CredenciaisFractionInvalidasError:
                                raise
                            except Exception as erro_sessao:
                                log(
                                    "Sessão do Fraction indisponível; consultas restantes "
                                    f"foram interrompidas: {type(erro_sessao).__name__}: "
                                    f"{erro_sessao}"
                                )
                                for shipment_id_pendente in shipment_ids_unicos[indice:]:
                                    resultados[shipment_id_pendente] = {
                                        "CTE": "",
                                        "Volumes Sistema": None,
                                        "Situação consulta": "Consulta interrompida: Fraction indisponível",
                                    }
                                break
                        continue

                resultados[shipment_id] = resultado
        finally:
            fechar_sessao()

    return resultados


def coletar_ctes_liberados_para_entrada(*tabelas: pd.DataFrame) -> list[str]:
    """Retorna os CTEs liberados, um por item e sem duplicatas."""
    situacoes_liberadas = {"LIBERADO - SEM DIVERGÊNCIA", "CONFIRMADO"}
    ctes: list[str] = []
    vistos: set[str] = set()

    for tabela in tabelas:
        if tabela.empty or "Situação da entrada" not in tabela.columns:
            continue
        if "CTEs para dar entrada" not in tabela.columns:
            continue

        liberados = tabela[tabela["Situação da entrada"].isin(situacoes_liberadas)]
        for valor in liberados["CTEs para dar entrada"]:
            if pd.isna(valor):
                continue
            for cte in str(valor).splitlines():
                cte = cte.strip()
                if not cte:
                    continue
                if not cte.isdigit():
                    raise ValueError(
                        f"CTE inválido para dar entrada: '{cte}'. "
                        "Foram aceitos somente códigos numéricos."
                    )
                if cte not in vistos:
                    vistos.add(cte)
                    ctes.append(cte)

    return ctes


def salvar_base_entrada_pedidos(
    tabela_romaneios: pd.DataFrame,
    tabela_sem_romaneio: pd.DataFrame,
    ctes_adicionados: list[str],
    *,
    log: Callable[[str], None] = print,
) -> Path:
    """Salva os pedidos efetivamente enviados ao recebimento em uma planilha consolidada."""
    adicionados = {str(cte).strip() for cte in ctes_adicionados if str(cte).strip()}
    if not adicionados:
        raise ValueError("Não há CTEs confirmados para salvar na base de entrada.")

    partes: list[pd.DataFrame] = []
    for tabela, sem_romaneio in (
        (tabela_romaneios, False),
        (tabela_sem_romaneio, True),
    ):
        if tabela.empty:
            continue
        if "CTEs para dar entrada" not in tabela.columns:
            continue
        incluir = tabela["CTEs para dar entrada"].fillna("").map(
            lambda valor: bool(
                adicionados.intersection(
                    linha.strip() for linha in str(valor).splitlines() if linha.strip()
                )
            )
        )
        selecionada = tabela.loc[incluir].copy()
        if selecionada.empty:
            continue
        if sem_romaneio:
            selecionada["Manifesto"] = "Sem Romaneio"
        partes.append(selecionada)

    if not partes:
        raise ValueError("Nenhuma linha da conferência corresponde aos CTEs enviados.")

    base = pd.concat(partes, ignore_index=True, sort=False)
    colunas_finais = [
        coluna for coluna in base.columns if coluna not in {"Destino", "Manifesto"}
    ]
    if "Destino" in base.columns:
        colunas_finais.append("Destino")
    if "Manifesto" in base.columns:
        colunas_finais.append("Manifesto")
    base = base[colunas_finais]
    RESULTADOS_ENTRADA_PEDIDOS.mkdir(parents=True, exist_ok=True)
    destino = RESULTADOS_ENTRADA_PEDIDOS / (
        f"Dar_entrada_pedidos_{datetime.now():%Y-%m-%d_%H-%M-%S-%f}.xlsx"
    )
    base.to_excel(destino, index=False, engine="openpyxl")
    log(
        f"Base de entrada salva: {destino} ({len(base)} pedido(s), "
        f"{len(adicionados)} CTE(s) enviados)."
    )
    return destino


def adicionar_ctes_ao_recebimento_fraction(
    ctes: list[str],
    *,
    log: Callable[[str], None] = print,
) -> dict[str, object]:
    """Dá entrada nos CTEs em lotes, aguardando o Fraction em cada etapa."""
    ctes_unicos = list(dict.fromkeys(str(cte).strip() for cte in ctes if str(cte).strip()))
    if not ctes_unicos:
        raise ValueError("Nenhum CTE liberado para dar entrada.")
    if any(not cte.isdigit() for cte in ctes_unicos):
        raise ValueError("A lista de CTEs contém código não numérico.")
    if not URL_FRACTION or not FRACTION_USER or not FRACTION_PASSWORD:
        raise ValueError(
            "URL_FRACTION, FRACTION_USER e FRACTION_PASSWORD precisam estar configurados."
        )

    lotes = [
        ctes_unicos[inicio : inicio + MAX_CTES_POR_LOTE_RECEBIMENTO]
        for inicio in range(0, len(ctes_unicos), MAX_CTES_POR_LOTE_RECEBIMENTO)
    ]

    with sync_playwright() as playwright:
        browser = None
        context = None
        page = None

        def fechar_sessao() -> None:
            nonlocal browser, context, page
            fechar_recurso_playwright_seguro(context, log)
            fechar_recurso_playwright_seguro(browser, log)
            browser = None
            context = None
            page = None

        def abrir_sessao() -> None:
            nonlocal browser, context, page
            fechar_sessao()
            ultimo_erro: Exception | None = None
            for tentativa in range(1, MAX_TENTATIVAS_ABRIR_SESSAO_SHIPMENT + 1):
                try:
                    log(
                        "Abrindo sessão do Fraction para Recebimento Remessa "
                        f"({tentativa}/{MAX_TENTATIVAS_ABRIR_SESSAO_SHIPMENT})."
                    )
                    browser = playwright.chromium.launch(
                        headless=HEADLESS,
                        slow_mo=SLOW_MO_MS,
                    )
                    context = browser.new_context(accept_downloads=True)
                    page = context.new_page()
                    page.on(
                        "pageerror",
                        lambda erro: log(f"Erro JavaScript no Fraction: {erro}"),
                    )
                    login_fraction(page, FRACTION_USER, FRACTION_PASSWORD, log)
                    return
                except CredenciaisFractionInvalidasError:
                    fechar_sessao()
                    raise
                except Exception as erro:
                    ultimo_erro = erro
                    log(
                        "Falha ao abrir sessão para Recebimento Remessa: "
                        f"{type(erro).__name__}: {erro}"
                    )
                    fechar_sessao()
                    if tentativa == MAX_TENTATIVAS_ABRIR_SESSAO_SHIPMENT:
                        break
            raise RuntimeError(
                "Não foi possível abrir o Fraction para Recebimento Remessa após "
                f"{MAX_TENTATIVAS_ABRIR_SESSAO_SHIPMENT} tentativas."
            ) from ultimo_erro

        def recuperar_sessao() -> None:
            if page is None:
                abrir_sessao()
                return
            recuperar_sessao_fraction(
                page,
                log,
                login_callback=lambda: login_fraction(
                    page,
                    FRACTION_USER,
                    FRACTION_PASSWORD,
                    log,
                ),
            )

        def abrir_recebimento() -> None:
            page.get_by_role("link", name="Operacional").click()
            page.get_by_role("link", name="Manifesto", exact=True).click()
            page.get_by_role(
                "link",
                name="Recebimento Remessa",
                exact=True,
            ).click()
            _aguardar_carregamento_fraction(page, log)

        def preencher_lote(lote: list[str], numero_lote: int) -> None:
            campo_ctes = page.locator('[id="form_recebe_remessa:txtArea"]')
            campo_ctes.wait_for(state="visible", timeout=60_000)
            campo_ctes.click()
            conteudo = "\n".join(lote)
            campo_ctes.fill(conteudo)
            if campo_ctes.input_value().splitlines() != lote:
                raise RuntimeError(
                    f"O lote {numero_lote} preenchido no Recebimento Remessa "
                    "não confere com os CTEs liberados."
                )
            log(
                f"Lote {numero_lote}/{len(lotes)} preparado com {len(lote)} "
                "CTE(s), um código por linha."
            )

        try:
            try:
                abrir_sessao()
            except CredenciaisFractionInvalidasError:
                raise
            except Exception as erro:
                log(
                    "Não foi possível iniciar o recebimento: "
                    f"{type(erro).__name__}: {erro}"
                )
                raise

            try:
                executar_com_retentativas_fraction(
                    abrir_recebimento,
                    recuperar_sessao,
                    log,
                    descricao="Abertura de Recebimento Remessa",
                    tentativas=MAX_TENTATIVAS_PREPARACAO_RECEBIMENTO,
                )
            except CredenciaisFractionInvalidasError:
                raise
            except Exception as erro:
                log(
                    "A abertura de Recebimento Remessa esgotou as tentativas com "
                    "refresh. Abrindo Chromium novo para repetir: "
                    f"{type(erro).__name__}: {erro}"
                )
                abrir_sessao()
                abrir_recebimento()

            alguma_acao_de_entrada = False
            try:
                for numero_lote, lote in enumerate(lotes, start=1):
                    preencher_lote(lote, numero_lote)

                    botao_adicionar = page.get_by_role(
                        "button",
                        name="Adicionar",
                        exact=True,
                    )
                    botao_adicionar.wait_for(state="visible", timeout=60_000)
                    if not botao_adicionar.is_enabled():
                        raise RuntimeError(
                            f"O botão Adicionar está desabilitado no lote {numero_lote}."
                        )

                    log(
                        f"Adicionando lote {numero_lote}/{len(lotes)} "
                        f"({len(lote)} CTE(s))."
                    )
                    # A partir daqui, um erro pode deixar o estado do lote
                    # incerto; não repetir automaticamente um clique de envio.
                    alguma_acao_de_entrada = True
                    botao_adicionar.click(timeout=60_000)
                    _aguardar_spinner_recebimento(
                        page,
                        log,
                        contexto=f"Adicionar do lote {numero_lote}/{len(lotes)}",
                    )

                botao_processar = page.get_by_role(
                    "button",
                    name="Processar lista",
                    exact=True,
                )
                botao_processar.wait_for(state="visible", timeout=60_000)
                if not botao_processar.is_enabled():
                    raise RuntimeError(
                        "O botão Processar lista está desabilitado após adicionar "
                        "todos os lotes."
                    )

                log(
                    f"Todos os {len(lotes)} lote(s) foram adicionados. "
                    "Clicando em Processar lista."
                )
                botao_processar.click(timeout=60_000)
                _aguardar_spinner_recebimento(
                    page,
                    log,
                    contexto="Processar lista",
                )
            except AdicaoRecebimentoIndeterminadaError:
                raise
            except Exception as erro:
                if alguma_acao_de_entrada:
                    mensagem = (
                        "A execução da entrada pode ter sido recebida parcialmente "
                        "ou concluída pelo Fraction, mas não foi confirmada. Confira "
                        "a tela do sistema antes de tentar novamente."
                    )
                    log(f"{mensagem} Erro: {type(erro).__name__}: {erro}")
                    raise AdicaoRecebimentoIndeterminadaError(mensagem) from erro
                raise

            log(
                f"Entrada concluída: {len(ctes_unicos)} CTE(s) processados em "
                f"{len(lotes)} lote(s)."
            )
            return {
                "ctes": ctes_unicos,
                "quantidade": len(ctes_unicos),
                "lotes": len(lotes),
            }
        finally:
            fechar_sessao()


def _normalizar_cabecalho(valor: object) -> str:
    texto = unicodedata.normalize("NFKD", str(valor).strip())
    texto = "".join(
        caractere
        for caractere in texto
        if not unicodedata.combining(caractere)
    )
    return " ".join(texto.upper().split())


def _localizar_coluna(df: pd.DataFrame, nome: str) -> object | None:
    procurado = _normalizar_cabecalho(nome)
    for coluna in df.columns:
        if _normalizar_cabecalho(coluna) == procurado:
            return coluna
    return None


def _extrair_rua_etiquetas(
    identificador_fonte: str,
    linhas: list[str],
) -> str:
    """Obtém a rua do caminho/nome do TXT ou de um cabeçalho explícito."""
    candidatos = PADRAO_IDENTIFICADOR_RUA.findall(identificador_fonte)
    if not candidatos:
        candidatos = [
            encontrado.group(1)
            for linha in linhas[:30]
            if (encontrado := PADRAO_IDENTIFICADOR_RUA.search(linha.strip()))
        ]
    ruas = list(dict.fromkeys(valor.upper() for valor in candidatos))
    if len(ruas) > 1:
        raise ValueError(
            "O TXT identifica mais de uma rua; cada arquivo da fila deve conter "
            f"uma única rua: {', '.join(ruas)}."
        )
    return f"Rua {ruas[0]}" if ruas else ""


def agrupar_etiquetas(
    texto: str | bytes,
    *,
    identificador_fonte: str = "",
) -> list[dict[str, object]]:
    """Agrupa etiquetas em TXT simples ou na exportação estruturada do Coletor."""
    if isinstance(texto, bytes):
        texto = texto.decode("utf-8-sig")

    linhas = texto.splitlines()
    secoes_exportadas = [
        encontrado
        for linha in linhas
        if (encontrado := PADRAO_BLOCO_SHIPMENT_TXT.fullmatch(linha.strip()))
    ]
    if secoes_exportadas or any(
        linha.strip().casefold().startswith("coletor evelog")
        for linha in linhas
    ):
        if not secoes_exportadas:
            return _agrupar_etiquetas_transferencia_simples(
                linhas,
                identificador_fonte=identificador_fonte,
            )
        return _agrupar_etiquetas_export_coletor(
            linhas,
            identificador_fonte=identificador_fonte,
        )

    pedidos: OrderedDict[str, list[str]] = OrderedDict()
    linhas_invalidas: list[int] = []

    for numero_linha, linha in enumerate(linhas, start=1):
        etiqueta = linha.strip()
        if not etiqueta:
            continue

        encontrado = PADRAO_ETIQUETA.fullmatch(etiqueta)
        if encontrado is None or encontrado.group(2) == "000":
            linhas_invalidas.append(numero_linha)
            continue

        shipment_id = encontrado.group(1)
        pedidos.setdefault(shipment_id, []).append(etiqueta)

    if linhas_invalidas:
        linhas = ", ".join(str(numero) for numero in linhas_invalidas[:10])
        sufixo = "..." if len(linhas_invalidas) > 10 else ""
        raise ValueError(
            "O TXT contém etiqueta(s) fora do formato esperado "
            "14 dígitos + '$' + sequência de volume com 3 dígitos + código. "
            f"Linha(s): {linhas}{sufixo}."
        )
    if not pedidos:
        raise ValueError("O TXT não contém etiquetas válidas para consultar.")

    rua = _extrair_rua_etiquetas(identificador_fonte, linhas)
    identificador_palete = " · ".join(
        parte
        for parte in (
            rua,
            identificador_fonte,
            "Sem palete",
        )
        if parte
    )

    return [
        {
            "ShipmentId": shipment_id,
            "ETIQUETAS": etiquetas,
            "VOLUMES_ETIQUETAS": len(etiquetas),
            "ETIQUETAS_POR_PALETE": {identificador_palete: list(etiquetas)},
            "RUA_POR_PALETE": {identificador_palete: rua},
        }
        for shipment_id, etiquetas in pedidos.items()
    ]


def _agrupar_etiquetas_transferencia_simples(
    linhas: list[str],
    *,
    identificador_fonte: str = "",
) -> list[dict[str, object]]:
    """Lê a exportação atual: cabeçalho de lote seguido por etiquetas soltas."""
    etiquetas_por_shipment: OrderedDict[str, list[str]] = OrderedDict()
    quantidade_shipments_declarada: int | None = None
    quantidade_etiquetas_declarada: int | None = None
    lote = ""
    linhas_invalidas: list[int] = []
    total_etiquetas = 0

    for numero_linha, linha in enumerate(linhas, start=1):
        texto = linha.strip()
        if not texto:
            continue

        encontrado_lote = re.fullmatch(r"Lote\s*:\s*(.+)", texto, re.IGNORECASE)
        if encontrado_lote:
            lote = encontrado_lote.group(1).strip()
            continue

        encontrado_total = re.fullmatch(
            r"(Shipment\s+IDs|Etiquetas)\s*:\s*(\d+)",
            texto,
            re.IGNORECASE,
        )
        if encontrado_total:
            quantidade = int(encontrado_total.group(2))
            if encontrado_total.group(1).casefold().startswith("shipment"):
                quantidade_shipments_declarada = quantidade
            else:
                quantidade_etiquetas_declarada = quantidade
            continue

        etiqueta = PADRAO_ETIQUETA.fullmatch(texto)
        if etiqueta is not None and etiqueta.group(2) != "000":
            shipment_id = etiqueta.group(1)
            etiquetas_por_shipment.setdefault(shipment_id, []).append(texto)
            total_etiquetas += 1
            continue

        if re.match(r"^\d{14}\$", texto):
            linhas_invalidas.append(numero_linha)

    if linhas_invalidas:
        linhas_texto = ", ".join(str(numero) for numero in linhas_invalidas[:10])
        sufixo = "..." if len(linhas_invalidas) > 10 else ""
        raise ValueError(
            "O TXT contém etiqueta(s) fora do formato esperado nas linha(s): "
            f"{linhas_texto}{sufixo}."
        )
    if not etiquetas_por_shipment:
        raise ValueError("O TXT não contém etiquetas válidas para consultar.")
    if (
        quantidade_shipments_declarada is not None
        and quantidade_shipments_declarada != len(etiquetas_por_shipment)
    ):
        raise ValueError(
            "O total de Shipment IDs informado no cabeçalho do TXT "
            f"({quantidade_shipments_declarada}) difere dos ShipmentIds "
            f"encontrados nas etiquetas ({len(etiquetas_por_shipment)})."
        )
    if (
        quantidade_etiquetas_declarada is not None
        and quantidade_etiquetas_declarada != total_etiquetas
    ):
        raise ValueError(
            "O total de etiquetas informado no cabeçalho do TXT "
            f"({quantidade_etiquetas_declarada}) difere das etiquetas "
            f"encontradas ({total_etiquetas})."
        )

    rua = _extrair_rua_etiquetas(identificador_fonte, linhas)
    origem = identificador_fonte or lote or "Transferência"
    identificador_palete = " · ".join(
        parte for parte in (rua, origem, "Etiquetas") if parte
    )
    return [
        {
            "ShipmentId": shipment_id,
            "ETIQUETAS": etiquetas,
            "VOLUMES_ETIQUETAS": len(etiquetas),
            "ETIQUETAS_POR_PALETE": {identificador_palete: list(etiquetas)},
            "RUA_POR_PALETE": {identificador_palete: rua},
        }
        for shipment_id, etiquetas in etiquetas_por_shipment.items()
    ]


def agrupar_etiquetas_importadas(
    conteudo: bytes,
    nome_arquivo: str,
    *,
    log: Callable[[str], None] | None = None,
) -> list[dict[str, object]]:
    """Lê um TXT ou reúne as etiquetas extraídas de todos os TXT de um ZIP."""
    if not nome_arquivo.casefold().endswith(".zip"):
        return agrupar_etiquetas(
            conteudo,
            identificador_fonte=Path(nome_arquivo).stem,
        )

    try:
        with ZipFile(BytesIO(conteudo)) as arquivo_zip:
            arquivos_txt = [
                membro
                for membro in arquivo_zip.infolist()
                if not membro.is_dir()
                and PurePosixPath(membro.filename).suffix.casefold() == ".txt"
                and "__macosx" not in {
                    parte.casefold()
                    for parte in PurePosixPath(membro.filename).parts
                }
            ]
            if not arquivos_txt:
                raise ValueError("O ZIP não contém arquivos TXT de etiquetas.")

            pedidos_por_shipment: OrderedDict[str, dict[str, object]] = OrderedDict()
            total_etiquetas = 0
            for membro in arquivos_txt:
                try:
                    pedidos_arquivo = agrupar_etiquetas(
                        arquivo_zip.read(membro),
                        identificador_fonte=PurePosixPath(membro.filename).with_suffix("").as_posix(),
                    )
                except Exception as erro:
                    raise ValueError(
                        f"Não foi possível processar '{membro.filename}' dentro do ZIP: "
                        f"{erro}"
                    ) from erro

                for pedido in pedidos_arquivo:
                    shipment_id = str(pedido["ShipmentId"])
                    etiquetas = list(pedido["ETIQUETAS"])
                    consolidado = pedidos_por_shipment.setdefault(
                        shipment_id,
                        {
                            "ETIQUETAS": [],
                            "ETIQUETAS_POR_PALETE": OrderedDict(),
                            "RUA_POR_PALETE": {},
                        },
                    )
                    consolidado["ETIQUETAS"].extend(etiquetas)
                    etiquetas_por_palete = pedido.get(
                        "ETIQUETAS_POR_PALETE",
                        {"Sem palete": etiquetas},
                    )
                    por_palete_consolidado = consolidado["ETIQUETAS_POR_PALETE"]
                    ruas_consolidadas = consolidado["RUA_POR_PALETE"]
                    ruas_pedido = pedido.get("RUA_POR_PALETE", {})
                    for palete, etiquetas_palete in etiquetas_por_palete.items():
                        por_palete_consolidado.setdefault(palete, []).extend(
                            etiquetas_palete
                        )
                        rua = str(ruas_pedido.get(palete, ""))
                        rua_existente = ruas_consolidadas.get(palete)
                        if rua_existente and rua and rua_existente != rua:
                            raise ValueError(
                                f"O palete '{palete}' foi associado a mais de uma rua."
                            )
                        ruas_consolidadas[palete] = rua or rua_existente or ""
                    total_etiquetas += len(etiquetas)

    except BadZipFile as erro:
        raise ValueError(
            f"O arquivo '{nome_arquivo}' não é um ZIP válido ou está corrompido."
        ) from erro
    except (OSError, RuntimeError) as erro:
        raise ValueError(
            f"Não foi possível ler o ZIP '{nome_arquivo}': {erro}"
        ) from erro

    pedidos = [
        {
            "ShipmentId": shipment_id,
            "ETIQUETAS": dados["ETIQUETAS"],
            "VOLUMES_ETIQUETAS": len(dados["ETIQUETAS"]),
            "ETIQUETAS_POR_PALETE": dict(dados["ETIQUETAS_POR_PALETE"]),
            "RUA_POR_PALETE": dict(dados["RUA_POR_PALETE"]),
        }
        for shipment_id, dados in pedidos_por_shipment.items()
    ]
    if log is not None:
        log(
            f"ZIP de etiquetas '{nome_arquivo}': {len(arquivos_txt)} TXT(s), "
            f"{len(pedidos)} ShipmentId(s) e {total_etiquetas} etiqueta(s) reunidos."
        )
    return pedidos


def _agrupar_etiquetas_export_coletor(
    linhas: list[str],
    *,
    identificador_fonte: str = "",
) -> list[dict[str, object]]:
    secoes: list[tuple[str, int | None, list[str], int, str]] = []
    secao_atual: tuple[str, int | None, list[str], int, str] | None = None
    palete_atual = "Sem palete"
    lote = ""
    paletes_declarados: dict[str, tuple[int, int]] = {}
    quantidade_paletes_declarada: int | None = None
    quantidade_shipments_declarada: int | None = None
    quantidade_etiquetas_declarada: int | None = None
    rua = _extrair_rua_etiquetas(identificador_fonte, linhas)
    linhas_invalidas: list[int] = []
    linhas_fora_de_secao: list[int] = []

    for numero_linha, linha in enumerate(linhas, start=1):
        texto_linha = linha.strip()
        if not texto_linha:
            continue

        encontrado_total_paletes = re.fullmatch(
            r"Paletes\s*:\s*(\d+)",
            texto_linha,
            flags=re.IGNORECASE,
        )
        if encontrado_total_paletes:
            quantidade_paletes_declarada = int(encontrado_total_paletes.group(1))
            continue

        encontrado_lote = re.fullmatch(
            r"Lote\s*:\s*(.+)",
            texto_linha,
            flags=re.IGNORECASE,
        )
        if encontrado_lote:
            lote = encontrado_lote.group(1).strip()
            continue

        encontrado_palete = PADRAO_BLOCO_PALETE_TXT.fullmatch(texto_linha)
        if encontrado_palete:
            identificador_palete = encontrado_palete.group(1).strip()
            rotulo_palete = f"Palete {identificador_palete}"
            componentes = [
                parte
                for parte in (rua, identificador_fonte, lote)
                if parte
            ]
            if componentes:
                rotulo_palete = " · ".join(componentes + [rotulo_palete])
            palete_atual = rotulo_palete
            paletes_declarados[palete_atual] = (
                int(encontrado_palete.group(2)),
                int(encontrado_palete.group(3)),
            )
            secao_atual = None
            continue

        encontrado_secao = PADRAO_BLOCO_SHIPMENT_TXT.fullmatch(texto_linha)
        if encontrado_secao:
            secao_atual = (
                encontrado_secao.group(1),
                int(encontrado_secao.group(2))
                if encontrado_secao.group(2) is not None
                else None,
                [],
                numero_linha,
                palete_atual,
            )
            secoes.append(secao_atual)
            continue

        encontrado_total = re.fullmatch(
            r"(?:Shipment\s+IDs|Etiquetas)\s*:\s*(\d+)",
            texto_linha,
            flags=re.IGNORECASE,
        )
        if encontrado_total:
            if texto_linha.casefold().startswith("shipment"):
                quantidade_shipments_declarada = int(encontrado_total.group(1))
            else:
                quantidade_etiquetas_declarada = int(encontrado_total.group(1))
            continue

        etiqueta = PADRAO_ETIQUETA.fullmatch(texto_linha)
        if etiqueta is not None and etiqueta.group(2) != "000":
            if secao_atual is None:
                linhas_fora_de_secao.append(numero_linha)
                continue
            if etiqueta.group(1) != secao_atual[0]:
                raise ValueError(
                    f"A etiqueta da linha {numero_linha} pertence ao ShipmentId "
                    f"{etiqueta.group(1)}, mas o bloco atual é {secao_atual[0]}."
                )
            secao_atual[2].append(texto_linha)
            continue

        if re.match(r"^\d{14}\$", texto_linha):
            linhas_invalidas.append(numero_linha)

    pedidos: OrderedDict[str, dict[str, object]] = OrderedDict()
    ruas_por_palete: dict[str, str] = {}
    divergencias_secoes: list[str] = []
    etiquetas_por_palete: dict[str, int] = defaultdict(int)
    shipments_por_palete: dict[str, set[str]] = defaultdict(set)
    for shipment_id, quantidade_declarada, etiquetas, linha_secao, palete in secoes:
        pedido = pedidos.setdefault(
            shipment_id,
            {"ETIQUETAS": [], "ETIQUETAS_POR_PALETE": OrderedDict()},
        )
        pedido["ETIQUETAS"].extend(etiquetas)
        pedido["ETIQUETAS_POR_PALETE"].setdefault(palete, []).extend(etiquetas)
        rua_existente = ruas_por_palete.get(palete)
        if rua_existente and rua and rua_existente != rua:
            raise ValueError(
                f"O palete '{palete}' foi associado a mais de uma rua."
            )
        ruas_por_palete[palete] = rua or rua_existente or ""
        etiquetas_por_palete[palete] += len(etiquetas)
        shipments_por_palete[palete].add(shipment_id)
        if quantidade_declarada is not None and len(etiquetas) != quantidade_declarada:
            divergencias_secoes.append(
                f"ShipmentId {shipment_id}, bloco na linha {linha_secao}: "
                f"esperadas {quantidade_declarada} etiqueta(s), encontradas {len(etiquetas)}"
            )

    for palete, (etiquetas_declaradas, shipments_declarados) in paletes_declarados.items():
        etiquetas_encontradas = etiquetas_por_palete.get(palete, 0)
        shipments_encontrados = len(shipments_por_palete.get(palete, set()))
        if etiquetas_encontradas != etiquetas_declaradas:
            divergencias_secoes.append(
                f"{palete}: esperadas {etiquetas_declaradas} etiqueta(s), "
                f"encontradas {etiquetas_encontradas}"
            )
        if shipments_encontrados != shipments_declarados:
            divergencias_secoes.append(
                f"{palete}: esperados {shipments_declarados} Shipment ID(s), "
                f"encontrados {shipments_encontrados}"
            )

    if linhas_invalidas:
        linhas_texto = ", ".join(str(numero) for numero in linhas_invalidas[:10])
        sufixo = "..." if len(linhas_invalidas) > 10 else ""
        raise ValueError(
            "O TXT contém etiqueta(s) fora do formato esperado nas linha(s): "
            f"{linhas_texto}{sufixo}."
        )
    if linhas_fora_de_secao:
        linhas_texto = ", ".join(str(numero) for numero in linhas_fora_de_secao[:10])
        sufixo = "..." if len(linhas_fora_de_secao) > 10 else ""
        raise ValueError(
            "O TXT estruturado contém etiqueta(s) fora de um bloco Shipment ID, "
            f"na(s) linha(s): {linhas_texto}{sufixo}."
        )
    if divergencias_secoes:
        raise ValueError(
            "A quantidade de etiqueta(s) declarada nos blocos não confere: "
            + "; ".join(divergencias_secoes[:5])
            + ("; ..." if len(divergencias_secoes) > 5 else ".")
        )

    total_etiquetas = sum(
        len(dados["ETIQUETAS"])
        for dados in pedidos.values()
    )
    if (
        quantidade_shipments_declarada is not None
        and quantidade_shipments_declarada != len(pedidos)
    ):
        raise ValueError(
            "O total de Shipment IDs informado no cabeçalho do TXT "
            f"({quantidade_shipments_declarada}) difere dos blocos encontrados "
            f"({len(pedidos)})."
        )
    if (
        quantidade_etiquetas_declarada is not None
        and quantidade_etiquetas_declarada
        != sum(etiquetas_por_palete.values())
    ):
        raise ValueError(
            "O total de etiquetas informado no cabeçalho do TXT "
            f"({quantidade_etiquetas_declarada}) difere das etiquetas extraídas "
            f"({sum(etiquetas_por_palete.values())})."
        )
    if (
        quantidade_paletes_declarada is not None
        and quantidade_paletes_declarada != len(paletes_declarados)
    ):
        raise ValueError(
            "O total de paletes informado no cabeçalho do TXT "
            f"({quantidade_paletes_declarada}) difere dos paletes encontrados "
            f"({len(paletes_declarados)})."
        )
    if not total_etiquetas:
        raise ValueError("O TXT não contém etiquetas válidas para consultar.")

    return [
        {
            "ShipmentId": shipment_id,
            "ETIQUETAS": dados["ETIQUETAS"],
            "VOLUMES_ETIQUETAS": len(dados["ETIQUETAS"]),
            "ETIQUETAS_POR_PALETE": dict(dados["ETIQUETAS_POR_PALETE"]),
            "RUA_POR_PALETE": {
                palete: ruas_por_palete.get(palete, "")
                for palete in dados["ETIQUETAS_POR_PALETE"]
            },
        }
        for shipment_id, dados in pedidos.items()
    ]


def _ler_base_unitizados(conteudo: bytes) -> pd.DataFrame:
    bruto = pd.read_excel(BytesIO(conteudo), header=None, dtype=object)
    cabecalho_esperado = {
        _normalizar_cabecalho("CTE"),
        _normalizar_cabecalho("Doc Recebedor"),
        _normalizar_cabecalho("Destino"),
    }

    indice_cabecalho = None
    for indice, linha in bruto.head(20).iterrows():
        valores = {
            _normalizar_cabecalho(valor)
            for valor in linha.tolist()
            if not pd.isna(valor)
        }
        if cabecalho_esperado.issubset(valores):
            indice_cabecalho = indice
            break

    if indice_cabecalho is None:
        raise ValueError(
            "Não encontrei no Unitizado um cabeçalho com CTE, Doc Recebedor e Destino."
        )

    cabecalhos = [
        "" if pd.isna(valor) else str(valor).strip()
        for valor in bruto.iloc[indice_cabecalho].tolist()
    ]
    dados = bruto.iloc[indice_cabecalho + 1 :].copy()
    dados.columns = cabecalhos
    dados.dropna(how="all", inplace=True)

    coluna_cte = _localizar_coluna(dados, "CTE")
    coluna_shipment = _localizar_coluna(dados, "Doc Recebedor")
    coluna_destino = _localizar_coluna(dados, "Destino")
    if coluna_cte is None or coluna_shipment is None or coluna_destino is None:
        raise ValueError("O Unitizado não contém todas as colunas necessárias.")

    unitizados = pd.DataFrame(
        {
            "CTE": dados[coluna_cte].map(_normalizar_codigo),
            "Manifesto": (
                dados[_localizar_coluna(dados, "Manifesto")].map(_normalizar_codigo)
                if _localizar_coluna(dados, "Manifesto") is not None
                else ""
            ),
            "ShipmentId": dados[coluna_shipment].map(_normalizar_shipment_id),
            "Destino": dados[coluna_destino].map(_normalizar_codigo),
        }
    )
    unitizados = unitizados[unitizados["CTE"] != ""].reset_index(drop=True)
    if unitizados.empty:
        raise ValueError("O Unitizado não contém linhas com CTE preenchido.")

    duplicados = unitizados[unitizados["CTE"].duplicated(keep=False)]
    if not duplicados.empty:
        ctes = sorted(duplicados["CTE"].unique())
        raise ValueError(
            "O Unitizado contém CTEs duplicados; não é seguro associar os destinos: "
            + ", ".join(ctes[:10])
        )
    return unitizados


def _ler_volumes_sistema(conteudo: bytes) -> tuple[dict[str, int], dict[str, list[int]]]:
    if conteudo.startswith((b"PK\x03\x04", b"\xd0\xcf\x11\xe0")):
        bruto = pd.read_excel(BytesIO(conteudo), header=None, dtype=object)
        colunas_necessarias = {
            _normalizar_cabecalho("CONHECIMENTO"),
            _normalizar_cabecalho("VOLUME"),
        }
        indice_cabecalho = None
        for indice, linha in bruto.head(20).iterrows():
            valores = {
                _normalizar_cabecalho(valor)
                for valor in linha.tolist()
                if not pd.isna(valor)
            }
            if colunas_necessarias.issubset(valores):
                indice_cabecalho = indice
                break
        if indice_cabecalho is None:
            raise ValueError(
                "A base InconsistenciaManifesto não contém as colunas "
                "CONHECIMENTO e VOLUME."
            )
        cabecalho = [
            "" if pd.isna(valor) else str(valor).strip()
            for valor in bruto.iloc[indice_cabecalho].tolist()
        ]
        inconsistencia = bruto.iloc[indice_cabecalho + 1 :].copy()
        inconsistencia.columns = cabecalho
    else:
        try:
            inconsistencia = pd.read_csv(
                BytesIO(conteudo),
                sep=None,
                engine="python",
                encoding="utf-8-sig",
                dtype=object,
            )
        except UnicodeDecodeError:
            inconsistencia = pd.read_csv(
                BytesIO(conteudo),
                sep=None,
                engine="python",
                encoding="cp1252",
                dtype=object,
            )

    coluna_cte = _localizar_coluna(inconsistencia, "CONHECIMENTO")
    coluna_volume = _localizar_coluna(inconsistencia, "VOLUME")
    if coluna_cte is None or coluna_volume is None:
        raise ValueError(
            "A Inconsistência precisa conter as colunas CONHECIMENTO e VOLUME."
        )

    totais_encontrados: dict[str, set[int]] = defaultdict(set)
    for _, linha in inconsistencia.iterrows():
        cte = _normalizar_codigo(linha[coluna_cte])
        volume = _normalizar_codigo(linha[coluna_volume])
        encontrado = PADRAO_VOLUME_TOTAL.fullmatch(volume)
        if cte and encontrado is not None:
            totais_encontrados[cte].add(int(encontrado.group(1)))

    conflitos = {
        cte: sorted(totais)
        for cte, totais in totais_encontrados.items()
        if len(totais) > 1
    }
    volumes = {
        cte: next(iter(totais))
        for cte, totais in totais_encontrados.items()
        if len(totais) == 1
    }
    return volumes, conflitos


def _sequencias_etiquetas(etiquetas: list[str]) -> list[int]:
    sequencias = {
        int(encontrado.group(2))
        for etiqueta in etiquetas
        if (encontrado := PADRAO_ETIQUETA.fullmatch(etiqueta.strip()))
    }
    return sorted(sequencias)


def gerar_ctes_para_entrada(
    cte: str,
    etiquetas: list[str],
    volumes_sistema: int | None,
) -> str:
    """Gera os complementos de CTE com base nas etiquetas realmente recebidas."""
    if volumes_sistema is None:
        return ""

    sequencias = _sequencias_etiquetas(etiquetas)
    if not sequencias:
        return ""
    if volumes_sistema == 1 and sequencias == [1]:
        return cte
    return "\n".join(f"{cte}{sequencia:03d}" for sequencia in sequencias)


def gerar_etiquetas_faltantes(
    shipment_id: str,
    etiquetas: list[str],
    volumes_sistema: int | None,
) -> str:
    """Reconstrói as etiquetas ausentes com base no total e no padrão do TXT."""
    if volumes_sistema is None or volumes_sistema <= 0:
        return ""

    sequencias_encontradas = set(_sequencias_etiquetas(etiquetas))
    faltantes = [
        sequencia
        for sequencia in range(1, volumes_sistema + 1)
        if sequencia not in sequencias_encontradas
    ]
    if not faltantes:
        return ""

    sufixos = {
        encontrado.group(3)
        for etiqueta in etiquetas
        if (encontrado := PADRAO_ETIQUETA.fullmatch(etiqueta.strip()))
    }
    if len(sufixos) == 1:
        sufixo = next(iter(sufixos))
        return "\n".join(
            f"{shipment_id}${sequencia:03d}{sufixo}"
            for sequencia in faltantes
        )

    return "\n".join(
        f"Volume {sequencia:03d} (código completo não inferido)"
        for sequencia in faltantes
    )


def completar_pedidos_fora_romaneios(
    pedidos_fora_romaneios: pd.DataFrame,
    consultas_fraction: dict[str, dict[str, object]],
    *,
    log: Callable[[str], None] = print,
) -> pd.DataFrame:
    """Acrescenta CTE, volumes e conferência aos ShipmentIds fora dos romaneios."""
    if pedidos_fora_romaneios.empty:
        return pd.DataFrame(columns=COLUNAS_PEDIDOS_FORA_ROMANEIOS)

    linhas: list[dict[str, object]] = []
    for _, pedido in pedidos_fora_romaneios.iterrows():
        shipment_id = _normalizar_shipment_id(pedido["ShipmentId"])
        etiquetas = sorted(
            [
                etiqueta.strip()
                for etiqueta in str(pedido["Etiquetas do pedido"]).splitlines()
                if etiqueta.strip()
            ],
            key=lambda etiqueta: int(PADRAO_ETIQUETA.fullmatch(etiqueta).group(2)),
        )
        consulta = consultas_fraction.get(shipment_id, {})
        cte = _normalizar_codigo(consulta.get("CTE", ""))
        volumes_sistema = consulta.get("Volumes Sistema")
        if pd.isna(volumes_sistema):
            volumes_sistema = None
        elif volumes_sistema is not None:
            volumes_sistema = int(volumes_sistema)

        situacao_consulta = str(
            consulta.get("Situação consulta", "Falha na consulta Fraction")
        )
        if situacao_consulta == "Consultado":
            situacao = "Fora dos romaneios consultados"
            etiquetas_faltantes = gerar_etiquetas_faltantes(
                shipment_id,
                etiquetas,
                volumes_sistema,
            )
            ctes_para_entrada = (
                gerar_ctes_para_entrada(cte, etiquetas, volumes_sistema)
                if cte
                else ""
            )
        else:
            situacao = situacao_consulta
            etiquetas_faltantes = ""
            ctes_para_entrada = ""

        linhas.append(
            {
                "CTE": cte,
                "ShipmentId": shipment_id,
                "Etiquetas do pedido": "\n".join(etiquetas),
                "Volumes Fraction": volumes_sistema,
                "Volumes Etiquetas": len(etiquetas),
                "Etiquetas faltantes": etiquetas_faltantes,
                "CTEs para dar entrada": ctes_para_entrada,
                "Situação": situacao,
            }
        )

    resultado = pd.DataFrame(linhas, columns=COLUNAS_PEDIDOS_FORA_ROMANEIOS)
    consultados = (resultado["Situação"] == "Fora dos romaneios consultados").sum()
    falhas = len(resultado) - consultados
    log(
        f"Pedidos fora dos romaneios: {consultados} consulta(s) concluída(s), "
        f"{falhas} sem resultado confirmado no Fraction."
    )
    return resultado


def montar_base_pedidos(
    unitizado: bytes,
    inconsistencia: bytes,
    pedidos: list[dict[str, object]],
    *,
    log: Callable[[str], None] = print,
) -> dict[str, object]:
    """Monta a conferência juntando Unitizado, Inconsistência e etiquetas TXT."""
    base_unitizados = _ler_base_unitizados(unitizado)
    volumes_por_cte, conflitos_volume = _ler_volumes_sistema(inconsistencia)
    etiquetas_por_shipment: dict[str, list[str]] = {
        str(pedido["ShipmentId"]): list(pedido["ETIQUETAS"])
        for pedido in pedidos
    }

    linhas: list[dict[str, object]] = []
    for _, pedido in base_unitizados.iterrows():
        cte = str(pedido["CTE"])
        shipment_id = str(pedido["ShipmentId"])
        etiquetas = sorted(
            etiquetas_por_shipment.get(shipment_id, []),
            key=lambda etiqueta: int(PADRAO_ETIQUETA.fullmatch(etiqueta).group(2)),
        )
        if not etiquetas:
            continue
        volumes_sistema = (
            None
            if cte in conflitos_volume
            else volumes_por_cte.get(cte)
        )
        linhas.append(
            {
                "CTE": cte,
                "Manifesto": str(pedido["Manifesto"]),
                "ShipmentId": shipment_id,
                "Destino": str(pedido["Destino"]),
                "Etiquetas do pedido": "\n".join(etiquetas),
                "Volumes Sistema": volumes_sistema,
                "Volumes Etiquetas": len(etiquetas),
                "Etiquetas faltantes": gerar_etiquetas_faltantes(
                    shipment_id,
                    etiquetas,
                    volumes_sistema,
                ),
                "CTEs para dar entrada": gerar_ctes_para_entrada(
                    cte,
                    etiquetas,
                    volumes_sistema,
                ),
            }
        )

    tabela = pd.DataFrame(linhas, columns=COLUNAS_RESULTADO)
    shipment_ids_unitizados = set(base_unitizados["ShipmentId"])
    shipment_ids_etiquetas = set(etiquetas_por_shipment)
    ctes_unitizados = {str(linha["CTE"]) for linha in linhas}
    ctes_sem_volume = sorted(
        cte
        for cte in ctes_unitizados
        if cte not in volumes_por_cte and cte not in conflitos_volume
    )
    pedidos_fora_romaneios = []
    for shipment_id in sorted(shipment_ids_etiquetas - shipment_ids_unitizados):
        etiquetas_fora = sorted(
            etiquetas_por_shipment[shipment_id],
            key=lambda etiqueta: int(PADRAO_ETIQUETA.fullmatch(etiqueta).group(2)),
        )
        pedidos_fora_romaneios.append(
            {
                "CTE": "",
                "ShipmentId": shipment_id,
                "Etiquetas do pedido": "\n".join(etiquetas_fora),
                "Volumes Fraction": None,
                "Volumes Etiquetas": len(etiquetas_fora),
                "Etiquetas faltantes": "",
                "CTEs para dar entrada": "",
                "Situação": "Fora dos romaneios consultados",
            }
        )
    etiquetas_sem_unitizado = sorted(
        shipment_id
        for shipment_id in shipment_ids_etiquetas - shipment_ids_unitizados
    )
    ctes_sem_destino = sorted(
        str(linha["CTE"])
        for linha in linhas
        if not str(linha["Destino"]).strip()
    )
    divergencias = tabela[
        tabela["Volumes Sistema"].notna()
        & (
            (tabela["Volumes Sistema"] != tabela["Volumes Etiquetas"])
            | tabela["Etiquetas faltantes"].fillna("").astype(str).str.strip().ne("")
        )
    ]

    log(
        f"Unitizado: {len(tabela)} CTE(s); TXT: {sum(len(p['ETIQUETAS']) for p in pedidos)} "
        f"etiqueta(s) em {len(pedidos)} ShipmentId(s); Inconsistência: "
        f"{len(volumes_por_cte)} total(is) de volume localizado(s)."
    )
    log(
        f"Conferência montada: {len(divergencias)} divergência(s) de volume; "
        f"{len(ctes_sem_volume)} CTE(s) sem volume do sistema."
    )

    avisos = {
        "CTEs sem volume na Inconsistência": ctes_sem_volume,
        "CTEs com total de volume conflitante": {
            cte: totais for cte, totais in sorted(conflitos_volume.items())
        },
        "ShipmentIds do TXT sem linha no Unitizado": etiquetas_sem_unitizado,
        "CTEs sem Destino no Unitizado": ctes_sem_destino,
    }
    return {
        "tabela": tabela,
        "pedidos_fora_romaneios": pd.DataFrame(
            pedidos_fora_romaneios,
            columns=COLUNAS_PEDIDOS_FORA_ROMANEIOS,
        ),
        "avisos": avisos,
        "divergencias": len(divergencias),
    }
