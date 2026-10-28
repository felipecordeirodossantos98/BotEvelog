from __future__ import annotations

import base64
from datetime import datetime
import json
from io import BytesIO
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import traceback
import unicodedata
from threading import Event
from urllib.parse import unquote_to_bytes

import pandas as pd
from playwright.sync_api import TimeoutError, sync_playwright

from servicos.fraction import login_fraction
from utils.config import (
    ARQUIVO_UNIDADES_FRACTION,
    ARQUIVO_ROTAS_ROMANEIOS,
    FRACTION_PASSWORD,
    FRACTION_USER,
    HEADLESS,
    RESULTADOS_ROMANEIOS,
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


def _normalizar_unidade_fraction(valor: object) -> str:
    nome = _normalizar_rota(valor)
    aliases_catalogo = {"FL MANAUS": "FL MANAUS JAD"}
    return aliases_catalogo.get(nome, nome)


def _normalizar_uf(valor: object) -> str:
    if valor is None or pd.isna(valor):
        return ""
    uf = _normalizar_rota(valor)
    nomes = {
        "ACRE": "AC",
        "ALAGOAS": "AL",
        "AMAPA": "AP",
        "AMAZONAS": "AM",
        "BAHIA": "BA",
        "CEARA": "CE",
        "DISTRITO FEDERAL": "DF",
        "ESPIRITO SANTO": "ES",
        "GOIAS": "GO",
        "MARANHAO": "MA",
        "MATO GROSSO": "MT",
        "MATO GROSSO DO SUL": "MS",
        "MINAS GERAIS": "MG",
        "PARA": "PA",
        "PARAIBA": "PB",
        "PARANA": "PR",
        "PERNAMBUCO": "PE",
        "PIAUI": "PI",
        "RIO DE JANEIRO": "RJ",
        "RIO GRANDE DO NORTE": "RN",
        "RIO GRANDE DO SUL": "RS",
        "RONDONIA": "RO",
        "RORAIMA": "RR",
        "SANTA CATARINA": "SC",
        "SAO PAULO": "SP",
        "SERGIPE": "SE",
        "TOCANTINS": "TO",
    }
    return nomes.get(uf, uf)


def carregar_regras_rotas_localidade(
    caminho: Path = ARQUIVO_ROTAS_ROMANEIOS,
) -> tuple[dict[str, str], dict[str, str]]:
    """Carrega as unidades atendentes por UF e por cidade normalizada."""
    if not caminho.exists():
        raise FileNotFoundError(f"Base de rotas não encontrada: '{caminho}'.")
    with caminho.open("r", encoding="utf-8-sig") as arquivo:
        dados = json.load(arquivo)
    if not isinstance(dados, dict):
        raise ValueError("A base de rotas precisa ser um objeto JSON.")
    if "por_uf" not in dados or "por_cidade" not in dados:
        raise ValueError(
            "A base de rotas precisa conter as listas 'por_uf' e 'por_cidade'."
        )

    regras_uf = dados["por_uf"]
    regras_cidade = dados["por_cidade"]
    if not isinstance(regras_uf, list) or not isinstance(regras_cidade, list):
        raise ValueError("'por_uf' e 'por_cidade' precisam ser listas.")

    rotas_por_uf: dict[str, str] = {}
    rotas_por_cidade: dict[str, str] = {}

    for regra in regras_uf:
        if not isinstance(regra, dict):
            raise ValueError("Cada item de 'por_uf' precisa ser um objeto JSON.")
        uf = _normalizar_uf(regra.get("uf") or "")
        unidade = str(regra.get("unidade") or "").strip()
        if not uf or not unidade:
            raise ValueError("Cada regra de 'por_uf' precisa de 'uf' e 'unidade'.")
        existente = rotas_por_uf.get(uf)
        if existente and _normalizar_rota(existente) != _normalizar_rota(unidade):
            raise ValueError(f"Mais de uma unidade foi definida para a UF {uf}.")
        rotas_por_uf[uf] = unidade

    for regra in regras_cidade:
        if not isinstance(regra, dict):
            raise ValueError("Cada item de 'por_cidade' precisa ser um objeto JSON.")
        cidade = _normalizar_rota(regra.get("cidade") or "")
        unidade = str(regra.get("unidade") or "").strip()
        if not cidade or not unidade:
            raise ValueError(
                "Cada regra de 'por_cidade' precisa de 'cidade' e 'unidade'."
            )
        existente = rotas_por_cidade.get(cidade)
        if existente and _normalizar_rota(existente) != _normalizar_rota(unidade):
            raise ValueError(f"Mais de uma unidade foi definida para a cidade {cidade}.")
        rotas_por_cidade[cidade] = unidade

    return rotas_por_uf, rotas_por_cidade


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
        if not isinstance(destino, dict):
            raise ValueError("Cada unidade precisa ser um objeto JSON.")
        if destino.get("ativo", True) is False:
            continue
        if not destino.get("unidade") or not destino.get("codigo"):
            raise ValueError("Cada unidade precisa ter 'unidade' e 'codigo'.")
        resultado.append({"nome": str(destino["unidade"]).strip(), "codigo": str(destino["codigo"]).strip()})
    return resultado


def carregar_codigos_preferenciais_unidades(
    caminho: Path = ARQUIVO_UNIDADES_FRACTION,
) -> dict[str, str]:
    """Carrega desempates de código junto ao catálogo de unidades do Fraction."""
    if not caminho.exists():
        raise FileNotFoundError(f"Arquivo de unidades Fraction não encontrado: '{caminho}'.")
    with caminho.open("r", encoding="utf-8-sig") as arquivo:
        dados = json.load(arquivo)
    if not isinstance(dados, dict):
        raise ValueError("O catálogo de unidades precisa ser um objeto JSON.")
    preferencias = dados.get("codigo_preferencial_por_unidade", {})
    if not isinstance(preferencias, dict):
        raise ValueError("'codigo_preferencial_por_unidade' precisa ser um objeto.")
    return {
        _normalizar_unidade_fraction(unidade): str(codigo).strip()
        for unidade, codigo in preferencias.items()
        if str(unidade).strip() and str(codigo).strip()
    }


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


def _capturar_resposta_pdf(response, arquivos_pdf: list[tuple[str, bytes]], *, log=None) -> None:
    cabecalhos = response.headers
    tipo = cabecalhos.get("content-type", "").lower()
    disposicao = cabecalhos.get("content-disposition", "").lower()
    url = response.url
    if "pdf" not in tipo and ".pdf" not in url.lower() and ".pdf" not in disposicao:
        return
    try:
        conteudo = response.body()
    except Exception as erro:
        _log(log, f"Não foi possível ler a resposta PDF de {url}: {erro}")
        return
    if conteudo.startswith(b"%PDF"):
        arquivos_pdf.append((url, conteudo))
        _log(log, f"PDF capturado do Fraction ({len(conteudo)} bytes).")


def _salvar_conteudo_pdf_romaneio(
    conteudo: bytes,
    unidade: str,
    *,
    log=None,
) -> Path:
    if not conteudo.startswith(b"%PDF"):
        raise RuntimeError(
            f"O arquivo recebido do Fraction para a unidade '{unidade}' "
            "não é um PDF válido."
        )

    nome_unidade = re.sub(r"[^A-Za-z0-9._-]+", "_", unidade).strip("_.") or "unidade"
    RESULTADOS_ROMANEIOS.mkdir(parents=True, exist_ok=True)
    caminho = RESULTADOS_ROMANEIOS / (
        f"Romaneio_{nome_unidade}_{datetime.now():%Y-%m-%d_%H-%M-%S-%f}.pdf"
    )
    caminho.write_bytes(conteudo)
    _log(log, f"Romaneio salvo em {caminho} ({len(conteudo)} bytes).")
    return caminho


def preparar_lote_impressao_romaneios(
    arquivos_pdf: list[str | Path],
    *,
    pasta_temporaria: str | Path | None = None,
    log=None,
) -> Path:
    """Valida todos os PDFs e cria um arquivo único para a impressão em lote."""
    from pypdf import PdfReader, PdfWriter

    caminhos = [Path(caminho) for caminho in arquivos_pdf]
    if not caminhos:
        raise ValueError("Não há PDFs de romaneio para imprimir.")

    pasta_lote = Path(pasta_temporaria) if pasta_temporaria else RESULTADOS_ROMANEIOS
    pasta_lote.mkdir(parents=True, exist_ok=True)
    caminho_lote = pasta_lote / (
        f"Romaneios_Emissao_impressao_{datetime.now():%Y-%m-%d_%H-%M-%S-%f}.pdf"
    )
    escritor = PdfWriter()
    arquivos_abertos = []
    total_paginas = 0

    try:
        for caminho in caminhos:
            if not caminho.is_file():
                raise FileNotFoundError(f"PDF de romaneio não encontrado: {caminho}.")
            arquivo = caminho.open("rb")
            arquivos_abertos.append(arquivo)
            try:
                leitor = PdfReader(arquivo, strict=True)
                paginas = len(leitor.pages)
            except Exception as erro:
                raise ValueError(
                    f"O PDF '{caminho.name}' não pôde ser validado: {erro}"
                ) from erro
            if paginas <= 0:
                raise ValueError(f"O PDF '{caminho.name}' não possui páginas.")
            escritor.append(leitor)
            total_paginas += paginas

        with caminho_lote.open("wb") as destino:
            escritor.write(destino)

        with caminho_lote.open("rb") as arquivo_lote:
            leitor_lote = PdfReader(arquivo_lote, strict=True)
            paginas_lote = len(leitor_lote.pages)
        if paginas_lote != total_paginas:
            raise RuntimeError(
                "A validação do PDF combinado encontrou uma quantidade de páginas "
                f"inesperada ({paginas_lote} de {total_paginas})."
            )
    except Exception:
        caminho_lote.unlink(missing_ok=True)
        raise
    finally:
        escritor.close()
        for arquivo in arquivos_abertos:
            arquivo.close()

    _log(
        log,
        f"Lote de impressão preparado: {len(caminhos)} PDF(s), "
        f"{total_paginas} página(s), arquivo {caminho_lote}.",
    )
    return caminho_lote


def _localizar_sumatra_pdf() -> Path | None:
    candidatos: list[Path] = []
    encontrado_no_path = shutil.which("SumatraPDF.exe") or shutil.which("SumatraPDF")
    if encontrado_no_path:
        candidatos.append(Path(encontrado_no_path))

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidatos.append(Path(local_app_data) / "SumatraPDF" / "SumatraPDF.exe")

    for variavel in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        pasta_programas = os.environ.get(variavel)
        if pasta_programas:
            candidatos.append(Path(pasta_programas) / "SumatraPDF" / "SumatraPDF.exe")

    if os.name == "nt":
        try:
            import winreg

            locais = (
                (
                    winreg.HKEY_CURRENT_USER,
                    r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\SumatraPDF.exe",
                ),
                (
                    winreg.HKEY_LOCAL_MACHINE,
                    r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\SumatraPDF.exe",
                ),
            )
            for raiz, subchave in locais:
                try:
                    with winreg.OpenKey(raiz, subchave) as chave:
                        caminho, _ = winreg.QueryValueEx(chave, "")
                    candidatos.append(Path(str(caminho).strip('"')))
                except OSError:
                    continue
        except ImportError:
            pass

    vistos: set[str] = set()
    for candidato in candidatos:
        chave = os.path.normcase(str(candidato))
        if chave in vistos:
            continue
        vistos.add(chave)
        if candidato.is_file() and candidato.name.lower() == "sumatrapdf.exe":
            return candidato
    return None


def enviar_pdf_para_impressora_padrao(
    caminho_pdf: str | Path,
    *,
    log=None,
) -> None:
    """Envia o PDF ao SumatraPDF para impressão e aguarda o resultado do comando."""
    arquivo = Path(caminho_pdf)
    if not arquivo.is_file():
        raise FileNotFoundError(f"PDF do lote de impressão não encontrado: {arquivo}.")
    if os.name != "nt":
        raise RuntimeError(
            "O envio direto para a impressora padrão está disponível somente no Windows."
        )

    with arquivo.open("rb") as entrada:
        if entrada.read(4) != b"%PDF":
            raise ValueError(f"O arquivo '{arquivo.name}' não é um PDF válido.")

    sumatra = _localizar_sumatra_pdf()
    if sumatra is None:
        raise RuntimeError(
            "Não encontrei o SumatraPDF.exe. Instale o SumatraPDF ou inclua-o no PATH; "
            "o PDF não foi enviado à impressora."
        )

    try:
        resultado = subprocess.run(
            [str(sumatra), "-print-to-default", "-silent", str(arquivo.resolve())],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except subprocess.TimeoutExpired as erro:
        raise RuntimeError(
            "O SumatraPDF não confirmou o fim do envio em 180 segundos. "
            "Confira a fila do Windows antes de tentar novamente para evitar uma "
            "impressão duplicada."
        ) from erro
    except OSError as erro:
        raise RuntimeError(
            f"Não foi possível iniciar o SumatraPDF em '{sumatra}': {erro}."
        ) from erro

    if resultado.returncode != 0:
        detalhe = (resultado.stderr or resultado.stdout or "").strip()
        sufixo = f" Detalhe: {detalhe[:1000]}" if detalhe else ""
        raise RuntimeError(
            "O SumatraPDF retornou erro ao enviar o lote à impressora "
            f"(código {resultado.returncode}). Verifique o leitor e a impressora padrão."
            f"{sufixo}"
        )

    _log(
        log,
        f"Lote {arquivo.name} enviado pelo SumatraPDF à impressora padrão "
        f"(código de saída {resultado.returncode}).",
    )


def _salvar_download_romaneio(
    download,
    unidade: str,
    *,
    log=None,
) -> Path:
    nome_unidade = re.sub(r"[^A-Za-z0-9._-]+", "_", unidade).strip("_.") or "unidade"
    RESULTADOS_ROMANEIOS.mkdir(parents=True, exist_ok=True)
    caminho = RESULTADOS_ROMANEIOS / (
        f"Romaneio_{nome_unidade}_{datetime.now():%Y-%m-%d_%H-%M-%S-%f}.pdf"
    )

    try:
        download.save_as(caminho)
        with caminho.open("rb") as arquivo:
            assinatura = arquivo.read(4)
        if assinatura != b"%PDF":
            raise RuntimeError(
                "O Fraction iniciou um download, mas o arquivo recebido "
                f"não é um PDF. Nome informado pelo navegador: "
                f"{download.suggested_filename!r}."
            )
    except Exception:
        caminho.unlink(missing_ok=True)
        raise

    _log(
        log,
        f"Download do romaneio ({download.suggested_filename}) salvo em {caminho}.",
    )
    return caminho


def _salvar_pdf_romaneio(
    popup,
    arquivos_pdf: list[tuple[str, bytes]],
    unidade: str,
    *,
    log=None,
) -> Path:
    conteudo = arquivos_pdf[-1][1] if arquivos_pdf else b""
    if not conteudo and popup.url.startswith("data:application/pdf"):
        cabecalho, _, payload = popup.url.partition(",")
        conteudo = (
            base64.b64decode(payload)
            if ";base64" in cabecalho.lower()
            else unquote_to_bytes(payload)
        )
    if not conteudo and popup.url.startswith("blob:"):
        payload = popup.evaluate(
            """async () => {
                const resposta = await fetch(location.href);
                const bytes = new Uint8Array(await resposta.arrayBuffer());
                let binario = "";
                for (let inicio = 0; inicio < bytes.length; inicio += 0x8000) {
                    binario += String.fromCharCode(...bytes.subarray(inicio, inicio + 0x8000));
                }
                return btoa(binario);
            }"""
        )
        conteudo = base64.b64decode(payload)
    if not conteudo.startswith(b"%PDF"):
        raise RuntimeError(
            "O Fraction abriu uma nova guia, mas não foi possível capturar o PDF "
            f"do romaneio da unidade '{unidade}'. URL da guia: {popup.url}"
        )
    return _salvar_conteudo_pdf_romaneio(
        conteudo,
        unidade,
        log=log,
    )


def associar_destinos_fraction(
    grupos: list[dict],
    destinos: list[dict[str, str]],
    *,
    codigos_preferenciais: dict[str, str] | None = None,
) -> tuple[list[dict], list[str]]:
    """Liga cada rota a uma unidade Fraction e resolve nomes duplicados por código."""
    por_nome: dict[str, list[dict[str, str]]] = {}
    for destino in destinos:
        por_nome.setdefault(
            _normalizar_unidade_fraction(destino["nome"]), []
        ).append(destino)
    preferencias = {
        _normalizar_unidade_fraction(nome): str(codigo).strip()
        for nome, codigo in (codigos_preferenciais or {}).items()
    }
    associados = []
    pendentes = []
    for grupo in grupos:
        chave_unidade = _normalizar_unidade_fraction(grupo["ROTA"])
        correspondencias = por_nome.get(chave_unidade, [])
        contexto = f"unidade '{grupo['ROTA']}'"
        codigo_preferencial = preferencias.get(chave_unidade)
        if codigo_preferencial:
            correspondencias_preferenciais = [
                destino
                for destino in correspondencias
                if str(destino["codigo"]).strip() == codigo_preferencial
            ]
            if len(correspondencias_preferenciais) != 1:
                detalhe = (
                    "não encontrado"
                    if not correspondencias_preferenciais
                    else "duplicado"
                )
                pendentes.append(
                    f"{contexto}: código preferencial {codigo_preferencial} "
                    f"{detalhe} no catálogo"
                )
                continue
            correspondencias = correspondencias_preferenciais
        elif len(correspondencias) != 1:
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
    """Resolve a unidade atendente primeiro por cidade e depois por UF."""
    tabela = tabela_base.copy()
    coluna_destino = next(
        (coluna for coluna in tabela if _normalizar_rota(coluna) == "DESTINO"),
        None,
    )
    if coluna_destino is None:
        raise ValueError("A base de entrada não contém a coluna 'Destino'.")

    coluna_uf = next(
        (
            coluna
            for coluna in tabela
            if _normalizar_rota(coluna) in {"UF", "ESTADO"}
        ),
        None,
    )
    coluna_cidade = next(
        (
            coluna
            for coluna in tabela
            if _normalizar_rota(coluna) in {"CIDADE", "MUNICIPIO"}
        ),
        None,
    )

    if coluna_uf is None and coluna_cidade is None:
        raise ValueError(
            "A base precisa conter a coluna 'UF' ou 'Cidade' para aplicar as rotas."
        )

    try:
        rotas_por_uf, rotas_por_cidade = carregar_regras_rotas_localidade()
        codigos_preferenciais = carregar_codigos_preferenciais_unidades()
        unidades = carregar_destinos_fraction()
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as erro:
        raise ValueError(f"Não foi possível carregar a base de rotas: {erro}") from erro

    def resolver(linha: pd.Series) -> str:
        uf = _normalizar_uf(linha[coluna_uf]) if coluna_uf is not None else ""
        cidade_valor = linha[coluna_cidade] if coluna_cidade is not None else ""
        cidade = (
            _normalizar_rota(str(cidade_valor))
            if pd.notna(cidade_valor)
            else ""
        )
        rota_cidade = rotas_por_cidade.get(cidade, "")
        if rota_cidade:
            return rota_cidade
        rota_uf = rotas_por_uf.get(uf, "")
        if rota_uf:
            return rota_uf

        return "Sem rota"

    tabela["Rota"] = tabela.apply(resolver, axis=1)
    unidades_por_nome: dict[str, list[dict[str, str]]] = {}
    for unidade in unidades:
        unidades_por_nome.setdefault(
            _normalizar_unidade_fraction(unidade["nome"]), []
        ).append(unidade)
    def resolver_codigo_rota(rota: object) -> str:
        chave = _normalizar_unidade_fraction(rota)
        correspondencias = unidades_por_nome.get(chave, [])
        codigo_preferencial = codigos_preferenciais.get(chave)
        if codigo_preferencial:
            correspondencias = [
                unidade
                for unidade in correspondencias
                if str(unidade["codigo"]).strip() == codigo_preferencial
            ]
        return (
            str(correspondencias[0]["codigo"])
            if len(correspondencias) == 1
            else "Sem código"
        )

    tabela["Código da rota"] = tabela["Rota"].map(resolver_codigo_rota)
    encontradas = int((~tabela["Rota"].isin(["Sem rota", "Rota ambígua"])).sum())
    sem_rota = int(tabela["Rota"].eq("Sem rota").sum())
    _log(
        log,
        f"Prévia de rotas por UF/cidade: {encontradas}/{len(tabela)} pedido(s) "
        f"com unidade atribuída; {sem_rota} sem regra de localidade.",
    )
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
) -> dict[str, object]:
    destinos, pendentes = associar_destinos_fraction(
        grupos,
        carregar_destinos_fraction(),
        codigos_preferenciais=carregar_codigos_preferenciais_unidades(),
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
    arquivos_romaneios: list[str] = []
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
        arquivos_pdf: list[tuple[str, bytes]] = []
        capturar_pdf = lambda resposta: _capturar_resposta_pdf(
            resposta,
            arquivos_pdf,
            log=log,
        )
        contexto = page.context
        contexto.on("response", capturar_pdf)
        popup = None
        download_recebido = None
        saida_recebida = Event()

        def registrar_download(download) -> None:
            nonlocal download_recebido
            download_recebido = download
            saida_recebida.set()

        def registrar_popup(nova_pagina) -> None:
            nonlocal popup
            popup = nova_pagina
            nova_pagina.on("download", registrar_download)
            saida_recebida.set()

        page.on("download", registrar_download)
        page.on("popup", registrar_popup)
        try:
            page.get_by_role("button", name="Processar lista").click(
                timeout=120_000
            )

            prazo = time.monotonic() + 600
            while not saida_recebida.is_set():
                restante = prazo - time.monotonic()
                if restante <= 0:
                    raise TimeoutError(
                        "O Fraction não iniciou o download do PDF nem abriu "
                        f"uma nova guia para a unidade '{rota_unidade}'."
                    )
                page.wait_for_timeout(min(250, max(1, int(restante * 1000))))

            if download_recebido is not None:
                _log(log, "Download automático do PDF do romaneio detectado.")
                caminho_pdf = _salvar_download_romaneio(
                    download_recebido,
                    rota_unidade,
                    log=log,
                )
            else:
                _log(log, "Nova guia do romaneio aberta pelo Fraction.")
                try:
                    popup.wait_for_load_state("load", timeout=120_000)
                except TimeoutError:
                    _log(log, "A guia do PDF não sinalizou load; usando a resposta capturada.")
                if download_recebido is not None:
                    _log(log, "Download automático do PDF detectado na nova guia.")
                    caminho_pdf = _salvar_download_romaneio(
                        download_recebido,
                        rota_unidade,
                        log=log,
                    )
                else:
                    caminho_pdf = _salvar_pdf_romaneio(
                        popup,
                        arquivos_pdf,
                        rota_unidade,
                        log=log,
                    )
            arquivos_romaneios.append(str(caminho_pdf))
            _log(log, f"Remessa da unidade {rota_unidade} processada e PDF salvo.")
        finally:
            page.remove_listener("download", registrar_download)
            page.remove_listener("popup", registrar_popup)
            contexto.remove_listener("response", capturar_pdf)
            if popup is not None and not popup.is_closed():
                popup.close()

    _log(log, "Todas as unidades roteadas foram processadas no Fraction.")

    return {"lotes": total_lotes, "arquivos_pdf": arquivos_romaneios}


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
    codigos_preferenciais: dict[str, str] | None = None,
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
    if codigos_preferenciais is None:
        codigos_preferenciais = carregar_codigos_preferenciais_unidades()
    grupos, destinos_pendentes = associar_destinos_fraction(
        grupos,
        carregar_destinos_fraction(),
        codigos_preferenciais=codigos_preferenciais,
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
            context = browser.new_context(accept_downloads=True)
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
                processamento = processar_manifestos(page, grupos, log=log)
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
        "lotes_manifesto": processamento["lotes"],
        "arquivos_pdf": processamento["arquivos_pdf"],
    }
    _log(
        log,
        f"Etapa de Manifesto concluída: {processamento['lotes']} lote(s), "
        f"{len(processamento['arquivos_pdf'])} PDF(s) salvo(s), {total_ctes} CTE(s), "
        f"{time.perf_counter() - inicio_execucao:.1f}s.",
    )
    return resultado
