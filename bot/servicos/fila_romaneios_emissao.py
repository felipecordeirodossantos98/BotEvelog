from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from pathlib import PurePosixPath
import sqlite3
from typing import Iterable
import unicodedata

import pandas as pd


class FilaRomaneiosEmissao:
    """Persistência local e isolada de uma fila incremental de romaneios."""

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

                CREATE TABLE IF NOT EXISTS arquivos_drive_dar_entrada (
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

                CREATE TABLE IF NOT EXISTS etiquetas_conflito (
                    etiqueta TEXT NOT NULL,
                    shipment_id TEXT NOT NULL,
                    rua TEXT NOT NULL,
                    triagem TEXT NOT NULL,
                    palete_origem TEXT NOT NULL,
                    rota TEXT NOT NULL,
                    destino TEXT NOT NULL,
                    arquivo_origem TEXT NOT NULL,
                    detectado_em TEXT NOT NULL,
                    PRIMARY KEY (
                        etiqueta, rua, triagem, palete_origem, arquivo_origem
                    )
                );
                """
            )

    def arquivos_importados(self) -> set[str]:
        with closing(self._conectar()) as conexao:
            linhas = conexao.execute(
                "SELECT chave, nome, sha256 FROM arquivos_drive"
            ).fetchall()
        chaves: set[str] = set()
        for linha in linhas:
            chave = str(linha["chave"] or "")
            nome = _nome_normalizado(str(linha["nome"] or ""))
            digest = str(linha["sha256"] or "")
            if chave:
                chaves.add(chave)
            if nome and digest:
                chaves.add(_chave_nome_conteudo(nome, digest))
        return chaves

    def nomes_arquivos_importados(self) -> set[str]:
        with closing(self._conectar()) as conexao:
            linhas = conexao.execute(
                "SELECT DISTINCT nome FROM arquivos_drive"
            ).fetchall()
        return {str(linha["nome"]).strip().casefold() for linha in linhas}

    def arquivos_dar_entrada_importados(self) -> set[str]:
        """Retorna os TXT já processados pela automação Dar Entrada."""
        with closing(self._conectar()) as conexao:
            linhas = conexao.execute(
                "SELECT chave, nome, sha256 FROM arquivos_drive_dar_entrada"
            ).fetchall()
        chaves: set[str] = set()
        for linha in linhas:
            chave = str(linha["chave"] or "")
            nome = _nome_normalizado(str(linha["nome"] or ""))
            digest = str(linha["sha256"] or "")
            if chave:
                chaves.add(chave)
            if nome and digest:
                chaves.add(_chave_nome_conteudo(nome, digest))
        return chaves

    def registrar_arquivos_dar_entrada(
        self,
        arquivos: Iterable[dict[str, str]],
    ) -> int:
        """Registra TXT após os CTEs correspondentes entrarem no Fraction."""
        arquivos = list(arquivos)
        if not arquivos:
            return 0
        agora = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with closing(self._conectar()) as conexao:
            cursor = conexao.executemany(
                "INSERT OR IGNORE INTO arquivos_drive_dar_entrada "
                "(chave, nome, caminho_no_zip, sha256, importado_em) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        arquivo["chave"],
                        arquivo["nome"],
                        arquivo["caminho_no_zip"],
                        arquivo["sha256"],
                        agora,
                    )
                    for arquivo in arquivos
                ],
            )
            conexao.commit()
            return max(cursor.rowcount, 0)

    def etiquetas_conhecidas(self) -> set[str]:
        """Retorna etiquetas já presentes na fila ou no histórico local."""
        with closing(self._conectar()) as conexao:
            linhas = conexao.execute(
                "SELECT etiqueta FROM etiquetas "
                "UNION SELECT etiqueta FROM etiquetas_conflito"
            ).fetchall()
        return {str(linha["etiqueta"]).strip() for linha in linhas if linha["etiqueta"]}

    def importar_fila(
        self,
        tabela: pd.DataFrame,
        arquivos: Iterable[dict[str, str]],
        *,
        log=None,
    ) -> dict[str, object]:
        """Grava a fila e isola etiquetas encontradas em ruas/triagens distintas."""
        agora = datetime.now(timezone.utc).isoformat(timespec="seconds")
        novos = 0
        repetidos = 0
        conflitos: dict[str, set[tuple[str, str, str, str]]] = {}
        shipments_conflito: set[str] = set()
        inseridas_nesta_importacao: dict[str, str] = {}
        arquivos = list(arquivos)
        linhas = tabela.to_dict(orient="records") if not tabela.empty else []

        def registrar_ocorrencia_conflito(
            conexao: sqlite3.Connection,
            dados: dict[str, object],
        ) -> None:
            conexao.execute(
                "INSERT OR IGNORE INTO etiquetas_conflito "
                "(etiqueta, shipment_id, rua, triagem, palete_origem, rota, "
                "destino, arquivo_origem, detectado_em) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _texto(dados.get("etiqueta")),
                    _texto(dados.get("shipment_id")),
                    _texto(dados.get("rua")),
                    _texto(dados.get("triagem")),
                    _texto(dados.get("palete_origem")),
                    _texto(dados.get("rota")),
                    _texto(dados.get("destino")),
                    _texto(dados.get("arquivo_origem")),
                    agora,
                ),
            )

        def registrar_conflito(
            conexao: sqlite3.Connection,
            etiqueta: str,
            dados: dict[str, object],
        ) -> None:
            registrar_ocorrencia_conflito(conexao, dados)
            shipment_id = _texto(dados.get("shipment_id"))
            if shipment_id:
                shipments_conflito.add(shipment_id)
            conflitos.setdefault(etiqueta, set()).add(
                (
                    _texto(dados.get("rua")),
                    _texto(dados.get("triagem")),
                    _texto(dados.get("palete_origem")),
                    _texto(dados.get("arquivo_origem")),
                )
            )

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
                            "SELECT * FROM etiquetas "
                            "WHERE etiqueta = ?",
                            (etiqueta,),
                        ).fetchone()
                        if existente:
                            conflito_de_local = (
                                existente["estado"] == "ABERTO"
                                and (
                                    (
                                        existente["rua"]
                                        and dados["rua"]
                                        and _chave(existente["rua"])
                                        != _chave(dados["rua"])
                                    )
                                    or (
                                        existente["triagem"]
                                        and dados["triagem"]
                                        and _chave(existente["triagem"])
                                        != _chave(dados["triagem"])
                                    )
                                )
                            )
                            if conflito_de_local:
                                dados_existentes = dict(existente)
                                conexao.execute(
                                    "UPDATE etiquetas SET estado = 'CONFLITO' "
                                    "WHERE etiqueta = ? AND estado = 'ABERTO'",
                                    (etiqueta,),
                                )
                                if etiqueta in inseridas_nesta_importacao:
                                    inseridas_nesta_importacao.pop(etiqueta)
                                registrar_conflito(
                                    conexao,
                                    etiqueta,
                                    dados_existentes,
                                )
                                registrar_conflito(conexao, etiqueta, dados)
                                continue
                            if existente["estado"] == "CONFLITO":
                                shipment_existente = _texto(
                                    existente["shipment_id"]
                                )
                                shipment_atual = _texto(dados["shipment_id"])
                                if shipment_existente:
                                    shipments_conflito.add(shipment_existente)
                                if shipment_atual:
                                    shipments_conflito.add(shipment_atual)
                                ja_eh_etiqueta_conflitante = conexao.execute(
                                    "SELECT 1 FROM etiquetas_conflito "
                                    "WHERE etiqueta = ? LIMIT 1",
                                    (etiqueta,),
                                ).fetchone()
                                if ja_eh_etiqueta_conflitante:
                                    registrar_conflito(
                                        conexao,
                                        etiqueta,
                                        dict(existente),
                                    )
                                    registrar_conflito(conexao, etiqueta, dados)
                                continue
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
                        inseridas_nesta_importacao[etiqueta] = dados["shipment_id"]

                etiquetas_dos_shipments_isolados: set[str] = set()
                for shipment_id in shipments_conflito:
                    linhas_pedido = conexao.execute(
                        "SELECT etiqueta FROM etiquetas "
                        "WHERE shipment_id = ? AND estado = 'ABERTO'",
                        (shipment_id,),
                    ).fetchall()
                    etiquetas_dos_shipments_isolados.update(
                        str(linha["etiqueta"]) for linha in linhas_pedido
                    )
                    conexao.execute(
                        "UPDATE etiquetas SET estado = 'CONFLITO' "
                        "WHERE shipment_id = ? AND estado = 'ABERTO'",
                        (shipment_id,),
                    )
                etiquetas_novas_ativas = sum(
                    1
                    for etiqueta, shipment_id in inseridas_nesta_importacao.items()
                    if shipment_id not in shipments_conflito
                    and etiqueta not in etiquetas_dos_shipments_isolados
                )
                novos = etiquetas_novas_ativas

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
                f"{len(arquivos)} arquivo(s) registrado(s), "
                f"{len(conflitos)} etiqueta(s) isolada(s) por divergência de "
                f"rua/triagem; {len(shipments_conflito)} pedido(s) isolado(s)."
            )
            for etiqueta, ocorrencias in sorted(conflitos.items()):
                locais = "; ".join(
                    f"Rua {rua or 'não identificada'} / triagem "
                    f"{triagem or 'não identificada'} / palete {palete or 'não identificado'}"
                    for rua, triagem, palete, _ in sorted(ocorrencias)
                )
                log(f"Etiqueta {etiqueta} isolada: encontrada em {locais}.")
        return {
            "novas": novos,
            "repetidas": repetidos,
            "conflitos": len(conflitos),
            "etiquetas_conflito": sorted(conflitos),
            "pedidos_isolados": len(shipments_conflito),
            "shipments_isolados": sorted(shipments_conflito),
        }

    def carregar_etiquetas_conflito(self) -> pd.DataFrame:
        """Lista etiquetas isoladas até revisão manual de rua/triagem."""
        with closing(self._conectar()) as conexao:
            linhas = conexao.execute(
                "SELECT etiqueta AS Etiqueta, shipment_id AS ShipmentId, "
                "rua AS Rua, triagem AS Triagem, "
                "palete_origem AS 'Palete de origem', rota AS Rota, "
                "destino AS Destino, detectado_em AS 'Detectado em', "
                "(SELECT e.status FROM etiquetas e "
                "WHERE e.etiqueta = etiquetas_conflito.etiqueta) AS Status "
                "FROM etiquetas_conflito "
                "ORDER BY etiqueta, rua, triagem, palete_origem"
            ).fetchall()
        return pd.DataFrame([dict(linha) for linha in linhas])

    def resolver_conflitos_para_rua(
        self,
        rua: str,
        shipment_ids: Iterable[str],
    ) -> dict[str, int]:
        """Reabre pedidos isolados, atribuindo todas as etiquetas a uma rua."""
        rua_destino = _texto(rua)
        shipments = list(dict.fromkeys(_texto(item) for item in shipment_ids))
        shipments = [shipment for shipment in shipments if shipment]
        if not rua_destino:
            raise ValueError("Selecione a rua de destino dos pedidos isolados.")
        if not shipments:
            raise ValueError("Selecione ao menos um pedido isolado para mover.")

        placeholders = ", ".join("?" for _ in shipments)
        with closing(self._conectar()) as conexao:
            conexao.execute("BEGIN IMMEDIATE")
            try:
                ocorrencias = conexao.execute(
                    f"SELECT shipment_id, rua FROM etiquetas_conflito "
                    f"WHERE shipment_id IN ({placeholders})",
                    shipments,
                ).fetchall()
                ruas_por_pedido: dict[str, set[str]] = {}
                for ocorrencia in ocorrencias:
                    ruas_por_pedido.setdefault(
                        _texto(ocorrencia["shipment_id"]), set()
                    ).add(_chave(ocorrencia["rua"]))
                invalidos = [
                    shipment
                    for shipment in shipments
                    if _chave(rua_destino)
                    not in ruas_por_pedido.get(shipment, set())
                ]
                if invalidos:
                    raise ValueError(
                        "A rua escolhida não está entre as ruas em que o pedido foi "
                        f"bipado: {', '.join(invalidos[:10])}."
                    )
                pedidos_reabertos = conexao.execute(
                    f"SELECT COUNT(DISTINCT shipment_id) AS total "
                    f"FROM etiquetas WHERE estado = 'CONFLITO' "
                    f"AND shipment_id IN ({placeholders})",
                    shipments,
                ).fetchone()["total"]
                cursor = conexao.execute(
                    f"UPDATE etiquetas SET rua = ?, estado = 'ABERTO' "
                    f"WHERE estado = 'CONFLITO' AND shipment_id IN ({placeholders})",
                    [rua_destino, *shipments],
                )
                etiquetas_reabertas = max(cursor.rowcount, 0)
                if not etiquetas_reabertas:
                    raise ValueError(
                        "Os pedidos selecionados não têm etiquetas isoladas para mover."
                    )
                conexao.execute(
                    f"DELETE FROM etiquetas_conflito "
                    f"WHERE shipment_id IN ({placeholders})",
                    shipments,
                )
                conexao.commit()
            except Exception:
                conexao.rollback()
                raise

        return {
            "pedidos": int(pedidos_reabertos),
            "etiquetas": etiquetas_reabertas,
        }

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


def _nome_normalizado(valor: str) -> str:
    nome = PurePosixPath(str(valor).replace("\\", "/")).name
    normalizado = unicodedata.normalize("NFKD", nome)
    return "".join(
        caractere
        for caractere in normalizado
        if not unicodedata.combining(caractere)
    ).casefold()


def _chave_nome_conteudo(nome: str, digest: str) -> str:
    return sha256(nome.encode("utf-8") + b"\0" + digest.encode("ascii")).hexdigest()
