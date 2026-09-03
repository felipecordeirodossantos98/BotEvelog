from datetime import timedelta
from pathlib import Path

from playwright.sync_api import (
    TimeoutError,
    sync_playwright,
)

from ..utils.config import URL_FRACTION, FRACTION_USER, FRACTION_PASSWORD, ORIGINAIS_PATH, SLOW_MO_MS, HEADLESS


# ==========================================================
# CONFIGURAÇÃO
# ==========================================================

BASES = Path(ORIGINAIS_PATH)
BASES.mkdir(parents=True, exist_ok=True)


# ==========================================================
# LOGIN
# ==========================================================

def login(page, token):

    print("Realizando login...")

    page.goto(URL_FRACTION)

    page.get_by_role(
        "textbox",
        name="Usuário"
    ).fill(FRACTION_USER)

    page.get_by_role(
        "textbox",
        name="Senha"
    ).fill(FRACTION_PASSWORD)

    page.get_by_role(
        "button",
        name="Login"
    ).click()

    page.get_by_role(
        "textbox",
        name="Digite o código MFA:"
    ).wait_for(timeout=60000)

    page.get_by_role(
        "textbox",
        name="Digite o código MFA:"
    ).fill(token)

    page.get_by_role(
        "button",
        name="Validar"
    ).click()

    page.get_by_role(
        "link",
        name="Financeiro"
    ).wait_for(timeout=60000)

    print("Login concluído.")


# ==========================================================
# ABRIR RELATÓRIO
# ==========================================================

def abrir_relatorio(page):

    print("Abrindo relatório...")

    page.get_by_role(
        "link",
        name="Financeiro"
    ).click()

    page.get_by_role(
        "link",
        name="Faturamento"
    ).click()

    page.get_by_role(
        "link",
        name="Relatórios"
    ).click()

    page.get_by_text(
        "Analítico"
    ).click()

    page.wait_for_load_state("networkidle")

    page.locator(
        '[id="form_relatorios:id_data_ini_input"]'
    ).wait_for()

    print("Tela do relatório aberta.")


# ==========================================================
# PREENCHER DATA
# ==========================================================

def preencher_data(page, data):

    texto = data.strftime("%d/%m/%Y")

    print(f"Preenchendo {texto}")

    inicio = page.locator(
        '[id="form_relatorios:id_data_ini_input"]'
    )

    fim = page.locator(
        '[id="form_relatorios:id_data_fim_input"]'
    )

    inicio.click()
    inicio.clear()
    inicio.fill(texto)
    inicio.press("Tab")

    fim.click()
    fim.clear()
    fim.fill(texto)
    fim.press("Tab")

    page.wait_for_timeout(500)


# ==========================================================
# PROCESSAR
# ==========================================================

def processar(page):

    print("Processando relatório...")

    botao = page.get_by_role(
        "button",
        name="Processar"
    )

    botao.wait_for(timeout=60000)

    botao.click()

    page.wait_for_load_state("networkidle")

    exportar = page.get_by_role(
        "button",
        name=" Exportar para Excel"
    )

    # Mesmo timeout da versão original que foi validada pelo usuário:
    # 10 minutos como limite máximo para o sistema gerar o relatório.
    exportar.wait_for(timeout=600000)

    print("Processamento concluído.")


# ==========================================================
# DOWNLOAD
# ==========================================================

def baixar(page, data):

    nome = f"Analítico_{data.strftime('%d-%m-%Y')}.xlsx"

    destino = BASES / nome

    print(f"Baixando {nome}")

    # Mesmo timeout da versão original que foi validada pelo usuário.
    with page.expect_download(timeout=600000) as download:

        page.get_by_role(
            "button",
            name=" Exportar para Excel"
        ).click(
            force=True,
            no_wait_after=True
        )

    download = download.value

    download.save_as(destino)

    page.wait_for_timeout(1000)

    print("Download concluído.")

    return str(destino)


# ==========================================================
# EXECUÇÃO
# ==========================================================

def executar(
    data_inicial,
    data_final,
    token,
    callback=None,
    arquivo_callback=None,
):

    arquivos = []

    total = (data_final - data_inicial).days + 1

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=HEADLESS,
            slow_mo=SLOW_MO_MS,
        )

        context = browser.new_context(
            accept_downloads=True
        )

        page = context.new_page()

        try:

            # -----------------------------
            # LOGIN (APENAS UMA VEZ)
            # -----------------------------
            login(page, token)

            data = data_inicial
            contador = 1

            while data <= data_final:

                print("=" * 60)
                print(f"Extraindo {data.strftime('%d/%m/%Y')}")
                print("=" * 60)

                if callback:

                    callback(
                        contador,
                        total,
                        data
                    )

                try:

                    # -----------------------------------
                    # REABRE A TELA DO RELATÓRIO
                    # -----------------------------------

                    abrir_relatorio(page)

                    # -----------------------------------
                    # PREENCHE A DATA
                    # -----------------------------------

                    preencher_data(
                        page,
                        data
                    )

                    # -----------------------------------
                    # PROCESSA
                    # -----------------------------------

                    processar(page)

                    # -----------------------------------
                    # DOWNLOAD
                    # -----------------------------------

                    arquivo = baixar(
                        page,
                        data
                    )

                    arquivos.append(arquivo)

                    # A limpeza é iniciada assim que o download termina,
                    # mas a extração segue seu loop original normalmente.
                    if arquivo_callback:
                        arquivo_callback(
                            arquivo,
                            contador,
                            total,
                            data,
                        )

                    print(
                        f"✔ {data.strftime('%d/%m/%Y')} concluído."
                    )

                except TimeoutError as erro:

                    print(
                        f"Erro na data {data.strftime('%d/%m/%Y')}"
                    )

                    raise Exception(
                        f"Falha ao extrair {data.strftime('%d/%m/%Y')}.\n\n{erro}"
                    )

                # -----------------------------------
                # PEQUENA PAUSA
                # -----------------------------------

                page.wait_for_timeout(1000)

                data += timedelta(days=1)

                contador += 1

        finally:

            print("Encerrando navegador...")

            context.close()

            browser.close()

    return arquivos
