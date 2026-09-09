from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
from threading import Lock
from typing import Callable

from .config import PASTA_LOGS


LogCallback = Callable[[str], None]


PADRAO_ICONES_LOG = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\u2600-\u26FF"
    "\u2700-\u27BF"
    "\uFE0F"
    "]"
)


def limpar_icones_log(
    mensagem: object,
) -> str:
    """
    Remove emojis/ícones decorativos dos logs.

    Mantém letras, acentos, números e pontuação normal.
    """
    texto = str(
        mensagem
    )

    texto = PADRAO_ICONES_LOG.sub(
        "",
        texto,
    )

    return texto.strip()


def criar_logger_execucao(
    fluxo: str,
    log_visual: LogCallback | None = None,
    *,
    rotulo: str = "execucao",
) -> tuple[LogCallback, Path]:
    """
    Cria um arquivo TXT por execução e devolve um callback compatível
    com os callbacks de log usados pelas automações.

    O texto é salvo em:
        <raiz>/logs/<fluxo>/<rotulo>_AAAA-MM-DD_HH-MM-SS.txt

    O callback também encaminha a mensagem para a interface, quando
    ``log_visual`` for informado.
    """
    pasta = PASTA_LOGS / fluxo
    pasta.mkdir(
        parents=True,
        exist_ok=True,
    )

    agora = datetime.now()
    nome_base = (
        f"{rotulo}_{agora.strftime('%Y-%m-%d_%H-%M-%S')}"
    )

    caminho = pasta / f"{nome_base}.txt"
    sequencia = 2

    while caminho.exists():
        caminho = pasta / (
            f"{nome_base}_{sequencia}.txt"
        )
        sequencia += 1

    lock = Lock()

    cabecalho = (
        "Bot Evelog\n"
        f"Automação: {fluxo}\n"
        f"Início: {agora.strftime('%d/%m/%Y %H:%M:%S')}\n"
        + "=" * 72
        + "\n"
    )

    caminho.write_text(
        cabecalho,
        encoding="utf-8",
    )

    def log(mensagem: str) -> None:
        texto = limpar_icones_log(
            mensagem
        )
        horario = datetime.now().strftime(
            "%d/%m/%Y %H:%M:%S"
        )
        linha = f"[{horario}] {texto}\n"

        with lock:
            with caminho.open(
                "a",
                encoding="utf-8",
            ) as arquivo:
                arquivo.write(linha)

        if log_visual is not None:
            log_visual(texto)

    log(
        f"Arquivo de log: {caminho}"
    )

    return log, caminho
