-- =============================================================================
-- 001 · Esquema inicial del pipeline de sentencias
-- Bloque 2. Ver pipeline/docs/02-esquema.md
--
-- Requisitos: PostgreSQL 16 o superior, extensiones unaccent y pg_trgm.
-- Se aplica una sola vez, dentro de una transacción:
--   psql -v ON_ERROR_STOP=1 -1 -f 001_esquema_inicial.sql
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE SCHEMA justicia;   -- datos que puede leer la API
CREATE SCHEMA privado;    -- texto original sin enmascarar (acceso restringido)

SET search_path = justicia, public;

-- -----------------------------------------------------------------------------
-- Búsqueda en español sin acentos: "sentencia" encuentra "SENTENCIA" y
-- "condenación" encuentra "condenacion".
-- -----------------------------------------------------------------------------
CREATE TEXT SEARCH CONFIGURATION justicia.es_unaccent (COPY = pg_catalog.spanish);
ALTER TEXT SEARCH CONFIGURATION justicia.es_unaccent
    ALTER MAPPING FOR hword, hword_part, word
    WITH public.unaccent, pg_catalog.spanish_stem;

-- -----------------------------------------------------------------------------
-- Tipos enumerados
-- -----------------------------------------------------------------------------

-- Ciclo de vida de un documento (diagrama de estados del Bloque 1).
CREATE TYPE estado_documento AS ENUM (
    'descubierta',   -- registrado desde el listado; falta descargar
    'descargada',    -- archivo guardado en MinIO/S3; falta procesar
    'fallida',       -- reintentos agotados; espera reintento programado
    'duplicada',     -- mismo archivo (nivel 2) o mismo texto (nivel 3)
    'extraida',      -- texto obtenido; faltan metadatos o índice
    'revision',      -- la validación de metadatos falló
    'indexada'       -- terminado
);

CREATE TYPE etapa_pipeline AS ENUM ('descubrir', 'descargar', 'procesar');

CREATE TYPE estado_ejecucion AS ENUM ('en_curso', 'exitosa', 'fallida', 'cancelada');

-- -----------------------------------------------------------------------------
-- Función común: mantiene actualizado_en
-- -----------------------------------------------------------------------------
CREATE FUNCTION tocar_actualizado_en() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.actualizado_en := now();
    RETURN NEW;
END;
$$;

-- array_to_string() está marcada STABLE porque acepta cualquier tipo de arreglo.
-- Para text[] el resultado no depende de la configuración, así que este
-- envoltorio IMMUTABLE es seguro y permite usarlo en columnas generadas.
CREATE FUNCTION unir_texto(text[]) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT array_to_string($1, ' ');
$$;

-- =============================================================================
-- 1. fuente: un registro por adaptador (portal + materia)
-- =============================================================================
CREATE TABLE fuente (
    id                      text PRIMARY KEY
                            CHECK (id ~ '^[a-z]{2,4}-[a-z0-9-]+$'),   -- "nl-penal"
    clave_agee              smallint NOT NULL CHECK (clave_agee BETWEEN 1 AND 32),
    entidad                 text NOT NULL,                             -- "Nuevo León"
    materia                 text NOT NULL,                             -- "penal"
    url_base                text NOT NULL,
    activa                  boolean NOT NULL DEFAULT true,

    -- Carga incremental (Bloque 1, sección 4.4)
    marca_de_agua           date,                 -- fecha de publicación más reciente procesada
    ventana_dias            smallint NOT NULL DEFAULT 7 CHECK (ventana_dias >= 0),

    -- Cosecha responsable (Bloque 1, sección 4.6)
    intervalo_min_s         numeric(5,2) NOT NULL DEFAULT 2 CHECK (intervalo_min_s >= 1),
    intervalo_max_s         numeric(5,2) NOT NULL DEFAULT 5,
    circuito_abierto_hasta  timestamptz,          -- no cosechar antes de esta hora

    creado_en               timestamptz NOT NULL DEFAULT now(),
    actualizado_en          timestamptz NOT NULL DEFAULT now(),

    CHECK (intervalo_max_s >= intervalo_min_s)
);

CREATE TRIGGER fuente_actualizado_en BEFORE UPDATE ON fuente
    FOR EACH ROW EXECUTE FUNCTION tocar_actualizado_en();

COMMENT ON COLUMN fuente.marca_de_agua IS
    'Cada ejecución consulta desde marca_de_agua - ventana_dias.';

-- =============================================================================
-- 2. ejecucion: una corrida de una etapa para una fuente
-- =============================================================================
CREATE TABLE ejecucion (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),   -- run_id de los logs
    fuente_id       text NOT NULL REFERENCES fuente(id),
    etapa           etapa_pipeline NOT NULL,
    estado          estado_ejecucion NOT NULL DEFAULT 'en_curso',
    iniciada_en     timestamptz NOT NULL DEFAULT now(),
    terminada_en    timestamptz,
    contadores      jsonb NOT NULL DEFAULT '{}'::jsonb,   -- {"nuevos": 12, "duplicados": 3, ...}
    error           text,

    CHECK (terminada_en IS NULL OR terminada_en >= iniciada_en),
    CHECK ((estado = 'en_curso') = (terminada_en IS NULL))
);

