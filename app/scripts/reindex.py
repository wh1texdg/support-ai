import asyncio

from app.database.session import Session
from app.services.runtime import create_runtime


async def main():
    runtime = create_runtime()
    try:
        async with Session() as session, session.begin():
            await runtime.rag.reindex(session)
        print("Index committed successfully")
    finally:
        await runtime.close()


if __name__ == "__main__":
    asyncio.run(main())
