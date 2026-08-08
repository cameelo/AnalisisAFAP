# Sitio web para el análisis de estados de cuenta AFAP

## Contexto

Hoy `analisis_afap.py` es un script de línea de comandos: lee la carpeta `EstadosDeCuenta/`, extrae los datos con PyMuPDF, y escribe `informe_afap.html` con las 6 gráficas. Solo sirve en tu máquina, con Python y las dependencias instaladas. La idea es que cualquier afiliado pueda subir sus estados de cuenta y ver el mismo informe, sin instalar nada.

Dos restricciones mandan sobre el diseño:

1. **Los PDFs contienen datos personales** — nombre, dirección, cédula y número de afiliado, además de los montos. Mandarlos a un servidor de terceros es un problema de confianza que además complica el aspecto legal.
2. **No hay servidor propio.**

La arquitectura elegida resuelve las dos: **todo corre en el navegador del usuario y el sitio es 100% estático**. Los documentos nunca se suben a ningún lado, y un sitio estático se hostea gratis en GitHub Pages.

El sitio no se va a monetizar: sin publicidad no hace falta dominio propio, `ads.txt` ni banner de consentimiento.

## Arquitectura

```
PDFs (drag & drop)
      │
      ▼
  pdf.js (WASM)          ← extrae el texto, en el navegador
      │  líneas de texto por archivo
      ▼
  Pyodide (Python WASM)  ← corre afap_core.py, TU código actual sin cambios
      │  HTML del informe
      ▼
  <iframe> + botón "Descargar informe"
```

La clave: **el único punto de `analisis_afap.py` que depende de PyMuPDF son las 4 líneas de `extract_pdf_data` que abren el PDF** (`analisis_afap.py:103-108`). Todo el resto — parseo de líneas, anclas, estimación de periodos, cálculos de inflación, generación del HTML — es Python puro con `re`, `datetime` y `json`, que corre en Pyodide sin tocar una coma. Si extraemos el texto con pdf.js y le pasamos las líneas al mismo código, no hay lógica financiera duplicada en dos lenguajes.

