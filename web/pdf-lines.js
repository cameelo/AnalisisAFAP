/**
 * Extracción de las líneas de texto de un estado de cuenta con pdf.js, en el
 * mismo formato canónico que produce el CLI a partir de PyMuPDF.
 *
 * `afap_core.py` parsea el documento como una secuencia de líneas y depende de
 * cómo estén cortadas: cada celda de la tabla tiene que ser su propia línea
 * ('45,150074', '6.348,44', '286.632,51' y '1.744,25' por separado), pero las
 * palabras de una misma frase tienen que quedar juntas.
 *
 * Los dos extractores llegan ahí por caminos distintos:
 *
 *   - PyMuPDF une celdas lejanas en una sola línea separándolas con dos o más
 *     espacios; `afap_core.canonical_lines` corta por ahí.
 *   - pdf.js ya entrega una celda por item, pero llega a partir una palabra en
 *     varios items cuando lleva acentos ("JOAQUIN NU" / "Ñ" / "EZ 3105 301").
 *     Este módulo vuelve a pegar sólo los fragmentos que se tocan, que es
 *     exactamente ese caso, y deja separado todo lo que tiene aire entre medio.
 *
 * La equivalencia está verificada sobre los estados de cuenta reales con
 * `web/verify.html` (o `node tests/verify_lines.mjs`) contra `tests/lines_golden.json`.
 */

// Tolerancia vertical para considerar que dos items comparten línea base, como
// fracción de la altura del texto.
const BASELINE_TOLERANCE = 0.5;

// Separación horizontal máxima para mantener dos items en la misma línea, en
// múltiplos del ancho de un espacio. Por debajo son palabras de una misma frase;
// por encima, celdas distintas de la tabla.
//
// El valor está medido sobre los estados de cuenta reales. El hueco más ancho que
// PyMuPDF trata como parte de una frase es el de "21/07/2020 Aporte obligatorio
// sueldo" (2,9 espacios); el más angosto que separa dos celdas numéricas vecinas
// es el de los estados con subfondos (3,7 espacios). El umbral va entre esos dos,
// y el margen es chico: al tocarlo hay que volver a correr la verificación.
const LINE_BREAK_GAP_FACTOR = 3.2;

// Por debajo de esta separación los items son fragmentos de una misma palabra
// (pdf.js parte en dos al llegar a un acento) y se pegan sin espacio.
const GLUE_GAP_FACTOR = 0.35;

// Ancho de espacio estimado a partir del alto de la fuente: pdf.js no lo informa
// y depende de la tipografía, pero ~0,25 em alcanza para estos documentos.
const SPACE_WIDTH_RATIO = 0.25;

/**
 * Devuelve las líneas canónicas de un PDF.
 *
 * @param {object} pdfjsLib módulo de pdf.js ya configurado (con su worker)
 * @param {ArrayBuffer|Uint8Array} data contenido del PDF
 * @returns {Promise<string[]>} líneas listas para `afap_core`
 */
export async function extractLines(pdfjsLib, data) {
  const bytes = data instanceof ArrayBuffer ? new Uint8Array(data) : data;
  const doc = await pdfjsLib.getDocument({ data: bytes, isEvalSupported: false }).promise;
  const lines = [];
  try {
    for (let pageNum = 1; pageNum <= doc.numPages; pageNum++) {
      const page = await doc.getPage(pageNum);
      const content = await page.getTextContent();
      lines.push(...groupItemsIntoLines(content.items));
      page.cleanup();
    }
  } finally {
    await doc.destroy();
  }
  return canonicalLines(lines);
}

/**
 * Agrupa los items de `getTextContent()` en líneas de texto.
 *
 * Se recorre en el orden en que pdf.js los entrega, que es el de dibujado y
 * coincide con el de PyMuPDF. Se abre una línea nueva al cambiar de línea base, al
 * encontrar un `hasEOL`, o cuando el hueco horizontal con el item anterior es
 * demasiado grande para ser un espacio.
 *
 * Los items que sólo contienen espacios no aportan texto: pdf.js los usa para
 * representar la separación entre celdas, y su ancho ya queda reflejado en la
 * posición del item siguiente. Se saltean, pero se respeta su `hasEOL`.
 */
export function groupItemsIntoLines(items) {
  const lines = [];
  let current = null;

  const flush = () => {
    if (current !== null) lines.push(current.text);
    current = null;
  };

  for (const item of items) {
    // pdf.js intercala items de marked content sin texto
    if (typeof item.str !== 'string') continue;
    if (item.str.trim() === '') {
      if (item.hasEOL) flush();
      continue;
    }

    const [, , , scaleY, x, y] = item.transform;
    const height = Math.abs(item.height || scaleY) || 1;
    const spaceWidth = height * SPACE_WIDTH_RATIO;
    const gap = current === null ? Infinity : x - current.right;

    // Un item que arranca a la izquierda de donde terminó el anterior no continúa
    // la línea: o el PDF volvió atrás para dibujar otra cosa, o está reimprimiendo
    // el mismo texto encima para simular negrita. En los dos casos son líneas
    // distintas, que es como las devuelve PyMuPDF.
    const backwards = gap < -spaceWidth * 0.5;

    const sameLine =
      current !== null &&
      !backwards &&
      Math.abs(y - current.y) <= height * BASELINE_TOLERANCE &&
      gap <= spaceWidth * LINE_BREAK_GAP_FACTOR;

    if (sameLine) {
      current.text += (gap > spaceWidth * GLUE_GAP_FACTOR ? ' ' : '') + item.str;
      current.right = x + (item.width || 0);
    } else {
      flush();
      current = { text: item.str, y, right: x + (item.width || 0) };
    }

    if (item.hasEOL) flush();
  }

  flush();
  return lines;
}

/**
 * Misma normalización que `afap_core.canonical_lines`: corta por corridas de dos
 * o más espacios, colapsa los blancos y descarta las líneas vacías. Los dos
 * extractores tienen que aplicarla para producir la misma lista.
 */
export function canonicalLines(lines) {
  const result = [];
  for (const line of lines) {
    for (const piece of line.split(/\s{2,}/)) {
      const text = piece.trim().replace(/\s+/g, ' ');
      if (text) result.push(text);
    }
  }
  return result;
}
