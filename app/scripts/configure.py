"""Create local configuration with independent random API tokens, without overwriting it."""

import secrets
from pathlib import Path


def main():
    target = Path(".env")
    if target.exists():
        print(".env already exists; left unchanged")
        return
    content = Path(".env.example").read_text(encoding="utf-8")
    for placeholder in (
        "replace-with-random-admin-token-32-chars",
        "replace-with-random-bot-token-32-chars",
        "replace-with-random-operator-token-32-chars",
    ):
        content = content.replace(placeholder, secrets.token_urlsafe(32))
    password = secrets.token_urlsafe(24)
    content = content.replace("POSTGRES_PASSWORD=support", f"POSTGRES_PASSWORD={password}")
    content = content.replace("support:support@db", f"support:{password}@db")
    with target.open("x", encoding="utf-8") as stream:
        stream.write(content)
    print("Created .env with random local credentials. Set BOT_TOKEN and OPENAI_API_KEY before live use.")


if __name__ == "__main__":
    main()
