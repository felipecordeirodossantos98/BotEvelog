from __future__ import annotations

from collections import defaultdict, deque
from copy import copy
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path
import re
import shutil
import unicodedata
import zipfile
from xml.etree import ElementTree as ET
from xml.etree.ElementTree import ParseError

import pandas as pd
from openpyxl import load_workbook
from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from automacoes.baixar_relatorios_performance import (
    LIMITE_POR_CONSULTA as LIMITE_POR_CONSULTA_PERFORMANCE,
    fazer_login as fazer_login_performance,
    processar_lote as processar_lote_performance,
    validar_env as validar_env_performance,
)
from servicos.fraction import (
    baixar_relatorios_descritivos_correntista_ate_data_fraction,
    executar_com_retentativas_fraction,
    login_fraction_mega_cartela,
    pesquisar_e_capturar_peso_taxado_fraction,
    recuperar_sessao_fraction,
)
from utils.config import HEADLESS


PASTA_RAIZ = Path(__file__).resolve().parents[2]

PASTA_RESULTADOS = (
    PASTA_RAIZ
    / "resultados"
    / "mega_cartela_lsm"
)

ARQUIVO_MEGA_CARTELA = (
    PASTA_RESULTADOS
    / "mega_cartela_lsm.xlsx"
)


# Estrutura da tabela principal de cada aba diária da Mega Cartela.
COLUNAS_DESTINO = [
    "SIGLA",
    "Rastreio",
    "CNPJ",
    "DATA DO PEDIDO",
    "CÓDIGO",
    "QTD UND",
    "QTD MIL",
    "CEP",
    "UF",
    "MANUSEIO",
    "PREVISAO DIAS ÚTEIS",
    "DATA PREVISTA",
    "status",
    "Data da entrega",
]


# Nesta primeira etapa, somente estes campos vêm da planilha importada.
# Os demais serão preenchidos posteriormente pelo fluxo de atualização.
MAPEAMENTO_IMPORTACAO = {
    "SIGLA": "SIGLA",
    "CNPJ": "CNPJ",
    "DATA DO PEDIDO": "DATA DO PEDIDO",
    "CÓDIGO": "CÓDIGO",
    "QTD UND": "QTD UND",
    "CEP": "CEP",
    "UF": "UF",
}


STATUS_RESUMO = [
    "EM PRODUÇÃO",
    "ENTRADA",
    "TRANSFERENCIA",
    "EM ROTA",
    "ENTREGUE",
    "EM TRATATIVA",
    "EXTRAVIO",
]


STATUS_FINALIZADORES = {
    "ENTREGUE",
    "EXTRAVIO",
}

PASTA_UPDATE_TEMP = (
    PASTA_RESULTADOS
    / ".update"
)

PASTA_FOLHA_APOIO_TEMP = (
    PASTA_RESULTADOS
    / ".folha_apoio"
)

TENTATIVAS_CONSULTA_UPDATE = 3

# Conversão da coluna CÓDIGO da Mega Cartela para a DIVISÃO
# utilizada no campo Destinatário da Folha de apoio.
CONVERSAO_DIVISAO_FOLHA_APOIO = {
    "SOBRE_APP": "SOBREMESA",
    "SOBREVERAO": "SOBREMESA",
    "CAFE_26": "CAFE",
    "SAND_APP": "SANDUICHES",
    "DRIVE_APP": "DRIVE",
    "MULTI_APP": "MULTI-APP",
}


XML_INVALIDO_BYTES = re.compile(
    rb"[\x00-\x08\x0B\x0C\x0E-\x1F]"
)

XML_INVALIDO_TEXTO = re.compile(
    r"[\x00-\x08\x0B\x0C\x0E-\x1F\uFFFE\uFFFF]"
)

XML_REFERENCIA_HEX = re.compile(
    r"&#x([0-9A-Fa-f]+);"
)

XML_REFERENCIA_DEC = re.compile(
    r"&#([0-9]+);"
)

XML_AMPERSAND_SOLTO = re.compile(
    r"&(?!(?:amp|lt|gt|quot|apos);|#\d+;|#x[0-9A-Fa-f]+;)"
)


def _codigo_xml_valido(
    codigo: int,
) -> bool:
    return (
        codigo == 0x09
        or codigo == 0x0A
        or codigo == 0x0D
        or 0x20 <= codigo <= 0xD7FF
        or 0xE000 <= codigo <= 0xFFFD
        or 0x10000 <= codigo <= 0x10FFFF
    )


def _sanitizar_referencia_hex(
    resultado,
) -> str:
    codigo = int(
        resultado.group(1),
        16,
    )

    if _codigo_xml_valido(
        codigo
    ):
        return resultado.group(0)

    return ""


def _sanitizar_referencia_dec(
    resultado,
) -> str:
    codigo = int(
        resultado.group(1),
        10,
    )

    if _codigo_xml_valido(
        codigo
    ):
        return resultado.group(0)

    return ""


def _indice_xml_por_linha_coluna(
    texto_xml: str,
    linha: int,
    coluna: int,
) -> int | None:
    linhas = texto_xml.splitlines(
        keepends=True
    )

    if (
        linha < 1
        or linha > len(
            linhas
        )
    ):
        return None

    return (
        sum(
            len(item)
            for item in linhas[
                : linha - 1
            ]
        )
        + coluna
    )


def _sanitizar_xml_bytes(
    dados: bytes,
) -> bytes:
    """
    Faz uma recuperação mais forte dos XMLs internos do XLSX.

    Trata:
    - caracteres de controle inválidos;
    - referências numéricas inválidas, como &#x0B; / &#11;;
    - ampersand não escapado;
    - e, como último recurso, remove o caractere exato apontado
      pelo parser quando o erro for "invalid token".
    """
    dados = XML_INVALIDO_BYTES.sub(
        b"",
        dados,
    )

    texto_xml = dados.decode(
        "utf-8",
        errors="replace",
    )

    texto_xml = XML_INVALIDO_TEXTO.sub(
        "",
        texto_xml,
    )

    texto_xml = XML_REFERENCIA_HEX.sub(
        _sanitizar_referencia_hex,
        texto_xml,
    )

    texto_xml = XML_REFERENCIA_DEC.sub(
        _sanitizar_referencia_dec,
        texto_xml,
    )

    texto_xml = XML_AMPERSAND_SOLTO.sub(
        "&amp;",
        texto_xml,
    )

    # Valida. Se ainda houver um "invalid token", remove somente
    # o caractere no ponto indicado e tenta novamente.
    for _ in range(
        50
    ):
        try:
            ET.fromstring(
                texto_xml
            )
            break

        except ParseError as erro:
            mensagem = str(
                erro
            ).lower()

            if "invalid token" not in mensagem:
                raise

            linha, coluna = (
                erro.position
            )

            indice = (
                _indice_xml_por_linha_coluna(
                    texto_xml,
                    linha,
                    coluna,
                )
            )

            if (
                indice is None
                or indice >= len(
                    texto_xml
                )
            ):
                raise

            caractere = texto_xml[
                indice
            ]

            if caractere == "&":
                texto_xml = (
                    texto_xml[:indice]
                    + "&amp;"
                    + texto_xml[
                        indice + 1:
                    ]
                )

            elif caractere == "<":
                texto_xml = (
                    texto_xml[:indice]
                    + "&lt;"
                    + texto_xml[
                        indice + 1:
                    ]
                )

            else:
                texto_xml = (
                    texto_xml[:indice]
                    + texto_xml[
                        indice + 1:
                    ]
                )

    else:
        raise RuntimeError(
            "O XML interno continuou inválido após as tentativas "
            "automáticas de recuperação."
        )

    return texto_xml.encode(
        "utf-8"
    )


def _erro_xml_invalido(
    erro: Exception,
) -> bool:
    mensagem = str(
        erro
    ).lower()

    return (
        isinstance(
            erro,
            ParseError,
        )
        or "not well-formed" in mensagem
        or "invalid token" in mensagem
    )


def _criar_copia_xlsx_sanitizada(
    caminho_origem: Path,
) -> Path:
    """
    Cria uma cópia temporária do XLSX removendo apenas caracteres
    de controle que não são válidos em XML 1.0.

    A cópia fica dentro da pasta oculta .update e é apagada
    assim que deixa de ser necessária.
    """
    PASTA_UPDATE_TEMP.mkdir(
        parents=True,
        exist_ok=True,
    )

    agora = datetime.now().strftime(
        "%Y%m%d_%H%M%S_%f"
    )

    caminho_destino = (
        PASTA_UPDATE_TEMP
        / (
            ".mega_cartela_lsm_"
            f"sanitizada_{agora}.xlsx"
        )
    )

    with zipfile.ZipFile(
        caminho_origem,
        "r",
    ) as zip_origem:
        with zipfile.ZipFile(
            caminho_destino,
            "w",
        ) as zip_destino:
            for item in zip_origem.infolist():
                dados = zip_origem.read(
                    item.filename
                )

                if (
                    item.filename.endswith(
                        ".xml"
                    )
                    or item.filename.endswith(
                        ".rels"
                    )
                ):
                    try:
                        ET.fromstring(
                            dados
                        )
                    except ParseError:
                        # Só altera o XML que estiver realmente inválido.
                        # XMLs válidos (inclusive estilos/tema) são copiados
                        # byte a byte sem qualquer modificação.
                        dados = _sanitizar_xml_bytes(
                            dados
                        )

                zip_destino.writestr(
                    item,
                    dados,
                )

    return caminho_destino


