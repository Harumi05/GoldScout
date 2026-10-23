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

```powershell
python -m research.quant.session_regime_study `
  --input-dir "<checkout con research/output>" `
  --output-dir research/analysis/session_regime
```

Outputs (ignorados por Git): `session_regime_study.{json,md}` y
`session_regime_hypotheses.csv`. 1.427 candidatos (1.030 LONG / 397 SHORT),
todos con resultado OK (0 censurados). Folds idénticos a `edge_study`: los
tests 0–2 son 100% LONG, el 3 es 100% SHORT y el 4 un 52% LONG, así que **todo
el OOS SHORT procede de los dos últimos folds**.

### Benchmarks (STRESS, single slot, OOS)

Con la secuenciación corregida (entrada real, liberación al cierre del M1)
los benchmarks reproducen `edge_study` (1,25R: 195 vs 196 trades).

| Regla | TP | n | E[R] | IC95 | PF | DSR |
|---|---:|---:|---:|---|---:|---:|
| BASELINE | 1,25 | 195 | +0,067 | [−0,066; 0,197] | 1,15 | 0,04 |
| LONG_ONLY | 1,25 | 131 | +0,109 | [−0,051; 0,270] | 1,25 | 0,07 |
| SHORT_ONLY | 1,25 | 64 | −0,017 | [−0,217; 0,200] | 0,96 | 0,00 |
| BASELINE | 2,0 | 152 | +0,093 | [−0,076; 0,258] | 1,19 | 0,04 |
| LONG_ONLY | 2,0 | 101 | +0,151 | [−0,064; 0,371] | 1,33 | 0,08 |
| SHORT_ONLY | 2,0 | 51 | −0,024 | [−0,275; 0,241] | 0,96 | 0,00 |

### Hipótesis más destacadas (de 102; tabla completa en el CSV)

| Hipótesis | n | E[R] | IC95 | p | PF | DSR |
|---|---:|---:|---|---:|---:|---:|
| tp1,25 DOW_MON LONG | 33 | +0,380 | [0,010; 0,723] | 0,015 | 2,16 | 0,30 |
| tp1,25 HOUR_18_23 LONG | 62 | +0,233 | [0,027; 0,455] | 0,019 | 1,60 | 0,19 |
| tp2,0 DOW_MON LONG | 26 | +0,458 | [−0,037; 0,941] | 0,038 | 2,41 | 0,21 |
| tp1,25 HOUR_12_17 BOTH (n≥100) | 120 | +0,001 | [−0,158; 0,165] | 0,50 | 1,00 | 0,00 |
| tp1,25 RV_HIGH BOTH (n≥100) | 139 | +0,030 | [−0,113; 0,176] | 0,32 | 1,06 | 0,01 |
| tp1,25 VR_EXPANSION BOTH (n≥100) | 101 | +0,099 | [−0,064; 0,274] | 0,14 | 1,21 | 0,04 |
| tp1,25 VR_CONTRACTION BOTH (n≥100) | 112 | +0,016 | [−0,150; 0,185] | 0,43 | 1,03 | 0,01 |

El p mínimo (0,015) está muy por encima del umbral BH del rango 1
(0,10/102 ≈ 0,001). Solo 6 de 102 celdas alcanzan n ≥ 100 y ninguna supera
a LONG_ONLY con IC inferior > 0.

### SHORT por régimen (STRESS, OOS)

| Bucket SHORT | TP | single slot n | E[R] | IC95 | todos los eventos n / E[R] |
|---|---:|---:|---:|---|---|
| HOUR_12_17 | 1,25 | 40 | +0,091 | [−0,193; 0,399] | 101 / +0,140 |
| HOUR_12_17 | 2,0 | 34 | +0,169 | [−0,232; 0,604] | 101 / +0,150 |
| H4S_LOW (H4 poco extendido) | 1,25 | 33 | +0,087 | [−0,224; 0,384] | 174 / +0,129 |
| H4S_LOW | 2,0 | 29 | −0,020 | [−0,383; 0,379] | 174 / +0,208 |
| H4S_MID | 1,25 | 26 | −0,080 | [−0,454; 0,325] | 113 / −0,291 |
| H4S_HIGH | 1,25 | 13 | +0,025 | [−0,505; 0,574] | 65 / −0,269 |
| RV_LOW | 1,25 | 9 | +0,244 | [−0,371; 0,880] | 30 / +0,362 |
| HOUR_00_05 | 1,25 | 28 | −0,194 | [−0,528; 0,162] | 88 / −0,255 |
| HOUR_18_23 | 1,25 | 25 | −0,230 | [−0,527; 0,061] | 71 / −0,201 |
| DOW_FRI | 1,25 | 15 | −0,322 | [−0,705; 0,100] | 48 / −0,438 |

Ninguna celda SHORT tiene IC inferior > 0 ni n suficiente. Patrones con signo
coherente en ambos TP (solo **hipótesis**, sin significación):

- SHORT en horas de servidor 12–17 es positivo en single slot y en todos los
  eventos; SHORT en 00–05 y 18–23 es negativo.
- SHORT con H4 poco extendido (`H4S_LOW`) es positivo en todos los eventos;
  con H4 medio/alto extendido es claramente negativo. Esto es coherente con el
  diagnóstico previo de "entrada tardía" (el precio rebota en < 24 h cuando la
  bajada H4 ya está extendida), pero el n single-slot es ~30.

## 4. Veredicto

**Ninguna hipótesis pasa la compuerta. 0 rechazos BH-FDR. Ningún candidato a
PAPER.** No hay evidencia de que un filtro de hora de servidor, régimen de
volatilidad, expansión/contracción, fuerza H4 o día de la semana mejore el
proxy del EA en OOS. Tampoco hay un régimen donde SHORT sea demostrablemente
positivo.

## 5. Limitaciones

- Offset UTC del servidor desconocido: no hay sesiones nombradas; si el
  servidor sigue DST, los bloques horarios se desplazan 1 h en parte del año.
- Potencia muy baja: dividir ~195 trades single-slot en 17 buckets × 3
  direcciones deja 8–140 trades por celda; con n_trials = 142 el SR máximo esperado
  por azar es ≈ 2,65/√(n−1) y DSR ≥ 0,95 exige un SR por trade de
  aproximadamente (2,65 + 1,645)/√(n−1) (≈ 0,43 con n = 100), no alcanzado: el SR
  máximo observado es 0,37 con n = 26 (umbral ≈ 0,86 para ese n).
- La volatilidad realizada sube durante la muestra: los terciles de train
  quedan por debajo del nivel del test y `RV_HIGH` concentra ~70% del OOS
  (821 de ~1.190 eventos). Los terciles train-only son causales pero no
  estacionarios.
- El OOS SHORT procede solo de los folds 3–4 (régimen concreto); la
  comparación por dirección está confundida con el período.
- Mismos datos que `edge_study` y el 60/20/20 previo: no es un OOS intocado.
  Mismas aproximaciones de SL, TP fijo, M1 mid y costes STRESS.
- `n_trials` acumula los 34 de `edge_study` pero no otros estudios del
  repositorio.

## 6. Siguiente paso propuesto (requiere aprobación)

Prerregistrar, para datos posteriores a 2026-09-11 (holdout real), solo dos
hipótesis SHORT: (1) SHORT con `|h4_dist_ema50_atr|` en el tercil bajo de
train y (2) SHORT en horas de servidor 12–17, con la misma compuerta y sin
tocar el EA. Persistir pares servidor/UTC (`TimeTradeServer`/`TimeGMT`) para
poder establecer el offset antes de cualquier análisis de sesiones.
