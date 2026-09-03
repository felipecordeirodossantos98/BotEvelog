from __future__ import annotations

from datetime import datetime
from pathlib import Path
import os
import re
import unicodedata
from typing import Callable

import pandas as pd
from dotenv import load_dotenv
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright


PASTA_FLUXO = Path(__file__).resolve().parent
PASTA_PROJETO = Path(__file__).resolve().parents[2]

PASTA_BOT = Path(__file__).resolve().parents[2]
PASTA_RAIZ = PASTA_BOT.parent

load_dotenv(PASTA_PROJETO / ".env")


def env_obrigatoria(nome: str) -> str:
    valor = os.getenv(nome, "").strip()

    if not valor:
        raise ValueError(
            f"A variável {nome} não foi definida no arquivo .env."
        )

    return valor


URL_ZENDESK = env_obrigatoria("URL_ZENDESK")
URL_FRACTION = env_obrigatoria("URL_FRACTION")

ZENDESK_USER = env_obrigatoria("ZENDESK_USER")
ZENDESK_PASSWORD = env_obrigatoria("ZENDESK_PASSWORD")

FRACTION_USER = env_obrigatoria("FRACTION_USER")
FRACTION_PASSWORD = env_obrigatoria("FRACTION_PASSWORD")

HEADLESS = os.getenv(
    "HEADLESS",
    "False",
).strip().lower() == "true"

PASTA_RESULTADOS = (
    PASTA_RAIZ
    / "resultados"
    / "tickets_zendesk"
)

PASTA_PERFIS = (
    PASTA_PROJETO.parent
    / "perfis"
)

PERFIL_ZENDESK = (
    PASTA_PERFIS
    / "gerar_tickets_zendesk"
    / "chromium"
)

COLUNAS_OBRIGATORIAS = {"Codigo", "Pedido", "Status", "Descricao"}