def _abrir_mega_cartela_seguro(
    caminho_cartela: Path,
    **kwargs,
):
    """
    Tenta abrir a Mega Cartela normalmente.

    Se o XLSX possuir um caractere inválido em algum XML interno,
    cria uma cópia temporária sanitizada e tenta novamente.

    Retorna:
        workbook, caminho_temporario_ou_none, foi_sanitizado
    """
    try:
        workbook = load_workbook(
            caminho_cartela,
            **kwargs,
        )

        return (
            workbook,
            None,
            False,
        )

    except Exception as erro:
        if not _erro_xml_invalido(
            erro
        ):
            raise

    caminho_sanitizado = (
        _criar_copia_xlsx_sanitizada(
            caminho_cartela
        )
    )

    try:
        workbook = load_workbook(
            caminho_sanitizado,
            **kwargs,
        )

    except Exception as erro_sanitizado:
        caminho_sanitizado.unlink(
            missing_ok=True
        )

        raise RuntimeError(
            "A Mega Cartela possui um XML interno inválido "
            "e não foi possível recuperá-lo automaticamente. "
            f"Detalhe: {erro_sanitizado}"
        ) from erro_sanitizado

    return (
        workbook,
        caminho_sanitizado,
        True,
    )


def _apagar_temporario_xlsx(
    caminho_temporario,
) -> None:
    if caminho_temporario is None:
        return

    try:
        Path(
            caminho_temporario
        ).unlink(
            missing_ok=True
        )
    except Exception:
        pass


def normalizar_texto(
    valor,
) -> str:
    if valor is None:
        return ""

    texto = unicodedata.normalize(
        "NFKD",
        str(valor).strip(),
    )

    texto = "".join(
        caractere
        for caractere in texto
        if not unicodedata.combining(
            caractere
        )
    )

    return " ".join(
        texto.upper().split()
    )


def localizar_mega_cartela() -> Path:
    """
    A planilha principal possui nome fixo:

        resultados/mega_cartela_lsm/mega_cartela_lsm.xlsx
    """
    PASTA_RESULTADOS.mkdir(
        parents=True,
        exist_ok=True,
    )

    if not ARQUIVO_MEGA_CARTELA.exists():
        raise FileNotFoundError(
            "A Mega Cartela principal não foi encontrada. "
            "Coloque o arquivo em "
            f"{ARQUIVO_MEGA_CARTELA}."
        )

    return ARQUIVO_MEGA_CARTELA


def extrair_nome_aba(
    nome_arquivo: str,
) -> str:
    """
    Extrai DD-MM do nome da planilha importada.

    Exemplos:
        18.09 - Evelog - CARTELAS LSM.xlsx -> 18-09
        18-09 - Evelog.xlsx                -> 18-09
        18_09_2026.xlsx                    -> 18-09
    """
    nome = Path(
        nome_arquivo
    ).stem

    resultado = re.search(
        r"(?<!\d)"
        r"(\d{1,2})"
        r"[.\-_]"
        r"(\d{1,2})"
        r"(?:[.\-_](\d{2,4}))?"
        r"(?!\d)",
        nome,
    )

    if not resultado:
        raise ValueError(
            "Não foi possível identificar a data da solicitação "
            "no nome do arquivo. O nome precisa conter DD.MM "
            "ou DD-MM."
        )

    dia = int(
        resultado.group(1)
    )
    mes = int(
        resultado.group(2)
    )

    if not 1 <= dia <= 31:
        raise ValueError(
            f"Dia inválido no nome do arquivo: {dia}."
        )

    if not 1 <= mes <= 12:
        raise ValueError(
            f"Mês inválido no nome do arquivo: {mes}."
        )

    return f"{dia:02d}-{mes:02d}"


def _mapa_cabecalho_linha(
    worksheet,
    linha: int,
) -> dict[str, int]:
    mapa = {}

    for coluna in range(
        1,
        worksheet.max_column + 1,
    ):
        nome = normalizar_texto(
            worksheet.cell(
                row=linha,
                column=coluna,
            ).value
        )

        if nome:
            mapa[nome] = coluna

    return mapa


def _localizar_linha_cabecalho_destino(
    worksheet,
) -> int | None:
    """
    Localiza a tabela pela coluna B.

    Regra:
      - percorre a coluna B de cima para baixo;
      - ao encontrar "Rastreio", aquela linha é o cabeçalho da tabela.
    """
    for linha in range(
        1,
        worksheet.max_row + 1,
    ):
        valor = normalizar_texto(
            worksheet.cell(
                row=linha,
                column=2,
            ).value
        )

        if valor == "RASTREIO":
            return linha

    return None


def _data_titulo_aba(
    titulo: str,
):
    """
    Extrai DD-MM do início/nome da aba quando existir.
    Exemplos aceitos:
        28-08
        26-06 DRIVEAPP
         26-06 SOBREAPP
    """
    resultado = re.search(
        r"(\d{1,2})-(\d{1,2})",
        str(titulo),
    )

    if not resultado:
        return None

    dia = int(
        resultado.group(1)
    )
    mes = int(
        resultado.group(2)
    )

    if not (
        1 <= dia <= 31
        and 1 <= mes <= 12
    ):
        return None

    return (
        mes,
        dia,
    )


def _data_completa_aba(
    titulo: str,
    referencia: date | None = None,
) -> date | None:
    """
    Converte a data DD-MM do nome da aba em uma data completa.

    Como as abas não possuem ano no nome, usa o ano da referência.
    Se a aba parecer estar no futuro em relação à referência, considera
    que ela pertence ao ano anterior (ex.: janeiro -> dezembro).
    """
    partes = _data_titulo_aba(
        titulo
    )

    if partes is None:
        return None

    if referencia is None:
        referencia = datetime.now().date()

    mes, dia = partes
    ano = referencia.year

    if (mes, dia) > (
        referencia.month,
        referencia.day,
    ):
        ano -= 1

    try:
        return date(
            ano,
            mes,
            dia,
        )
    except ValueError:
        return None


def _localizar_aba_modelo(
    workbook,
    nome_nova_aba: str,
):
    """
    Usa uma aba já existente da Mega Cartela como modelo visual.

    A escolha prioriza a aba compatível com data mais próxima ANTERIOR
    à nova solicitação. Ex.: para 18-09, a aba 28-08 é preferida.

    Isso preserva a estrutura visual mais recente:
    - tabela STATUS do topo;
    - cores;
    - bordas;
    - tamanhos;
    - cabeçalho;
    - larguras de colunas.

    Nenhuma fórmula do modelo será mantida na nova aba.
    """
    data_nova = _data_titulo_aba(
        nome_nova_aba
    )

    candidatas = []

    for indice, worksheet in enumerate(
        workbook.worksheets
    ):
        linha_cabecalho = (
            _localizar_linha_cabecalho_destino(
                worksheet
            )
        )

        if linha_cabecalho is None:
            continue

        data_aba = _data_titulo_aba(
            worksheet.title
        )

        candidatas.append(
            (
                indice,
                data_aba,
                worksheet,
                linha_cabecalho,
            )
        )

    if not candidatas:
        raise ValueError(
            "Não foi encontrada na Mega Cartela nenhuma aba "
            "com a estrutura esperada para servir de modelo."
        )

    # Se conseguirmos interpretar a data da nova aba, usa a aba
    # cronologicamente mais próxima anterior a ela.
    if data_nova is not None:
        anteriores = [
            item
            for item in candidatas
            if (
                item[1] is not None
                and item[1] < data_nova
            )
        ]

        if anteriores:
            anteriores.sort(
                key=lambda item: (
                    item[1],
                    item[0],
                ),
                reverse=True,
            )

            _, _, worksheet, linha_cabecalho = (
                anteriores[0]
            )

            return (
                worksheet,
                linha_cabecalho,
            )

    # Fallback: última aba compatível no workbook.
    _, _, worksheet, linha_cabecalho = (
        candidatas[-1]
    )

    return (
        worksheet,
        linha_cabecalho,
    )


