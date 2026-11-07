"""Connector para a API do Google Gemini (Classificador Semântico sob Contrato).

Conceitualmente, este módulo NÃO é um agente autônomo: o modelo não escolhe ferramentas,
não controla o laço de execução e não tem poder de escrita sobre a planilha ou o Gmail.
Ele atua como um tradutor semântico isolado atrás de um connector: recebe texto livre (a conversa),
traduz em um JSON restrito pelo schema e devolve um dict cru para que o nosso domínio valide
se a transição de estado é permitida.

Guarda três coisas: qual modelo chamar, o tempo máximo de espera (TIMEOUT) e o desembrulho
da resposta em dict.
"""

import json
from typing import Any, Protocol

from google import genai
from google.genai import types

from src.domain.enums import ReplyIntent, ThreadStatus

DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"

# Schema de saída passado ao Gemini. Restringe o formato que vem de fora antes de chegar no código.
STAGE2_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "required": ["status", "intent", "summary_context"],
    "properties": {
        "status": {
            "type": "STRING",
            "enum": [
                ThreadStatus.REPLIED.value,
                ThreadStatus.PIVOT_REDIRECTED.value,
                ThreadStatus.AUTO_REPLY_OOO.value,
            ],
        },
        "intent": {
            "type": "STRING",
            "enum": [
                ReplyIntent.POSITIVE_CONFIRMED.value,
                ReplyIntent.QUESTION_OR_RESCHEDULE.value,
                ReplyIntent.DECLINED.value,
                ReplyIntent.REDIRECTED_CONTACT.value,
                ReplyIntent.OUT_OF_OFFICE_NO_ALT.value,
            ],
        },
        "alternative_email": {
            "type": "STRING",
            "nullable": True,
        },
        "summary_context": {
            "type": "STRING",
        },
    },
}

SYSTEM_INSTRUCTION = (
    "Você analisa o histórico cronológico de uma conversa de e-mail iniciada pela nossa equipe "
    "e devolve a classificação do estado atual em JSON:\n"
    "1. Férias/ausência automática (Out of Office) COM indicação de outro e-mail: "
    "status='PIVOTAR_REDIRECIONADO', intent='INDICOU_OUTRO_CONTATO' e 'alternative_email'.\n"
    "2. Férias/ausência automática SEM substituto: "
    "status='AUSENTE_AUTO_REPLY', intent='AUSENTE_SEM_SUBSTITUTO' e alternative_email=null.\n"
    "3. Resposta humana indicando falar com outra pessoa/setor: "
    "status='PIVOTAR_REDIRECIONADO', intent='INDICOU_OUTRO_CONTATO' e 'alternative_email'.\n"
    "4. Resposta humana tratando do assunto: status='RESPONDIDO' e intent entre "
    "'POSITIVO_CONFIRMADO', 'DUVIDA_OU_REAGENDAR' ou 'RECUSADO'.\n"
    "5. 'summary_context' deve resumir a resposta em português em até 120 caracteres."
)


class GenAIModelsClientProtocol(Protocol):
    """Contrato mínimo do cliente SDK para permitir injeção de stub nos testes sem rede."""

    def generate_content(
        self,
        *,
        model: str,
        contents: str,
        config: types.GenerateContentConfig,
    ) -> Any:
        ...


class GeminiConnectorError(Exception):
    """Falha de comunicação ou resposta ilegível vinda do serviço externo (Gemini)."""


class GeminiConnector:
    """Conversa com a API do Google Gemini para análise semântica de respostas externas.

    Segue a regra de ouro de connectors: tem TIMEOUT explícito (quem está fora sempre tem prazo),
    monta a chamada externa, desembrulha o JSON e devolve um dict simples.
    """

    TIMEOUT_SECONDS = 15  # quem está fora sempre tem prazo (15s = 15_000ms na config HTTP do SDK)

    def __init__(
        self,
        api_key: str | None = None,
        model_name: str = DEFAULT_GEMINI_MODEL,
        models_client: GenAIModelsClientProtocol | None = None,
    ) -> None:
        """Configura o modelo (`gemini-2.5-flash` por padrão ou `gemini-2.5-pro`) e o cliente HTTP.

        A chave vem da variável de ambiente `GEMINI_API_KEY`, nunca fixa no código.
        """
        cleaned_model = model_name.strip() if model_name else DEFAULT_GEMINI_MODEL
        if not cleaned_model:
            raise ValueError("model_name não pode ser vazio.")
        self.model_name = cleaned_model

        if models_client is not None:
            self._models_client = models_client
        else:
            if not api_key or not api_key.strip():
                raise ValueError(
                    "GEMINI_API_KEY é obrigatória quando models_client não é injetado."
                )
            http_options = types.HttpOptions(timeout=self.TIMEOUT_SECONDS * 1000)
            client = genai.Client(api_key=api_key.strip(), http_options=http_options)
            self._models_client = client.models

    def analyze_transcript(self, transcript: str) -> dict[str, Any]:
        """Envia a transcrição da conversa para o Gemini e devolve o JSON desembrulhado em dict.

        Repare que o connector não decide se a transição de status faz sentido no negócio:
        ele apenas garante que o serviço de fora respondeu dentro do prazo e que o corpo é um JSON.
        """
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            response_mime_type="application/json",
            response_schema=STAGE2_RESPONSE_SCHEMA,
            temperature=0.0,
        )

        try:
            response = self._models_client.generate_content(
                model=self.model_name,
                contents=transcript,
                config=config,
            )
        except Exception as exc:
            raise GeminiConnectorError(
                f"Serviço externo Gemini ({self.model_name}) falhou ou excedeu timeout: {exc}"
            ) from exc

        raw_text = getattr(response, "text", None)
        if not raw_text or not str(raw_text).strip():
            raise GeminiConnectorError("Gemini devolveu corpo vazio.")

        try:
            unwrapped = json.loads(str(raw_text))
        except json.JSONDecodeError as exc:
            raise GeminiConnectorError(f"Resposta do Gemini não é um JSON válido: {exc}") from exc

        if not isinstance(unwrapped, dict):
            raise GeminiConnectorError("Formato inesperado: o JSON do Gemini não é um objeto.")

        return unwrapped  # desembrulha e devolve; não decide nada
