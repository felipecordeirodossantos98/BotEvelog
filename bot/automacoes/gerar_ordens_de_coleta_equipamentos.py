from __future__ import annotations

import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Callable

import pandas as pd
from pypdf import PdfReader

from automacoes.gerar_ordens_de_coleta_malotes import (
    abrir_solicitacao_coleta,
    capturar_unidade_coletora,
    colar_sem_tab,
    digitar,
    gerar_coleta_e_capturar_ordem,
    localizar_destinatario,
    localizar_remetente,
    realizar_login,
)
from servicos.fraction import fechar_recurso_playwright_seguro
from playwright.sync_api import sync_playwright

from utils.config import (
    ARQUIVO_EMAILS_UNIDADES,
    FRACTION_PASSWORD,
    FRACTION_USER,
    RESULTADOS_ORDENS_EQUIPAMENTOS,
    URL_FRACTION,
)

TIMEOUT = 30_000
MAX_TENTATIVAS = 5

CONTA_CORRENTE = "0149333"
OBSERVACAO = "Equipamento"
VOLUMES = "1"
PESO = "1,00"
VALOR_COLETA = "0,00"
MODALIDADE = "ECONOMICO"

PASTA_RESULTADOS = RESULTADOS_ORDENS_EQUIPAMENTOS


class NotaFiscalInvalida(ValueError):
    pass


def texto_limpo(valor: object) -> str:
    if valor is None:
        return ""
    texto = str(valor).strip()
    return "" if texto.lower() == "nan" else texto


def somente_digitos(valor: object) -> str:
    return "".join(c for c in texto_limpo(valor) if c.isdigit())


def normalizar_espacos(texto: str) -> str:
    return re.sub(r"\s+", " ", texto).strip()


def carregar_emails_unidades() -> dict[str, str]:
    import json

    if not ARQUIVO_EMAILS_UNIDADES.exists():
        return {}

    with ARQUIVO_EMAILS_UNIDADES.open("r", encoding="utf-8") as arquivo:
        dados = json.load(arquivo)

    emails: dict[str, str] = {}
    for unidade, valores in dados.items():
        chave = normalizar_espacos(texto_limpo(unidade)).upper()
        if isinstance(valores, list):
            lista = [texto_limpo(v) for v in valores if texto_limpo(v)]
            emails[chave] = "; ".join(lista)
        elif isinstance(valores, str):
            emails[chave] = texto_limpo(valores)
    return emails


def extrair_texto_pdf(caminho: Path) -> str:
    reader = PdfReader(str(caminho))
    if not reader.pages:
        raise NotaFiscalInvalida("O PDF não possui páginas.")

    paginas = [pagina.extract_text() or "" for pagina in reader.pages]
    texto = "\n".join(paginas)

    if not texto.strip():
        raise NotaFiscalInvalida(
            "Não foi possível extrair texto do PDF. A NF pode estar digitalizada como imagem."
        )

    return texto


def extrair_numero_nota(texto: str) -> str:
    padroes = (
        r"(?:N[º°o]|NO)\s*([0-9]{1,20})",
        r"([0-9]{1,20})\s*N[º°o]",
    )
    for padrao in padroes:
        resultado = re.search(padrao, texto, flags=re.IGNORECASE)
        if resultado:
            return resultado.group(1)
    raise NotaFiscalInvalida("Número da NF (Nº) não encontrado.")


def extrair_serie(texto: str) -> str:
    resultado = re.search(r"S[ÉE]RIE\s*([A-Za-z0-9]+)", texto, flags=re.IGNORECASE)
    if resultado:
        return resultado.group(1)

    resultado = re.search(r"S[ÉE]RIE\s*\n\s*([A-Za-z0-9]+)", texto, flags=re.IGNORECASE)
    if resultado:
        return resultado.group(1)

    raise NotaFiscalInvalida("Série da NF não encontrada.")


def extrair_cnpj_do_trecho(trecho: str) -> str | None:
    padroes = (
        r"\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}",
        r"(?<!\d)\d{14}(?!\d)",
    )
    for padrao in padroes:
        resultado = re.search(padrao, trecho)
        if resultado:
            valor = somente_digitos(resultado.group(0))
            if len(valor) == 14:
                return valor
    return None


