import json

from sqlalchemy import delete, select, text

from app.core.config import settings
from app.database.models import FAQ, KnowledgeChunk, KnowledgeDocument, Product


def chunk_text(content: str, size=800, overlap=120) -> list[str]:
    if not 0 <= overlap < size:
        raise ValueError("overlap must be smaller than size")
    content = content.strip()
    chunks, start = [], 0
    while start < len(content):
        end = min(start + size, len(content))
        if end < len(content):
            boundary = content.rfind(" ", start + size // 2, end)
            if boundary > start:
                end = boundary
        chunks.append(content[start:end])
        if end == len(content):
            break
        start = max(start + 1, end - overlap)
    return chunks


async def indexing_lock(session):
    # Serialize index mutations across processes; released automatically on commit/rollback.
    if session.bind.dialect.name == "postgresql":
        await session.execute(text("SELECT pg_advisory_xact_lock(81420)"))


class RAGService:
    def __init__(self, embeddings):
        self.embeddings = embeddings

    async def index_document(self, session, document):
        chunks = chunk_text(document.title + "\n" + document.content)
        vectors = await self.embeddings.create_embeddings(chunks)
        await session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id))
        session.add_all(
            [
                KnowledgeChunk(
                    document_id=document.id,
                    content=chunk,
                    embedding=vector,
                    embedding_model=settings().embedding_model,
                )
                for chunk, vector in zip(chunks, vectors, strict=True)
            ]
        )
        await session.flush()

    async def sync_entity(self, session, row):
        if isinstance(row, Product):
            source, title = f"product:{row.id}", row.name
            content = (
                f"{row.description}\nКатегория: {row.category}\nЦена: {row.price} RUB. "
                f"Остаток: {row.stock} шт.\nХарактеристики: "
                + json.dumps(row.specifications, ensure_ascii=False)
            )
        else:
            source, title, content = f"faq:{row.id}", row.question, row.answer
        document = await session.scalar(select(KnowledgeDocument).where(KnowledgeDocument.source == source))
        if document is None:
            document = KnowledgeDocument(title=title, content=content, source=source)
            session.add(document)
        else:
            document.title, document.content = title, content
        await session.flush()
        await self.index_document(session, document)

    async def delete_entity(self, session, row):
        prefix = "product" if isinstance(row, Product) else "faq"
        await session.execute(
            delete(KnowledgeDocument).where(KnowledgeDocument.source == f"{prefix}:{row.id}")
        )

    async def reindex(self, session):
        await indexing_lock(session)
        for model in (Product, FAQ):
            for row in (await session.scalars(select(model))).all():
                await self.sync_entity(session, row)
        docs = (
            await session.scalars(
                select(KnowledgeDocument).where(
                    ~KnowledgeDocument.source.like("product:%"), ~KnowledgeDocument.source.like("faq:%")
                )
            )
        ).all()
        for document in docs:
            await self.index_document(session, document)

    async def search(self, session, query):
        vector = await self.embeddings.create_embedding(query)
        distance = KnowledgeChunk.embedding.cosine_distance(vector)
        result = await session.execute(
            select(
                KnowledgeChunk, KnowledgeDocument.title, KnowledgeDocument.source, distance.label("distance")
            )
            .join(KnowledgeDocument)
            .where(KnowledgeChunk.embedding_model == settings().embedding_model)
            .order_by(distance)
            .limit(settings().top_k)
        )
        return [
            {
                "id": chunk.id,
                "title": title,
                "source": source,
                "content": chunk.content,
                "score": round(1 - float(dist), 6),
            }
            for chunk, title, source, dist in result
        ]
