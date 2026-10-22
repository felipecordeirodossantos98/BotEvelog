from __future__ import annotations

from datetime import date, datetime, timedelta
import json
from pathlib import Path
import re
import shutil
import time
import traceback
import unicodedata

import pandas as pd
import xlsxwriter
from playwright.sync_api import TimeoutError, sync_playwright

from automacoes.baixar_relatorios_analitico import (
    MAX_TENTATIVAS_EXTRACAO_DIA,
    abrir_relatorio,
    preencher_data,
    processar,
    recuperar_pagina_analitico,
)
from servicos.fraction import login_fraction
from utils.config import (
    BASES_ROMANEIOS,
    ARQUIVO_DESTINOS_FRACTION,
    ARQUIVO_ROTAS_ROMANEIOS,
    FRACTION_PASSWORD,
    FRACTION_USER,
    HEADLESS,
    SLOW_MO_MS,
    TEMPORARIOS_ROMANEIOS,
    URL_FRACTION,
)

REGIAO_POR_UF = {
    "AC": "Norte", "AP": "Norte", "AM": "Norte", "PA": "Norte",
    "RO": "Norte", "RR": "Norte", "TO": "Norte",
    "AL": "Nordeste", "BA": "Nordeste", "CE": "Nordeste",
    "MA": "Nordeste", "PB": "Nordeste", "PE": "Nordeste",
    "PI": "Nordeste", "RN": "Nordeste", "SE": "Nordeste",
    "DF": "Centro-Oeste", "GO": "Centro-Oeste", "MT": "Centro-Oeste",
    "MS": "Centro-Oeste",
    "ES": "Sudeste", "MG": "Sudeste", "RJ": "Sudeste", "SP": "Sudeste",
    "PR": "Sul", "RS": "Sul", "SC": "Sul",
}
ID_DESTINO_LABEL = '[id="form_envio_remessa:selectDestino_label"]'
ID_DESTINO_FILTRO = '[id="form_envio_remessa:selectDestino_filter"]'
ID_DESTINO_BUSCAR = ".ui-selectonemenu-filter-container > .ui-icon"
ID_ROTA_LABEL = '[id="form_envio_remessa:selectRota_label"]'
ID_ROTA_PANEL = '[id="form_envio_remessa:selectRota_panel"]'
ID_CTES = '[id="form_envio_remessa:txtArea"]'
TAMANHO_LOTE_CTES = 1000


def _log(log, mensagem: str) -> None:
    if log is None:
        print(mensagem)
    else:
        log(mensagem)


def _valor_excel(valor):
    if pd.isna(valor):
        return None
    if isinstance(valor, pd.Timestamp):
        return valor.to_pydatetime()
    return valor


def _unificar_romaneios(
    arquivos: list[Path],
    data_inicial: date,
    data_final: date,
    log=None,
) -> dict:
    if not arquivos:
        raise ValueError("Nenhuma extração foi gerada para o período selecionado.")

    BASES_ROMANEIOS.mkdir(parents=True, exist_ok=True)
    agora = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    nome = (
        f"Gerar_Romaneios_{data_inicial:%d-%m-%Y}_a_"
        f"{data_final:%d-%m-%Y}_{agora}.xlsx"
    )
    destino = BASES_ROMANEIOS / nome
    temporario = BASES_ROMANEIOS / f".{nome}.tmp.xlsx"
    workbook = None
    colunas_referencia = None
    linha_atual = 0
    total_linhas = 0
    linhas_emissao = 0

    try:
        workbook = xlsxwriter.Workbook(temporario, {"constant_memory": True})
        worksheet = workbook.add_worksheet("Romaneios")

        for caminho in arquivos:
            _log(log, f"Unificando {caminho.name}...")
            dataframe = pd.read_excel(caminho, engine="openpyxl")
            linhas_arquivo = len(dataframe)
            colunas = list(dataframe.columns)
            mapa_colunas = {str(coluna).strip().upper(): coluna for coluna in colunas}
            coluna_status = mapa_colunas.get("STATUS")
            if coluna_status is None:
                raise ValueError(f"A coluna STATUS não foi encontrada em '{caminho.name}'.")

            if colunas_referencia is None:
                colunas_referencia = colunas
                for indice, coluna in enumerate(colunas):
                    worksheet.write(linha_atual, indice, coluna)
                linha_atual += 1
            elif colunas != colunas_referencia:
                raise ValueError(
                    f"O layout da base '{caminho.name}' é diferente do primeiro arquivo."
                )

            status = (
                dataframe[coluna_status]
                .fillna("")
                .astype(str)
                .str.strip()
                .str.upper()
            )
            dataframe = dataframe.loc[status.eq("EMISSAO")]
            linhas_emissao += len(dataframe)
            total_linhas += linhas_arquivo
            _log(
                log,
                f"{caminho.name}: {linhas_arquivo} linha(s) lida(s), "
                f"{len(dataframe)} com STATUS=EMISSAO; "
                f"{linhas_arquivo - len(dataframe)} descartada(s).",
            )

            for valores in dataframe.itertuples(index=False, name=None):
                for indice, valor in enumerate(valores):
                    worksheet.write(linha_atual, indice, _valor_excel(valor))
                linha_atual += 1

            del dataframe

        workbook.close()
        workbook = None
        temporario.replace(destino)
    except Exception:
        if workbook is not None:
            try:
                workbook.close()
            except Exception:
                pass
        temporario.unlink(missing_ok=True)
        raise

    return {
        "arquivo_final": str(destino),
        "linhas_consultadas": total_linhas,
        "linhas_emissao": linhas_emissao,
        "arquivos_unificados": len(arquivos),
        "tamanho_arquivo_bytes": destino.stat().st_size,
    }


