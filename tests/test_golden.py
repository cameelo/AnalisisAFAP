#!/usr/bin/env python3
"""
Red de seguridad del refactor a `afap_core`.

Corre los PDFs de `EstadosDeCuenta/` y compara los registros extraídos contra
`tests/statements_golden.json`. Ese archivo es el volcado congelado de lo que
producía el script original antes del refactor: cualquier diferencia numérica es
una regresión del parser.

Además cubre los casos borde que el sitio web va a recibir y que los 12 PDFs
propios nunca produjeron: dos semestres faltantes seguidos, un documento
ilegible, un solo estado y estados duplicados del mismo semestre.

    python tests/test_golden.py            # compara contra el golden
    python tests/test_golden.py --update   # regenera el golden (usar con criterio)
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import afap_core
from afap_core import StatementError, build_report, parse_statement, statement_to_json
from analisis_afap import STATEMENTS_DIR, pdf_lines

GOLDEN_STATEMENTS = Path(__file__).parent / 'statements_golden.json'
GOLDEN_LINES = Path(__file__).parent / 'lines_golden.json'
PDFJS_LINES = Path(__file__).parent / 'pdfjs_lines.json'   # lo escribe verify_lines.mjs

TOLERANCE = 0.005  # los importes vienen con 2 decimales

# Diferencias contra el golden que son correcciones buscadas, no regresiones.
# El golden se generó con el script original, que reconocía la UR sólo por su
# magnitud (1.100 < UR < 2.200) y por eso llegaba a tomar un saldo como si fuera
# una UR. `afap_core` la identifica por posición y por la identidad
# saldo/UR = saldo_en_UR, así que acá corrige el valor.
KNOWN_CORRECTIONS = {
    # Jun2021 abre el 31/12/2020: la UR de esa fecha es 1.291,44 (el cierre de
    # Dic2020). El original tomaba 1.990,71, que es el saldo de Dic2020 y cae
    # dentro del rango de magnitud viejo. No afecta al informe: este campo sólo
    # se usa para estimar un semestre faltante anterior, y no hay ninguno.
    'Jun2021.pdf.opening_ur': (1291.44, 1990.71),
}


def extract_all():
    """Registros de todos los PDFs, ordenados por cierre de periodo."""
    statements = []
    for pdf in sorted(STATEMENTS_DIR.glob('*.pdf')):
        statements.append(parse_statement(pdf_lines(pdf), pdf.name))
    statements.sort(key=lambda s: s['period_end'])
    return statements


def dump_lines():
    """Líneas de PyMuPDF por archivo — referencia para el extractor de pdf.js."""
    return {pdf.name: pdf_lines(pdf) for pdf in sorted(STATEMENTS_DIR.glob('*.pdf'))}


def _compare(actual, expected, path, errors):
    fix = KNOWN_CORRECTIONS.get(path)
    if fix is not None:
        corrected, original = fix
        if abs(float(actual) - corrected) <= TOLERANCE:
            return
        errors.append(f"{path}: {actual!r}; se esperaba la corrección {corrected!r} "
                      f"(el original daba {original!r})")
        return
    if isinstance(expected, dict):
        for k in set(expected) | set(actual or {}):
            if k not in (actual or {}):
                errors.append(f"{path}.{k}: falta en el resultado actual")
            elif k not in expected:
                errors.append(f"{path}.{k}: apareció un campo nuevo ({actual[k]!r})")
            else:
                _compare(actual[k], expected[k], f"{path}.{k}", errors)
    elif isinstance(expected, list):
        if len(actual) != len(expected):
            errors.append(f"{path}: {len(actual)} elementos, se esperaban {len(expected)}")
            return
        for i, (a, e) in enumerate(zip(actual, expected)):
            _compare(a, e, f"{path}[{i}]", errors)
    elif isinstance(expected, float) or isinstance(actual, float):
        if actual is None or expected is None:
            if actual != expected:
                errors.append(f"{path}: {actual!r} != {expected!r}")
        elif abs(float(actual) - float(expected)) > TOLERANCE:
            errors.append(f"{path}: {actual!r} != {expected!r}")
    elif actual != expected:
        errors.append(f"{path}: {actual!r} != {expected!r}")


def test_statements():
    """Los 12 PDFs producen exactamente los registros congelados."""
    actual = [statement_to_json(s) for s in extract_all()]
    expected = json.loads(GOLDEN_STATEMENTS.read_text(encoding='utf-8'))

    errors = []
    if len(actual) != len(expected):
        errors.append(f"se extrajeron {len(actual)} estados, se esperaban {len(expected)}")
    else:
        for a, e in zip(actual, expected):
            _compare(a, e, e['file'], errors)
    return errors


def test_pdfjs_records():
    """
    Las líneas que extrae pdf.js producen los mismos registros que las de PyMuPDF.

    Es la verificación end-to-end de la paridad entre los dos extractores: no
    alcanza con que las líneas se parezcan, tienen que llevar al mismo informe.
    Requiere haber corrido antes `node tests/verify_lines.mjs`, que deja las
    líneas de pdf.js en `tests/pdfjs_lines.json`.
    """
    if not PDFJS_LINES.exists():
        print("       (salteado: falta tests/pdfjs_lines.json — corré antes "
              "`node tests/verify_lines.mjs`)")
        return []

    js_lines = json.loads(PDFJS_LINES.read_text(encoding='utf-8'))
    expected = {s['file']: s for s in json.loads(GOLDEN_STATEMENTS.read_text(encoding='utf-8'))}

    errors = []
    for name, lines in sorted(js_lines.items()):
        if name not in expected:
            errors.append(f"{name}: no está en el golden de registros")
            continue
        try:
            actual = statement_to_json(parse_statement(lines, name))
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__}: {e}")
            continue
        _compare(actual, expected[name], name, errors)
    return errors


def _synthetic(period_end, saldo, ur, aportes=1000.0, opening_saldo=0.0, opening_ur=None):
    """Registro mínimo para probar la lógica de periodos sin depender de un PDF."""
    return {
        'file': f"s{period_end.year}{period_end.month:02d}.pdf",
        'period_start': afap_core._period_start_for(period_end),
        'period_end': period_end,
        'aportes': aportes,
        'comision_admin': -aportes * 0.04,
        'seguro': -aportes * 0.10,
        'custodia': -aportes * 0.001,
        'deducciones': -aportes * 0.141,
        'rentabilidad': 50.0,
        'saldo': saldo,
        'ur': ur,
        'opening_saldo': opening_saldo,
        'opening_ur': opening_ur if opening_ur is not None else ur,
        'anchors': [],
    }


def test_two_missing_semesters():
    """Dos semestres faltantes seguidos se estiman en cadena, sin explotar."""
    errors = []
    a = _synthetic(datetime(2020, 12, 31), 10000.0, 1300.0, opening_saldo=0.0, opening_ur=1290.0)
    b = _synthetic(datetime(2022, 12, 31), 40000.0, 1500.0,
                   opening_saldo=30000.0, opening_ur=1450.0)
    try:
        html, warnings = build_report([a, b])
    except Exception as e:
        return [f"dos semestres faltantes: {type(e).__name__}: {e}"]

    if 'Jun2021' not in html or 'Dic2021' not in html or 'Jun2022' not in html:
        errors.append("dos semestres faltantes: faltan periodos estimados en la tabla")
    if not any('Jun2021' in w and 'Dic2021' in w for w in warnings):
        errors.append("dos semestres faltantes: no se avisó del hueco encadenado")
    return errors


def test_unreadable_document():
    """Un PDF que no es de AFAP se rechaza con un mensaje, no con un ValueError."""
    lines = ['Factura N° 123', 'Total', '1.234,56', 'Gracias por su compra']
    try:
        parse_statement(lines, 'factura.pdf')
    except StatementError:
        return []
    except Exception as e:
        return [f"documento ilegible: se esperaba StatementError y llegó {type(e).__name__}: {e}"]
    return ["documento ilegible: se aceptó un documento que no es un estado de cuenta"]


def test_single_statement():
    """Un solo estado genera informe y avisa de las limitaciones."""
    one = _synthetic(datetime(2025, 6, 30), 5000.0, 1800.0, opening_saldo=3000.0, opening_ur=1750.0)
    try:
        html, warnings = build_report([one])
    except Exception as e:
        return [f"un solo estado: {type(e).__name__}: {e}"]
    if not any('un solo semestre' in w.lower() for w in warnings):
        return ["un solo estado: no se avisó de la limitación"]
    return []


def test_duplicate_semester():
    """Dos estados del mismo semestre: se usa uno y se avisa del otro."""
    a = _synthetic(datetime(2025, 6, 30), 5000.0, 1800.0)
    b = dict(a, file='copia.pdf', saldo=5000.0)
    try:
        html, warnings = build_report([a, b])
    except Exception as e:
        return [f"duplicados: {type(e).__name__}: {e}"]
    if not any('copia.pdf' in w for w in warnings):
        return ["duplicados: no se avisó que se ignoraba el segundo estado"]
    return []


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--update', action='store_true',
                        help="Regenera los archivos golden a partir de los PDFs actuales.")
    args = parser.parse_args()

    if args.update:
        statements = [statement_to_json(s) for s in extract_all()]
        GOLDEN_STATEMENTS.write_text(
            json.dumps(statements, ensure_ascii=False, indent=2), encoding='utf-8')
        GOLDEN_LINES.write_text(
            json.dumps(dump_lines(), ensure_ascii=False, indent=1), encoding='utf-8')
        print(f"Golden actualizado: {GOLDEN_STATEMENTS.name}, {GOLDEN_LINES.name}")
        return 0

    checks = [
        ('registros de los PDFs', test_statements),
        ('registros desde las líneas de pdf.js', test_pdfjs_records),
        ('dos semestres faltantes seguidos', test_two_missing_semesters),
        ('documento ilegible', test_unreadable_document),
        ('un solo estado', test_single_statement),
        ('semestre duplicado', test_duplicate_semester),
    ]

    failed = 0
    for name, check in checks:
        errors = check()
        if errors:
            failed += 1
            print(f"FALLA  {name}")
            for e in errors[:20]:
                print(f"         {e}")
            if len(errors) > 20:
                print(f"         ... y {len(errors) - 20} diferencias más")
        else:
            print(f"OK     {name}")

    print()
    print(f"{len(checks) - failed}/{len(checks)} verificaciones OK")
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
