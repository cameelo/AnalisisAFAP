/**
 * Orquestación del sitio: pdf.js extrae las líneas de cada PDF, Pyodide corre
 * `afap_core.py` sobre ellas y devuelve el HTML del informe.
 *
 * Nada de esto sale del navegador: los PDFs se leen con `File.arrayBuffer()` y
 * los únicos pedidos de red son los de las bibliotecas y el propio `afap_core.py`.
 */

import { extractLines } from './pdf-lines.js';

const PDFJS_VERSION = '4.7.76';
const PYODIDE_VERSION = '0.26.4';
const PDFJS_BASE = `https://cdn.jsdelivr.net/npm/pdfjs-dist@${PDFJS_VERSION}/build/`;
const PYODIDE_BASE = `https://cdn.jsdelivr.net/pyodide/v${PYODIDE_VERSION}/full/`;

// `afap_core.py` vive en la raíz del repositorio y lo comparten el sitio y el CLI.
// Se prueban las dos ubicaciones para que el sitio funcione tanto si GitHub Pages
// publica el repo entero como si publica sólo esta carpeta.
const CORE_URLS = ['../afap_core.py', './afap_core.py'];

const el = {
  dropzone: document.getElementById('dropzone'),
  fileInput: document.getElementById('file-input'),
  files: document.getElementById('files'),
  generate: document.getElementById('generate'),
  clear: document.getElementById('clear'),
  status: document.getElementById('status'),
  progress: document.getElementById('progress'),
  reportCard: document.getElementById('report-card'),
  report: document.getElementById('report'),
  download: document.getElementById('download'),
  openTab: document.getElementById('open-tab'),
};

/** Estado de la página: un item por archivo cargado. */
const documents = [];
let reportHtml = null;
let reportUrl = null;

// ---------------------------------------------------------------------------
// Carga de las bibliotecas
// ---------------------------------------------------------------------------

const pdfjsReady = (async () => {
  const lib = await import(/* @vite-ignore */ `${PDFJS_BASE}pdf.min.mjs`);
  lib.GlobalWorkerOptions.workerSrc = `${PDFJS_BASE}pdf.worker.min.mjs`;
  return lib;
})();

/**
 * Arranca Pyodide y le carga `afap_core.py`. Se dispara apenas abre la página
 * para que la descarga (~10 MB la primera vez) se solape con la carga de los PDFs.
 */
const pythonReady = (async () => {
  setStatus('Preparando el motor de análisis…', 10);
  const { loadPyodide } = await import(/* @vite-ignore */ `${PYODIDE_BASE}pyodide.mjs`);
  const pyodide = await loadPyodide({ indexURL: PYODIDE_BASE });

  setStatus('Cargando el análisis…', 80);
  pyodide.FS.writeFile('afap_core.py', await fetchCore(), { encoding: 'utf8' });
  // Las dos funciones puente se definen una sola vez y se reutilizan en cada
  // archivo y en cada informe. Devuelven JSON para no tener que manipular
  // proxies de Python desde JavaScript.
  pyodide.runPython(`
import json, sys
sys.path.insert(0, '')
import afap_core


def _read_statement(lines, filename):
    """Registro de un documento, o el motivo por el que no se pudo leer."""
    try:
        s = afap_core.parse_statement(list(lines), filename)
    except afap_core.StatementError as e:
        return json.dumps({'ok': False, 'error': str(e)})
    except Exception as e:
        return json.dumps({'ok': False, 'error': f'no pude interpretar el archivo ({e})'})
    return json.dumps({'ok': True, 'statement': afap_core.statement_to_json(s)})


def _build_report(payload):
    """Informe HTML a partir de los registros ya extraídos."""
    statements = [afap_core.statement_from_json(d) for d in json.loads(payload)]
    try:
        html, warnings = afap_core.build_report(statements)
    except afap_core.StatementError as e:
        return json.dumps({'ok': False, 'error': str(e)})
    return json.dumps({'ok': True, 'html': html, 'warnings': warnings})
`);

  setStatus('Listo. Cargá tus estados de cuenta.', 100);
  el.progress.hidden = true;
  return pyodide;
})().catch((err) => {
  setStatus(`No se pudo cargar el motor de análisis: ${err.message}`, 0);
  throw err;
});

async function fetchCore() {
  let lastError;
  for (const url of CORE_URLS) {
    try {
      const res = await fetch(url);
      if (res.ok) return await res.text();
      lastError = new Error(`${url} respondió ${res.status}`);
    } catch (err) {
      lastError = err;
    }
  }
  throw lastError ?? new Error('no se encontró afap_core.py');
}

/**
 * Llama a una de las funciones puente de Python y devuelve su JSON.
 * El proxy de la función se libera siempre, para no dejar objetos vivos en el
 * intérprete después de procesar decenas de archivos.
 */
function callPython(pyodide, name, ...args) {
  const fn = pyodide.globals.get(name);
  try {
    return fn(...args);
  } finally {
    fn.destroy();
  }
}

// ---------------------------------------------------------------------------
// Carga de archivos
// ---------------------------------------------------------------------------

el.dropzone.addEventListener('click', () => el.fileInput.click());
el.dropzone.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' || e.key === ' ') {
    e.preventDefault();
    el.fileInput.click();
  }
});
el.fileInput.addEventListener('change', () => {
  addFiles(el.fileInput.files);
  el.fileInput.value = '';   // permite volver a elegir el mismo archivo
});

