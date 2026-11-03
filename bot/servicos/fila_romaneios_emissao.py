from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Iterable

import pandas as pd


class FilaRomaneiosEmissao:
    """Persistência local da fila incremental de etiquetas de emissão."""

    def __init__(self, caminho: Path) -> None:
        self.caminho = Path(caminho)
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        self._inicializar()

    def _conectar(self) -> sqlite3.Connection:
        conexao = sqlite3.connect(self.caminho, timeout=30)
        conexao.row_factory = sqlite3.Row
        conexao.execute("PRAGMA foreign_keys = ON")
        conexao.execute("PRAGMA busy_timeout = 30000")
        return conexao

    def _inicializar(self) -> None:
        with closing(self._conectar()) as conexao:
            conexao.execute("PRAGMA journal_mode = WAL")
            conexao.execute("PRAGMA synchronous = FULL")
            conexao.executescript(
                """
                CREATE TABLE IF NOT EXISTS arquivos_drive (
                    chave TEXT PRIMARY KEY,
                    nome TEXT NOT NULL,
                    caminho_no_zip TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    importado_em TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS etiquetas (
                    etiqueta TEXT PRIMARY KEY,
                    shipment_id TEXT NOT NULL,
                    cte TEXT NOT NULL,
                    rua TEXT NOT NULL,
                    palete_origem TEXT NOT NULL,
                    triagem TEXT NOT NULL,
                    rota TEXT NOT NULL,
                    destino TEXT NOT NULL,
                    uf TEXT NOT NULL,
                    cidade TEXT NOT NULL,
                    volumes_sistema INTEGER,
                    status TEXT NOT NULL,
                    origem TEXT NOT NULL,
                    estado TEXT NOT NULL DEFAULT 'ABERTO',
                    manifesto_id INTEGER,
                    arquivo_origem TEXT NOT NULL,
                    importado_em TEXT NOT NULL,
                    FOREIGN KEY(manifesto_id) REFERENCES romaneios(id)
                );

                CREATE INDEX IF NOT EXISTS idx_etiquetas_fila
                    ON etiquetas(estado, rua, triagem, rota);
                CREATE INDEX IF NOT EXISTS idx_etiquetas_shipment
                    ON etiquetas(shipment_id, rua, palete_origem, estado);

                CREATE TABLE IF NOT EXISTS romaneios (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rua TEXT NOT NULL,
                    triagem TEXT NOT NULL,
                    rota TEXT NOT NULL,
                    quantidade_etiquetas INTEGER NOT NULL,
                    quantidade_shipments INTEGER NOT NULL,
                    caminho_pdf TEXT NOT NULL,
                    fechado_em TEXT NOT NULL
                );
                """
            )

    def arquivos_importados(self) -> set[str]:
        with closing(self._conectar()) as conexao:
            linhas = conexao.execute(
                "SELECT chave FROM arquivos_drive"
            ).fetchall()
        return {str(linha["chave"]) for linha in linhas}

    def nomes_arquivos_importados(self) -> set[str]:
        with closing(self._conectar()) as conexao:
            linhas = conexao.execute(
                "SELECT DISTINCT nome FROM arquivos_drive"
            ).fetchall()
        return {str(linha["nome"]).strip().casefold() for linha in linhas}

    def importar_fila(
        self,
        tabela: pd.DataFrame,
        arquivos: Iterable[dict[str, str]],
        *,
        log=None,
    ) -> dict[str, int]:
        """Grava uma sincronização inteira atomicamente e deduplica etiquetas."""
        agora = datetime.now(timezone.utc).isoformat(timespec="seconds")
        novos = 0
        repetidos = 0
        arquivos = list(arquivos)
        linhas = tabela.to_dict(orient="records") if not tabela.empty else []

        with closing(self._conectar()) as conexao:
            conexao.execute("BEGIN IMMEDIATE")
            try:
                for linha in linhas:
                    etiquetas = [
                        valor.strip()
                        for valor in str(linha.get("Etiquetas do pedido", "")).splitlines()
                        if valor.strip() and valor.strip().lower() != "nan"
                    ]
                    ctes_por_etiqueta = [
                        valor.strip()
                        for valor in str(linha.get("CTEs por etiqueta", "")).splitlines()
                    ]
                    if ctes_por_etiqueta and len(ctes_por_etiqueta) != len(etiquetas):
                        raise ValueError(
                            "A quantidade de CTEs associados não corresponde à "
                            "quantidade de etiquetas do pedido "
                            f"{_texto(linha.get('ShipmentId'))}."
                        )
                    if not ctes_por_etiqueta:
                        ctes_por_etiqueta = [_texto(linha.get("CTE"))] * len(etiquetas)
                    cte_por_etiqueta = dict(zip(etiquetas, ctes_por_etiqueta))
                    for etiqueta in dict.fromkeys(etiquetas):
                        dados = {
                            "etiqueta": etiqueta,
                            "shipment_id": _texto(linha.get("ShipmentId")),
                            "cte": cte_por_etiqueta.get(etiqueta, ""),
                            "rua": _texto(linha.get("Rua")),
                            "palete_origem": _texto(linha.get("Palete de origem")),
                            "triagem": _texto(linha.get("Triagem")),
                            "rota": _texto(linha.get("Rota")),
                            "destino": _texto(linha.get("Destino")),
                            "uf": _texto(linha.get("UF")),
                            "cidade": _texto(linha.get("Cidade")),
                            "volumes_sistema": _inteiro_ou_none(
                                linha.get("Volumes Sistema")
                            ),
                            "status": _texto(linha.get("Status")),
                            "origem": _texto(linha.get("Origem")),
                            "arquivo_origem": _texto(
                                linha.get("Palete de origem")
                            ),
                            "importado_em": agora,
                        }
                        existente = conexao.execute(
                            "SELECT rua, triagem, estado FROM etiquetas "
                            "WHERE etiqueta = ?",
                            (etiqueta,),
                        ).fetchone()
                        if existente:
                            if (
                                existente["estado"] == "ABERTO"
                                and existente["rua"]
                                and dados["rua"]
                                and _chave(existente["rua"]) != _chave(dados["rua"])
                            ) or (
                                existente["estado"] == "ABERTO"
                                and existente["triagem"]
                                and dados["triagem"]
                                and _chave(existente["triagem"])
                                != _chave(dados["triagem"])
                            ):
                                raise ValueError(
                                    f"A etiqueta {etiqueta} já está na fila aberta "
                                    "com outra rua ou triagem. Confira os TXT e as "
                                    "regras de rota antes de sincronizar."
                                )
                            if existente["estado"] == "ABERTO":
                                conexao.execute(
                                "UPDATE etiquetas SET "
                                "cte = COALESCE(NULLIF(?, ''), cte), "
                                "triagem = COALESCE(NULLIF(?, ''), triagem), "
                                "rota = COALESCE(NULLIF(?, ''), rota), "
                                "destino = COALESCE(NULLIF(?, ''), destino), "
                                "uf = COALESCE(NULLIF(?, ''), uf), "
                                "cidade = COALESCE(NULLIF(?, ''), cidade), "
                                "volumes_sistema = ?, "
                                    "status = COALESCE(NULLIF(?, ''), status) "
                                    "WHERE etiqueta = ?",
                                    (
                                        dados["cte"], dados["triagem"], dados["rota"],
                                        dados["destino"], dados["uf"], dados["cidade"],
                                        dados["volumes_sistema"], dados["status"], etiqueta,
                                    ),
                                )
                            repetidos += 1
                            continue

                        conexao.execute(
                            "INSERT INTO etiquetas ("
                            "etiqueta, shipment_id, cte, rua, palete_origem, triagem, "
                            "rota, destino, uf, cidade, volumes_sistema, status, origem, "
                            "arquivo_origem, importado_em"
                            ") VALUES ("
                            ":etiqueta, :shipment_id, :cte, :rua, :palete_origem, :triagem, "
                            ":rota, :destino, :uf, :cidade, :volumes_sistema, :status, :origem, "
                            ":arquivo_origem, :importado_em"
                            ")",
                            dados,
                        )
                        novos += 1

                for arquivo in arquivos:
                    conexao.execute(
                        "INSERT OR IGNORE INTO arquivos_drive "
                        "(chave, nome, caminho_no_zip, sha256, importado_em) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            arquivo["chave"],
                            arquivo["nome"],
                            arquivo["caminho_no_zip"],
                            arquivo["sha256"],
                            agora,
                        ),
                    )
                conexao.commit()
            except Exception:
                conexao.rollback()
                raise

        if log is not None:
            log(
                f"Persistência incremental: {novos} etiqueta(s) nova(s), "
                f"{repetidos} etiqueta(s) já conhecida(s), "
                f"{len(list(arquivos))} arquivo(s) registrado(s)."
            )
        return {"novas": novos, "repetidas": repetidos}

    def carregar_etiquetas_abertas(self) -> pd.DataFrame:
        with closing(self._conectar()) as conexao:
            linhas = conexao.execute(
                "SELECT * FROM etiquetas WHERE estado = 'ABERTO' "
                "ORDER BY rua, triagem, rota, palete_origem, shipment_id, etiqueta"
            ).fetchall()
        return pd.DataFrame([dict(linha) for linha in linhas])

    def atualizar_rotas_abertas(self, tabela: pd.DataFrame) -> int:
        """Persiste rota/triagem recalculadas somente nas etiquetas ainda abertas."""
        atualizacoes: dict[str, tuple[str, str]] = {}
        for _, linha in tabela.iterrows():
            rota = _texto(linha.get("Rota"))
            triagem = _texto(linha.get("Triagem"))
            for etiqueta in str(linha.get("Etiquetas do pedido", "")).splitlines():
                etiqueta = etiqueta.strip()
                if etiqueta:
                    atualizacoes[etiqueta] = (rota, triagem)

        if not atualizacoes:
            return 0

        with closing(self._conectar()) as conexao:
            conexao.execute("BEGIN IMMEDIATE")
            try:
                cursor = conexao.executemany(
                    "UPDATE etiquetas SET rota = ?, triagem = ? "
                    "WHERE etiqueta = ? AND estado = 'ABERTO' "
                    "AND (rota IS NOT ? OR triagem IS NOT ?)",
                    [
                        (rota, triagem, etiqueta, rota, triagem)
                        for etiqueta, (rota, triagem) in atualizacoes.items()
                    ],
                )
                alteradas = max(cursor.rowcount, 0)
                conexao.commit()
            except Exception:
                conexao.rollback()
                raise
        return alteradas

    def atualizar_status_abertas(self, status_por_cte: dict[str, str]) -> int:
        """Atualiza status por CTE somente nas etiquetas que ainda estão abertas."""
        atualizacoes = [
            (str(status).strip(), str(cte).strip())
            for cte, status in status_por_cte.items()
            if str(cte).strip() and str(status).strip()
        ]
        if not atualizacoes:
            return 0

        with closing(self._conectar()) as conexao:
            conexao.execute("BEGIN IMMEDIATE")
            try:
                cursor = conexao.executemany(
                    "UPDATE etiquetas SET status = ? "
                    "WHERE estado = 'ABERTO' AND cte = ? AND status IS NOT ?",
                    [
                        (status, cte, status)
                        for status, cte in atualizacoes
                    ],
                )
                alteradas = max(cursor.rowcount, 0)
                conexao.commit()
            except Exception:
                conexao.rollback()
                raise
        return alteradas

    def fechar_romaneio(
        self,
        *,
        rua: str,
        triagem: str,
        rota: str,
        etiquetas: Iterable[str],
        caminho_pdf: Path | str,
    ) -> int:
        etiquetas_unicas = list(dict.fromkeys(
            etiqueta.strip() for etiqueta in etiquetas if etiqueta.strip()
        ))
        if not etiquetas_unicas:
            raise ValueError("Não há etiquetas para fechar neste romaneio.")

        agora = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with closing(self._conectar()) as conexao:
            conexao.execute("BEGIN IMMEDIATE")
            try:
                existentes = conexao.execute(
                    "SELECT etiqueta, shipment_id FROM etiquetas "
                    "WHERE estado = 'ABERTO' AND rua = ? AND triagem = ? AND rota = ?",
                    (rua, triagem, rota),
                ).fetchall()
                selecionadas = set(etiquetas_unicas)
                etiquetas_fechadas = [
                    str(linha["etiqueta"])
                    for linha in existentes
                    if str(linha["etiqueta"]) in selecionadas
                ]
                if not etiquetas_fechadas:
                    raise ValueError(
                        "A fila selecionada já foi fechada ou não corresponde "
                        "às etiquetas abertas. Sincronize novamente."
                    )
                shipments = len({
                    str(linha["shipment_id"])
                    for linha in existentes
                    if str(linha["etiqueta"]) in selecionadas
                })
                cursor = conexao.execute(
                    "INSERT INTO romaneios "
                    "(rua, triagem, rota, quantidade_etiquetas, "
                    "quantidade_shipments, caminho_pdf, fechado_em) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        rua, triagem, rota, len(etiquetas_fechadas),
                        int(shipments), str(caminho_pdf), agora,
                    ),
                )
                manifesto_id = cursor.lastrowid
                conexao.executemany(
                    "UPDATE etiquetas SET estado = 'FECHADO', manifesto_id = ? "
                    "WHERE etiqueta = ? AND estado = 'ABERTO'",
                    [(manifesto_id, etiqueta) for etiqueta in etiquetas_fechadas],
                )
                conexao.commit()
            except Exception:
                conexao.rollback()
                raise
        return len(etiquetas_fechadas)

    def carregar_historico(self, limite: int = 100) -> pd.DataFrame:
        with closing(self._conectar()) as conexao:
            linhas = conexao.execute(
                "SELECT rua AS Rua, triagem AS Triagem, rota AS Rota, "
                "quantidade_shipments AS Pedidos, "
                "quantidade_etiquetas AS Etiquetas, caminho_pdf AS PDF, "
                "fechado_em AS 'Fechado em' "
                "FROM romaneios ORDER BY id DESC LIMIT ?",
                (limite,),
            ).fetchall()
        return pd.DataFrame([dict(linha) for linha in linhas])


def _texto(valor: object) -> str:
    if valor is None or pd.isna(valor):
        return ""
    return str(valor).strip()


def _inteiro_ou_none(valor: object) -> int | None:
    if valor is None or pd.isna(valor):
        return None
    texto = str(valor).strip()
    if not texto or texto.lower() == "nan":
        return None
    return int(float(texto))


def _chave(valor: object) -> str:
    return " ".join(str(valor).strip().upper().split())