def extrair_cnpj_remetente_destinatario(texto: str) -> tuple[str, str]:
    marcadores_emitente = list(re.finditer(
        r"IDENTIFICAÇÃO DO EMITENTE", texto, flags=re.IGNORECASE
    ))
    marcador_destinatario = re.search(
        r"DESTINATÁRIO\s*/\s*REMETENTE", texto, flags=re.IGNORECASE
    )
    marcador_fatura = re.search(
        r"FATURA/DUPLICATA", texto, flags=re.IGNORECASE
    )

    if not marcadores_emitente or not marcador_destinatario:
        raise NotaFiscalInvalida("Blocos de emitente/destinatário não encontrados na NF.")

    # Alguns extratores colocam o bloco do destinatário antes do bloco do
    # emitente. Por isso usamos a última ocorrência do título do emitente.
    marcador_emitente = marcadores_emitente[-1]
    trecho_emitente = texto[marcador_emitente.start():]

    inicio_destinatario = marcador_destinatario.start()
    fim_destinatario = marcador_fatura.start() if marcador_fatura else marcador_emitente.start()
    trecho_destinatario = texto[inicio_destinatario:fim_destinatario]

    cnpj_remetente = extrair_cnpj_do_trecho(trecho_emitente)
    cnpj_destinatario = extrair_cnpj_do_trecho(trecho_destinatario)

    if not cnpj_remetente:
        raise NotaFiscalInvalida("CNPJ do emitente/remetente não encontrado.")
    if not cnpj_destinatario:
        raise NotaFiscalInvalida("CNPJ/CPF do destinatário não encontrado.")

    return cnpj_remetente, cnpj_destinatario


def extrair_descricao_produto(texto: str) -> str:
    # Em alguns DANFEs o extrator de PDF reorganiza a tabela e coloca a
    # linha do produto antes do cabeçalho "DADOS DOS PRODUTOS / SERVIÇOS".
    # Procuramos então pelo padrão estrutural da linha: código do produto
    # (normalmente 10 dígitos), descrição textual e NCM/CST em seguida.
    padrao = re.search(
        r"\b\d{10}\s*([A-ZÀ-ÿ][\s\S]{1,300}?)(?=\s*\d{8}\s*\d{3}\b)",
        texto,
        flags=re.IGNORECASE,
    )
    if not padrao:
        raise NotaFiscalInvalida(
            "Descrição dos produtos/serviços não encontrada na NF."
        )

    descricao = normalizar_espacos(padrao.group(1))
    if not descricao:
        raise NotaFiscalInvalida("Descrição dos produtos/serviços está vazia.")

    return descricao


def extrair_equipamento(descricao: str) -> str:
    # Para a regra atual, o nome do equipamento é o primeiro termo da descrição.
    equipamento = re.split(r"\s*\(", descricao, maxsplit=1)[0].strip()
    equipamento = equipamento.split()[0] if equipamento.split() else ""
    equipamento = equipamento.strip("-–—")

    if not equipamento:
        raise NotaFiscalInvalida("Não foi possível identificar o nome do equipamento.")

    return equipamento


def extrair_valor_total_nota(texto: str) -> str:
    padroes = (
        r"([\d.]+,\d{2})\s+VALOR TOTAL DA NOTA",
        r"VALOR TOTAL DA NOTA\s*[:\-]?\s*([\d.]+,\d{2})",
    )
    for padrao in padroes:
        resultado = re.search(padrao, texto, flags=re.IGNORECASE | re.DOTALL)
        if resultado:
            return resultado.group(1)
    raise NotaFiscalInvalida("Valor total da nota não encontrado.")


def extrair_nf(caminho: Path, *, nome_arquivo: str | None = None) -> dict:
    texto = extrair_texto_pdf(caminho)
    numero_nota = extrair_numero_nota(texto)
    serie = extrair_serie(texto)
    descricao = extrair_descricao_produto(texto)
    cnpj_remetente, cnpj_destinatario = extrair_cnpj_remetente_destinatario(texto)
    valor_nota = extrair_valor_total_nota(texto)
    equipamento = extrair_equipamento(descricao)
    nome_arquivo = texto_limpo(nome_arquivo) or caminho.name
    numero_pedido = Path(nome_arquivo).stem

    return {
        "NUMERO_PEDIDO": numero_pedido,
        "EQUIPAMENTO": equipamento,
        "CONTA_CORRENTE": CONTA_CORRENTE,
        "OBSERVACAO": OBSERVACAO,
        "CONTEUDO": equipamento,
        "VOLUMES": VOLUMES,
        "PESO": PESO,
        "VALOR_COLETA": VALOR_COLETA,
        "NUMERO_NOTA": numero_nota,
        "SERIE": serie,
        "VALOR_NOTA": valor_nota,
        "MODALIDADE": MODALIDADE,
        "CNPJ_REMETENTE": cnpj_remetente,
        "CNPJ_DESTINATARIO": cnpj_destinatario,
    }