def _localizar_aba_origem(
    workbook,
):
    """
    Localiza a planilha da nova solicitação pelo nome das colunas.

    A origem pode ter outras colunas, como REGIONAL, mas basta possuir
    os campos usados nesta primeira etapa.
    """
    obrigatorias = {
        normalizar_texto(
            coluna
        )
        for coluna in MAPEAMENTO_IMPORTACAO.values()
    }

    candidatas = []

    for worksheet in workbook.worksheets:
        mapa = _mapa_cabecalho_linha(
            worksheet,
            1,
        )

        if not obrigatorias.issubset(
            set(
                mapa
            )
        ):
            continue

        quantidade = 0

        coluna_sigla = mapa[
            normalizar_texto(
                "SIGLA"
            )
        ]

        for linha in range(
            2,
            worksheet.max_row + 1,
        ):
            valor = worksheet.cell(
                row=linha,
                column=coluna_sigla,
            ).value

            if (
                valor is not None
                and str(valor).strip()
            ):
                quantidade += 1

        candidatas.append(
            (
                quantidade,
                worksheet,
            )
        )

    if not candidatas:
        raise ValueError(
            "Não foi encontrada uma aba de solicitação com as colunas: "
            + ", ".join(
                MAPEAMENTO_IMPORTACAO.values()
            )
        )

    candidatas.sort(
        key=lambda item: item[0],
        reverse=True,
    )

    return candidatas[0][1]


def _extrair_dados_origem(
    worksheet,
) -> list[dict]:
    """
    Lê a planilha importada por NOME DE COLUNA.

    O workbook de origem é aberto com data_only=True, então nenhuma
    fórmula é copiada para a Mega Cartela. São utilizados apenas os
    valores calculados salvos no XLSX.
    """
    mapa = _mapa_cabecalho_linha(
        worksheet,
        1,
    )

    linhas = []

    coluna_sigla = mapa[
        normalizar_texto(
            "SIGLA"
        )
    ]

    for numero_linha in range(
        2,
        worksheet.max_row + 1,
    ):
        sigla = worksheet.cell(
            row=numero_linha,
            column=coluna_sigla,
        ).value

        if (
            sigla is None
            or not str(sigla).strip()
        ):
            continue

        item = {}

        for coluna_destino in COLUNAS_DESTINO:
            coluna_origem = (
                MAPEAMENTO_IMPORTACAO.get(
                    coluna_destino
                )
            )

            if coluna_origem is None:
                # Campos preenchidos somente no processo de UPDATE.
                item[
                    coluna_destino
                ] = None
                continue

            indice_origem = mapa.get(
                normalizar_texto(
                    coluna_origem
                )
            )

            if indice_origem is None:
                item[
                    coluna_destino
                ] = None
                continue

            item[
                coluna_destino
            ] = worksheet.cell(
                row=numero_linha,
                column=indice_origem,
            ).value

        linhas.append(
            item
        )

    if not linhas:
        raise ValueError(
            "A planilha de solicitação não possui linhas para importar."
        )

    return linhas


def _copiar_estilo_celula(
    origem,
    destino,
) -> None:
    if origem.has_style:
        destino._style = copy(
            origem._style
        )

    destino.font = copy(
        origem.font
    )
    destino.fill = copy(
        origem.fill
    )
    destino.border = copy(
        origem.border
    )
    destino.alignment = copy(
        origem.alignment
    )
    destino.protection = copy(
        origem.protection
    )
    destino.number_format = (
        origem.number_format
    )


def _desmesclar_area_dados(
    worksheet,
    linha_cabecalho: int,
) -> None:
    """
    Algumas abas antigas possuem SIGLAs mescladas verticalmente.
    A nova solicitação precisa de uma linha independente para cada pedido,
    então qualquer mesclagem que toque a área de dados é removida.

    Mesclagens do topo, como STATUS em B2:C2, são preservadas.
    """
    intervalos = list(
        worksheet.merged_cells.ranges
    )

    for intervalo in intervalos:
        if intervalo.max_row > linha_cabecalho:
            worksheet.unmerge_cells(
                str(
                    intervalo
                )
            )


def _corrigir_estilo_cabecalho(
    worksheet,
    linha_cabecalho: int,
) -> None:
    """
    Mantém o cabeçalho A:N com o mesmo estilo visual.

    A coluna A (SIGLA) é usada como referência porque no modelo
    correto todo o cabeçalho da tabela possui o mesmo tema vermelho.

    Isso corrige abas em que J:N acabaram ficando sem o preenchimento
    vermelho, sem alterar larguras das colunas nem os dados.
    """
    celula_modelo = worksheet.cell(
        row=linha_cabecalho,
        column=1,
    )

    for coluna in range(
        1,
        len(COLUNAS_DESTINO) + 1,
    ):
        destino = worksheet.cell(
            row=linha_cabecalho,
            column=coluna,
        )

        valor = destino.value

        _copiar_estilo_celula(
            celula_modelo,
            destino,
        )

        destino.value = valor


def _remover_formulas(
    worksheet,
) -> None:
    """
    A nova aba não deve possuir fórmulas nesta primeira etapa.
    """
    for linha in worksheet.iter_rows():
        for celula in linha:
            valor = celula.value

            if (
                celula.data_type == "f"
                or (
                    isinstance(
                        valor,
                        str,
                    )
                    and valor.startswith("=")
                )
            ):
                celula.value = None


def _zerar_resumo_status(
    worksheet,
    linha_cabecalho: int,
) -> None:
    """
    Mantém a tabela STATUS do topo, mas sem fórmula.

    As quantidades ficam em zero e serão recalculadas pelo futuro
    processo de atualização de dados.
    """
    status_normalizados = {
        normalizar_texto(
            status
        )
        for status in STATUS_RESUMO
    }

    for linha in range(
        1,
        linha_cabecalho,
    ):
        valor = normalizar_texto(
            worksheet.cell(
                row=linha,
                column=2,
            ).value
        )

        if valor in status_normalizados:
            worksheet.cell(
                row=linha,
                column=3,
                value=0,
            )


def _limpar_tabela_dados(
    worksheet,
    linha_cabecalho: int,
) -> None:
    """
    Remove os dados antigos da aba-modelo, preservando o layout.
    """
    for linha in range(
        linha_cabecalho + 1,
        worksheet.max_row + 1,
    ):
        for coluna in range(
            1,
            len(COLUNAS_DESTINO) + 1,
        ):
            worksheet.cell(
                row=linha,
                column=coluna,
            ).value = None


def _preencher_tabela(
    worksheet,
    linha_cabecalho: int,
    dados: list[dict],
) -> None:
    """
    Insere os dados na nova aba seguindo EXATAMENTE o nome das colunas.

    Nesta etapa ficam em branco:
    - Rastreio
    - MANUSEIO
    - PREVISAO DIAS ÚTEIS
    - DATA PREVISTA
    - status
    - Data da entrega

    QTD MIL recebe "-" já no momento da criação da aba.
    """
    linha_modelo = (
        linha_cabecalho + 1
    )

    # Garante o cabeçalho exatamente como solicitado.
    for coluna, nome in enumerate(
        COLUNAS_DESTINO,
        start=1,
    ):
        worksheet.cell(
            row=linha_cabecalho,
            column=coluna,
            value=nome,
        )

    for deslocamento, item in enumerate(
        dados,
        start=1,
    ):
        linha_destino = (
            linha_cabecalho
            + deslocamento
        )

        # Para linhas novas além das existentes no modelo,
        # reaproveita o estilo da primeira linha da tabela.
        if linha_destino > worksheet.max_row:
            for coluna in range(
                1,
                len(COLUNAS_DESTINO) + 1,
            ):
                _copiar_estilo_celula(
                    worksheet.cell(
                        row=linha_modelo,
                        column=coluna,
                    ),
                    worksheet.cell(
                        row=linha_destino,
                        column=coluna,
                    ),
                )

        else:
            # Mesmo dentro da área já existente, garante o estilo padrão.
            for coluna in range(
                1,
                len(COLUNAS_DESTINO) + 1,
            ):
                if not worksheet.cell(
                    row=linha_destino,
                    column=coluna,
                ).has_style:
                    _copiar_estilo_celula(
                        worksheet.cell(
                            row=linha_modelo,
                            column=coluna,
                        ),
                        worksheet.cell(
                            row=linha_destino,
                            column=coluna,
                        ),
                    )

        for coluna, nome_coluna in enumerate(
            COLUNAS_DESTINO,
            start=1,
        ):
            valor = item.get(
                nome_coluna
            )

            if nome_coluna == "QTD MIL":
                valor = "-"

            if nome_coluna == "status":
                valor = "EM PRODUÇÃO"

            worksheet.cell(
                row=linha_destino,
                column=coluna,
                value=valor,
            )

    # Formatos úteis sem adicionar qualquer fórmula.
    for linha in range(
        linha_cabecalho + 1,
        linha_cabecalho + len(dados) + 1,
    ):
        # CNPJ
        worksheet.cell(
            row=linha,
            column=3,
        ).number_format = "0"

        # Data do pedido
        worksheet.cell(
            row=linha,
            column=4,
        ).number_format = "dd/mm/yyyy"

        # Quantidade
        worksheet.cell(
            row=linha,
            column=6,
        ).number_format = "#,##0"

        # CEP
        worksheet.cell(
            row=linha,
            column=8,
        ).number_format = "00000000"



