"""Exercise the running Compose stack without real Telegram/OpenAI credentials."""

import asyncio
import os
import uuid

import httpx

from app.core.config import settings


async def main():
    config = settings()
    async with httpx.AsyncClient(
        base_url=os.getenv("SMOKE_URL", "http://localhost:8000"), timeout=30
    ) as client:
        health = await client.get("/health/ready")
        health.raise_for_status()
        bot = {"Authorization": f"Bearer {config.bot_api_key.get_secret_value()}"}
        admin = {"Authorization": f"Bearer {config.admin_api_key.get_secret_value()}"}
        operator = {"Authorization": f"Bearer {next(iter(config.operator_keys))}"}
        response = await client.get("/api/products", headers=admin)
        response.raise_for_status()
        assert len(response.json()) >= 20
        response = await client.post(
            "/api/chat",
            headers=bot,
            json={"telegram_id": 999999999, "event_id": f"smoke:{uuid.uuid4()}", "text": "/operator"},
        )
        response.raise_for_status()
        conversation = response.json()["conversation_id"]
        response = await client.get("/api/support/requests", headers=operator)
        response.raise_for_status()
        request_id = next(row["id"] for row in response.json() if row["conversation_id"] == conversation)
        for action, body in [("take", None), ("message", {"text": "Smoke test reply"}), ("close", None)]:
            response = await client.post(f"/api/support/{request_id}/{action}", headers=operator, json=body)
            response.raise_for_status()
        response = await client.get(f"/api/conversations/{conversation}", headers=operator)
        response.raise_for_status()
        assert response.json()["status"] == "closed"
        response = await client.get("/api/support/delivery", headers=operator)
        response.raise_for_status()
        assert any(row["content"] == "Smoke test reply" for row in response.json())
    print("Compose smoke passed: readiness, seeded catalog, handoff, operator reply, outbox, closure")


if __name__ == "__main__":
    asyncio.run(main())