def agrupar_ctes_por_destino(arquivo: str | Path) -> pd.DataFrame:
    dataframe = pd.read_excel(
        arquivo,
        engine="openpyxl",
        dtype={"CTE": "string"},
    )
    colunas = {str(coluna).strip().upper(): coluna for coluna in dataframe.columns}
    faltantes = [nome for nome in ("CTE", "CIDADE_DEST", "UF.1") if nome not in colunas]
    if faltantes:
        raise ValueError(
            "Coluna(s) obrigatória(s) não encontrada(s) no arquivo unificado: "
            + ", ".join(faltantes)
        )

    dados = pd.DataFrame(
        {
            "CTE": dataframe[colunas["CTE"]].fillna("").astype(str).str.strip(),
            "CIDADE_DEST": dataframe[colunas["CIDADE_DEST"]].fillna("").astype(str).str.strip(),
            "UF": dataframe[colunas["UF.1"]].fillna("").astype(str).str.strip().str.upper(),
        }
    )
    dados["CTE"] = dados["CTE"].str.replace(r"\.0$", "", regex=True)
    dados = dados.loc[dados["CTE"].ne("")].copy()
    dados["REGIAO"] = dados["UF"].map(REGIAO_POR_UF).fillna("Não identificada")
    dados["_CIDADE_CHAVE"] = dados["CIDADE_DEST"].map(_normalizar_rota)

    grupos = (
        dados.groupby(
            ["REGIAO", "UF", "_CIDADE_CHAVE"],
            dropna=False,
            sort=True,
        )["CTE"]
        .agg(list)
        .reset_index()
    )
    cidades = (
        dados.groupby(
            ["REGIAO", "UF", "_CIDADE_CHAVE"],
            dropna=False,
            sort=True,
        )["CIDADE_DEST"]
        .first()
        .reset_index(drop=True)
    )
    grupos["CIDADE_DEST"] = cidades
    grupos["TOTAL_CTES"] = grupos["CTE"].map(len)
    grupos["CTES"] = grupos["CTE"].map(lambda ctes: ", ".join(ctes))
    return grupos[["REGIAO", "UF", "CIDADE_DEST", "TOTAL_CTES", "CTES"]]


def _normalizar_rota(valor: object) -> str:
    texto = unicodedata.normalize("NFKD", str(valor).strip())
    texto = "".join(caractere for caractere in texto if not unicodedata.combining(caractere))
    return " ".join(texto.upper().split())