def normalizar_rastreio(
    valor,
) -> str:
    if valor is None:
        return ""

    if (
        isinstance(
            valor,
            float,
        )
        and valor.is_integer()
    ):
        return str(
            int(
                valor
            )
        )

    texto = str(
        valor
    ).strip()

    if re.fullmatch(
        r"\d+\.0",
        texto,
    ):
        return texto[:-2]

    return texto


def _converter_data(
    valor,
):
    if valor is None:
        return None

    if isinstance(
        valor,
        (
            datetime,
            date,
        ),
    ):
        return valor

    texto_data = str(
        valor
    ).strip()

    if not texto_data:
        return None

    formatos = [
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%d/%m/%Y",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ]

    for formato in formatos:
        try:
            return datetime.strptime(
                texto_data,
                formato,
            )
        except ValueError:
            pass

    return valor


def _localizar_coluna_dataframe(
    dataframe: pd.DataFrame,
    *nomes: str,
):
    mapa = {
        normalizar_texto(
            coluna
        ): coluna
        for coluna in dataframe.columns
    }

    for nome in nomes:
        coluna = mapa.get(
            normalizar_texto(
                nome
            )
        )

        if coluna is not None:
            return coluna

    return None


def _coletar_linhas_sem_rastreio(
    workbook,
) -> list[dict]:
    """
    Localiza pedidos que ainda não possuem Rastreio na coluna B.

    Para cada aba, a tabela é localizada exatamente pela mesma regra já
    usada no restante da Mega Cartela: procurar ``Rastreio`` na coluna B.
    Somente as linhas de dados abaixo desse cabeçalho são consideradas.

    Para cada candidato são usados:
      A = SIGLA
      D = DATA DO PEDIDO
      E = CÓDIGO

    Os candidatos são devolvidos da aba mais recente para a mais antiga.
    """
    pendentes = []

    for worksheet in workbook.worksheets:
        linha_cabecalho = _localizar_linha_cabecalho_destino(
            worksheet
        )

        if linha_cabecalho is None:
            continue

        data_aba = _data_completa_aba(
            worksheet.title
        )

        for linha in range(
            linha_cabecalho + 1,
            worksheet.max_row + 1,
        ):
            rastreio = normalizar_rastreio(
                worksheet.cell(
                    row=linha,
                    column=2,
                ).value
            )

            if rastreio:
                continue

            sigla = worksheet.cell(
                row=linha,
                column=1,
            ).value

            data_pedido = worksheet.cell(
                row=linha,
                column=4,
            ).value

            codigo = worksheet.cell(
                row=linha,
                column=5,
            ).value

            if (
                sigla is None
                or not str(sigla).strip()
            ):
                continue

            if (
                codigo is None
                or not str(codigo).strip()
            ):
                continue

            pendentes.append(
                {
                    "worksheet": worksheet,
                    "linha": linha,
                    "sigla": str(sigla).strip(),
                    "data_pedido": data_pedido,
                    "codigo": str(codigo).strip(),
                    "aba": worksheet.title,
                    "data_aba": data_aba,
                }
            )

    pendentes.sort(
        key=lambda item: (
            item["data_aba"] is not None,
            item["data_aba"] or date.min,
        ),
        reverse=True,
    )

    return pendentes


def _normalizar_data_busca(
    valor,
):
    """
    Converte datas da Mega Cartela e da Folha de apoio para date.

    Aceita:
      - datetime/date;
      - datas textuais em formatos comuns;
      - números seriais de Excel.
    """
    if valor is None:
        return None

    if isinstance(
        valor,
        datetime,
    ):
        return valor.date()

    if isinstance(
        valor,
        date,
    ):
        return valor

    if isinstance(
        valor,
        (
            int,
            float,
        ),
    ):
        try:
            if pd.isna(valor):
                return None

            return (
                datetime(
                    1899,
                    12,
                    30,
                )
                + timedelta(
                    days=float(valor)
                )
            ).date()

        except Exception:
            pass

    texto = str(
        valor
    ).strip()

    if not texto:
        return None

    timestamp = pd.to_datetime(
        texto,
        dayfirst=True,
        errors="coerce",
    )

    if pd.isna(timestamp):
        return None

    return timestamp.date()


def _normalizar_destinatario_folha(
    valor,
) -> str:
    if valor is None:
        return ""

    texto = normalizar_texto(
        valor
    )

    texto = re.sub(
        r"\s*/\s*",
        "/",
        texto,
    )

    return texto


def _obter_divisao_folha(
    codigo,
) -> str | None:
    chave = normalizar_texto(
        codigo
    ).replace(
        " ",
        "_",
    )

    return CONVERSAO_DIVISAO_FOLHA_APOIO.get(
        chave
    )


def _encontrar_linha_cabecalho_folha_apoio(
    dataframe_bruto: pd.DataFrame,
) -> int:
    """
    A Folha de apoio normalmente possui um título na primeira linha
    e o cabeçalho na segunda. A busca é dinâmica para não depender
    de uma linha fixa.
    """
    limite = min(
        len(dataframe_bruto),
        15,
    )

    for indice in range(
        limite
    ):
        valores = {
            normalizar_texto(
                valor
            )
            for valor in dataframe_bruto.iloc[
                indice
            ].tolist()
            if valor is not None
        }

        possui_cte = (
            "CTE" in valores
            or "CODIGO" in valores
        )

        possui_destinatario = (
            "DESTINATARIO" in valores
        )

        if (
            possui_cte
            and possui_destinatario
        ):
            return indice

    raise ValueError(
        "Não foi possível localizar o cabeçalho "
        "CTE/DESTINATÁRIO na Folha de apoio."
    )


def _ler_folhas_apoio_rastreios(
    arquivos: list[Path],
    log,
) -> dict:
    """
    Lê as Folhas de apoio e monta candidatos por SIGLA/DIVISÃO.

    A data usada para validar o candidato é obrigatoriamente a coluna B
    da Folha de apoio (Data de emissão), conforme a estrutura do relatório:
      B2 = cabeçalho
      B3 em diante = dados.

    Cada entrada mantém a data de emissão para que a validação posterior
    possa exigir: Data de emissão >= data da aba da Mega Cartela.
    """
    por_chave = defaultdict(list)
    total_linhas = 0

    for arquivo in arquivos:
        bruto = pd.read_excel(
            arquivo,
            header=None,
            dtype=object,
        )

        linha_cabecalho = (
            _encontrar_linha_cabecalho_folha_apoio(
                bruto
            )
        )

        dataframe = pd.read_excel(
            arquivo,
            header=linha_cabecalho,
            dtype=object,
        )

        coluna_cte = _localizar_coluna_dataframe(
            dataframe,
            "CTE",
            "Código",
            "Codigo",
        )

        coluna_destinatario = _localizar_coluna_dataframe(
            dataframe,
            "Destinatário",
            "Destinatario",
        )

        if len(dataframe.columns) < 2:
            raise ValueError(
                f"A base {arquivo.name} não possui a coluna B "
                "necessária para a Data de emissão."
            )

        coluna_data_emissao = dataframe.columns[1]

        faltantes = []
        if coluna_cte is None:
            faltantes.append("CTE")
        if coluna_destinatario is None:
            faltantes.append("Destinatário")

        if faltantes:
            raise ValueError(
                f"A base {arquivo.name} não possui a(s) coluna(s): "
                + ", ".join(faltantes)
                + "."
            )

        for _, linha in dataframe.iterrows():
            cte = normalizar_rastreio(
                linha[coluna_cte]
            )

            if not cte:
                continue

            data_emissao = _normalizar_data_busca(
                linha[coluna_data_emissao]
            )

            if data_emissao is None:
                continue

            destinatario = _normalizar_destinatario_folha(
                linha[coluna_destinatario]
            )

            partes = [
                parte.strip()
                for parte in destinatario.split("/")
            ]

            if len(partes) < 3:
                continue

            sigla = normalizar_texto(partes[0])
            cliente = normalizar_texto(partes[1]).replace(" ", "")
            divisao = normalizar_texto(partes[-1])

            if (
                not sigla
                or cliente != "MCDONALDS"
                or not divisao
            ):
                continue

            por_chave[(sigla, divisao)].append(
                {
                    "cte": cte,
                    "data_emissao": data_emissao,
                    "arquivo": arquivo.name,
                }
            )
            total_linhas += 1

    for candidatos in por_chave.values():
        candidatos.sort(
            key=lambda item: item["data_emissao"]
        )

    log(
        "Folha de apoio lida: "
        f"{total_linhas} CTE(s) elegíveis para localização."
    )

    return {
        "por_chave": por_chave,
    }


def _coletar_rastreios_existentes(workbook) -> set[str]:
    """Retorna todos os CTEs já presentes na coluna B das tabelas."""
    existentes = set()

    for worksheet in workbook.worksheets:
        linha_cabecalho = _localizar_linha_cabecalho_destino(
            worksheet
        )

        if linha_cabecalho is None:
            continue

        for linha in range(
            linha_cabecalho + 1,
            worksheet.max_row + 1,
        ):
            cte = normalizar_rastreio(
                worksheet.cell(row=linha, column=2).value
            )
            if cte:
                existentes.add(cte)

    return existentes


def _peso_taxado_esperado(valor_qtd_mil) -> float | None:
    """Converte F da Mega Cartela em Peso Taxado esperado."""
    if valor_qtd_mil is None:
        return None

    if isinstance(valor_qtd_mil, str):
        texto = valor_qtd_mil.strip()
        if not texto:
            return None
        texto = texto.replace(".", "").replace(",", ".")
        try:
            valor = float(texto)
        except ValueError:
            return None
    else:
        try:
            valor = float(valor_qtd_mil)
        except (TypeError, ValueError):
            return None

    return (valor / 1000.0) * 1.80


def _peso_taxado_compativel(
    esperado: float | None,
    encontrado: float | None,
) -> bool:
    if esperado is None or encontrado is None:
        return False

    return abs(esperado - encontrado) <= 0.01


def _adicionar_rastreios_da_folha_apoio(
    workbook,
    arquivos_folha: list[Path],
    log,
    validar_cte_peso,
) -> dict:
    indice = _ler_folhas_apoio_rastreios(
        arquivos_folha,
        log,
    )

    por_chave = indice["por_chave"]
    ctes_utilizados = set()
    ctes_existentes = _coletar_rastreios_existentes(workbook)

    encontrados = 0
    nao_encontrados = 0
    sem_conversao = 0
    rejeitados_data = 0
    rejeitados_peso = 0

    for item in _coletar_linhas_sem_rastreio(workbook):
        divisao = _obter_divisao_folha(item["codigo"])

        if divisao is None:
            sem_conversao += 1
            log(
                f"Rastreio não localizado | Aba {item['aba']} | "
                f"Linha {item['linha']} | SIGLA {item['sigla']} | "
                f"CÓDIGO {item['codigo']} | "
                "não existe conversão para a divisão da Folha de apoio."
            )
            continue

        sigla = normalizar_texto(item["sigla"])
        chave = (sigla, normalizar_texto(divisao))
        data_aba = item["data_aba"]
        peso_esperado = _peso_taxado_esperado(
            item["worksheet"].cell(row=item["linha"], column=6).value
        )

        if data_aba is None:
            log(
                f"Rastreio não localizado | Aba {item['aba']} | "
                f"Linha {item['linha']} | data da aba inválida."
            )
            nao_encontrados += 1
            continue

        if peso_esperado is None:
            log(
                f"Rastreio não localizado | Aba {item['aba']} | "
                f"Linha {item['linha']} | CTE candidato não será validado "
                "porque a coluna F não possui QTD MIL válida."
            )
            rejeitados_peso += 1
            continue

        candidatos = por_chave.get(chave, [])
        candidato_aceito = None

        for candidato in candidatos:
            cte = candidato["cte"]

            if cte in ctes_existentes or cte in ctes_utilizados:
                continue

            data_emissao = candidato["data_emissao"]

            if data_emissao < data_aba:
                rejeitados_data += 1
                continue

            log(
                f"Validando CTE {cte} | Aba {item['aba']} | "
                f"Linha {item['linha']} | Data emissão "
                f"{data_emissao.strftime('%d/%m/%Y')} >= data da aba "
                f"{data_aba.strftime('%d/%m/%Y')} | "
                f"Peso esperado {peso_esperado:.2f}."
            )

            peso_encontrado = validar_cte_peso(cte, log)

            if not _peso_taxado_compativel(
                peso_esperado,
                peso_encontrado,
            ):
                rejeitados_peso += 1
                log(
                    f"CTE {cte} rejeitado | Peso Taxado "
                    f"encontrado: "
                    f"{peso_encontrado:.2f} | "
                    f"esperado: {peso_esperado:.2f}."
                    if peso_encontrado is not None
                    else
                    f"CTE {cte} rejeitado | Peso Taxado não encontrado | "
                    f"esperado: {peso_esperado:.2f}."
                )
                continue

            candidato_aceito = candidato
            break

        if candidato_aceito is None:
            nao_encontrados += 1
            log(
                f"Rastreio não localizado | Aba {item['aba']} | "
                f"Linha {item['linha']} | "
                f"Busca {item['sigla']}/MCDONALDS/{divisao} | "
                f"Peso esperado {peso_esperado:.2f}."
            )
            continue

        cte = candidato_aceito["cte"]
        data_emissao = candidato_aceito["data_emissao"]

        item["worksheet"].cell(
            row=item["linha"],
            column=2,
            value=cte,
        )

        ctes_utilizados.add(cte)
        ctes_existentes.add(cte)
        encontrados += 1

        log(
            f"Rastreio encontrado | Aba {item['aba']} | "
            f"Linha {item['linha']} | "
            f"Busca {item['sigla']}/MCDONALDS/{divisao} | "
            f"CTE {cte} | Emissão {data_emissao.strftime('%d/%m/%Y')} | "
            f"Peso Taxado {peso_esperado:.2f}."
        )

    return {
        "encontrados": encontrados,
        "nao_encontrados": nao_encontrados,
        "sem_conversao": sem_conversao,
        "rejeitados_data": rejeitados_data,
        "rejeitados_peso": rejeitados_peso,
    }


def _executar_busca_rastreios_folha_apoio(
    workbook,
    pasta_folha_apoio: Path,
    log,
) -> dict:
    """
    Localiza CTEs das células B vazias dentro das tabelas.

    A ordem é sempre a aba mais recente para a mais antiga.
    O candidato da Folha de apoio precisa ter Data de emissão igual ou
    posterior à data da aba e também passar pela validação de Peso Taxado
    no Fraction.
    """
    pendentes = _coletar_linhas_sem_rastreio(workbook)

    if not pendentes:
        log("Nenhum pedido sem Rastreio para localizar.")
        return {
            "pendentes": 0,
            "encontrados": 0,
            "nao_encontrados": 0,
            "sem_conversao": 0,
            "rejeitados_data": 0,
            "rejeitados_peso": 0,
        }

    log(
        f"{len(pendentes)} pedido(s) sem Rastreio. "
        "A busca será feita da aba mais recente para a mais antiga."
    )

    datas_aba = [
        item["data_aba"]
        for item in pendentes
        if item["data_aba"] is not None
    ]

    data_limite = min(datas_aba) if datas_aba else datetime.now().date()

    log(
        "Aba mais antiga com Rastreio vazio: "
        f"{data_limite.strftime('%d/%m/%Y')}. "
        "As Folhas de apoio serão baixadas até esse período."
    )

    pasta_folha_apoio.mkdir(
        parents=True,
        exist_ok=True,
    )

    browser = None
    context = None

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=HEADLESS
            )

            context = browser.new_context()
            page = context.new_page()

            log(
                "Abrindo e autenticando no Fraction com a conta da Mega Cartela..."
            )
            login_fraction_mega_cartela(page, log)

            arquivos = baixar_relatorios_descritivos_correntista_ate_data_fraction(
                page,
                pasta_folha_apoio,
                log,
                data_limite=data_limite,
            )

            def validar_cte_peso(cte, log_func):
                try:
                    return pesquisar_e_capturar_peso_taxado_fraction(
                        page,
                        cte,
                        log_func,
                    )
                except PlaywrightTimeoutError:
                    log_func(
                        f"CTE {cte}: timeout durante a validação do Peso Taxado."
                    )
                    return None

            return _adicionar_rastreios_da_folha_apoio(
                workbook,
                arquivos,
                log,
                validar_cte_peso,
            )

    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass

        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass


