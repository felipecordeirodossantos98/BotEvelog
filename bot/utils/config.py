from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


PASTA_BOT = Path(__file__).resolve().parents[1]
PASTA_RAIZ = PASTA_BOT.parent
ARQUIVO_ENV = PASTA_BOT / ".env"

load_dotenv(
    ARQUIVO_ENV,
    override=False,
)


def _env(nome: str, padrao: str = "") -> str:
    return os.getenv(nome, padrao).strip()


def _env_bool(nome: str, padrao: bool = True) -> bool:
    valor = os.getenv(nome)

    if valor is None:
        return padrao

    return valor.strip().lower() in {
        "1",
        "true",
        "t",
        "yes",
        "y",
        "sim",
        "s",
        "on",
    }


def _env_int(nome: str, padrao: int) -> int:
    try:
        return int(_env(nome, str(padrao)))
    except ValueError:
        return padrao


def _caminho_configuravel(
    nome_variavel: str,
    padrao: Path,
) -> Path:
    valor = _env(nome_variavel)

    if not valor:
        return padrao

    caminho = Path(valor).expanduser()

    if caminho.is_absolute():
        return caminho

    return PASTA_RAIZ / caminho


# -----------------------------------------------------------------------------
# Variáveis de ambiente
# -----------------------------------------------------------------------------

URL_FRACTION = _env("URL_FRACTION")
FRACTION_USER = _env("FRACTION_USER")
FRACTION_PASSWORD = _env("FRACTION_PASSWORD")

FRACTION_USER_ANALYTIC = _env("FRACTION_USER_ANALYTIC")
FRACTION_PASSWORD_ANALYTIC = _env("FRACTION_PASSWORD_ANALYTIC")

URL_ZENDESK = _env("URL_ZENDESK")
ZENDESK_USER = _env("ZENDESK_USER")
ZENDESK_PASSWORD = _env("ZENDESK_PASSWORD")

URL_MEUDANFE = (
    _env("URL_MEDANFE")
    or _env("URL_MEUDANFE")
    or "https://api.meudanfe.com.br/v2"
).rstrip("/")
API_KEY_MEUDANFE = _env("API_KEY_MEUDANFE")

HEADLESS = _env_bool("HEADLESS", padrao=True)
SLOW_MO_MS = _env_int("SLOW_MO_MS", 300)


# -----------------------------------------------------------------------------
# Pastas compartilhadas
# -----------------------------------------------------------------------------

PASTA_RESULTADOS = PASTA_RAIZ / "resultados"
PASTA_LOGS = PASTA_RAIZ / "logs"
PASTA_ESTADO = PASTA_RAIZ / "estado"
PASTA_PERFIS = PASTA_RAIZ / "perfis"
PASTA_DADOS = PASTA_BOT / "dados"

RESULTADOS_DANFES = PASTA_RESULTADOS / "danfes"
RESULTADOS_ORDENS_MALOTES = PASTA_RESULTADOS / "ordens_de_coleta_malotes"
RESULTADOS_ORDENS_EQUIPAMENTOS = PASTA_RESULTADOS / "ordens_de_coleta_equipamentos"
# Alias legado: a automação de ordens existente usa RESULTADOS_ORDENS.
RESULTADOS_ORDENS = RESULTADOS_ORDENS_MALOTES
RESULTADOS_TICKETS_ZENDESK = PASTA_RESULTADOS / "tickets_zendesk"
RESULTADOS_PERFORMANCE = PASTA_RESULTADOS / "relatorios_performance"
RESULTADOS_TDES = PASTA_RESULTADOS / "tdes"

RESULTADOS_ANALITICO = _caminho_configuravel(
    "RESULTADOS_PATH",
    PASTA_RESULTADOS / "relatorios_analitico",
)
ORIGINAIS_ANALITICO = _caminho_configuravel(
    "ORIGINAIS_PATH",
    RESULTADOS_ANALITICO / ".originais",
)
BASES_DIARIAS_ANALITICO = _caminho_configuravel(
    "BASES_DIARIAS_PATH",
    RESULTADOS_ANALITICO / "bases_diarias",
)

PERFIL_ZENDESK = PASTA_PERFIS / "gerar_tickets_zendesk" / "chromium"

ESTADO_ZENDESK = PASTA_ESTADO / "gerar_tickets_zendesk"
ARQUIVO_PEDIDOS_COM_TICKET = (
    ESTADO_ZENDESK
    / "pedidos_com_ticket.txt"
)

DADOS_ORDENS = PASTA_DADOS / "gerar_ordens_de_coleta"
ARQUIVO_BASE_CNPJS = DADOS_ORDENS / "base_cnpjs.json"
ARQUIVO_EMAILS_UNIDADES = DADOS_ORDENS / "emails_unidades.json"


# -----------------------------------------------------------------------------
# Configuração obrigatória por fluxo
# -----------------------------------------------------------------------------

VARIAVEIS_POR_FLUXO: dict[str, tuple[str, ...]] = {
    "gerar_ordens_de_coleta": (
        "URL_FRACTION",
        "FRACTION_USER",
        "FRACTION_PASSWORD",
    ),
    "gerar_ordens_de_coleta_malotes": (
        "URL_FRACTION",
        "FRACTION_USER",
        "FRACTION_PASSWORD",
    ),
    "gerar_ordens_de_coleta_equipamentos": (
        "URL_FRACTION",
        "FRACTION_USER",
        "FRACTION_PASSWORD",
    ),
    "gerar_tickets_zendesk": (
        "URL_ZENDESK",
        "ZENDESK_USER",
        "ZENDESK_PASSWORD",
    ),
    "baixar_danfes": (
        "URL_FRACTION",
        "FRACTION_USER",
        "FRACTION_PASSWORD",
        "API_KEY_MEUDANFE",
    ),
    "baixar_relatorios_performance": (
        "URL_FRACTION",
        "FRACTION_USER",
        "FRACTION_PASSWORD",
    ),
    "buscar_tdes": (
        "URL_FRACTION",
        "FRACTION_USER",
        "FRACTION_PASSWORD",
    ),
    "baixar_relatorios_analitico": (
        "URL_FRACTION",
        "FRACTION_USER_ANALYTIC",
        "FRACTION_PASSWORD_ANALYTIC",
    ),
}


# Variáveis opcionais: geram aviso, mas não bloqueiam a execução.
# O Zendesk consegue concluir os tickets e gerar a planilha mesmo
# quando a etapa posterior do Fraction não estiver configurada.
VARIAVEIS_OPCIONAIS_POR_FLUXO: dict[str, tuple[str, ...]] = {
    "gerar_tickets_zendesk": (
        "URL_FRACTION",
        "FRACTION_USER",
        "FRACTION_PASSWORD",
    ),
}


def variaveis_faltantes(fluxo: str) -> list[str]:
    return [
        nome
        for nome in VARIAVEIS_POR_FLUXO.get(fluxo, ())
        if not _env(nome)
    ]


def variaveis_opcionais_faltantes(fluxo: str) -> list[str]:
    return [
        nome
        for nome in VARIAVEIS_OPCIONAIS_POR_FLUXO.get(fluxo, ())
        if not _env(nome)
    ]


def configuracao_pronta(fluxo: str) -> bool:
    return not variaveis_faltantes(fluxo)
