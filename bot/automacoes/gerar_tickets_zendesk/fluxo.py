import queue
import threading
import unicodedata

import pandas as pd
import streamlit as st

from .automacao import (
    executar_automacao,
    validar_dataframe,
)


LARGURA_COMPONENTE = 600
LARGURA_BOTAO = 240


SESSION_KEYS = {
    "thread": "gerar_tickets_zendesk_thread",
    "estado": "gerar_tickets_zendesk_estado",
    "token_queue": "gerar_tickets_zendesk_token_queue",
    "df": "gerar_tickets_zendesk_df",
}


COLUNAS_CANONICAS = {
    "CODIGO": "Codigo",
    "PEDIDO": "Pedido",
    "STATUS": "Status",
    "DESCRICAO": "Descricao",
}


def normalizar_cabecalho(
    valor: object,
) -> str:
    texto = unicodedata.normalize(
        "NFKD",
        str(valor).strip(),
    )

    texto = "".join(
        caractere
        for caractere in texto
        if not unicodedata.combining(caractere)
    )

    return " ".join(
        texto.upper().split()
    )


def padronizar_colunas(
    df: pd.DataFrame,
) -> pd.DataFrame:
    df = df.copy()

    renomear = {}

    for coluna in df.columns:
        normalizada = normalizar_cabecalho(
            coluna
        )

        nome_canonico = COLUNAS_CANONICAS.get(
            normalizada
        )

        if nome_canonico:
            renomear[coluna] = nome_canonico

    return df.rename(
        columns=renomear
    )


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
    def log(msg):
        estado["logs"].append(msg)

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
        estado["fase"] = "CONCLUIDO"

    except Exception as erro:
        estado["erro"] = (
            f"{type(erro).__name__}: {erro}"
        )
        estado["fase"] = "ERRO"


@st.fragment(run_every=1)
def painel_gerar_tickets_zendesk() -> None:
    estado = st.session_state.get(
        SESSION_KEYS["estado"]
    )

    if not estado:
        return

    st.divider()

    fase = estado["fase"]

    st.subheader(
        f"Execução - {fase}"
    )

    if estado["total"]:
        st.progress(
            estado["atual"] / estado["total"],
            text=(
                f'{estado["atual"]} '
                f'de {estado["total"]}'
            ),
            width=LARGURA_COMPONENTE,
        )

    if fase == "AGUARDANDO_MFA":
        with st.form(
            "gerar_tickets_zendesk_mfa"
        ):
            token = st.text_input(
                "Token MFA",
                type="password",
                width=LARGURA_COMPONENTE,
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

                st.success(
                    "Token enviado.",
                    width=LARGURA_COMPONENTE,
                )

    if estado["logs"]:
        with st.expander(
            "Log da automação",
            expanded=False,
            width=LARGURA_COMPONENTE,
        ):
            st.text_area(
                "Log",
                "\n".join(
                    estado["logs"][-100:]
                ),
                height=320,
                disabled=True,
                label_visibility="collapsed",
                width="stretch",
            )

    if fase == "ERRO":
        st.error(
            estado["erro"],
            width=LARGURA_COMPONENTE,
        )

    if fase == "CONCLUIDO":
        st.success(
            "Automação concluída.",
            width=LARGURA_COMPONENTE,
        )


def renderizar_gerar_tickets_zendesk(
    arquivo,
) -> None:
    inicializar_estado()

    st.subheader(
        "Gerar tickets Zendesk"
    )

    try:
        arquivo.seek(0)

        # Linha 2 = cabeçalho.
        # Linha 3 em diante = dados.
        df = pd.read_excel(
            arquivo,
            header=1,
        )

        arquivo.seek(0)

        df = padronizar_colunas(
            df
        )

        validar_dataframe(
            df
        )

        st.session_state[
            SESSION_KEYS["df"]
        ] = df

        st.success(
            (
                "Planilha carregada com "
                f"{len(df)} linha(s)."
            ),
            width=LARGURA_COMPONENTE,
        )

        st.dataframe(
            df,
            width="stretch",
            height=300,
        )

    except Exception as erro:
        st.session_state[
            SESSION_KEYS["df"]
        ] = None

        st.error(
            str(erro),
            width=LARGURA_COMPONENTE,
        )

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
            st.session_state.get(
                SESSION_KEYS["df"]
            ) is None
            or rodando
        ),
        width=LARGURA_BOTAO,
    ):
        estado = {
            "fase": "INICIANDO",
            "logs": [],
            "atual": 0,
            "total": 0,
            "resultado": None,
            "caminho": None,
            "erro": None,
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