for (const type of ['dragenter', 'dragover']) {
  el.dropzone.addEventListener(type, (e) => {
    e.preventDefault();
    el.dropzone.classList.add('dragging');
  });
}
for (const type of ['dragleave', 'drop']) {
  el.dropzone.addEventListener(type, (e) => {
    e.preventDefault();
    el.dropzone.classList.remove('dragging');
  });
}
el.dropzone.addEventListener('drop', (e) => addFiles(e.dataTransfer.files));

el.clear.addEventListener('click', () => {
  documents.length = 0;
  clearReport();
  render();
});

async function addFiles(fileList) {
  const pdfs = [...fileList].filter(
    (f) => f.type === 'application/pdf' || f.name.toLowerCase().endsWith('.pdf'));

  for (const file of pdfs) {
    if (documents.some((d) => d.name === file.name && d.size === file.size)) continue;
    const doc = { name: file.name, size: file.size, state: 'pending' };
    documents.push(doc);
    render();
    await readDocument(doc, file);
    render();
  }
}

/** Lee un PDF y extrae su registro. Un archivo que falla no afecta a los demás. */
async function readDocument(doc, file) {
  try {
    const pdfjsLib = await pdfjsReady;
    const lines = await extractLines(pdfjsLib, await file.arrayBuffer());

    const pyodide = await pythonReady;
    const parsed = JSON.parse(callPython(pyodide, '_read_statement', lines, file.name));
    if (parsed.ok) {
      doc.state = 'ok';
      doc.statement = parsed.statement;
      if (parsed.statement.unverified) doc.note = 'resumen web (formato sin validar)';
    } else {
      doc.state = 'error';
      doc.error = parsed.error;
    }
  } catch (err) {
    doc.state = 'error';
    doc.error = err.message || String(err);
  }
}

// ---------------------------------------------------------------------------
// Informe
// ---------------------------------------------------------------------------

el.generate.addEventListener('click', async () => {
  const ok = documents.filter((d) => d.state === 'ok');
  if (!ok.length) return;

  el.generate.disabled = true;
  setStatus('Generando el informe…');
  try {
    const pyodide = await pythonReady;
    const parsed = JSON.parse(callPython(
      pyodide, '_build_report', JSON.stringify(ok.map((d) => d.statement))));
    if (!parsed.ok) {
      setStatus(`No se pudo generar el informe: ${parsed.error}`);
      return;
    }
    showReport(parsed.html);
    // Los avisos ya van dentro del informe; acá sólo se resume qué se usó.
    const skipped = documents.length - ok.length;
    setStatus(`Informe generado con ${ok.length} estado(s) de cuenta.`
      + (skipped ? ` ${skipped} archivo(s) quedaron afuera.` : '')
      + (parsed.warnings.length ? ' Revisá los avisos al comienzo del informe.' : ''));
  } catch (err) {
    setStatus(`No se pudo generar el informe: ${err.message}`);
  } finally {
    el.generate.disabled = false;
  }
});

function showReport(html) {
  clearReport();
  reportHtml = html;
  reportUrl = URL.createObjectURL(new Blob([html], { type: 'text/html' }));
  el.report.src = reportUrl;
  el.reportCard.style.display = 'block';
  el.reportCard.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function clearReport() {
  if (reportUrl) URL.revokeObjectURL(reportUrl);
  reportUrl = null;
  reportHtml = null;
  el.report.removeAttribute('src');
  el.reportCard.style.display = 'none';
}

el.download.addEventListener('click', () => {
  if (!reportHtml) return;
  const a = document.createElement('a');
  a.href = reportUrl;
  a.download = 'informe_afap.html';
  a.click();
});

el.openTab.addEventListener('click', () => {
  if (reportUrl) window.open(reportUrl, '_blank', 'noopener');
});

// ---------------------------------------------------------------------------
// Interfaz
// ---------------------------------------------------------------------------

const MESES = ['Ene', 'Feb', 'Mar', 'Abr', 'May', 'Jun',
               'Jul', 'Ago', 'Sep', 'Oct', 'Nov', 'Dic'];

function periodLabel(statement) {
  const end = new Date(statement.period_end);
  return `${MESES[end.getMonth()]}${end.getFullYear()}`;
}

function render() {
  el.files.innerHTML = '';
  for (const doc of documents) {
    const li = document.createElement('li');

    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = doc.name;
    li.appendChild(name);

    if (doc.state === 'ok') {
      const detail = document.createElement('span');
      detail.className = 'detail';
      detail.textContent = periodLabel(doc.statement) + (doc.note ? ` — ${doc.note}` : '');
      li.appendChild(detail);
    } else if (doc.state === 'error') {
      const detail = document.createElement('span');
      detail.className = 'detail';
      detail.textContent = doc.error;
      li.appendChild(detail);
    }

    const badge = document.createElement('span');
    badge.className = `badge ${doc.state === 'ok' ? (doc.note ? 'warn' : 'ok')
      : doc.state === 'error' ? 'error' : 'pending'}`;
    badge.textContent = doc.state === 'ok' ? (doc.note ? '⚠ leído' : '✔ leído')
      : doc.state === 'error' ? '✖ no se pudo leer' : '… leyendo';
    li.appendChild(badge);

    el.files.appendChild(li);
  }

  const readable = documents.filter((d) => d.state === 'ok').length;
  el.generate.disabled = readable === 0;
  el.clear.hidden = documents.length === 0;
}

function setStatus(text, progress) {
  el.status.textContent = text;
  if (progress !== undefined) {
    el.progress.hidden = false;
    el.progress.value = progress;
  }
}

render();