def preparar_pdfs(arquivos) -> tuple[pd.DataFrame, list[str]]:
    registros: list[dict] = []
    erros: list[str] = []

    for arquivo in arquivos:
        try:
            nome = texto_limpo(getattr(arquivo, "name", "arquivo.pdf"))
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as temporario:
                caminho_temporario = Path(temporario.name)
                temporario.write(arquivo.getvalue())
            registro = extrair_nf(caminho_temporario, nome_arquivo=nome)
            caminho_temporario.unlink(missing_ok=True)
            registros.append(registro)
        except Exception as erro:
            try:
                caminho_temporario.unlink(missing_ok=True)
            except Exception:
                pass
            erros.append(f"{getattr(arquivo, 'name', 'PDF')}: {erro}")

    return pd.DataFrame(registros), erros


def montar_preview_saida(df_nf: pd.DataFrame, *, data_geracao: str | None = None) -> pd.DataFrame:
    data = data_geracao or datetime.now().strftime("%d/%m/%Y")
    emails = carregar_emails_unidades()

    linhas = []
    for _, item in df_nf.iterrows():
        linhas.append({
            "DATA": data,
            "SIGLA": "",
            "PEDIDO": item["NUMERO_PEDIDO"],
            "CTE": "",
            "VINCULAR/ ACERTO": "",
            "EQUIPAMENTO": item["EQUIPAMENTO"],
            "ORDEM": "",
            "REVERSA/AUTO": "AUTORIZADO",
            "DT DE AGENDAMENTO": f"IMEDIATO {data}",
            "UNIDADES": "",
            "E-MAIL": "",
        })

    return pd.DataFrame(linhas, columns=[
        "DATA", "SIGLA", "PEDIDO", "CTE", "VINCULAR/ ACERTO",
        "EQUIPAMENTO", "ORDEM", "REVERSA/AUTO", "DT DE AGENDAMENTO",
        "UNIDADES", "E-MAIL",
    ])


def montar_preview_completo(df_nf: pd.DataFrame, *, data_geracao: str | None = None) -> pd.DataFrame:
    data = data_geracao or datetime.now().strftime("%d/%m/%Y")
    linhas = []
    for _, item in df_nf.iterrows():
        linhas.append({
            **item.to_dict(),
            "DATA": data,
            "SIGLA": "",
            "PEDIDO": item["NUMERO_PEDIDO"],
            "CTE": "",
            "VINCULAR/ ACERTO": "",
            "ORDEM": "",
            "REVERSA/AUTO": "AUTORIZADO",
            "DT DE AGENDAMENTO": f"IMEDIATO {data}",
            "UNIDADES": "",
            "E-MAIL": "",
        })
    return pd.DataFrame(linhas)


# A partir daqui ficam os componentes específicos da execução no Fraction.
# Os componentes comuns de login, navegação e preenchimento são reutilizados
# diretamente de gerar_ordens_de_coleta.py.


def selecionar_modalidade_economico(page):
    modalidade = page.locator('[id="form_emissao:modalidadeSelect"]')
    modalidade.wait_for(state="visible", timeout=TIMEOUT)
    caixa = modalidade.bounding_box()
    if caixa is None:
        raise RuntimeError("Não foi possível localizar o campo Modalidade.")
    page.mouse.click(caixa["x"] + caixa["width"] - 10, caixa["y"] + caixa["height"] / 2)
    page.wait_for_timeout(600)
    lista = page.locator('[id="form_emissao:modalidadeSelect_items"]:visible')
    lista.wait_for(state="visible", timeout=10_000)
    lista.get_by_role("option", name=MODALIDADE, exact=True).click()
    page.wait_for_timeout(700)


