# Quant Short Diagnostic V1 — 2026-10-22

## Prerregistro (escrito antes de calcular cualquier resultado OOS o por bucket)

Hasta aquí solo se conocía el resumen agregado del edge study
(`SHORT_ONLY` STRESS single-slot OOS E[R] ≈ −0,03 con TP 1,25R). Las reglas
siguientes se fijan ANTES de mirar resultados SHORT condicionados. Son
filtros sin entrenamiento sobre candidatos SHORT; un feature ausente (`None`)
excluye el candidato (nunca se imputa).

| Regla | Condición (además de `direction = SHORT`) | Hipótesis |
|---|---|---|
| S1 `SHORT_H4_BEAR` | `h4_trend_sign < 0` (EMA50 < EMA200 en H4) | vender solo con la tendencia de varias semanas |
| S2 `SHORT_TREND_ALIGNED` | `h4_trend_sign < 0` y `h1_tsmom_120 < 0` | tendencia H4 y momentum H1 de ~5 días alineados |
| S3 `SHORT_NOT_EXHAUSTED` | `h1_rsi14 >= 30` y `h1_dist_ema20_atr >= -1.5` | no vender en sobreventa / extensión |
| S4 `SHORT_ALIGNED_NOT_EXHAUSTED` | S2 y S3 | combinación |

Evaluación: mismos folds walk-forward purgados que `edge_study` (5 folds,
embargo 24 h, `min_train=150`, sobre todos los candidatos), solo los bloques
de test. Compuerta compartida: STRESS single-slot n ≥ 100, IC95 inferior > 0,
DSR ≥ 0,95, PF ≥ 1,2 y E[R] superior a BASELINE/LONG_ONLY/SHORT_ONLY.
Bootstrap circular por bloques: 5 single-slot, 20 eventos solapados.
`n_trials` del DSR = 34 (edge study) + 4 reglas × 2 TP = 42. BH-FDR (q=0,10)
sobre las 8 pruebas regla×TP y, aparte, sobre todos los buckets descriptivos.
Si n < 100 el veredicto es `INSUFFICIENT_SAMPLE`, sin excepción.

## Implementación

`research/quant/short_diagnostic.py` (solo stdlib, `observer_only=true`,
`score_effect=0`, `strategy_validated=false`). Reutiliza `edge_study`
(`Candidate`, `non_overlapping`, `summarize`, `verdict`), `barriers`,
`features` y `stats`; no modifica ningún módulo existente ni el EA.

```powershell
python -m research.quant.short_diagnostic `
  --input-dir "<checkout con research/output>" `
  --analysis-dir research/analysis/quant --output-dir research/analysis/quant
```

Lee `quant_candidates.csv` + `quant_edge_study.json` (para `sr_variance`) y
re-etiqueta cada candidato en M1 con `barriers.triple_barrier` para obtener
motivo de salida, MFE/MAE y tiempo de salida. Paridad con el CSV: **0
diferencias** de R en 1.427 × 4 etiquetas. Salidas (ignoradas por Git):
`research/analysis/quant/short_diagnostic.{json,md}`.

Añadidos descriptivos (NO prerregistrados, no son reglas): retorno causal de
480 barras H1 (~1 mes) para el contexto "rally de varios meses"
(`h1_tsmom_120` cubre solo ~5 días de trading, no 30); deriva bruta a 1/4/24 h;
deriva incondicional del mercado por trimestre; stop 1,5× como sensibilidad.

## Hallazgo estructural previo

**Todos los SHORT (397/397) tienen `h4_trend_sign < 0` y todos los LONG
(1.030/1.030) `> 0`.** El replay del EA ya solo arma a favor de la tendencia H4
(EMA50/EMA200). Por tanto S1 es idéntica a `SHORT_ONLY` y no aporta
información, y la hipótesis "los SHORT pierden por ir contra la tendencia H4"
queda descartada por construcción.

## 1. Anatomía del resultado (muestra completa, todos los eventos, in-sample)

STRESS (BASE en el JSON, mismas conclusiones):

