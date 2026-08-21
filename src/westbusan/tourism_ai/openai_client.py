"""Minimal, secret-safe OpenAI Responses API client."""

from __future__ import annotations

import json
from typing import Any

import httpx

from westbusan.tourism_ai.models import EvidenceMetric, MapSelection, ModelInsight


class OpenAIInsightError(RuntimeError):
    """An upstream insight could not be safely produced or parsed."""


_INSTRUCTIONS = """
당신은 부산 관광정책 내부검토를 지원하는 데이터 분석가입니다.
데이터로 확인된 사실과 정책 아이디어를 명확히 구분하십시오.
사실 판단에는 제공된 지표만 사용하고 외부사실을 추가하지 마십시오.
관광 종합현황, 공급 격차, 민간투자 유도 세 질문을 모두 다루십시오.
민간투자 대안은 신규 공급, 리모델링, 빈집 전환, 콘텐츠 중 해당 유형을
선택하고 우선순위를 부여하십시오. metric_ids에는 제공된 식별자만 넣고,
정책 아이디어에는 금융지원, 규제 사전검토, 교통·관광콘텐츠 연계, 민간사업자
공모처럼 지표에서 직접 나오지 않더라도 일반적인 정책수단을 제안할 수 있습니다.
다만 제안임을 명시하고 선택지역의 법적 가능성이나 사업성을 확정하지 마십시오.
법적 적합성·안전성·수익성을 확정하지 마십시오. 체류시간, 교통 또는
소비금액처럼 제공되지 않은 지표는 추정하지 마십시오.
""".strip()


class OpenAIResponsesClient:
    """Call Structured Outputs without exposing upstream bodies."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        transport: httpx.BaseTransport | None = None,
        endpoint: str = "https://api.openai.com/v1/responses",
        max_output_tokens: int = 1800,
    ) -> None:
        if not api_key.strip():
            raise ValueError("missing_openai_api_key")
        self._api_key = api_key
        self.model = model
        self.endpoint = endpoint
        self.max_output_tokens = max_output_tokens
        self._client = httpx.Client(transport=transport, timeout=45.0)

    def generate(
        self,
        catalogue: dict[str, EvidenceMetric],
        *,
        focus_region: str,
        focus_selection: MapSelection | None = None,
    ) -> ModelInsight:
        metrics = [
            metric.model_dump(mode="json")
            for metric in sorted(catalogue.values(), key=lambda item: item.metric_id)
        ]
        payload: dict[str, Any] = {
            "model": self.model,
            "input": [
                {
                    "role": "developer",
                    "content": [{"type": "input_text", "text": _INSTRUCTIONS}],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": json.dumps(
                                {
                                    "focus_region": focus_region,
                                    "focus_selection": (
                                        focus_selection.model_dump(mode="json")
                                        if focus_selection is not None
                                        else None
                                    ),
                                    "metrics": metrics,
                                },
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        }
                    ],
                },
            ],
            "max_output_tokens": self.max_output_tokens,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "tourism_policy_insight",
                    "strict": True,
                    "schema": ModelInsight.model_json_schema(),
                },
                "verbosity": "low",
            },
        }
        try:
            response = self._client.post(
                self.endpoint,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
            if body.get("status") != "completed":
                raise OpenAIInsightError("openai_response_incomplete")
            texts = [
                content.get("text")
                for output in body.get("output", [])
                if isinstance(output, dict) and output.get("type") == "message"
                for content in output.get("content", [])
                if isinstance(content, dict) and content.get("type") == "output_text"
            ]
            if len(texts) != 1 or not isinstance(texts[0], str):
                raise OpenAIInsightError("openai_output_missing")
            return ModelInsight.model_validate_json(texts[0])
        except OpenAIInsightError:
            raise
        except (httpx.HTTPError, json.JSONDecodeError, ValueError, TypeError) as error:
            raise OpenAIInsightError("openai_request_failed") from error
