import os

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright


# ==================================================
# CARREGA VARIÁVEIS DO .ENV
# ==================================================

load_dotenv()

URL_FRACTION = os.getenv("URL_FRACTION")
FRACTION_USER = os.getenv("FRACTION_USER")
FRACTION_PASSWORD = os.getenv("FRACTION_PASSWORD")


# ==================================================
# VALIDA CONFIGURAÇÃO
# ==================================================

if not URL_FRACTION:
    raise RuntimeError("A variável URL_FRACTION não foi configurada no .env.")

if not FRACTION_USER:
    raise RuntimeError("A variável FRACTION_USER não foi configurada no .env.")

if not FRACTION_PASSWORD:
    raise RuntimeError("A variável FRACTION_PASSWORD não foi configurada no .env.")


# ==================================================
# AUTOMACAO
# ==================================================

with sync_playwright() as p:

    browser = p.chromium.launch(
        headless=False
    )

    page = browser.new_page()

    print("Abrindo Fraction...")
    page.goto(URL_FRACTION)

    print("Preenchendo usuário...")
    page.get_by_role("textbox", name="Usuário").fill(FRACTION_USER)

    print("Preenchendo senha...")
    page.get_by_role("textbox", name="Senha").fill(FRACTION_PASSWORD)

    print("Clicando em Login...")
    page.get_by_role("button", name="Login").click()

    print("Login enviado.")
    print("Verifique o navegador.")

    # Mantém o navegador aberto
    input("\nPressione ENTER para fechar o navegador...")

    browser.close()