| Dir | TP | n | SL / TP / TIME % | E[R] | MFE q50 | MAE q50 | SL con MFE ≥ 1R | h hasta SL q50 | SL ≤ 2 h | SL prematuro* | E[R] stop 1,5× |
|---|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|
| SHORT | 1,25 | 397 | 53 / 31 / 16 | −0,166 | 0,77 | −1,00 | 9% | 6,5 | 12% | 7,6% | −0,106 |
| LONG | 1,25 | 1.030 | 42 / 42 / 17 | +0,089 | 1,02 | −0,79 | 7% | 5,5 | 20% | 16,8% | +0,092 |
| SHORT | 2,0 | 397 | 55 / 21 / 24 | −0,095 | 0,77 | −1,00 | 12% | 6,5 | 12% | 7,3% | −0,109 |
| LONG | 2,0 | 1.030 | 46 / 21 / 32 | +0,087 | 1,02 | −0,90 | 16% | 6,2 | 18% | 17,6% | +0,121 |

\* Fracción de SL tras los cuales el precio alcanzó +1R en la dirección del
trade (medido desde el fill) antes de completar 24 h desde la entrada
(n = 211 SL SHORT y 429 SL LONG con TP 1,25).

Deriva bruta alineada con la dirección (mid a mid, en R del stop):

| Dir | n | 1 h | 4 h | 24 h | % positivo a 24 h |
|---|---:|---:|---:|---:|---:|
| SHORT | 397 | −0,02 | −0,09 | **−0,39** | 38% |
| LONG | 1.030 | +0,01 | +0,07 | +0,33 | 59% |

Lectura:

- **No es el stop.** Los SL SHORT son *menos* prematuros que los LONG (7% vs
  17%), tardan más (mediana 6,5 h; solo 12% en ≤ 2 h, no son picos de ruido) y
  un stop 1,5× sigue negativo (−0,11R).
- **No es el target.** Solo 9–12% de los SL SHORT estuvieron alguna vez a +1R;
  la MFE mediana (0,77R) no llega a 1,25R. Bajar el target no lo arregla.
- **Es la dirección/timing de la entrada.** A 24 h el precio está *por encima*
  de la entrada en el 62% de los SHORT (−0,39R de media). El movimiento adverso
  es lento y sostenido.

## 2. Contexto (muestra completa, todos los eventos, descriptivo)

BH-FDR q=0,10 sobre las 120 filas de buckets (5 familias × dirección × coste ×
TP). STRESS, TP 1,25:

| Familia | Bucket SHORT | n | E[R] | IC95 | BH |
|---|---|---:|---:|---|---|
| H4 × mom 120 | h4− / mom120− | 383 | −0,198 | [−0,44; 0,07] | no |
| H4 × mom 120 | h4− / mom120+ | 14 | +0,708 | [0,26; 1,08] | **sí** |
| Tendencia H1 480 | baja (~1 mes) | 294 | −0,231 | [−0,51; 0,08] | no |
| Tendencia H1 480 | alza (contra el rally) | 103 | +0,021 | [−0,37; 0,42] | no |
| RSI14 H1 | < 30 | 34 | −0,096 | [−0,51; 0,36] | no |
| RSI14 H1 | 30–40 | 145 | −0,183 | [−0,51; 0,19] | no |
| RSI14 H1 | 40–50 | 206 | −0,196 | [−0,50; 0,15] | no |
| RSI14 H1 | 50–60 | 12 | +0,365 | [−0,12; 0,82] | no |
| Dist. EMA20 (ATR) | < −1,5 | 73 | −0,208 | [−0,55; 0,12] | no |
| Dist. EMA20 (ATR) | −1,5..−0,5 | 189 | −0,197 | [−0,44; 0,07] | no |
| Dist. EMA20 (ATR) | −0,5..0,5 | 128 | −0,124 | [−0,45; 0,23] | no |
| Score | 74–79 | 266 | −0,220 | [−0,46; 0,03] | no |
| Score | 80–85 | 111 | −0,061 | [−0,35; 0,26] | no |
| Score | ≥ 86 | 20 | −0,027 | [−0,50; 0,43] | no |

Lectura:

