#!/usr/bin/env python3
"""
Análisis de evolución de cuenta AFAP República (línea de comandos).

Lee los estados de cuenta en PDF de `EstadosDeCuenta/`, extrae el texto con
PyMuPDF y delega todo el análisis en `afap_core`, el mismo módulo que usa el
sitio web bajo Pyodide. Acá sólo vive lo que depende del disco y de PyMuPDF.
"""

import argparse
import json
from pathlib import Path

import pymupdf

import afap_core
from afap_core import (
    StatementError,
    build_report,
    fmt_money,
    insert_empty_periods,
    parse_statement,
    statement_from_json,
    statement_to_json,
)

BASE_DIR = Path(__file__).parent
STATEMENTS_DIR = BASE_DIR / 'EstadosDeCuenta'
OUTPUT_FILE = BASE_DIR / 'informe_afap.html'
DATA_FILE = BASE_DIR / 'datos_extraidos.json'  # estructura de datos intermedia persistida


def pdf_lines(filepath):
    """
    Convierte un PDF en la lista de líneas de texto que consume `afap_core`.

    Es el único punto del CLI que depende de PyMuPDF. En el navegador, el mismo
    resultado lo produce `web/pdf-lines.js` con pdf.js; la equivalencia entre
    ambos se verifica con `web/verify.html` contra `tests/lines_golden.json`.
    """
    doc = pymupdf.open(str(filepath))
    full_text = ''
    for page in doc:
        full_text += page.get_text() + '\n'
    return [l.strip() for l in full_text.split('\n') if l.strip()]


def save_statements(statements, path):
    """Persiste la estructura de datos intermedia en un archivo JSON."""
    payload = [statement_to_json(s) for s in statements]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


def load_statements(path):
    """Carga la estructura de datos intermedia desde un archivo JSON."""
    payload = json.loads(path.read_text(encoding='utf-8'))
    return [statement_from_json(d) for d in payload]


# ---------------------------------------------------------------------------
# PARTE 1 — EXTRACCIÓN
# Lee los estados de cuenta y arma la estructura de datos intermedia.
# ---------------------------------------------------------------------------

def extract_all_statements(statements_dir):
    """
    Extrae los datos de todos los estados de cuenta de `statements_dir` y devuelve
    la estructura de datos intermedia: una lista de registros (uno por estado),
    ordenada por fecha de cierre.

    Cada registro contiene los datos crudos que usamos de cada estado de cuenta:
    periodo (inicio/fin), aportes totales, retenciones (comisión de administración,
    seguro y custodia BCU) y su total, rentabilidad, saldo, valor de la UR, y las
    anclas de saldo (valor de la cuota / UR por fecha). No incluye estimaciones ni
    cálculos derivados: es la materia prima para armar el informe.

    Un PDF ilegible no interrumpe al resto: se informa y se sigue con los demás.
    """
    print("Leyendo estados de cuenta...")
    pdfs = sorted(statements_dir.glob('*.pdf'))

    if not pdfs:
        print(f"No se encontraron PDFs en {statements_dir}")
        return []

    statements = []
    for pdf in pdfs:
        print(f"  Procesando {pdf.name}...")
        try:
            data = parse_statement(pdf_lines(pdf), pdf.name)
            statements.append(data)
            print(f"    Periodo: {data['period_start'].strftime('%d/%m/%Y')} - "
                  f"{data['period_end'].strftime('%d/%m/%Y')}")
            print(f"    Aportes: {fmt_money(data['aportes'])} | "
                  f"Deducciones: {fmt_money(abs(data['deducciones']))} | "
                  f"Rentabilidad: {fmt_money(data['rentabilidad'])} | "
                  f"Saldo: {fmt_money(data['saldo'])}")
        except StatementError as e:
            print(f"    OMITIDO: {e}")
        except Exception as e:
            print(f"    ERROR: {e}")

    # Ordenar por fecha de cierre
    statements.sort(key=lambda x: x['period_end'])
    return statements


# ---------------------------------------------------------------------------
# PARTE 2 — INFORME
# Toma la estructura de datos intermedia y produce el informe HTML.
# ---------------------------------------------------------------------------

def generate_report(statements, output_file):
    """
    Escribe en `output_file` el informe que arma `afap_core.build_report` a partir
    de la estructura de datos intermedia, e imprime el resumen por consola.
    """
    print("\nGenerando informe HTML...")
    html, warnings = build_report(statements)

    output_file.write_text(html, encoding='utf-8')
    print(f"Informe generado: {output_file}")

    if warnings:
        print("\nAvisos:")
        for w in warnings:
            print(f"  - {w}")

    periods_data, _ = afap_core.estimate_empty_periods(
        insert_empty_periods([s for s in statements if not s.get('empty')]))
    analysis = afap_core.build_analysis(periods_data)
    print(f"\nResumen:")
    print(f"  Periodos analizados: {len(analysis)}")
    print(f"  Aportes totales:     {fmt_money(analysis[-1]['cum_aportes'])}")
    print(f"  Deducciones totales: {fmt_money(abs(analysis[-1]['cum_deducciones']))}")
    print(f"  Neto invertido:      {fmt_money(analysis[-1]['cum_neto'])}")
    print(f"  Rentabilidad total:  {fmt_money(analysis[-1]['cum_rentabilidad'])}")
    print(f"  Saldo actual:        {fmt_money(analysis[-1]['saldo'])}")
    print(f"  Inflación acumulada: {analysis[-1]['inflacion_acum']:.1f}%")
    return analysis


def main():
    parser = argparse.ArgumentParser(
        description="Genera el informe de la cuenta AFAP a partir de los estados de cuenta."
    )
    parser.add_argument(
        '-f', '--force', action='store_true',
        help="Fuerza la extracción desde los PDFs y regenera la estructura de datos, "
             "exista o no el archivo persistido."
    )
    args = parser.parse_args()

    # Reutilizar la estructura persistida si existe (salvo que se fuerce la extracción)
    if DATA_FILE.exists() and not args.force:
        print(f"Usando estructura de datos existente: {DATA_FILE}")
        statements = load_statements(DATA_FILE)
    else:
        if args.force and DATA_FILE.exists():
            print("Forzando re-extracción desde los PDFs...")
        # PARTE 1: extracción -> estructura de datos intermedia (con entradas vacías)
        statements = extract_all_statements(STATEMENTS_DIR)
        if not statements:
            return
        statements = insert_empty_periods(statements)
        save_statements(statements, DATA_FILE)
        print(f"Estructura de datos guardada: {DATA_FILE}")

    if not statements:
        return

    # PARTE 2: informe a partir de la estructura de datos intermedia
    generate_report(statements, OUTPUT_FILE)


if __name__ == '__main__':
    main()