def _coletar_rastreios_pendentes(
    workbook,
) -> tuple[list[str], int, int]:
    """
    Varre todas as abas usando:
      B = Rastreio
      M = status

    Depois de achar "Rastreio" na coluna B, começa na linha seguinte.
    Se o status da coluna M não for ENTREGUE nem EXTRAVIO,
    adiciona o Rastreio da coluna B à lista de consulta.

    Não é feita verificação de duplicidade.
    """
    rastreios = []
    linhas_pendentes = 0
    abas_processadas = 0

    for worksheet in workbook.worksheets:
        linha_cabecalho = (
            _localizar_linha_cabecalho_destino(
                worksheet
            )
        )

        if linha_cabecalho is None:
            continue

        abas_processadas += 1

        for linha in range(
            linha_cabecalho + 1,
            worksheet.max_row + 1,
        ):
            rastreio = normalizar_rastreio(
                worksheet.cell(
                    row=linha,
                    column=2,
                ).value
            )

            if not rastreio:
                continue

            status = normalizar_texto(
                worksheet.cell(
                    row=linha,
                    column=13,
                ).value
            )

            if status in STATUS_FINALIZADORES:
                continue

            linhas_pendentes += 1

            rastreios.append(
                rastreio
            )

    return (
        rastreios,
        linhas_pendentes,
        abas_processadas,
    )


