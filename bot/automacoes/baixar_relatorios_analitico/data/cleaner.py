from pathlib import Path

import pandas as pd

from ..utils.config import BASES_DIARIAS_PATH


ID_USER_ALVO = "129948"
REMETENTE_EXCLUIR = "L OREAL BRASIL COMERCIAL DE COSMETICOS LTDA"
STATUS_EXCLUIR = "TRAVADO"
PONTO_ATUAL_EXCLUIR = "TC EMISSAO TECA"
PONTO_FINAL_EXCLUIR = "TC EMISSAO TECA"


BASES_DIARIAS = Path(BASES_DIARIAS_PATH)
BASES_DIARIAS.mkdir(parents=True, exist_ok=True)


def _normalizar_texto(serie):
    return (
        serie
        .fillna("")
        .astype(str)
        .str.strip()
        .str.replace(r"\s+", " ", regex=True)
        .str.upper()
    )


def _normalizar_id_user(serie):
    return (
        serie
        .fillna("")
        .astype(str)
        .str.strip()
        .str.replace(r"\.0$", "", regex=True)
    )


def _localizar_coluna(df, nome_esperado, arquivo):
    mapa = {
        str(coluna).strip().upper(): coluna
        for coluna in df.columns
    }

    chave = nome_esperado.strip().upper()

    if chave not in mapa:
        raise ValueError(
            f"A coluna '{nome_esperado}' não foi encontrada em "
            f"'{arquivo.name}'."
        )

    return mapa[chave]


def _ler_excel(arquivo):
    # openpyxl é mantido para não mudar o comportamento das planilhas
    # que já estão sendo geradas pelo sistema.
    return pd.read_excel(
        arquivo,
        engine="openpyxl",
    )


def filtrar_dataframe(df, arquivo):
    """
    Aplica as regras na ordem definida:

    1. Mantém somente ID_USER = 129948.
    2. Remove REMETENTE = L OREAL BRASIL COMERCIAL DE COSMETICOS LTDA.
    3. Remove somente quando STATUS = TRAVADO e
       PONTO_ATUAL = TC EMISSAO TECA simultaneamente.
    """
    coluna_id = _localizar_coluna(
        df, "ID_USER", arquivo
    )
    coluna_remetente = _localizar_coluna(
        df, "REMETENTE", arquivo
    )
    coluna_status = _localizar_coluna(
        df, "STATUS", arquivo
    )
    coluna_ponto = _localizar_coluna(
        df, "PONTO_ATUAL", arquivo
    )
    coluna_unidade = _localizar_coluna(
        df, "UNIDADE_DESTINO", arquivo
    )

    # 1. Primeiro deixa apenas o ID_USER desejado.
    id_user = _normalizar_id_user(
        df[coluna_id]
    )

    mascara_id = id_user.eq(ID_USER_ALVO)

    removidas_id_user = int((~mascara_id).sum())
    df = df.loc[mascara_id].copy()

    # 2. Depois remove o remetente específico.
    remetente = _normalizar_texto(
        df[coluna_remetente]
    )

    mascara_remetente = remetente.eq(
        REMETENTE_EXCLUIR
    )

    removidas_remetente = int(
        mascara_remetente.sum()
    )

    df = df.loc[~mascara_remetente].copy()

    # 3. Por fim remove somente a combinação das duas condições.
    status = _normalizar_texto(
        df[coluna_status]
    )

    ponto_atual = _normalizar_texto(
        df[coluna_ponto]
    )

    ponto_final = _normalizar_texto(
        df[coluna_unidade]
    )

    mascara_status_ponto = (
        status.eq(STATUS_EXCLUIR)
        & ponto_atual.eq(PONTO_ATUAL_EXCLUIR)
        & ponto_final.eq(PONTO_FINAL_EXCLUIR)
    )

    removidas_status_ponto = int(
        mascara_status_ponto.sum()
    )

    df = df.loc[
        ~mascara_status_ponto
    ].copy()

    # ======================================================
    # 4. REMOVER LINHAS DUPLICADAS
    # ======================================================

    antes_duplicadas = len(df)

    df = df.drop_duplicates().copy()

    removidas_duplicadas = (
        antes_duplicadas - len(df)
    )

    return df, {
        "removidas_id_user": removidas_id_user,
        "removidas_remetente": removidas_remetente,
        "removidas_status_ponto": removidas_status_ponto,
        "removidas_duplicadas": removidas_duplicadas,
        "linhas_finais": len(df),
    }


def limpar_base(
    arquivo_original,
    arquivo_filtrado=None,
):
    """Lê uma base bruta, aplica a limpeza e salva uma base diária."""
    arquivo_original = Path(arquivo_original)

    if not arquivo_original.exists():
        raise FileNotFoundError(
            f"Arquivo original não encontrado: {arquivo_original}"
        )

    if arquivo_filtrado is None:
        arquivo_filtrado = (
            BASES_DIARIAS / arquivo_original.name
        )
    else:
        arquivo_filtrado = Path(arquivo_filtrado)

    arquivo_filtrado.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    df = _ler_excel(arquivo_original)
    linhas_originais = len(df)

    df_limpo, resumo = filtrar_dataframe(
        df,
        arquivo_original,
    )

    # Mantém o cabeçalho uma única vez nesta base diária.
    df_limpo.to_excel(
        arquivo_filtrado,
        index=False,
        engine="openpyxl",
    )

    resultado = {
        "arquivo_original": str(arquivo_original),
        "arquivo_filtrado": str(arquivo_filtrado),
        "linhas_originais": linhas_originais,
        "removidas_id_user": resumo["removidas_id_user"],
        "removidas_remetente": resumo["removidas_remetente"],
        "removidas_status_ponto": resumo["removidas_status_ponto"],
        "linhas_finais": resumo["linhas_finais"],
        "linhas_removidas_total": (
            resumo["removidas_id_user"]
            + resumo["removidas_remetente"]
            + resumo["removidas_status_ponto"]
        ),
    }

    del df
    del df_limpo

    return resultado