MAPEAMENTO_STATUS_ZENDESK = {
    "MUDOU-SE": "Mudou-se",
    "DESTINATARIO DESCONHECIDO": "Destinatário desconhecido",
    "FECHADO": "Local fechado",
    "NUMERO NAO LOCALIZADO": "Número não localizado",
    "AUSENTE": "Ausente",
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
    "FECHADO": "FECHADO",
    "NUMERO NAO LOCALIZADO": "NÚMERO NÃO LOCALIZADO",
    "AUSENTE": "AUSENTE",
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

    "FECHADO": "FECHADO",

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

    "AUSENTE 3": "AUSENTE 3",

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
    Mesma pesquisa da versão base do GitHub.

    Faz somente 1 tentativa para o Pedido, mantendo os delays maiores:
    - 1,5 s antes de preencher;
    - 5 s para a pesquisa estabilizar;
    - até 10 s aguardando o resultado;
    - 1,5 s após fechar a pesquisa.
    """

    if log:
        log(f"Pesquisando pedido {pedido}...")

    page.locator(
        '[data-test-id="header-toolbar-search-button"]'
    ).click()

    page.wait_for_timeout(1_500)

    container_pesquisa = page.locator(
        ".StyledTextInput-sc-1r6733h-0.StyledTextFauxInput-sc-yqw7j9-0"
    ).last

    container_pesquisa.wait_for(
        state="visible",
        timeout=30_000,
    )

    campo_pesquisa = container_pesquisa.locator("input").first

    campo_pesquisa.wait_for(
        state="visible",
        timeout=30_000,
    )

    campo_pesquisa.click()
    campo_pesquisa.fill(pedido)

    page.wait_for_timeout(5_000)

    resultado = page.locator(
        '[data-test-id="search-dialog-matches-item"]'
    )

    try:
        resultado.first.wait_for(
            state="visible",
            timeout=10_000,
        )
        encontrado = True

    except PlaywrightTimeoutError:
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



def login_fraction(page: Page, usuario: str, senha: str, log) -> None:
    log("Abrindo FractionWeb...")
    page.goto(URL_FRACTION, wait_until="domcontentloaded", timeout=120_000)
    page.get_by_role("textbox", name="Usuário").fill(usuario)
    page.get_by_role("textbox", name="Senha").fill(senha)
    page.get_by_role("button", name="Login").click()
    page.get_by_role("link", name="Consultas").wait_for(
        state="visible", timeout=120_000
    )
    log("Login no Fraction concluído.")


def preencher_observacao_fraction(page: Page, codigo: str, log) -> None:
    page.get_by_role("link", name="Consultas").click()
    page.wait_for_timeout(400)
    page.get_by_role("link", name="Pesquisar").click()
    page.wait_for_timeout(500)
    page.locator('[id="frmPesquisa:cte"]').fill(codigo)
    page.get_by_role("button", name="Processar").click()

    botao_obs = page.get_by_role("button", name="Incluir Observação")
    botao_obs.wait_for(state="visible", timeout=120_000)
    botao_obs.click()

    campo = page.locator('[id="form_add_obs:descObsv"]')
    campo.wait_for(state="visible", timeout=30_000)
    campo.fill("REMETENTE ACIONADO.")

    botao_salvar = page.get_by_role(
        "button",
        name="Salvar",
    )

    botao_salvar.wait_for(
        state="visible",
        timeout=30_000,
    )

    botao_salvar.click()
    page.wait_for_timeout(1_000)

    log(
        f"Código {codigo}: observação REMETENTE ACIONADO. salva."
    )



def preencher_observacao_fraction_com_retentativas(
    page: Page,
    codigo: str,
    log,
    tentativas: int = 3,
) -> None:
    ultimo_erro = None

    for tentativa in range(1, tentativas + 1):
        try:
            if tentativa > 1:
                log(
                    f"Código {codigo}: nova tentativa "
                    f"{tentativa}/{tentativas}."
                )
                page.wait_for_timeout(2_000)

            preencher_observacao_fraction(
                page,
                codigo,
                log,
            )

            if tentativa > 1:
                log(
                    f"Código {codigo}: sucesso na tentativa "
                    f"{tentativa}/{tentativas}."
                )

            return

        except Exception as erro:
            ultimo_erro = erro

            log(
                f"Código {codigo}: tentativa {tentativa}/{tentativas} "
                f"falhou: {type(erro).__name__}: {erro}"
            )

    raise ultimo_erro



def salvar_resultado(df: pd.DataFrame) -> Path:
    PASTA_RESULTADOS.mkdir(parents=True, exist_ok=True)
    nome = "tickets_zendesk_" + datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + ".xlsx"
    caminho = PASTA_RESULTADOS / nome
    df.to_excel(caminho, index=False)
    return caminho


def executar_automacao(
    df_entrada: pd.DataFrame,
    solicitar_token: Callable[[], str],
    log: Callable[[str], None],
    atualizar_progresso: Callable[[str, int, int], None] | None = None,
) -> tuple[pd.DataFrame, Path]:
    """
    Ordem:
      1) Processa TODA a fila Zendesk.
      2) Fecha Zendesk.
      3) Abre Fraction e inicia a fila de observações.

    Filtro da fila de tickets:
      Status = CUSTODIA E Descricao presente no mapeamento.

    Versão final:
      - Faz uma pesquisa por Pedido, mantendo os delays maiores.
      - Remove somente o dígito 2 final do Pedido antes da pesquisa/preenchimento.
      - Cria o ticket no Zendesk e fecha a aba depois da criação.
      - Reutiliza a sessão persistente do Zendesk enquanto ela estiver válida.
      - Depois de concluir toda a fila do Zendesk, abre o Fraction.
      - No Fraction faz até 3 tentativas por Código em caso de erro.
      - Preenche e salva REMETENTE ACIONADO.
    """
    df = preparar_dataframe(df_entrada)
    uz = ZENDESK_USER
    sz = ZENDESK_PASSWORD

    uf = FRACTION_USER
    sf = FRACTION_PASSWORD

    fila = df.index[df["Fila_Ticket"] == "SIM"].tolist()
    log(f"{len(fila)} pedido(s) entraram na fila de tickets.")

    # Somente tickets realmente criados entram na fila do Fraction.
    fila_fraction: list[int] = []

    with sync_playwright() as p:
        # -------- FASE 1: ZENDESK --------
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

        pz = cz.pages[0] if cz.pages else cz.new_page()

        try:
            login_zendesk(pz, uz, sz, solicitar_token, log)
            total = len(fila)

            for pos, i in enumerate(fila, start=1):
                if atualizar_progresso:
                    atualizar_progresso("ZENDESK", pos, total)

                linha = df.loc[i]
                pedido = remover_dois_final(linha["Pedido"])
                codigo = valor_para_texto(linha["Codigo"])
                status = valor_para_texto(linha["Status"])
                descricao = valor_para_texto(linha["Descricao"])

                try:
                    if not pedido:
                        raise ValueError("Pedido vazio.")
                    if not codigo:
                        raise ValueError("Codigo vazio.")

                    log(f"[Zendesk {pos}/{total}] Pedido {pedido} | {descricao}")

                    if pesquisar_ticket(pz, pedido, log):
                        df.at[i, "Ticket_Criado"] = "NAO - TICKET JA EXISTE"
                        df.at[i, "Observacao_Fraction"] = "NAO EXECUTADO"
                        continue

                    preencher_ticket(
                        pz,
                        pedido,
                        status,
                        descricao,
                        log,
                    )

                    # Fecha a aba depois da criação, antes de pesquisar o próximo.
                    fechar_ticket_atual(
                        pz,
                        log,
                    )

                    df.at[i, "Ticket_Criado"] = "SIM"
                    fila_fraction.append(i)

                except Exception as erro:
                    df.at[i, "Ticket_Criado"] = "NAO - ERRO"
                    df.at[i, "Observacao_Fraction"] = "NAO EXECUTADO"
                    df.at[i, "Erro_Automacao"] = f"{type(erro).__name__}: {erro}"
                    log(f"Erro no pedido {pedido}: {erro}")

        finally:
            cz.close()

        # -------- FASE 2: FRACTION --------
        log("===== FASE 2: FRACTION =====")
        if fila_fraction:
            bf = p.chromium.launch(headless=HEADLESS, slow_mo=500, args=["--start-maximized"])
            cf = bf.new_context(no_viewport=True)
            pf = cf.new_page()

            try:
                login_fraction(pf, uf, sf, log)
                total = len(fila_fraction)

                for pos, i in enumerate(fila_fraction, start=1):
                    if atualizar_progresso:
                        atualizar_progresso("FRACTION", pos, total)

                    codigo = valor_para_texto(df.loc[i, "Codigo"])
                    log(f"[Fraction {pos}/{total}] Código {codigo}")

                    try:
                        preencher_observacao_fraction_com_retentativas(
                            pf,
                            codigo,
                            log,
                            tentativas=3,
                        )

                        df.at[i, "Observacao_Fraction"] = "SIM"

                        log(
                            "Observação salva. Seguindo para o próximo código."
                        )

                        pf.wait_for_timeout(500)
                    except Exception as erro:
                        df.at[i, "Observacao_Fraction"] = "NAO - ERRO"
                        df.at[i, "Erro_Automacao"] = (
                            (df.at[i, "Erro_Automacao"] + " | " if df.at[i, "Erro_Automacao"] else "")
                            + f"Fraction: {type(erro).__name__}: {erro}"
                        )
                        log(f"Erro no Fraction código {codigo}: {erro}")

            finally:
                cf.close()
                bf.close()

    # A planilha final contém somente os pedidos válidos que entraram
    # na fila de tickets (Status = CUSTODIA + Descricao mapeada).
    df_resultado = df[
        df["Fila_Ticket"] == "SIM"
    ].copy()

    df_resultado.reset_index(
        drop=True,
        inplace=True,
    )

    caminho = salvar_resultado(
        df_resultado
    )

    log(
        f"Resultado salvo em {caminho} "
        f"com {len(df_resultado)} pedido(s) válido(s)."
    )

    return df_resultado, caminho