def resumir_pendentes_cartela() -> dict:
    caminho_cartela = (
        localizar_mega_cartela()
    )

    (
        workbook,
        caminho_sanitizado,
        _,
    ) = _abrir_mega_cartela_seguro(
        caminho_cartela,
        read_only=True,
        data_only=True,
    )

    try:
        (
            rastreios,
            linhas_pendentes,
            abas_processadas,
        ) = _coletar_rastreios_pendentes(
            workbook
        )

    finally:
        workbook.close()

        _apagar_temporario_xlsx(
            caminho_sanitizado
        )

    return {
        "rastreios": len(
            rastreios
        ),
        "linhas": linhas_pendentes,
        "abas": abas_processadas,
    }


def _criar_pasta_update_temp() -> tuple[Path, Path]:
    agora = datetime.now().strftime(
        "%Y-%m-%d_%H-%M-%S"
    )

    pasta_execucao = (
        PASTA_UPDATE_TEMP
        / f"extracao_{agora}"
    )

    pasta_bases = (
        pasta_execucao
        / "bases"
    )

    pasta_bases.mkdir(
        parents=True,
        exist_ok=True,
    )

    return (
        pasta_execucao,
        pasta_bases,
    )


def _consultar_rastreios_fraction(
    rastreios: list[str],
    pasta_bases: Path,
    log,
) -> tuple[list[Path], set[str]]:
    """
    Usa exatamente a mesma navegação/consulta do fluxo
    baixar_relatorios_performance:

        Consultas -> Consulta Geral -> até 1000 CTEs -> Processar -> Excel.

    A recuperação também segue o mesmo padrão:
      tentativas com refresh -> Chromium novo -> tentativa final.
    """
    validar_env_performance()

    arquivos_bases = []
    rastreios_falhos: set[str] = set()

    with sync_playwright() as playwright:
        browser = None
        context = None
        page = None

        def fechar_sessao():
            nonlocal browser, context, page

            if context is not None:
                try:
                    context.close()
                except Exception as erro:
                    log(
                        "Aviso ao fechar contexto do Fraction: "
                        f"{type(erro).__name__}: {erro}"
                    )

            if browser is not None:
                try:
                    browser.close()
                except Exception as erro:
                    log(
                        "Aviso ao fechar Chromium do Fraction: "
                        f"{type(erro).__name__}: {erro}"
                    )

            browser = None
            context = None
            page = None

        def abrir_sessao():
            nonlocal browser, context, page

            fechar_sessao()

            browser = playwright.chromium.launch(
                headless=HEADLESS
            )
            context = browser.new_context()
            page = context.new_page()

            log(
                "Abrindo e autenticando no Fraction..."
            )

            fazer_login_performance(
                page
            )

            log(
                "Login no Fraction concluído."
            )

        def recuperar_sessao():
            recuperar_sessao_fraction(
                page,
                log,
                login_callback=lambda: (
                    fazer_login_performance(
                        page
                    )
                ),
            )

        try:
            try:
                abrir_sessao()

            except Exception as erro_login:
                log(
                    "Fraction indisponível no início da atualização. "
                    "Os rastreios permanecerão pendentes para a próxima execução. "
                    f"Detalhe: {type(erro_login).__name__}: {erro_login}"
                )

                rastreios_falhos.update(
                    rastreios
                )

            total = len(
                rastreios
            )

            for inicio in range(
                0,
                total,
                LIMITE_POR_CONSULTA_PERFORMANCE,
            ):
                lote = rastreios[
                    inicio:
                    inicio
                    + LIMITE_POR_CONSULTA_PERFORMANCE
                ]

                numero_lote = (
                    inicio
                    // LIMITE_POR_CONSULTA_PERFORMANCE
                ) + 1

                if all(
                    rastreio in rastreios_falhos
                    for rastreio in lote
                ):
                    continue

                log(
                    f"Processando lote {numero_lote} "
                    f"com {len(lote)} CTE(s)."
                )

                def consultar_lote():
                    return processar_lote_performance(
                        page,
                        lote,
                        numero_lote,
                        pasta_bases,
                    )

                try:
                    arquivo_base = (
                        executar_com_retentativas_fraction(
                            consultar_lote,
                            recuperar_sessao,
                            log,
                            descricao=(
                                f"Lote {numero_lote}"
                            ),
                            tentativas=(
                                TENTATIVAS_CONSULTA_UPDATE
                            ),
                        )
                    )

                    arquivos_bases.append(
                        arquivo_base
                    )

                    log(
                        f"Lote {numero_lote} consultado com sucesso."
                    )

                except PlaywrightTimeoutError:
                    log(
                        f"Lote {numero_lote}: retentativas com refresh "
                        "esgotadas. Reiniciando o Chromium para uma "
                        "última tentativa."
                    )

                    try:
                        abrir_sessao()

                        arquivo_base = (
                            consultar_lote()
                        )

                        arquivos_bases.append(
                            arquivo_base
                        )

                        log(
                            f"Lote {numero_lote}: recuperado após "
                            "reiniciar o Chromium."
                        )

                    except Exception as erro_final:
                        rastreios_falhos.update(
                            lote
                        )

                        log(
                            f"Lote {numero_lote}: erro definitivo. "
                            f"{type(erro_final).__name__}: {erro_final}"
                        )

                        try:
                            abrir_sessao()

                        except Exception as erro_reabertura:
                            restantes = rastreios[
                                inicio
                                + LIMITE_POR_CONSULTA_PERFORMANCE:
                            ]

                            rastreios_falhos.update(
                                restantes
                            )

                            log(
                                "Não foi possível restabelecer o Fraction. "
                                "Os lotes restantes ficarão pendentes para "
                                "a próxima atualização. "
                                f"Detalhe: {type(erro_reabertura).__name__}: "
                                f"{erro_reabertura}"
                            )

                            break

                except Exception as erro_lote:
                    rastreios_falhos.update(
                        lote
                    )

                    log(
                        f"Lote {numero_lote}: erro na consulta. "
                        f"{type(erro_lote).__name__}: {erro_lote}"
                    )

        finally:
            fechar_sessao()

    return (
        arquivos_bases,
        rastreios_falhos,
    )


def _ler_bases_fraction(
    arquivos: list[Path],
) -> dict[str, dict]:
    """
    Os arquivos exportados pela Consulta Geral possuem cabeçalho na linha 2.

    Campos usados na Mega Cartela:
      CTE       -> chave para localizar Rastreio
      Status    -> status
      Dt Evento -> Data da entrega quando ENTREGUE
      Dt Emissão/Dt Emissao -> MANUSEIO
    """
    dados = {}

    for arquivo in arquivos:
        dataframe = pd.read_excel(
            arquivo,
            header=1,
            dtype=object,
        )

        coluna_cte = _localizar_coluna_dataframe(
            dataframe,
            "CTE",
            "Código",
            "Codigo",
        )
        coluna_status = _localizar_coluna_dataframe(
            dataframe,
            "Status",
        )
        coluna_dt_evento = _localizar_coluna_dataframe(
            dataframe,
            "Dt Evento",
        )
        coluna_dt_emissao = _localizar_coluna_dataframe(
            dataframe,
            "Dt Emissão",
            "Dt Emissao",
        )

        faltantes = []

        if coluna_cte is None:
            faltantes.append(
                "CTE"
            )

        if coluna_status is None:
            faltantes.append(
                "Status"
            )

        if coluna_dt_evento is None:
            faltantes.append(
                "Dt Evento"
            )

        if coluna_dt_emissao is None:
            faltantes.append(
                "Dt Emissão"
            )

        if faltantes:
            raise ValueError(
                f"A base exportada {arquivo.name} não possui "
                "a(s) coluna(s): "
                + ", ".join(
                    faltantes
                )
                + "."
            )

        for _, linha in dataframe.iterrows():
            rastreio = normalizar_rastreio(
                linha[
                    coluna_cte
                ]
            )

            if not rastreio:
                continue

            dados[
                rastreio
            ] = {
                "Status": linha[
                    coluna_status
                ],
                "Dt Evento": linha[
                    coluna_dt_evento
                ],
                "Dt Emissao": linha[
                    coluna_dt_emissao
                ],
            }

    return dados


def _status_para_cartela(
    valor,
) -> str:
    status = (
        ""
        if valor is None
        else str(
            valor
        ).strip()
    )

    if normalizar_texto(
        status
    ) == "CUSTODIA":
        return "EM TRATATIVA"

    return status


