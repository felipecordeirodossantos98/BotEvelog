from __future__ import annotations

import time
import unicodedata
from typing import Callable

from playwright.sync_api import (
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

from utils.config import URL_FRACTION


def validar_url_fraction() -> None:
    if not URL_FRACTION:
        raise RuntimeError(
            "URL_FRACTION não foi informada no arquivo .env."
        )


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

    page.get_by_role(
        "link",
        name="Consultas",
    ).wait_for(
        state="visible",
        timeout=120_000,
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

        if tentativa < tentativas:
            log(
                "Fechando a tentativa anterior e abrindo "
                "um Chromium novo para testar novamente."
            )
            time.sleep(intervalo_segundos)

    return False, ultimo_erro