CREATE INDEX ejecucion_fuente_fecha_idx ON ejecucion (fuente_id, iniciada_en DESC);

-- =============================================================================
-- 3. solicitud_http: auditoría de cada solicitud a un portal
-- =============================================================================
CREATE TABLE solicitud_http (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ejecucion_id        uuid REFERENCES ejecucion(id),
    fuente_id           text NOT NULL REFERENCES fuente(id),
    metodo              text NOT NULL DEFAULT 'GET',
    url                 text NOT NULL,
    permitida_robots    boolean NOT NULL,       -- false: no se envió la solicitud
    estado_http         smallint,               -- NULL si no hubo respuesta
    bytes               bigint,
    sha256              bytea CHECK (sha256 IS NULL OR length(sha256) = 32),
    duracion_ms         integer,
    error               text,
    solicitada_en       timestamptz NOT NULL DEFAULT now()
);

-- BRIN: índice muy pequeño para una tabla que solo crece en orden de tiempo.
CREATE INDEX solicitud_http_fecha_brin ON solicitud_http USING brin (solicitada_en);
CREATE INDEX solicitud_http_ejecucion_idx ON solicitud_http (ejecucion_id);

-- =============================================================================
-- 4. archivo: un registro por contenido único (deduplicación nivel 2)
-- =============================================================================
CREATE TABLE archivo (
    sha256          bytea PRIMARY KEY CHECK (length(sha256) = 32),
    bytes           bigint NOT NULL CHECK (bytes >= 0),
    tipo_mime       text NOT NULL,                    -- "application/pdf"
    clave_objeto    text NOT NULL UNIQUE,             -- "raw/ab/abcdef….pdf"
    creado_en       timestamptz NOT NULL DEFAULT now()
);

-- =============================================================================
-- 5. sentencia: un registro por texto único (deduplicación nivel 3)
-- =============================================================================
CREATE TABLE sentencia (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    fuente_id           text NOT NULL REFERENCES fuente(id),
    archivo_sha256      bytea NOT NULL REFERENCES archivo(sha256),
    texto_sha256        bytea NOT NULL UNIQUE CHECK (length(texto_sha256) = 32),

    -- Metadatos jurídicos
    expediente          text,
    organo              text,                 -- juzgado o sala
    juez                text,
    materia             text NOT NULL,
    fecha_emision       date,
    fecha_publicacion   date,
    sentido_fallo       text,                 -- "CONDENATORIO", "ABSOLUTORIO", …
    delitos             text[] NOT NULL DEFAULT '{}',
    descripcion         text,
    metadatos_extra     jsonb NOT NULL DEFAULT '{}'::jsonb,   -- campos propios de cada estado

    -- Texto enmascarado (el original vive en privado.texto_original)
    texto_publico       text NOT NULL,
    num_paginas         integer CHECK (num_paginas > 0),
    uso_ocr             boolean NOT NULL DEFAULT false,

    -- Reproducibilidad: permite reprocesar solo lo que cambió
    extractor_version   text NOT NULL,
    metadatos_version   text NOT NULL,

    busqueda            tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('justicia.es_unaccent',
                  coalesce(expediente, '') || ' ' || unir_texto(delitos)), 'A') ||
        setweight(to_tsvector('justicia.es_unaccent',
                  coalesce(sentido_fallo, '') || ' ' || coalesce(descripcion, '')), 'B') ||
        setweight(to_tsvector('justicia.es_unaccent', texto_publico), 'D')
    ) STORED,

    creado_en           timestamptz NOT NULL DEFAULT now(),
    actualizado_en      timestamptz NOT NULL DEFAULT now(),

    CHECK (fecha_publicacion IS NULL OR fecha_emision IS NULL
           OR fecha_publicacion >= fecha_emision)
);

CREATE TRIGGER sentencia_actualizado_en BEFORE UPDATE ON sentencia
    FOR EACH ROW EXECUTE FUNCTION tocar_actualizado_en();

CREATE INDEX sentencia_busqueda_gin   ON sentencia USING gin (busqueda);
CREATE INDEX sentencia_fuente_fecha   ON sentencia (fuente_id, fecha_emision DESC);
CREATE INDEX sentencia_delitos_gin    ON sentencia USING gin (delitos);
CREATE INDEX sentencia_juez_trgm      ON sentencia USING gin (juez gin_trgm_ops);
CREATE INDEX sentencia_extractor_idx  ON sentencia (extractor_version);