def _recalcular_resumo_status(
    worksheet,
    linha_cabecalho: int,
) -> None:
    """
    Recalcula a tabela STATUS do topo.
    A coluna M é a coluna fixa de status.
    """
    contagens = {
        normalizar_texto(
            status
        ): 0
        for status in STATUS_RESUMO
    }

    for linha in range(
        linha_cabecalho + 1,
        worksheet.max_row + 1,
    ):
        status = normalizar_texto(
            worksheet.cell(
                row=linha,
                column=13,
            ).value
        )

        if status in contagens:
            contagens[
                status
            ] += 1

    for linha in range(
        1,
        linha_cabecalho,
    ):
        status_resumo = normalizar_texto(
            worksheet.cell(
                row=linha,
                column=2,
            ).value
        )

        if status_resumo in contagens:
            worksheet.cell(
                row=linha,
                column=3,
                value=contagens[
                    status_resumo
                ],
            )


def _formatar_valor_log(
    valor,
) -> str:
    if valor is None:
        return "vazio"

    if isinstance(
        valor,
        datetime,
    ):
        return valor.strftime(
            "%d/%m/%Y"
        )

    if isinstance(
        valor,
        date,
    ):
        return valor.strftime(
            "%d/%m/%Y"
        )

    texto = str(
        valor
    ).strip()

    return (
        texto
        if texto
        else "vazio"
    )


def _valores_iguais_log(
    antigo,
    novo,
) -> bool:
    return (
        _formatar_valor_log(
            antigo
        )
        == _formatar_valor_log(
            novo
        )
    )


def _aplicar_update_fraction(
    workbook,
    dados_update: dict[str, dict],
    log=lambda mensagem: None,
) -> dict:
    """
    Aplica o retorno do Fraction nas colunas fixas:
      B = Rastreio
      J = MANUSEIO
      M = status
      N = Data da entrega
    """
    linhas_atualizadas = 0
    abas_atualizadas = set()
    rastreios_atualizados = set()

    for worksheet in workbook.worksheets:
        linha_cabecalho = (
            _localizar_linha_cabecalho_destino(
                worksheet
            )
        )

        if linha_cabecalho is None:
            continue

        _corrigir_estilo_cabecalho(
            worksheet,
            linha_cabecalho,
        )

        atualizou_aba = False

        for linha in range(
            linha_cabecalho + 1,
            worksheet.max_row + 1,
        ):
            rastreio = normalizar_rastreio(
                worksheet.cell(
                    row=linha,
                    column=2,
                ).value
            )

            if (
                not rastreio
                or rastreio not in dados_update
            ):
                continue

            update = dados_update[
                rastreio
            ]

            status_fraction = (
                ""
                if update[
                    "Status"
                ] is None
                else str(
                    update[
                        "Status"
                    ]
                ).strip()
            )

            status_novo = _status_para_cartela(
                status_fraction
            )

            celula_status = worksheet.cell(
                row=linha,
                column=13,
            )
            celula_manuseio = worksheet.cell(
                row=linha,
                column=10,
            )
            celula_entrega = worksheet.cell(
                row=linha,
                column=14,
            )

            status_antigo = (
                celula_status.value
            )
            manuseio_antigo = (
                celula_manuseio.value
            )
            entrega_antiga = (
                celula_entrega.value
            )

            manuseio_novo = _converter_data(
                update[
                    "Dt Emissao"
                ]
            )

            if normalizar_texto(
                status_novo
            ) == "ENTREGUE":
                entrega_nova = _converter_data(
                    update[
                        "Dt Evento"
                    ]
                )
            else:
                entrega_nova = None

            celula_status.value = (
                status_novo
            )

            celula_manuseio.value = (
                manuseio_novo
            )
            celula_manuseio.number_format = (
                "dd/mm/yyyy"
            )

            celula_entrega.value = (
                entrega_nova
            )

            if entrega_nova is not None:
                celula_entrega.number_format = (
                    "dd/mm/yyyy"
                )

            mudancas = []

            if not _valores_iguais_log(
                status_antigo,
                status_novo,
            ):
                texto_status_novo = (
                    _formatar_valor_log(
                        status_novo
                    )
                )

                if (
                    normalizar_texto(
                        status_fraction
                    )
                    == "CUSTODIA"
                ):
                    texto_status_novo += (
                        " (Fraction: CUSTODIA)"
                    )

                mudancas.append(
                    "status: "
                    f"{_formatar_valor_log(status_antigo)}"
                    " -> "
                    f"{texto_status_novo}"
                )

            if not _valores_iguais_log(
                manuseio_antigo,
                manuseio_novo,
            ):
                mudancas.append(
                    "MANUSEIO: "
                    f"{_formatar_valor_log(manuseio_antigo)}"
                    " -> "
                    f"{_formatar_valor_log(manuseio_novo)}"
                )

            if not _valores_iguais_log(
                entrega_antiga,
                entrega_nova,
            ):
                mudancas.append(
                    "Data da entrega: "
                    f"{_formatar_valor_log(entrega_antiga)}"
                    " -> "
                    f"{_formatar_valor_log(entrega_nova)}"
                )

            if mudancas:
                log(
                    f"CTE {rastreio} | "
                    f"Aba {worksheet.title} | "
                    + " | ".join(
                        mudancas
                    )
                )
            else:
                log(
                    f"CTE {rastreio} | "
                    f"Aba {worksheet.title} | "
                    "sem alteração."
                )

            linhas_atualizadas += 1
            rastreios_atualizados.add(
                rastreio
            )
            atualizou_aba = True

        _recalcular_resumo_status(
            worksheet,
            linha_cabecalho,
        )

        if atualizou_aba:
            abas_atualizadas.add(
                worksheet.title
            )

    return {
        "linhas_atualizadas": (
            linhas_atualizadas
        ),
        "abas_atualizadas": sorted(
            abas_atualizadas
        ),
        "rastreios_atualizados": (
            rastreios_atualizados
        ),
    }


def _limpar_arquivos_pasta(pasta: Path) -> None:
    """Remove somente os arquivos de uma pasta, preservando a pasta."""
    pasta.mkdir(parents=True, exist_ok=True)

    for caminho in pasta.iterdir():
        if caminho.is_file() or caminho.is_symlink():
            try:
                caminho.unlink()
            except FileNotFoundError:
                pass
        elif caminho.is_dir():
            shutil.rmtree(caminho, ignore_errors=True)


