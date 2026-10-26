from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
import re
import time
import traceback
import unicodedata

import pandas as pd
from playwright.sync_api import TimeoutError, sync_playwright

from servicos.fraction import login_fraction
from utils.config import (
    ARQUIVO_UNIDADES_FRACTION,
    ARQUIVO_ROTAS_ROMANEIOS,
    FRACTION_PASSWORD,
    FRACTION_USER,
    HEADLESS,
    SLOW_MO_MS,
    URL_FRACTION,
)

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


def _normalizar_rota(valor: object) -> str:
    texto = unicodedata.normalize("NFKD", str(valor).strip())
    texto = "".join(caractere for caractere in texto if not unicodedata.combining(caractere))
    return " ".join(texto.upper().split())


def carregar_rotas_destino_origem(
    caminho: Path = ARQUIVO_ROTAS_ROMANEIOS,
) -> tuple[dict[str, str], set[str]]:
    if not caminho.exists():
        raise FileNotFoundError(
            "Base de rotas não encontrada. Coloque o JSON em "
            f"'{caminho}'."
        )

    with caminho.open("r", encoding="utf-8-sig") as arquivo:
        dados = json.load(arquivo)

    registros = dados.get("rotas") if isinstance(dados, dict) else None
    if not isinstance(registros, list):
        raise ValueError("A base de rotas precisa conter uma lista 'rotas'.")

    origens_por_destino: dict[str, set[str]] = {}
    for registro in registros:
        if not isinstance(registro, dict):
            raise ValueError("Cada rota precisa conter 'origem' e 'destino'.")
        origem = str(registro.get("origem") or "").strip()
        destino = str(registro.get("destino") or "").strip()
        if not origem or not destino:
            continue
        origens_por_destino.setdefault(_normalizar_rota(destino), set()).add(origem)

    mapa = {
        destino: next(iter(origens))
        for destino, origens in origens_por_destino.items()
        if len(origens) == 1
    }
    ambiguos = {
        destino for destino, origens in origens_por_destino.items() if len(origens) > 1
    }
    return mapa, ambiguos


def carregar_mapa_rotas(caminho: Path = ARQUIVO_ROTAS_ROMANEIOS) -> dict[str, str]:
    """Retorna destino final → origem, omitindo destinos com mais de uma origem."""
    return carregar_rotas_destino_origem(caminho)[0]


def carregar_destinos_fraction(
    caminho: Path = ARQUIVO_UNIDADES_FRACTION,
) -> list[dict[str, str]]:
    if not caminho.exists():
        raise FileNotFoundError(f"Arquivo de unidades Fraction não encontrado: '{caminho}'.")
    with caminho.open("r", encoding="utf-8-sig") as arquivo:
        dados = json.load(arquivo)
    destinos = dados.get("unidades") if isinstance(dados, dict) else None
    if not isinstance(destinos, list):
        raise ValueError("O catálogo de unidades precisa conter uma lista 'unidades'.")
    resultado = []
    for destino in destinos:
        if not isinstance(destino, dict) or not destino.get("unidade") or not destino.get("codigo"):
            raise ValueError("Cada unidade precisa ter 'unidade' e 'codigo'.")
        resultado.append({"nome": str(destino["unidade"]).strip(), "codigo": str(destino["codigo"]).strip()})
    return resultado


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
) -> tuple[list[dict], list[str]]:
    """Liga cada origem de rota a uma unidade Fraction pelo nome exato."""
    por_nome: dict[str, list[dict[str, str]]] = {}
    for destino in destinos:
        por_nome.setdefault(_normalizar_rota(destino["nome"]), []).append(destino)
    associados = []
    pendentes = []
    for grupo in grupos:
        correspondencias = por_nome.get(_normalizar_rota(grupo["ROTA"]), [])
        contexto = f"unidade '{grupo['ROTA']}'"
        if len(correspondencias) != 1:
            detalhe = "não encontrada" if not correspondencias else "ambígua"
            pendentes.append(f"{contexto} {detalhe} no catálogo")
            continue
        destino = correspondencias[0]
        item = dict(grupo)
        item["NOME_DESTINO"] = destino["nome"]
        item["CODIGO_DESTINO"] = destino["codigo"]
        associados.append(item)
    return associados, pendentes


