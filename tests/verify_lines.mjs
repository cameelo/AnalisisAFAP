/**
 * Paridad de extracción: lo que produce `web/pdf-lines.js` con pdf.js tiene que
 * ser equivalente a lo que produce PyMuPDF, congelado en `tests/lines_golden.json`.
 *
 * Es la misma verificación que hace `web/verify.html` en el navegador, pero desde
 * Node, para poder correrla sin abrir una página.
 *
 *   npm install
 *   node tests/verify_lines.mjs [ruta/a/node_modules/pdfjs-dist]
 *
 * Se comprueban dos cosas:
 *
 *   1. Que no se pierda ni aparezca contenido: la concatenación de todas las
 *      líneas tiene que ser idéntica. Esta es la condición dura.
 *   2. Cuántas líneas quedan cortadas distinto. Los dos extractores no coinciden
 *      al 100% — pdf.js entrega la frase "Saldo ... al ... correspondiente ..." de
 *      una pieza donde PyMuPDF la parte en tres, y agrupa distinto la dirección
 *      postal —, así que las diferencias se listan para revisarlas, pero no son
 *      un fallo por sí solas: `afap_core._balance_phrases` lee esa frase en
 *      cualquiera de las dos formas.
 *
 * La prueba decisiva es la de `tests/test_golden.py`, que compara los registros
 * extraídos. Este script deja las líneas de pdf.js en `tests/pdfjs_lines.json`
 * para que esa comparación pueda correr sin un navegador.
 */

import { readFile, writeFile } from 'node:fs/promises';
import { readdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

import { extractLines, canonicalLines } from '../web/pdf-lines.js';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..');
const STATEMENTS = path.join(ROOT, 'EstadosDeCuenta');
const GOLDEN = path.join(HERE, 'lines_golden.json');
const OUTPUT = path.join(HERE, 'pdfjs_lines.json');

const pdfjsRoot = process.argv[2] || path.join(ROOT, 'node_modules', 'pdfjs-dist');
const pdfjsLib = await import(
  pathToFileURL(path.join(pdfjsRoot, 'legacy', 'build', 'pdf.mjs')).href
);
pdfjsLib.GlobalWorkerOptions.workerSrc =
  pathToFileURL(path.join(pdfjsRoot, 'legacy', 'build', 'pdf.worker.mjs')).href;

const golden = JSON.parse(await readFile(GOLDEN, 'utf8'));
const pdfs = readdirSync(STATEMENTS).filter((f) => f.toLowerCase().endsWith('.pdf')).sort();

const extracted = {};
let failed = 0;
let regrouped = 0;

for (const name of pdfs) {
  if (!golden[name]) {
    console.log(`OMITIDO ${name}: no está en el golden`);
    continue;
  }
  // El golden guarda la salida cruda de PyMuPDF; se compara en formato canónico,
  // que es lo que efectivamente consume el parser.
  const expected = canonicalLines(golden[name]);

  // pdf.js rechaza un Buffer de Node aunque herede de Uint8Array
  const buf = await readFile(path.join(STATEMENTS, name));
  const actual = await extractLines(pdfjsLib, new Uint8Array(buf));
  extracted[name] = actual;

  if (actual.join(' ') !== expected.join(' ')) {
    failed++;
    console.log(`FALLA   ${name}: el contenido no coincide`);
    const max = Math.max(actual.length, expected.length);
    for (let i = 0, shown = 0; i < max && shown < 8; i++) {
      if (actual[i] !== expected[i]) {
        console.log(`          línea ${i}: pdf.js ${JSON.stringify(actual[i])} / PyMuPDF ${JSON.stringify(expected[i])}`);
        shown++;
      }
    }
  } else if (actual.length !== expected.length) {
    regrouped++;
    console.log(`OK      ${name}: mismo contenido, ${Math.abs(actual.length - expected.length)} línea(s) agrupadas distinto`);
  } else {
    console.log(`OK      ${name}: ${actual.length} líneas idénticas`);
  }
}

await writeFile(OUTPUT, JSON.stringify(extracted, null, 1), 'utf8');

console.log(`\n${pdfs.length - failed}/${pdfs.length} archivos con el mismo contenido` +
  (regrouped ? ` (${regrouped} con líneas agrupadas distinto)` : ''));
console.log(`Líneas de pdf.js guardadas en ${path.relative(ROOT, OUTPUT)};` +
  ` corré \`python tests/test_golden.py\` para comparar los registros.`);
process.exit(failed ? 1 : 0);
