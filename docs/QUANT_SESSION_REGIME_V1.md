# Quant Session / Regime V1 — 2026-10-22

> Investigación offline: `observer_only=true`, `score_effect=0`,
> `strategy_validated=false`. Ningún archivo MQL, input, riesgo, SL/TP,
> scoring, umbral, sesión, dashboard ni autorización de órdenes cambia.
> Track "sesión / hora del día y régimen de volatilidad", ejecutado por un
> sub-agente Claude en sustitución de Antigravity (cuota agotada).

## 1. Reloj del servidor (paso previo)

`research/output/broker_clock_analysis.json` (generado por
`research/broker_clock_analysis.py`) declara:

- `broker_clock_status = UNKNOWN`, `broker_server_utc_offset = null`,
  `dst_behavior = UNKNOWN`, `broker_day_rollover = null`,
  `session_attribution_allowed = false`, `confidence = 0`.
- No hay ningún par servidor/UTC del mismo instante persistido
  (`TRUSTED_PAIRED_SAMPLES` vacío). Las pausas M1 no son ancla UTC.

**Conclusión: el offset UTC del servidor MT5 NO está establecido.** Por tanto
este estudio **no** asigna sesiones Asia / Londres / NY / solape. Solo analiza
la **hora de pared del servidor** (bloques de 6 h) y el día de la semana del
servidor. Cualquier lectura de "sesión" es una hipótesis, no un resultado.

Observación (no usada como ancla): las reaperturas semanales aparecen a las
22:00 de servidor en verano y 23:00 en invierno, lo que sería compatible con un
reloj cercano a UTC sin DST propio; pero eso no prueba offset ni DST y el
contrato de `broker_clock_analysis.py` lo prohíbe como evidencia.

## 2. Prerregistro (escrito y commiteado ANTES de ejecutar el OOS)

Candidatos, resultados y validación idénticos a `research/quant/edge_study.py`:

- Candidato = primera decisión `ARMED` por hora H1 y dirección con score ≥ 74.
- SL = aproximación CURRENT; TP fijo 1,25R y 2,0R; hold máximo 24 h.
- Walk-forward expansivo de 5 folds con purga y embargo de 24 h, min_train 150,
  folds construidos exactamente como en `edge_study` (etiqueta BASE).
- Evaluación en coste **STRESS**, **single slot**, OOS concatenado.

### Familia de hipótesis (filtros), definidos sobre la vela de señal

| Familia | Buckets | Definición |
|---|---|---|
| (a) Hora servidor | `HOUR_00_05`, `HOUR_06_11`, `HOUR_12_17`, `HOUR_18_23` | hora de pared del servidor de `observed_at` |
| (b1) Volatilidad | `RV_LOW`, `RV_MID`, `RV_HIGH` | terciles de `h1_rv24`; cortes solo con el train de cada fold (ambas direcciones) |
| (b2) Expansión | `VR_EXPANSION`, `VR_CONTRACTION` | `h1_vol_ratio > 1` vs `≤ 1` (umbral fijo, sin ajuste) |
| (c) Fuerza H4 | `H4S_LOW`, `H4S_MID`, `H4S_HIGH` | terciles de `abs(h4_dist_ema50_atr)`; cortes solo con train |
| (d) Día semana | `DOW_MON`, `DOW_TUE`, `DOW_WED`, `DOW_THU`, `DOW_FRI` | día del servidor; domingo (reapertura) se agrupa en `DOW_MON` |

17 buckets × 3 direcciones (`BOTH`, `LONG`, `SHORT`) × 2 TP = **102
hipótesis**. Feature ausente (`None`) ⇒ el candidato no entra en ese bucket.
El signo de tendencia H4 **no** se usa como filtro: todo SHORT tiene signo H4
< 0 y todo LONG > 0 (el EA arma con H4), así que sería redundante con la
dirección.

Benchmarks (mismo pipeline): `BASELINE`, `LONG_ONLY`, `SHORT_ONLY` por TP.

### Secuenciación single-slot (corrige R1/R4 de la revisión de Codex)

- Regla primero, slot después: cada hipótesis secuencia solo sus candidatos.
- Orden y comparación por `entry_time_ms` real (apertura del M1 de entrada),
  no por la hora de señal.
- El slot se libera al **cierre** del M1 de salida: `exit_time_ms + 60 s`
  para salidas SL/TP intrabar; las salidas TIME ya están en el cierre.
- Un candidato censurado (`INSUFFICIENT_PATH`) **reserva el slot** hasta
  `entry_time + 24 h` aunque no aporte R.

### Estadística y compuerta

- p-valor: bootstrap circular unilateral H0: E[R] ≤ 0, bloque 5 (single slot).
  IC95 con bloque 5 (tope n/10). Descriptivo "all events" con bloque 20.
- **BH-FDR q = 0,10 conjunto sobre las 102 hipótesis** (R6).
- DSR con `n_trials = 142` (102 de este estudio + 6 benchmarks + 34 ya
  declarados en `edge_study`) y `sr_variance = 1/(n−1)` de la secuencia
  evaluada, es decir la varianza de ruido de un SR por trade estimado con n
  observaciones (corrige R2: unidades consistentes con el SR por trade).
- Compuerta (misma que `edge_study` + BH): n ≥ 100, IC95 inferior > 0,
  DSR ≥ 0,95, PF ≥ 1,2, E[R] > los tres benchmarks del mismo TP y **los tres
  benchmarks presentes con valor conocido** (R7), y rechazo BH.
- Pregunta específica SHORT: se reportan todas las celdas SHORT aunque no
  pasen, con n, E[R] e IC.

Cualquier análisis añadido después de ver el OOS se etiqueta **POST-HOC**.

## 3. Resultados

(Pendiente: se completa tras ejecutar el estudio prerregistrado.)
