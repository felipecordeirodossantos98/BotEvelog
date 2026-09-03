import os
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()

URL_FRACTION = os.getenv("URL_FRACTION", "").strip()
FRACTION_USER = os.getenv("FRACTION_USER_ANALYTIC", "").strip()
FRACTION_PASSWORD = os.getenv("FRACTION_PASSWORD_ANALYTIC", "")

PASTA_BOT = Path(__file__).resolve().parents[3]
PASTA_RAIZ = PASTA_BOT.parent

RESULTADOS_PATH = (
    os.getenv("RESULTADOS_PATH", "").strip()
    or str(
        PASTA_RAIZ
        / "resultados"
        / "relatorios_analitico"
    )
)

ORIGINAIS_PATH = os.getenv(
    "ORIGINAIS_PATH",
    str(Path(RESULTADOS_PATH) / ".originais"),
).strip()

BASES_DIARIAS_PATH = os.getenv(
    "BASES_DIARIAS_PATH",
    str(Path(RESULTADOS_PATH) / "bases_diarias"),
).strip()

HEADLESS = os.getenv(
    "HEADLESS",
    "False",
).strip().lower() in {
    "1",
    "true",
    "yes",
    "sim",
}

try:
    SLOW_MO_MS = int(
        os.getenv("SLOW_MO_MS", "300")
    )
except ValueError:
    SLOW_MO_MS = 300


def validar_configuracao():
    faltantes = []

    if not URL_FRACTION:
        faltantes.append("URL_FRACTION")

    if not FRACTION_USER:
        faltantes.append("FRACTION_USER")

    if not FRACTION_PASSWORD:
        faltantes.append("FRACTION_PASSWORD")

    if faltantes:
        raise RuntimeError(
            "Variável(is) ausente(s) no .env: "
            + ", ".join(faltantes)
        )
