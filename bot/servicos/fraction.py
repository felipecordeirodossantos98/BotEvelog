from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import time
import traceback
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Callable, Iterable
from urllib.parse import unquote_to_bytes

import pandas as pd

from playwright.sync_api import (
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from utils.config import (
    ARQUIVO_ROTAS_ROMANEIOS,
    ARQUIVO_UNIDADES_FRACTION,
    FRACTION_PASSWORD,
    FRACTION_PASSWORD_PERFORMANCE,
    FRACTION_USER,
    FRACTION_USER_PERFORMANCE,
    HEADLESS,
    RESULTADOS_PERFORMANCE,
    RESULTADOS_ROMANEIOS,
    SLOW_MO_MS,
    URL_FRACTION,
)

MAX_TENTATIVAS_CONSULTA_SHIPMENT = 5
MAX_TENTATIVAS_ABRIR_SESSAO_SHIPMENT = 3


def _normalizar_cabecalho_consulta_fraction(valor: object) -> str:
    texto = unicodedata.normalize("NFKD", str(valor).strip())
    texto = "".join(caractere for caractere in texto if not unicodedata.combining(caractere))
    return " ".join(texto.upper().split())


def _aguardar_carregamento_consulta_fraction(page: Page, log: Callable[[str], None]) -> None:
    page.wait_for_timeout(1000)
    page.wait_for_function(
        """() => {
            const visivel = (e) => e.getClientRects().length > 0 &&
                getComputedStyle(e).visibility !== "hidden" &&
                getComputedStyle(e).display !== "none";
            const carregando = Array.from(document.querySelectorAll("body *"))
                .some((e) => e.children.length === 0 &&
                    e.innerText?.trim().includes("Carregando") && visivel(e));
            const ajax = window.jQuery && window.jQuery.active > 0;
            const fila = window.PrimeFaces?.ajax?.Queue;
            const filaVazia = !fila || typeof fila.isEmpty !== "function" || fila.isEmpty();
            const ocupado = Array.from(document.querySelectorAll('[aria-busy="true"]')).some(visivel);
            return !carregando && !ajax && filaVazia && !ocupado;
        }""",
        timeout=180_000,
        polling=500,
    )
    log("A atualização do Fraction terminou.")


def _normalizar_shipment_id_consulta(valor: object) -> str:
    if pd.isna(valor):
        return ""
    shipment_id = str(valor).strip()
    if re.fullmatch(r"\d+\.0", shipment_id):
        shipment_id = shipment_id[:-2]
    if shipment_id.isdigit() and len(shipment_id) < 14:
        shipment_id = shipment_id.zfill(14)
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
        texto = _normalizar_cabecalho_consulta_fraction(page.locator("body").inner_text())
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
    _aguardar_carregamento_consulta_fraction(page, log)

    campo_pesquisa = page.locator('[id="frmPesquisa:cte"]')
    campo_pesquisa.wait_for(state="visible", timeout=60_000)
    campo_pesquisa.fill(shipment_id)
    page.get_by_role("button", name="Processar").click()
    log(f"ShipmentId {shipment_id}: pesquisa enviada.")
    _aguardar_carregamento_consulta_fraction(page, log)

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
        dict.fromkeys(_normalizar_shipment_id_consulta(item) for item in shipment_ids)
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



def validar_url_fraction() -> None:
    if not URL_FRACTION:
        raise RuntimeError(
            "URL_FRACTION não foi informada no arquivo .env."
        )


def login_fraction_mega_cartela(
    page: Page,
    log: Callable[[str], None],
) -> None:
    """
    Login do Fraction usado especificamente pela Mega Cartela LSM.

    Mantém o login genérico intacto e apenas centraliza a escolha
    das credenciais FRACTION_USER_MEGA_CARTELA / PASSWORD.
    """
    usuario = os.getenv(
        "FRACTION_USER_MEGA_CARTELA",
        "",
    ).strip()

    senha = os.getenv(
        "FRACTION_PASSWORD_MEGA_CARTELA",
        "",
    )

    if not usuario:
        raise RuntimeError(
            "FRACTION_USER_MEGA_CARTELA não foi informado no .env."
        )

    if not senha:
        raise RuntimeError(
            "FRACTION_PASSWORD_MEGA_CARTELA não foi informado no .env."
        )

    login_fraction(
        page,
        usuario,
        senha,
        log,
    )


class CredenciaisFractionInvalidasError(RuntimeError):
    """O Fraction recusou as credenciais informadas no login."""


def login_fraction(
    page: Page,
    usuario: str,
    senha: str,
    log: Callable[[str], None],
) -> None:
    validar_url_fraction()

    log("Abrindo FractionWeb...")

    page.goto(
        URL_FRACTION,
        wait_until="domcontentloaded",
        timeout=120_000,
    )

    page.get_by_role(
        "textbox",
        name="Usuário",
    ).fill(usuario)

    page.get_by_role(
        "textbox",
        name="Senha",
    ).fill(senha)

    page.get_by_role(
        "button",
        name="Login",
    ).click()

    # Aguarda um resultado explícito do login para não deixar uma senha
    # inválida parecer apenas uma tela que travou até estourar timeout.
    page.wait_for_function(
        """() => {
            const texto = document.body?.innerText || "";
            if (texto.includes("Login ou senha inválidos.")) return true;

            const visivel = (elemento) =>
                elemento.getClientRects().length > 0 &&
                getComputedStyle(elemento).visibility !== "hidden" &&
                getComputedStyle(elemento).display !== "none";

            return Array.from(
                document.querySelectorAll('a, [role="link"]')
            ).some((elemento) =>
                elemento.innerText.replace(/\\s+/g, " ").trim() === "Consultas" &&
                visivel(elemento)
            );
        }""",
        timeout=120_000,
    )

    erro_login = page.get_by_text(
        "Login ou senha inválidos.",
        exact=True,
    ).first
    if erro_login.is_visible():
        mensagem_log = (
            "O Fraction informou 'Login ou senha inválidos.'. Confirme o usuário "
            "e a senha e atualize a variável de senha correspondente no arquivo "
            "bot/.env do projeto."
        )
        log(f"Warning: {mensagem_log}")
        raise CredenciaisFractionInvalidasError(
            "O Fraction recusou o login. Confira o usuário e a senha em bot/.env."
        )

    page.get_by_role(
        "link",
        name="Consultas",
    ).wait_for(
        state="visible",
        timeout=5_000,
    )

    log("Login no Fraction concluído.")


def pesquisar_cte_fraction(
    page: Page,
    codigo: str,
    log: Callable[[str], None],
) -> None:
    """
    Caminho compartilhado até o resultado da pesquisa:

    Consultas -> Pesquisar -> CTE -> Processar

    Para aqui propositalmente para outros fluxos poderem
    reutilizar a mesma navegação e tratar o resultado como quiserem.
    """
    codigo = str(codigo).strip()

    if not codigo:
        raise ValueError("Código/CTE não informado.")

    log(f"Pesquisando código {codigo} no Fraction...")

    page.get_by_role(
        "link",
        name="Consultas",
    ).click()

    page.wait_for_timeout(400)

    page.get_by_role(
        "link",
        name="Pesquisar",
    ).click()

    page.wait_for_timeout(500)

    campo_cte = page.locator(
        '[id="frmPesquisa:cte"]'
    )

    campo_cte.wait_for(
        state="visible",
        timeout=30_000,
    )

    campo_cte.fill(codigo)

    page.get_by_role(
        "button",
        name="Processar",
    ).click()

    log(f"Código {codigo}: pesquisa enviada.")


def capturar_peso_taxado_fraction(
    page: Page,
    log: Callable[[str], None],
) -> float | None:
    """Captura o valor exibido ao lado de ``Peso Taxado:``.

    O Fraction pode estruturar o rótulo e o valor em elementos diferentes.
    Por isso subimos alguns níveis no DOM e procuramos o valor no texto
    combinado do bloco.
    """
    marcador = page.get_by_text(
        "Peso Taxado:",
        exact=False,
    ).first

    try:
        marcador.wait_for(
            state="visible",
            timeout=30_000,
        )
    except PlaywrightTimeoutError:
        log("Peso Taxado não foi localizado no resultado do CTE.")
        return None

    textos = []

    for nivel in range(0, 6):
        if nivel == 0:
            elemento = marcador
        else:
            elemento = marcador.locator(
                "xpath=" + "/.." * nivel
            )

        try:
            texto = elemento.inner_text().strip()
        except Exception:
            continue

        if texto and texto not in textos:
            textos.append(texto)

        if re.search(
            r"Peso\s+Taxado\s*:\s*([0-9]+(?:[.,][0-9]+)?)",
            texto,
            flags=re.IGNORECASE,
        ):
            break

    padrao = re.compile(
        r"Peso\s+Taxado\s*:\s*([0-9]+(?:[.,][0-9]+)?)",
        flags=re.IGNORECASE,
    )

    for texto in textos:
        encontrado = padrao.search(texto)
        if not encontrado:
            continue

        valor_texto = encontrado.group(1).replace(
            ".",
            ".",
        ).replace(
            ",",
            ".",
        )

        try:
            valor = float(valor_texto)
        except ValueError:
            continue

        log(
            f"Peso Taxado encontrado: {valor:.2f}."
        )
        return valor

    log("Peso Taxado não pôde ser extraído do resultado do CTE.")
    return None


def pesquisar_e_capturar_peso_taxado_fraction(
    page: Page,
    codigo: str,
    log: Callable[[str], None],
) -> float | None:
    """Pesquisa um CTE e retorna o Peso Taxado do pedido encontrado."""
    pesquisar_cte_fraction(
        page,
        codigo,
        log,
    )

    return capturar_peso_taxado_fraction(
        page,
        log,
    )


def _normalizar_operacao_historico(valor: str) -> str:
    texto = unicodedata.normalize(
        "NFKD",
        str(valor).strip(),
    )
    texto = "".join(
        caractere
        for caractere in texto
        if not unicodedata.combining(caractere)
    )
    return " ".join(
        texto.upper().split()
    )


def capturar_operacoes_historico_fraction(
    page: Page,
    log: Callable[[str], None],
) -> list[str]:
    """
    Captura as operações da tabela Histórico do resultado pesquisado.

    A coluna é localizada dinamicamente pelo cabeçalho ``Operação`` para
    permitir reutilização futura com qualquer status do histórico.
    """
    titulo_historico = page.get_by_text(
        "Histórico",
        exact=True,
    )

    titulo_historico.wait_for(
        state="visible",
        timeout=120_000,
    )

    cabecalho_operacao = page.get_by_role(
        "columnheader",
        name="Operação",
        exact=True,
    )

    cabecalho_operacao.wait_for(
        state="visible",
        timeout=30_000,
    )

    indice_operacao = cabecalho_operacao.evaluate(
        "(elemento) => elemento.cellIndex"
    )

    tabela = cabecalho_operacao.locator(
        "xpath=ancestor::table[1]"
    )

    linhas = tabela.locator("tr")
    operacoes: list[str] = []

    for indice in range(linhas.count()):
        linha = linhas.nth(indice)
        celulas = linha.locator("td")

        if celulas.count() <= indice_operacao:
            continue

        operacao = (
            celulas
            .nth(indice_operacao)
            .inner_text()
            .strip()
        )

        if operacao:
            operacoes.append(operacao)

    log(
        f"Histórico carregado: {len(operacoes)} operação(ões) encontrada(s)."
    )

    return operacoes


def contar_operacao_historico_fraction(
    page: Page,
    operacao: str,
    log: Callable[[str], None],
) -> int:
    """Conta ocorrências exatas de uma operação na coluna Operação."""
    operacao_procurada = _normalizar_operacao_historico(
        operacao
    )

    if not operacao_procurada:
        raise ValueError(
            "Operação do histórico não informada."
        )

    operacoes = capturar_operacoes_historico_fraction(
        page,
        log,
    )

    quantidade = sum(
        1
        for valor in operacoes
        if _normalizar_operacao_historico(valor)
        == operacao_procurada
    )

    log(
        f'Operação "{operacao}": {quantidade} ocorrência(s) no histórico.'
    )

    return quantidade


def pesquisar_e_contar_operacao_historico_fraction(
    page: Page,
    codigo: str,
    operacao: str,
    log: Callable[[str], None],
) -> int:
    """Pesquisa o código e conta uma operação específica no Histórico."""
    pesquisar_cte_fraction(
        page,
        codigo,
        log,
    )

    return contar_operacao_historico_fraction(
        page,
        operacao,
        log,
    )


def preencher_observacao_fraction(
    page: Page,
    codigo: str,
    log: Callable[[str], None],
) -> None:
    """
    Mantém o comportamento atual do fluxo Zendesk.

    A navegação/pesquisa fica centralizada em pesquisar_cte_fraction(),
    permitindo que outros fluxos reutilizem somente o caminho comum.
    """
    pesquisar_cte_fraction(
        page,
        codigo,
        log,
    )

    botao_obs = page.get_by_role(
        "button",
        name="Incluir Observação",
    )

    botao_obs.wait_for(
        state="visible",
        timeout=120_000,
    )

    botao_obs.click()

    campo = page.locator(
        '[id="form_add_obs:descObsv"]'
    )

    campo.wait_for(
        state="visible",
        timeout=30_000,
    )

    campo.fill(
        "REMETENTE ACIONADO."
    )

    botao_salvar = page.get_by_role(
        "button",
        name="Salvar",
    )

    botao_salvar.wait_for(
        state="visible",
        timeout=30_000,
    )

    botao_salvar.click()

    page.wait_for_timeout(1_000)

    log(
        f"Código {codigo}: observação "
        "REMETENTE ACIONADO. salva."
    )

def capturar_descricoes_tde_fraction(
    page: Page,
    log: Callable[[str], None],
) -> list[str]:
    """
    Captura dinamicamente todas as células da coluna "Descrição"
    na seção "Observação" do resultado pesquisado no Fraction.
    """
    titulo_observacao = page.get_by_text(
        "Observação",
        exact=True,
    )

    titulo_observacao.wait_for(
        state="visible",
        timeout=120_000,
    )

    cabecalho_descricao = page.get_by_role(
        "columnheader",
        name="Descrição",
        exact=True,
    )

    cabecalho_descricao.wait_for(
        state="visible",
        timeout=30_000,
    )

    indice_descricao = cabecalho_descricao.evaluate(
        "(elemento) => elemento.cellIndex"
    )

    tabela = cabecalho_descricao.locator(
        "xpath=ancestor::table[1]"
    )

    linhas = tabela.locator("tr")

    descricoes: list[str] = []

    for indice in range(linhas.count()):
        linha = linhas.nth(indice)
        celulas = linha.locator("td")

        if celulas.count() <= indice_descricao:
            continue

        descricao = (
            celulas
            .nth(indice_descricao)
            .inner_text()
            .strip()
        )

        if descricao:
            descricoes.append(descricao)

    log(
        f"{len(descricoes)} descrição(ões) TDE encontrada(s)."
    )

    return descricoes


def pesquisar_e_capturar_tdes_fraction(
    page: Page,
    codigo: str,
    log: Callable[[str], None],
) -> list[str]:
    """
    Pesquisa um CTE e devolve somente as descrições da seção Observação.
    """
    pesquisar_cte_fraction(
        page,
        codigo,
        log,
    )

    return capturar_descricoes_tde_fraction(
        page,
        log,
    )

def recuperar_pagina_fraction(
    page: Page,
    log: Callable[[str], None],
    *,
    validar_callback: Callable[[], None],
    login_callback: Callable[[], None] | None = None,
    nome_tela: str = "Fraction",
) -> None:
    """
    Recuperação genérica de uma sessão já aberta do Fraction.

    O caminho normal da automação não é alterado. Esta função só entra
    depois de uma falha:
      1. F5/page.reload na mesma sessão;
      2. valida a tela esperada por callback;
      3. se a sessão tiver sido perdida, refaz o login pelo callback;
      4. valida novamente a tela esperada.

    Fluxos de consulta usam normalmente o menu Consultas; operações como
    Ordens de Coleta podem validar outra tela, como Operacional.
    """
    log(
        f"Recuperando {nome_tela} com refresh..."
    )

    try:
        page.reload(
            wait_until="domcontentloaded",
            timeout=60_000,
        )
    except Exception as erro:
        log(
            "O refresh do Fraction retornou erro, mas a sessão "
            "ainda será verificada: "
            f"{type(erro).__name__}: {erro}"
        )

    # Somente no caminho de recuperação.
    page.wait_for_timeout(1_000)

    try:
        validar_callback()
        log(
            f"{nome_tela}: sessão recuperada após o refresh."
        )
        return
    except Exception:
        if login_callback is None:
            raise

    log(
        f"{nome_tela}: a tela esperada não voltou após o refresh. "
        "Tentando autenticar novamente no mesmo navegador..."
    )

    login_callback()
    validar_callback()

    log(
        f"{nome_tela}: sessão autenticada novamente."
    )


def recuperar_sessao_fraction(
    page: Page,
    log: Callable[[str], None],
    *,
    login_callback: Callable[[], None] | None = None,
) -> None:
    """Recupera a área de Consultas do Fraction."""

    def validar_consultas() -> None:
        page.get_by_role(
            "link",
            name="Consultas",
        ).wait_for(
            state="visible",
            timeout=15_000,
        )

    recuperar_pagina_fraction(
        page,
        log,
        validar_callback=validar_consultas,
        login_callback=login_callback,
        nome_tela="Consultas do Fraction",
    )


def executar_com_retentativas_fraction(
    acao: Callable[[], object],
    recuperar: Callable[[], None],
    log: Callable[[str], None],
    *,
    descricao: str,
    tentativas: int = 5,
):
    """
    Executa uma consulta do Fraction com retentativa apenas para timeouts
    do Playwright.

    Erros de regra/negócio não são repetidos automaticamente. Entre
    timeouts, a função de recuperação fornecida pelo fluxo é executada.
    """
    ultimo_erro = None

    for tentativa in range(
        1,
        tentativas + 1,
    ):
        try:
            if tentativa > 1:
                log(
                    f"{descricao}: nova tentativa "
                    f"({tentativa}/{tentativas})."
                )

            return acao()

        except PlaywrightTimeoutError as erro:
            ultimo_erro = erro

            log(
                f"{descricao}: timeout na tentativa "
                f"{tentativa}/{tentativas}."
            )

            if tentativa >= tentativas:
                break

            recuperar()

    if ultimo_erro is not None:
        raise ultimo_erro

    raise RuntimeError(
        f"{descricao}: consulta não concluída."
    )


class FractionIndisponivelError(RuntimeError):
    """Falha de conexão/login no Fraction após tentativas isoladas."""


def fechar_recurso_playwright_seguro(
    recurso,
    log: Callable[[str], None] | None = None,
) -> None:
    """Fecha browser/context sem deixar erro de fechamento quebrar o fluxo."""
    if recurso is None:
        return

    try:
        recurso.close()
    except Exception as erro:
        if log is not None:
            log(
                "Aviso ao fechar recurso do navegador: "
                f"{type(erro).__name__}: {erro}"
            )


def testar_conexao_fraction_isolada(
    playwright,
    usuario: str,
    senha: str,
    log: Callable[[str], None],
    *,
    headless: bool,
    slow_mo: int = 0,
    tentativas: int = 3,
    intervalo_segundos: int = 3,
) -> tuple[bool, str]:
    """
    Testa o Fraction em um navegador descartável.

    Cada tentativa abre um Chromium novo, executa o mesmo login validado
    em ``login_fraction`` e fecha completamente browser/context antes da
    próxima tentativa. Assim, uma sessão quebrada não contamina a fase real.

    Não altera seletores, delays ou timeouts da navegação já existente.
    """
    ultimo_erro = ""

    for tentativa in range(1, tentativas + 1):
        browser = None
        context = None

        try:
            log(
                "Testando disponibilidade do Fraction "
                f"({tentativa}/{tentativas})..."
            )

            browser = playwright.chromium.launch(
                headless=headless,
                slow_mo=slow_mo,
                args=["--start-maximized"],
            )

            context = browser.new_context(
                no_viewport=True,
            )

            page = context.new_page()

            login_fraction(
                page,
                usuario,
                senha,
                log,
            )

            log(
                "Teste do Fraction concluído com sucesso. "
                "A sessão de teste será fechada antes da fase real."
            )

            return True, ""

        except Exception as erro:
            ultimo_erro = (
                f"{type(erro).__name__}: {erro}"
            )
            credenciais_invalidas = isinstance(
                erro,
                CredenciaisFractionInvalidasError,
            )
            if not credenciais_invalidas:
                log(
                    "Falha no teste de disponibilidade do Fraction: "
                    + ultimo_erro
                )

        finally:
            fechar_recurso_playwright_seguro(
                context,
                log,
            )
            fechar_recurso_playwright_seguro(
                browser,
                log,
            )

        if credenciais_invalidas:
            return False, ultimo_erro

        if tentativa < tentativas:
            log(
                "Fechando a tentativa anterior e abrindo "
                "um Chromium novo para testar novamente."
            )
            time.sleep(intervalo_segundos)

    return False, ultimo_erro

# ==========================================================
# FOLHA DE APOIO - RELATÓRIO DESCRITIVO POR CORRENTISTA
# ==========================================================

MESES_PT_BR = {
    1: "janeiro",
    2: "fevereiro",
    3: "marco",
    4: "abril",
    5: "maio",
    6: "junho",
    7: "julho",
    8: "agosto",
    9: "setembro",
    10: "outubro",
    11: "novembro",
    12: "dezembro",
}


def abrir_relatorio_descritivo_correntista_fraction(
    page: Page,
    log: Callable[[str], None],
    *,
    codigo_correntista: str = "014995",
    nome_correntista: str = "ARCOS DOURADOS COMERCIO DE",
) -> None:
    log("Abrindo Folha de apoio do Fraction...")

    menu_financeiro = page.get_by_role(
        "link",
        name="Financeiro",
    )

    menu_financeiro.wait_for(
        state="visible",
        timeout=30_000,
    )

    # O submenu do PrimeFaces pode permanecer oculto após o clique
    # se o menu ainda estiver em animação. O hover sobre o item pai
    # força a abertura visual do submenu antes do clique.
    menu_financeiro.click()
    page.wait_for_timeout(500)
    menu_financeiro.hover()

    folha_apoio = page.get_by_role(
        "link",
        name="Folha de apoio",
    )

    folha_apoio.wait_for(
        state="visible",
        timeout=30_000,
    )

    folha_apoio.click()

    page.locator(
        "#j_idt357_content"
    ).get_by_text(
        "Correntista"
    ).click()

    page.get_by_text(
        "Relatório descritivo"
    ).click()

    page.get_by_role(
        "button",
        name="Adicionar",
    ).click()

    campo_busca = page.get_by_role(
        "textbox",
        name="Busca por razão social",
    )

    campo_busca.wait_for(
        state="visible",
        timeout=30_000,
    )

    campo_busca.fill(
        codigo_correntista
    )

    page.get_by_role(
        "button",
        name="Buscar",
    ).click()

    link_correntista = page.get_by_role(
        "link",
        name=nome_correntista,
    )

    link_correntista.wait_for(
        state="visible",
        timeout=30_000,
    )

    link_correntista.click()

    page.locator(
        "#j_idt369_input"
    ).wait_for(
        state="visible",
        timeout=30_000,
    )

    log(
        "Relatório descritivo aberto para o correntista "
        f"{codigo_correntista}."
    )


def _nome_periodo_folha_apoio(
    inicio: int,
    fim: int,
    mes: int,
    ano: int,
) -> str:
    nome_mes = MESES_PT_BR.get(
        mes,
        f"mes_{mes:02d}",
    )

    return (
        f"{inicio:02d}_a_{fim:02d}_"
        f"{nome_mes}_{ano}.xlsx"
    )


def _selecionar_mes_ano_relatorio_descritivo_fraction(
    page: Page,
    mes: int,
    ano: int,
    log: Callable[[str], None],
) -> None:
    """
    Ajusta o calendário do relatório para o mês/ano solicitado.

    No calendário atual do Fraction, ``select month`` e ``select year``
    podem ser elementos ``span`` (e não ``select`` HTML). Por isso, não
    usamos ``select_option``. A navegação é feita pelos botões
    ``Anterior``/``Próximo`` e validada pelo texto exibido no calendário.
    """
    seletor_mes = page.get_by_label(
        "select month"
    )

    seletor_ano = page.get_by_label(
        "select year"
    )

    seletor_mes.wait_for(
        state="visible",
        timeout=30_000,
    )

    seletor_ano.wait_for(
        state="visible",
        timeout=30_000,
    )

    nomes_mes = {
        nome.lower(): numero
        for numero, nome in MESES_PT_BR.items()
    }

    # O Fraction pode exibir março com ou sem acento dependendo da tela.
    nomes_mes["março"] = 3

    def obter_mes_ano_exibidos() -> tuple[int, int]:
        texto_mes = seletor_mes.inner_text().strip().lower()
        texto_ano = seletor_ano.inner_text().strip()

        mes_atual = nomes_mes.get(texto_mes)
        if mes_atual is None:
            raise RuntimeError(
                "Não foi possível identificar o mês exibido no calendário "
                f"do Fraction: {texto_mes!r}."
            )

        try:
            ano_atual = int(texto_ano)
        except ValueError as exc:
            raise RuntimeError(
                "Não foi possível identificar o ano exibido no calendário "
                f"do Fraction: {texto_ano!r}."
            ) from exc

        return mes_atual, ano_atual

    alvo = ano * 12 + mes

    for _ in range(120):
        mes_atual, ano_atual = obter_mes_ano_exibidos()
        atual = ano_atual * 12 + mes_atual

        if atual == alvo:
            log(
                "Calendário ajustado para "
                f"{mes:02d}/{ano}."
            )
            return

        if atual > alvo:
            botao_anterior = page.get_by_title(
                "Anterior"
            )
            botao_anterior.wait_for(
                state="visible",
                timeout=30_000,
            )
            botao_anterior.click()
        else:
            botao_proximo = page.get_by_title(
                "Próximo"
            )
            botao_proximo.wait_for(
                state="visible",
                timeout=30_000,
            )
            botao_proximo.click()

        page.wait_for_timeout(150)

    raise RuntimeError(
        "Não foi possível posicionar o calendário do Fraction em "
        f"{mes:02d}/{ano}."
    )


def _selecionar_dia_relatorio_descritivo_fraction(
    page: Page,
    dia: int,
    log: Callable[[str], None],
    *,
    mes: int | None = None,
    ano: int | None = None,
) -> None:
    campo_data = page.locator(
        "#j_idt369_input"
    )

    campo_data.click()

    seletor_mes = page.get_by_label(
        "select month"
    )

    seletor_mes.wait_for(
        state="visible",
        timeout=30_000,
    )

    if mes is not None and ano is not None:
        _selecionar_mes_ano_relatorio_descritivo_fraction(
            page,
            mes,
            ano,
            log,
        )
    else:
        try:
            mes_visivel = seletor_mes.input_value()
            log(
                "Calendário aberto. "
                f"Mês identificado: {mes_visivel}."
            )
        except Exception:
            log("Calendário aberto.")

    link_dia = page.get_by_role(
        "link",
        name=str(dia),
        exact=True,
    )

    link_dia.wait_for(
        state="visible",
        timeout=30_000,
    )

    link_dia.click()

    log(
        f"Dia {dia} selecionado no relatório descritivo."
    )


def baixar_periodo_relatorio_descritivo_fraction(
    page: Page,
    pasta_destino: Path | str,
    *,
    dia: int,
    inicio_periodo: int,
    fim_periodo: int,
    mes: int,
    ano: int,
    log: Callable[[str], None],
) -> Path:
    pasta_destino = Path(
        pasta_destino
    )

    pasta_destino.mkdir(
        parents=True,
        exist_ok=True,
    )

    _selecionar_dia_relatorio_descritivo_fraction(
        page,
        dia,
        log,
        mes=mes,
        ano=ano,
    )

    exportador = page.locator(
        "#id_exportar_excel"
    )

    exportador.wait_for(
        state="visible",
        timeout=120_000,
    )

    nome_arquivo = _nome_periodo_folha_apoio(
        inicio_periodo,
        fim_periodo,
        mes,
        ano,
    )

    caminho = (
        pasta_destino
        / nome_arquivo
    )

    with page.expect_download(
        timeout=240_000
    ) as download_info:
        exportador.click()

    download = download_info.value

    download.save_as(
        caminho
    )

    log(
        "Relatório descritivo baixado: "
        f"{caminho.name}"
    )

    return caminho


def baixar_relatorios_descritivos_correntista_fraction(
    page: Page,
    pasta_destino: Path | str,
    log: Callable[[str], None],
    *,
    mes: int | None = None,
    ano: int | None = None,
    codigo_correntista: str = "014995",
    nome_correntista: str = "ARCOS DOURADOS COMERCIO DE",
) -> list[Path]:
    """
    Baixa dois relatórios quinzenais:
      - 01 a 15
      - 16 a 30

    Nesta primeira versão não troca de mês no calendário.
    Isso será acrescentado depois, conforme a regra de negócio.
    """
    agora = datetime.now()

    if mes is None:
        mes = agora.month

    if ano is None:
        ano = agora.year

    if not 1 <= mes <= 12:
        raise ValueError(
            f"Mês inválido: {mes}."
        )

    abrir_relatorio_descritivo_correntista_fraction(
        page,
        log,
        codigo_correntista=codigo_correntista,
        nome_correntista=nome_correntista,
    )

    arquivos = []

    arquivos.append(
        baixar_periodo_relatorio_descritivo_fraction(
            page,
            pasta_destino,
            dia=15,
            inicio_periodo=1,
            fim_periodo=15,
            mes=mes,
            ano=ano,
            log=log,
        )
    )

    if not (
        mes == agora.month
        and ano == agora.year
        and agora.day <= 15
    ):
        ultimo_dia_mes = (
            date(ano, mes, 1) + timedelta(days=32)
        ).replace(day=1) - timedelta(days=1)

        arquivos.append(
            baixar_periodo_relatorio_descritivo_fraction(
                page,
                pasta_destino,
                dia=ultimo_dia_mes.day,
                inicio_periodo=16,
                fim_periodo=ultimo_dia_mes.day,
                mes=mes,
                ano=ano,
                log=log,
            )
        )

    log(
        "Download dos relatórios descritivos concluído. "
        f"{len(arquivos)} arquivo(s) salvo(s)."
    )

    return arquivos



def baixar_relatorios_descritivos_correntista_ate_data_fraction(
    page: Page,
    pasta_destino: Path | str,
    log: Callable[[str], None],
    *,
    data_limite: date | datetime | None = None,
    codigo_correntista: str = "014995",
    nome_correntista: str = "ARCOS DOURADOS COMERCIO DE",
) -> list[Path]:
    """
    Baixa as Folhas de apoio em quinzenas, do período mais recente
    até o mês/quinzena que alcança ``data_limite``.

    Exemplo em 30/09 com uma aba vazia de agosto:
        16 a 30/09, 01 a 15/09, 16 a 31/08, 01 a 15/08.

    A ordem dos arquivos retornados é a mesma ordem de busca:
    mais recente -> mais antiga.
    """
    referencia = (
        data_limite.date()
        if isinstance(data_limite, datetime)
        else data_limite
    )

    if referencia is None:
        referencia = datetime.now().date()

    inicio_mes_atual = datetime.now().date().replace(
        day=1
    )
    inicio_mes_limite = referencia.replace(
        day=1
    )

    if inicio_mes_limite > inicio_mes_atual:
        raise ValueError(
            "A data limite da Folha de apoio não pode estar no futuro."
        )

    abrir_relatorio_descritivo_correntista_fraction(
        page,
        log,
        codigo_correntista=codigo_correntista,
        nome_correntista=nome_correntista,
    )

    arquivos: list[Path] = []
    cursor = inicio_mes_atual

    while cursor >= inicio_mes_limite:
        ultimo_dia = (
            cursor.replace(
                day=28
            )
            + timedelta(
                days=4
            )
        ).replace(
            day=1
        ) - timedelta(days=1)

        mes = cursor.month
        ano = cursor.year

        # No mês atual, antes do dia 16, ainda não existe segunda quinzena.
        # Nos meses anteriores (ou após o dia 15 do mês atual), mantemos
        # a ordem mais recente -> antiga.
        baixar_segunda_quinzena = (
            cursor != inicio_mes_atual
            or datetime.now().day > 15
        )

        if baixar_segunda_quinzena:
            arquivos.append(
                baixar_periodo_relatorio_descritivo_fraction(
                    page,
                    pasta_destino,
                    dia=ultimo_dia.day,
                    inicio_periodo=16,
                    fim_periodo=ultimo_dia.day,
                    mes=mes,
                    ano=ano,
                    log=log,
                )
            )

        arquivos.append(
            baixar_periodo_relatorio_descritivo_fraction(
                page,
                pasta_destino,
                dia=15,
                inicio_periodo=1,
                fim_periodo=15,
                mes=mes,
                ano=ano,
                log=log,
            )
        )

        if mes == 1:
            cursor = cursor.replace(
                year=ano - 1,
                month=12,
                day=1,
            )
        else:
            cursor = cursor.replace(
                month=mes - 1,
                day=1,
            )

    log(
        "Download das Folhas de apoio concluído. "
        f"{len(arquivos)} arquivo(s) salvo(s), "
        "do período mais recente ao mais antigo."
    )

    return arquivos

# -----------------------------------------------------------------------------
# Operações reutilizáveis do Manifesto no Fraction
# -----------------------------------------------------------------------------

ID_DESTINO_LABEL = '[id="form_envio_remessa:selectDestino_label"]'
ID_DESTINO_FILTRO = '[id="form_envio_remessa:selectDestino_filter"]'
ID_DESTINO_BUSCAR = ".ui-selectonemenu-filter-container > .ui-icon"
ID_ROTA_LABEL = '[id="form_envio_remessa:selectRota_label"]'
ID_ROTA_PANEL = '[id="form_envio_remessa:selectRota_panel"]'
ID_CTES = '[id="form_envio_remessa:txtArea"]'
TAMANHO_LOTE_CTES = 1000
SELETOR_DATA_INICIAL_PERFORMANCE = '[id="formPerfEntFiltro:dtini_input"]'
SELETOR_DATA_FINAL_PERFORMANCE = '[id="formPerfEntFiltro:dtfim_input"]'
TIMEOUT_DOWNLOAD_PERFORMANCE_INTERVALO_MS = 600_000


def _log(log, mensagem: str) -> None:
    if log is None:
        print(mensagem)
    else:
        log(mensagem)


def _normalizar_rota(valor: object) -> str:
    texto = unicodedata.normalize("NFKD", str(valor).strip())
    texto = "".join(caractere for caractere in texto if not unicodedata.combining(caractere))
    return " ".join(texto.upper().split())


def _normalizar_unidade_fraction(valor: object) -> str:
    nome = _normalizar_rota(valor)
    aliases_catalogo = {"FL MANAUS": "FL MANAUS JAD"}
    return aliases_catalogo.get(nome, nome)


def _normalizar_uf(valor: object) -> str:
    if valor is None or pd.isna(valor):
        return ""
    uf = _normalizar_rota(valor)
    nomes = {
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
    return nomes.get(uf, uf)


def carregar_regras_triagem_rotas(
    caminho: Path = ARQUIVO_ROTAS_ROMANEIOS,
) -> list[dict[str, object]]:
    """Carrega a configuração hierárquica: triagem, localidades e unidade."""
    if not caminho.exists():
        raise FileNotFoundError(f"Base de rotas não encontrada: '{caminho}'.")
    with caminho.open("r", encoding="utf-8-sig") as arquivo:
        dados = json.load(arquivo)
    if not isinstance(dados, dict):
        raise ValueError("A base de rotas precisa ser um objeto JSON.")
    regras = dados.get("triagens")
    if not isinstance(regras, list):
        raise ValueError("A base de rotas precisa conter a lista 'triagens'.")

    resultado: list[dict[str, object]] = []
    for indice, regra in enumerate(regras, start=1):
        if not isinstance(regra, dict):
            raise ValueError(f"A triagem #{indice} precisa ser um objeto JSON.")
        triagem = " ".join(str(regra.get("triagem") or "").split())
        unidade = " ".join(str(regra.get("unidade") or "").split())
        if not triagem or not unidade:
            raise ValueError(
                f"A triagem #{indice} precisa conter 'triagem' e 'unidade'."
            )

        localidades: dict[str, list[str]] = {}
        marcadores_triagem = {
            _normalizar_rota(parte)
            for parte in triagem.split(",")
            if _normalizar_rota(parte)
        }
        for campo in ("ufs", "cidades", "destinos"):
            valores = regra.get(campo, [])
            if not isinstance(valores, list):
                raise ValueError(
                    f"O campo '{campo}' da triagem '{triagem}' precisa ser uma lista."
                )
            normalizados = []
            for valor in valores:
                normalizado = (
                    _normalizar_uf(valor)
                    if campo == "ufs"
                    else _normalizar_rota(valor)
                )
                if (
                    campo != "ufs"
                    and normalizado in marcadores_triagem
                ):
                    # In grouped-pallet rules, triage codes are visual section
                    # markers (A, B, P05, ...), not locality names.
                    continue
                if normalizado and normalizado not in normalizados:
                    normalizados.append(normalizado)
            localidades[campo] = normalizados

        if not any(localidades.values()):
            raise ValueError(
                f"A triagem '{triagem}' precisa ter ao menos uma UF, cidade ou destino."
            )
        resultado.append(
            {
                "triagem": triagem,
                "unidade": unidade,
                **localidades,
            }
        )
    return resultado


def resolver_rota_por_localidade(
    regras: list[dict[str, object]],
    *,
    uf: object = "",
    cidade: object = "",
    destino: object = "",
    triagem: object = "",
) -> tuple[str, str]:
    """Resolve destino com prioridade para destino, cidade e depois UF.

    Quando informada, a triagem pode ser o grupo completo ou um de seus códigos.
    Códigos separados por vírgula dentro da regra representam um único palete.
    Sem triagem, só retorna a triagem quando um grupo configurado é único.
    """
    uf_normalizada = _normalizar_uf(uf)
    cidade_normalizada = _normalizar_rota(cidade)
    destino_normalizado = _normalizar_rota(destino)
    triagem_normalizada = _normalizar_rota(triagem)
    triagem_informada = " ".join(str(triagem or "").split())
    triagens_compativeis: dict[str, str] = {}

    def codigos_da_triagem(regra: dict[str, object]) -> tuple[str, set[str]]:
        nome = " ".join(str(regra.get("triagem") or "").split())
        componentes = {
            _normalizar_rota(parte)
            for parte in nome.split(",")
            if _normalizar_rota(parte)
        }
        return nome, componentes

    candidatas: list[tuple[int, str, str]] = []

    for regra in regras:
        nome_triagem, componentes_triagem = codigos_da_triagem(regra)
        unidade = str(regra.get("unidade") or "").strip()
        if triagem_normalizada:
            if (
                _normalizar_rota(nome_triagem) != triagem_normalizada
                and triagem_normalizada not in componentes_triagem
            ):
                continue
            triagens_compativeis.setdefault(_normalizar_rota(nome_triagem), nome_triagem)
        prioridades = []
        if destino_normalizado in regra.get("destinos", []):
            prioridades.append(3)
        if cidade_normalizada in regra.get("cidades", []):
            prioridades.append(2)
        if uf_normalizada in regra.get("ufs", []):
            prioridades.append(1)
        if prioridades:
            candidatas.append((max(prioridades), unidade, nome_triagem))

    triagem_canonica = (
        next(iter(triagens_compativeis.values()))
        if len(triagens_compativeis) == 1
        else triagem_informada
    )
    if not candidatas:
        return "Sem rota", triagem_canonica
    maior_prioridade = max(candidata[0] for candidata in candidatas)
    resultados = {
        (_normalizar_rota(unidade), _normalizar_rota(nome_triagem)):
        (unidade, nome_triagem)
        for prioridade, unidade, nome_triagem in candidatas
        if prioridade == maior_prioridade
    }
    unidades = {_normalizar_rota(unidade) for unidade, _ in resultados.values()}
    triagens_unicas: dict[str, str] = {}
    for _, nome_triagem in resultados.values():
        triagens_unicas.setdefault(_normalizar_rota(nome_triagem), nome_triagem)
    if len(unidades) != 1:
        return "Rota ambígua", triagem_canonica
    unidade = next(iter(resultados.values()))[0]
    if len(triagens_unicas) != 1:
        return unidade, ""
    return unidade, next(iter(triagens_unicas.values()))


def listar_triagens_por_localidade(
    regras: list[dict[str, object]],
    *,
    uf: object = "",
    cidade: object = "",
    destino: object = "",
    triagem: object = "",
) -> list[str]:
    """Lista as triagens compatíveis na prioridade mais alta da localidade."""
    uf_normalizada = _normalizar_uf(uf)
    cidade_normalizada = _normalizar_rota(cidade)
    destino_normalizado = _normalizar_rota(destino)
    triagem_normalizada = _normalizar_rota(triagem)
    candidatas: list[tuple[int, str]] = []

    for regra in regras:
        nome_triagem = " ".join(str(regra.get("triagem") or "").split())
        componentes_triagem = {
            _normalizar_rota(parte)
            for parte in nome_triagem.split(",")
            if _normalizar_rota(parte)
        }
        if triagem_normalizada and (
            _normalizar_rota(nome_triagem) != triagem_normalizada
            and triagem_normalizada not in componentes_triagem
        ):
            continue

        prioridades = []
        if destino_normalizado in regra.get("destinos", []):
            prioridades.append(3)
        if cidade_normalizada in regra.get("cidades", []):
            prioridades.append(2)
        if uf_normalizada in regra.get("ufs", []):
            prioridades.append(1)
        if prioridades:
            candidatas.append((max(prioridades), nome_triagem))

    if not candidatas:
        return []
    maior_prioridade = max(prioridade for prioridade, _ in candidatas)
    triagens: dict[str, str] = {}
    for prioridade, nome_triagem in candidatas:
        if prioridade == maior_prioridade and nome_triagem:
            triagens.setdefault(_normalizar_rota(nome_triagem), nome_triagem)
    return list(triagens.values())


def carregar_regras_rotas_com_destino_e_triagem(
    caminho: Path = ARQUIVO_ROTAS_ROMANEIOS,
) -> tuple[
    dict[str, str],
    dict[str, str],
    dict[str, str],
    dict[str, str],
    dict[str, str],
    dict[str, str],
]:
    """Projeção de compatibilidade para consumidores antigos das regras."""
    regras = carregar_regras_triagem_rotas(caminho)
    localidades: dict[str, dict[str, tuple[str, list[str]]]] = {
        "ufs": {},
        "cidades": {},
        "destinos": {},
    }
    for regra in regras:
        unidade = str(regra["unidade"])
        triagem = str(regra["triagem"])
        for campo, chave in (
            ("ufs", "uf"),
            ("cidades", "cidade"),
            ("destinos", "destino"),
        ):
            for valor in regra[campo]:
                existente = localidades[campo].get(valor)
                if (
                    existente
                    and _normalizar_rota(existente[0]) != _normalizar_rota(unidade)
                ):
                    raise ValueError(
                        f"A localidade '{valor}' está associada a mais de uma unidade; "
                        "use o carregador hierárquico de triagens."
                    )
                unidade_existente, triagens = existente or (unidade, [])
                if triagem not in triagens:
                    triagens.append(triagem)
                localidades[campo][valor] = (unidade_existente, triagens)

    def projetar(campo: str) -> tuple[dict[str, str], dict[str, str]]:
        rotas = {chave: valor[0] for chave, valor in localidades[campo].items()}
        triagens = {
            chave: ", ".join(valor[1])
            for chave, valor in localidades[campo].items()
        }
        return rotas, triagens

    rotas_por_uf, triagens_por_uf = projetar("ufs")
    rotas_por_cidade, triagens_por_cidade = projetar("cidades")
    rotas_por_destino, triagens_por_destino = projetar("destinos")
    return (
        rotas_por_uf,
        rotas_por_cidade,
        rotas_por_destino,
        triagens_por_uf,
        triagens_por_cidade,
        triagens_por_destino,
    )


def carregar_regras_rotas_localidade_com_triagem(
    caminho: Path = ARQUIVO_ROTAS_ROMANEIOS,
) -> tuple[dict[str, str], dict[str, str], dict[str, str], dict[str, str]]:
    """Mantém a interface antiga para regras de UF e cidade."""
    (
        rotas_por_uf,
        rotas_por_cidade,
        _,
        triagens_por_uf,
        triagens_por_cidade,
        _,
    ) = carregar_regras_rotas_com_destino_e_triagem(caminho)
    return (
        rotas_por_uf,
        rotas_por_cidade,
        triagens_por_uf,
        triagens_por_cidade,
    )


def carregar_regras_rotas_localidade(
    caminho: Path = ARQUIVO_ROTAS_ROMANEIOS,
) -> tuple[dict[str, str], dict[str, str]]:
    """Carrega as unidades atendentes por UF e por cidade normalizada."""
    rotas_por_uf, rotas_por_cidade, _, _ = (
        carregar_regras_rotas_localidade_com_triagem(caminho)
    )
    return rotas_por_uf, rotas_por_cidade


def carregar_destinos_fraction(
    caminho: Path = ARQUIVO_UNIDADES_FRACTION,
) -> list[dict[str, str]]:
    if not caminho.exists():
        raise FileNotFoundError(f"Arquivo de unidades Fraction não encontrado: '{caminho}'.")
    with caminho.open("r", encoding="utf-8-sig") as arquivo:
        dados = json.load(arquivo)
    destinos = dados.get("unidades") if isinstance(dados, dict) else None
    if not isinstance(destinos, list):
        raise ValueError("O catálogo de unidades precisa conter uma lista 'unidades'.")
    resultado = []
    for destino in destinos:
        if not isinstance(destino, dict):
            raise ValueError("Cada unidade precisa ser um objeto JSON.")
        if destino.get("ativo", True) is False:
            continue
        if not destino.get("unidade") or not destino.get("codigo"):
            raise ValueError("Cada unidade precisa ter 'unidade' e 'codigo'.")
        resultado.append({"nome": str(destino["unidade"]).strip(), "codigo": str(destino["codigo"]).strip()})
    return resultado


def carregar_codigos_preferenciais_unidades(
    caminho: Path = ARQUIVO_UNIDADES_FRACTION,
) -> dict[str, str]:
    """Carrega desempates de código junto ao catálogo de unidades do Fraction."""
    if not caminho.exists():
        raise FileNotFoundError(f"Arquivo de unidades Fraction não encontrado: '{caminho}'.")
    with caminho.open("r", encoding="utf-8-sig") as arquivo:
        dados = json.load(arquivo)
    if not isinstance(dados, dict):
        raise ValueError("O catálogo de unidades precisa ser um objeto JSON.")
    preferencias = dados.get("codigo_preferencial_por_unidade", {})
    if not isinstance(preferencias, dict):
        raise ValueError("'codigo_preferencial_por_unidade' precisa ser um objeto.")
    return {
        _normalizar_unidade_fraction(unidade): str(codigo).strip()
        for unidade, codigo in preferencias.items()
        if str(unidade).strip() and str(codigo).strip()
    }


def selecionar_rota_manifesto(page, *, log=None) -> str:
    """Seleciona a única opção com RODO e confirma que o campo foi atualizado."""
    painel = page.locator(ID_ROTA_PANEL)
    for tentativa in range(1, 4):
        page.locator(ID_ROTA_LABEL).click()
        opcoes = page.get_by_role(
            "option",
            name=re.compile("RODO", re.IGNORECASE),
        )
        try:
            opcoes.first.wait_for(state="visible", timeout=10000)
        except PlaywrightTimeoutError as erro:
            raise RuntimeError(
                "Nenhuma opção de rota contendo 'RODO' apareceu no Fraction."
            ) from erro

        quantidade = opcoes.count()
        nomes = [nome.strip() for nome in opcoes.all_inner_texts() if nome.strip()]
        if quantidade != 1:
            raise RuntimeError(
                "Era esperada uma única opção de rota contendo 'RODO', "
                f"mas foram encontradas {quantidade}: {nomes}"
            )

        selecionada = nomes[0] if nomes else "opção contendo RODO"
        _log(log, f"Tentativa {tentativa}/3: selecionando rota '{selecionada}'.")
        opcoes.click()
        page.keyboard.press("Escape")
        try:
            painel.wait_for(state="hidden", timeout=3000)
        except PlaywrightTimeoutError:
            _log(log, "Menu da rota ainda aberto; fechando pelo seletor.")
            page.locator(ID_ROTA_LABEL).click()
            painel.wait_for(state="hidden", timeout=5000)

        rotulo = page.locator(ID_ROTA_LABEL)
        for _ in range(20):
            valor_atual = rotulo.inner_text().strip()
            if _normalizar_rota(selecionada) in _normalizar_rota(valor_atual):
                _log(log, f"Rota confirmada no campo: {valor_atual}.")
                return selecionada
            page.wait_for_timeout(250)

        _log(
            log,
            f"A rota não apareceu no campo após a tentativa {tentativa}/3. "
            f"Valor atual: '{rotulo.inner_text().strip()}'.",
        )

    raise RuntimeError(
        "A opção com RODO foi clicada, mas o campo Rota não confirmou o valor "
        "após três tentativas."
    )


def aguardar_carregamento_manifesto(page, *, log=None) -> None:
    """Espera o spinner do Ajax aparecer e desaparecer após adicionar CTEs."""
    spinner = page.get_by_text("Carregando").first
    _log(log, "Aguardando o spinner 'Carregando' aparecer após Adicionar.")
    try:
        spinner.wait_for(state="visible", timeout=120_000)
    except PlaywrightTimeoutError as erro:
        raise PlaywrightTimeoutError(
            "O Fraction não exibiu o spinner 'Carregando' após adicionar os CTEs; "
            "não é possível confirmar que a lista terminou de atualizar."
        ) from erro

    _log(log, "Spinner detectado; aguardando o fim do carregamento dos CTEs.")
    spinner.wait_for(state="hidden", timeout=180_000)
    _log(log, "Spinner 'Carregando' desapareceu; atualização concluída.")


def aguardar_ajax_fraction(page, *, log=None, timeout_ms: int = 120_000) -> None:
    """Espera o AJAX/PrimeFaces terminar depois de paginar ou mudar a tabela."""
    page.wait_for_timeout(500)
    page.wait_for_function(
        """() => {
            const visivel = (elemento) => elemento.getClientRects().length > 0 &&
                getComputedStyle(elemento).visibility !== "hidden" &&
                getComputedStyle(elemento).display !== "none";
            const carregando = Array.from(document.querySelectorAll("body *"))
                .some((elemento) => elemento.children.length === 0 &&
                    elemento.innerText?.trim().includes("Carregando") && visivel(elemento));
            const jqueryOcupado = window.jQuery && window.jQuery.active > 0;
            const fila = window.PrimeFaces?.ajax?.Queue;
            const filaVazia = !fila || typeof fila.isEmpty !== "function" || fila.isEmpty();
            const ariaOcupado = Array.from(document.querySelectorAll('[aria-busy="true"]'))
                .some(visivel);
            return !carregando && !jqueryOcupado && filaVazia && !ariaOcupado;
        }""",
        timeout=timeout_ms,
    )
    _log(log, "AJAX do Fraction encerrado; tabela pronta para leitura.")


def consultar_ctes_por_etiquetas_fraction(
    etiquetas: list[str],
    *,
    log=None,
    tamanho_lote: int = TAMANHO_LOTE_CTES,
    por_shipment_id: bool = False,
) -> dict[str, str]:
    """Consulta no Envio de remessa o CTE correspondente a cada etiqueta.

    A rotina só adiciona as etiquetas e lê a tabela Código Barras/Remessa.
    Com ``por_shipment_id=True``, recebe ShipmentIds e só retorna associações
    que apontam para uma Remessa única. Ela não aciona Processar lista.
    """
    etiquetas_unicas = list(dict.fromkeys(
        str(etiqueta).strip() for etiqueta in etiquetas if str(etiqueta).strip()
    ))
    if not etiquetas_unicas:
        return {}
    if tamanho_lote < 1 or tamanho_lote > TAMANHO_LOTE_CTES:
        raise ValueError(
            f"O tamanho do lote deve ficar entre 1 e {TAMANHO_LOTE_CTES}."
        )
    if not URL_FRACTION or not FRACTION_USER or not FRACTION_PASSWORD:
        raise ValueError(
            "URL_FRACTION, FRACTION_USER e FRACTION_PASSWORD precisam estar configurados."
        )

    def registrar(mensagem: str) -> None:
        _log(log, mensagem)

    def abrir_envio_remessa(page) -> None:
        registrar(
            "Recarregando o Fraction antes do lote para limpar resultados "
            "de consultas anteriores."
        )
        page.reload(wait_until="domcontentloaded", timeout=120_000)
        page.get_by_role("link", name="Operacional").wait_for(
            state="visible", timeout=120_000
        )
        page.get_by_role("link", name="Operacional").click()
        page.get_by_role("link", name="Manifesto", exact=True).click()
        page.get_by_role("link", name="Envio de remessa").click()
        page.locator(ID_CTES).wait_for(state="visible", timeout=120_000)

    def normalizar_cabecalho(valor: object) -> str:
        return _normalizar_rota(valor).replace(" ", "")

    def consultar_lote(page, lote: list[str], numero_lote: int) -> dict[str, str]:
        abrir_envio_remessa(page)
        tipo_consulta = "ShipmentId(s)" if por_shipment_id else "etiqueta(s)"
        registrar(
            f"Lote {numero_lote}: preenchendo {len(lote)} {tipo_consulta} no campo "
            "do Envio de remessa."
        )
        page.locator(ID_CTES).fill("\n".join(lote))
        page.get_by_role("button", name="Adicionar").click(timeout=120_000)
        registrar(
            f"Lote {numero_lote}: consulta adicionada; aguardando atualização "
            "da tabela. Processar lista não será clicado."
        )
        aguardar_carregamento_manifesto(page, log=log)

        seletor_linhas = page.get_by_label("Rows Per Page")
        seletor_linhas.wait_for(state="visible", timeout=60_000)
        quantidade = len(lote)
        tamanho_pagina = 100 if por_shipment_id else next(
            (limite for limite in (10, 25, 50, 100) if quantidade <= limite),
            100,
        )
        seletor_linhas.select_option(str(tamanho_pagina))
        registrar(
            f"Lote {numero_lote}: tabela configurada para {tamanho_pagina} "
            "linhas por página."
        )
        aguardar_ajax_fraction(page, log=log)

        cabecalho_barras = page.get_by_role(
            "columnheader", name=re.compile(r"Código Barras", re.IGNORECASE)
        ).first
        cabecalho_barras.wait_for(state="visible", timeout=60_000)
        tabela = cabecalho_barras.locator("xpath=ancestor::table[1]")
        cabecalhos = tabela.locator("thead tr").last.locator("th")
        indice_barras = None
        indice_remessa = None
        for indice in range(cabecalhos.count()):
            texto = normalizar_cabecalho(cabecalhos.nth(indice).inner_text())
            if "CODIGOBARRAS" in texto:
                indice_barras = indice
            elif "REMESSA" in texto:
                indice_remessa = indice
        if indice_barras is None or indice_remessa is None:
            raise RuntimeError(
                "A tabela do Envio de remessa não exibiu as colunas "
                "Código Barras e Remessa."
            )

        encontrados: dict[str, str] = {}
        ambiguos: set[str] = set()
        pagina_atual = 1
        paginas_visitadas: set[int] = set()
        while pagina_atual not in paginas_visitadas:
            paginas_visitadas.add(pagina_atual)
            linhas = tabela.locator("tbody tr")
            for indice_linha in range(linhas.count()):
                celulas = linhas.nth(indice_linha).locator("td")
                if celulas.count() <= max(indice_barras, indice_remessa):
                    continue
                codigo_barras = celulas.nth(indice_barras).inner_text().strip()
                remessa = celulas.nth(indice_remessa).inner_text().strip()
                if not codigo_barras or not remessa:
                    continue
                chave = (
                    codigo_barras.split("$", 1)[0].strip()
                    if por_shipment_id
                    else codigo_barras
                )
                if chave not in lote or chave in ambiguos:
                    continue
                anterior = encontrados.get(chave)
                if anterior and anterior != remessa:
                    if por_shipment_id:
                        encontrados.pop(chave, None)
                        ambiguos.add(chave)
                        registrar(
                            f"ShipmentId {chave} retornou mais de uma Remessa/CTE; "
                            "a associação alternativa será ignorada por ambiguidade."
                        )
                        continue
                    raise ValueError(
                        f"A etiqueta {codigo_barras} apareceu associada às remessas "
                        f"{anterior} e {remessa}."
                    )
                if not anterior:
                    encontrados[chave] = remessa
                    if por_shipment_id:
                        registrar(
                            f"ShipmentId {chave}: código de barras "
                            f"'{codigo_barras}' associado à Remessa {remessa}."
                        )
                    else:
                        registrar(
                            f"Etiqueta {codigo_barras} associada à Remessa {remessa}."
                        )
            registrar(
                f"Lote {numero_lote}, página {pagina_atual}: "
                f"{len(encontrados)}/{len(lote)} consulta(s) localizada(s) até agora."
            )

            if not por_shipment_id and len(encontrados) == len(lote):
                break

            link_proxima = page.get_by_role(
                "link", name=f"Page {pagina_atual + 1}", exact=True
            )
            if link_proxima.count() and link_proxima.first.is_visible():
                link_proxima.first.click(timeout=30_000)
                pagina_atual += 1
                aguardar_ajax_fraction(page, log=log)
                continue

            botao_proxima = page.locator(".ui-paginator-next").last
            if not botao_proxima.count():
                break
            classes = (botao_proxima.get_attribute("class") or "").casefold()
            aria_disabled = (botao_proxima.get_attribute("aria-disabled") or "").casefold()
            if "ui-state-disabled" in classes or aria_disabled == "true":
                break
            if not botao_proxima.is_enabled():
                break
            botao_proxima.click(timeout=30_000)
            pagina_ativa = page.locator(
                ".ui-paginator-page.ui-state-active"
            ).last
            try:
                pagina_ativa.wait_for(state="visible", timeout=30_000)
                pagina_atual = int(pagina_ativa.inner_text().strip())
            except (PlaywrightTimeoutError, ValueError):
                pagina_atual += 1
            aguardar_ajax_fraction(page, log=log)

        registrar(
            f"Lote {numero_lote}: {len(encontrados)}/{len(lote)} "
            f"{tipo_consulta} associada(s) a uma Remessa única na tabela."
        )
        if ambiguos:
            registrar(
                f"Lote {numero_lote}: {len(ambiguos)} ShipmentId(s) tinham "
                "mais de uma Remessa/CTE e não serão usados como fallback."
            )
        return encontrados

    resultado: dict[str, str] = {}
    with sync_playwright() as playwright:
        browser = None
        contexto = None
        page = None

        def fechar_sessao() -> None:
            nonlocal browser, contexto, page
            if contexto is not None:
                try:
                    contexto.close()
                except Exception as erro:
                    registrar(f"Aviso ao fechar o Fraction: {type(erro).__name__}: {erro}")
            if browser is not None:
                try:
                    browser.close()
                except Exception as erro:
                    registrar(f"Aviso ao fechar Chromium: {type(erro).__name__}: {erro}")
            browser = contexto = page = None

        def abrir_sessao() -> None:
            nonlocal browser, contexto, page
            fechar_sessao()
            browser = playwright.chromium.launch(headless=HEADLESS, slow_mo=SLOW_MO_MS)
            contexto = browser.new_context()
            page = contexto.new_page()
            page.on("pageerror", lambda erro: registrar(f"Erro JavaScript no Fraction: {erro}"))
            registrar("Abrindo sessão padrão do Fraction para consultar as etiquetas.")
            login_fraction(page, FRACTION_USER, FRACTION_PASSWORD, registrar)

        def recuperar_sessao() -> None:
            recuperar_sessao_fraction(
                page,
                registrar,
                login_callback=lambda: login_fraction(
                    page, FRACTION_USER, FRACTION_PASSWORD, registrar
                ),
            )

        try:
            ultimo_erro = None
            for tentativa in range(1, 4):
                try:
                    registrar(f"Abrindo o Fraction (tentativa {tentativa}/3).")
                    abrir_sessao()
                    ultimo_erro = None
                    break
                except Exception as erro:
                    ultimo_erro = erro
                    registrar(
                        f"Falha ao abrir o Fraction ({tentativa}/3): "
                        f"{type(erro).__name__}: {erro}"
                    )
                    if tentativa < 3:
                        time.sleep(min(tentativa * 2, 5))
            if ultimo_erro is not None:
                raise RuntimeError(
                    f"Não foi possível abrir o Fraction: {ultimo_erro}"
                ) from ultimo_erro

            lotes = [
                etiquetas_unicas[inicio : inicio + tamanho_lote]
                for inicio in range(0, len(etiquetas_unicas), tamanho_lote)
            ]
            for numero_lote, lote in enumerate(lotes, start=1):
                tipo_consulta = "ShipmentId(s)" if por_shipment_id else "etiqueta(s)"
                registrar(
                    f"Consultando lote {numero_lote}/{len(lotes)} com "
                    f"{len(lote)} {tipo_consulta}."
                )
                parte = executar_com_retentativas_fraction(
                    lambda lote=lote, numero_lote=numero_lote: consultar_lote(
                        page, lote, numero_lote
                    ),
                    recuperar_sessao,
                    registrar,
                    descricao=(
                        f"Consulta de {tipo_consulta}, lote {numero_lote}"
                    ),
                    tentativas=5,
                )
                resultado.update(parte)
        finally:
            fechar_sessao()

    return resultado


def _selecionar_data_performance_intervalo(page, seletor: str, valor: date) -> None:
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
    dias = page.get_by_role("link", name=str(valor.day), exact=True)
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


def extrair_relatorio_performance_intervalo_fraction(
    data_inicial: date,
    data_final: date,
    token: str,
    *,
    pasta_destino: str | Path | None = None,
    log=None,
) -> Path:
    """Baixa a Performance por período para reutilização em outras automações.

    Mantém disponível no serviço Fraction o fluxo antigo de extração por datas,
    embora a fila de romaneios de Emissão passe a usar a consulta por CTE.
    """
    if data_final < data_inicial:
        raise ValueError("A data final precisa ser igual ou posterior à inicial.")
    if (data_final - data_inicial).days + 1 > 31:
        raise ValueError("O período da Performance pode ter no máximo 31 dias.")
    if not token.strip():
        raise ValueError("Informe o Token MFA para consultar o Fraction.")
    if not URL_FRACTION or not FRACTION_USER_PERFORMANCE or not FRACTION_PASSWORD_PERFORMANCE:
        raise RuntimeError(
            "Configure URL_FRACTION, FRACTION_USER_PERFORMANCE e "
            "FRACTION_PASSWORD_PERFORMANCE no .env."
        )

    pasta = Path(pasta_destino) if pasta_destino is not None else RESULTADOS_PERFORMANCE
    pasta.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
    caminho_zip = pasta / (
        f"Performance_{data_inicial:%Y-%m-%d}_a_{data_final:%Y-%m-%d}_"
        f"{timestamp}.zip"
    )
    _log(
        log,
        f"Extração reutilizável da Performance: {data_inicial:%d/%m/%Y} a "
        f"{data_final:%d/%m/%Y}.",
    )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=HEADLESS, slow_mo=SLOW_MO_MS)
        contexto = browser.new_context(accept_downloads=True)
        page = contexto.new_page()
        try:
            page.goto(URL_FRACTION, wait_until="domcontentloaded", timeout=120_000)
            page.get_by_role("textbox", name="Usuário").fill(FRACTION_USER_PERFORMANCE)
            page.get_by_role("textbox", name="Senha").fill(FRACTION_PASSWORD_PERFORMANCE)
            page.get_by_role("button", name="Login").click()
            campo_token = page.get_by_role("textbox", name="Digite o código MFA:")
            campo_token.wait_for(state="visible", timeout=60_000)
            campo_token.fill(token.strip())
            page.get_by_role("button", name="Validar").click()
            page.get_by_role("link", name="Gerencial").wait_for(
                state="visible", timeout=120_000
            )
            _log(log, "Login da extração de Performance concluído com Token MFA.")

            page.get_by_role("link", name="Gerencial").click()
            page.get_by_role("link", name="Performance", exact=True).click()
            page.locator(SELETOR_DATA_INICIAL_PERFORMANCE).wait_for(
                state="visible", timeout=120_000
            )
            page.locator(SELETOR_DATA_FINAL_PERFORMANCE).wait_for(
                state="visible", timeout=120_000
            )
            _selecionar_data_performance_intervalo(
                page, SELETOR_DATA_INICIAL_PERFORMANCE, data_inicial
            )
            _selecionar_data_performance_intervalo(
                page, SELETOR_DATA_FINAL_PERFORMANCE, data_final
            )
            page.get_by_role("button", name="Processar").click(timeout=120_000)

            total = page.get_by_role(
                "gridcell", name="TOTAL DO PERIODO", exact=True
            )
            total.wait_for(state="visible", timeout=240_000)
            linha_total = total.locator("xpath=ancestor::tr[1]")
            celulas = linha_total.get_by_role("gridcell")
            if celulas.count() < 2 or not celulas.nth(1).inner_text().strip():
                raise RuntimeError(
                    "A linha TOTAL DO PERIODO não retornou a quantidade de pedidos."
                )
            _log(
                log,
                "Aguardando o ZIP da Performance; quantidade do período: "
                f"{celulas.nth(1).inner_text().strip()}.",
            )
            try:
                with page.expect_download(
                    timeout=TIMEOUT_DOWNLOAD_PERFORMANCE_INTERVALO_MS
                ) as informacao_download:
                    celulas.nth(1).click()
                download = informacao_download.value
            except PlaywrightTimeoutError as erro:
                raise TimeoutError(
                    "O Fraction apresentou o total do período, mas não iniciou "
                    "o download ZIP da Performance."
                ) from erro
            download.save_as(caminho_zip)
            _log(log, f"ZIP da Performance salvo em {caminho_zip}.")
            return caminho_zip
        finally:
            try:
                contexto.close()
            finally:
                browser.close()


def _capturar_resposta_pdf(response, arquivos_pdf: list[tuple[str, bytes]], *, log=None) -> None:
    cabecalhos = response.headers
    tipo = cabecalhos.get("content-type", "").lower()
    disposicao = cabecalhos.get("content-disposition", "").lower()
    url = response.url
    if "pdf" not in tipo and ".pdf" not in url.lower() and ".pdf" not in disposicao:
        return
    try:
        conteudo = response.body()
    except Exception as erro:
        _log(log, f"Não foi possível ler a resposta PDF de {url}: {erro}")
        return
    if conteudo.startswith(b"%PDF"):
        arquivos_pdf.append((url, conteudo))
        _log(log, f"PDF capturado do Fraction ({len(conteudo)} bytes).")


def _caminho_pdf_romaneio(
    unidade: str,
    *,
    palete: str | None = None,
    rua: str | None = None,
    triagem: str | None = None,
    pasta_destino: str | Path | None = None,
) -> Path:
    pasta = Path(pasta_destino) if pasta_destino is not None else RESULTADOS_ROMANEIOS
    pasta.mkdir(parents=True, exist_ok=True)

    def nome_seguro(valor: str) -> str:
        return re.sub(r"[^A-Za-z0-9._-]+", "_", valor).strip("_.")

    nome_unidade = (nome_seguro(unidade) or "unidade")[:48]
    componentes = ["Romaneio", nome_unidade]
    if rua:
        componentes.append((nome_seguro(rua) or "rua")[:24])
    if triagem:
        componentes.append((nome_seguro(triagem) or "triagem")[:48])
    if palete:
        identificacao_palete = (
            nome_seguro(palete) or "identificacao_indisponivel"
        )[:88]
        componentes.extend(["Palete", identificacao_palete])
    componentes.append(datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f"))
    return pasta / ("_".join(componentes) + ".pdf")


def _salvar_conteudo_pdf_romaneio(
    conteudo: bytes,
    unidade: str,
    *,
    palete: str | None = None,
    rua: str | None = None,
    triagem: str | None = None,
    destino_exibicao: str | None = None,
    pasta_destino: str | Path | None = None,
    log=None,
) -> Path:
    if not conteudo.startswith(b"%PDF"):
        raise RuntimeError(
            f"O arquivo recebido do Fraction para a unidade '{unidade}' "
            "não é um PDF válido."
        )

    caminho = _caminho_pdf_romaneio(
        unidade,
        palete=palete,
        rua=rua,
        triagem=triagem,
        pasta_destino=pasta_destino,
    )
    caminho.write_bytes(conteudo)
    try:
        identificacao = _aplicar_identificacao_romaneio_pdf(
            caminho,
            rua=rua,
            triagem=triagem,
            destino_exibicao=destino_exibicao or unidade,
        )
    except Exception:
        caminho.unlink(missing_ok=True)
        raise
    _log(log, f"Romaneio salvo em {caminho} ({len(conteudo)} bytes).")
    _log(log, f"Identificação aplicada ao PDF: {identificacao}.")
    return caminho


def _aplicar_identificacao_romaneio_pdf(
    caminho: Path,
    *,
    rua: str | None,
    triagem: str | None,
    destino_exibicao: str,
) -> str:
    """Sobrepõe Rua / triagem / código interno no cabeçalho de cada página."""
    from io import BytesIO

    from pypdf import PdfReader, PdfWriter
    from reportlab.lib.colors import white
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.pdfgen import canvas

    conteudo = caminho.read_bytes()
    leitor = PdfReader(BytesIO(conteudo), strict=True)
    if not leitor.pages:
        raise ValueError(f"O PDF '{caminho.name}' não contém páginas.")

    codigo_interno = ""
    for pagina in leitor.pages:
        texto = pagina.extract_text() or ""
        encontrado = re.search(r"(?<![A-Z0-9])[A-Z]{1,3}-\d{2,3}(?!\d)", texto)
        if encontrado:
            codigo_interno = encontrado.group(0)
            break

    destino = codigo_interno or destino_exibicao.strip()
    partes = [parte.strip() for parte in (rua, triagem, destino) if parte and parte.strip()]
    if not partes:
        raise ValueError("Não há Rua, triagem ou unidade para identificar o PDF.")
    identificacao = " / ".join(partes)
    identificacao = unicodedata.normalize("NFKD", identificacao)
    identificacao = "".join(
        caractere for caractere in identificacao
        if not unicodedata.combining(caractere)
    )

    escritor = PdfWriter()
    for pagina in leitor.pages:
        largura = float(pagina.mediabox.width)
        altura = float(pagina.mediabox.height)
        camada_bytes = BytesIO()
        camada = canvas.Canvas(camada_bytes, pagesize=(largura, altura))
        largura_faixa = min(185.0, largura - 32.0)
        esquerda = (largura - largura_faixa) / 2
        base = max(0.0, altura - 61.0)
        camada.setFillColor(white)
        camada.rect(esquerda, base, largura_faixa, 25.0, fill=1, stroke=0)

        tamanho_fonte = 14.0
        while (
            tamanho_fonte > 6.0
            and stringWidth(identificacao, "Helvetica-Bold", tamanho_fonte)
            > largura_faixa - 12.0
        ):
            tamanho_fonte -= 0.25
        camada.setFillColorRGB(0, 0, 0)
        camada.setFont("Helvetica-Bold", tamanho_fonte)
        camada.drawCentredString(
            largura / 2,
            base + 3.0,
            identificacao,
        )
        camada.save()
        camada_bytes.seek(0)
        pagina.merge_page(PdfReader(camada_bytes, strict=True).pages[0])
        escritor.add_page(pagina)

    saida = BytesIO()
    escritor.write(saida)
    caminho.write_bytes(saida.getvalue())
    return identificacao


def preparar_lote_impressao_romaneios(
    arquivos_pdf: list[str | Path],
    *,
    pasta_destino: str | Path | None = None,
    log=None,
) -> Path:
    """Valida todos os PDFs e cria um arquivo único para a impressão em lote."""
    from pypdf import PdfReader, PdfWriter

    caminhos = [Path(caminho) for caminho in arquivos_pdf]
    if not caminhos:
        raise ValueError("Não há PDFs de romaneio para imprimir.")

    pasta_lote = Path(pasta_destino) if pasta_destino else RESULTADOS_ROMANEIOS
    pasta_lote.mkdir(parents=True, exist_ok=True)
    caminho_lote = pasta_lote / (
        f"Romaneios_Emissao_impressao_{datetime.now():%Y-%m-%d_%H-%M-%S-%f}.pdf"
    )
    escritor = PdfWriter()
    arquivos_abertos = []
    total_paginas = 0

    try:
        for caminho in caminhos:
            if not caminho.is_file():
                raise FileNotFoundError(f"PDF de romaneio não encontrado: {caminho}.")
            arquivo = caminho.open("rb")
            arquivos_abertos.append(arquivo)
            try:
                leitor = PdfReader(arquivo, strict=True)
                paginas = len(leitor.pages)
            except Exception as erro:
                raise ValueError(
                    f"O PDF '{caminho.name}' não pôde ser validado: {erro}"
                ) from erro
            if paginas <= 0:
                raise ValueError(f"O PDF '{caminho.name}' não possui páginas.")
            escritor.append(leitor)
            total_paginas += paginas

        with caminho_lote.open("wb") as destino:
            escritor.write(destino)

        with caminho_lote.open("rb") as arquivo_lote:
            leitor_lote = PdfReader(arquivo_lote, strict=True)
            paginas_lote = len(leitor_lote.pages)
        if paginas_lote != total_paginas:
            raise RuntimeError(
                "A validação do PDF combinado encontrou uma quantidade de páginas "
                f"inesperada ({paginas_lote} de {total_paginas})."
            )
    except Exception:
        caminho_lote.unlink(missing_ok=True)
        raise
    finally:
        escritor.close()
        for arquivo in arquivos_abertos:
            arquivo.close()

    _log(
        log,
        f"Lote de impressão preparado: {len(caminhos)} PDF(s), "
        f"{total_paginas} página(s), arquivo {caminho_lote}.",
    )
    return caminho_lote


def _localizar_sumatra_pdf() -> Path | None:
    candidatos: list[Path] = []
    encontrado_no_path = shutil.which("SumatraPDF.exe") or shutil.which("SumatraPDF")
    if encontrado_no_path:
        candidatos.append(Path(encontrado_no_path))

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidatos.append(Path(local_app_data) / "SumatraPDF" / "SumatraPDF.exe")

    for variavel in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        pasta_programas = os.environ.get(variavel)
        if pasta_programas:
            candidatos.append(Path(pasta_programas) / "SumatraPDF" / "SumatraPDF.exe")

    if os.name == "nt":
        try:
            import winreg

            locais = (
                (
                    winreg.HKEY_CURRENT_USER,
                    r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\SumatraPDF.exe",
                ),
                (
                    winreg.HKEY_LOCAL_MACHINE,
                    r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\SumatraPDF.exe",
                ),
            )
            for raiz, subchave in locais:
                try:
                    with winreg.OpenKey(raiz, subchave) as chave:
                        caminho, _ = winreg.QueryValueEx(chave, "")
                    candidatos.append(Path(str(caminho).strip('"')))
                except OSError:
                    continue
        except ImportError:
            pass

    vistos: set[str] = set()
    for candidato in candidatos:
        chave = os.path.normcase(str(candidato))
        if chave in vistos:
            continue
        vistos.add(chave)
        if candidato.is_file() and candidato.name.lower() == "sumatrapdf.exe":
            return candidato
    return None


def enviar_pdf_para_impressora_padrao(
    caminho_pdf: str | Path,
    *,
    log=None,
) -> None:
    """Envia o PDF ao SumatraPDF para impressão e aguarda o resultado do comando."""
    arquivo = Path(caminho_pdf)
    if not arquivo.is_file():
        raise FileNotFoundError(f"PDF do lote de impressão não encontrado: {arquivo}.")
    if os.name != "nt":
        raise RuntimeError(
            "O envio direto para a impressora padrão está disponível somente no Windows."
        )

    with arquivo.open("rb") as entrada:
        if entrada.read(4) != b"%PDF":
            raise ValueError(f"O arquivo '{arquivo.name}' não é um PDF válido.")

    sumatra = _localizar_sumatra_pdf()
    if sumatra is None:
        raise RuntimeError(
            "Não encontrei o SumatraPDF.exe. Instale o SumatraPDF ou inclua-o no PATH; "
            "o PDF não foi enviado à impressora."
        )

    try:
        resultado = subprocess.run(
            [str(sumatra), "-print-to-default", "-silent", str(arquivo.resolve())],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except subprocess.TimeoutExpired as erro:
        raise RuntimeError(
            "O SumatraPDF não confirmou o fim do envio em 180 segundos. "
            "Confira a fila do Windows antes de tentar novamente para evitar uma "
            "impressão duplicada."
        ) from erro
    except OSError as erro:
        raise RuntimeError(
            f"Não foi possível iniciar o SumatraPDF em '{sumatra}': {erro}."
        ) from erro

    if resultado.returncode != 0:
        detalhe = (resultado.stderr or resultado.stdout or "").strip()
        sufixo = f" Detalhe: {detalhe[:1000]}" if detalhe else ""
        raise RuntimeError(
            "O SumatraPDF retornou erro ao enviar o lote à impressora "
            f"(código {resultado.returncode}). Verifique o leitor e a impressora padrão."
            f"{sufixo}"
        )

    _log(
        log,
        f"Lote {arquivo.name} enviado pelo SumatraPDF à impressora padrão "
        f"(código de saída {resultado.returncode}).",
    )


def _salvar_download_romaneio(
    download,
    unidade: str,
    *,
    palete: str | None = None,
    rua: str | None = None,
    triagem: str | None = None,
    destino_exibicao: str | None = None,
    pasta_destino: str | Path | None = None,
    log=None,
) -> Path:
    caminho = _caminho_pdf_romaneio(
        unidade,
        palete=palete,
        rua=rua,
        triagem=triagem,
        pasta_destino=pasta_destino,
    )

    try:
        download.save_as(caminho)
        with caminho.open("rb") as arquivo:
            assinatura = arquivo.read(4)
        if assinatura != b"%PDF":
            raise RuntimeError(
                "O Fraction iniciou um download, mas o arquivo recebido "
                f"não é um PDF. Nome informado pelo navegador: "
                f"{download.suggested_filename!r}."
            )
        identificacao = _aplicar_identificacao_romaneio_pdf(
            caminho,
            rua=rua,
            triagem=triagem,
            destino_exibicao=destino_exibicao or unidade,
        )
    except Exception:
        caminho.unlink(missing_ok=True)
        raise

    _log(
        log,
        f"Download do romaneio ({download.suggested_filename}) salvo em {caminho}; "
        f"identificação aplicada: {identificacao}.",
    )
    return caminho


def _salvar_pdf_romaneio(
    popup,
    arquivos_pdf: list[tuple[str, bytes]],
    unidade: str,
    *,
    palete: str | None = None,
    rua: str | None = None,
    triagem: str | None = None,
    destino_exibicao: str | None = None,
    pasta_destino: str | Path | None = None,
    log=None,
) -> Path:
    conteudo = arquivos_pdf[-1][1] if arquivos_pdf else b""
    if not conteudo and popup.url.startswith("data:application/pdf"):
        cabecalho, _, payload = popup.url.partition(",")
        conteudo = (
            base64.b64decode(payload)
            if ";base64" in cabecalho.lower()
            else unquote_to_bytes(payload)
        )
    if not conteudo and popup.url.startswith("blob:"):
        payload = popup.evaluate(
            """async () => {
                const resposta = await fetch(location.href);
                const bytes = new Uint8Array(await resposta.arrayBuffer());
                let binario = "";
                for (let inicio = 0; inicio < bytes.length; inicio += 0x8000) {
                    binario += String.fromCharCode(...bytes.subarray(inicio, inicio + 0x8000));
                }
                return btoa(binario);
            }"""
        )
        conteudo = base64.b64decode(payload)
    if not conteudo.startswith(b"%PDF"):
        raise RuntimeError(
            "O Fraction abriu uma nova guia, mas não foi possível capturar o PDF "
            f"do romaneio da unidade '{unidade}'. URL da guia: {popup.url}"
        )
    return _salvar_conteudo_pdf_romaneio(
        conteudo,
        unidade,
        palete=palete,
        rua=rua,
        triagem=triagem,
        destino_exibicao=destino_exibicao,
        pasta_destino=pasta_destino,
        log=log,
    )


def associar_destinos_fraction(
    grupos: list[dict],
    destinos: list[dict[str, str]],
    *,
    codigos_preferenciais: dict[str, str] | None = None,
) -> tuple[list[dict], list[str]]:
    """Liga cada rota a uma unidade Fraction e resolve nomes duplicados por código."""
    por_nome: dict[str, list[dict[str, str]]] = {}
    for destino in destinos:
        por_nome.setdefault(
            _normalizar_unidade_fraction(destino["nome"]), []
        ).append(destino)
    preferencias = {
        _normalizar_unidade_fraction(nome): str(codigo).strip()
        for nome, codigo in (codigos_preferenciais or {}).items()
    }
    associados = []
    pendentes = []
    for grupo in grupos:
        chave_unidade = _normalizar_unidade_fraction(grupo["ROTA"])
        correspondencias = por_nome.get(chave_unidade, [])
        contexto = f"unidade '{grupo['ROTA']}'"
        codigo_preferencial = preferencias.get(chave_unidade)
        if codigo_preferencial:
            correspondencias_preferenciais = [
                destino
                for destino in correspondencias
                if str(destino["codigo"]).strip() == codigo_preferencial
            ]
            if len(correspondencias_preferenciais) != 1:
                detalhe = (
                    "não encontrado"
                    if not correspondencias_preferenciais
                    else "duplicado"
                )
                pendentes.append(
                    f"{contexto}: código preferencial {codigo_preferencial} "
                    f"{detalhe} no catálogo"
                )
                continue
            correspondencias = correspondencias_preferenciais
        elif len(correspondencias) != 1:
            detalhe = "não encontrada" if not correspondencias else "ambígua"
            pendentes.append(f"{contexto} {detalhe} no catálogo")
            continue
        destino = correspondencias[0]
        item = dict(grupo)
        item["NOME_DESTINO"] = destino["nome"]
        item["CODIGO_DESTINO"] = destino["codigo"]
        associados.append(item)
    return associados, pendentes


def adicionar_rotas_a_previa(tabela_base: pd.DataFrame, *, log=None) -> pd.DataFrame:
    """Resolve a unidade por destino específico, cidade e depois UF."""
    tabela = tabela_base.copy()
    coluna_destino = next(
        (coluna for coluna in tabela if _normalizar_rota(coluna) == "DESTINO"),
        None,
    )
    if coluna_destino is None:
        raise ValueError("A base de entrada não contém a coluna 'Destino'.")

    coluna_uf = next(
        (
            coluna
            for coluna in tabela
            if _normalizar_rota(coluna) in {"UF", "ESTADO"}
        ),
        None,
    )
    coluna_cidade = next(
        (
            coluna
            for coluna in tabela
            if _normalizar_rota(coluna) in {"CIDADE", "MUNICIPIO"}
        ),
        None,
    )

    try:
        regras_rotas = carregar_regras_triagem_rotas()
        codigos_preferenciais = carregar_codigos_preferenciais_unidades()
        unidades = carregar_destinos_fraction()
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as erro:
        raise ValueError(f"Não foi possível carregar a base de rotas: {erro}") from erro

    def resolver(linha: pd.Series) -> str:
        uf = _normalizar_uf(linha[coluna_uf]) if coluna_uf is not None else ""
        cidade_valor = linha[coluna_cidade] if coluna_cidade is not None else ""
        cidade = (
            _normalizar_rota(str(cidade_valor))
            if pd.notna(cidade_valor)
            else ""
        )
        destino_valor = linha[coluna_destino]
        destino = (
            _normalizar_rota(str(destino_valor))
            if pd.notna(destino_valor)
            else ""
        )
        rota, _ = resolver_rota_por_localidade(
            regras_rotas,
            uf=uf,
            cidade=cidade,
            destino=destino,
        )
        return rota

    tabela["Rota"] = tabela.apply(resolver, axis=1)
    unidades_por_nome: dict[str, list[dict[str, str]]] = {}
    for unidade in unidades:
        unidades_por_nome.setdefault(
            _normalizar_unidade_fraction(unidade["nome"]), []
        ).append(unidade)
    def resolver_codigo_rota(rota: object) -> str:
        chave = _normalizar_unidade_fraction(rota)
        correspondencias = unidades_por_nome.get(chave, [])
        codigo_preferencial = codigos_preferenciais.get(chave)
        if codigo_preferencial:
            correspondencias = [
                unidade
                for unidade in correspondencias
                if str(unidade["codigo"]).strip() == codigo_preferencial
            ]
        return (
            str(correspondencias[0]["codigo"])
            if len(correspondencias) == 1
            else "Sem código"
        )

    tabela["Código da rota"] = tabela["Rota"].map(resolver_codigo_rota)
    encontradas = int((~tabela["Rota"].isin(["Sem rota", "Rota ambígua"])).sum())
    sem_rota = int(tabela["Rota"].eq("Sem rota").sum())
    _log(
        log,
        f"Prévia de rotas por UF/cidade/destino: {encontradas}/{len(tabela)} "
        f"pedido(s) com unidade atribuída; {sem_rota} sem regra.",
    )
    return tabela


def mapear_rotas_para_grupos(
    tabela_base: pd.DataFrame,
    *,
    separar_por_palete: bool = False,
    separar_por_rua_triagem: bool = False,
    separar_por_triagem: bool = False,
    agrupar_triagens_por_rota: Iterable[str] = (),
) -> tuple[list[dict], list[str]]:
    coluna_cte = next(
        (coluna for coluna in tabela_base if _normalizar_rota(coluna) == "CTE"),
        None,
    )
    if coluna_cte is None or "Rota" not in tabela_base:
        raise ValueError("A base precisa conter as colunas CTE, Destino e Rota.")
    colunas_ctes = {
        _normalizar_rota(coluna): coluna
        for coluna in tabela_base
    }
    coluna_ctes_entrada = colunas_ctes.get("CTES PARA ROMANEIO")
    if (
        coluna_ctes_entrada is None
        or not tabela_base[coluna_ctes_entrada]
        .fillna("")
        .astype(str)
        .str.strip()
        .ne("")
        .any()
    ):
        coluna_ctes_entrada = colunas_ctes.get("CTES PARA DAR ENTRADA")
    coluna_palete = next(
        (
            coluna
            for coluna in tabela_base
            if _normalizar_rota(coluna) == "PALETE"
        ),
        None,
    )
    coluna_rua = next(
        (coluna for coluna in tabela_base if _normalizar_rota(coluna) == "RUA"),
        None,
    )
    coluna_triagem = next(
        (
            coluna
            for coluna in tabela_base
            if _normalizar_rota(coluna) == "TRIAGEM"
        ),
        None,
    )
    grupos_por_chave: dict[tuple[str, ...], list[str]] = {}
    ignorados: list[str] = []
    for _, linha in tabela_base.iterrows():
        rota = str(linha["Rota"]).strip()
        palete = (
            str(linha[coluna_palete]).strip()
            if coluna_palete is not None and pd.notna(linha[coluna_palete])
            else "Sem palete"
        )
        valor_ctes = (
            linha[coluna_ctes_entrada]
            if coluna_ctes_entrada is not None
            else linha[coluna_cte]
        )
        texto_ctes = "" if pd.isna(valor_ctes) else str(valor_ctes)
        ctes_linha = [
            re.sub(r"\.0$", "", cte.strip())
            for cte in texto_ctes.splitlines()
            if cte.strip() and cte.strip().lower() != "nan"
        ]
        if not ctes_linha:
            continue
        rua = (
            str(linha[coluna_rua]).strip()
            if coluna_rua is not None and pd.notna(linha[coluna_rua])
            else ""
        )
        triagem = (
            " ".join(str(linha[coluna_triagem]).split())
            if coluna_triagem is not None and pd.notna(linha[coluna_triagem])
            else ""
        )
        if separar_por_rua_triagem and (not rua or not triagem):
            ausentes = []
            if not rua:
                ausentes.append("Rua")
            if not triagem:
                ausentes.append("triagem")
            ignorados.append(
                f"CTE(s) {', '.join(ctes_linha)}: sem {' e '.join(ausentes)}; "
                "não é possível identificar o palete."
            )
            continue
        if rota in {"Sem rota", "Rota ambígua", ""}:
            if separar_por_rua_triagem:
                identificacao = f" em {rua} / {triagem}"
            else:
                identificacao = f" no palete '{palete}'" if separar_por_palete else ""
            ignorados.append(
                f"CTE(s) {', '.join(ctes_linha)}{identificacao}: {rota or 'sem rota'}"
            )
            continue
        if separar_por_rua_triagem:
            chave = (rua, triagem, rota)
        elif separar_por_triagem:
            if not triagem:
                ignorados.append(
                    f"CTE(s) {', '.join(ctes_linha)}: sem triagem; "
                    "não é possível montar a fila de Entrada."
                )
                continue
            chave = (triagem, rota)
        elif separar_por_palete:
            chave = (palete, rota)
        else:
            chave = ("", rota)
        grupos_por_chave.setdefault(chave, [])
        for cte in ctes_linha:
            if cte not in grupos_por_chave[chave]:
                grupos_por_chave[chave].append(cte)
    grupos = []
    for chave, ctes in sorted(grupos_por_chave.items()):
        if separar_por_rua_triagem:
            rua, triagem, rota = chave
            grupo = {
                "RUA": rua,
                "TRIAGEM": triagem,
                "ROTA": rota,
                "LISTA_CTES": ctes,
            }
        elif separar_por_triagem:
            triagem, rota = chave
            grupo = {
                "TRIAGEM": triagem,
                "ROTA": rota,
                "LISTA_CTES": ctes,
            }
        elif separar_por_palete:
            palete, rota = chave
            grupo = {"ROTA": rota, "LISTA_CTES": ctes, "PALETE": palete}
        else:
            _, rota = chave
            grupo = {"ROTA": rota, "LISTA_CTES": ctes}
        grupos.append(grupo)
    rotas_agregadas = {
        _normalizar_rota(rota)
        for rota in agrupar_triagens_por_rota
        if str(rota).strip()
    }
    if not separar_por_triagem or not rotas_agregadas:
        return grupos, ignorados

    grupos_finais: list[dict] = []
    grupos_por_rota: dict[str, dict] = {}
    for grupo in grupos:
        rota = str(grupo.get("ROTA", "")).strip()
        chave_rota = _normalizar_rota(rota)
        if chave_rota not in rotas_agregadas:
            grupos_finais.append(grupo)
            continue

        grupo_agregado = grupos_por_rota.get(chave_rota)
        if grupo_agregado is None:
            grupo_agregado = {
                "ROTA": rota,
                "TRIAGENS": [],
                "LISTA_CTES": [],
            }
            grupos_por_rota[chave_rota] = grupo_agregado
            grupos_finais.append(grupo_agregado)
        triagem = str(grupo.get("TRIAGEM", "")).strip()
        if triagem and triagem not in grupo_agregado["TRIAGENS"]:
            grupo_agregado["TRIAGENS"].append(triagem)
        for cte in grupo["LISTA_CTES"]:
            if cte not in grupo_agregado["LISTA_CTES"]:
                grupo_agregado["LISTA_CTES"].append(cte)

    for grupo in grupos_por_rota.values():
        triagens = grupo["TRIAGENS"]
        grupo["TRIAGEM"] = ", ".join(triagens)

    return grupos_finais, ignorados


def processar_manifestos(
    page,
    grupos: list[dict],
    *,
    pasta_destino_pdf: str | Path | None = None,
    ao_concluir_grupo: Callable[[dict, Path], None] | None = None,
    log=None,
) -> dict[str, object]:
    destinos, pendentes = associar_destinos_fraction(
        grupos,
        carregar_destinos_fraction(),
        codigos_preferenciais=carregar_codigos_preferenciais_unidades(),
    )
    if pendentes:
        raise ValueError(
            "Manifestos não iniciados; revise os destinos no catálogo: "
            + "; ".join(pendentes)
        )
    def abrir_envio_remessa() -> None:
        page.get_by_role("link", name="Operacional").click()
        page.get_by_role("link", name="Manifesto", exact=True).click()
        page.get_by_role("link", name="Envio de remessa").click()

    _log(log, "Acessando Manifesto no Fraction...")
    abrir_envio_remessa()

    total_lotes = 0
    arquivos_romaneios: list[str] = []
    pasta_pdf = (
        Path(pasta_destino_pdf)
        if pasta_destino_pdf is not None
        else RESULTADOS_ROMANEIOS
    )
    pasta_pdf.mkdir(parents=True, exist_ok=True)
    _log(log, f"Os PDFs desta execução serão salvos em {pasta_pdf}.")
    for indice_grupo, grupo in enumerate(destinos):
        if indice_grupo:
            _log(
                log,
                "Recarregando Envio de remessa para limpar a lista anterior "
                "antes de selecionar a próxima unidade.",
            )
            page.reload(wait_until="domcontentloaded", timeout=120_000)
            page.get_by_role("link", name="Operacional").wait_for(
                state="visible",
                timeout=120_000,
            )
            abrir_envio_remessa()
        rota_unidade = grupo["ROTA"]
        rota = grupo["NOME_DESTINO"]
        codigo_destino = grupo["CODIGO_DESTINO"]
        palete = str(grupo.get("PALETE") or "").strip() or None
        rua = str(grupo.get("RUA") or "").strip() or None
        triagem = str(grupo.get("TRIAGEM") or "").strip() or None
        ctes = grupo["LISTA_CTES"]
        contexto_palete = f", palete '{palete}'" if palete else ""
        contexto_rua_triagem = (
            f", {rua} / triagem '{triagem}'"
            if rua or triagem
            else ""
        )
        _log(
            log,
            f"Unidade {rota_unidade}: {len(ctes)} CTE(s), destino Fraction "
            f"'{rota}', código selecionado {codigo_destino}"
            f"{contexto_rua_triagem}{contexto_palete}.",
        )
        _log(log, "Abrindo seletor de unidade do Manifesto.")
        page.locator(ID_DESTINO_LABEL).click()
        filtro = page.locator(ID_DESTINO_FILTRO)
        filtro.fill(codigo_destino)
        _log(log, f"Filtro de unidade preenchido com o código {codigo_destino}.")
        page.locator(ID_DESTINO_BUSCAR).first.click()
        _log(log, "Busca de unidade acionada; aguardando opção correspondente.")
        page.get_by_role(
            "option", name=f"{rota} - {codigo_destino}", exact=True
        ).click()
        _log(log, f"Unidade selecionada: {rota} - {codigo_destino}.")
        _log(log, "Procurando a opção de rota que contém 'RODO'.")
        selecionar_rota_manifesto(page, log=log)

        for inicio in range(0, len(ctes), TAMANHO_LOTE_CTES):
            lote = ctes[inicio : inicio + TAMANHO_LOTE_CTES]
            numero_lote = inicio // TAMANHO_LOTE_CTES + 1
            total_lotes += 1
            page.locator(ID_CTES).fill("\n".join(lote))
            _log(
                log,
                f"Lote {numero_lote}: preenchidos {len(lote)} CTE(s) "
                f"para {rota_unidade} (unidade {codigo_destino}).",
            )
            page.get_by_role(
                "button",
                name="Adicionar",
            ).click()
            _log(log, f"Lote {numero_lote} adicionado ao Manifesto.")
            aguardar_carregamento_manifesto(page, log=log)

        _log(log, f"Finalizando a remessa da unidade {rota_unidade}.")
        arquivos_pdf: list[tuple[str, bytes]] = []
        capturar_pdf = lambda resposta: _capturar_resposta_pdf(
            resposta,
            arquivos_pdf,
            log=log,
        )
        contexto = page.context
        contexto.on("response", capturar_pdf)
        popup = None
        download_recebido = None
        saida_recebida = Event()

        def registrar_download(download) -> None:
            nonlocal download_recebido
            download_recebido = download
            saida_recebida.set()

        def registrar_popup(nova_pagina) -> None:
            nonlocal popup
            popup = nova_pagina
            nova_pagina.on("download", registrar_download)
            saida_recebida.set()

        page.on("download", registrar_download)
        page.on("popup", registrar_popup)
        try:
            page.get_by_role("button", name="Processar lista").click(
                timeout=120_000
            )

            prazo = time.monotonic() + 600
            while not saida_recebida.is_set():
                restante = prazo - time.monotonic()
                if restante <= 0:
                    raise PlaywrightTimeoutError(
                        "O Fraction não iniciou o download do PDF nem abriu "
                        f"uma nova guia para a unidade '{rota_unidade}'."
                    )
                page.wait_for_timeout(min(250, max(1, int(restante * 1000))))

            if download_recebido is not None:
                _log(log, "Download automático do PDF do romaneio detectado.")
                caminho_pdf = _salvar_download_romaneio(
                    download_recebido,
                    rota_unidade,
                    palete=palete,
                    rua=rua,
                    triagem=triagem,
                    destino_exibicao=rota,
                    pasta_destino=pasta_pdf,
                    log=log,
                )
            else:
                _log(log, "Nova guia do romaneio aberta pelo Fraction.")
                try:
                    popup.wait_for_load_state("load", timeout=120_000)
                except PlaywrightTimeoutError:
                    _log(log, "A guia do PDF não sinalizou load; usando a resposta capturada.")
                if download_recebido is not None:
                    _log(log, "Download automático do PDF detectado na nova guia.")
                    caminho_pdf = _salvar_download_romaneio(
                        download_recebido,
                        rota_unidade,
                        palete=palete,
                        rua=rua,
                        triagem=triagem,
                        destino_exibicao=rota,
                        pasta_destino=pasta_pdf,
                        log=log,
                    )
                else:
                    caminho_pdf = _salvar_pdf_romaneio(
                        popup,
                        arquivos_pdf,
                        rota_unidade,
                        palete=palete,
                        rua=rua,
                        triagem=triagem,
                        destino_exibicao=rota,
                        pasta_destino=pasta_pdf,
                        log=log,
                    )
            arquivos_romaneios.append(str(caminho_pdf))
            _log(
                log,
                f"Remessa da unidade {rota_unidade}{contexto_rua_triagem}"
                f"{contexto_palete} "
                f"processada e PDF salvo em {caminho_pdf}.",
            )
            if ao_concluir_grupo is not None:
                ao_concluir_grupo(grupo, caminho_pdf)
        finally:
            page.remove_listener("download", registrar_download)
            page.remove_listener("popup", registrar_popup)
            contexto.remove_listener("response", capturar_pdf)
            if popup is not None and not popup.is_closed():
                popup.close()

    _log(log, "Todas as unidades roteadas foram processadas no Fraction.")

    return {
        "lotes": total_lotes,
        "arquivos_pdf": arquivos_romaneios,
        "pasta_pdf": str(pasta_pdf),
    }


def executar_manifestos_romaneios(
    arquivo_base: str | Path,
    *,
    tabela_previa: pd.DataFrame | None = None,
    codigos_preferenciais: dict[str, str] | None = None,
    separar_por_palete: bool = False,
    separar_por_rua_triagem: bool = False,
    separar_por_triagem: bool = False,
    pasta_destino_pdf: str | Path | None = None,
    ao_concluir_grupo: Callable[[dict, Path], None] | None = None,
    log=None,
) -> dict:
    """Envia os grupos de uma base já conferida ao fluxo de Manifesto."""
    caminho = Path(arquivo_base)
    if tabela_previa is None and not caminho.is_file():
        raise FileNotFoundError(f"Base de romaneios não encontrada: '{caminho}'.")
    if not URL_FRACTION or not FRACTION_USER or not FRACTION_PASSWORD:
        raise ValueError("URL_FRACTION, FRACTION_USER e FRACTION_PASSWORD precisam estar configurados.")

    inicio_execucao = time.perf_counter()
    _log(log, f"Iniciando segunda etapa com a base revisada: {arquivo_base}.")
    if caminho.is_file():
        _log(log, f"Base selecionada: {caminho.stat().st_size} bytes.")
    _log(log, "Lendo CTEs e rotas da base salva após Dar entrada.")
    tabela = (
        tabela_previa.copy()
        if tabela_previa is not None
        else pd.read_excel(caminho, engine="openpyxl", dtype={"CTE": "string"})
    )
    if "Rota" not in tabela:
        tabela = adicionar_rotas_a_previa(tabela, log=log)
    grupos, rotas_ausentes = mapear_rotas_para_grupos(
        tabela,
        separar_por_palete=separar_por_palete,
        separar_por_rua_triagem=separar_por_rua_triagem,
        separar_por_triagem=separar_por_triagem,
    )
    if rotas_ausentes:
        _log(
            log,
            f"Ignorando {len(rotas_ausentes)} CTE(s) sem rota única: "
            + "; ".join(rotas_ausentes),
        )
    if codigos_preferenciais is None:
        codigos_preferenciais = carregar_codigos_preferenciais_unidades()
    grupos, destinos_pendentes = associar_destinos_fraction(
        grupos,
        carregar_destinos_fraction(),
        codigos_preferenciais=codigos_preferenciais,
    )
    if destinos_pendentes:
        _log(
            log,
            f"Ignorando {len(destinos_pendentes)} grupo(s) sem unidade/código "
            f"válido: {'; '.join(destinos_pendentes)}",
        )
    if not grupos:
        raise ValueError(
            "Nenhum grupo tem uma rota única e código de unidade configurados. "
            "Confira Destino, rotas.json e unidades.json."
        )

    total_ctes = sum(len(grupo["LISTA_CTES"]) for grupo in grupos)
    _log(
        log,
        f"Pré-validação concluída: {len(grupos)} grupo(s) de envio, "
        f"{total_ctes} CTE(s), "
        f"até {TAMANHO_LOTE_CTES} por lote.",
    )
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=HEADLESS, slow_mo=SLOW_MO_MS)
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()
            page.on("pageerror", lambda erro: _log(log, f"Erro JavaScript na página: {erro}"))
            try:
                _log(log, "Realizando novo login no Fraction para a etapa de Manifesto.")
                login_fraction(
                    page,
                    FRACTION_USER,
                    FRACTION_PASSWORD,
                    lambda mensagem: _log(log, mensagem),
                )
                _log(log, f"Login concluído; página atual: {page.url}.")
                processamento = processar_manifestos(
                    page,
                    grupos,
                    pasta_destino_pdf=pasta_destino_pdf,
                    ao_concluir_grupo=ao_concluir_grupo,
                    log=log,
                )
            finally:
                _log(log, "Encerrando navegador da etapa de Manifesto.")
                context.close()
                browser.close()
    except Exception:
        _log(log, "Falha na etapa de Manifesto:\n" + traceback.format_exc())
        raise

    resultado = {
        "arquivo_final": str(caminho),
        "grupos_manifesto": len(grupos),
        "ctes_manifesto": total_ctes,
        "lotes_manifesto": processamento["lotes"],
        "arquivos_pdf": processamento["arquivos_pdf"],
        "pasta_pdf": processamento["pasta_pdf"],
    }
    _log(
        log,
        f"Etapa de Manifesto concluída: {processamento['lotes']} lote(s), "
        f"{len(processamento['arquivos_pdf'])} PDF(s) salvo(s), {total_ctes} CTE(s), "
        f"{time.perf_counter() - inicio_execucao:.1f}s.",
    )
    return resultado
