#!/usr/bin/env python3
"""
Núcleo compartido del análisis de estados de cuenta AFAP República.

Este módulo contiene TODO el análisis: parseo de las líneas de texto de un estado
de cuenta, estimación de periodos faltantes, cálculos de inflación y generación
del informe HTML. No depende de PyMuPDF ni del sistema de archivos, sólo de la
biblioteca estándar (`re`, `datetime`, `json`), por lo que corre igual en CPython
(el CLI `analisis_afap.py`) y en el navegador bajo Pyodide (el sitio en `web/`).

El único paso que queda fuera es convertir un PDF en una lista de líneas de texto:
en el CLI lo hace PyMuPDF, en el navegador pdf.js (`web/pdf-lines.js`). Ambos
deben producir la misma lista de líneas; eso se verifica con `web/verify.html`.
"""

import json
import re
from datetime import datetime

# Meses en español para etiquetas
MESES = {1: 'Ene', 2: 'Feb', 3: 'Mar', 4: 'Abr', 5: 'May', 6: 'Jun',
          7: 'Jul', 8: 'Ago', 9: 'Sep', 10: 'Oct', 11: 'Nov', 12: 'Dic'}

# Reconocimiento de la Unidad Reajustable.
#
# El parser tiene que distinguir la UR de un saldo o del valor de la cuota, que
# aparecen en columnas vecinas. Un rango fijo de magnitud ("entre 1.100 y 2.200")
# es una bomba de tiempo: la UR sube ~4-5% al año (1.921,36 en Jun2026) y hacia
# 2030 dejaría de reconocer los estados nuevos. Acá se usan dos señales que no
# caducan:
#
#   1. La identidad `saldo_en_UR = saldo / UR` de la terna (ver `_ur_ratio_ok`).
#      Es necesaria pero no suficiente: la terna (saldo, cuota, cantidad de
#      cuotas) cumple exactamente la misma división.
#   2. Una ventana de plausibilidad anclada a un valor conocido de la UR y a la
#      fecha del propio documento (ver `_ur_window`), que se ensancha con los
#      años en vez de quedar fija.
#
# Cuando el documento trae anclas de saldo (la mayoría de los casos), la UR real
# se conoce de ahí y las ventanas ni se usan.
UR_REF_VALUE = 1255.72   # UR al 30/06/2020
UR_REF_YEAR = 2020.5
UR_MIN_GROWTH = 1.02     # cota inferior de crecimiento anual
UR_MAX_GROWTH = 1.12     # cota superior de crecimiento anual

_NUM_RE = re.compile(r'^-?[\d.]+,\d{2}$')
_QTY_RE = re.compile(r'^\d+,\d{6}$')  # cantidad de cuotas (6 decimales)


class StatementError(Exception):
    """El archivo no pudo interpretarse como un estado de cuenta AFAP."""


def parse_num(s):
    """Convierte formato español '1.234,56' a float."""
    return float(s.strip().replace('.', '').replace(',', '.'))


def fmt_money(val):
    """Formatea número como moneda uruguaya."""
    if val < 0:
        return f"-$ {abs(val):,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.')
    return f"$ {val:,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.')


def canonical_lines(lines):
    """
    Normaliza las líneas de un documento al formato canónico que consume el parser.

    PyMuPDF y pdf.js no cortan el texto igual: PyMuPDF une en una sola línea celdas
    de tabla que están lejos entre sí (separándolas con dos o más espacios), y
    pdf.js llega a partir una palabra en varios fragmentos cuando lleva acentos. El
    formato canónico se define como el más granular de los dos —una celda por
    línea— y se obtiene cortando por las corridas de dos o más espacios. El
    extractor de pdf.js repara antes los cortes a mitad de palabra, de modo que
    ambos caminos terminan en la misma lista.

    Es la única concesión del refactor: el parser ya no lee la salida cruda de
    PyMuPDF sino esta forma común, y por eso `extract_balance_anchors` reconstruye
    la frase "Saldo ... al ... correspondiente ..." leyendo varias líneas seguidas.
    """
    result = []
    for line in lines:
        for piece in re.split(r'\s{2,}', line):
            piece = ' '.join(piece.split())
            if piece:
                result.append(piece)
    return result


def _ur_window(date):
    """
    Rango plausible para la UR en una fecha dada, extrapolando desde `UR_REF_VALUE`
    con cotas de crecimiento anual holgadas. Se ensancha con el paso de los años,
    de modo que el parser no caduca; su único trabajo es separar la UR (≈1.900 hoy)
    del valor de la cuota (≈7.500 hoy), que crece bastante más rápido.
    """
    y = (date.year + (date.month - 0.5) / 12.0) - UR_REF_YEAR
    return (UR_REF_VALUE * 0.85 * (UR_MIN_GROWTH ** y),
            UR_REF_VALUE * 1.15 * (UR_MAX_GROWTH ** y))


def _ur_ratio_ok(saldo, ur, saldo_ur):
    """
    ¿La terna (saldo, UR, saldo_en_UR) es consistente?

    En las tablas de saldo, el importe en pesos viene seguido del valor de la UR y
    del mismo importe expresado en UR: `saldo / UR == saldo_en_UR`. Es una
    condición necesaria, no suficiente — la terna (saldo, cuota, cantidad de
    cuotas) cumple la misma división —, así que siempre se combina con la ventana
    de `_ur_window` o con las URs conocidas del documento.
    """
    if ur <= 0:
        return False
    # Las tres cifras son columnas distintas de la tabla. Sin esta condición, una
    # secuencia degenerada como (x, x, 1,00) satisface la división y se cuela como
    # si fuera un saldo con su UR.
    if abs(saldo) > 0.005 and (abs(saldo - ur) < 0.005 or abs(saldo - saldo_ur) < 0.005):
        return False
    expected = saldo / ur
    return abs(expected - saldo_ur) <= max(0.011, abs(saldo_ur) * 0.0005)


def _run_from(lines, idx, limit=6):
    """Números consecutivos en formato español a partir de `lines[idx]`."""
    seq = []
    for j in range(idx, min(idx + limit, len(lines))):
        if _NUM_RE.match(lines[j]):
            seq.append(parse_num(lines[j]))
        else:
            break
    return seq


def _balance_phrases(lines):
    """
    Localiza las frases "Saldo ... al DD/MM/AAAA correspondiente ..." y devuelve
    `(fecha, inicio, fin)` por cada una: `inicio` es el índice de la primera línea
    de la frase y `fin` el de la primera línea posterior, donde arrancan las cifras.

    En el formato canónico la frase puede venir entera en una línea o repartida en
    varias ("Saldo de la cuenta ... al" / "31/12/2021" / "correspondiente al
    subfondo de acumulación"), según cómo la haya cortado el PDF. Se acumulan
    líneas hasta toparse con la primera cifra, que ya es dato de la tabla.
    """
    phrases = []
    for i, line in enumerate(lines):
        if 'saldo' not in line.lower():
            continue
        parts = []
        j = i
        while j < len(lines) and len(parts) < 5:
            if _NUM_RE.match(lines[j]) or _QTY_RE.match(lines[j]):
                break
            parts.append(lines[j])
            j += 1
        text = ' '.join(parts)
        low = text.lower()
        if 'correspondiente' not in low:
            continue
        m = re.search(r'al\s+(\d{2}/\d{2}/\d{4})', text)
        if not m or j >= len(lines):
            continue
        try:
            date = datetime.strptime(m.group(1), '%d/%m/%Y')
        except ValueError:
            continue
        phrases.append((date, i, j))
    return phrases