-- =============================================================================
-- 6. documento: cada aparición en un listado. Es también la cola de trabajo.
-- =============================================================================
CREATE TABLE documento (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    fuente_id           text NOT NULL REFERENCES fuente(id),
    source_key          text NOT NULL,           -- NL: "146198-<uuid>"
    url                 text NOT NULL,
    datos_listado       jsonb NOT NULL DEFAULT '{}'::jsonb,   -- fila del listado tal cual
    fecha_publicacion   date,                    -- del listado; alimenta la marca de agua

    estado              estado_documento NOT NULL DEFAULT 'descubierta',
    intentos            smallint NOT NULL DEFAULT 0,
    siguiente_intento   timestamptz NOT NULL DEFAULT now(),
    bloqueado_hasta     timestamptz,             -- lease del worker que lo tomó
    ultimo_error        text,

    archivo_sha256      bytea REFERENCES archivo(sha256),
    sentencia_id        bigint REFERENCES sentencia(id),

    descubierto_en      timestamptz NOT NULL DEFAULT now(),
    actualizado_en      timestamptz NOT NULL DEFAULT now(),

    -- Deduplicación nivel 1: el mismo item del portal no se registra dos veces
    UNIQUE (fuente_id, source_key),

    -- Coherencia entre estado y referencias
    CHECK (estado IN ('descubierta', 'fallida') OR archivo_sha256 IS NOT NULL),
    CHECK (estado <> 'indexada' OR sentencia_id IS NOT NULL)
);

CREATE TRIGGER documento_actualizado_en BEFORE UPDATE ON documento
    FOR EACH ROW EXECUTE FUNCTION tocar_actualizado_en();

-- Índice parcial: solo contiene trabajo pendiente, así que se mantiene pequeño.
CREATE INDEX documento_cola_idx ON documento (estado, siguiente_intento)
    WHERE estado IN ('descubierta', 'descargada', 'fallida');
CREATE INDEX documento_sentencia_idx ON documento (sentencia_id);
CREATE INDEX documento_archivo_idx   ON documento (archivo_sha256);

-- -----------------------------------------------------------------------------
-- tomar_documentos: entrega trabajo a un worker sin chocar con otros workers.
-- FOR UPDATE SKIP LOCKED salta las filas que otro worker ya está tomando.
-- El lease (bloqueado_hasta) libera el trabajo si el worker muere.
-- -----------------------------------------------------------------------------
CREATE FUNCTION tomar_documentos(
    p_estado    estado_documento,
    p_limite    integer DEFAULT 10,
    p_lease     interval DEFAULT interval '15 minutes',
    p_fuente    text DEFAULT NULL
) RETURNS SETOF documento
LANGUAGE sql AS $$
    UPDATE documento d
       SET bloqueado_hasta = now() + p_lease,
           intentos        = d.intentos + 1
     WHERE d.id IN (
        SELECT id
          FROM documento
         WHERE estado = p_estado
           AND siguiente_intento <= now()
           AND (bloqueado_hasta IS NULL OR bloqueado_hasta < now())
           AND (p_fuente IS NULL OR fuente_id = p_fuente)
         ORDER BY siguiente_intento
         LIMIT p_limite
         FOR UPDATE SKIP LOCKED
     )
    RETURNING d.*;
$$;

-- =============================================================================
-- 7. privado.texto_original: texto sin enmascarar, solo para reprocesar
-- =============================================================================
CREATE TABLE privado.texto_original (
    sentencia_id    bigint PRIMARY KEY REFERENCES justicia.sentencia(id) ON DELETE CASCADE,
    texto           text NOT NULL,
    creado_en       timestamptz NOT NULL DEFAULT now()
);

-- =============================================================================
-- 8. Roles y permisos
--    Roles de grupo sin login. Los usuarios reales se crean aparte y heredan:
--      CREATE ROLE api LOGIN PASSWORD '…' IN ROLE justicia_lectura;
-- =============================================================================
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'justicia_lectura') THEN
        CREATE ROLE justicia_lectura NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'justicia_pipeline') THEN
        CREATE ROLE justicia_pipeline NOLOGIN;
    END IF;
END;
$$;

REVOKE ALL ON SCHEMA privado FROM PUBLIC;

-- La API solo lee el esquema justicia, y nunca la auditoría HTTP.
GRANT USAGE ON SCHEMA justicia TO justicia_lectura;
GRANT SELECT ON fuente, sentencia, archivo TO justicia_lectura;

-- El pipeline escribe en ambos esquemas.
GRANT USAGE ON SCHEMA justicia, privado TO justicia_pipeline;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA justicia TO justicia_pipeline;
GRANT SELECT, INSERT, UPDATE ON privado.texto_original TO justicia_pipeline;
GRANT EXECUTE ON FUNCTION tomar_documentos(estado_documento, integer, interval, text)
    TO justicia_pipeline;

-- =============================================================================
-- 9. Datos iniciales
-- =============================================================================
INSERT INTO fuente (id, clave_agee, entidad, materia, url_base)
VALUES ('nl-penal', 19, 'Nuevo León', 'penal',
        'https://www.pjenl.gob.mx/SentenciasPublicas/');
