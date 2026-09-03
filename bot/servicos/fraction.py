from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv
from playwright.sync_api import Page


PASTA_SERVICOS = Path(__file__).resolve().parent
PASTA_BOT = PASTA_SERVICOS.parent

load_dotenv(
    PASTA_BOT / ".env",
    override=False,
)

URL_FRACTION = os.getenv(
    "URL_FRACTION",
    "",
).strip()


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