def carregar_mapa_rotas(caminho: Path = ARQUIVO_ROTAS_ROMANEIOS) -> dict[str, str]:
    if not caminho.exists():
        raise FileNotFoundError(
            "Base de rotas não encontrada. Coloque o JSON em "
            f"'{caminho}'."
        )

    with caminho.open("r", encoding="utf-8-sig") as arquivo:
        dados = json.load(arquivo)

    mapa: dict[str, str] = {}

    def campo(registro: dict, *nomes: str):
        campos = {
            str(chave).strip().casefold(): valor
            for chave, valor in registro.items()
        }
        for nome in nomes:
            valor = campos.get(nome.casefold())
            if valor not in (None, ""):
                return valor
        return None

    def adicionar(cidade: object, rota: object, uf: object = "") -> None:
        if not cidade or not rota:
            raise ValueError("Cada item da base de rotas precisa informar cidade e rota.")
        cidade_normalizada = _normalizar_rota(cidade)
        uf_normalizada = _normalizar_rota(uf) if uf else ""
        chave = f"{uf_normalizada}|{cidade_normalizada}" if uf_normalizada else cidade_normalizada
        valor_rota = str(rota).strip()
        if chave in mapa and mapa[chave] != valor_rota:
            raise ValueError(f"Há mais de uma rota configurada para '{cidade}' ({uf}).")
        mapa[chave] = valor_rota

    if isinstance(dados, dict):
        registros = campo(dados, "rotas", "filiais")
        if isinstance(registros, list):
            dados = registros
        elif (
            campo(dados, "cidade", "cidade_dest", "cidade_destino")
            and campo(dados, "rota")
        ):
            adicionar(
                campo(dados, "cidade", "cidade_dest", "cidade_destino"),
                campo(dados, "rota"),
                campo(dados, "uf"),
            )
            return mapa
        else:
            for chave, valor in dados.items():
                if isinstance(valor, dict):
                    adicionar(
                        campo(valor, "cidade", "cidade_dest", "cidade_destino"),
                        campo(valor, "rota"),
                        campo(valor, "uf"),
                    )
                elif isinstance(chave, str) and "|" in chave:
                    uf, cidade = chave.split("|", 1)
                    adicionar(cidade, valor, uf)
                else:
                    adicionar(chave, valor)
            return mapa

    if isinstance(dados, list):
        for item in dados:
            if not isinstance(item, dict):
                raise ValueError("A lista de rotas precisa conter objetos JSON.")
            filial = campo(item, "filial")
            cidades = campo(item, "cidades")
            if filial and isinstance(cidades, list):
                uf = campo(item, "uf")
                for cidade in cidades:
                    adicionar(cidade, filial, uf)
                continue
            adicionar(
                campo(item, "cidade", "cidade_dest", "cidade_destino"),
                campo(item, "rota", "filial"),
                campo(item, "uf"),
            )
        return mapa

    raise ValueError(
        "Formato de JSON inválido. Use um objeto cidade: rota ou uma lista "
        "de objetos com cidade e rota (UF opcional)."
    )


def carregar_destinos_fraction(
    caminho: Path = ARQUIVO_ROTAS_ROMANEIOS,
) -> list[dict[str, str]]:
    if not caminho.exists():
        raise FileNotFoundError(f"Arquivo de rotas e unidades não encontrado: '{caminho}'.")
    with caminho.open("r", encoding="utf-8-sig") as arquivo:
        dados = json.load(arquivo)
    destinos = dados.get("unidades") if isinstance(dados, dict) else None
    if not isinstance(destinos, list) and ARQUIVO_DESTINOS_FRACTION.exists():
        with ARQUIVO_DESTINOS_FRACTION.open("r", encoding="utf-8-sig") as arquivo:
            catalogo = json.load(arquivo)
        destinos = catalogo.get("destinos") if isinstance(catalogo, dict) else None
    if not isinstance(destinos, list):
        raise ValueError("O catálogo de destinos precisa conter uma lista 'destinos'.")
    resultado = []
    for destino in destinos:
        if not isinstance(destino, dict) or not destino.get("nome") or not destino.get("codigo"):
            raise ValueError("Cada destino precisa ter 'nome' e 'codigo'.")
        resultado.append({"nome": str(destino["nome"]).strip(), "codigo": str(destino["codigo"]).strip()})
    return resultado