- **La hipótesis "SHORT contra el rally de varios meses" no se sostiene:** los
  SHORT *alineados* con la caída del último mes son los peores (−0,23R); los que
  van contra el mes alcista están en ≈ 0. Coherente con Hali: 6 pérdidas SHORT
  con etiqueta H1/H4 BAJISTA.
- **"Vender en agotamiento" (RSI < 30) no explica el problema:** n=34, no peor
  que 30–50. La extensión bajo la EMA20 tampoco muestra gradiente.
- El único bucket SHORT que sobrevive a BH es `h4− / mom120+` (vender un rebote
  de ~5 días dentro de tendencia H4 bajista) con **n=14**: post-hoc, minúsculo y
  no prerregistrado. Es una hipótesis para datos futuros, no una regla.
- Score: los SHORT de score 74–79 son los peores; no significativo.
- De paso, en LONG: `ret480_down` (n=48, −0,52R) también sobrevive a BH; los
  LONG contra la tendencia del mes fallan igual. El patrón común es entrar
  después de un movimiento ya hecho, no la dirección en sí.

## 3. Reglas prerregistradas: walk-forward OOS, single slot

Mismos bloques de test que `edge_study` (reproduce `SHORT_ONLY` STRESS 1,25:
n=65, −0,033). `n_trials`=42.

| Regla | TP | n STRESS | E[R] STRESS | IC95 | PF | DSR | > SHORT_ONLY | BH | Estado |
|---|---:|---:|---:|---|---:|---:|---|---|---|
| SHORT_ONLY (benchmark) | 1,25 | 65 | −0,033 | [−0,24; 0,19] | 0,93 | 0,01 | — | — | — |
| S1 SHORT_H4_BEAR | 1,25 | 65 | −0,033 | [−0,24; 0,19] | 0,93 | 0,01 | no (= benchmark) | no | INSUFFICIENT_SAMPLE |
| S2 SHORT_TREND_ALIGNED | 1,25 | 62 | +0,014 | [−0,19; 0,21] | 1,03 | 0,02 | sí | no | INSUFFICIENT_SAMPLE |
| S3 SHORT_NOT_EXHAUSTED | 1,25 | 50 | −0,019 | [−0,26; 0,23] | 0,96 | 0,02 | sí | no | INSUFFICIENT_SAMPLE |
| S4 ALIGNED_NOT_EXHAUSTED | 1,25 | 47 | +0,022 | [−0,21; 0,28] | 1,05 | 0,05 | sí | no | INSUFFICIENT_SAMPLE |
| SHORT_ONLY (benchmark) | 2,0 | 51 | −0,024 | [−0,28; 0,24] | 0,96 | 0,02 | — | — | — |
| S1 SHORT_H4_BEAR | 2,0 | 51 | −0,024 | [−0,28; 0,24] | 0,96 | 0,02 | no (= benchmark) | no | INSUFFICIENT_SAMPLE |
| S2 SHORT_TREND_ALIGNED | 2,0 | 48 | +0,036 | [−0,23; 0,33] | 1,07 | 0,05 | sí | no | INSUFFICIENT_SAMPLE |
| S3 SHORT_NOT_EXHAUSTED | 2,0 | 46 | −0,097 | [−0,36; 0,16] | 0,82 | 0,01 | no | no | INSUFFICIENT_SAMPLE |
| S4 ALIGNED_NOT_EXHAUSTED | 2,0 | 43 | −0,053 | [−0,32; 0,21] | 0,90 | 0,02 | no | no | INSUFFICIENT_SAMPLE |

Ninguna regla llega a n ≥ 100 ni tiene IC95 inferior > 0. Ninguna sobrevive a
BH ni supera a LONG_ONLY/BASELINE. Las diferencias frente a SHORT_ONLY (±0,05R)
están muy dentro del ruido. **Veredicto: ningún candidato SHORT a PAPER.** En
OOS, S2 (mes bajista) queda levemente por encima de SHORT_ONLY, mientras que en
la muestra completa el mes bajista es peor: esa inconsistencia es otra señal de
que no hay un efecto estable.

## 4. Estabilidad temporal (STRESS, TP 1,25)