def _document_dates(lines):
    """Todas las fechas DD/MM/AAAA del documento, como datetime."""
    dates = []
    for d in re.findall(r'\d{2}/\d{2}/\d{4}', '\n'.join(lines)):
        try:
            dates.append(datetime.strptime(d, '%d/%m/%Y'))
        except ValueError:
            continue
    return dates


def extract_balance_anchors(lines):
    """
    Extrae anclas de saldo (fecha, valor de la cuota, saldo, UR) de las líneas
    "Saldo ... al DD/MM/YYYY correspondiente ...".

    El valor de la cuota es el precio de la cuota del fondo, independiente de los
    flujos de esta cuenta. Se maneja el layout normal (qty, cuota, saldo, UR juntos)
    y el layout "corrido" de algunos PDFs, donde la línea de saldo solo trae
    (saldo, UR, saldo_UR) y el valor de la cuota se recupera por el saldo desde la
    tupla anclada en la cantidad de cuotas.
    """
    dates = _document_dates(lines)
    doc_window = _ur_window(max(dates)) if dates else (0.0, float('inf'))

    # Tuplas ancladas por la cantidad de cuotas: [cuota, saldo, UR, saldo_UR].
    # La cantidad de cuotas se reconoce por sus 6 decimales, así que acá la UR está
    # identificada por posición y sólo hace falta confirmarla.
    qty_tuples = []
    for i, l in enumerate(lines):
        if not _QTY_RE.match(l):
            continue
        seq = _run_from(lines, i + 1)
        if len(seq) < 3 or not (doc_window[0] < seq[2] < doc_window[1]):
            continue
        if len(seq) >= 4 and not _ur_ratio_ok(seq[1], seq[2], seq[3]):
            continue
        qty_tuples.append({'cuota': seq[0], 'saldo': seq[1], 'ur': seq[2]})

    def cuota_by_saldo(s):
        for t in qty_tuples:
            if abs(t['saldo'] - s) < 0.005:
                return t['cuota']
        return None

    anchors = []
    for date, _start, end in _balance_phrases(lines):
        lo, hi = _ur_window(date)   # la fecha del ancla acota la UR con precisión
        if _QTY_RE.match(lines[end]):          # layout normal: qty, cuota, saldo, UR, ...
            seq = _run_from(lines, end + 1)
            if len(seq) >= 3 and lo < seq[2] < hi:
                if len(seq) >= 4 and not _ur_ratio_ok(seq[1], seq[2], seq[3]):
                    continue
                anchors.append({'date': date, 'cuota': seq[0], 'saldo': seq[1], 'ur': seq[2]})
        else:                                  # layout corrido: saldo, UR, saldo_UR
            seq = _run_from(lines, end)
            if len(seq) >= 2 and lo < seq[1] < hi:
                if len(seq) >= 3 and not _ur_ratio_ok(seq[0], seq[1], seq[2]):
                    continue
                cuota = cuota_by_saldo(seq[0])
                if cuota is not None:
                    anchors.append({'date': date, 'cuota': cuota, 'saldo': seq[0], 'ur': seq[1]})
    return anchors


# ---------------------------------------------------------------------------
# DETECCIÓN DE FORMATO
# ---------------------------------------------------------------------------

def detect_format(lines):
    """
    Decide qué parser corresponde a un documento a partir de sus líneas.

    - `'official'`: el estado de cuenta semestral en PDF de República AFAP, que trae
      las líneas "Saldo ... correspondiente al subfondo ...".
    - `'web_summary'`: el resumen que se descarga del autoservicio web, con
      "Saldo inicial" / "Saldo final" en vez del detalle por subfondo.
    - `None`: no parece un estado de cuenta AFAP.
    """
    joined = '\n'.join(lines).lower()
    if 'correspondiente' in joined and 'saldo' in joined and re.search(
            r'\d{2}/\d{2}/\d{4}\s*-\s*\d{2}/\d{2}/\d{4}', joined):
        return 'official'
    if 'saldo inicial' in joined and 'saldo final' in joined:
        return 'web_summary'
    return None


def parse_statement(lines, filename):
    """
    Punto de entrada único: detecta el formato y devuelve el registro del estado.
    Lanza `StatementError` con un mensaje legible si el documento no se reconoce.
    """
    lines = canonical_lines(lines)
    fmt = detect_format(lines)
    if fmt == 'official':
        return extract_statement_from_lines(lines, filename)
    if fmt == 'web_summary':
        return extract_web_summary_from_lines(lines, filename)
    raise StatementError(
        "no parece un estado de cuenta de República AFAP "
        "(no encontré ni el detalle por subfondo ni un resumen con saldo inicial y final)")


# ---------------------------------------------------------------------------
# FORMATO OFICIAL — estado de cuenta semestral en PDF
# ---------------------------------------------------------------------------

def extract_statement_from_lines(lines, filename):
    """
    Extrae datos de transacciones y saldos de las líneas de un estado de cuenta
    oficial. `lines` son las líneas del PDF en formato canónico (`canonical_lines`);
    `filename` sólo se usa para identificar el registro.
    """
    lines = canonical_lines(lines)   # idempotente: admite entrada ya canónica
    full_text = '\n'.join(lines)

    # Extraer fechas del periodo
    periods = re.findall(r'(\d{2}/\d{2}/\d{4})\s*-\s*(\d{2}/\d{2}/\d{4})', full_text)
    parsed = []
    for s, e in periods:
        try:
            parsed.append((datetime.strptime(s, '%d/%m/%Y'), datetime.strptime(e, '%d/%m/%Y')))
        except ValueError:
            continue
    if not parsed:
        raise StatementError("no encontré el periodo (DD/MM/AAAA - DD/MM/AAAA) en el documento")
    period_start, period_end = min(parsed, key=lambda x: x[0])

    # Acumuladores de transacciones
    aportes = 0.0
    comision_admin = 0.0
    seguro = 0.0
    custodia = 0.0
    rentabilidad = 0.0

    for i, line in enumerate(lines):
        # Formato 1: "DD/MM/YYYY concepto" en la misma línea
        dm = re.match(r'\d{2}/\d{2}/\d{4}\s+(.*)', line)
        if dm:
            concept = dm.group(1).lower()
        # Formato 2: "DD/MM/YYYY" solo, concepto en la línea siguiente (Dic2023)
        elif re.match(r'^\d{2}/\d{2}/\d{4}$', line) and i + 1 < len(lines):
            concept = lines[i + 1].lower()
        else:
            continue

        # Ignorar transferencias internas entre subfondos
        if 'transferencia' in concept:
            continue

        # Buscar el primer número en las siguientes líneas
        for j in range(i + 1, min(i + 8, len(lines))):
            if _NUM_RE.match(lines[j]):
                amt = parse_num(lines[j])
                if 'aporte obligatorio' in concept:
                    aportes += amt
                elif 'comisi' in concept and 'administraci' in concept:
                    comision_admin += amt
                elif 'seguro' in concept:
                    seguro += amt
                elif 'custodia' in concept:
                    custodia += amt
                elif 'rentabilidad' in concept:
                    rentabilidad += amt
                break

    # Extraer saldo y UR del cierre usando ternas (saldo, UR, saldo_en_UR).
    # Las anclas de saldo ya identificaron la UR de este documento por posición;
    # cuando existen, la terna sólo se acepta si su segundo número coincide con
    # una de esas URs. Es lo que evita confundirla con la terna homóloga
    # (saldo, valor de la cuota, cantidad de cuotas), que cumple la misma división.
    anchors = extract_balance_anchors(lines)
    known_urs = {round(a['ur'], 2) for a in anchors}
    ur_lo, ur_hi = _ur_window(period_end)

    def _is_ur(value):
        if known_urs:
            return round(value, 2) in known_urs
        return ur_lo < value < ur_hi

    triplets = []
    for i in range(len(lines) - 2):
        if _NUM_RE.match(lines[i]) and _NUM_RE.match(lines[i+1]) and _NUM_RE.match(lines[i+2]):
            v = [parse_num(lines[k]) for k in range(i, i + 3)]
            if v[0] >= 0 and v[2] >= 0 and _is_ur(v[1]) and _ur_ratio_ok(v[0], v[1], v[2]):
                triplets.append(v)

    closing_saldo = triplets[-1][0] if triplets else 0.0
    ur_value = triplets[-1][1] if triplets else 0.0

    # Calcular saldo de apertura a partir de transacciones (más preciso que triplets)
    opening_saldo = closing_saldo - (aportes + comision_admin + seguro + custodia + rentabilidad)

    # UR de apertura: buscar la línea "Saldo" con la fecha más temprana (distinta al cierre)
    opening_ur = 0.0
    period_end_str = period_end.strftime('%d/%m/%Y')
    opening_candidates = []
    for dt, start, end in _balance_phrases(lines):
        if dt.strftime('%d/%m/%Y') == period_end_str:
            continue
        # Buscar UR en los 5 números anteriores a la frase
        ur_val = None
        nums = []
        for j in range(start - 1, max(start - 6, -1), -1):
            if _NUM_RE.match(lines[j]):
                nums.insert(0, parse_num(lines[j]))
            else:
                break
        # En la secuencia de 4-5 números, el UR siempre es el penúltimo
        if len(nums) >= 3:
            ur_val = nums[-2]
        if ur_val is None:
            nums = _run_from(lines, end)
            if len(nums) >= 3:
                ur_val = nums[-2]
        if ur_val is not None:
            opening_candidates.append((dt, ur_val))

    if opening_candidates:
        # Tomar la fecha más temprana (apertura real del periodo)
        opening_candidates.sort(key=lambda x: x[0])
        opening_ur = opening_candidates[0][1]
    elif triplets:
        opening_ur = triplets[0][1]

    if not triplets and aportes == 0 and rentabilidad == 0:
        raise StatementError("reconocí el periodo pero no encontré saldos ni movimientos")

    return {
        'file': filename,
        'period_start': period_start,
        'period_end': period_end,
        'aportes': aportes,
        'comision_admin': comision_admin,
        'seguro': seguro,
        'custodia': custodia,
        'deducciones': comision_admin + seguro + custodia,
        'rentabilidad': rentabilidad,
        'saldo': closing_saldo,
        'ur': ur_value,
        'opening_saldo': opening_saldo,
        'opening_ur': opening_ur,
        'anchors': anchors,
    }