def adicionar_rotas_a_previa(tabela_base: pd.DataFrame, *, log=None) -> pd.DataFrame:
    """Acrescenta a origem de rota correspondente à coluna Destino da base."""
    tabela = tabela_base.copy()
    coluna_destino = next(
        (coluna for coluna in tabela if _normalizar_rota(coluna) == "DESTINO"),
        None,
    )
    if coluna_destino is None:
        raise ValueError("A base de entrada não contém a coluna 'Destino'.")

    try:
        mapa_rotas, destinos_ambiguos = carregar_rotas_destino_origem()
        unidades = carregar_destinos_fraction()
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as erro:
        raise ValueError(f"Não foi possível carregar a base de rotas: {erro}") from erro

    def resolver(destino):
        chave = _normalizar_rota(destino) if pd.notna(destino) else ""
        if chave in destinos_ambiguos:
            return "Rota ambígua"
        return mapa_rotas.get(chave, "Sem rota")

    tabela["Rota"] = tabela[coluna_destino].map(resolver)
    unidades_por_nome: dict[str, list[dict[str, str]]] = {}
    for unidade in unidades:
        unidades_por_nome.setdefault(_normalizar_rota(unidade["nome"]), []).append(unidade)
    tabela["Código da rota"] = tabela["Rota"].map(
        lambda rota: (
            unidades_por_nome[_normalizar_rota(rota)][0]["codigo"]
            if len(unidades_por_nome.get(_normalizar_rota(rota), [])) == 1
            else "Sem código"
        )
    )
    encontradas = int((~tabela["Rota"].isin(["Sem rota", "Rota ambígua"])).sum())
    ambiguas = int(tabela["Rota"].eq("Rota ambígua").sum())
    _log(log, f"Prévia de rotas: {encontradas}/{len(tabela)} destino(s) com origem única; {ambiguas} ambíguo(s).")
    return tabela


def mapear_rotas_para_grupos(
    tabela_base: pd.DataFrame,
) -> tuple[list[dict], list[str]]:
    coluna_cte = next(
        (coluna for coluna in tabela_base if _normalizar_rota(coluna) == "CTE"),
        None,
    )
    if coluna_cte is None or "Rota" not in tabela_base:
        raise ValueError("A base precisa conter as colunas CTE, Destino e Rota.")
    coluna_ctes_entrada = next(
        (
            coluna
            for coluna in tabela_base
            if _normalizar_rota(coluna) == "CTES PARA DAR ENTRADA"
        ),
        None,
    )
    grupos_por_rota: dict[str, list[str]] = {}
    ignorados: list[str] = []
    for _, linha in tabela_base.iterrows():
        rota = str(linha["Rota"]).strip()
        valor_ctes = (
            linha[coluna_ctes_entrada]
            if coluna_ctes_entrada is not None
            else linha[coluna_cte]
        )
        texto_ctes = "" if pd.isna(valor_ctes) else str(valor_ctes)
        ctes_linha = [
            re.sub(r"\.0$", "", cte.strip())
            for cte in texto_ctes.splitlines()
            if cte.strip() and cte.strip().lower() != "nan"
        ]
        if not ctes_linha:
            continue
        if rota in {"Sem rota", "Rota ambígua", ""}:
            ignorados.append(f"CTE(s) {', '.join(ctes_linha)}: {rota or 'sem rota'}")
            continue
        grupos_por_rota.setdefault(rota, [])
        for cte in ctes_linha:
            if cte not in grupos_por_rota[rota]:
                grupos_por_rota[rota].append(cte)
    grupos = [
        {"ROTA": rota, "LISTA_CTES": ctes}
        for rota, ctes in sorted(grupos_por_rota.items())
    ]
    return grupos, ignorados


def processar_manifestos(
    page,
    grupos: list[dict],
    *,
    log=None,
) -> int:
    destinos, pendentes = associar_destinos_fraction(
        grupos,
        carregar_destinos_fraction(),
    )
    if pendentes:
        raise ValueError(
            "Manifestos não iniciados; revise os destinos no catálogo: "
            + "; ".join(pendentes)
        )
    def abrir_envio_remessa() -> None:
        page.get_by_role("link", name="Operacional").click()
        page.get_by_role("link", name="Manifesto", exact=True).click()
        page.get_by_role("link", name="Envio de remessa").click()

    _log(log, "Acessando Manifesto no Fraction...")
    abrir_envio_remessa()

    total_lotes = 0
    for indice_grupo, grupo in enumerate(destinos):
        if indice_grupo:
            _log(
                log,
                "Recarregando Envio de remessa para limpar a lista anterior "
                "antes de selecionar a próxima unidade.",
            )
            page.reload(wait_until="domcontentloaded", timeout=120_000)
            page.get_by_role("link", name="Operacional").wait_for(
                state="visible",
                timeout=120_000,
            )
            abrir_envio_remessa()
        rota_unidade = grupo["ROTA"]
        rota = grupo["NOME_DESTINO"]
        codigo_destino = grupo["CODIGO_DESTINO"]
        ctes = grupo["LISTA_CTES"]
        _log(
            log,
            f"Unidade {rota_unidade}: {len(ctes)} CTE(s), destino Fraction "
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
                f"para {rota_unidade} (unidade {codigo_destino}).",
            )
            page.get_by_role(
                "button",
                name="Adicionar",
            ).click()
            _log(log, f"Lote {numero_lote} adicionado ao Manifesto.")
            aguardar_carregamento_manifesto(page, log=log)

        _log(log, f"Finalizando a remessa da unidade {rota_unidade}.")
        page.get_by_role("button", name="Processar lista").click()
        aguardar_carregamento_manifesto(page, log=log)
        _log(log, f"Remessa da unidade {rota_unidade} processada.")

    _log(log, "Todas as unidades roteadas foram processadas no Fraction.")

    return total_lotes