def carregar_codigos_filiais(
    caminho: Path = ARQUIVO_ROTAS_ROMANEIOS,
) -> dict[str, list[str]]:
    """Lê códigos de destino incorporados aos registros de filial do rotas.json."""
    with caminho.open("r", encoding="utf-8-sig") as arquivo:
        dados = json.load(arquivo)
    filiais = dados.get("filiais") if isinstance(dados, dict) else None
    if not isinstance(filiais, list):
        raise ValueError("O JSON de rotas precisa conter uma lista 'filiais'.")
    codigos: dict[str, list[str]] = {}
    for filial in filiais:
        if not isinstance(filial, dict) or not filial.get("filial"):
            raise ValueError("Cada registro de filial precisa informar 'filial'.")
        codigos_configurados = filial.get("codigos_destino") or []
        if isinstance(codigos_configurados, str):
            codigos_configurados = [codigos_configurados]
        codigo_prioritario = str(filial.get("codigo_destino") or "").strip()
        codigos_configurados = [
            str(codigo).strip() for codigo in codigos_configurados if str(codigo).strip()
        ]
        if codigo_prioritario and codigo_prioritario not in codigos_configurados:
            codigos_configurados.insert(0, codigo_prioritario)
        if not codigos_configurados:
            continue
        chave = _normalizar_rota(filial["filial"])
        if chave in codigos and codigos[chave] != codigos_configurados:
            raise ValueError(f"Há códigos de destino conflitantes para '{filial['filial']}'.")
        codigos[chave] = codigos_configurados
    return codigos


def selecionar_rota_manifesto(page, *, log=None) -> str:
    """Seleciona a única opção com RODO e confirma que o campo foi atualizado."""
    painel = page.locator(ID_ROTA_PANEL)
    for tentativa in range(1, 4):
        page.locator(ID_ROTA_LABEL).click()
        opcoes = page.get_by_role(
            "option",
            name=re.compile("RODO", re.IGNORECASE),
        )
        try:
            opcoes.first.wait_for(state="visible", timeout=10000)
        except TimeoutError as erro:
            raise RuntimeError(
                "Nenhuma opção de rota contendo 'RODO' apareceu no Fraction."
            ) from erro

        quantidade = opcoes.count()
        nomes = [nome.strip() for nome in opcoes.all_inner_texts() if nome.strip()]
        if quantidade != 1:
            raise RuntimeError(
                "Era esperada uma única opção de rota contendo 'RODO', "
                f"mas foram encontradas {quantidade}: {nomes}"
            )

        selecionada = nomes[0] if nomes else "opção contendo RODO"
        _log(log, f"Tentativa {tentativa}/3: selecionando rota '{selecionada}'.")
        opcoes.click()
        page.keyboard.press("Escape")
        try:
            painel.wait_for(state="hidden", timeout=3000)
        except TimeoutError:
            _log(log, "Menu da rota ainda aberto; fechando pelo seletor.")
            page.locator(ID_ROTA_LABEL).click()
            painel.wait_for(state="hidden", timeout=5000)

        rotulo = page.locator(ID_ROTA_LABEL)
        for _ in range(20):
            valor_atual = rotulo.inner_text().strip()
            if _normalizar_rota(selecionada) in _normalizar_rota(valor_atual):
                _log(log, f"Rota confirmada no campo: {valor_atual}.")
                return selecionada
            page.wait_for_timeout(250)

        _log(
            log,
            f"A rota não apareceu no campo após a tentativa {tentativa}/3. "
            f"Valor atual: '{rotulo.inner_text().strip()}'.",
        )

    raise RuntimeError(
        "A opção com RODO foi clicada, mas o campo Rota não confirmou o valor "
        "após três tentativas."
    )


def aguardar_carregamento_manifesto(page, *, log=None) -> None:
    """Espera o spinner do Ajax aparecer e desaparecer após adicionar CTEs."""
    spinner = page.get_by_text("Carregando").first
    _log(log, "Aguardando o spinner 'Carregando' aparecer após Adicionar.")
    try:
        spinner.wait_for(state="visible", timeout=120_000)
    except TimeoutError as erro:
        raise TimeoutError(
            "O Fraction não exibiu o spinner 'Carregando' após adicionar os CTEs; "
            "não é possível confirmar que a lista terminou de atualizar."
        ) from erro

    _log(log, "Spinner detectado; aguardando o fim do carregamento dos CTEs.")
    spinner.wait_for(state="hidden", timeout=180_000)
    _log(log, "Spinner 'Carregando' desapareceu; atualização concluída.")