| Trimestre | SHORT n | SHORT E[R] | SHORT slot n / E[R] | LONG n | LONG E[R] | Deriva 24 h mercado (USD) | Deriva 24 h SHORT alineada (USD) |
|---|---:|---:|---|---:|---:|---:|---:|
| 2025Q3 | 45 | −0,845 | 7 / −0,57 | 304 | +0,137 | +8,46 | −31,48 |
| 2025Q4 | 0 | — | — | 371 | −0,023 | +8,04 | — |
| 2026Q1 | 44 | +0,016 | 10 / −0,14 | 231 | +0,276 | +6,27 | +21,10 |
| 2026Q2 | 245 | −0,048 | 42 / +0,03 | 0 | — | **−11,73** | **−8,11** |
| 2026Q3 | 63 | −0,268 | 13 / −0,14 | 124 | −0,039 | +6,52 | −18,91 |

- SHORT es negativo en 3 de los 4 trimestres en que aparece. El 62% de los
  SHORT cae en 2026Q2, el único trimestre bajista (y el fold 3 del edge study).
- **Dato clave:** en 2026Q2 el mercado bajó de media 11,7 USD en 24 h (vender a
  ciegas habría ganado), pero los SHORT armados perdieron 8,1 USD alineados.
  Los SHORT rinden peor que la deriva incondicional incluso en régimen bajista:
  el problema es el **timing** (se arma tras la caída y el precio rebota), no
  solo el rally alcista.
- Los LONG tampoco baten de forma consistente la deriva del mercado (2025Q4:
  −3,2 vs +8,0 USD), así que parte del "edge LONG" es la deriva del rally.

## Causal vs descriptivo

- **Causal / OOS** (features as-of con barras cerradas, folds purgados): solo la
  sección 3. Resultado: sin evidencia.
- **Descriptivo / in-sample**: anatomía, buckets, trimestres y deriva. Usan el
  futuro de cada trade por definición (MFE, SL prematuro, deriva) y no pueden
  convertirse en reglas sin un nuevo prerregistro y datos nuevos.
- El bucket `h4−/mom120+` (n=14) y la tendencia de 480 barras son post-hoc.

## Conclusión

SHORT no funciona en este proxy porque **la entrada llega tarde**: el EA arma
SHORT solo con H4 bajista y normalmente después de una caída de varios días, y
el precio tiende a revertir en las 24 h siguientes (deriva −0,39R; 62% por
encima de la entrada). No es un problema de anchura de stop (pocos SL
prematuros; un stop 1,5× sigue negativo) ni de target (MFE mediana 0,77R).
Ninguna de las 4 reglas prerregistradas tiene muestra suficiente ni efecto.

**Recomendación (no cambia el EA):** marcar la rama SHORT como **riesgo no
validado** y **monitorizar SHORT en la telemetría DEMO** (registrar por trade
SHORT: retorno H1 de ~480 barras, `h1_tsmom_120`, RSI, distancia EMA20, MFE/MAE
y motivo de salida) para reevaluar con datos posteriores a 2026-09-11 como
holdout real. **Sin candidato PAPER.** Esto no justifica bloquear ventas: con
n≈65 OOS tampoco hay evidencia de edge negativo significativo (IC95
[−0,24; 0,19]). Esa decisión corresponde al propietario.

## Limitaciones

- Las mismas del edge study: scores de replay parcial (sin patrones, noticias
  ni intrabar), SL aproximado, TP fijo, M1 mid con SL primero ante ambigüedad,
  costes STRESS que no prueban colas de slippage.
- Solo 397 SHORT, concentrados en un trimestre (2026Q2); el OOS single-slot
  SHORT es de 43–65 trades: potencia muy baja, INSUFFICIENT_SAMPLE en todo.
- Los SHORT en vivo de Hali (23/25, PF 1,07) no entran en este cálculo; la
  diferencia de frecuencia de SHORT entre replay y vivo no se explica aquí.
- El histórico ya fue examinado por estudios previos; no es un OOS intocado.
- Los timestamps de servidor MT5 se tratan como UTC para asignar trimestres.
- La deriva de mercado por trimestre usa cierres H1 en USD, no en R; es una
  comparación de orden de magnitud.