def executar_gerar_romaneios(
    arquivo_base: str | Path | bytes,
    *,
    nome_arquivo: str | None = None,
    log=None,
) -> dict:
    conteudo_upload = arquivo_base if isinstance(arquivo_base, bytes) else None
    caminho = None if conteudo_upload is not None else Path(arquivo_base)
    rotulo_arquivo = nome_arquivo or (caminho.name if caminho else "planilha importada")
    if caminho is not None and not caminho.is_file():
        raise FileNotFoundError(f"Base de entrada não encontrada: '{caminho}'.")
    tamanho = len(conteudo_upload) if conteudo_upload is not None else caminho.stat().st_size
    _log(log, f"Importando base de entrada: {rotulo_arquivo} ({tamanho} bytes).")
    try:
        origem = BytesIO(conteudo_upload) if conteudo_upload is not None else caminho
        tabela = pd.read_excel(origem, engine="openpyxl", dtype={"CTE": "string"})
        tabela = adicionar_rotas_a_previa(tabela, log=log)
        if not any(_normalizar_rota(coluna) == "CTE" for coluna in tabela.columns):
            raise ValueError("A planilha não contém a coluna obrigatória 'CTE'.")
        _log(log, f"Prévia importada: {len(tabela)} linha(s), {len(tabela.columns)} coluna(s).")
        return {
            "arquivo_final": rotulo_arquivo if conteudo_upload is not None else str(caminho),
            "tabela_destinos": tabela,
            "linhas_importadas": len(tabela),
        }
    except Exception:
        _log(log, "Falha ao importar a base de entrada:\n" + traceback.format_exc())
        raise


def executar_manifestos_romaneios(
    arquivo_base: str | Path,
    *,
    tabela_previa: pd.DataFrame | None = None,
    log=None,
) -> dict:
    """Envia os grupos de uma base já conferida ao fluxo de Manifesto."""
    caminho = Path(arquivo_base)
    if tabela_previa is None and not caminho.is_file():
        raise FileNotFoundError(f"Base de romaneios não encontrada: '{caminho}'.")
    if not URL_FRACTION or not FRACTION_USER or not FRACTION_PASSWORD:
        raise ValueError("URL_FRACTION, FRACTION_USER e FRACTION_PASSWORD precisam estar configurados.")

    inicio_execucao = time.perf_counter()
    _log(log, f"Iniciando segunda etapa com a base revisada: {arquivo_base}.")
    if caminho.is_file():
        _log(log, f"Base selecionada: {caminho.stat().st_size} bytes.")
    _log(log, "Lendo CTEs e rotas da base salva após Dar entrada.")
    tabela = (
        tabela_previa.copy()
        if tabela_previa is not None
        else pd.read_excel(caminho, engine="openpyxl", dtype={"CTE": "string"})
    )
    if "Rota" not in tabela:
        tabela = adicionar_rotas_a_previa(tabela, log=log)
    grupos, rotas_ausentes = mapear_rotas_para_grupos(tabela)
    if rotas_ausentes:
        _log(
            log,
            f"Ignorando {len(rotas_ausentes)} CTE(s) sem rota única: "
            + "; ".join(rotas_ausentes),
        )
    grupos, destinos_pendentes = associar_destinos_fraction(
        grupos,
        carregar_destinos_fraction(),
    )
    if destinos_pendentes:
        _log(
            log,
            f"Ignorando {len(destinos_pendentes)} grupo(s) sem unidade/código "
            f"válido: {'; '.join(destinos_pendentes)}",
        )
    if not grupos:
        raise ValueError(
            "Nenhum grupo tem uma rota única e código de unidade configurados. "
            "Confira Destino, rotas.json e unidades.json."
        )

    total_ctes = sum(len(grupo["LISTA_CTES"]) for grupo in grupos)
    _log(
        log,
        f"Pré-validação concluída: {len(grupos)} unidade(s), {total_ctes} CTE(s), "
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
