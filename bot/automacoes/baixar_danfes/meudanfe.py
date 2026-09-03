from pathlib import Path
import base64
import os
import time

import pandas as pd
import requests
from dotenv import load_dotenv

DEFAULT_API_BASE_URL = "https://api.meudanfe.com.br/v2"

PASTA_FLUXO = Path(__file__).resolve().parent
PASTA_PROJETO = Path(__file__).resolve().parents[2]

BASE_DIR = PASTA_FLUXO
ENV_FILE = PASTA_PROJETO / ".env"


HTTP_MESSAGES = {
    400: "Chave de acesso inválida",
    401: "Api-Key não informada ou inválida",
    402: "Saldo insuficiente no Meu DANFE",
    403: "Api-Key substituída/inválida. Gere ou confira a chave em API / Integração",
    404: "NF-e ainda não encontrada na Área do Cliente",
    500: "Erro interno do Meu DANFE",
}


def carregar_config_api():
    load_dotenv(ENV_FILE, override=False)

    api_key = os.getenv("API_KEY_MEUDANFE", "").strip()
    if not api_key:
        raise RuntimeError("Variável API_KEY_MEUDANFE não informada no arquivo .env")

    # Mantém o nome URL_MEDANFE conforme o .env definido no projeto.
    # Também aceita URL_MEUDANFE caso o nome seja corrigido futuramente.
    base_url = (
        os.getenv("URL_MEDANFE", "").strip()
        or os.getenv("URL_MEUDANFE", "").strip()
        or DEFAULT_API_BASE_URL
    ).rstrip("/")

    return api_key, base_url



def _mensagem_http(response):
    texto = HTTP_MESSAGES.get(response.status_code)
    if texto:
        return texto

    try:
        corpo = response.json()
        detalhe = corpo.get("statusMessage") or corpo.get("message") or corpo.get("error")
        if detalhe:
            return str(detalhe)
    except Exception:
        pass

    corpo = response.text.strip()
    return corpo[:250] if corpo else f"HTTP {response.status_code}"


def _decodificar_pdf(dados_json):
    if not isinstance(dados_json, dict):
        raise RuntimeError("Resposta de download inválida: JSON esperado")

    formato = str(dados_json.get("format", "")).upper()
    if formato and formato != "BASE64":
        raise RuntimeError(f"Formato inesperado retornado pela API: {formato}")

    conteudo_base64 = dados_json.get("data")
    if not conteudo_base64:
        raise RuntimeError("A API não retornou o campo data com o PDF")

    conteudo_base64 = str(conteudo_base64).strip()
    if "," in conteudo_base64 and conteudo_base64.lower().startswith("data:application/pdf"):
        conteudo_base64 = conteudo_base64.split(",", 1)[1]

    try:
        pdf = base64.b64decode(conteudo_base64, validate=False)
    except Exception as exc:
        raise RuntimeError(f"Não foi possível decodificar o PDF em Base64: {exc}") from exc

    if not pdf.startswith(b"%PDF"):
        raise RuntimeError("O conteúdo retornado não parece ser um PDF válido")

    return pdf


def _baixar_pdf_quando_disponivel(
    session,
    base_url,
    chave,
    timeout_total=60,
    intervalo=2,
    log=lambda msg: None,
    pedido="",
):
    """Consulta o endpoint gratuito de download até o documento ficar disponível.

    O PUT de inclusão é feito apenas uma vez. Enquanto o Meu DANFE estiver
    processando a consulta (WAITING/SEARCHING), repetimos somente o GET gratuito.
    """
    url = f"{base_url}/fd/get/da/{chave}"
    inicio = time.monotonic()
    tentativa = 0

    while True:
        tentativa += 1
        response = session.get(url, timeout=90)

        if response.status_code == 200:
            try:
                dados = response.json()
            except Exception as exc:
                raise RuntimeError("Meu DANFE retornou HTTP 200, mas a resposta não é JSON") from exc
            return _decodificar_pdf(dados)

        # 404 é esperado enquanto a consulta ainda não terminou/adicionou a nota.
        if response.status_code != 404:
            raise RuntimeError(
                f"download HTTP {response.status_code}: {_mensagem_http(response)}"
            )

        decorrido = time.monotonic() - inicio
        if decorrido >= timeout_total:
            raise RuntimeError(
                f"DANFE não ficou disponível após {timeout_total}s"
            )

        log(
            f"⏳ {pedido}: DANFE ainda em processamento; "
            f"nova tentativa em {intervalo}s..."
        )
        time.sleep(intervalo)


