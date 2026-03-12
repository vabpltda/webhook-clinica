"""
Webhook da Clínica - Triagem de Mensagens WhatsApp
====================================================
Recebe mensagens via webhook, analisa o sentimento com Claude AI,
e envia alertas para o Telegram quando detecta mensagens negativas
(reclamações, cancelamentos, pedidos de estorno).

Uso:
    python webhook_clinica.py

O servidor ficará rodando na porta 5000.
Configure seu WhatsApp Business para enviar mensagens para:
    http://SEU_IP:5000/webhook
"""

import os
import json
import requests
import logging
from flask import Flask, request, jsonify
from flask_cors import CORS
from anthropic import Anthropic

# ─────────────────────────────────────────────
#  CONFIGURAÇÕES — edite aqui antes de rodar
# ─────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = "8704137786:AAEgJxISJW2CGZ5ODrXFTEDYSI4CooMVqgo"
TELEGRAM_USERNAME  = "victorabp"          # sem o @
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")

# URL do webhook que você registrou no WhatsApp Business
# (não é necessário configurar aqui, apenas documental)
WEBHOOK_URL = "https://webhook.site/9ff48186-2eb9-45c5-98ab-56291a80e2f4"

# ─────────────────────────────────────────────
#  CONFIGURAÇÃO DE LOG
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("webhook_clinica.log"),
    ],
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)
client = Anthropic(api_key=ANTHROPIC_API_KEY)


# ─────────────────────────────────────────────
#  FUNÇÕES PRINCIPAIS
# ─────────────────────────────────────────────

def analisar_mensagem(texto: str) -> dict:
    """
    Usa Claude para analisar se a mensagem é negativa e gerar um resumo.
    Retorna dict com:
        - eh_negativa  (bool)
        - categoria    (str)  ex: "Solicitação de estorno"
        - resumo       (str)  resumo em até 3 linhas
        - urgencia     (str)  "Alta", "Média" ou "Baixa"
    """
    prompt = f"""Você é um assistente jurídico/administrativo de uma clínica médica.
Analise a mensagem abaixo recebida pelo WhatsApp da clínica e responda em JSON.

Mensagem do paciente:
\"\"\"{texto}\"\"\"

Responda SOMENTE com um JSON válido no formato abaixo (sem markdown, sem explicações):
{{
  "eh_negativa": true ou false,
  "categoria": "uma das opções: Solicitação de estorno | Cancelamento de agendamento | Reclamação geral | Reclamação sobre atendimento | Reclamação sobre cobrança | Ameaça / escalada jurídica | Outro negativo | Positiva ou neutra",
  "resumo": "Resumo objetivo da mensagem em até 3 linhas, com os pontos principais.",
  "urgencia": "Alta, Média ou Baixa",
  "nome_paciente": "nome do paciente se mencionado, senão 'Não identificado'"
}}

Considere mensagem NEGATIVA qualquer pedido de estorno, reembolso, cancelamento, reclamação, insatisfação, ameaça de processo, ou expressão de frustração."""

    try:
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        # Remove possíveis blocos de markdown
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw)
    except Exception as e:
        logger.error(f"Erro ao analisar com Claude: {e}")
        return {
            "eh_negativa": False,
            "categoria": "Erro na análise",
            "resumo": f"Não foi possível analisar a mensagem. Erro: {e}",
            "urgencia": "Média",
            "nome_paciente": "Não identificado",
        }


