from datetime import datetime
from pathlib import Path

import pandas as pd
import xlsxwriter

from ..utils.config import RESULTADOS_PATH


RESULTADOS = Path(RESULTADOS_PATH)
RESULTADOS.mkdir(parents=True, exist_ok=True)


def _valor_excel(valor):
    if pd.isna(valor):
        return None

    if isinstance(valor, pd.Timestamp):
        return valor.to_pydatetime()

    return valor


def unificar_bases(
    arquivos,
    data_inicial,
    data_final,
):
    """
    Unifica somente as bases diárias recebidas.

    O processamento é feito uma planilha por vez e a saída é escrita
    linha por linha com XlsxWriter em constant_memory, evitando manter
    todas as bases em um único DataFrame.
    """
    caminhos = [Path(arquivo) for arquivo in arquivos]

    if not caminhos:
        raise ValueError(
            "Nenhuma base diária foi fornecida para unificação."
        )

    agora = datetime.now()

    nome_final = (
        "Analítico_"
        f"{data_inicial.strftime('%d-%m-%Y')}_a_"
        f"{data_final.strftime('%d-%m-%Y')}_"
        "Extraido_"
        f"{agora.strftime('%d-%m-%Y_%H-%M-%S')}.xlsx"
    )

    destino = RESULTADOS / nome_final
    temporario = RESULTADOS / f".{nome_final}.tmp.xlsx"

    workbook = None
    worksheet = None
    linha_atual = 0
    primeira_planilha = True
    colunas_referencia = None

    estatisticas = {
        "arquivos": 0,
        "linhas_finais": 0,
    }

    try:
        workbook = xlsxwriter.Workbook(
            temporario,
            {
                "constant_memory": True,
            },
        )

        worksheet = workbook.add_worksheet("Analítico")

        for caminho in caminhos:
            if not caminho.exists():
                raise FileNotFoundError(
                    f"Base diária não encontrada: {caminho}"
                )

            print(f"Unificando {caminho.name}...")

            df = pd.read_excel(
                caminho,
                engine="openpyxl",
            )

            colunas = list(df.columns)

            if colunas_referencia is None:
                colunas_referencia = colunas
            elif colunas != colunas_referencia:
                raise ValueError(
                    "O layout da base "
                    f"'{caminho.name}' é diferente do primeiro arquivo."
                )

            if primeira_planilha:
                for coluna, nome in enumerate(colunas):
                    worksheet.write(
                        linha_atual,
                        coluna,
                        nome,
                    )

                linha_atual += 1
                primeira_planilha = False

            for valores in df.itertuples(
                index=False,
                name=None,
            ):
                for coluna, valor in enumerate(valores):
                    worksheet.write(
                        linha_atual,
                        coluna,
                        _valor_excel(valor),
                    )

                linha_atual += 1
                estatisticas["linhas_finais"] += 1

            estatisticas["arquivos"] += 1

            del df

        workbook.close()
        workbook = None

        temporario.replace(destino)

    except Exception:
        if workbook is not None:
            try:
                workbook.close()
            except Exception:
                pass

        if temporario.exists():
            temporario.unlink()

        raise

    return {
        "arquivo_final": str(destino),
        **estatisticas,
    }
