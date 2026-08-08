#!/usr/bin/env python3
"""
Análisis de evolución de cuenta AFAP República.
Lee estados de cuenta en PDF y genera informe HTML con gráficas.
"""

import pymupdf
import re
import os
import json
import argparse
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
STATEMENTS_DIR = BASE_DIR / 'EstadosDeCuenta'
OUTPUT_FILE = BASE_DIR / 'informe_afap.html'
DATA_FILE = BASE_DIR / 'datos_extraidos.json'  # estructura de datos intermedia persistida

# Meses en español para etiquetas
MESES = {1: 'Ene', 2: 'Feb', 3: 'Mar', 4: 'Abr', 5: 'May', 6: 'Jun',
          7: 'Jul', 8: 'Ago', 9: 'Sep', 10: 'Oct', 11: 'Nov', 12: 'Dic'}


def parse_num(s):
    """Convierte formato español '1.234,56' a float."""
    return float(s.strip().replace('.', '').replace(',', '.'))


def fmt_money(val):
    """Formatea número como moneda uruguaya."""
    if val < 0:
        return f"-$ {abs(val):,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.')
    return f"$ {val:,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.')


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
    num_re = re.compile(r'^-?[\d.]+,\d{2}$')
    qty_re = re.compile(r'^\d+,\d{6}$')  # cantidad de cuotas (6 decimales)

    # Tuplas ancladas por la cantidad de cuotas: [cuota, saldo, UR, ...]
    qty_tuples = []
    for i, l in enumerate(lines):
        if qty_re.match(l):
            seq = []
            for k in range(i + 1, min(i + 6, len(lines))):
                if num_re.match(lines[k]):
                    seq.append(parse_num(lines[k]))
                else:
                    break
            if len(seq) >= 3 and 1100 < seq[2] < 2200:
                qty_tuples.append({'cuota': seq[0], 'saldo': seq[1], 'ur': seq[2]})

    def cuota_by_saldo(s):
        for t in qty_tuples:
            if abs(t['saldo'] - s) < 0.005:
                return t['cuota']
        return None

    def run_from(idx):
        seq = []
        for j in range(idx, min(idx + 6, len(lines))):
            if num_re.match(lines[j]):
                seq.append(parse_num(lines[j]))
            else:
                break
        return seq

    anchors = []
    for i, l in enumerate(lines):
        low = l.lower()
        if not ('saldo' in low and 'correspondiente' in low):
            continue
        m = re.search(r'al\s+(\d{2}/\d{2}/\d{4})', l)
        if not m or i + 1 >= len(lines):
            continue
        date = datetime.strptime(m.group(1), '%d/%m/%Y')
        if qty_re.match(lines[i + 1]):        # layout normal: qty, cuota, saldo, UR, ...
            seq = run_from(i + 2)
            if len(seq) >= 3 and 1100 < seq[2] < 2200:
                anchors.append({'date': date, 'cuota': seq[0], 'saldo': seq[1], 'ur': seq[2]})
        elif num_re.match(lines[i + 1]):      # layout corrido: saldo, UR, saldo_UR
            seq = run_from(i + 1)
            if len(seq) >= 2 and 1100 < seq[1] < 2200:
                cuota = cuota_by_saldo(seq[0])
                if cuota is not None:
                    anchors.append({'date': date, 'cuota': cuota, 'saldo': seq[0], 'ur': seq[1]})
    return anchors