# ---------------------------------------------------------------------------
# FORMATO RESUMEN WEB — descarga del autoservicio
# ---------------------------------------------------------------------------

_WEB_CONCEPTS = (
    ('aportes', ('aporte',)),
    ('comision_admin', ('comisi',)),
    ('seguro', ('seguro',)),
    ('custodia', ('custodia',)),
    ('rentabilidad', ('rentabilidad', 'rendimiento')),
)


def _num_near(lines, i, window=6):
    """Primer número en formato español dentro de la misma línea o las siguientes."""
    inline = re.search(r'-?[\d.]+,\d{2}\s*$', lines[i])
    if inline:
        return parse_num(inline.group(0))
    for j in range(i + 1, min(i + 1 + window, len(lines))):
        if _NUM_RE.match(lines[j]):
            return parse_num(lines[j])
    return None


def extract_web_summary_from_lines(lines, filename):
    """
    Extrae un registro desde el resumen del autoservicio web, que trae "Saldo
    inicial" y "Saldo final" en vez del detalle por subfondo.

    ATENCIÓN: este parser no está validado contra un documento real — se escribió
    a partir de la descripción del formato. Los registros que produce se marcan
    con `'unverified': True` y el informe muestra un aviso. Al conseguir un
    documento de este tipo hay que verificarlo y quitar la marca.
    """
    lines = canonical_lines(lines)   # idempotente: admite entrada ya canónica
    full_text = '\n'.join(lines)

    periods = re.findall(r'(\d{2}/\d{2}/\d{4})\s*-\s*(\d{2}/\d{2}/\d{4})', full_text)
    parsed = []
    for s, e in periods:
        try:
            parsed.append((datetime.strptime(s, '%d/%m/%Y'), datetime.strptime(e, '%d/%m/%Y')))
        except ValueError:
            continue
    if parsed:
        period_start, period_end = min(parsed, key=lambda x: x[0])
    else:
        # Sin rango explícito: usar todas las fechas sueltas del documento
        dates = []
        for d in re.findall(r'\d{2}/\d{2}/\d{4}', full_text):
            try:
                dates.append(datetime.strptime(d, '%d/%m/%Y'))
            except ValueError:
                continue
        if not dates:
            raise StatementError("no encontré fechas de periodo en el resumen")
        period_start, period_end = min(dates), max(dates)

    values = {k: 0.0 for k, _ in _WEB_CONCEPTS}
    opening_saldo = None
    closing_saldo = None
    opening_ur = 0.0
    ur_value = 0.0

    for i, line in enumerate(lines):
        low = line.lower()
        if 'saldo inicial' in low:
            v = _num_near(lines, i)
            if v is not None:
                opening_saldo = v
            continue
        if 'saldo final' in low:
            v = _num_near(lines, i)
            if v is not None:
                closing_saldo = v
            continue
        for key, needles in _WEB_CONCEPTS:
            if any(nd in low for nd in needles):
                v = _num_near(lines, i)
                if v is not None:
                    values[key] += v
                break

    if opening_saldo is None or closing_saldo is None:
        raise StatementError("el resumen no trae saldo inicial y saldo final legibles")

    # La UR no siempre aparece en el resumen; se toma de las anclas si están.
    anchors = extract_balance_anchors(lines)
    for a in anchors:
        if a['date'] <= period_start and not opening_ur:
            opening_ur = a['ur']
        if a['date'] >= period_end:
            ur_value = a['ur']

    # Signos: en el resumen las deducciones suelen venir en positivo.
    for key in ('comision_admin', 'seguro', 'custodia'):
        values[key] = -abs(values[key])

    # La rentabilidad se deduce por diferencia si el resumen no la trae explícita:
    # es lo único que puede cerrar la identidad saldo_final = saldo_inicial + flujos.
    movimientos = values['aportes'] + values['comision_admin'] + values['seguro'] + values['custodia']
    if values['rentabilidad'] == 0.0:
        values['rentabilidad'] = closing_saldo - opening_saldo - movimientos

    return {
        'file': filename,
        'period_start': period_start,
        'period_end': period_end,
        'aportes': values['aportes'],
        'comision_admin': values['comision_admin'],
        'seguro': values['seguro'],
        'custodia': values['custodia'],
        'deducciones': values['comision_admin'] + values['seguro'] + values['custodia'],
        'rentabilidad': values['rentabilidad'],
        'saldo': closing_saldo,
        'ur': ur_value,
        'opening_saldo': opening_saldo,
        'opening_ur': opening_ur,
        'anchors': anchors,
        'unverified': True,
    }


# ---------------------------------------------------------------------------
# LÍNEA TEMPORAL DE SEMESTRES
# ---------------------------------------------------------------------------

def _period_start_for(period_end):
    """Fecha de inicio del semestre que termina en `period_end` (Jun30 / Dic31)."""
    if period_end.month == 6:
        return datetime(period_end.year, 1, 1)
    return datetime(period_end.year, 7, 1)


def _next_semester_end(period_end):
    """Cierre del semestre siguiente a `period_end`."""
    if period_end.month == 6:
        return datetime(period_end.year, 12, 31)
    return datetime(period_end.year + 1, 6, 30)