def associar_destinos_fraction(
    grupos: list[dict],
    destinos: list[dict[str, str]],
    codigos_filiais: dict[str, list[str]] | None = None,
) -> tuple[list[dict], list[str]]:
    """Liga cada filial ao destino pelo código configurado, ou nome único legado."""
    por_nome: dict[str, list[dict[str, str]]] = {}
    por_codigo: dict[str, list[dict[str, str]]] = {}
    for destino in destinos:
        por_nome.setdefault(_normalizar_rota(destino["nome"]), []).append(destino)
        por_codigo.setdefault(destino["codigo"], []).append(destino)
    codigos_filiais = codigos_filiais or {}
    associados = []
    pendentes = []
    for grupo in grupos:
        codigos_configurados = codigos_filiais.get(_normalizar_rota(grupo["ROTA"]), [])
        if codigos_configurados:
            codigo_selecionado = next(
                (
                    codigo
                    for codigo in codigos_configurados
                    if len(por_codigo.get(codigo, [])) == 1
                ),
                None,
            )
            correspondencias = por_codigo.get(codigo_selecionado, []) if codigo_selecionado else []
        else:
            correspondencias = por_nome.get(_normalizar_rota(grupo["ROTA"]), [])
        contexto = f"{grupo['CIDADE_DEST']}/{grupo['UF']} (filial '{grupo['ROTA']}')"
        if len(correspondencias) != 1:
            detalhe = "não encontrada" if not correspondencias else "ambígua"
            pendentes.append(f"{contexto}: unidade {detalhe} no catálogo")
            continue
        destino = correspondencias[0]
        item = dict(grupo)
        item["NOME_DESTINO"] = destino["nome"]
        item["CODIGO_DESTINO"] = destino["codigo"]
        associados.append(item)
    return associados, pendentes


def adicionar_rotas_a_previa(tabela_destinos: pd.DataFrame, *, log=None) -> pd.DataFrame:
    """Acrescenta filial e código de destino à tabela exibida para conferência."""
    tabela = tabela_destinos.copy()
    tabela["ROTA"] = "Sem rota"
    tabela["CODIGO_ROTA"] = "Sem código"
    try:
        mapa_rotas = carregar_mapa_rotas()
        destinos = carregar_destinos_fraction()
        codigos_filiais = carregar_codigos_filiais()
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as erro:
        _log(log, f"Não foi possível carregar rotas/códigos para a prévia: {erro}")
        return tabela

    destinos_por_codigo: dict[str, list[dict[str, str]]] = {}
    destinos_por_nome: dict[str, list[dict[str, str]]] = {}
    for destino in destinos:
        destinos_por_codigo.setdefault(destino["codigo"], []).append(destino)
        destinos_por_nome.setdefault(_normalizar_rota(destino["nome"]), []).append(destino)

    rotas_encontradas = 0
    codigos_encontrados = 0
    for indice, registro in tabela.iterrows():
        rota = _rota_do_grupo(
            mapa_rotas,
            str(registro["UF"]),
            str(registro["CIDADE_DEST"]),
        )
        if rota is None:
            continue
        tabela.at[indice, "ROTA"] = rota
        rotas_encontradas += 1

        codigos = codigos_filiais.get(_normalizar_rota(rota), [])
        if codigos:
            destino = next(
                (
                    destinos_por_codigo[codigo][0]
                    for codigo in codigos
                    if len(destinos_por_codigo.get(codigo, [])) == 1
                ),
                None,
            )
        else:
            opcoes = destinos_por_nome.get(_normalizar_rota(rota), [])
            destino = opcoes[0] if len(opcoes) == 1 else None
        if destino is not None:
            tabela.at[indice, "CODIGO_ROTA"] = destino["codigo"]
            codigos_encontrados += 1

    _log(
        log,
        f"Prévia de rotas: {rotas_encontradas}/{len(tabela)} rota(s) e "
        f"{codigos_encontrados}/{len(tabela)} código(s) identificados.",
    )
    return tabela


def _rota_do_grupo(mapa: dict[str, str], uf: str, cidade: str) -> str | None:
    cidade_normalizada = _normalizar_rota(cidade)
    uf_normalizada = _normalizar_rota(uf)
    return mapa.get(f"{uf_normalizada}|{cidade_normalizada}") or mapa.get(
        cidade_normalizada
    )


def mapear_rotas_para_grupos(
    tabela_destinos: pd.DataFrame,
    mapa_rotas: dict[str, str],
) -> tuple[list[dict], list[str]]:
    grupos = []
    faltantes = []
    for registro in tabela_destinos.to_dict("records"):
        rota = _rota_do_grupo(
            mapa_rotas,
            str(registro["UF"]),
            str(registro["CIDADE_DEST"]),
        )
        if rota is None:
            faltantes.append(
                f"{registro['CIDADE_DEST']}/{registro['UF']}"
            )
            continue
        registro["ROTA"] = rota
        registro["LISTA_CTES"] = str(registro["CTES"]).split(", ")
        grupos.append(registro)

    return grupos, faltantes


