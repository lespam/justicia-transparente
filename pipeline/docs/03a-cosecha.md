# 03a · Cosecha: descubrir y descargar

Primera parte del **Bloque 3**. Cubre las etapas 1 y 2 del pipeline para Nuevo León.

- **3a (este documento):** gateway de cortesía, adaptador de Nuevo León, descubrimiento incremental y descarga.
- **3b:** extracción de texto, metadatos y enmascaramiento.
- **3c:** API FastAPI.

---

## 1. Cómo correrlo

```bash
cd pipeline
pip install -e '.[dev]'
psql -v ON_ERROR_STOP=1 -1 -d justicia -f sql/001_esquema_inicial.sql

export DATABASE_URL=postgresql://usuario:clave@localhost/justicia
export ALMACEN_DIR=./datos

justicia descubrir nl-penal --max-paginas 1   # etapa 1
justicia descargar nl-penal --limite 20       # etapa 2
justicia estado nl-penal                      # documentos por estado
```

Pruebas (la de base de datos necesita un usuario que pueda crear bases):

```bash
TEST_ADMIN_DSN=postgresql://postgres:clave@localhost/postgres pytest
```

---

## 2. Módulos

| Módulo | Responsabilidad |
| :--- | :--- |
| `core/http.py` | Gateway de cortesía: robots.txt, 2–5 s entre solicitudes, reintentos, *circuit breaker*, auditoría. |
| `core/robots.py` | robots.txt según RFC 9309 (combina grupos repetidos). |
| `core/almacen.py` | Guarda archivos por SHA-256. Hoy en disco; en el Bloque 4, MinIO/S3. |
| `core/db.py` | Todo el SQL de la cosecha. |
| `core/fechas.py` | Fechas en español (`18/marzo/2021`). |
| `adapters/nuevo_leon.py` | Lectura del portal de Nuevo León. |
| `flows/cosecha.py` | Etapas `descubrir` y `descargar`. Prefect las programará en el Bloque 4. |

---

## 3. Lo que se aprendió del portal real (2026-11-08)

Estos hallazgos **corrigen supuestos del Bloque 1**.

| Hallazgo | Decisión |
| :--- | :--- |
| `Penales.aspx` se movió a `Modulos/Penales.aspx`. El listado tiene **25,257** sentencias penales. | El adaptador usa la ruta nueva. |
| El uuid del archivo (`489347-<uuid>.pdf`) **cambia en cada clic**, pero el PDF es el mismo (mismo SHA-256). | La llave de nivel 1 es el **número de registro**, no el id del archivo. |
| El número de registro aparece en el listado **antes** de hacer clic. | Lo conocido se salta sin solicitudes extra. |
| Un registro puede ocupar varias filas (por ejemplo, un delito por fila). | Las filas de un registro se guardan juntas en `datos_listado.filas`. |
| Las URLs de `sentencias_with_url.csv` (2021) **ya no funcionan**. Una URL de hace una hora sí. | Se descartó la carga desde el CSV. La descarga debe correr poco después del descubrimiento. |
| `urllib.robotparser` solo lee el primer grupo `User-agent: *`. El robots.txt del portal tiene dos. | Se escribió un lector propio según RFC 9309. |

### Recorrido de una sentencia en el portal

1. `GET Modulos/Penales.aspx` → listado de 20 filas (formulario ASP.NET).
2. `POST` con el clic en la fila → la respuesta abre `PDFVisor/Visor.aspx?p=<token>`.
3. `GET Visor.aspx` → contiene `DEFAULT_URL = 'controller.aspx?file=~\files\<registro>-<uuid>.pdf'`.
4. `GET controller.aspx` → redirección (302) → PDF.

---

## 4. Carga incremental

1. `desde = marca_de_agua − 7 días`.
2. Se ignoran las filas publicadas antes de `desde`.
3. Se saltan los registros que ya están en `documento`, sin hacer clic.
4. El recorrido termina cuando **toda** una página fue emitida antes de `desde` (el listado está ordenado por fecha de emisión).
5. La marca de agua **solo avanza si la ejecución termina completa**. Una ejecución cortada por error, `--max-paginas` o `--max-nuevos` no la mueve, para no dejar huecos.

---

## 5. Resultado de la corrida real

| Ejecución | Solicitudes | Resultado |
| :--- | :--- | :--- |
| `descubrir --max-paginas 1` | 42 (robots + listado + 20 clics + 20 visores) | 20 nuevas, 2 min 48 s |
| `descargar --limite 20` | 41 (robots + 20 redirecciones + 20 PDF) | 20 descargadas, 0 errores, 1 min 9 s |
| `descubrir --max-paginas 1` (repetida) | 2 (robots + listado) | 0 nuevas, 20 conocidas, 0 clics |

Sobre los 20 PDF (571 páginas en total):

- Los 20 tienen capa de texto. No necesitan OCR.
- Los 20 son versiones públicas con datos testados (`**********`).
- El registro 489195 no trae delito ni sentido en el listado. El Bloque 3b debe obtenerlos del texto.

---

## 6. Pendientes conocidos

- **Carga histórica:** recorrer las ~1,260 páginas toma varias noches. Cada página cuesta una solicitud aunque sus filas ya sean conocidas. Se planea en el Bloque 5.
- **URLs temporales vencidas:** si una descarga se retrasa y la URL expira, hoy cuenta como fallo. Falta volver a resolverla desde el listado.
- **Reintento de `fallida`:** el estado existe, pero aún no hay tarea que lo regrese a la cola.
