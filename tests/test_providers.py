import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import AsyncOpenAI

from app.services.embeddings import EmbeddingService
from app.services.llm import OpenAIProvider


@pytest.mark.parametrize("polza", [True, False])
async def test_provider_credentials_stay_with_selected_endpoint(monkeypatch, polza):
    from app.core.config import Settings
    from app.services import runtime as module

    config = Settings(
        _env_file=None, openai_api_key="openai-test", polza_ai_api_key="polza-test" if polza else ""
    )
    monkeypatch.setattr(module, "settings", lambda: config)
    runtime = module.create_runtime()
    try:
        assert runtime.client.api_key == ("polza-test" if polza else "openai-test")
        assert str(runtime.client.base_url) == (
            "https://polza.ai/api/v1/" if polza else "https://api.openai.com/v1/"
        )
    finally:
        await runtime.close()


async def test_structured_output_adapter_uses_official_sdk():
    def handle(request):
        payload = json.loads(request.content)
        assert payload["response_format"]["type"] == "json_schema"
        assert payload["response_format"]["json_schema"]["strict"] is True
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 1,
                "model": "test",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {"answer": "Доставка доступна.", "insufficient": False, "source_ids": [7]}
                            ),
                        },
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        async with AsyncOpenAI(api_key="test", http_client=http) as client:
            result = await OpenAIProvider(client).generate_answer(
                [{"role": "user", "content": "Доставка?"}], [{"id": 7, "content": "Доставка доступна"}]
            )
    assert result.source_ids == [7] and not result.insufficient


async def test_embedding_batch_order_validation_and_cache(runtime):
    client = SimpleNamespace(
        embeddings=SimpleNamespace(
            create=AsyncMock(
                return_value=SimpleNamespace(data=[SimpleNamespace(index=0, embedding=[1.0] + [0.0] * 1535)])
            )
        )
    )
    service = EmbeddingService(client, runtime.redis)
    assert (await service.create_embedding("delivery"))[0] == 1
    await service.create_embedding("delivery")
    client.embeddings.create.assert_awaited_once()
    client.embeddings.create.return_value.data[0].embedding = [float("nan")] * 1536
    with pytest.raises(ValueError):
        await service.create_embeddings(["invalid"])