def preencher_formulario_equipamento(page, item: dict) -> None:
    # A ordem dos campos e os seletores comuns seguem a automação original
    # de Ordens de Coleta. Apenas os valores específicos de Equipamentos
    # (pedido pelo nome do PDF e modalidade ECONOMICO) são adicionados.
    digitar(
        page.get_by_role(
            "textbox",
            name="Conta Corrente",
            exact=True,
        ),
        CONTA_CORRENTE,
    )

    digitar(
        page.locator(
            '[id="form_emissao:observacaoArea"]'
        ),
        OBSERVACAO,
    )

    digitar(
        page.locator(
            '[id="form_emissao:conteudoArea"]'
        ),
        item["CONTEUDO"],
    )

    # Seletores de Peso, Valor da Coleta e Número da NF iguais aos validados
    # na automação original.
    digitar(
        page.get_by_role(
            "textbox",
            name="ALT+7",
            exact=True,
        ),
        PESO,
    )

    digitar(
        page.get_by_role(
            "textbox",
            name="ALT+8",
            exact=True,
        ),
        VALOR_COLETA,
    )

    digitar(
        page.get_by_role(
            "textbox",
            name="ALT+9",
            exact=True,
        ),
        item["NUMERO_NOTA"],
    )

    # Reproduz exatamente o tratamento da automação original: Volume e Série
    # são reforçados somente quando seus IDs estão presentes no formulário.
    campo_volume = page.locator(
        '[id="form_emissao:quantidadeVolume_input"]'
    )

    if campo_volume.count() > 0:
        digitar(
            campo_volume,
            VOLUMES,
        )

    campo_serie = page.locator(
        '[id="form_emissao:bonfs:0:nota_serie"]'
    )

    if campo_serie.count() > 0:
        digitar(
            campo_serie,
            item["SERIE"],
        )

    digitar(
        page.locator(
            '[id="form_emissao:bonfs:0:nota_valor"]'
        ),
        item["VALOR_NOTA"],
    )

    # Campo específico da variação Equipamentos: nome do arquivo PDF, sem
    # extensão, como Número do Pedido.
    digitar(
        page.locator(
            '[id="form_emissao:bonfs:0:nota_numPedido"]'
        ),
        item["NUMERO_PEDIDO"],
    )

    # Reutiliza as mesmas funções e a mesma ordem de preenchimento dos CNPJs.
    colar_sem_tab(
        page,
        localizar_remetente(page),
        item["CNPJ_REMETENTE"],
    )

    colar_sem_tab(
        page,
        localizar_destinatario(page),
        item["CNPJ_DESTINATARIO"],
    )

    # Modalidade é selecionada por último, tal como no fluxo original, para
    # permitir que o Fraction atualize os dados vinculados aos CNPJs.
    selecionar_modalidade_economico(page)

    page.wait_for_timeout(4_000)

    modalidade_exibida = page.locator(
        '[id="form_emissao:modalidadeSelect_label"]'
    ).inner_text()

    if modalidade_exibida.strip().upper() != MODALIDADE:
        raise RuntimeError(
            f"Modalidade inesperada: {modalidade_exibida}"
        )


def reiniciar_sessao(page, usuario: str, senha: str, log: Callable[[str], None]) -> None:
    sair = page.get_by_role("link", name="Sair")
    sair.wait_for(state="visible", timeout=TIMEOUT)
    sair.click(timeout=TIMEOUT)
    page.get_by_role("textbox", name="Usuário", exact=True).wait_for(
        state="visible", timeout=TIMEOUT
    )
    realizar_login(page, usuario, senha, log)


def criar_planilha_resultado(itens: list[dict]) -> Path | None:
    if not itens:
        return None
    PASTA_RESULTADOS.mkdir(parents=True, exist_ok=True)
    colunas = [
        "DATA", "SIGLA", "PEDIDO", "CTE", "VINCULAR/ ACERTO",
        "EQUIPAMENTO", "ORDEM", "REVERSA/AUTO", "DT DE AGENDAMENTO",
        "UNIDADES", "E-MAIL",
    ]
    df = pd.DataFrame(itens, columns=colunas)
    caminho = PASTA_RESULTADOS / f"ordens_equipamentos_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
    df.to_excel(caminho, index=False, engine="openpyxl")
    return caminho


