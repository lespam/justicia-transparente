"""PDF mínimos con texto, escritos a mano, para pruebas sin archivos reales."""


def _escapar(linea: str) -> bytes:
    texto = linea.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return texto.encode("cp1252")


def crear_pdf(paginas: list[list[str]]) -> bytes:
    """Un PDF con una página por lista de líneas (Helvetica, WinAnsi)."""
    objetos: list[bytes] = []
    n_paginas = len(paginas)
    # 1: catálogo, 2: árbol de páginas, 3: fuente, luego (página, contenido) por página
    hijos = " ".join(f"{4 + 2 * i} 0 R" for i in range(n_paginas))
    objetos.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objetos.append(f"<< /Type /Pages /Kids [{hijos}] /Count {n_paginas} >>".encode())
    objetos.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
                   b"/Encoding /WinAnsiEncoding >>")
    for i, lineas in enumerate(paginas):
        flujo = b"BT /F1 10 Tf 40 800 Td 12 TL " + b" ".join(
            b"(" + _escapar(linea) + b") '" for linea in lineas) + b" ET"
        objetos.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>".encode())
        objetos.append(f"<< /Length {len(flujo)} >>\nstream\n".encode() + flujo + b"\nendstream")

    salida = bytearray(b"%PDF-1.4\n")
    posiciones = []
    for numero, cuerpo in enumerate(objetos, start=1):
        posiciones.append(len(salida))
        salida += f"{numero} 0 obj\n".encode() + cuerpo + b"\nendobj\n"
    inicio_xref = len(salida)
    salida += f"xref\n0 {len(objetos) + 1}\n0000000000 65535 f \n".encode()
    salida += b"".join(f"{p:010d} 00000 n \n".encode() for p in posiciones)
    salida += (f"trailer\n<< /Size {len(objetos) + 1} /Root 1 0 R >>\n"
               f"startxref\n{inicio_xref}\n%%EOF\n").encode()
    return bytes(salida)


def sentencia_nl(codigo: str = "CO000012345678", sentido: str = "condenó",
                 relleno: int = 30) -> bytes:
    """PDF con el encabezado real de Nuevo León y texto ficticio."""
    portada = [
        f"*{codigo}*", codigo, "SENTENCIAS", "SE DICTA SENTENCIA DE PROCEDIMIENTO ABREVIADO",
        "PODER JUDICIAL DEL", "ESTADO DE NUEVO LEÓN", "0001",
        "En San Nicolás de los Garza, Nuevo León, a **********de **********del año dos",
        "mil veintiséis.",
        f"Se procede a plasmar por escrito la sentencia definitiva mediante la cual se {sentido}",
        "a **********, dentro del procedimiento abreviado en la carpeta judicial número",
        "**********/2026. El testigo Pedro Gómez Ruiz declaró en la audiencia.",
        "Su teléfono es 81 1234 5678 y vive en calle Hidalgo 123, colonia Centro.",
        "Resolvió la Jueza Ana Laura Treviño Garza.",
    ]
    cuerpo = ["Texto de relleno para la valoración de las pruebas en el presente asunto."] * relleno
    return crear_pdf([portada, cuerpo])