def baixar_danfes(
    df: pd.DataFrame,
    pasta="danfes",
    log=lambda msg: None,
    timeout_processamento=60,
    intervalo_consulta=2,
) -> pd.DataFrame:
    api_key, base_url = carregar_config_api()
    resultado = df.copy()
    destino = Path(pasta)
    destino.mkdir(parents=True, exist_ok=True)

    base_url = (base_url or DEFAULT_API_BASE_URL).rstrip("/")

    session = requests.Session()
    session.headers.update(
        {
            "Api-Key": api_key,
            "Accept": "application/json",
        }
    )

    for index, row in resultado.iterrows():
        pedido = str(row.get("pedido", "")).strip()
        chave = str(row.get("chave_nfe", "")).strip()

        if row.get("status_fraction") != "OK" or len(chave) != 44 or not chave.isdigit():
            if resultado.at[index, "status_danfe"] == "PENDENTE":
                resultado.at[index, "status_danfe"] = "PULADO"
            if not resultado.at[index, "mensagem"]:
                resultado.at[index, "mensagem"] = "Chave da NF-e não disponível"
            continue

        try:
            arquivo = destino / f"{pedido}.pdf"
            log(f"🌐 {pedido}: enviando consulta ao Meu DANFE...")

            # Etapa paga (R$ 0,03 quando a NF-e ainda não está armazenada).
            # É feita somente uma vez por execução/pedido.
            response_add = session.put(
                f"{base_url}/fd/add/{chave}",
                timeout=90,
            )

            if response_add.status_code != 200:
                raise RuntimeError(
                    f"consulta HTTP {response_add.status_code}: {_mensagem_http(response_add)}"
                )

            try:
                dados_add = response_add.json()
            except Exception as exc:
                raise RuntimeError("Resposta da consulta não é um JSON válido") from exc

            status = str(dados_add.get("status", "")).upper().strip()
            status_msg = str(dados_add.get("statusMessage", "")).strip()

            if status in {"NOT_FOUND", "ERROR"}:
                detalhe = status_msg or status
                raise RuntimeError(f"consulta retornou {status}: {detalhe}")

            if status in {"WAITING", "SEARCHING"}:
                log(f"⏳ {pedido}: consulta em processamento ({status}).")
                # A documentação alerta para aguardar pelo menos 1 segundo.
                # Usamos 2 segundos por segurança e, depois, consultamos apenas o GET gratuito.
                time.sleep(intervalo_consulta)
            elif status == "OK":
                log(f"✅ {pedido}: NF-e disponível no Meu DANFE.")
            elif status:
                log(f"ℹ️ {pedido}: status retornado pela API: {status}.")

            log(f"📥 {pedido}: obtendo DANFE PDF...")
            pdf = _baixar_pdf_quando_disponivel(
                session=session,
                base_url=base_url,
                chave=chave,
                timeout_total=timeout_processamento,
                intervalo=intervalo_consulta,
                log=log,
                pedido=pedido,
            )

            arquivo.write_bytes(pdf)

            resultado.at[index, "status_danfe"] = "OK"
            resultado.at[index, "mensagem"] = "DANFE baixado"
            log(f"✅ {pedido}: DANFE salvo como {arquivo.name}")

        except Exception as exc:
            resultado.at[index, "status_danfe"] = "ERRO"
            resultado.at[index, "mensagem"] = f"Meu DANFE: {exc}"
            log(f"❌ {pedido}: erro no DANFE — {exc}")

    return resultado