def atualizar_dados_cartela(
    log=lambda mensagem: None,
) -> dict:
    """
    Atualização completa da Mega Cartela em um único botão.

    ORDEM:
      1. Localiza CTEs das células B vazias e valida cada candidato.
      2. Depois coleta TODOS os Rastreios não finalizados, incluindo os
         CTEs recém-adicionados, e executa a atualização normal.

    A localização de CTE exige:
      - mesma SIGLA/MCDONALDS/DIVISÃO;
      - Data de emissão da Folha de apoio >= data da aba;
      - CTE ainda não utilizado em nenhuma outra aba da cartela;
      - Peso Taxado do Fraction compatível com a coluna F da Mega Cartela.
    """
    caminho_cartela = localizar_mega_cartela()

    (
        workbook,
        caminho_sanitizado,
        cartela_sanitizada,
    ) = _abrir_mega_cartela_seguro(caminho_cartela)

    if cartela_sanitizada:
        log(
            "Foi detectado um XML inválido na Mega Cartela. "
            "Uma cópia temporária corrigida foi usada para continuar."
        )

    pasta_folha_apoio = PASTA_FOLHA_APOIO_TEMP
    pasta_execucao = None
    workbook_salvo = False

    resultado_busca = {
        "pendentes": 0,
        "encontrados": 0,
        "nao_encontrados": 0,
        "sem_conversao": 0,
        "rejeitados_data": 0,
        "rejeitados_peso": 0,
    }

    rastreios = []
    linhas_pendentes = 0
    abas_processadas = 0
    rastreios_sem_retorno = set()
    rastreios_falhos = set()

    resultado_aplicacao = {
        "linhas_atualizadas": 0,
        "abas_atualizadas": [],
        "rastreios_atualizados": set(),
    }

    try:
        # --------------------------------------------------
        # ETAPA 1 - localizar e adicionar CTEs faltantes.
        # --------------------------------------------------
        resultado_busca = _executar_busca_rastreios_folha_apoio(
            workbook,
            pasta_folha_apoio,
            log,
        )

        log(
            f"Busca de CTEs concluída: {resultado_busca['encontrados']} "
            "Rastreio(s) adicionado(s)."
        )

        # --------------------------------------------------
        # ETAPA 2 - atualizar TODOS os CTEs não finalizados,
        # inclusive os recém-adicionados na etapa anterior.
        # --------------------------------------------------
        (
            rastreios,
            linhas_pendentes,
            abas_processadas,
        ) = _coletar_rastreios_pendentes(workbook)

        if rastreios:
            log(
                f"{len(rastreios)} Rastreio(s) pendente(s) "
                f"em {linhas_pendentes} linha(s) para atualização."
            )

            total_lotes = (
                (
                    len(rastreios)
                    + LIMITE_POR_CONSULTA_PERFORMANCE
                    - 1
                )
                // LIMITE_POR_CONSULTA_PERFORMANCE
            )

            log(
                f"A consulta será realizada em {total_lotes} lote(s) "
                f"de até {LIMITE_POR_CONSULTA_PERFORMANCE} CTE(s)."
            )

            (
                pasta_execucao,
                pasta_bases,
            ) = _criar_pasta_update_temp()

            (
                arquivos_bases,
                rastreios_falhos,
            ) = _consultar_rastreios_fraction(
                rastreios,
                pasta_bases,
                log,
            )

            dados_update = (
                _ler_bases_fraction(
                    arquivos_bases
                )
                if arquivos_bases
                else {}
            )

            rastreios_sem_retorno = (
                set(rastreios)
                - set(dados_update)
            )

            if dados_update:
                resultado_aplicacao = _aplicar_update_fraction(
                    workbook,
                    dados_update,
                    log=log,
                )
            else:
                log(
                    "Nenhum retorno válido foi obtido do Fraction "
                    "para a atualização."
                )
        else:
            log(
                "Nenhum Rastreio preenchido e não finalizado "
                "para atualização."
            )

        houve_alteracao = (
            resultado_busca["encontrados"] > 0
            or resultado_aplicacao["linhas_atualizadas"] > 0
        )

        caminho_backup = None

        if houve_alteracao:
            pasta_backups = PASTA_RESULTADOS / ".backups"
            pasta_backups.mkdir(
                parents=True,
                exist_ok=True,
            )

            agora = datetime.now().strftime(
                "%Y-%m-%d_%H-%M-%S"
            )

            caminho_backup = (
                pasta_backups
                / (
                    "mega_cartela_lsm_"
                    f"antes_update_{agora}.xlsx"
                )
            )

            caminho_backup.write_bytes(
                caminho_cartela.read_bytes()
            )

            caminho_temporario = caminho_cartela.with_name(
                ".mega_cartela_lsm_update.tmp.xlsx"
            )

            try:
                workbook.save(caminho_temporario)
                caminho_temporario.replace(caminho_cartela)
                workbook_salvo = True
            except PermissionError as erro:
                raise PermissionError(
                    "Não foi possível atualizar mega_cartela_lsm.xlsx. "
                    "Confirme se a planilha está fechada no Excel."
                ) from erro
            finally:
                if caminho_temporario.exists():
                    caminho_temporario.unlink(missing_ok=True)

        log(
            f"{resultado_busca['encontrados']} Rastreio(s) adicionado(s) "
            "pela Folha de apoio."
        )

        log(
            f"{resultado_aplicacao['linhas_atualizadas']} linha(s) "
            "atualizada(s) no Fraction."
        )

        if resultado_busca["nao_encontrados"]:
            log(
                f"{resultado_busca['nao_encontrados']} pedido(s) "
                "continuaram sem Rastreio."
            )

        if resultado_busca["sem_conversao"]:
            log(
                f"{resultado_busca['sem_conversao']} pedido(s) "
                "ficaram sem Rastreio por falta de conversão do CÓDIGO."
            )

        if resultado_busca["rejeitados_data"]:
            log(
                f"{resultado_busca['rejeitados_data']} candidato(s) "
                "foram rejeitados por Data de emissão anterior à data da aba."
            )

        if resultado_busca["rejeitados_peso"]:
            log(
                f"{resultado_busca['rejeitados_peso']} candidato(s) "
                "foram rejeitados na validação de Peso Taxado."
            )

        if rastreios_sem_retorno:
            log(
                f"{len(rastreios_sem_retorno)} Rastreio(s) ficaram "
                "sem retorno na atualização."
            )

        return {
            "arquivo": caminho_cartela,
            "backup": caminho_backup,
            "rastreios_encontrados": resultado_busca["encontrados"],
            "rastreios_pendentes_sem_encontrar": (
                resultado_busca["nao_encontrados"]
                + resultado_busca["sem_conversao"]
            ),
            "rastreios_consultados": len(rastreios),
            "linhas_pendentes": linhas_pendentes,
            "linhas_atualizadas": resultado_aplicacao["linhas_atualizadas"],
            "abas_atualizadas": resultado_aplicacao["abas_atualizadas"],
            "rastreios_sem_retorno": len(rastreios_sem_retorno),
            "rastreios_falhos": len(rastreios_falhos),
            "abas_processadas": abas_processadas,
        }

    finally:
        try:
            workbook.close()
        except Exception:
            pass

        if pasta_execucao is not None:
            shutil.rmtree(
                pasta_execucao,
                ignore_errors=True,
            )

        _limpar_arquivos_pasta(
            pasta_folha_apoio
        )

        _apagar_temporario_xlsx(
            caminho_sanitizado
        )


def adicionar_aba_solicitacao(
    arquivo_importado,
) -> dict:
    """
    Primeira etapa do fluxo Mega Cartela LSM.

    Processo:
    - abre resultados/mega_cartela_lsm/mega_cartela_lsm.xlsx;
    - usa uma aba existente como MODELO VISUAL;
    - cria uma cópia com nome DD-MM;
    - remove TODAS as fórmulas da nova aba;
    - mantém a tabela STATUS do topo com contadores zerados;
    - limpa os pedidos antigos;
    - importa somente os dados disponíveis pelo nome das colunas;
    - preenche QTD MIL com "-";
    - deixa os demais campos vazios para o futuro UPDATE.
    """
    caminho_cartela = (
        localizar_mega_cartela()
    )

    nome_aba = extrair_nome_aba(
        arquivo_importado.name
    )

    dados_arquivo = (
        arquivo_importado.getvalue()
    )

    # Somente valores calculados. Fórmulas do import não são transferidas.
    workbook_origem = load_workbook(
        BytesIO(
            dados_arquivo
        ),
        data_only=True,
    )

    worksheet_origem = (
        _localizar_aba_origem(
            workbook_origem
        )
    )

    dados = _extrair_dados_origem(
        worksheet_origem
    )

    workbook_destino = load_workbook(
        caminho_cartela
    )

    if nome_aba in workbook_destino.sheetnames:
        raise ValueError(
            f"A aba '{nome_aba}' já existe na Mega Cartela."
        )

    (
        worksheet_modelo,
        linha_cabecalho_modelo,
    ) = _localizar_aba_modelo(
        workbook_destino,
        nome_aba,
    )

    worksheet_nova = (
        workbook_destino.copy_worksheet(
            worksheet_modelo
        )
    )

    worksheet_nova.title = (
        nome_aba
    )

    linha_cabecalho = (
        _localizar_linha_cabecalho_destino(
            worksheet_nova
        )
    )

    if linha_cabecalho is None:
        linha_cabecalho = (
            linha_cabecalho_modelo
        )

    _desmesclar_area_dados(
        worksheet_nova,
        linha_cabecalho,
    )

    _remover_formulas(
        worksheet_nova
    )

    _zerar_resumo_status(
        worksheet_nova,
        linha_cabecalho,
    )

    _limpar_tabela_dados(
        worksheet_nova,
        linha_cabecalho,
    )

    _preencher_tabela(
        worksheet_nova,
        linha_cabecalho,
        dados,
    )

    # Pedidos recém-adicionados entram inicialmente como EM PRODUÇÃO.
    # Atualiza também o contador correspondente na tabela de status.
    quantidade_em_producao = len(dados)

    for linha in range(
        1,
        linha_cabecalho,
    ):
        valor_status = normalizar_texto(
            worksheet_nova.cell(
                row=linha,
                column=2,
            ).value
        )

        if valor_status == normalizar_texto(
            "EM PRODUÇÃO"
        ):
            worksheet_nova.cell(
                row=linha,
                column=3,
                value=quantidade_em_producao,
            )
            break

    pasta_backups = (
        PASTA_RESULTADOS
        / ".backups"
    )

    pasta_backups.mkdir(
        parents=True,
        exist_ok=True,
    )

    agora = datetime.now().strftime(
        "%Y-%m-%d_%H-%M-%S"
    )

    caminho_backup = (
        pasta_backups
        / (
            "mega_cartela_lsm_"
            f"antes_{nome_aba}_{agora}.xlsx"
        )
    )

    caminho_backup.write_bytes(
        caminho_cartela.read_bytes()
    )

    caminho_temporario = (
        caminho_cartela.with_name(
            ".mega_cartela_lsm.tmp.xlsx"
        )
    )

    try:
        workbook_destino.save(
            caminho_temporario
        )

        caminho_temporario.replace(
            caminho_cartela
        )

    except PermissionError as erro:
        raise PermissionError(
            "Não foi possível atualizar mega_cartela_lsm.xlsx. "
            "Confirme se a planilha está fechada no Excel."
        ) from erro

    finally:
        if caminho_temporario.exists():
            caminho_temporario.unlink(
                missing_ok=True
            )

    return {
        "arquivo": caminho_cartela,
        "backup": caminho_backup,
        "aba": nome_aba,
        "linhas": len(
            dados
        ),
        "aba_origem": (
            worksheet_origem.title
        ),
        "aba_modelo": (
            worksheet_modelo.title
        ),
    }
