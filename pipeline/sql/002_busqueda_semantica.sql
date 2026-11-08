-- =============================================================================
-- 002 · Búsqueda semántica con pgvector
-- Bloque 2. Ver pipeline/docs/02-esquema.md
--
-- Requisitos: 001 aplicado y la extensión pgvector 0.7 o superior (halfvec).
-- Va en un archivo aparte porque pgvector puede no estar instalado en todos
-- los ambientes. Sin este archivo, la búsqueda por palabras funciona igual.
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS vector;

SET search_path = justicia, public;

-- La dimensión depende del modelo de embeddings. 1024 corresponde a modelos
-- multilingües como bge-m3 o multilingual-e5-large. Cambiar de modelo con otra
-- dimensión requiere una migración nueva.
CREATE TABLE fragmento (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sentencia_id    bigint NOT NULL REFERENCES sentencia(id) ON DELETE CASCADE,
    orden           smallint NOT NULL CHECK (orden >= 0),
    inicio          integer NOT NULL CHECK (inicio >= 0),   -- posición en texto_publico
    fin             integer NOT NULL,
    texto           text NOT NULL,
    modelo          text NOT NULL,                          -- "bge-m3"
    embedding       halfvec(1024) NOT NULL,
    creado_en       timestamptz NOT NULL DEFAULT now(),

    UNIQUE (sentencia_id, orden),
    CHECK (fin > inicio)
);

-- HNSW con distancia coseno. halfvec usa la mitad de memoria que vector.
CREATE INDEX fragmento_embedding_hnsw ON fragmento
    USING hnsw (embedding halfvec_cosine_ops);

GRANT SELECT ON fragmento TO justicia_lectura;
GRANT SELECT, INSERT, UPDATE, DELETE ON fragmento TO justicia_pipeline;