def validar_rotas_para_grupos(
    tabela_destinos: pd.DataFrame,
    mapa_rotas: dict[str, str],
) -> list[dict]:
    grupos, faltantes = mapear_rotas_para_grupos(
        tabela_destinos,
        mapa_rotas,
    )
    if faltantes:
        raise ValueError(
            "Não há rota cadastrada para: " + ", ".join(faltantes)
        )
    return grupos


def processar_manifestos(
    page,
    grupos: list[dict],
    *,
    log=None,
) -> int:
    destinos, pendentes = associar_destinos_fraction(
        grupos,
        carregar_destinos_fraction(),
        carregar_codigos_filiais(),
    )
    if pendentes:
        raise ValueError(
            "Manifestos não iniciados; revise os destinos no catálogo: "
            + "; ".join(pendentes)
        )
    _log(log, "Acessando Manifesto no Fraction...")
    page.get_by_role("link", name="Operacional").click()
    page.get_by_role("link", name="Manifesto", exact=True).click()
    page.get_by_role("link", name="Envio de remessa").click()

    total_lotes = 0
    for grupo in destinos:
        cidade = grupo["CIDADE_DEST"]
        uf = grupo["UF"]
        rota = grupo["NOME_DESTINO"]
        codigo_destino = grupo["CODIGO_DESTINO"]
        ctes = grupo["LISTA_CTES"]
        _log(
            log,
            f"Destino {cidade}/{uf}: {len(ctes)} CTE(s), unidade "
            f"'{rota}', código selecionado {codigo_destino}.",
        )
        _log(log, "Abrindo seletor de unidade do Manifesto.")
        page.locator(ID_DESTINO_LABEL).click()
        filtro = page.locator(ID_DESTINO_FILTRO)
        filtro.fill(codigo_destino)
        _log(log, f"Filtro de unidade preenchido com o código {codigo_destino}.")
        page.locator(ID_DESTINO_BUSCAR).first.click()
        _log(log, "Busca de unidade acionada; aguardando opção correspondente.")
        page.get_by_role(
            "option", name=f"{rota} - {codigo_destino}", exact=True
        ).click()
        _log(log, f"Unidade selecionada: {rota} - {codigo_destino}.")
        _log(log, "Procurando a opção de rota que contém 'RODO'.")
        selecionar_rota_manifesto(page, log=log)

        for inicio in range(0, len(ctes), TAMANHO_LOTE_CTES):
            lote = ctes[inicio : inicio + TAMANHO_LOTE_CTES]
            numero_lote = inicio // TAMANHO_LOTE_CTES + 1
            total_lotes += 1
            page.locator(ID_CTES).fill("\n".join(lote))
            _log(
                log,
                f"Lote {numero_lote}: preenchidos {len(lote)} CTE(s) "
                f"para {cidade}/{uf} (unidade {codigo_destino}).",
            )
            page.get_by_role(
                "button",
                name="Adicionar",
            ).click()
            _log(log, f"Lote {numero_lote} adicionado ao Manifesto.")
            aguardar_carregamento_manifesto(page, log=log)

    _log(log, "Todos os lotes foram adicionados; finalizando com Processar lista.")
    page.get_by_role("button", name="Processar lista").click()
    _log(log, "Botão Processar lista acionado no Fraction.")

    return total_lotes


