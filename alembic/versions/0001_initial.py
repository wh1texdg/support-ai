"""Initial PostgreSQL schema, frozen at revision creation."""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute(
        "CREATE TABLE faq (\n\tquestion VARCHAR(500) NOT NULL, \n\tanswer TEXT NOT NULL, \n\tcategory VARCHAR(100) NOT NULL, \n\tid SERIAL NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (question)\n)"
    )
    op.execute(
        "CREATE TABLE incoming_events (\n\tevent_id VARCHAR(128) NOT NULL, \n\tresponse JSON NOT NULL, \n\tid SERIAL NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (event_id)\n)"
    )
    op.execute(
        "CREATE TABLE knowledge_documents (\n\ttitle VARCHAR(500) NOT NULL, \n\tcontent TEXT NOT NULL, \n\tsource VARCHAR(500) NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tid SERIAL NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (source)\n)"
    )
    op.execute(
        "CREATE TABLE outbox (\n\ttelegram_id BIGINT NOT NULL, \n\tcontent TEXT NOT NULL, \n\tattempts INTEGER NOT NULL, \n\tstatus VARCHAR(20) NOT NULL, \n\tnext_attempt_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tsent_at TIMESTAMP WITH TIME ZONE, \n\tid SERIAL NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id)\n)"
    )
    op.execute("CREATE INDEX ix_outbox_status ON outbox (status)")
    op.execute(
        "CREATE TABLE products (\n\tname VARCHAR(200) NOT NULL, \n\tdescription TEXT NOT NULL, \n\tcategory VARCHAR(100) NOT NULL, \n\tprice NUMERIC(12, 2) NOT NULL, \n\tstock INTEGER NOT NULL, \n\tspecifications JSON NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tid SERIAL NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tCHECK (price >= 0), \n\tCHECK (stock >= 0), \n\tUNIQUE (name)\n)"
    )
    op.execute(
        "CREATE TABLE users (\n\ttelegram_id BIGINT NOT NULL, \n\tusername VARCHAR(128), \n\tfirst_name VARCHAR(128), \n\tid SERIAL NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (telegram_id)\n)"
    )
    op.execute(
        "CREATE TABLE conversations (\n\tuser_id INTEGER NOT NULL, \n\tstatus VARCHAR(32) NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tid SERIAL NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tCHECK (status IN ('bot','waiting_operator','operator','closed')), \n\tFOREIGN KEY(user_id) REFERENCES users (id)\n)"
    )
    op.execute("CREATE INDEX ix_conversations_user_id ON conversations (user_id)")
    op.execute(
        "CREATE UNIQUE INDEX uq_active_conversation ON conversations (user_id) WHERE status != 'closed'"
    )
    op.execute(
        "CREATE TABLE knowledge_chunks (\n\tdocument_id INTEGER NOT NULL, \n\tcontent TEXT NOT NULL, \n\tembedding VECTOR(1536) NOT NULL, \n\tembedding_model VARCHAR(100) NOT NULL, \n\tid SERIAL NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(document_id) REFERENCES knowledge_documents (id) ON DELETE CASCADE\n)"
    )
    op.execute("CREATE INDEX ix_chunks_cosine ON knowledge_chunks USING hnsw (embedding vector_cosine_ops)")
    op.execute("CREATE INDEX ix_knowledge_chunks_document_id ON knowledge_chunks (document_id)")
    op.execute(
        "CREATE TABLE messages (\n\tconversation_id INTEGER NOT NULL, \n\tsender_type VARCHAR(16) NOT NULL, \n\tcontent TEXT NOT NULL, \n\tsources JSON NOT NULL, \n\tid SERIAL NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tCHECK (sender_type IN ('user','assistant','operator','system')), \n\tFOREIGN KEY(conversation_id) REFERENCES conversations (id)\n)"
    )
    op.execute("CREATE INDEX ix_messages_conversation_id ON messages (conversation_id)")
    op.execute(
        "CREATE TABLE support_requests (\n\tconversation_id INTEGER NOT NULL, \n\treason VARCHAR(500) NOT NULL, \n\tstatus VARCHAR(20) NOT NULL, \n\tassigned_operator_id VARCHAR(100), \n\tclosed_at TIMESTAMP WITH TIME ZONE, \n\tid SERIAL NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tCHECK (status IN ('waiting','assigned','closed')), \n\tUNIQUE (conversation_id), \n\tFOREIGN KEY(conversation_id) REFERENCES conversations (id)\n)"
    )
    op.execute(
        "CREATE TABLE feedback (\n\tmessage_id INTEGER NOT NULL, \n\tuser_id INTEGER NOT NULL, \n\thelpful BOOLEAN NOT NULL, \n\tid SERIAL NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (message_id), \n\tFOREIGN KEY(message_id) REFERENCES messages (id), \n\tFOREIGN KEY(user_id) REFERENCES users (id)\n)"
    )


def downgrade():
    op.drop_table("feedback")
    op.drop_table("support_requests")
    op.drop_table("messages")
    op.drop_table("knowledge_chunks")
    op.drop_table("conversations")
    op.drop_table("users")
    op.drop_table("products")
    op.drop_table("outbox")
    op.drop_table("knowledge_documents")
    op.drop_table("incoming_events")
    op.drop_table("faq")