def executar_automacao(
    df_nf: pd.DataFrame,
    headless: bool,
    continuar_em_erro: bool,
    log: Callable[[str], None],
) -> dict:
    if df_nf.empty:
        raise ValueError("Nenhuma NF válida para processar.")
    if not FRACTION_USER or not FRACTION_PASSWORD or not URL_FRACTION:
        raise ValueError("FRACTION_USER, FRACTION_PASSWORD e URL_FRACTION precisam estar configurados.")

    emails_unidades = carregar_emails_unidades()
    resultados: list[dict] = []
    detalhes: list[dict] = []
    sucessos = falhas = indeterminados = 0

    with sync_playwright() as p:
        navegador = contexto = page = None

        def fechar_sessao():
            nonlocal navegador, contexto, page
            fechar_recurso_playwright_seguro(contexto, log)
            fechar_recurso_playwright_seguro(navegador, log)
            navegador = contexto = page = None

        def abrir_sessao():
            nonlocal navegador, contexto, page
            fechar_sessao()
            navegador = p.chromium.launch(headless=headless, slow_mo=120 if not headless else 0)
            contexto = navegador.new_context(permissions=["clipboard-read", "clipboard-write"])
            page = contexto.new_page()
            page.set_default_timeout(TIMEOUT)
            realizar_login(page, FRACTION_USER, FRACTION_PASSWORD, log)

        try:
            abrir_sessao()
            total = len(df_nf)
            for posicao, (_, linha) in enumerate(df_nf.iterrows(), 1):
                item = linha.to_dict()
                pedido = texto_limpo(item["NUMERO_PEDIDO"])
                sucesso = False
                indeterminado = False
                ultima_mensagem = ""
                ultimo_erro = None
                log(f"[{posicao}/{total}] Pedido {pedido} - Equipamento: {item['EQUIPAMENTO']}")

                for tentativa in range(1, MAX_TENTATIVAS + 1):
                    geracao_iniciada = False
                    try:
                        log(f"Tentativa {tentativa}/{MAX_TENTATIVAS}")
                        abrir_solicitacao_coleta(page, log)
                        preencher_formulario_equipamento(page, item)
                        unidade = capturar_unidade_coletora(page)
                        email = emails_unidades.get(normalizar_espacos(unidade).upper(), "")
                        if email:
                            log(f"E-mail da unidade: {email}")
                        else:
                            log(f"Aviso: e-mail não encontrado para {unidade}")

                        geracao_iniciada = True
                        ordem = gerar_coleta_e_capturar_ordem(page)
                        data = datetime.now().strftime("%d/%m/%Y")
                        resultados.append({
                            "DATA": data,
                            "SIGLA": "",
                            "PEDIDO": pedido,
                            "CTE": "",
                            "VINCULAR/ ACERTO": "",
                            "EQUIPAMENTO": item["EQUIPAMENTO"],
                            "ORDEM": ordem,
                            "REVERSA/AUTO": "AUTORIZADO",
                            "DT DE AGENDAMENTO": f"IMEDIATO {data}",
                            "UNIDADES": unidade,
                            "E-MAIL": email,
                        })
                        detalhes.append({"PEDIDO": pedido, "ORDEM": ordem, "SITUAÇÃO": "AUTORIZADO"})
                        sucessos += 1
                        sucesso = True
                        log(f"Ordem gerada: {ordem}")
                        break
                    except Exception as erro:
                        ultimo_erro = erro
                        ultima_mensagem = f"{type(erro).__name__}: {erro}"
                        log(f"Falha na tentativa {tentativa}/{MAX_TENTATIVAS}: {ultima_mensagem}")
                        if geracao_iniciada:
                            indeterminado = True
                            log("A falha ocorreu após iniciar a geração. O item não será repetido automaticamente.")
                            break
                        if tentativa < MAX_TENTATIVAS:
                            try:
                                reiniciar_sessao(page, FRACTION_USER, FRACTION_PASSWORD, log)
                            except Exception:
                                try:
                                    abrir_sessao()
                                except Exception as restart_error:
                                    ultimo_erro = restart_error
                                    ultima_mensagem = f"Falha ao restabelecer o Fraction: {type(restart_error).__name__}: {restart_error}"
                                    break

                if not sucesso:
                    if indeterminado:
                        indeterminados += 1
                        situacao = "STATUS INDETERMINADO"
                    else:
                        falhas += 1
                        situacao = "FALHA"
                    detalhes.append({"PEDIDO": pedido, "ORDEM": "", "SITUAÇÃO": situacao, "MENSAGEM": ultima_mensagem})
                    if not continuar_em_erro:
                        raise RuntimeError(ultima_mensagem) from ultimo_erro
                    try:
                        reiniciar_sessao(page, FRACTION_USER, FRACTION_PASSWORD, log)
                    except Exception:
                        try:
                            abrir_sessao()
                        except Exception:
                            log("Não foi possível restabelecer o Fraction após a falha.")
                            break
        finally:
            fechar_sessao()

    caminho = criar_planilha_resultado(resultados)
    return {
        "total": len(df_nf),
        "sucessos": sucessos,
        "falhas": falhas,
        "indeterminados": indeterminados,
        "detalhes": detalhes,
        "arquivo_resultado": str(caminho) if caminho else None,
    }
