from __future__ import annotations

from datetime import datetime
import re
import unicodedata
from pathlib import Path
from typing import Callable

import pandas as pd
from playwright.sync_api import (
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from servicos.fraction import (
    executar_com_retentativas_fraction,
    fechar_recurso_playwright_seguro,
    login_fraction,
    preencher_observacao_fraction,
    pesquisar_e_contar_operacao_historico_fraction,
    recuperar_sessao_fraction,
    testar_conexao_fraction_isolada,
)
from utils.config import (
    ARQUIVO_PEDIDOS_COM_TICKET,
    FRACTION_PASSWORD,
    FRACTION_USER,
    HEADLESS,
    PERFIL_ZENDESK,
    RESULTADOS_TICKETS_ZENDESK,
    URL_FRACTION,
    URL_ZENDESK,
    ZENDESK_PASSWORD,
    ZENDESK_USER,
    variaveis_opcionais_faltantes,
)
from utils.estado import RegistroTexto


PASTA_RESULTADOS = RESULTADOS_TICKETS_ZENDESK

COLUNAS_OBRIGATORIAS = {"Codigo", "Pedido", "Status", "Descricao"}

MAPEAMENTO_STATUS_ZENDESK = {
    "MUDOU-SE": "Mudou-se",
    "DESTINATARIO DESCONHECIDO": "Destinatário desconhecido",
    "NUMERO NAO LOCALIZADO": "Número não localizado",
    "AUSENTE 2": "Ausente",
    "AUSENTE 3": "Ausente",
    "ENDERECO NAO LOCALIZADO": "Endereço não localizado",
    "CEP ERRADO": "Cep não atendido",
    "RESTRICAO DE ACESSO / MOVIMENTACAO": "Área de risco",
    "ENDERECO EM ZONA RURAL": "Área rural",
    "RECUSADO": "Recusou-se a receber",
}

MAPEAMENTO_STATUS_ASSUNTO = {
    "MUDOU-SE": "MUDOU-SE",
    "DESTINATARIO DESCONHECIDO": "DESTINATÁRIO DESCONHECIDO",
    "NUMERO NAO LOCALIZADO": "NÚMERO NÃO LOCALIZADO",
    "AUSENTE 2": "AUSENTE 2",
    "AUSENTE 3": "AUSENTE 3",
    "ENDERECO NAO LOCALIZADO": "ENDEREÇO NÃO LOCALIZADO",
    "CEP ERRADO": "CEP ERRADO",
    "RESTRICAO DE ACESSO / MOVIMENTACAO": "RESTRIÇÃO DE ACESSO / MOVIMENTAÇÃO",
    "ENDERECO EM ZONA RURAL": "ENDEREÇO EM ZONA RURAL",
    "RECUSADO": "RECUSADO",
}

MAPEAMENTO_TEXTO_TICKET = {
    "MUDOU-SE": (
        '"Prezados, remessa teve ocorrência de "MUDOU-SE", '
        'favor confirmar os dados de endereço de entrega, mais ponto de '
        'referência e telefone ativo para contato."'
    ),

    "DESTINATARIO DESCONHECIDO": (
        '"Prezados, remessa teve ocorrência de "DESTINATARIO DESCONHECIDO", '
        'favor confirmar os dados de endereço de entrega, mais ponto de '
        'referência e telefone ativo para contato."'
    ),

    "NUMERO NAO LOCALIZADO": (
        '"Prezados, remessa teve ocorrência de "NÚMERO NÃO LOCALIZADO", '
        'favor confirmar os dados de endereço de entrega, mais ponto de '
        'referência e telefone ativo para contato."'
    ),

    "AUSENTE 2": (
        '"Prezados, remessa em questão teve a sua ocorrência de "AUSENTE 2", '
        'favor acionar ao destinatário para podermos evitar que a terceira e ultima '
        'tentativa de entrega resulte em falha."'
    ),

    "AUSENTE 3": (
        '"AUSENTE 3 - Prezados o pedido em questão teve a sua terceira ocorrência '
        'de ausente, favor verificar com o cliente se há interesse em retirar o '
        'volume na unidade?"'
    ),

    "ENDERECO NAO LOCALIZADO": (
        '"Prezados, remessa teve ocorrência de "ENDERECO NAO LOCALIZADO", '
        'favor confirmar os dados de endereço de entrega, mais ponto de '
        'referência e telefone ativo para contato."'
    ),

    "CEP ERRADO": (
        '"Prezados, remessa teve ocorrência de "CEP ERRADO", '
        'favor confirmar os dados de endereço de entrega, mais ponto de '
        'referência e telefone ativo para contato."'
    ),

    "RESTRICAO DE ACESSO / MOVIMENTACAO": (
        '"Prezados, remessa em questão foi classificada como "ÁREA DE RISCO", '
        'por gentileza solicitar ao cliente dados de endereço alternativo para '
        'finalização dessa entrega."'
    ),

    "ENDERECO EM ZONA RURAL": (
        '"Prezados, remessa foi dada como "ZONA RURAL" favor confirmar com o '
        'cliente se ele possuí dados de endereço alternativo em perímetro urbano '
        'para finalização dessa entrega?"'
    ),

    "RECUSADO": (
        '"Prezados, remessa teve ocorrência de "RECUSADO", '
        'favor confirmar os dados de endereço de entrega, mais ponto de '
        'referência e telefone ativo para contato."'
    ),
}


def normalizar_texto(valor) -> str:
    if pd.isna(valor):
        return ""
    texto = unicodedata.normalize("NFKD", str(valor).strip())
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", texto).upper()


COLUNAS_CANONICAS = {
    "CODIGO": "Codigo",
    "PEDIDO": "Pedido",
    "STATUS": "Status",
    "DESCRICAO": "Descricao",
}


def padronizar_colunas(
    df: pd.DataFrame,
) -> pd.DataFrame:
    df = df.copy()
    renomear = {}

    for coluna in df.columns:
        nome_canonico = COLUNAS_CANONICAS.get(
            normalizar_texto(coluna)
        )

        if nome_canonico:
            renomear[coluna] = nome_canonico

    return df.rename(
        columns=renomear
    )


def carregar_planilha_zendesk(
    arquivo,
) -> pd.DataFrame:
    arquivo.seek(0)

    df = pd.read_excel(
        arquivo,
        header=1,
    )

    arquivo.seek(0)

    df = padronizar_colunas(df)
    validar_dataframe(df)

    return df


def valor_para_texto(valor) -> str:
    if pd.isna(valor):
        return ""
    if isinstance(valor, float) and valor.is_integer():
        return str(int(valor))
    return str(valor).strip()


def remover_dois_final(pedido) -> str:
    """
    Remove somente um dígito 2 quando ele estiver no final do Pedido.

    Exemplo:
    7490658792 -> 749065879
    """
    pedido = valor_para_texto(pedido)

    if pedido.endswith("2"):
        return pedido[:-1]

    return pedido


def validar_dataframe(df: pd.DataFrame) -> None:
    faltantes = COLUNAS_OBRIGATORIAS - set(df.columns)
    if faltantes:
        raise ValueError(
            "A planilha precisa conter Codigo, Pedido, Status e Descricao. "
            f"Ausentes: {sorted(faltantes)}"
        )


def _descricao_ausente_por_quantidade(quantidade: int) -> str:
    """Converte a quantidade real de AUSENTE para a descrição da fila."""
    if quantidade >= 3:
        return "AUSENTE 3"

    if quantidade == 2:
        return "AUSENTE 2"

    return "AUSENTE"


def validar_ausentes_no_fraction(
    df_entrada: pd.DataFrame,
    log: Callable[[str], None],
) -> pd.DataFrame:
    """
    Pré-automação da fila Zendesk.

    Somente linhas com Status CUSTODIA e Descricao AUSENTE são consultadas.
    Para cada código, conta quantas ocorrências AUSENTE existem na coluna
    Operação da tabela Histórico do Fraction e atualiza o DataFrame para:

      0 ou 1 ocorrência -> AUSENTE
      2 ocorrências      -> AUSENTE 2
      3 ou mais          -> AUSENTE 3

    Se um código não puder ser validado, sua descrição permanece AUSENTE,
    portanto ele não entra indevidamente na fila de tickets.
    """
    df = padronizar_colunas(
        df_entrada
    ).copy()
    validar_dataframe(df)

    candidatos = df.index[
        df.apply(
            lambda linha: (
                normalizar_texto(
                    linha["Status"]
                )
                == "CUSTODIA"
                and normalizar_texto(
                    linha["Descricao"]
                )
                == "AUSENTE"
            ),
            axis=1,
        )
    ].tolist()

    if not candidatos:
        log(
            "Nenhum pedido CUSTODIA / AUSENTE precisa de validação no Fraction."
        )
        return df

    log(
        "===== PRÉ-FASE: VALIDAR AUSENTES NO FRACTION ====="
    )
    log(
        f"{len(candidatos)} pedido(s) CUSTODIA / AUSENTE serão verificados."
    )

    faltantes_fraction = variaveis_opcionais_faltantes(
        "gerar_tickets_zendesk"
    )

    if faltantes_fraction:
        log(
            "Fraction não configurado para validar AUSENTE. "
            "Os pedidos permanecerão como AUSENTE e não entrarão na fila. "
            "Variável(is) faltante(s): "
            + ", ".join(faltantes_fraction)
        )
        return df

    uf = FRACTION_USER
    sf = FRACTION_PASSWORD

    with sync_playwright() as p:
        browser = None
        context = None
        page = None

        def fechar_sessao() -> None:
            nonlocal browser, context, page

            fechar_recurso_playwright_seguro(
                context,
                log,
            )
            fechar_recurso_playwright_seguro(
                browser,
                log,
            )

            browser = None
            context = None
            page = None

        def abrir_sessao() -> None:
            nonlocal browser, context, page

            fechar_sessao()

            log(
                "Abrindo Chromium para validar histórico de AUSENTE."
            )

            browser = p.chromium.launch(
                headless=HEADLESS,
                slow_mo=500,
                args=["--start-maximized"],
            )

            context = browser.new_context(
                no_viewport=True,
            )

            page = context.new_page()

            login_fraction(
                page,
                uf,
                sf,
                log,
            )

        def recuperar_sessao() -> None:
            recuperar_sessao_fraction(
                page,
                log,
                login_callback=lambda: login_fraction(
                    page,
                    uf,
                    sf,
                    log,
                ),
            )

            page.wait_for_timeout(2_000)

        try:
            abrir_sessao()

            for pos, indice in enumerate(
                candidatos,
                start=1,
            ):
                codigo = valor_para_texto(
                    df.at[indice, "Codigo"]
                )

                if not codigo:
                    log(
                        f"[Ausente {pos}/{len(candidatos)}] Código vazio. "
                        "Descrição mantida como AUSENTE."
                    )
                    continue

                log(
                    f"[Ausente {pos}/{len(candidatos)}] Código {codigo}"
                )

                try:
                    quantidade = executar_com_retentativas_fraction(
                        lambda: pesquisar_e_contar_operacao_historico_fraction(
                            page,
                            codigo,
                            "AUSENTE",
                            log,
                        ),
                        recuperar_sessao,
                        log,
                        descricao=(
                            f"Histórico do código {codigo}"
                        ),
                        tentativas=5,
                    )

                except PlaywrightTimeoutError:
                    log(
                        f"Código {codigo}: retentativas de consulta do histórico "
                        "esgotadas. Abrindo Chromium novo para uma última tentativa."
                    )

                    try:
                        abrir_sessao()

                        quantidade = (
                            pesquisar_e_contar_operacao_historico_fraction(
                                page,
                                codigo,
                                "AUSENTE",
                                log,
                            )
                        )

                    except Exception as erro_final:
                        log(
                            f"Código {codigo}: não foi possível validar AUSENTE: "
                            f"{type(erro_final).__name__}: {erro_final}. "
                            "Descrição mantida como AUSENTE."
                        )

                        try:
                            abrir_sessao()
                        except Exception as erro_reabertura:
                            log(
                                "Fraction indisponível durante a pré-validação. "
                                "Os AUSENTES restantes permanecerão sem alteração: "
                                f"{type(erro_reabertura).__name__}: "
                                f"{erro_reabertura}"
                            )
                            break

                        continue

                except Exception as erro:
                    log(
                        f"Código {codigo}: erro ao validar AUSENTE: "
                        f"{type(erro).__name__}: {erro}. "
                        "Descrição mantida como AUSENTE."
                    )

                    try:
                        abrir_sessao()
                    except Exception as erro_reabertura:
                        log(
                            "Fraction indisponível durante a pré-validação. "
                            "Os AUSENTES restantes permanecerão sem alteração: "
                            f"{type(erro_reabertura).__name__}: "
                            f"{erro_reabertura}"
                        )
                        break

                    continue

                nova_descricao = _descricao_ausente_por_quantidade(
                    quantidade
                )

                df.at[
                    indice,
                    "Descricao",
                ] = nova_descricao

                log(
                    f"Código {codigo}: {quantidade} ocorrência(s) AUSENTE no "
                    f"histórico -> Descricao atualizada para {nova_descricao}."
                )

        except Exception as erro_fase:
            log(
                "Não foi possível iniciar a validação de AUSENTES no Fraction. "
                "Os pedidos permanecerão como AUSENTE e não entrarão na fila: "
                f"{type(erro_fase).__name__}: {erro_fase}"
            )

        finally:
            fechar_sessao()

    return df


def preparar_dataframe(df_entrada: pd.DataFrame) -> pd.DataFrame:
    validar_dataframe(df_entrada)
    df = df_entrada.copy()
    df["Fila_Ticket"] = ""
    df["Ticket_Criado"] = ""
    df["Observacao_Fraction"] = ""
    df["Erro_Automacao"] = ""

    for i, linha in df.iterrows():
        status = normalizar_texto(linha["Status"])
        descricao = normalizar_texto(linha["Descricao"])

        if status != "CUSTODIA":
            df.at[i, "Fila_Ticket"] = "NAO - STATUS DIFERENTE DE CUSTODIA"
            df.at[i, "Ticket_Criado"] = "NAO"
            df.at[i, "Observacao_Fraction"] = "NAO EXECUTADO"
        elif descricao not in MAPEAMENTO_STATUS_ZENDESK:
            df.at[i, "Fila_Ticket"] = "NAO - DESCRICAO FORA DA FILA"
            df.at[i, "Ticket_Criado"] = "NAO"
            df.at[i, "Observacao_Fraction"] = "NAO EXECUTADO"
        else:
            df.at[i, "Fila_Ticket"] = "SIM"

    return df


def login_zendesk(page: Page, usuario: str, senha: str, solicitar_token, log) -> None:
    log("Abrindo Zendesk...")

    page.goto(
        URL_ZENDESK,
        wait_until="domcontentloaded",
        timeout=120_000,
    )

    try:
        page.locator(
            '[data-test-id="header-toolbar-search-button"]'
        ).wait_for(
            state="visible",
            timeout=8_000,
        )

        log("Sessão Zendesk reutilizada. MFA não foi necessário.")
        return

    except PlaywrightTimeoutError:
        pass

    log("Sessão expirada ou inexistente. Fazendo login...")

    page.get_by_test_id("email-input").wait_for(
        state="visible",
        timeout=30_000,
    )

    page.get_by_test_id("email-input").fill(usuario)
    page.get_by_test_id("password-input").fill(senha)
    page.get_by_test_id("submit-button").click()

    campo_token = page.get_by_test_id("mfa-challenge-input")
    campo_token.wait_for(
        state="visible",
        timeout=60_000,
    )

    token = solicitar_token().strip()

    if not token:
        raise ValueError("Token MFA não informado.")

    campo_token.fill(token)
    page.get_by_test_id("mfa-challenge-submit").click()

    page.locator(
        '[data-test-id="header-toolbar-search-button"]'
    ).wait_for(
        state="visible",
        timeout=120_000,
    )

    log("Login concluído. Sessão persistente salva.")



def pesquisar_ticket(page: Page, pedido: str, log=None) -> bool:
    """
    Pesquisa a existência do Pedido no Zendesk.

    Se a interface de pesquisa travar ou ficar bloqueada por um modal anterior,
    faz refresh e repete a pesquisa do MESMO pedido uma única vez.

    O timeout usado para concluir que não existe resultado continua separado:
    ele retorna False normalmente e não dispara refresh.
    """

    if log:
        log(f"Pesquisando pedido {pedido}...")

    for tentativa in range(1, 3):
        try:
            page.locator(
                '[data-test-id="header-toolbar-search-button"]'
            ).click(
                timeout=30_000,
            )

            page.wait_for_timeout(1_500)

            container_pesquisa = page.locator(
                ".StyledTextInput-sc-1r6733h-0."
                "StyledTextFauxInput-sc-yqw7j9-0"
            ).last

            container_pesquisa.wait_for(
                state="visible",
                timeout=30_000,
            )

            campo_pesquisa = (
                container_pesquisa
                .locator("input")
                .first
            )

            campo_pesquisa.wait_for(
                state="visible",
                timeout=30_000,
            )

            campo_pesquisa.click()
            campo_pesquisa.fill(pedido)

            # Delay atual preservado.
            page.wait_for_timeout(10_000)

            resultado = page.locator(
                '[data-test-id="search-dialog-matches-item"]'
            )

            try:
                resultado.first.wait_for(
                    state="visible",
                    timeout=20_000,
                )
                encontrado = True

            except PlaywrightTimeoutError:
                # Aqui o timeout significa somente "nenhum ticket encontrado".
                encontrado = False

            finally:
                page.keyboard.press("Escape")
                page.wait_for_timeout(1_500)

            if log:
                if encontrado:
                    log(f"Pedido {pedido}: ticket encontrado.")
                else:
                    log(f"Pedido {pedido}: ticket não encontrado.")

            return encontrado

        except PlaywrightTimeoutError:
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass

            if tentativa >= 2:
                raise

            if log:
                log(
                    f"Pedido {pedido}: pesquisa do Zendesk travou. "
                    "Atualizando a página e repetindo o mesmo pedido..."
                )

            page.reload(
                wait_until="domcontentloaded",
                timeout=120_000,
            )

            page.locator(
                '[data-test-id="header-toolbar-search-button"]'
            ).wait_for(
                state="visible",
                timeout=120_000,
            )

            page.wait_for_timeout(1_500)

            if log:
                log(
                    f"Pedido {pedido}: Zendesk recuperado após refresh. "
                    "Nova tentativa de pesquisa."
                )

    return False

def preencher_assunto(page: Page, pedido: str, descricao: str) -> None:
    desc = normalizar_texto(descricao)
    assunto = f"{MAPEAMENTO_STATUS_ASSUNTO[desc]} | {pedido}"
    campo = page.locator('[data-test-id="omni-header-subject"]')
    campo.wait_for(state="visible", timeout=30_000)
    tag = campo.evaluate("(el) => el.tagName.toLowerCase()")
    if tag in ("input", "textarea"):
        campo.fill(assunto)
    else:
        real = campo.locator("input, textarea").first
        real.wait_for(state="visible", timeout=30_000)
        real.fill(assunto)


def preencher_solicitante(page: Page) -> None:
    container = page.locator('[data-test-id="ticket-system-field-requester-select"]')
    container.wait_for(state="visible", timeout=30_000)
    container.click()
    campo = container.locator("input").first
    campo.wait_for(state="visible", timeout=30_000)
    campo.fill("jadlog")
    page.wait_for_timeout(600)
    page.get_by_text("Jadlog atendimento4@evelog.", exact=False).click()


def preencher_ticket(page: Page, pedido: str, status_planilha: str, descricao: str, log) -> None:
    desc = normalizar_texto(descricao)

    page.locator('[data-test-id="header-toolbar-add-menu-button"]').click()
    page.wait_for_timeout(400)
    page.locator('[data-test-id="header-toolbar-add-menu-new-ticket"]').click()

    page.locator('[data-test-id="ticket-system-field-requester-select"]').wait_for(
        state="visible", timeout=60_000
    )

    preencher_assunto(page, pedido, descricao)
    preencher_solicitante(page)

    page.locator(
        '[data-test-id="ticket-form-field-dropdown-field-29872094462107"] '
        '[data-test-id="ticket-form-field-dropdown-button"]'
    ).click()
    page.get_by_role("option", name="Transportadoras", exact=True).click()

    page.locator(
        '[data-test-id="ticket-form-field-dropdown-field-29900641482651"] '
        '[data-test-id="ticket-form-field-dropdown-button"]'
    ).click()
    page.get_by_role("option", name="Insucesso na entrega", exact=True).click()

    page.locator(
        '[data-test-id="ticket-form-field-dropdown-field-29873874671003"] '
        '[data-test-id="ticket-form-field-dropdown-button"]'
    ).click()
    page.get_by_role(
        "option", name=MAPEAMENTO_STATUS_ZENDESK[desc], exact=True
    ).click()

    page.locator(
        '[data-test-id="ticket-form-field-multiline-field-29873683570203"] '
        '[data-test-id="ticket-fields-multiline-field"]'
    ).fill(pedido)

    # Por enquanto, o comentário recebe a Descricao, com a mesma escrita
    # corrigida usada no assunto. Ex.: NUMERO NAO LOCALIZADO ->
    # NÚMERO NÃO LOCALIZADO. Depois este trecho pode ser substituído por um
    # mapeamento de textos específicos para cada descrição.

    texto_ticket = MAPEAMENTO_TEXTO_TICKET[desc]

    editor = page.locator(
        '[data-test-id="omnicomposer-rich-text-ckeditor"]'
    )

    editor.wait_for(
        state="visible",
        timeout=30_000,
    )

    editor.click()
    editor.fill(texto_ticket)

    # VERSÃO FINAL: cria o ticket.
    botao_criar = page.locator(
        '[data-test-id="submit_button-button"]'
    )

    botao_criar.wait_for(
        state="visible",
        timeout=30_000,
    )

    botao_criar.click()
    page.wait_for_timeout(2_000)

    log(
        f"Pedido {pedido}: ticket criado."
    )



def fechar_ticket_atual(page: Page, log: Callable[[str], None]) -> None:
    """Fecha a aba interna do ticket depois da criação."""
    botao_fechar = page.locator(
        '[data-test-id="close-button"]'
    ).last

    botao_fechar.wait_for(
        state="visible",
        timeout=30_000,
    )

    botao_fechar.click()
    page.wait_for_timeout(700)

    log("Aba do ticket fechada.")



def preencher_observacao_fraction_com_retentativas(
    page: Page,
    codigo: str,
    log,
    recuperar: Callable[[], None],
    tentativas: int = 3,
) -> None:
    executar_com_retentativas_fraction(
        lambda: preencher_observacao_fraction(
            page,
            codigo,
            log,
        ),
        recuperar,
        log,
        descricao=f"Código {codigo}",
        tentativas=tentativas,
    )



def _df_resultado(df: pd.DataFrame) -> pd.DataFrame:
    """Retorna somente a fila válida que compõe a planilha final."""
    resultado = df[
        df["Fila_Ticket"] == "SIM"
    ].copy()

    resultado.reset_index(
        drop=True,
        inplace=True,
    )

    return resultado


def salvar_resultado(
    df: pd.DataFrame,
    caminho: Path | None = None,
) -> Path:
    """
    Salva de forma atômica para manter sempre um checkpoint íntegro.

    Quando ``caminho`` já existe, ele é substituído somente depois que
    o novo XLSX temporário foi escrito com sucesso.
    """
    PASTA_RESULTADOS.mkdir(
        parents=True,
        exist_ok=True,
    )

    if caminho is None:
        nome = (
            "tickets_zendesk_"
            + datetime.now().strftime(
                "%Y-%m-%d_%H-%M-%S"
            )
            + ".xlsx"
        )
        caminho = PASTA_RESULTADOS / nome

    temporario = caminho.with_name(
        f".{caminho.stem}.tmp.xlsx"
    )

    df.to_excel(
        temporario,
        index=False,
    )

    temporario.replace(caminho)

    return caminho


def atualizar_checkpoint(
    df: pd.DataFrame,
    caminho: Path,
    log: Callable[[str], None] | None = None,
) -> None:
    salvar_resultado(
        _df_resultado(df),
        caminho=caminho,
    )

    if log is not None:
        log(
            f"Checkpoint atualizado: {caminho}"
        )


def _registrar_pedido_com_ticket(
    registro: RegistroTexto,
    pedido: str,
    log: Callable[[str], None],
) -> None:
    """
    Atualiza o estado local de tickets conhecidos.

    Se o arquivo local estiver indisponível, a automação continua:
    a pesquisa normal do Zendesk continua sendo a fonte de segurança.
    """
    try:
        novo = registro.adicionar(
            pedido
        )

        if novo:
            log(
                f"Pedido {pedido}: adicionado ao registro local "
                "de tickets conhecidos."
            )

    except Exception as erro:
        log(
            "AVISO: não foi possível atualizar o registro local "
            f"do pedido {pedido}: {type(erro).__name__}: {erro}"
        )


def _acrescentar_erro(
    df: pd.DataFrame,
    indice: int,
    mensagem: str,
) -> None:
    atual = valor_para_texto(
        df.at[indice, "Erro_Automacao"]
    )

    df.at[indice, "Erro_Automacao"] = (
        f"{atual} | {mensagem}"
        if atual
        else mensagem
    )


def _marcar_fraction_indisponivel(
    df: pd.DataFrame,
    fila_fraction: list[int],
    mensagem: str,
    *,
    status: str = "NAO - FRACTION INDISPONIVEL",
) -> None:
    for indice in fila_fraction:
        status_atual = valor_para_texto(
            df.at[indice, "Observacao_Fraction"]
        )

        # Não sobrescreve um item que já tenha sido concluído ou
        # que já possua um erro específico registrado.
        if status_atual:
            continue

        df.at[
            indice,
            "Observacao_Fraction",
        ] = status

        _acrescentar_erro(
            df,
            indice,
            mensagem,
        )


def executar_automacao(
    df_entrada: pd.DataFrame,
    solicitar_token: Callable[[], str],
    log: Callable[[str], None],
    atualizar_progresso: Callable[[str, int, int], None] | None = None,
) -> tuple[pd.DataFrame, Path]:
    """
    Valida AUSENTES no Fraction, executa Zendesk e depois a observação no Fraction.

    Blindagem da execução:
      - CUSTODIA / AUSENTE é validado no Histórico do Fraction antes da fila;
      - a planilha de resultado é criada antes de abrir o Zendesk;
      - o arquivo é atualizado após cada pedido do Zendesk;
      - ao terminar o Zendesk, o arquivo já contém todos os tickets criados;
      - o Fraction é testado em Chromium descartável;
      - cada teste fecha completamente o navegador antes de tentar de novo;
      - falha de conexão/login no Fraction nunca apaga o resultado do Zendesk;
      - a planilha é atualizada após cada código do Fraction;
      - pedidos já confirmados no Zendesk ficam em estado local persistente;
      - numa nova execução, pedidos conhecidos não abrem a pesquisa do Zendesk;
      - tickets novos entram no registro imediatamente após a criação.

    Seletores, waits e delays das ações dos sites permanecem os mesmos.
    """
    df_validado = validar_ausentes_no_fraction(
        df_entrada,
        log,
    )

    df = preparar_dataframe(
        df_validado
    )
    uz = ZENDESK_USER
    sz = ZENDESK_PASSWORD

    uf = FRACTION_USER
    sf = FRACTION_PASSWORD

    fila = df.index[
        df["Fila_Ticket"] == "SIM"
    ].tolist()

    log(
        f"{len(fila)} pedido(s) entraram na fila de tickets."
    )

    registro_tickets = RegistroTexto(
        ARQUIVO_PEDIDOS_COM_TICKET,
        normalizar=lambda valor: valor_para_texto(valor),
    )

    log(
        "Registro local de tickets carregado: "
        f"{len(registro_tickets)} pedido(s) conhecido(s)."
    )

    log(
        "Arquivo de estado Zendesk: "
        f"{ARQUIVO_PEDIDOS_COM_TICKET}"
    )

    # O checkpoint nasce antes da navegação. Mesmo uma falha posterior
    # deixa um XLSX disponível para diagnóstico e continuidade manual.
    caminho = salvar_resultado(
        _df_resultado(df)
    )

    log(
        "Planilha de checkpoint criada antes da automação: "
        f"{caminho}"
    )

    # Somente tickets realmente criados entram na fila do Fraction.
    fila_fraction: list[int] = []

    with sync_playwright() as p:
        # ======================================================
        # FASE 1: ZENDESK
        # ======================================================
        log("===== FASE 1: ZENDESK =====")

        PERFIL_ZENDESK.mkdir(
            parents=True,
            exist_ok=True,
        )

        cz = p.chromium.launch_persistent_context(
            user_data_dir=str(PERFIL_ZENDESK),
            headless=HEADLESS,
            slow_mo=500,
            no_viewport=True,
            args=["--start-maximized"],
        )

        pz = (
            cz.pages[0]
            if cz.pages
            else cz.new_page()
        )

        try:
            login_zendesk(
                pz,
                uz,
                sz,
                solicitar_token,
                log,
            )

            total = len(fila)

            for pos, i in enumerate(
                fila,
                start=1,
            ):
                if atualizar_progresso:
                    atualizar_progresso(
                        "ZENDESK",
                        pos,
                        total,
                    )

                linha = df.loc[i]
                pedido = remover_dois_final(
                    linha["Pedido"]
                )
                codigo = valor_para_texto(
                    linha["Codigo"]
                )
                status = valor_para_texto(
                    linha["Status"]
                )
                descricao = valor_para_texto(
                    linha["Descricao"]
                )

                try:
                    if not pedido:
                        raise ValueError(
                            "Pedido vazio."
                        )

                    if not codigo:
                        raise ValueError(
                            "Codigo vazio."
                        )

                    log(
                        f"[Zendesk {pos}/{total}] "
                        f"Pedido {pedido} | {descricao}"
                    )

                    if registro_tickets.contem(
                        pedido
                    ):
                        log(
                            f"Pedido {pedido}: ticket já conhecido "
                            "pelo registro local. Pesquisa no Zendesk ignorada."
                        )

                        df.at[
                            i,
                            "Ticket_Criado",
                        ] = "NAO - TICKET JA EXISTE"

                        df.at[
                            i,
                            "Observacao_Fraction",
                        ] = "NAO EXECUTADO"

                    elif pesquisar_ticket(
                        pz,
                        pedido,
                        log,
                    ):
                        df.at[
                            i,
                            "Ticket_Criado",
                        ] = "NAO - TICKET JA EXISTE"

                        df.at[
                            i,
                            "Observacao_Fraction",
                        ] = "NAO EXECUTADO"

                        _registrar_pedido_com_ticket(
                            registro_tickets,
                            pedido,
                            log,
                        )

                    else:
                        preencher_ticket(
                            pz,
                            pedido,
                            status,
                            descricao,
                            log,
                        )

                        # O ticket já foi criado neste ponto.
                        # Registra imediatamente antes de qualquer ação
                        # posterior para evitar duplicidade numa nova execução.
                        df.at[
                            i,
                            "Ticket_Criado",
                        ] = "SIM"

                        _registrar_pedido_com_ticket(
                            registro_tickets,
                            pedido,
                            log,
                        )

                        fila_fraction.append(i)

                        # Delay e seletor de fechamento permanecem os mesmos.
                        # Falhar ao fechar a aba não desfaz um ticket já criado.
                        try:
                            fechar_ticket_atual(
                                pz,
                                log,
                            )

                        except Exception as erro_fechar:
                            mensagem_fechar = (
                                "Ticket criado, mas houve falha ao fechar "
                                "a aba interna do Zendesk: "
                                f"{type(erro_fechar).__name__}: "
                                f"{erro_fechar}"
                            )

                            _acrescentar_erro(
                                df,
                                i,
                                mensagem_fechar,
                            )

                            log(
                                f"AVISO pedido {pedido}: "
                                f"{mensagem_fechar}"
                            )

                except Exception as erro:
                    df.at[
                        i,
                        "Ticket_Criado",
                    ] = "NAO - ERRO"

                    df.at[
                        i,
                        "Observacao_Fraction",
                    ] = "NAO EXECUTADO"

                    _acrescentar_erro(
                        df,
                        i,
                        f"Zendesk: {type(erro).__name__}: {erro}",
                    )

                    log(
                        f"Erro no pedido {pedido}: {erro}"
                    )

                finally:
                    # Depois de cada pedido, a planilha em disco reflete
                    # exatamente o estado já concluído no Zendesk.
                    atualizar_checkpoint(
                        df,
                        caminho,
                    )

        finally:
            fechar_recurso_playwright_seguro(
                cz,
                log,
            )

        # ======================================================
        # FASE 2: FRACTION
        # ======================================================
        log("===== FASE 2: FRACTION =====")

        if not fila_fraction:
            log(
                "Nenhum ticket novo precisa de observação no Fraction."
            )
        else:
            faltantes_fraction = (
                variaveis_opcionais_faltantes(
                    "gerar_tickets_zendesk"
                )
            )

            if faltantes_fraction:
                mensagem = (
                    "Fraction não configurado. Variável(is) faltante(s): "
                    + ", ".join(faltantes_fraction)
                )

                log(mensagem)

                _marcar_fraction_indisponivel(
                    df,
                    fila_fraction,
                    mensagem,
                    status="NAO - FRACTION NAO CONFIGURADO",
                )

                atualizar_checkpoint(
                    df,
                    caminho,
                    log,
                )

            else:
                disponivel, erro_teste = (
                    testar_conexao_fraction_isolada(
                        p,
                        uf,
                        sf,
                        log,
                        headless=HEADLESS,
                        slow_mo=500,
                        tentativas=3,
                    )
                )

                if not disponivel:
                    mensagem = (
                        "Fraction indisponível após o teste isolado. "
                        f"{erro_teste}"
                    )

                    log(mensagem)

                    _marcar_fraction_indisponivel(
                        df,
                        fila_fraction,
                        mensagem,
                    )

                    atualizar_checkpoint(
                        df,
                        caminho,
                        log,
                    )

                else:
                    # O navegador usado no teste já foi fechado.
                    # A fase real sempre começa em um Chromium novo.
                    bf = None
                    cf = None
                    pf = None

                    def fechar_fraction_real() -> None:
                        nonlocal bf, cf, pf

                        fechar_recurso_playwright_seguro(
                            cf,
                            log,
                        )
                        fechar_recurso_playwright_seguro(
                            bf,
                            log,
                        )

                        bf = None
                        cf = None
                        pf = None

                    def abrir_fraction_real() -> None:
                        nonlocal bf, cf, pf

                        fechar_fraction_real()

                        log(
                            "Abrindo Chromium novo para a fase real do Fraction."
                        )

                        bf = p.chromium.launch(
                            headless=HEADLESS,
                            slow_mo=500,
                            args=["--start-maximized"],
                        )

                        cf = bf.new_context(
                            no_viewport=True,
                        )

                        pf = cf.new_page()

                        login_fraction(
                            pf,
                            uf,
                            sf,
                            log,
                        )

                    def recuperar_fraction_real() -> None:
                        recuperar_sessao_fraction(
                            pf,
                            log,
                            login_callback=lambda: login_fraction(
                                pf,
                                uf,
                                sf,
                                log,
                            ),
                        )

                        # Delay original entre retentativas preservado.
                        pf.wait_for_timeout(2_000)

                    try:
                        abrir_fraction_real()

                        total = len(fila_fraction)
                        interromper_fraction = False

                        for pos, i in enumerate(
                            fila_fraction,
                            start=1,
                        ):
                            if atualizar_progresso:
                                atualizar_progresso(
                                    "FRACTION",
                                    pos,
                                    total,
                                )

                            codigo = valor_para_texto(
                                df.loc[i, "Codigo"]
                            )

                            log(
                                f"[Fraction {pos}/{total}] Código {codigo}"
                            )

                            try:
                                preencher_observacao_fraction_com_retentativas(
                                    pf,
                                    codigo,
                                    log,
                                    recuperar_fraction_real,
                                    tentativas=5,
                                )

                                df.at[
                                    i,
                                    "Observacao_Fraction",
                                ] = "SIM"

                                log(
                                    "Observação salva. "
                                    "Seguindo para o próximo código."
                                )

                                pf.wait_for_timeout(500)

                            except PlaywrightTimeoutError:
                                log(
                                    f"Código {codigo}: retentativas com refresh "
                                    "esgotadas. Reiniciando o Chromium para "
                                    "uma última tentativa."
                                )

                                try:
                                    abrir_fraction_real()

                                    preencher_observacao_fraction(
                                        pf,
                                        codigo,
                                        log,
                                    )

                                    df.at[
                                        i,
                                        "Observacao_Fraction",
                                    ] = "SIM"

                                    log(
                                        f"Código {codigo}: observação recuperada "
                                        "após reiniciar o Chromium."
                                    )

                                except Exception as erro_final:
                                    df.at[
                                        i,
                                        "Observacao_Fraction",
                                    ] = "NAO - ERRO"

                                    _acrescentar_erro(
                                        df,
                                        i,
                                        "Fraction: "
                                        f"{type(erro_final).__name__}: "
                                        f"{erro_final}",
                                    )

                                    log(
                                        "Erro definitivo no Fraction código "
                                        f"{codigo}: {erro_final}"
                                    )

                                    # Prepara uma sessão limpa para o próximo.
                                    try:
                                        abrir_fraction_real()
                                    except Exception as erro_reabertura:
                                        mensagem = (
                                            "Fraction indisponível após reiniciar "
                                            "o navegador: "
                                            f"{type(erro_reabertura).__name__}: "
                                            f"{erro_reabertura}"
                                        )

                                        log(mensagem)

                                        restantes = fila_fraction[pos:]

                                        _marcar_fraction_indisponivel(
                                            df,
                                            restantes,
                                            mensagem,
                                        )

                                        interromper_fraction = True

                            except Exception as erro:
                                df.at[
                                    i,
                                    "Observacao_Fraction",
                                ] = "NAO - ERRO"

                                _acrescentar_erro(
                                    df,
                                    i,
                                    "Fraction: "
                                    f"{type(erro).__name__}: {erro}",
                                )

                                log(
                                    "Erro no Fraction código "
                                    f"{codigo}: {erro}"
                                )

                            finally:
                                atualizar_checkpoint(
                                    df,
                                    caminho,
                                )

                            if interromper_fraction:
                                atualizar_checkpoint(
                                    df,
                                    caminho,
                                    log,
                                )
                                break

                    except Exception as erro_fase:
                        # Falha no launch/login da fase real é tratada como
                        # indisponibilidade. O resultado do Zendesk permanece.
                        mensagem = (
                            "A fase real do Fraction não pôde ser iniciada: "
                            f"{type(erro_fase).__name__}: {erro_fase}"
                        )

                        log(mensagem)

                        _marcar_fraction_indisponivel(
                            df,
                            fila_fraction,
                            mensagem,
                        )

                        atualizar_checkpoint(
                            df,
                            caminho,
                            log,
                        )

                    finally:
                        fechar_fraction_real()

    df_resultado = _df_resultado(df)

    salvar_resultado(
        df_resultado,
        caminho=caminho,
    )

    log(
        f"Resultado salvo em {caminho} "
        f"com {len(df_resultado)} pedido(s) válido(s)."
    )

    return df_resultado, caminho