PyMuPDF **no** puede correr en el navegador: [su documentación](https://pymupdf.readthedocs.io/en/latest/pyodide.html) dice que el build de Pyodide es experimental, hay que compilarlo a mano y `micropip.install()` no funciona. Por eso pdf.js hace la extracción de texto y Python hace el análisis.

## Fase 0 — Refactor del Python en núcleo compartido

Crear `afap_core.py` con todo lo que no depende de PyMuPDF, para que CLI y web compartan exactamente el mismo código:

| Función | Origen | Cambio |
|---|---|---|
| `parse_num`, `fmt_money`, `MESES` | `analisis_afap.py:21-34` | mover tal cual |
| `extract_balance_anchors(lines)` | `analisis_afap.py:37` | mover tal cual (ya recibe líneas) |
| **`extract_statement_from_lines(lines, filename)`** | cuerpo de `extract_pdf_data`, `analisis_afap.py:108-226` | nuevo punto de entrada: recibe líneas en vez de un path |
| **`extract_web_summary_from_lines(lines, filename)`** | `extract_jun2023` + `build_record` de `procesar_jun2023.py:36,100` | portar al mismo esquema de registro |
| **`detect_format(lines)`** | nuevo | elige entre formato oficial y resumen web (el web trae "Saldo inicial"/"Saldo final"; el oficial trae "correspondiente al subfondo") |
| `insert_empty_periods`, `estimate_empty_periods`, `build_analysis`, `_adjust_flows_by_inflation`, `compute_inflation_adjusted_contributions`, `build_fund_anchor_map`, `compute_fund_real_returns`, `generate_html` | `analisis_afap.py:229-...` | mover tal cual |
| **`build_report(statements) -> (html, warnings)`** | Parte 2 de `generate_report`, `analisis_afap.py:1143` | misma lógica, sin escribir archivos |

`analisis_afap.py` queda como CLI fino: abre los PDFs con PyMuPDF, arma las líneas, delega en `afap_core`. Conserva `--force` y el JSON intermedio. `procesar_jun2023.py` deja de ser un script aparte que parchea el JSON: su formato pasa a estar soportado dentro del pipeline.

**Red de seguridad:** un test dorado que corre los 12 PDFs de `EstadosDeCuenta/` y compara los registros contra `datos_extraidos.json`, más un diff del `informe_afap.html` generado antes y después del refactor (debe ser idéntico salvo la fecha del pie). Sin esto, cualquier regresión en el parser pasa desapercibida.

### Robustez que el sitio necesita y hoy no existe

Con uploads arbitrarios aparecen casos que tus 12 PDFs nunca produjeron:

- **Dos semestres faltantes seguidos** — `estimate_empty_periods` (`analisis_afap.py:286`) usa `filled[i-1]`, que en ese caso sigue siendo un registro vacío con `None`, y `build_analysis` explota. Hay que estimar en cadena o rechazar el hueco con un aviso.
- **PDF ilegible o que no es de AFAP** — `period_start, period_end = min(parsed, ...)` (`analisis_afap.py:114`) tira `ValueError` sobre lista vacía. Necesita un mensaje "no pude leer este archivo" por archivo, sin tumbar el resto.
- **Un solo PDF, o PDFs duplicados del mismo semestre** — decidir el comportamiento (quedarse con uno; avisar que con un solo periodo varias gráficas pierden sentido).
- **Bomba de tiempo en la detección de la UR** — el rango `1100 < ur < 2200` está hardcodeado en tres lugares (`analisis_afap.py:61,90,163`). La UR de Jun2026 es 1921 y sube ~4-5% al año: alrededor de 2029-2030 el parser deja de reconocer los estados nuevos. Para un sitio público hay que ampliar el rango o derivar la UR por posición en vez de por magnitud.

## Fase 1 — Extractor de texto en JS equivalente a PyMuPDF

`web/pdf-lines.js`: pdf.js → la misma lista de líneas que produce `page.get_text()`.

En estos PDFs cada celda de la tabla es un bloque de texto propio: PyMuPDF devuelve `'45,150074'`, `'6.348,44'`, `'286.632,51'`, `'1.744,25'` como cuatro líneas separadas, y el parser depende de esa secuencia. Por eso la regla de partida es **una línea por item de `getTextContent()`**, no agrupar por coordenada Y (que juntaría la fila entera en una sola línea y rompería todo el parseo).

**Este es el paso de riesgo del proyecto y se valida antes de escribir la UI:** una página `web/verify.html` corre los 12 PDFs y compara las líneas de pdf.js contra un volcado de las líneas de PyMuPDF (`tests/lines_golden.json`). Cualquier diferencia se resuelve acá, ajustando el extractor JS, no el parser Python.

## Fase 2 — La aplicación web

- `web/index.html` — drag & drop, lista de archivos con el periodo detectado y su estado (✔ leído / ⚠ estimado / ✖ error), botón "Generar informe", informe embebido y botón de descarga. Aviso visible y destacado de que los archivos no se suben a ningún servidor: es la única razón por la que alguien va a animarse a usarlo.
- `web/app.js` — orquesta pdf.js → Pyodide → HTML. Precarga Pyodide en segundo plano apenas abre la página, con barra de progreso: son ~10 MB la primera vez, cacheados después.
- `web/afap_core.py` — el mismo archivo de la Fase 0, servido como asset estático.
- Las 6 gráficas y el CSS salen tal cual de `generate_html`, sin rehacer nada.
- Una sección breve "cómo funciona" y "cómo se calculan los ajustes por inflación" en la misma página, más el aviso legal (no es asesoramiento financiero, sin relación con República AFAP).

## Fase 3 — Publicación en GitHub Pages

Sin monetización, GitHub Pages es la opción más simple: ya tenés la cuenta, es gratis, no requiere dominio ni tarjeta, y publica directo desde el repo.

- Repo público con el sitio en `/web` (o en la raíz), Pages sirviendo desde la rama `main`.
- Queda en `usuario.github.io/repo`. Un dominio propio es opcional y se puede agregar después sin rehacer nada.
- Si más adelante querés repo privado o despliegues más rápidos, Cloudflare Pages es el reemplazo directo, también gratis y conectado al mismo repo.

## Pasos que tenés que completar vos

1. **Elegir el nombre** del repo (es parte de la URL pública).
2. **Crear el repo en GitHub** y activar Pages (Settings → Pages → Source: rama `main`).
3. **Decidir si el repo es público.** Recomiendo que sí: es la forma de que alguien confíe lo suficiente como para subir sus documentos — puede verificar que no salen del navegador.
4. **Probar con PDFs que no sean los tuyos.** Es el mayor riesgo abierto: pedir a 2-3 conocidos estados de otros subfondos, otras edades y otros años. Sin eso no sabemos qué formatos existen ahí afuera.
5. **Aprobar el texto legal** (no es asesoramiento financiero, sin relación con República AFAP).
6. **Revisar que ningún dato tuyo quede en el repo** antes de hacerlo público: hoy `datos_extraidos.json`, `informe_afap.html`, los PDFs de `EstadosDeCuenta/` y `Jun2023.pdf` contienen tu información personal y financiera. Van a `.gitignore`, y los PDFs de prueba se quedan fuera del repo.

## Verificación

- **Test dorado del refactor:** los 12 PDFs producen registros idénticos a `datos_extraidos.json`, y el `informe_afap.html` del CLI no cambia (salvo la fecha del pie).
- **Paridad de extracción:** `web/verify.html` confirma que las líneas de pdf.js coinciden con las de PyMuPDF en los 12 PDFs.
- **End-to-end:** subir los 12 PDFs en el navegador y comparar el informe resultante contra el que genera el CLI — mismos números en las tarjetas y en la tabla del punto 7.
- **Casos borde:** un solo PDF, PDFs salteados, un PDF duplicado, un PDF cualquiera que no sea de AFAP, y el resumen web de `Jun2023.pdf`.
- **Navegadores:** Chrome de escritorio y Safari en iPhone (los 10 MB de Pyodide en datos móviles son parte de la prueba).

## Riesgos y plan B

- **Si pdf.js no reproduce las líneas de forma confiable:** normalizar en JS a un formato canónico propio y adaptar el parser Python a esa entrada, revalidando contra el JSON dorado. Es más trabajo pero no cambia la arquitectura.
- **Si Pyodide resulta demasiado pesado en móvil:** portar solo el parser a JS y dejar Python para el CLI, asumiendo el costo de mantener dos implementaciones.
- **Si aparecen formatos de estado de cuenta desconocidos:** el sitio debe degradar bien — informar qué archivo no pudo leer y generar el informe con el resto.