def _semester_end_for(date):
    """Cierre del semestre al que pertenece `date` (30/06 o 31/12)."""
    if date.month <= 6:
        return datetime(date.year, 6, 30)
    return datetime(date.year, 12, 31)


def empty_statement(period_end):
    """
    Registro vacío (placeholder) para un semestre sin estado de cuenta disponible.
    Conserva las fechas del periodo y marca `empty`; los datos financieros quedan
    en None. La estimación se realiza recién al generar el informe (Parte 2).
    """
    return {
        'file': None,
        'period_start': _period_start_for(period_end),
        'period_end': period_end,
        'aportes': None,
        'comision_admin': None,
        'seguro': None,
        'custodia': None,
        'deducciones': None,
        'rentabilidad': None,
        'saldo': None,
        'ur': None,
        'opening_saldo': None,
        'opening_ur': None,
        'anchors': [],
        'empty': True,
    }


def normalize_statements(statements):
    """
    Ordena los estados por cierre de periodo, alinea cada uno a su semestre y
    descarta duplicados del mismo semestre (se queda con el primero que llegó).
    Devuelve `(statements, warnings)`.
    """
    warnings = []
    normalized = []
    for s in statements:
        s = dict(s)
        end = _semester_end_for(s['period_end'])
        if end != s['period_end']:
            warnings.append(
                f"{s.get('file') or 'documento'}: el periodo cierra el "
                f"{s['period_end'].strftime('%d/%m/%Y')}; se lo trata como el semestre "
                f"que cierra el {end.strftime('%d/%m/%Y')}.")
            s['period_end'] = end
            s['period_start'] = _period_start_for(end)
        normalized.append(s)

    normalized.sort(key=lambda x: x['period_end'])

    unique = []
    seen = {}
    for s in normalized:
        end = s['period_end']
        if end in seen:
            warnings.append(
                f"{s.get('file') or 'documento'}: es un segundo estado del semestre "
                f"{MESES[end.month]}{end.year}; se usa {seen[end]} y este se ignora.")
            continue
        seen[end] = s.get('file') or 'documento'
        unique.append(s)

    return unique, warnings


def insert_empty_periods(statements):
    """
    Completa la línea temporal de semestres entre el primer y el último estado
    disponible, insertando un registro vacío (`empty_statement`) por cada semestre
    sin documento. Devuelve la estructura ordenada cronológicamente.
    """
    if not statements:
        return []
    by_end = {s['period_end']: s for s in statements}
    result = []
    cur = statements[0]['period_end']
    last = statements[-1]['period_end']
    while cur <= last:
        result.append(by_end.get(cur) or empty_statement(cur))
        cur = _next_semester_end(cur)
    return result


def estimate_empty_periods(statements):
    """
    Reemplaza cada registro vacío por uno estimado y devuelve `(periodos, avisos)`.

    Un hueco de un solo semestre se resuelve con el saldo de apertura del periodo
    siguiente (que es el cierre real del faltante) y las proporciones de ese
    periodo para desglosar aportes, deducciones y rentabilidad. Para huecos de dos
    o más semestres seguidos se interpola el saldo y la UR entre el último cierre
    real y la apertura del siguiente estado, y se aplica el mismo desglose a cada
    tramo: sin esto, encadenar estimaciones sobre un registro todavía vacío rompe
    el análisis. Los registros con datos reales no se tocan.
    """
    filled = list(statements)
    warnings = []
    n = len(filled)
    i = 0
    while i < n:
        if not filled[i].get('empty'):
            i += 1
            continue

        # Extensión del hueco: [i, j)
        j = i
        while j < n and filled[j].get('empty'):
            j += 1
        k = j - i

        if i == 0 or j >= n:
            # Hueco en un extremo: no hay con qué anclarlo (no debería ocurrir,
            # `insert_empty_periods` sólo crea huecos internos).
            i = j
            continue

        prev_p = filled[i - 1]   # último periodo con datos antes del hueco
        next_p = filled[j]       # primer periodo con datos después del hueco

        target_saldo = next_p['opening_saldo']
        target_ur = next_p['opening_ur']
        if target_saldo is None or target_ur is None:
            i = j
            continue

        # Proporciones del periodo siguiente para desglosar cada tramo
        next_total_mov = next_p['aportes'] + next_p['deducciones'] + next_p['rentabilidad']
        if abs(next_total_mov) > 0:
            ratio_aportes = next_p['aportes'] / next_total_mov
            ratio_deduc = next_p['deducciones'] / next_total_mov
            ratio_rent = next_p['rentabilidad'] / next_total_mov
        else:
            ratio_aportes = 0.6
            ratio_deduc = -0.1
            ratio_rent = 0.5

        base_saldo = prev_p['saldo']
        base_ur = prev_p['ur']
        for step in range(1, k + 1):
            p = filled[i + step - 1]
            frac_prev = (step - 1) / k
            frac_cur = step / k
            saldo_prev = base_saldo + (target_saldo - base_saldo) * frac_prev
            saldo_cur = base_saldo + (target_saldo - base_saldo) * frac_cur
            ur_prev = base_ur + (target_ur - base_ur) * frac_prev
            ur_cur = base_ur + (target_ur - base_ur) * frac_cur

            delta = saldo_cur - saldo_prev
            est_aportes = abs(delta * ratio_aportes)
            est_deduc = delta * ratio_deduc
            est_rent = delta * ratio_rent

            filled[i + step - 1] = {
                'file': '(estimado)',
                'period_start': p['period_start'],
                'period_end': p['period_end'],
                'aportes': est_aportes,
                'comision_admin': est_deduc * 0.3,
                'seguro': est_deduc * 0.6,
                'custodia': est_deduc * 0.1,
                'deducciones': est_deduc,
                'rentabilidad': est_rent,
                'saldo': saldo_cur,
                'ur': ur_cur,
                'opening_saldo': saldo_prev,
                'opening_ur': ur_prev,
                'estimated': True,
            }

        label_range = ', '.join(
            f"{MESES[filled[x]['period_end'].month]}{filled[x]['period_end'].year}"
            for x in range(i, j))
        warnings.append(
            f"Sin estado de cuenta para {label_range}: se estimó a partir de los periodos vecinos."
            + (" Al ser más de un semestre seguido, la estimación es más gruesa." if k > 1 else ""))

        i = j

    # Cualquier registro que haya quedado vacío no puede entrar al análisis
    result = []
    for p in filled:
        if p.get('empty'):
            end = p['period_end']
            warnings.append(
                f"{MESES[end.month]}{end.year} quedó fuera del informe: no hay datos "
                f"suficientes en los periodos vecinos para estimarlo.")
            continue
        result.append(p)

    return result, warnings


# ---------------------------------------------------------------------------
# ANÁLISIS
# ---------------------------------------------------------------------------

