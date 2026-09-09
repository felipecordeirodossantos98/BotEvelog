import re
from datetime import datetime

import pandas as pd
from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from servicos.fraction import (
    executar_com_retentativas_fraction,
    recuperar_sessao_fraction,
)
from servicos.meudanfe import baixar_danfes
from utils.arquivos import salvar_planilha_reimportacao
from utils.config import (
    FRACTION_PASSWORD,
    FRACTION_USER,
    HEADLESS,
    RESULTADOS_DANFES,
    URL_FRACTION,
)


def carregar_config_fraction():
    return (
        URL_FRACTION,
        FRACTION_USER,
        FRACTION_PASSWORD,
        HEADLESS,
    )


def _login_fraction_danfe(
    page,
    url_fraction,
    usuario,
    senha,
    log,
):
    """
    Mantém os mesmos seletores e timeouts do login já validado
    no fluxo de DANFEs.
    """
    log("Fazendo login no FractionWeb...")

    page.goto(
        url_fraction,
        wait_until="domcontentloaded",
        timeout=60000,
    )

    page.fill(
        "input[name='id_usuario']",
        usuario,
    )

    page.fill(
        "input[name='id_senha']",
        senha,
    )

    page.click(
        "input[type='submit']"
    )

    page.wait_for_load_state(
        "networkidle",
        timeout=60000,
    )

    try:
        page.get_by_role(
            "button",
            name="Close",
        ).first.click(
            timeout=5000
        )
    except Exception:
        pass

    log("Login realizado.")


def _consultar_chave_fraction(
    page,
    pedido,
    log,
) -> str:
    """
    Consulta uma chave mantendo os mesmos elementos/timeouts
    que o fluxo já utilizava antes da blindagem.
    """
    log(
        f"{pedido}: buscando chave da NF-e..."
    )

    page.get_by_role(
        "link",
        name="Consultas",
    ).click(
        timeout=15000
    )

    page.get_by_role(
        "link",
        name="Pesquisar",
    ).click(
        timeout=15000
    )

    page.wait_for_load_state(
        "networkidle",
        timeout=60000,
    )

    campo = page.get_by_role(
        "textbox"
    ).first

    campo.fill("")
    campo.fill(
        pedido
    )

    page.get_by_role(
        "button",
        name="Processar",
    ).click()

    toggler = page.locator(
        ".ui-tree-toggler"
    ).first

    toggler.wait_for(
        timeout=15000
    )

    toggler.click()

    item_nf = (
        page.get_by_role(
            "treeitem"
        )
        .filter(
            has_text="NFe:"
        )
        .last
    )

    item_nf.wait_for(
        timeout=15000
    )

    match = re.search(
        r"\d{44}",
        item_nf.inner_text(),
    )

    if not match:
        raise RuntimeError(
            "Chave de 44 dígitos não encontrada"
        )

    return match.group()


