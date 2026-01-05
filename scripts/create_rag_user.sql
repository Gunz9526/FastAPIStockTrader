-- Create READ ONLY user for RAG service
-- This ensures RAG cannot modify trading data

-- Create user
CREATE USER rag_reader WITH PASSWORD 'rag_reader_qlqjs!';

-- Grant connection
GRANT CONNECT ON DATABASE stocktrader TO rag_reader;

-- Grant usage on schema
GRANT USAGE ON SCHEMA public TO rag_reader;

-- Grant SELECT on all existing tables
GRANT SELECT ON ALL TABLES IN SCHEMA public TO rag_reader;

-- Grant SELECT on future tables (auto-grant)
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON TABLES TO rag_reader;

-- Grant USAGE on sequences (for ID columns)
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO rag_reader;

-- Verify permissions
\du rag_reader
\dp public.*