def executar_gerar_romaneios(
    data_inicial: date,
    data_final: date,
    *,
    callback=None,
    log=None,
) -> dict:
    if not URL_FRACTION or not FRACTION_USER or not FRACTION_PASSWORD:
        raise ValueError("URL_FRACTION, FRACTION_USER e FRACTION_PASSWORD precisam estar configurados.")
    if data_final < data_inicial:
        raise ValueError("A data final precisa ser igual ou posterior à data inicial.")

    pasta_temporaria = TEMPORARIOS_ROMANEIOS / "gerar_romaneios"
    if pasta_temporaria.exists():
        shutil.rmtree(pasta_temporaria)
    pasta_temporaria.mkdir(parents=True, exist_ok=True)

    arquivos: list[Path] = []
    total_dias = (data_final - data_inicial).days + 1
    inicio_execucao = time.perf_counter()
    _log(
        log,
        f"Configuração: {total_dias} dia(s), navegador "
        f"{'headless' if HEADLESS else 'visível'}, até "
        f"{MAX_TENTATIVAS_EXTRACAO_DIA} tentativa(s) por dia, "
        f"lotes máximos de {TAMANHO_LOTE_CTES} CTE(s).",
    )
    _log(log, f"Temporários da execução: {pasta_temporaria}.")
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=HEADLESS, slow_mo=SLOW_MO_MS)
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()
            page.on(
                "pageerror",
                lambda erro: _log(log, f"Erro JavaScript na página: {erro}"),
            )
            try:
                _log(log, "Realizando login no Fraction com usuário operacional...")
                login_fraction(page, FRACTION_USER, FRACTION_PASSWORD, lambda msg: _log(log, msg))
                _log(log, f"Login concluído; página atual: {page.url}.")

                data = data_inicial
                contador = 1
                while data <= data_final:
                    if callback:
                        callback(contador, total_dias, data)
                    _log(log, f"Iniciando extração de {data:%d/%m/%Y} ({contador}/{total_dias}).")
                    tentativa = 1
                    while tentativa <= MAX_TENTATIVAS_EXTRACAO_DIA:
                        inicio_tentativa = time.perf_counter()
                        try:
                            _log(log, f"Tentativa {tentativa}: abrindo relatório analítico.")
                            abrir_relatorio(page, log=log)
                            _log(log, f"Tentativa {tentativa}: preenchendo data do relatório.")
                            preencher_data(page, data, log=log)
                            _log(log, f"Tentativa {tentativa}: solicitando extração.")
                            processar(page, log=log)

                            caminho = pasta_temporaria / f"Romaneios_{data:%d-%m-%Y}.xlsx"
                            _log(log, f"Aguardando exportação Excel para {caminho.name}.")
                            with page.expect_download(timeout=600000) as download_info:
                                page.get_by_role(
                                    "button", name=" Exportar para Excel"
                                ).click(force=True, no_wait_after=True)
                            download_info.value.save_as(caminho)
                            arquivos.append(caminho)
                            _log(
                                log,
                                f"Extração de {data:%d/%m/%Y} concluída: "
                                f"{caminho.stat().st_size} bytes em "
                                f"{time.perf_counter() - inicio_tentativa:.1f}s.",
                            )
                            break
                        except TimeoutError as erro:
                            _log(
                                log,
                                f"Timeout em {data:%d/%m/%Y}, tentativa "
                                f"{tentativa}/{MAX_TENTATIVAS_EXTRACAO_DIA}; "
                                f"URL atual: {page.url}; erro: {erro}",
                            )
                            if tentativa >= MAX_TENTATIVAS_EXTRACAO_DIA:
                                raise RuntimeError(
                                    f"Falha ao extrair {data:%d/%m/%Y} após "
                                    f"{MAX_TENTATIVAS_EXTRACAO_DIA} tentativas: {erro}"
                                ) from erro
                            recuperar_pagina_analitico(page, log=log)
                            tentativa += 1

                    page.wait_for_timeout(1000)
                    data += timedelta(days=1)
                    contador += 1

                _log(log, "Unificando extrações e mantendo somente STATUS = EMISSAO...")
                resultado = _unificar_romaneios(
                    arquivos,
                    data_inicial,
                    data_final,
                    log=log,
                )
                _log(log, "Agrupando CTEs por região, UF e cidade de destino...")
                resultado["tabela_destinos"] = agrupar_ctes_por_destino(
                    resultado["arquivo_final"]
                )
                resultado["tabela_destinos"] = adicionar_rotas_a_previa(
                    resultado["tabela_destinos"],
                    log=log,
                )
                resultado["total_ctes_agrupados"] = int(
                    resultado["tabela_destinos"]["TOTAL_CTES"].sum()
                )
                _log(
                    log,
                    f"Agrupamento concluído: {len(resultado['tabela_destinos'])} "
                    f"grupo(s) de região/UF/cidade e "
                    f"{resultado['total_ctes_agrupados']} CTE(s).",
                )
                _log(
                    log,
                    f"Arquivo final salvo em: {resultado['arquivo_final']} "
                    f"({resultado['tamanho_arquivo_bytes']} bytes; "
                    f"{resultado['linhas_consultadas']} linha(s) consultada(s), "
                    f"{resultado['linhas_emissao']} EMISSAO).",
                )
                _log(
                    log,
                    "Prévia pronta para conferência. Os manifestos não foram "
                    "iniciados nesta etapa.",
                )
            finally:
                _log(log, "Encerrando navegador do Fraction...")
                context.close()
                browser.close()
        _log(
            log,
            f"Execução concluída em {time.perf_counter() - inicio_execucao:.1f}s; "
            f"{len(arquivos)} arquivo(s) diário(s) extraído(s).",
        )
        return resultado
    except Exception:
        _log(log, "Falha geral na automação:\n" + traceback.format_exc())
        raise
    finally:
        shutil.rmtree(pasta_temporaria, ignore_errors=True)
        TEMPORARIOS_ROMANEIOS.mkdir(parents=True, exist_ok=True)


