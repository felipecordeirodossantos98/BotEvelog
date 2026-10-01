from __future__ import annotations

import os
import re
import time
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
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

    arquivos.append(
        baixar_periodo_relatorio_descritivo_fraction(
            page,
            pasta_destino,
            dia=30,
            inicio_periodo=16,
            fim_periodo=30,
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

        # Sempre começamos pela segunda quinzena, pois a procura dos
        # CTEs também precisa respeitar a ordem mais recente -> antiga.
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
