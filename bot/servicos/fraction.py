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
from typing import Callable
from urllib.parse import unquote_to_bytes

import pandas as pd

from playwright.sync_api import (
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from utils.config import (
    ARQUIVO_ROTAS_ROMANEIOS,
    ARQUIVO_UNIDADES_FRACTION,
    FRACTION_PASSWORD,
    FRACTION_USER,
    HEADLESS,
    RESULTADOS_ROMANEIOS,
    SLOW_MO_MS,
    URL_FRACTION,
)


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


def carregar_regras_rotas_localidade(
    caminho: Path = ARQUIVO_ROTAS_ROMANEIOS,
) -> tuple[dict[str, str], dict[str, str]]:
    """Carrega as unidades atendentes por UF e por cidade normalizada."""
    if not caminho.exists():
        raise FileNotFoundError(f"Base de rotas não encontrada: '{caminho}'.")
    with caminho.open("r", encoding="utf-8-sig") as arquivo:
        dados = json.load(arquivo)
    if not isinstance(dados, dict):
        raise ValueError("A base de rotas precisa ser um objeto JSON.")
    if "por_uf" not in dados or "por_cidade" not in dados:
        raise ValueError(
            "A base de rotas precisa conter as listas 'por_uf' e 'por_cidade'."
        )

    regras_uf = dados["por_uf"]
    regras_cidade = dados["por_cidade"]
    if not isinstance(regras_uf, list) or not isinstance(regras_cidade, list):
        raise ValueError("'por_uf' e 'por_cidade' precisam ser listas.")

    rotas_por_uf: dict[str, str] = {}
    rotas_por_cidade: dict[str, str] = {}

    for regra in regras_uf:
        if not isinstance(regra, dict):
            raise ValueError("Cada item de 'por_uf' precisa ser um objeto JSON.")
        uf = _normalizar_uf(regra.get("uf") or "")
        unidade = str(regra.get("unidade") or "").strip()
        if not uf or not unidade:
            raise ValueError("Cada regra de 'por_uf' precisa de 'uf' e 'unidade'.")
        existente = rotas_por_uf.get(uf)
        if existente and _normalizar_rota(existente) != _normalizar_rota(unidade):
            raise ValueError(f"Mais de uma unidade foi definida para a UF {uf}.")
        rotas_por_uf[uf] = unidade

    for regra in regras_cidade:
        if not isinstance(regra, dict):
            raise ValueError("Cada item de 'por_cidade' precisa ser um objeto JSON.")
        cidade = _normalizar_rota(regra.get("cidade") or "")
        unidade = str(regra.get("unidade") or "").strip()
        if not cidade or not unidade:
            raise ValueError(
                "Cada regra de 'por_cidade' precisa de 'cidade' e 'unidade'."
            )
        existente = rotas_por_cidade.get(cidade)
        if existente and _normalizar_rota(existente) != _normalizar_rota(unidade):
            raise ValueError(f"Mais de uma unidade foi definida para a cidade {cidade}.")
        rotas_por_cidade[cidade] = unidade

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
    pasta_destino: str | Path | None = None,
) -> Path:
    pasta = Path(pasta_destino) if pasta_destino is not None else RESULTADOS_ROMANEIOS
    pasta.mkdir(parents=True, exist_ok=True)

    def nome_seguro(valor: str) -> str:
        return re.sub(r"[^A-Za-z0-9._-]+", "_", valor).strip("_.")

    nome_unidade = (nome_seguro(unidade) or "unidade")[:48]
    componentes = ["Romaneio", nome_unidade]
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
        pasta_destino=pasta_destino,
    )
    caminho.write_bytes(conteudo)
    _log(log, f"Romaneio salvo em {caminho} ({len(conteudo)} bytes).")
    return caminho