def build_analysis(periods_data):
    """Construye el análisis acumulativo periodo a periodo."""
    analysis = []
    cum_aportes = 0.0
    cum_deducciones = 0.0
    cum_rentabilidad = 0.0
    cum_neto = 0.0
    base_ur = periods_data[0]['opening_ur'] if periods_data else 1.0

    for p in periods_data:
        cum_aportes += p['aportes']
        cum_deducciones += p['deducciones']
        cum_neto += p['aportes'] + p['deducciones']  # deducciones son negativos
        cum_rentabilidad += p['rentabilidad']

        # Inflación acumulada desde el inicio
        inflacion_acum = (p['ur'] / base_ur - 1) * 100 if base_ur > 0 else 0

        # Crecimiento del saldo como % de aportes netos
        crecimiento_vs_neto = ((p['saldo'] / cum_neto - 1) * 100) if cum_neto > 0 else 0

        # Valor teórico si aportes netos solo crecieran con inflación
        # Calcular ajustando cada aporte por inflación desde su momento
        saldo_inflacion = cum_neto * (p['ur'] / base_ur) if base_ur > 0 else cum_neto

        label = f"{MESES[p['period_end'].month]}{p['period_end'].year}"

        analysis.append({
            'label': label,
            'period_end': p['period_end'],
            'aportes_periodo': p['aportes'],
            'deducciones_periodo': p['deducciones'],
            'comision_admin_periodo': p['comision_admin'],
            'seguro_periodo': p['seguro'],
            'custodia_periodo': p['custodia'],
            'neto_invertido_periodo': p['aportes'] + p['deducciones'],
            'rentabilidad_periodo': p['rentabilidad'],
            'cum_aportes': cum_aportes,
            'cum_deducciones': cum_deducciones,
            'cum_neto': cum_neto,
            'cum_rentabilidad': cum_rentabilidad,
            'saldo': p['saldo'],
            'ur': p['ur'],
            'inflacion_acum': inflacion_acum,
            'saldo_inflacion': saldo_inflacion,
            'estimated': p.get('estimated', False),
        })

    return analysis


def _adjust_flows_by_inflation(periods_data, flow_of):
    """
    Acumula un flujo semestral ajustándolo por inflación (UR).

    `flow_of(p)` devuelve el flujo del periodo `p`. Cada flujo del periodo `s` se
    expresa en pesos del periodo `t` multiplicándolo por `UR_t / UR_s` (factor >= 1:
    el dinero viejo vale más pesos de hoy). El valor en la posición `t` es, entonces,
    todo lo aportado hasta ese momento expresado en pesos de esa misma fecha, que es
    la unidad en la que está el saldo real con el que se compara.
    """
    if not periods_data:
        return []

    results = []
    for t in range(len(periods_data)):
        adjusted_total = 0.0
        for s in range(t + 1):
            if periods_data[s]['ur'] > 0:
                factor = periods_data[t]['ur'] / periods_data[s]['ur']
            else:
                factor = 1.0
            adjusted_total += flow_of(periods_data[s]) * factor
        results.append(adjusted_total)

    return results


def compute_inflation_adjusted_contributions(periods_data):
    """
    Valor ajustado por inflación de los aportes, en dos series alineadas con
    `periods_data`: (netos, brutos). Son las mismas dos series acumuladas de la
    gráfica 3, pero ajustadas por inflación, para poder compararlas lado a lado.
    """
    neto = _adjust_flows_by_inflation(periods_data, lambda p: p['aportes'] + p['deducciones'])
    bruto = _adjust_flows_by_inflation(periods_data, lambda p: p['aportes'])
    return neto, bruto


def build_fund_anchor_map(periods_data):
    """
    Combina las anclas de saldo de todos los estados en un mapa {fecha: ancla}.
    Ante fechas repetidas (transición entre subfondos) prioriza el saldo mayor,
    es decir, el subfondo donde efectivamente está el dinero.
    """
    anchor_map = {}
    for p in periods_data:
        for a in p.get('anchors', []):
            d = a['date']
            if d not in anchor_map or a['saldo'] > anchor_map[d]['saldo']:
                anchor_map[d] = a
    return anchor_map


def compute_fund_real_returns(analysis, anchor_map):
    """
    Rendimiento del fondo por semestre (en %), descompuesto en sus tres cifras:

        nominal   = cuota_t / cuota_{t-1} - 1        (variación del valor de la cuota)
        inflacion = UR_t / UR_{t-1} - 1              (inflación del semestre)
        real      = (1 + nominal) / (1 + inflacion) - 1

    Es el rendimiento del fondo en general (independiente de los flujos de esta
    cuenta). Devuelve una lista alineada con `analysis` de dicts
    {'nominal', 'inflacion', 'real'}, o None si falta el ancla del periodo.
    Nota: el real NO es la resta de las otras dos (aunque se le parece); por eso se
    calcula aparte y se muestra en el tooltip.
    Nota: los valores de la cuota y la UR provienen siempre de documentos reales
    (incluida la apertura del estado siguiente), por lo que un periodo sin estado
    de cuenta individual igual puede mostrar la rentabilidad real del fondo.
    """
    if not anchor_map:
        return [None] * len(analysis)

    base_date = min(anchor_map)
    results = []
    prev_date = base_date
    for a in analysis:
        d = a['period_end']
        cur = anchor_map.get(d)
        pv = anchor_map.get(prev_date)
        if cur and pv and pv['cuota'] > 0 and pv['ur'] > 0 and cur['ur'] > 0:
            nominal = cur['cuota'] / pv['cuota'] - 1
            inflacion = cur['ur'] / pv['ur'] - 1
            real = (1 + nominal) / (1 + inflacion) - 1
            results.append({
                'nominal': round(nominal * 100, 2),
                'inflacion': round(inflacion * 100, 2),
                'real': round(real * 100, 2),
            })
        else:
            results.append(None)
        if cur:
            prev_date = d
    return results


# ---------------------------------------------------------------------------
# INFORME HTML
# ---------------------------------------------------------------------------