def obter_chat_id(username: str) -> int | None:
    """
    Tenta obter o chat_id de um usuário do Telegram pelo username.
    NOTA: O bot precisa ter recebido ao menos uma mensagem do usuário antes.
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    try:
        resp = requests.get(url, timeout=10)
        updates = resp.json().get("result", [])
        for update in reversed(updates):  # mais recente primeiro
            msg = update.get("message") or update.get("channel_post")
            if msg:
                chat = msg.get("chat", {})
                uname = chat.get("username", "")
                if uname.lower() == username.lower():
                    return chat["id"]
    except Exception as e:
        logger.error(f"Erro ao obter chat_id: {e}")
    return None


def enviar_telegram(analise: dict, mensagem_original: str, remetente: str = "Paciente") -> bool:
    """
    Envia alerta formatado para o Telegram.
    """
    # Emojis por urgência
    emoji_urgencia = {"Alta": "🔴", "Média": "🟡", "Baixa": "🟢"}.get(analise.get("urgencia", "Média"), "⚪")
    emoji_categoria = {
        "Solicitação de estorno": "💸",
        "Cancelamento de agendamento": "📅",
        "Reclamação geral": "😠",
        "Reclamação sobre atendimento": "🏥",
        "Reclamação sobre cobrança": "💳",
        "Ameaça / escalada jurídica": "⚖️",
    }.get(analise.get("categoria", ""), "❗")

    mensagem_telegram = (
        f"{emoji_urgencia} *ALERTA — MENSAGEM NEGATIVA RECEBIDA*\n\n"
        f"{emoji_categoria} *Categoria:* {analise.get('categoria', 'N/A')}\n"
        f"👤 *Paciente:* {analise.get('nome_paciente', 'Não identificado')}\n"
        f"📊 *Urgência:* {analise.get('urgencia', 'N/A')}\n\n"
        f"📝 *Resumo da Mensagem:*\n{analise.get('resumo', 'Sem resumo disponível')}\n\n"
        f"💬 *Mensagem original:*\n_{mensagem_original[:400]}{'...' if len(mensagem_original) > 400 else ''}_"
    )

    # Tenta obter o chat_id pelo username
    chat_id = obter_chat_id(TELEGRAM_USERNAME)

    if not chat_id:
        logger.warning(
            f"Não foi possível encontrar chat_id para @{TELEGRAM_USERNAME}. "
            "Certifique-se de que o usuário enviou ao menos uma mensagem para o bot."
        )
        # Fallback: tenta enviar para o próprio username (funciona para canais/grupos públicos)
        chat_id = f"@{TELEGRAM_USERNAME}"

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": mensagem_telegram,
        "parse_mode": "Markdown",
    }

    try:
        resp = requests.post(url, json=payload, timeout=10)
        result = resp.json()
        if result.get("ok"):
            logger.info(f"✅ Alerta Telegram enviado com sucesso para @{TELEGRAM_USERNAME}")
            return True
        else:
            logger.error(f"Telegram retornou erro: {result}")
            return False
    except Exception as e:
        logger.error(f"Erro ao enviar para Telegram: {e}")
        return False


# ─────────────────────────────────────────────
#  ROTAS DO WEBHOOK
# ─────────────────────────────────────────────

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        # Verificação do webhook (usada por alguns providers como WhatsApp Cloud API)
        challenge = request.args.get("hub.challenge")
        if challenge:
            return challenge, 200
        return jsonify({"status": "Webhook ativo ✅"}), 200

    # POST — processa a mensagem recebida
    try:
        data = request.get_json(force=True) or {}
        logger.info(f"Payload recebido: {json.dumps(data, ensure_ascii=False)[:500]}")

        # ── Extrai texto e remetente conforme o formato do payload ──
        # Suporta vários formatos comuns (WhatsApp Cloud API, Z-API, Evolution API, genérico)
        texto = ""
        remetente = "Paciente"

        # WhatsApp Cloud API (Meta)
        entry = data.get("entry", [{}])[0] if data.get("entry") else {}
        changes = entry.get("changes", [{}])[0] if entry.get("changes") else {}
        value = changes.get("value", {})
        messages = value.get("messages", [])
        if messages:
            msg = messages[0]
            texto = msg.get("text", {}).get("body", "")
            remetente = msg.get("from", "Paciente")

        # Z-API / Evolution API
        if not texto:
            texto = (
                data.get("text", {}).get("message", "")
                or data.get("body", "")
                or data.get("message", "")
                or data.get("content", "")
                or str(data)
            )
            remetente = (
                data.get("phone", "")
                or data.get("from", "")
                or data.get("sender", "Paciente")
            )

        # Formato genérico / webhook.site (plain text ou JSON simples)
        if not texto or texto == str(data):
            raw_body = request.get_data(as_text=True)
            if raw_body and not raw_body.startswith("{"):
                texto = raw_body.strip()

        if not texto:
            logger.warning("Nenhum texto encontrado no payload.")
            return jsonify({"status": "ok", "info": "sem texto"}), 200

        logger.info(f"📩 Mensagem recebida de [{remetente}]: {texto[:200]}")

        # ── Analisa com Claude ──
        analise = analisar_mensagem(texto)
        logger.info(f"Análise: {analise}")

        # ── Envia para Telegram se for negativa ──
        if analise.get("eh_negativa"):
            enviar_telegram(analise, texto, remetente)
        else:
            logger.info("Mensagem classificada como positiva/neutra. Nenhum alerta enviado.")

        return jsonify({"status": "ok", "analise": analise}), 200

    except Exception as e:
        logger.error(f"Erro no processamento do webhook: {e}", exc_info=True)
        return jsonify({"status": "erro", "mensagem": str(e)}), 500


@app.route("/testar", methods=["POST"])
def testar():
    """
    Endpoint de teste. Envie um JSON com {"mensagem": "..."} para simular.
    Exemplo:
        curl -X POST http://localhost:5000/testar \\
             -H "Content-Type: application/json" \\
             -d '{"mensagem": "Quero cancelar minha consulta e pedir reembolso"}'
    """
    data = request.get_json(force=True) or {}
    texto = data.get("mensagem", "")
    if not texto:
        return jsonify({"erro": "Forneça o campo 'mensagem'"}), 400

    analise = analisar_mensagem(texto)

    if analise.get("eh_negativa"):
        enviado = enviar_telegram(analise, texto)
        analise["telegram_enviado"] = enviado
    else:
        analise["telegram_enviado"] = False

    return jsonify(analise), 200


@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "servico": "Webhook Clínica — Triagem de Mensagens WhatsApp",
        "status": "ativo",
        "rotas": {
            "GET /webhook": "Verificação do webhook",
            "POST /webhook": "Recebe mensagens do WhatsApp",
            "POST /testar": "Testa manualmente com JSON {mensagem: '...'}",
        }
    }), 200


# ─────────────────────────────────────────────
#  ENTRADA
# ─────────────────────────────────────────────

if __name__ == "__main__":
    if not ANTHROPIC_API_KEY:
        print("⚠️  ATENÇÃO: A variável de ambiente ANTHROPIC_API_KEY não está definida!")
        print("   Defina-a antes de rodar: export ANTHROPIC_API_KEY='sua-chave-aqui'")
    else:
        print("✅ Anthropic API Key carregada.")

    print(f"🤖 Bot Telegram configurado para @{TELEGRAM_USERNAME}")
    print("🚀 Iniciando servidor na porta 5000...")
    app.run(host="0.0.0.0", port=5000, debug=False)