def preparar_lote_impressao_romaneios(
    arquivos_pdf: list[str | Path],
    *,
    pasta_temporaria: str | Path | None = None,
    log=None,
) -> Path:
    """Valida todos os PDFs e cria um arquivo único para a impressão em lote."""
    from pypdf import PdfReader, PdfWriter

    caminhos = [Path(caminho) for caminho in arquivos_pdf]
    if not caminhos:
        raise ValueError("Não há PDFs de romaneio para imprimir.")

    pasta_lote = Path(pasta_temporaria) if pasta_temporaria else RESULTADOS_ROMANEIOS
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
    pasta_destino: str | Path | None = None,
    log=None,
) -> Path:
    caminho = _caminho_pdf_romaneio(
        unidade,
        palete=palete,
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
    except Exception:
        caminho.unlink(missing_ok=True)
        raise

    _log(
        log,
        f"Download do romaneio ({download.suggested_filename}) salvo em {caminho}.",
    )
    return caminho


def _salvar_pdf_romaneio(
    popup,
    arquivos_pdf: list[tuple[str, bytes]],
    unidade: str,
    *,
    palete: str | None = None,
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
    """Resolve a unidade atendente primeiro por cidade e depois por UF."""
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

    if coluna_uf is None and coluna_cidade is None:
        raise ValueError(
            "A base precisa conter a coluna 'UF' ou 'Cidade' para aplicar as rotas."
        )

    try:
        rotas_por_uf, rotas_por_cidade = carregar_regras_rotas_localidade()
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
        rota_cidade = rotas_por_cidade.get(cidade, "")
        if rota_cidade:
            return rota_cidade
        rota_uf = rotas_por_uf.get(uf, "")
        if rota_uf:
            return rota_uf

        return "Sem rota"

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
        f"Prévia de rotas por UF/cidade: {encontradas}/{len(tabela)} pedido(s) "
        f"com unidade atribuída; {sem_rota} sem regra de localidade.",
    )
    return tabela


def mapear_rotas_para_grupos(
    tabela_base: pd.DataFrame,
    *,
    separar_por_palete: bool = False,
) -> tuple[list[dict], list[str]]:
    coluna_cte = next(
        (coluna for coluna in tabela_base if _normalizar_rota(coluna) == "CTE"),
        None,
    )
    if coluna_cte is None or "Rota" not in tabela_base:
        raise ValueError("A base precisa conter as colunas CTE, Destino e Rota.")
    coluna_ctes_entrada = next(
        (
            coluna
            for coluna in tabela_base
            if _normalizar_rota(coluna) == "CTES PARA DAR ENTRADA"
        ),
        None,
    )
    coluna_palete = next(
        (
            coluna
            for coluna in tabela_base
            if _normalizar_rota(coluna) == "PALETE"
        ),
        None,
    )
    grupos_por_chave: dict[tuple[str, str], list[str]] = {}
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
        if rota in {"Sem rota", "Rota ambígua", ""}:
            identificacao = f" no palete '{palete}'" if separar_por_palete else ""
            ignorados.append(
                f"CTE(s) {', '.join(ctes_linha)}{identificacao}: {rota or 'sem rota'}"
            )
            continue
        chave = (palete, rota) if separar_por_palete else ("", rota)
        grupos_por_chave.setdefault(chave, [])
        for cte in ctes_linha:
            if cte not in grupos_por_chave[chave]:
                grupos_por_chave[chave].append(cte)
    grupos = []
    for (palete, rota), ctes in sorted(grupos_por_chave.items()):
        grupo = {"ROTA": rota, "LISTA_CTES": ctes}
        if separar_por_palete:
            grupo["PALETE"] = palete
        grupos.append(grupo)
    return grupos, ignorados


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
        ctes = grupo["LISTA_CTES"]
        contexto_palete = f", palete '{palete}'" if palete else ""
        _log(
            log,
            f"Unidade {rota_unidade}: {len(ctes)} CTE(s), destino Fraction "
            f"'{rota}', código selecionado {codigo_destino}{contexto_palete}.",
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
                        pasta_destino=pasta_pdf,
                        log=log,
                    )
                else:
                    caminho_pdf = _salvar_pdf_romaneio(
                        popup,
                        arquivos_pdf,
                        rota_unidade,
                        palete=palete,
                        pasta_destino=pasta_pdf,
                        log=log,
                    )
            arquivos_romaneios.append(str(caminho_pdf))
            _log(
                log,
                f"Remessa da unidade {rota_unidade}{contexto_palete} "
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