def buscar_chaves(
    df: pd.DataFrame,
    log=lambda msg: None,
) -> pd.DataFrame:
    (
        url_fraction,
        usuario,
        senha,
        headless,
    ) = carregar_config_fraction()

    resultado = df.copy()
    resultado["chave_nfe"] = ""
    resultado["status_fraction"] = "PENDENTE"
    resultado["status_danfe"] = "PENDENTE"
    resultado["mensagem"] = ""

    with sync_playwright() as p:
        browser = None
        context = None
        page = None

        def fechar_sessao():
            nonlocal browser, context, page

            if context is not None:
                try:
                    context.close()
                except Exception as erro:
                    log(
                        "Aviso ao fechar contexto do Fraction: "
                        f"{type(erro).__name__}: {erro}"
                    )

            if browser is not None:
                try:
                    browser.close()
                except Exception as erro:
                    log(
                        "Aviso ao fechar Chromium do Fraction: "
                        f"{type(erro).__name__}: {erro}"
                    )

            browser = None
            context = None
            page = None

        def abrir_sessao():
            nonlocal browser, context, page

            fechar_sessao()

            browser = p.chromium.launch(
                headless=headless,
                slow_mo=0 if headless else 200,
            )

            context = browser.new_context()
            page = context.new_page()

            _login_fraction_danfe(
                page,
                url_fraction,
                usuario,
                senha,
                log,
            )

        def recuperar_sessao():
            recuperar_sessao_fraction(
                page,
                log,
                login_callback=lambda: _login_fraction_danfe(
                    page,
                    url_fraction,
                    usuario,
                    senha,
                    log,
                ),
            )

        try:
            abrir_sessao()

            for index, row in resultado.iterrows():
                pedido = str(
                    row["pedido"]
                ).strip()

                if (
                    not pedido
                    or pedido.lower() == "nan"
                ):
                    resultado.at[
                        index,
                        "status_fraction",
                    ] = "PULADO"

                    resultado.at[
                        index,
                        "status_danfe",
                    ] = "PULADO"

                    resultado.at[
                        index,
                        "mensagem",
                    ] = "Pedido vazio"

                    continue

                def consultar():
                    return _consultar_chave_fraction(
                        page,
                        pedido,
                        log,
                    )

                try:
                    chave = executar_com_retentativas_fraction(
                        consultar,
                        recuperar_sessao,
                        log,
                        descricao=f"Pedido {pedido}",
                        tentativas=5,
                    )

                    resultado.at[
                        index,
                        "chave_nfe",
                    ] = chave

                    resultado.at[
                        index,
                        "status_fraction",
                    ] = "OK"

                    resultado.at[
                        index,
                        "mensagem",
                    ] = "Chave encontrada"

                    log(
                        f"{pedido}: {chave}"
                    )

                except PlaywrightTimeoutError:
                    log(
                        f"{pedido}: retentativas com refresh "
                        "esgotadas. Reiniciando o Chromium para "
                        "uma última tentativa."
                    )

                    try:
                        abrir_sessao()

                        chave = consultar()

                        resultado.at[
                            index,
                            "chave_nfe",
                        ] = chave

                        resultado.at[
                            index,
                            "status_fraction",
                        ] = "OK"

                        resultado.at[
                            index,
                            "mensagem",
                        ] = "Chave encontrada após reiniciar Chromium"

                        log(
                            f"{pedido}: consulta recuperada após "
                            "reiniciar o Chromium."
                        )

                    except Exception as exc:
                        resultado.at[
                            index,
                            "status_fraction",
                        ] = "ERRO"

                        resultado.at[
                            index,
                            "status_danfe",
                        ] = "PULADO"

                        resultado.at[
                            index,
                            "mensagem",
                        ] = f"FractionWeb: {exc}"

                        log(
                            f"{pedido}: erro definitivo no "
                            f"FractionWeb — {exc}"
                        )

                        # Deixa uma sessão limpa preparada para o próximo
                        # pedido. Se nem isso for possível, para de insistir
                        # e marca os restantes para reimportação.
                        try:
                            abrir_sessao()

                        except Exception as erro_reabertura:
                            mensagem = (
                                "FractionWeb indisponível após reiniciar "
                                f"o navegador: {type(erro_reabertura).__name__}: "
                                f"{erro_reabertura}"
                            )

                            log(
                                f"{mensagem}"
                            )

                            pendentes = resultado[
                                resultado["status_fraction"]
                                == "PENDENTE"
                            ].index

                            for indice_pendente in pendentes:
                                resultado.at[
                                    indice_pendente,
                                    "status_fraction",
                                ] = "ERRO"

                                resultado.at[
                                    indice_pendente,
                                    "status_danfe",
                                ] = "PULADO"

                                resultado.at[
                                    indice_pendente,
                                    "mensagem",
                                ] = mensagem

                            break

                except Exception as exc:
                    resultado.at[
                        index,
                        "status_fraction",
                    ] = "ERRO"

                    resultado.at[
                        index,
                        "status_danfe",
                    ] = "PULADO"

                    resultado.at[
                        index,
                        "mensagem",
                    ] = f"FractionWeb: {exc}"

                    log(
                        f"{pedido}: erro no FractionWeb — {exc}"
                    )

        except Exception as exc:
            mensagem = (
                "FractionWeb indisponível: "
                f"{type(exc).__name__}: {exc}"
            )

            log(
                f"{mensagem}"
            )

            pendentes = resultado[
                resultado["status_fraction"]
                == "PENDENTE"
            ].index

            for index in pendentes:
                resultado.at[
                    index,
                    "status_fraction",
                ] = "ERRO"

                resultado.at[
                    index,
                    "status_danfe",
                ] = "PULADO"

                resultado.at[
                    index,
                    "mensagem",
                ] = mensagem

        finally:
            fechar_sessao()

    return resultado


def executar_baixar_danfes(
    arquivo,
    log=lambda msg: None,
) -> dict:
    arquivo.seek(0)

    df_importacao = pd.read_excel(
        arquivo,
        header=0,
        dtype=object,
    )

    arquivo.seek(0)

    if df_importacao.empty:
        raise ValueError(
            "A planilha não possui pedidos."
        )

    cabecalho_importacao = str(
        df_importacao.columns[0]
    )

    df = df_importacao.iloc[
        :,
        [0],
    ].copy()

    df.columns = [
        "pedido"
    ]

    df = (
        df[
            df["pedido"].notna()
        ]
        .reset_index(
            drop=True
        )
    )

    agora = datetime.now().strftime(
        "%Y-%m-%d_%H-%M-%S"
    )

    pasta_resultado = (
        RESULTADOS_DANFES
        / f"danfes_{agora}"
    )

    pasta_resultado.mkdir(
        parents=True,
        exist_ok=True,
    )

    resultado = buscar_chaves(
        df,
        log=log,
    )

    resultado = baixar_danfes(
        resultado,
        pasta=pasta_resultado,
        log=log,
    )

    falhas = resultado[
        (
            resultado["pedido"]
            .fillna("")
            .astype(str)
            .str.strip()
            != ""
        )
        & (
            resultado["status_danfe"]
            != "OK"
        )
    ].copy()

    arquivo_erros = None

    if not falhas.empty:
        reimportar = pd.DataFrame(
            {
                cabecalho_importacao: (
                    falhas["pedido"]
                    .astype(str)
                    .tolist()
                )
            }
        )

        arquivo_erros = salvar_planilha_reimportacao(
            reimportar,
            pasta_resultado,
            "erros_reimportar",
            colunas_texto=(
                cabecalho_importacao,
            ),
        )

        log(
            f"{len(reimportar)} pedido(s) ficaram pendentes. "
            "Planilha pronta para reimportação: "
            f"{arquivo_erros}"
        )

    log(
        f"Arquivos salvos em: {pasta_resultado}"
    )
    log("Processo finalizado.")

    return {
        "resultado": resultado,
        "pasta": pasta_resultado,
        "arquivo_erros": arquivo_erros,
        "total_erros": len(falhas),
    }
