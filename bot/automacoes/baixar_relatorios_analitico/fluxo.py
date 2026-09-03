from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
import re

import streamlit as st

from .automation.extractor import executar
from .data.cleaner import limpar_base
from .data.unifier import unificar_bases
from .utils.config import (
    BASES_DIARIAS_PATH,
    ORIGINAIS_PATH,
    validar_configuracao,
)


LARGURA_COMPONENTE = 600
LARGURA_BOTAO = 300

BASES_DIARIAS = Path(BASES_DIARIAS_PATH)
ORIGINAIS = Path(ORIGINAIS_PATH)

BASES_DIARIAS.mkdir(parents=True, exist_ok=True)
ORIGINAIS.mkdir(parents=True, exist_ok=True)

PADRAO_BASE = re.compile(
    r"^Analítico_(\d{2}-\d{2}-\d{4})\.xlsx$",
    re.IGNORECASE,
)


def executar_extracao_e_filtragem(
    data_inicial,
    data_final,
    token,
    atualizar_extracao=None,
    atualizar_tratamento=None,
):
    arquivos_filtrados = []
    futuros = []

    executor = ThreadPoolExecutor(max_workers=1)

    try:
        def callback_extracao(atual, total_dias, data):
            if atualizar_extracao:
                atualizar_extracao(atual, total_dias, data)

        def callback_arquivo(
            arquivo_original,
            atual,
            total_dias,
            data,
        ):
            if atualizar_tratamento:
                atualizar_tratamento(
                    atual,
                    total_dias,
                    f"Filtrando {Path(arquivo_original).name}",
                )

            futuro = executor.submit(
                limpar_base,
                arquivo_original,
            )
            futuros.append(futuro)

        arquivos_originais = executar(
            data_inicial=data_inicial,
            data_final=data_final,
            token=token,
            callback=callback_extracao,
            arquivo_callback=callback_arquivo,
        )

        for futuro in futuros:
            resultado = futuro.result()
            arquivos_filtrados.append(
                resultado["arquivo_filtrado"]
            )

        # Só remove os originais quando todos os downloads e filtros
        # do período terminaram com sucesso.
        for arquivo in arquivos_originais:
            caminho = Path(arquivo)
            if caminho.exists():
                caminho.unlink()

        ORIGINAIS.mkdir(parents=True, exist_ok=True)

        return {
            "arquivos_originais": arquivos_originais,
            "arquivos_filtrados": arquivos_filtrados,
        }

    finally:
        executor.shutdown(wait=True)


def listar_bases_diarias():
    bases = []

    for arquivo in BASES_DIARIAS.glob("Analítico_*.xlsx"):
        match = PADRAO_BASE.match(arquivo.name)

        if not match:
            continue

        try:
            data_base = date.fromisoformat(
                "-".join(reversed(match.group(1).split("-")))
            )
        except ValueError:
            continue

        bases.append((data_base, arquivo))

    bases.sort(key=lambda item: item[0])
    return bases


