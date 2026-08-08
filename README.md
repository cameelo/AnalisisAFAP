# Análisis de estados de cuenta AFAP

Herramienta para leer los estados de cuenta semestrales de República AFAP y ver, en un
informe con gráficas, cuánto se aportó, cuánto se fue en comisiones y seguros, y si el
ahorro le ganó a la inflación.

Hay dos formas de usarla, y **las dos corren exactamente el mismo código de análisis**
(`afap_core.py`):

| | Dónde corre | Para qué |
|---|---|---|
| **Sitio web** (`web/`) | En el navegador de quien lo usa | Que cualquier afiliado pueda hacerlo sin instalar nada |
| **CLI** (`analisis_afap.py`) | En tu máquina, con Python | Desarrollo y verificación |

## Los documentos no salen del navegador

El sitio es 100% estático. Los PDFs se leen con [pdf.js](https://mozilla.github.io/pdf.js/)
y se analizan con `afap_core.py` corriendo en [Pyodide](https://pyodide.org/) (Python
compilado a WebAssembly). No hay servidor que reciba nada: una vez cargada la página se
puede cortar internet y el informe se genera igual.

Es la razón por la que el repositorio es público: los estados de cuenta traen nombre,
dirección, cédula y número de afiliado, y la única forma de que alguien confíe lo
suficiente como para cargarlos es que pueda revisar el código.

## Cómo está organizado

```
afap_core.py          Todo el análisis. Sin dependencias más allá de la biblioteca estándar.
analisis_afap.py      CLI: abre los PDFs con PyMuPDF y delega en afap_core.
web/index.html        La página: drag & drop, lista de archivos, informe embebido.
web/app.js            Orquesta pdf.js -> Pyodide -> HTML.
web/pdf-lines.js      Extractor de texto con pdf.js, equivalente al de PyMuPDF.
web/verify.html       Verificación de paridad entre los dos extractores.
tests/                Pruebas y archivos golden (ver abajo).
```

La única parte que existe dos veces es la extracción del texto del PDF: PyMuPDF en el
CLI, pdf.js en el navegador. PyMuPDF [no puede correr en
Pyodide](https://pymupdf.readthedocs.io/en/latest/pyodide.html) de forma soportada, así
que no hay alternativa. Todo lo demás — parseo, estimaciones, cálculos de inflación,
generación del HTML — es un solo archivo compartido.

Los dos extractores producen líneas cortadas en lugares levemente distintos, así que
ambos normalizan al mismo **formato canónico** (`afap_core.canonical_lines` /
`canonicalLines` en `pdf-lines.js`): una celda de tabla por línea. Es lo que hace que el
parser no dependa de con qué se leyó el PDF.

## Uso

### Sitio web

Necesita servirse por HTTP (los módulos de JavaScript no funcionan con `file://`):

```bash
python -m http.server 8000
# abrir http://127.0.0.1:8000/web/
```

### CLI

```bash
pip install pymupdf
# poner los PDFs en EstadosDeCuenta/
python analisis_afap.py            # reutiliza datos_extraidos.json si existe
python analisis_afap.py --force    # re-extrae desde los PDFs
```

Genera `informe_afap.html`.

## Pruebas

```bash
python tests/test_golden.py     # registros extraídos + casos borde
npm install && npm run verify-lines   # paridad pdf.js vs PyMuPDF
```

`tests/test_golden.py` compara los registros de los PDFs contra
`tests/statements_golden.json`, congelado antes del refactor, y cubre los casos que el
sitio recibe y los PDFs propios nunca produjeron: dos semestres faltantes seguidos, un
documento que no es de AFAP, un solo estado y estados duplicados del mismo semestre.

`tests/verify_lines.mjs` comprueba que pdf.js y PyMuPDF entreguen el mismo contenido, y
deja las líneas de pdf.js en `tests/pdfjs_lines.json`; con ese archivo presente,
`test_golden.py` verifica además que **los registros extraídos por los dos caminos sean
idénticos**, que es la garantía de que el informe del navegador coincide con el del CLI.

Los archivos golden se generan desde los PDFs propios y por eso están en `.gitignore`:
contienen datos personales. Para regenerarlos: `python tests/test_golden.py --update`.

## Publicación en GitHub Pages

Settings → Pages → Source: rama `main`, carpeta `/` (la raíz). El sitio queda en
`usuario.github.io/repo/web/`.

Tiene que publicarse desde la raíz, no desde `/web`: la página carga `../afap_core.py`
para no tener una copia del análisis. (Si preferís publicar sólo `web/`, copiá
`afap_core.py` dentro de esa carpeta — `app.js` prueba las dos ubicaciones.)

Antes de hacer público el repositorio, confirmá que no quedó ningún dato personal:

```bash
git ls-files | grep -Ei 'pdf|datos_extraidos|informe_afap|golden'   # no debe devolver nada
```

## Limitaciones conocidas

- **Sólo se probó con estados de cuenta de una persona.** No sabemos qué otros formatos
  hay: otros subfondos, otras edades, otros años. Es el mayor riesgo abierto.
- **El formato "resumen web"** (el que se descarga del autoservicio, con "Saldo inicial" y
  "Saldo final") está implementado pero **no validado contra un documento real**: se
  escribió a partir de la descripción del formato. Los registros que produce se marcan
  como `unverified` y el informe lo avisa.
- **Un semestre faltante se estima.** Con dos o más seguidos la estimación es más gruesa;
  el informe lo aclara y marca los periodos estimados con un asterisco.

## Aviso

Este proyecto no tiene relación con República AFAP ni con ninguna otra AFAP, y no es
asesoramiento financiero. Ante cualquier diferencia, vale lo que diga tu AFAP.
