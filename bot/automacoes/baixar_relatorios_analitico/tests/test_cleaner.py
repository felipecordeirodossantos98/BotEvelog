import pandas as pd

from data.cleaner import filtrar_dataframe


class ArquivoFake:
    name = "Analítico_16-07-2026.xlsx"


def test_filtro_exato():
    df = pd.DataFrame(
        [
            # ID diferente: sai na primeira etapa.
            {
                "ID_USER": 111111,
                "REMETENTE": "OUTRO",
                "STATUS": "NORMAL",
                "PONTO_ATUAL": "OUTRO",
            },
            # ID correto, remetente proibido: sai na segunda etapa.
            {
                "ID_USER": 129948,
                "REMETENTE": "L OREAL BRASIL COMERCIAL DE COSMETICOS LTDA",
                "STATUS": "NORMAL",
                "PONTO_ATUAL": "OUTRO",
            },
            # ID correto + combinação: sai na terceira etapa.
            {
                "ID_USER": 129948,
                "REMETENTE": "OUTRO",
                "STATUS": "TRAVADO",
                "PONTO_ATUAL": "TC EMISSAO TECA",
            },
            # ID correto e não enquadrado: permanece.
            {
                "ID_USER": 129948,
                "REMETENTE": "OUTRO",
                "STATUS": "TRAVADO",
                "PONTO_ATUAL": "OUTRO",
            },
        ]
    )

    filtrado, resumo = filtrar_dataframe(
        df,
        ArquivoFake(),
    )

    assert len(filtrado) == 1
    assert filtrado.iloc[0]["ID_USER"] == 129948
    assert filtrado.iloc[0]["PONTO_ATUAL"] == "OUTRO"

    assert resumo["removidas_id_user"] == 1
    assert resumo["removidas_remetente"] == 1
    assert resumo["removidas_status_ponto"] == 1
    assert resumo["linhas_finais"] == 1