def renderizar_baixar_relatorios_analitico() -> None:
    st.subheader("Baixar relatório analítico")

    hoje = date.today()
    max_data_extracao = hoje - timedelta(days=1)

    data_extracao = st.date_input(
        "📅 Período de extração",
        value=(max_data_extracao, max_data_extracao),
        min_value=date(2000, 1, 1),
        max_value=max_data_extracao,
        key="baixar_relatorios_analitico_periodo_extracao",
        format="DD/MM/YYYY",
        width=LARGURA_COMPONENTE,
    )

    if isinstance(data_extracao, tuple):
        if len(data_extracao) == 2:
            data_extracao_inicial, data_extracao_final = data_extracao
        else:
            data_extracao_inicial = data_extracao[0]
            data_extracao_final = data_extracao[0]
    else:
        data_extracao_inicial = data_extracao
        data_extracao_final = data_extracao

    quantidade_dias_extracao = (
        data_extracao_final - data_extracao_inicial
    ).days + 1

    st.caption(
        f"Período selecionado: {quantidade_dias_extracao} dia(s). "
        f"A data máxima permitida é "
        f"{max_data_extracao.strftime('%d/%m/%Y')}."
    )

    token = st.text_input(
        "Token MFA",
        type="password",
        max_chars=6,
        placeholder="Digite o código MFA",
        key="baixar_relatorios_analitico_token_mfa",
        width=LARGURA_COMPONENTE,
    )

    if st.button(
        "Extrair e filtrar bases",
        type="primary",
        key="baixar_relatorios_analitico_extrair",
        width=LARGURA_BOTAO,
    ):
        if not token:
            st.error(
                "Informe o Token MFA.",
                width=LARGURA_COMPONENTE,
            )
            st.stop()

        try:
            validar_configuracao()
        except RuntimeError as erro:
            st.error(
                str(erro),
                width=LARGURA_COMPONENTE,
            )
            st.stop()

        barra = st.progress(0.0, width=LARGURA_COMPONENTE)
        status = st.empty()

        def atualizar_extracao(atual, total, data):
            barra.progress(
                min(0.70 * (atual / total), 0.70)
            )
            status.info(
                f"Extraindo **{data.strftime('%d/%m/%Y')}** "
                f"({atual}/{total})...",
                width=LARGURA_COMPONENTE,
            )

        def atualizar_tratamento(atual, total, mensagem):
            status.info(
                mensagem,
                width=LARGURA_COMPONENTE,
            )

        try:
            resultado = executar_extracao_e_filtragem(
                data_inicial=data_extracao_inicial,
                data_final=data_extracao_final,
                token=token,
                atualizar_extracao=atualizar_extracao,
                atualizar_tratamento=atualizar_tratamento,
            )

            barra.progress(1.0)
            status.success(
                "Extração e filtragem concluídas.",
                width=LARGURA_COMPONENTE,
            )

            st.success(
                f"{len(resultado['arquivos_filtrados'])} "
                "base(s) diária(s) foram preparadas.",
                width=LARGURA_COMPONENTE,
            )

            with st.expander(
                "Bases diárias geradas",
                width=LARGURA_COMPONENTE,
            ):
                for arquivo in resultado["arquivos_filtrados"]:
                    st.write(f"✅ {arquivo}")

        except Exception as erro:
            barra.empty()
            status.error(
                "O processo de extração/filtragem foi interrompido.",
                width=LARGURA_COMPONENTE,
            )
            st.exception(erro)

    st.divider(width=LARGURA_COMPONENTE)

    bases_disponiveis = listar_bases_diarias()

    if not bases_disponiveis:
        st.info(
            "Ainda não existem bases diárias em "
            "resultados/relatorios_analitico/bases_diarias/.",
            width=LARGURA_COMPONENTE,
        )
        return

    min_emissao = bases_disponiveis[0][0]
    max_emissao = bases_disponiveis[-1][0]

    hash_base = (
        len(bases_disponiveis),
        min_emissao,
        max_emissao,
    )

    chave_hash = "baixar_relatorios_analitico_hash_base_unificacao"
    chave_filtro = "baixar_relatorios_analitico_filtro_global_unificacao"

    if chave_hash not in st.session_state:
        st.session_state[chave_hash] = None

    if st.session_state[chave_hash] != hash_base:
        st.session_state[chave_filtro] = (
            min_emissao,
            max_emissao,
        )
        st.session_state[chave_hash] = hash_base

    data_unificacao = st.date_input(
        "📅 Período das bases para unificar",
        min_value=min_emissao,
        max_value=max_emissao,
        key=chave_filtro,
        format="DD/MM/YYYY",
        width=LARGURA_COMPONENTE,
    )

    if isinstance(data_unificacao, tuple):
        if len(data_unificacao) == 1:
            data_unificacao_inicial = data_unificacao[0]
            data_unificacao_final = data_unificacao[0]
        else:
            data_unificacao_inicial, data_unificacao_final = data_unificacao
    else:
        data_unificacao_inicial = data_unificacao
        data_unificacao_final = data_unificacao

    st.caption(
        f"Bases disponíveis: {min_emissao.strftime('%d/%m/%Y')} a "
        f"{max_emissao.strftime('%d/%m/%Y')}."
    )

    bases_selecionadas = [
        (data_base, arquivo)
        for data_base, arquivo in bases_disponiveis
        if data_unificacao_inicial
        <= data_base
        <= data_unificacao_final
    ]

    with st.expander(
        f"Bases encontradas no período: {len(bases_selecionadas)}",
        width=LARGURA_COMPONENTE,
    ):
        if bases_selecionadas:
            for data_base, arquivo in bases_selecionadas:
                st.write(
                    f"📄 **{data_base.strftime('%d/%m/%Y')}** — "
                    f"`{arquivo.name}`"
                )
        else:
            st.warning(
                "Nenhuma base diária foi encontrada dentro "
                "do período selecionado.",
                width=LARGURA_COMPONENTE,
            )

    if st.button(
        "Unificar bases diárias",
        key="baixar_relatorios_analitico_unificar",
        width=LARGURA_BOTAO,
    ):
        arquivos = [
            arquivo
            for _, arquivo in bases_selecionadas
        ]

        if not arquivos:
            st.error(
                "Nenhuma base diária encontrada dentro "
                "do período selecionado.",
                width=LARGURA_COMPONENTE,
            )
            st.stop()

        try:
            with st.spinner(
                "Unificando bases diárias...",
                width=LARGURA_COMPONENTE,
            ):
                resultado_unificacao = unificar_bases(
                    arquivos=arquivos,
                    data_inicial=data_unificacao_inicial,
                    data_final=data_unificacao_final,
                )

            st.success(
                "Unificação concluída com sucesso.",
                width=LARGURA_COMPONENTE,
            )

            st.info(
                f"Arquivo final: "
                f"{resultado_unificacao['arquivo_final']}",
                width=LARGURA_COMPONENTE,
            )

            st.metric(
                "Bases unificadas",
                resultado_unificacao["arquivos"],
                width=LARGURA_COMPONENTE,
            )

            st.metric(
                "Linhas finais",
                f"{resultado_unificacao['linhas_finais']:,}".replace(",", "."),
                width=LARGURA_COMPONENTE,
            )

        except Exception as erro:
            st.error(
                "A unificação foi interrompida.",
                width=LARGURA_COMPONENTE,
            )
            st.exception(erro)