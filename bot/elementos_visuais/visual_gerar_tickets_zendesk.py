import queue
import threading

import streamlit as st

from automacoes.gerar_tickets_zendesk import (
    carregar_planilha_zendesk,
    executar_automacao,
)
from utils.interface import (
    LARGURA_BOTAO,
    avisar_variaveis_faltantes,
    error,
    renderizar_logs,
    success,
    warning,
)
from utils.logs import criar_logger_execucao


SESSION_KEYS = {
    "thread": "visual_zendesk_thread",
    "estado": "visual_zendesk_estado",
    "token_queue": "visual_zendesk_token_queue",
    "df": "visual_zendesk_df",
}


def inicializar_estado() -> None:
    valores_iniciais = {
        SESSION_KEYS["thread"]: None,
        SESSION_KEYS["estado"]: None,
        SESSION_KEYS["token_queue"]: None,
        SESSION_KEYS["df"]: None,
    }

    for chave, valor in valores_iniciais.items():
        if chave not in st.session_state:
            st.session_state[chave] = valor


def worker(
    df,
    estado,
    token_queue,
) -> None:
    def log_visual(msg):
        estado["logs"].append(msg)

    log, caminho_log = criar_logger_execucao(
        "gerar_tickets_zendesk",
        log_visual,
    )

    estado["arquivo_log"] = str(
        caminho_log
    )

    def token():
        estado["fase"] = "AGUARDANDO_MFA"
        return token_queue.get()

    def progresso(
        site,
        atual,
        total,
    ):
        estado["fase"] = site
        estado["atual"] = atual
        estado["total"] = total

    try:
        resultado, caminho = executar_automacao(
            df,
            token,
            log,
            progresso,
        )

        estado["resultado"] = resultado
        estado["caminho"] = str(caminho)

        tickets_criados = (
            resultado["Ticket_Criado"]
            .fillna("")
            .astype(str)
            == "SIM"
        )

        observacao_ok = (
            resultado["Observacao_Fraction"]
            .fillna("")
            .astype(str)
            == "SIM"
        )

        falhas_fraction = (
            tickets_criados & ~observacao_ok
        ).any()

        estado["fase"] = (
            "CONCLUIDO_COM_ALERTA"
            if falhas_fraction
            else "CONCLUIDO"
        )

    except Exception as erro:
        mensagem = (
            f"{type(erro).__name__}: {erro}"
        )
        log(
            "Erro fatal da automação: "
            + mensagem
        )
        estado["erro"] = mensagem
        estado["fase"] = "ERRO"


@st.fragment(run_every=1)
def painel_gerar_tickets_zendesk() -> None:
    estado = st.session_state.get(
        SESSION_KEYS["estado"]
    )

    if not estado:
        return

    fase = estado["fase"]

    if fase == "AGUARDANDO_MFA":
        with st.form(
            "visual_zendesk_mfa"
        ):
            token = st.text_input(
                "Token MFA",
                type="password",
                width="stretch",
            )

            enviar = st.form_submit_button(
                "Enviar token",
                width=LARGURA_BOTAO,
            )

        if enviar and token.strip():
            token_queue = st.session_state.get(
                SESSION_KEYS["token_queue"]
            )

            if token_queue is not None:
                token_queue.put(
                    token.strip()
                )
                success("Token enviado.")

    if fase == "ERRO":
        error(estado["erro"])

    if fase == "CONCLUIDO":
        success(
            "Automação concluída. "
            f"Resultado salvo em: {estado['caminho']}"
        )

    if fase == "CONCLUIDO_COM_ALERTA":
        warning(
            "Tickets processados e planilha gerada, "
            "mas a etapa do Fraction ficou pendente. "
            f"Resultado salvo em: {estado['caminho']}"
        )

    renderizar_logs(
        estado["logs"]
    )


def renderizar_gerar_tickets_zendesk(
    arquivo,
) -> None:
    inicializar_estado()

    st.subheader("Gerar tickets Zendesk")

    faltantes = avisar_variaveis_faltantes(
        "gerar_tickets_zendesk"
    )

    erro_arquivo = None

    try:
        df = carregar_planilha_zendesk(
            arquivo
        )
        st.session_state[
            SESSION_KEYS["df"]
        ] = df

    except Exception as erro:
        erro_arquivo = str(erro)
        st.session_state[
            SESSION_KEYS["df"]
        ] = None
        error(erro_arquivo)

    thread = st.session_state.get(
        SESSION_KEYS["thread"]
    )

    rodando = (
        thread is not None
        and thread.is_alive()
    )

    if st.button(
        "Iniciar automação",
        type="primary",
        disabled=(
            bool(faltantes)
            or bool(erro_arquivo)
            or st.session_state.get(
                SESSION_KEYS["df"]
            ) is None
            or rodando
        ),
        width=LARGURA_BOTAO,
        key="visual_zendesk_iniciar",
    ):
        estado = {
            "fase": "INICIANDO",
            "logs": [],
            "atual": 0,
            "total": 0,
            "resultado": None,
            "caminho": None,
            "erro": None,
            "arquivo_log": None,
        }

        token_queue = queue.Queue()

        thread = threading.Thread(
            target=worker,
            args=(
                st.session_state[
                    SESSION_KEYS["df"]
                ].copy(),
                estado,
                token_queue,
            ),
            daemon=True,
        )

        st.session_state[
            SESSION_KEYS["estado"]
        ] = estado
        st.session_state[
            SESSION_KEYS["token_queue"]
        ] = token_queue
        st.session_state[
            SESSION_KEYS["thread"]
        ] = thread

        thread.start()
        st.rerun()

    painel_gerar_tickets_zendesk()