def generate_html(analysis, inflation_adjusted, inflation_adjusted_bruto, fund_real_returns,
                  warnings=None):
    """Genera el informe HTML con gráficas Chart.js."""

    labels = [a['label'] for a in analysis]
    n = len(analysis)

    # Resumen general
    last = analysis[-1]
    total_aportes = last['cum_aportes']
    total_deducciones = last['cum_deducciones']
    total_neto = last['cum_neto']
    total_rentabilidad = last['cum_rentabilidad']
    saldo_final = last['saldo']
    inflacion_total = last['inflacion_acum']

    pct_deducciones = (abs(total_deducciones) / total_aportes * 100) if total_aportes > 0 else 0
    pct_invertido = (total_neto / total_aportes * 100) if total_aportes > 0 else 0
    # Ganancia real sobre el neto invertido: cuánto del saldo excede a los aportes que
    # efectivamente entraron al fondo, una vez que estos se ajustan por inflación.
    ganancia_real = saldo_final - inflation_adjusted[-1] if inflation_adjusted else 0
    pct_ganancia_real = (ganancia_real / inflation_adjusted[-1] * 100) if inflation_adjusted and inflation_adjusted[-1] > 0 else 0

    # Ídem, pero contra los aportes brutos: incluye el costo de comisiones y seguros,
    # así que responde "¿el sistema me devolvió el poder de compra de todo lo que aporté?".
    ganancia_real_bruto = saldo_final - inflation_adjusted_bruto[-1] if inflation_adjusted_bruto else 0
    pct_ganancia_real_bruto = (ganancia_real_bruto / inflation_adjusted_bruto[-1] * 100) if inflation_adjusted_bruto and inflation_adjusted_bruto[-1] > 0 else 0

    # Datos para gráficas
    aportes_periodo = [round(a['aportes_periodo'], 2) for a in analysis]
    deducciones_periodo = [round(abs(a['deducciones_periodo']), 2) for a in analysis]
    neto_periodo = [round(a['neto_invertido_periodo'], 2) for a in analysis]
    rentabilidad_periodo = [round(a['rentabilidad_periodo'], 2) for a in analysis]
    cum_aportes = [round(a['cum_aportes'], 2) for a in analysis]
    cum_deducciones = [round(abs(a['cum_deducciones']), 2) for a in analysis]
    cum_neto = [round(a['cum_neto'], 2) for a in analysis]
    saldos = [round(a['saldo'], 2) for a in analysis]
    inflation_adj = [round(v, 2) for v in inflation_adjusted]
    inflation_adj_bruto = [round(v, 2) for v in inflation_adjusted_bruto]

    # Desglose deducciones por periodo
    comision_admin = [round(abs(a['comision_admin_periodo']), 2) for a in analysis]
    seguro = [round(abs(a['seguro_periodo']), 2) for a in analysis]
    custodia = [round(abs(a['custodia_periodo']), 2) for a in analysis]

    # Rentabilidad real (por encima de inflación)
    rentabilidad_real_acum = [round(saldos[i] - inflation_adj[i], 2) for i in range(n)]

    # Rendimiento del fondo por periodo (%) — None -> null para Chart.js
    fund_nominal_js = json.dumps([f['nominal'] if f else None for f in fund_real_returns])
    fund_inflacion_js = json.dumps([f['inflacion'] if f else None for f in fund_real_returns])
    fund_real_js = json.dumps([f['real'] if f else None for f in fund_real_returns])

    # Avisos del procesamiento (periodos estimados, archivos ignorados, etc.)
    warnings_html = ''
    if warnings:
        items = '\n'.join(f"            <li>{_escape(w)}</li>" for w in warnings)
        warnings_html = f"""
    <div class="notice">
        <h3>Avisos sobre estos datos</h3>
        <ul>
{items}
        </ul>
    </div>
"""

    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Análisis de Cuenta AFAP República</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: #f0f2f5;
            color: #1a1a2e;
            line-height: 1.6;
        }}
        .container {{
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
        }}
        header {{
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%);
            color: white;
            padding: 40px 20px;
            text-align: center;
            margin-bottom: 30px;
        }}
        header h1 {{ font-size: 2em; margin-bottom: 10px; }}
        header p {{ opacity: 0.8; font-size: 1.1em; }}

        .notice {{
            background: #fff8e1;
            border: 1px solid #f0d9a0;
            border-left: 4px solid #f39c12;
            border-radius: 12px;
            padding: 18px 24px;
            margin-bottom: 30px;
        }}
        .notice h3 {{ font-size: 0.95em; margin-bottom: 8px; color: #8a6d1f; }}
        .notice ul {{ margin: 0 0 0 18px; font-size: 0.9em; color: #6b5518; }}

        .summary-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 20px;
            margin-bottom: 40px;
        }}
        .summary-card {{
            background: white;
            border-radius: 12px;
            padding: 24px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.08);
            border-left: 4px solid #0f3460;
        }}
        .summary-card.positive {{ border-left-color: #27ae60; }}
        .summary-card.negative {{ border-left-color: #e74c3c; }}
        .summary-card.info {{ border-left-color: #3498db; }}
        .summary-card.neutral {{ border-left-color: #f39c12; }}
        .summary-card h3 {{
            font-size: 0.85em;
            text-transform: uppercase;
            color: #666;
            margin-bottom: 8px;
            letter-spacing: 0.5px;
        }}
        .summary-card .value {{
            font-size: 1.6em;
            font-weight: 700;
            color: #1a1a2e;
        }}
        .summary-card .detail {{
            font-size: 0.85em;
            color: #888;
            margin-top: 4px;
        }}

        .chart-section {{
            background: white;
            border-radius: 12px;
            padding: 30px;
            margin-bottom: 30px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.08);
        }}
        .chart-section h2 {{
            font-size: 1.3em;
            margin-bottom: 8px;
            color: #1a1a2e;
        }}
        .chart-section .description {{
            color: #666;
            font-size: 0.9em;
            margin-bottom: 20px;
        }}
        .chart-container {{
            position: relative;
            width: 100%;
            max-height: 450px;
        }}

        table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 0.85em;
        }}
        th, td {{
            padding: 10px 12px;
            text-align: right;
            border-bottom: 1px solid #eee;
        }}
        th {{
            background: #f8f9fa;
            font-weight: 600;
            color: #555;
            text-transform: uppercase;
            font-size: 0.8em;
            letter-spacing: 0.5px;
        }}
        td:first-child, th:first-child {{ text-align: left; }}
        tr:hover td {{ background: #f8f9fa; }}
        .estimated {{ color: #999; font-style: italic; }}

        footer {{
            text-align: center;
            padding: 30px;
            color: #999;
            font-size: 0.85em;
        }}

        @media (max-width: 768px) {{
            header h1 {{ font-size: 1.4em; }}
            .summary-card .value {{ font-size: 1.3em; }}
            .chart-section {{ padding: 15px; }}
            table {{ font-size: 0.75em; }}
            th, td {{ padding: 6px 8px; }}
        }}
    </style>
</head>
<body>

<header>
    <h1>Análisis de Cuenta AFAP República</h1>
    <p>Evolución de la cuenta de capitalización individual &mdash;
       {analysis[0]['label']} a {analysis[-1]['label']}</p>
</header>

<div class="container">
{warnings_html}
    <!-- RESUMEN -->
    <div class="summary-grid">
        <div class="summary-card info">
            <h3>Aportes Totales</h3>
            <div class="value">{fmt_money(total_aportes)}</div>
            <div class="detail">Suma de todos los aportes realizados</div>
        </div>
        <div class="summary-card negative">
            <h3>Deducciones Totales</h3>
            <div class="value">{fmt_money(abs(total_deducciones))}</div>
            <div class="detail">{pct_deducciones:.1f}% de los aportes</div>
        </div>
        <div class="summary-card neutral">
            <h3>Neto Invertido</h3>
            <div class="value">{fmt_money(total_neto)}</div>
            <div class="detail">{pct_invertido:.1f}% de los aportes efectivamente invertido</div>
        </div>
        <div class="summary-card positive">
            <h3>Rentabilidad Generada</h3>
            <div class="value">{fmt_money(total_rentabilidad)}</div>
            <div class="detail">Renta acumulada sobre lo invertido</div>
        </div>
        <div class="summary-card" style="border-left-color: #8e44ad;">
            <h3>Saldo Actual</h3>
            <div class="value">{fmt_money(saldo_final)}</div>
            <div class="detail">Al cierre del último periodo</div>
        </div>
        <div class="summary-card info">
            <h3>Inflación Acumulada</h3>
            <div class="value">{inflacion_total:.1f}%</div>
            <div class="detail">Variación de la UR en el periodo</div>
        </div>
        <div class="summary-card {'positive' if ganancia_real > 0 else 'negative'}">
            <h3>Ganancia Real vs Neto Invertido</h3>
            <div class="value">{fmt_money(ganancia_real)}</div>
            <div class="detail">{"Por encima" if ganancia_real > 0 else "Por debajo"} del neto invertido ajustado por inflación ({pct_ganancia_real:+.1f}%)</div>
        </div>
        <div class="summary-card {'positive' if ganancia_real_bruto > 0 else 'negative'}">
            <h3>Ganancia Real vs Aportes Totales</h3>
            <div class="value">{fmt_money(ganancia_real_bruto)}</div>
            <div class="detail">{"Por encima" if ganancia_real_bruto > 0 else "Por debajo"} de los aportes totales ajustados por inflación ({pct_ganancia_real_bruto:+.1f}%)</div>
        </div>
    </div>

    <!-- GRÁFICA 1: Aportes vs Deducciones por periodo -->
    <div class="chart-section">
        <h2>1. Aportes y Deducciones por Periodo</h2>
        <p class="description">Muestra cuánto se aportó en cada semestre y cuánto se dedujo en comisiones y seguros.</p>
        <div class="chart-container">
            <canvas id="chart1"></canvas>
        </div>
    </div>

    <!-- GRÁFICA 2: Desglose de deducciones -->
    <div class="chart-section">
        <h2>2. Desglose de Deducciones por Periodo</h2>
        <p class="description">Comisión por administración, seguro de invalidez y fallecimiento, y custodia BCU.</p>
        <div class="chart-container">
            <canvas id="chart2"></canvas>
        </div>
    </div>

    <!-- GRÁFICA 3: Evolución del saldo -->
    <div class="chart-section">
        <h2>3. Evolución del Saldo vs Aportes Acumulados</h2>
        <p class="description">Compara el saldo real de la cuenta con los aportes totales y el neto efectivamente invertido.
        La diferencia entre el saldo y el neto invertido representa la rentabilidad generada.</p>
        <div class="chart-container">
            <canvas id="chart3"></canvas>
        </div>
    </div>

    <!-- GRÁFICA 4: Saldo vs Inflación (misma info que la 3, ajustada por inflación) -->
    <div class="chart-section">
        <h2>4. Evolución del Saldo vs Aportes Acumulados, Ajustados por Inflación</h2>
        <p class="description">Las mismas tres series de la gráfica 3, pero con los aportes expresados en pesos de
        cada fecha: cada aporte se ajusta por la inflación (medida por la Unidad Reajustable) desde el momento
        en que se hizo. Es decir, cuánto habría que tener hoy para igualar el poder de compra de lo aportado.
        Si el saldo está por encima del neto invertido ajustado, la rentabilidad superó a la inflación.</p>
        <div class="chart-container">
            <canvas id="chart5"></canvas>
        </div>
    </div>

    <!-- GRÁFICA 5: Rentabilidad por periodo -->
    <div class="chart-section">
        <h2>5. Rentabilidad por Periodo</h2>
        <p class="description">Renta generada en cada semestre por la inversión del fondo.</p>
        <div class="chart-container">
            <canvas id="chart4"></canvas>
        </div>
    </div>

    <!-- GRÁFICA 6: Rendimiento del fondo vs inflación (%) -->
    <div class="chart-section">
        <h2>6. Rendimiento del Fondo vs Inflación por Periodo (%)</h2>
        <p class="description">Para cada semestre, cuánto subió el valor de la cuota del subfondo de
        República AFAP y cuánto subió la inflación (UR). El dinero ya invertido ganó poder de compra en los
        semestres en que la barra del fondo supera a la de inflación. A diferencia de la gráfica 5, no es la
        renta de esta cuenta sino el desempeño del fondo en general, independiente de los aportes individuales.
        El tooltip muestra la rentabilidad real exacta del semestre.</p>
        <div class="chart-container">
            <canvas id="chartFondo"></canvas>
        </div>
    </div>

    <!-- TABLA DETALLADA -->
    <div class="chart-section">
        <h2>7. Detalle Periodo a Periodo</h2>
        <p class="description">Datos numéricos de cada semestre.</p>
        <div style="overflow-x: auto;">
            <table>
                <thead>
                    <tr>
                        <th>Periodo</th>
                        <th>Aportes</th>
                        <th>Deducciones</th>
                        <th>Neto Invertido</th>
                        <th>Rentabilidad</th>
                        <th>Saldo</th>
                        <th>UR</th>
                        <th>Inflac. Acum.</th>
                    </tr>
                </thead>
                <tbody>
"""

    for a in analysis:
        est_class = ' class="estimated"' if a.get('estimated') else ''
        html += f"""                    <tr{est_class}>
                        <td>{a['label']}{'*' if a.get('estimated') else ''}</td>
                        <td>{fmt_money(a['aportes_periodo'])}</td>
                        <td>{fmt_money(abs(a['deducciones_periodo']))}</td>
                        <td>{fmt_money(a['neto_invertido_periodo'])}</td>
                        <td>{fmt_money(a['rentabilidad_periodo'])}</td>
                        <td>{fmt_money(a['saldo'])}</td>
                        <td>{a['ur']:,.2f}</td>
                        <td>{a['inflacion_acum']:.1f}%</td>
                    </tr>
"""

    html += f"""                </tbody>
            </table>
            <p style="color: #999; font-size: 0.8em; margin-top: 10px;">
                * Periodo estimado (no se dispone del estado de cuenta individual).
            </p>
        </div>
    </div>

</div>

<footer>
    Informe generado el {datetime.now().strftime('%d/%m/%Y %H:%M')} &mdash;
    Datos extraídos de estados de cuenta de República AFAP
</footer>

<script>
    const labels = {labels};
    const chartColors = {{
        blue: 'rgb(52, 152, 219)',
        green: 'rgb(39, 174, 96)',
        red: 'rgb(231, 76, 60)',
        orange: 'rgb(243, 156, 18)',
        purple: 'rgb(142, 68, 173)',
        teal: 'rgb(22, 160, 133)',
        blueFill: 'rgba(52, 152, 219, 0.15)',
        greenFill: 'rgba(39, 174, 96, 0.15)',
        redFill: 'rgba(231, 76, 60, 0.15)',
        purpleFill: 'rgba(142, 68, 173, 0.15)',
    }};

    const tooltipConfig = {{
        callbacks: {{
            label: function(context) {{
                let val = context.parsed.y;
                return context.dataset.label + ': $ ' +
                    val.toLocaleString('es-UY', {{minimumFractionDigits: 0, maximumFractionDigits: 0}});
            }}
        }}
    }};

    const scaleY = {{
        ticks: {{
            callback: function(value) {{
                if (Math.abs(value) >= 1000) {{
                    return '$ ' + (value/1000).toFixed(0) + 'k';
                }}
                return '$ ' + value;
            }}
        }}
    }};

    // Gráfica 1: Aportes vs Deducciones
    new Chart(document.getElementById('chart1'), {{
        type: 'bar',
        data: {{
            labels: labels,
            datasets: [
                {{
                    label: 'Aportes',
                    data: {aportes_periodo},
                    backgroundColor: chartColors.blue,
                    borderRadius: 4,
                }},
                {{
                    label: 'Deducciones',
                    data: {deducciones_periodo},
                    backgroundColor: chartColors.red,
                    borderRadius: 4,
                }},
                {{
                    label: 'Neto Invertido',
                    data: {neto_periodo},
                    backgroundColor: chartColors.green,
                    borderRadius: 4,
                }}
            ]
        }},
        options: {{
            responsive: true,
            plugins: {{ tooltip: tooltipConfig }},
            scales: {{ y: scaleY }}
        }}
    }});

    // Gráfica 2: Desglose deducciones
    new Chart(document.getElementById('chart2'), {{
        type: 'bar',
        data: {{
            labels: labels,
            datasets: [
                {{
                    label: 'Comisión Administración',
                    data: {comision_admin},
                    backgroundColor: 'rgb(231, 76, 60)',
                    borderRadius: 4,
                }},
                {{
                    label: 'Seguro Inv. y Fallecimiento',
                    data: {seguro},
                    backgroundColor: 'rgb(243, 156, 18)',
                    borderRadius: 4,
                }},
                {{
                    label: 'Custodia BCU',
                    data: {custodia},
                    backgroundColor: 'rgb(149, 165, 166)',
                    borderRadius: 4,
                }}
            ]
        }},
        options: {{
            responsive: true,
            plugins: {{ tooltip: tooltipConfig }},
            scales: {{ x: {{ stacked: true }}, y: {{ ...scaleY, stacked: true }} }}
        }}
    }});

    // Gráfica 3: Evolución del saldo
    new Chart(document.getElementById('chart3'), {{
        type: 'line',
        data: {{
            labels: labels,
            datasets: [
                {{
                    label: 'Saldo Real',
                    data: {saldos},
                    borderColor: chartColors.purple,
                    backgroundColor: chartColors.purpleFill,
                    fill: true,
                    tension: 0.3,
                    pointRadius: 4,
                    borderWidth: 2.5,
                }},
                {{
                    label: 'Aportes Acumulados (Bruto)',
                    data: {cum_aportes},
                    borderColor: chartColors.blue,
                    borderDash: [6, 3],
                    tension: 0.3,
                    pointRadius: 3,
                    borderWidth: 2,
                    fill: false,
                }},
                {{
                    label: 'Neto Invertido Acumulado',
                    data: {cum_neto},
                    borderColor: chartColors.green,
                    borderDash: [6, 3],
                    tension: 0.3,
                    pointRadius: 3,
                    borderWidth: 2,
                    fill: false,
                }}
            ]
        }},
        options: {{
            responsive: true,
            plugins: {{ tooltip: tooltipConfig }},
            scales: {{ y: scaleY }}
        }}
    }});

    // Gráfica 4: Saldo vs aportes ajustados por inflación
    // Mismas series que la gráfica 3 (mismos colores y trazos), con los aportes
    // ajustados por inflación, para poder leer las dos gráficas una al lado de la otra.
    new Chart(document.getElementById('chart5'), {{
        type: 'line',
        data: {{
            labels: labels,
            datasets: [
                {{
                    label: 'Saldo Real',
                    data: {saldos},
                    borderColor: chartColors.purple,
                    backgroundColor: chartColors.purpleFill,
                    fill: true,
                    tension: 0.3,
                    pointRadius: 4,
                    borderWidth: 2.5,
                }},
                {{
                    label: 'Aportes Acumulados (Bruto) ajustados por inflación (UR)',
                    data: {inflation_adj_bruto},
                    borderColor: chartColors.blue,
                    borderDash: [6, 3],
                    tension: 0.3,
                    pointRadius: 3,
                    borderWidth: 2,
                    fill: false,
                }},
                {{
                    label: 'Neto Invertido Acumulado ajustado por inflación (UR)',
                    data: {inflation_adj},
                    borderColor: chartColors.green,
                    borderDash: [6, 3],
                    tension: 0.3,
                    pointRadius: 3,
                    borderWidth: 2,
                    fill: false,
                }}
            ]
        }},
        options: {{
            responsive: true,
            plugins: {{ tooltip: tooltipConfig }},
            scales: {{ y: scaleY }}
        }}
    }});

    // Gráfica 5: Rentabilidad por periodo
    new Chart(document.getElementById('chart4'), {{
        type: 'bar',
        data: {{
            labels: labels,
            datasets: [{{
                label: 'Rentabilidad',
                data: {rentabilidad_periodo},
                backgroundColor: {rentabilidad_periodo}.map(v => v >= 0 ? chartColors.green : chartColors.red),
                borderRadius: 4,
            }}]
        }},
        options: {{
            responsive: true,
            plugins: {{ tooltip: tooltipConfig }},
            scales: {{ y: scaleY }}
        }}
    }});

    // Gráfica 6: Rendimiento del fondo vs inflación por periodo (%)
    const fundNominal = {fund_nominal_js};
    const fundInflacion = {fund_inflacion_js};
    const fundReal = {fund_real_js};
    new Chart(document.getElementById('chartFondo'), {{
        type: 'bar',
        data: {{
            labels: labels,
            datasets: [
                {{
                    label: 'Fondo (valor de la cuota)',
                    data: fundNominal,
                    // El color marca el resultado real del semestre: verde si el fondo
                    // le ganó a la inflación, rojo si perdió contra ella.
                    backgroundColor: fundReal.map(v => v === null ? 'rgba(0,0,0,0.05)' : (v >= 0 ? chartColors.teal : chartColors.red)),
                    borderRadius: 4,
                }},
                {{
                    label: 'Inflación (UR)',
                    data: fundInflacion,
                    backgroundColor: 'rgb(149, 165, 166)',
                    borderRadius: 4,
                }}
            ]
        }},
        options: {{
            responsive: true,
            plugins: {{
                tooltip: {{
                    callbacks: {{
                        label: function(context) {{
                            const v = context.parsed.y;
                            return v === null ? context.dataset.label + ': sin dato'
                                              : context.dataset.label + ': ' + v.toFixed(2) + ' %';
                        }},
                        footer: function(items) {{
                            const v = fundReal[items[0].dataIndex];
                            return v === null ? '' : 'Rentabilidad real: ' + v.toFixed(2) + ' %';
                        }}
                    }}
                }}
            }},
            scales: {{
                y: {{
                    ticks: {{ callback: function(value) {{ return value + ' %'; }} }}
                }}
            }}
        }}
    }});

</script>

</body>
</html>"""

    return html


def _escape(s):
    """Escapa texto para insertarlo en el HTML del informe."""
    return (str(s).replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;'))


# ---------------------------------------------------------------------------
# INFORME COMPLETO
# ---------------------------------------------------------------------------

def build_report(statements):
    """
    Construye el informe HTML a partir de la estructura de datos intermedia.

    Ordena y deduplica los estados, completa y estima los semestres faltantes,
    arma el análisis acumulado, calcula los aportes ajustados por inflación y la
    rentabilidad real del fondo, y devuelve `(html, warnings)`. No escribe
    archivos: el CLI guarda el HTML en disco y el sitio lo muestra en un iframe.
    """
    if not statements:
        raise StatementError("no hay ningún estado de cuenta para analizar")

    warnings = []

    # Los estados pueden venir ya con huecos (JSON persistido) o sin ellos (extracción
    # nueva); en ambos casos se normaliza y se completa la línea temporal.
    real = [s for s in statements if not s.get('empty')]
    real, norm_warnings = normalize_statements(real)
    warnings.extend(norm_warnings)

    if any(s.get('unverified') for s in real):
        warnings.append(
            "Alguno de los documentos es un resumen del autoservicio web; ese formato "
            "todavía no está validado contra un documento real, así que revisá los "
            "números de ese periodo contra el original.")

    periods_data = insert_empty_periods(real)
    periods_data, est_warnings = estimate_empty_periods(periods_data)
    warnings.extend(est_warnings)

    if not periods_data:
        raise StatementError("no quedó ningún periodo con datos suficientes para el informe")

    if len(periods_data) == 1:
        warnings.append(
            "Hay un solo semestre: las gráficas de evolución muestran un único punto y "
            "la inflación acumulada es 0%. Cargá más estados para ver la evolución.")

    analysis = build_analysis(periods_data)
    inflation_adjusted, inflation_adjusted_bruto = compute_inflation_adjusted_contributions(periods_data)
    anchor_map = build_fund_anchor_map(periods_data)
    fund_real_returns = compute_fund_real_returns(analysis, anchor_map)

    html = generate_html(analysis, inflation_adjusted, inflation_adjusted_bruto,
                         fund_real_returns, warnings)
    return html, warnings


# ---------------------------------------------------------------------------
# SERIALIZACIÓN de la estructura de datos intermedia
# ---------------------------------------------------------------------------

_DATE_FIELDS = ('period_start', 'period_end')


def statement_to_json(s):
    """Convierte un registro a una forma serializable en JSON (fechas -> ISO)."""
    d = dict(s)
    for k in _DATE_FIELDS:
        if isinstance(d.get(k), datetime):
            d[k] = d[k].isoformat()
    if d.get('anchors'):
        d['anchors'] = [{**a, 'date': a['date'].isoformat()} for a in d['anchors']]
    return d


def statement_from_json(d):
    """Reconstruye un registro desde JSON (ISO -> fechas)."""
    s = dict(d)
    for k in _DATE_FIELDS:
        if s.get(k):
            s[k] = datetime.fromisoformat(s[k])
    if s.get('anchors'):
        s['anchors'] = [{**a, 'date': datetime.fromisoformat(a['date'])} for a in s['anchors']]
    return s
