from __future__ import annotations

from pathlib import Path
from typing import Iterable

from servicos.fila_romaneios_emissao import FilaRomaneiosEmissao
from utils.config import ARQUIVO_SHIPMENTS_ROMANEIOS_FINALIZADOS
from utils.estado import RegistroTexto


def carregar_shipments_finalizados() -> set[str]:
    caminho = ARQUIVO_SHIPMENTS_ROMANEIOS_FINALIZADOS
    if not caminho.is_file():
        return set()
    return {
        linha.strip()
        for linha in caminho.read_text(encoding="utf-8-sig").splitlines()
        if linha.strip()
    }


def registrar_shipments_finalizados(shipments: Iterable[str]) -> list[str]:
    registro = RegistroTexto(ARQUIVO_SHIPMENTS_ROMANEIOS_FINALIZADOS)
    return registro.adicionar_varios(shipments)


def sincronizar_shipments_finalizados(
    caminhos_fila: Iterable[Path | str],
) -> set[str]:
    caminhos = list(dict.fromkeys(Path(caminho) for caminho in caminhos_fila))
    shipments_finalizados = carregar_shipments_finalizados()

    # Migra para o registro compartilhado os romaneios já fechados antes desta regra.
    for caminho in caminhos:
        if caminho.is_file():
            shipments_finalizados.update(
                FilaRomaneiosEmissao(caminho).shipments_finalizados_na_fila()
            )
    registrar_shipments_finalizados(shipments_finalizados)
    shipments_finalizados = carregar_shipments_finalizados()

    # Bloqueia cópias abertas do mesmo pedido nos bancos das duas filas.
    for caminho in caminhos:
        if caminho.is_file():
            FilaRomaneiosEmissao(caminho).finalizar_shipments(
                shipments_finalizados
            )
    return shipments_finalizados
