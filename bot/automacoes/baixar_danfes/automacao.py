import os
import re
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

PASTA_FLUXO = Path(__file__).resolve().parent
PASTA_PROJETO = Path(__file__).resolve().parents[2]

BASE_DIR = PASTA_FLUXO
ENV_FILE = PASTA_PROJETO / ".env"

PASTA_PERFIS = (
    PASTA_PROJETO.parent
    / "perfis"
)

PERFIL_FRACTION = (
    PASTA_PERFIS
    / "baixar_danfes"
    / "chromium"
)

DEFAULT_FRACTION_URL = "https://www.jadlog.com.br/FractionWeb/login.jad?state=invalid"


def _carregar_env():
    load_dotenv(ENV_FILE, override=False)


def _valor_obrigatorio(nome):
    valor = os.getenv(nome, "").strip()
    if not valor:
        raise RuntimeError(f"Variável {nome} não informada no arquivo .env")
    return valor


def _valor_bool(nome, padrao=False):
    valor = os.getenv(nome)
    if valor is None:
        return padrao

    valor = valor.strip().lower()
    if valor in {"1", "true", "t", "yes", "y", "sim", "s"}:
        return True
    if valor in {"0", "false", "f", "no", "n", "nao", "não"}:
        return False

    raise RuntimeError(
        f"Valor inválido para {nome}: {valor!r}. Use True ou False."
    )


def carregar_config_fraction():
    _carregar_env()

    url = os.getenv("URL_FRACTION", DEFAULT_FRACTION_URL).strip()
    usuario = _valor_obrigatorio("FRACTION_USER")
    senha = _valor_obrigatorio("FRACTION_PASSWORD")
    headless = _valor_bool("HEADLESS", padrao=True)

    if not url:
        url = DEFAULT_FRACTION_URL

    return url, usuario, senha, headless


def buscar_chaves(df: pd.DataFrame, log=lambda msg: None) -> pd.DataFrame:
    url_fraction, usuario, senha, headless = carregar_config_fraction()

    resultado = df.copy()
    resultado["chave_nfe"] = ""
    resultado["status_fraction"] = "PENDENTE"
    resultado["status_danfe"] = "PENDENTE"
    resultado["mensagem"] = ""

    perfil = PERFIL_FRACTION

    perfil.mkdir(
        parents=True,
        exist_ok=True,
    )

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(perfil),
            headless=headless,
            slow_mo=0 if headless else 200,
        )
        page = context.pages[0] if context.pages else context.new_page()

        try:
            log("🔐 Fazendo login no FractionWeb...")
            page.goto(url_fraction, wait_until="domcontentloaded", timeout=60000)
            page.fill("input[name='id_usuario']", usuario)
            page.fill("input[name='id_senha']", senha)
            page.click("input[type='submit']")
            page.wait_for_load_state("networkidle", timeout=60000)

            try:
                page.get_by_role("button", name="Close").first.click(timeout=5000)
            except Exception:
                pass

            log("✅ Login realizado.")

            for index, row in resultado.iterrows():
                pedido = str(row["pedido"]).strip()
                if not pedido or pedido.lower() == "nan":
                    resultado.at[index, "status_fraction"] = "PULADO"
                    resultado.at[index, "status_danfe"] = "PULADO"
                    resultado.at[index, "mensagem"] = "Pedido vazio"
                    continue

                try:
                    log(f"📦 {pedido}: buscando chave da NF-e...")
                    page.get_by_role("link", name="Consultas").click(timeout=15000)
                    page.get_by_role("link", name="Pesquisar").click(timeout=15000)
                    page.wait_for_load_state("networkidle", timeout=60000)

                    campo = page.get_by_role("textbox").first
                    campo.fill("")
                    campo.fill(pedido)
                    page.get_by_role("button", name="Processar").click()

                    toggler = page.locator(".ui-tree-toggler").first
                    toggler.wait_for(timeout=15000)
                    toggler.click()

                    item_nf = page.get_by_role("treeitem").filter(has_text="NFe:").last
                    item_nf.wait_for(timeout=15000)
                    match = re.search(r"\d{44}", item_nf.inner_text())
                    if not match:
                        raise RuntimeError("Chave de 44 dígitos não encontrada")

                    chave = match.group()
                    resultado.at[index, "chave_nfe"] = chave
                    resultado.at[index, "status_fraction"] = "OK"
                    resultado.at[index, "mensagem"] = "Chave encontrada"
                    log(f"✅ {pedido}: {chave}")

                except Exception as exc:
                    resultado.at[index, "status_fraction"] = "ERRO"
                    resultado.at[index, "status_danfe"] = "PULADO"
                    resultado.at[index, "mensagem"] = f"FractionWeb: {exc}"
                    log(f"❌ {pedido}: erro no FractionWeb — {exc}")
        finally:
            context.close()

    return resultado