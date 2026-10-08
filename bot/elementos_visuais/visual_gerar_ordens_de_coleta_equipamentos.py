from __future__ import annotations

import streamlit as st

from automacoes.gerar_ordens_de_coleta_equipamentos import executar_automacao, preparar_pdfs
from utils.config import HEADLESS
from utils.interface import LARGURA_BOTAO, avisar_variaveis_faltantes, criar_painel_execucao, error, warning
from utils.logs import criar_logger_execucao

CHAVE_LOGS = "visual_gerar_ordens_equipamentos_logs"

def renderizar_gerar_ordens_de_coleta_equipamentos(arquivos) -> None:
    st.subheader("Gerar ordens de coleta — Equipamentos")
    st.caption("Importe uma ou mais NFs em PDF. Cada PDF gera uma ordem de coleta.")
    if not arquivos:
        st.info("Importe um ou mais PDFs de nota fiscal para começar.")
        return
    faltantes = avisar_variaveis_faltantes("gerar_ordens_de_coleta_equipamentos")
    df_nf, erros = preparar_pdfs(arquivos)
    for mensagem in erros:
        warning(mensagem)
    if df_nf.empty:
        error("Nenhum PDF pôde ser processado.")
        return
    st.info(f"{len(df_nf)} PDF(s) válido(s) pronto(s) para geração.")
    iniciar = st.button("Gerar ordens", type="primary", width=LARGURA_BOTAO, disabled=bool(faltantes), key="visual_gerar_ordens_equipamentos_iniciar")
    if not iniciar:
        return
    feedback, log_visual = criar_painel_execucao(CHAVE_LOGS)
    log, _ = criar_logger_execucao("gerar_ordens_de_coleta_equipamentos", log_visual)
    try:
        with st.spinner("Gerando ordens de coleta..."):
            resultado = executar_automacao(df_nf=df_nf, headless=HEADLESS, continuar_em_erro=True, log=log)
        caminho_resultado = resultado.get("arquivo_resultado")
        mensagem = (f"Processamento concluído. Autorizadas: {resultado['sucessos']} | "
                    f"Falhas: {resultado['falhas']} | Status indeterminado: {resultado['indeterminados']}")
        feedback.success(mensagem, width="stretch")
        if caminho_resultado:
            st.success(f"Planilha final gerada: {caminho_resultado}")
    except Exception as erro_execucao:
        log(f"Erro: {type(erro_execucao).__name__}: {erro_execucao}")
        feedback.error(f"A automação foi interrompida: {erro_execucao}", width="stretch")