def extract_pdf_data(filepath):
    """Extrae datos de transacciones y saldos de un PDF de estado de cuenta."""
    doc = pymupdf.open(str(filepath))
    full_text = ''
    for page in doc:
        full_text += page.get_text() + '\n'

    lines = [l.strip() for l in full_text.split('\n') if l.strip()]

    # Extraer fechas del periodo
    periods = re.findall(r'(\d{2}/\d{2}/\d{4})\s*-\s*(\d{2}/\d{2}/\d{4})', full_text)
    parsed = [(datetime.strptime(s, '%d/%m/%Y'), datetime.strptime(e, '%d/%m/%Y'))
              for s, e in periods]
    period_start, period_end = min(parsed, key=lambda x: x[0])

    # Patrón para números en formato español
    num_re = re.compile(r'^-?[\d.]+,\d{2}$')

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
            if num_re.match(lines[j]):
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

    # Extraer saldo y UR del cierre usando tripletas (saldo, UR, cuotas)
    # El UR de Uruguay está en el rango ~1100-2200 para 2020-2026
    triplets = []
    for i in range(len(lines) - 2):
        if num_re.match(lines[i]) and num_re.match(lines[i+1]) and num_re.match(lines[i+2]):
            v = [parse_num(lines[k]) for k in range(i, i + 3)]
            if 1100 < v[1] < 2200 and v[0] >= 0 and v[2] >= 0:
                triplets.append(v)

    closing_saldo = triplets[-1][0] if triplets else 0.0
    ur_value = triplets[-1][1] if triplets else 0.0

    # Calcular saldo de apertura a partir de transacciones (más preciso que triplets)
    opening_saldo = closing_saldo - (aportes + comision_admin + seguro + custodia + rentabilidad)

    # UR de apertura: buscar la línea "Saldo" con la fecha más temprana (distinta al cierre)
    opening_ur = 0.0
    period_end_str = period_end.strftime('%d/%m/%Y')
    opening_candidates = []
    for i, line in enumerate(lines):
        if 'saldo' in line.lower() and 'correspondiente' in line.lower():
            date_m = re.search(r'al\s+(\d{2}/\d{2}/\d{4})', line)
            if date_m and date_m.group(1).strip() != period_end_str.strip():
                dt = datetime.strptime(date_m.group(1).strip(), '%d/%m/%Y')
                # Buscar UR en 5 números antes de esta línea
                ur_val = None
                nums = []
                for j in range(i - 1, max(i - 6, -1), -1):
                    if num_re.match(lines[j]):
                        nums.insert(0, parse_num(lines[j]))
                    else:
                        break
                # En la secuencia de 4-5 números, el UR siempre es el penúltimo
                if len(nums) >= 3:
                    ur_val = nums[-2]
                if ur_val is None:
                    nums = []
                    for j in range(i + 1, min(i + 6, len(lines))):
                        if num_re.match(lines[j]):
                            nums.append(parse_num(lines[j]))
                        else:
                            break
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

    return {
        'file': filepath.name,
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
        'anchors': extract_balance_anchors(lines),
    }


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
    Reemplaza cada registro vacío por uno estimado, usando el saldo de apertura del
    periodo siguiente (cierre real del faltante) y las proporciones de ese periodo
    para desglosar aportes, deducciones y rentabilidad. No altera los registros con
    datos reales.
    """
    filled = list(statements)
    for i, p in enumerate(filled):
        if not p.get('empty'):
            continue
        if i == 0 or i + 1 >= len(filled):
            continue  # no hay vecinos para estimar (no debería ocurrir en huecos internos)

        current = filled[i - 1]   # periodo anterior (con datos)
        next_p = filled[i + 1]    # periodo siguiente (con datos)

        # El saldo de apertura del siguiente periodo es el cierre del faltante
        missing_saldo = next_p['opening_saldo']
        missing_ur = next_p['opening_ur']

        # Estimar transacciones por diferencia
        delta = missing_saldo - current['saldo']
        # Usar proporciones del periodo siguiente para estimar breakdown
        next_total_mov = next_p['aportes'] + next_p['deducciones'] + next_p['rentabilidad']
        if abs(next_total_mov) > 0:
            ratio_aportes = next_p['aportes'] / next_total_mov
            ratio_deduc = next_p['deducciones'] / next_total_mov
            ratio_rent = next_p['rentabilidad'] / next_total_mov
        else:
            ratio_aportes = 0.6
            ratio_deduc = -0.1
            ratio_rent = 0.5

        est_aportes = abs(delta * ratio_aportes)
        est_deduc = delta * ratio_deduc
        est_rent = delta * ratio_rent

        filled[i] = {
            'file': '(estimado)',
            'period_start': p['period_start'],
            'period_end': p['period_end'],
            'aportes': est_aportes,
            'comision_admin': est_deduc * 0.3,
            'seguro': est_deduc * 0.6,
            'custodia': est_deduc * 0.1,
            'deducciones': est_deduc,
            'rentabilidad': est_rent,
            'saldo': missing_saldo,
            'ur': missing_ur,
            'opening_saldo': current['saldo'],
            'opening_ur': current['ur'],
            'estimated': True,
        }

    return filled


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


def generate_html(analysis, inflation_adjusted, inflation_adjusted_bruto, fund_real_returns):
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


# ---------------------------------------------------------------------------
# PERSISTENCIA de la estructura de datos intermedia
# ---------------------------------------------------------------------------

_DATE_FIELDS = ('period_start', 'period_end')


def _statement_to_json(s):
    """Convierte un registro a una forma serializable en JSON (fechas -> ISO)."""
    d = dict(s)
    for k in _DATE_FIELDS:
        if isinstance(d.get(k), datetime):
            d[k] = d[k].isoformat()
    if d.get('anchors'):
        d['anchors'] = [{**a, 'date': a['date'].isoformat()} for a in d['anchors']]
    return d


def _statement_from_json(d):
    """Reconstruye un registro desde JSON (ISO -> fechas)."""
    s = dict(d)
    for k in _DATE_FIELDS:
        if s.get(k):
            s[k] = datetime.fromisoformat(s[k])
    if s.get('anchors'):
        s['anchors'] = [{**a, 'date': datetime.fromisoformat(a['date'])} for a in s['anchors']]
    return s


def save_statements(statements, path):
    """Persiste la estructura de datos intermedia en un archivo JSON."""
    payload = [_statement_to_json(s) for s in statements]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


def load_statements(path):
    """Carga la estructura de datos intermedia desde un archivo JSON."""
    payload = json.loads(path.read_text(encoding='utf-8'))
    return [_statement_from_json(d) for d in payload]


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
            data = extract_pdf_data(pdf)
            statements.append(data)
            print(f"    Periodo: {data['period_start'].strftime('%d/%m/%Y')} - "
                  f"{data['period_end'].strftime('%d/%m/%Y')}")
            print(f"    Aportes: {fmt_money(data['aportes'])} | "
                  f"Deducciones: {fmt_money(abs(data['deducciones']))} | "
                  f"Rentabilidad: {fmt_money(data['rentabilidad'])} | "
                  f"Saldo: {fmt_money(data['saldo'])}")
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
    A partir de la estructura de datos intermedia (`statements`) construye el
    informe HTML actual: completa periodos faltantes, arma el análisis acumulado,
    calcula los aportes ajustados por inflación y la rentabilidad real del fondo,
    y escribe el HTML en `output_file`. Devuelve el análisis por periodo.
    """
    # Completar periodos faltantes (estimación de las entradas vacías)
    periods_data = estimate_empty_periods(statements)

    # Construir análisis acumulado
    analysis = build_analysis(periods_data)

    # Aportes ajustados por inflación (netos y brutos)
    inflation_adjusted, inflation_adjusted_bruto = compute_inflation_adjusted_contributions(periods_data)

    # Rendimiento del fondo por periodo (nominal / inflación / real, en %)
    anchor_map = build_fund_anchor_map(periods_data)
    fund_real_returns = compute_fund_real_returns(analysis, anchor_map)

    # Generar HTML
    print("\nGenerando informe HTML...")
    html = generate_html(analysis, inflation_adjusted, inflation_adjusted_bruto, fund_real_returns)

    output_file.write_text(html, encoding='utf-8')
    print(f"Informe generado: {output_file}")
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