def executar_manifestos_romaneios(
    arquivo_base: str | Path,
    *,
    log=None,
) -> dict:
    """Envia os grupos de uma base já conferida ao fluxo de Manifesto."""
    caminho = Path(arquivo_base)
    if not caminho.is_file():
        raise FileNotFoundError(f"Base de romaneios não encontrada: '{caminho}'.")
    if not URL_FRACTION or not FRACTION_USER or not FRACTION_PASSWORD:
        raise ValueError("URL_FRACTION, FRACTION_USER e FRACTION_PASSWORD precisam estar configurados.")

    inicio_execucao = time.perf_counter()
    _log(log, f"Iniciando segunda etapa com a base revisada: {caminho}.")
    _log(log, f"Base selecionada: {caminho.stat().st_size} bytes.")
    _log(log, "Lendo CTEs agrupados da base final e validando rotas/unidades.")
    tabela = agrupar_ctes_por_destino(caminho)
    mapa_rotas = carregar_mapa_rotas()
    grupos, rotas_ausentes = mapear_rotas_para_grupos(tabela, mapa_rotas)
    if rotas_ausentes:
        _log(
            log,
            f"Ignorando {len(rotas_ausentes)} grupo(s) sem rota cadastrada: "
            + "; ".join(rotas_ausentes),
        )
    grupos, destinos_pendentes = associar_destinos_fraction(
        grupos,
        carregar_destinos_fraction(),
        carregar_codigos_filiais(),
    )
    if destinos_pendentes:
        _log(
            log,
            f"Ignorando {len(destinos_pendentes)} grupo(s) sem unidade/código "
            f"válido: {'; '.join(destinos_pendentes)}",
        )
    if not grupos:
        raise ValueError(
            "Nenhum grupo tem rota e código de unidade configurados. "
            "Confira a tabela de prévia e o rotas.json."
        )

    total_ctes = sum(len(grupo["LISTA_CTES"]) for grupo in grupos)
    _log(
        log,
        f"Pré-validação concluída: {len(grupos)} grupo(s), {total_ctes} CTE(s), "
        f"até {TAMANHO_LOTE_CTES} por lote.",
    )
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=HEADLESS, slow_mo=SLOW_MO_MS)
            context = browser.new_context()
            page = context.new_page()
            page.on("pageerror", lambda erro: _log(log, f"Erro JavaScript na página: {erro}"))
            try:
                _log(log, "Realizando novo login no Fraction para a etapa de Manifesto.")
                login_fraction(
                    page,
                    FRACTION_USER,
                    FRACTION_PASSWORD,
                    lambda mensagem: _log(log, mensagem),
                )
                _log(log, f"Login concluído; página atual: {page.url}.")
                lotes = processar_manifestos(page, grupos, log=log)
            finally:
                _log(log, "Encerrando navegador da etapa de Manifesto.")
                context.close()
                browser.close()
    except Exception:
        _log(log, "Falha na etapa de Manifesto:\n" + traceback.format_exc())
        raise

    resultado = {
        "arquivo_final": str(caminho),
        "grupos_manifesto": len(grupos),
        "ctes_manifesto": total_ctes,
        "lotes_manifesto": lotes,
    }
    _log(
        log,
        f"Etapa de Manifesto concluída: {lotes} lote(s), {total_ctes} CTE(s), "
        f"{time.perf_counter() - inicio_execucao:.1f}s.",
    )
    return resultado
