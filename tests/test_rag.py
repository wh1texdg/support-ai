from unittest.mock import AsyncMock

import pytest
from redis.exceptions import ConnectionError

from app.services.llm import Answer
from app.services.rag import chunk_text
from tests.test_api import send

HIT = {"id": 1, "title": "Доставка", "source": "demo:delivery", "content": "Доставка в Москву", "score": 0.8}


def test_chunking_covers_document_with_overlap():
    value = "0123456789" * 500
    chunks = chunk_text(value)
    assert all(len(chunk) <= 800 for chunk in chunks)
    reconstructed = chunks[0] + "".join(chunk[120:] for chunk in chunks[1:])
    assert reconstructed == value
    assert chunk_text("  ") == []
    with pytest.raises(ValueError):
        chunk_text("text", overlap=800)


async def test_grounded_answer_and_context_window(client, runtime):
    runtime.rag.search.return_value = [HIT]
    for number in range(7):
        response = await send(client, "MacBook Air" if number == 5 else "Сколько он стоит?", str(number))
        assert response.json()["sources"][0]["title"] == "Доставка"
    messages, context = runtime.llm.generate_answer.call_args.args
    assert len(messages) == 11
    assert "MacBook Air" in runtime.rag.search.call_args.args[1]


@pytest.mark.parametrize(
    "result",
    [
        Answer(answer="Unknown", insufficient=True, source_ids=[1]),
        Answer(answer="Fake", insufficient=False, source_ids=[999]),
        Answer(answer="No source", insufficient=False, source_ids=[]),
    ],
)
async def test_unsupported_answer_is_rejected(client, runtime, result):
    runtime.rag.search.return_value = [HIT]
    runtime.llm.generate_answer.return_value = result
    response = (await send(client)).json()
    assert response["reason"] == "insufficient_evidence"
    assert response["offer_operator"]


async def test_provider_timeout_fallback(client, runtime):
    runtime.rag.search.return_value = [HIT]
    runtime.llm.generate_answer.side_effect = TimeoutError()
    assert (await send(client)).json()["reason"] == "provider_unavailable"


async def test_redis_outage_still_allows_operator(client, runtime):
    runtime.redis.eval = AsyncMock(side_effect=ConnectionError())
    assert (await send(client)).status_code == 503
    assert (await send(client, "/operator", "2")).json()["status"] == "waiting_operator"
