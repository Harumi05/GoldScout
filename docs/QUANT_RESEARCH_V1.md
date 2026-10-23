# Quant Research V1 — 2026-10-22

## Objetivo y alcance

Petición del propietario: mejorar el bot con trading cuantitativo. Conforme a
`CURRENT_TASK.md`, se implementó primero investigación/validación offline con
`observer_only=true`, `score_effect=0` y `strategy_validated=false`. No se
modificó ningún archivo MQL, input, riesgo, SL/TP, scoring, umbral, sesión,
ledger, dashboard ni autorización de órdenes. Nada del paquete es importado por
el EA ni por Hali.

Paquete `research/quant/` (solo biblioteca estándar, contrato en `CONTRACT.md`):

| Módulo | Contenido |
|---|---|
| `stats.py` | bootstrap circular por bloques, PF/expectativa, PSR/DSR, Benjamini-Hochberg, walk-forward con purga/embargo, PBO por CSCV |
| `features.py` | 22 features causales H1/H4/M15 (volatilidad realizada, TSMOM, distancia EMA/ATR, eficiencia de Kaufman, variance ratio, RSI, rango, contexto H4) con protección `LookAheadError` |
| `barriers.py` | triple barrera en M1 con fill al open siguiente, cierre en bid/ask, gaps al open, SL primero si la vela toca ambos, escenarios de coste ZERO/BASE/STRESS, multiplicador de volatilidad solo a la baja |
| `model.py` | regresión logística L2 para meta-labeling, estandarización solo con train |
| `edge_study.py` | CLI que integra todo y aplica una compuerta prerregistrada |

```powershell
python -m research.quant.edge_study `
  --input-dir "<checkout con research/output>" `
  --output-dir research/analysis/quant
```

Los outputs (`research/analysis/quant/`) están ignorados por Git.

## Datos y candidatos

- Fuente: replay histórico existente (ticks MT5 2025-05-15 → 2026-09-11) y su
  sidecar `historical_decisions.jsonl` (`PARTIAL_EXACT_CLOSED_BAR_CORE`: sin
  patrones, noticias ni trigger intrabar). No es paridad del EA.
- Candidato = primera decisión `ARMED` por hora H1 y dirección con score propio
  ≥ 74 (`threshold_trade`). 1.427 candidatos (1.030 LONG, 397 SHORT), del
  2025-07-03 al 2026-09-04.
- SL = aproximación de CURRENT (ATR14 H1 × 1,20 ó estructura de 20 velas +
  0,25 ATR, limitado a 0,8–2,0 ATR). TP fijo de 1,25R y 2,0R; tiempo máximo 24 h.
- Validación: walk-forward expansivo de 5 folds, purga por solapamiento de
  etiqueta y embargo de 24 h. "Single slot" = una posición a la vez, como el EA.

## Resultados (STRESS, single slot, OOS walk-forward)

Cifras tras las correcciones de la revisión Codex (commit posterior a `bbaed7f`):

| Regla | TP | n | E[R] | IC95 | PF | DSR |
|---|---:|---:|---:|---|---:|---:|
| BASELINE (proxy EA) | 1,25 | 194 | +0,070 | [-0,062; 0,202] | 1,15 | 0,00 |
| LONG_ONLY | 1,25 | 130 | +0,125 | [-0,033; 0,281] | 1,28 | 0,06 |
| SHORT_ONLY | 1,25 | 64 | -0,042 | [-0,238; 0,188] | 0,92 | 0,01 |
| META_TOP50 | 1,25 | 85 | +0,113 | [-0,099; 0,332] | 1,26 | 0,09 |
| META_TOP50_DIR_NEUTRAL (post-hoc) | 1,25 | 105 | +0,097 | [-0,072; 0,273] | 1,21 | 0,05 |
| VOL_TARGET_DOWN_ONLY (R escalado) | 1,25 | 194 | +0,064 | [-0,036; 0,175] | 1,19 | 0,01 |
| BASELINE (proxy EA) | 2,0 | 151 | +0,099 | [-0,068; 0,275] | 1,20 | 0,01 |
| LONG_ONLY | 2,0 | 100 | +0,180 | [-0,029; 0,397] | 1,40 | 0,13 |
| META_TOP50 | 2,0 | 79 | +0,150 | [-0,055; 0,356] | 1,33 | 0,11 |

CSCV PBO de 18 reglas simples: 0,39 (solo reglas simples, ver R6). DSR con 34
pruebas declaradas y varianza per-trade de discovery. Ninguna regla de features
sobrevive a BH-FDR global (q=0,10, 210 hipótesis) más la persistencia de signo.

**Veredicto de la compuerta: ningún candidato a PAPER.** La compuerta exige
n ≥ 100, IC95 inferior > 0, DSR ≥ 0,95, PF ≥ 1,2 y superar a BASELINE,
LONG_ONLY y SHORT_ONLY con el mismo TP/coste.

## Interpretación

1. **El proxy del EA tiene expectativa positiva pero no significativa**
   (+0,06/+0,09R con STRESS). No hay evidencia estadística de edge, pero
   tampoco de edge negativo.
2. **Todo el edge observado está en LONG; SHORT es ≈ 0 o negativo** en todas
   las variantes. El período es un fuerte rally del oro y los folds están
   dominados por una dirección (folds 0–2 = 100% LONG, fold 3 = 100% SHORT),
   así que esto puede ser régimen, no una propiedad de la estrategia.
   Contraste relevante: Hali mostró 23 de 25 cierres SHORT (PF 1,07). Es una
   hipótesis a investigar, **no** una justificación para bloquear ventas.
3. **El meta-labeling no añade valor demostrable**: su mejora es casi toda el
   filtro implícito LONG (97% de sus selecciones). Con la dirección
   neutralizada (variante post-hoc tras inspeccionar el fold 3), la mejora
   desaparece con 1,25R y es débil con 2,0R.
4. **El volatility targeting solo a la baja** reduce el drawdown máximo de
   −8,4 a −6,7 (en unidades escaladas) con una expectativa por unidad de riesgo
   similar. Es un candidato de gestión de riesgo, no de edge; sus cifras no son
   comparables directamente en R con BASELINE. No supera ningún techo de riesgo.

## Revisión independiente

Un agente revisor distinto (solo lectura) auditó el paquete y reconstruyó los
resultados:

- Sin fuga temporal: 60 candidatos reconstruidos solo con barras cerradas, 0
  diferencias. Entrada en el M1 que abre en `observed_at`.
- Purga/embargo, estandarización, cortes y vol objetivo: solo con train.
- Corregido tras la revisión: benchmarks LONG/SHORT_ONLY en la compuerta;
  bloque bootstrap 20 para eventos solapados (autocorrelación ≈ 0,75 a lag 1)
  y 5 (con tope n/10) para single slot; tiempos de salida por escenario de
  coste; censura filtrada por escenario; contrato de spread corregido.
- Pendiente/limitación: `sr_variance` del DSR viene de Sharpe semanales de
  reglas simples (unidades distintas, actualmente conservador); `n_trials` no
  incluye estudios anteriores del repositorio; el PBO solo cubre reglas
  simples. La revisión de Codex/humana sigue siendo necesaria antes del merge.

## Correcciones tras la revisión formal de Codex

`docs/REVIEW_QUANT_RESEARCH_V1_CODEX.md` (rama `codex/quant-exits-v1`) reportó
R1–R7. Corregidas por Claude (implementador distinto del revisor):

- **R1** single slot: secuencia por `entry_time_ms` real; una salida SL/TP
  intrabar mantiene el slot hasta el cierre de su vela M1.
- **R2** DSR: varianza de Sharpe **por trade** entre reglas simples single-slot,
  medida solo en el primer bloque walk-forward (nunca test), excluyendo reglas
  con < 20 trades (con 2–15 trades el ruido hacía inalcanzable cualquier
  benchmark; el filtro se añadió tras ese diagnóstico). Sin varianza → DSR
  desconocido → no pasa la compuerta.
- **R3** `value_at` rechaza `lag` negativo, booleano o no entero (`LookAheadError`).
- **R4** un candidato censurado reserva el slot durante todo su horizonte.
- **R5** si un gap cruza el vencimiento temporal, la salida es el primer open
  ejecutable posterior (SL si el open lo atraviesa; TP solo al nivel).
- **R6** BH global sobre todas las hipótesis feature × quintil × TP. El PBO
  sigue cubriendo solo reglas simples (limitación declarada).
- **R7** la compuerta exige BASELINE/LONG_ONLY/SHORT_ONLY presentes y conocidos.

El veredicto no cambia: **ningún candidato a PAPER**.

## Limitaciones

- Scores de replay parcial; no hay paridad con patrones, noticias ni intrabar.
- SL aproximado; TP fijo, no la política CURRENT/TP V2.
- Simulación con barras M1 mid: SL primero ante ambigüedad, gaps pesimistas,
  hold de 24 h en tiempo de pared (los fines de semana cierran al último bar).
- Spread histórico variable (0,30 → ~0,55); con un stop mediano de ~32 USD el
  coste es ~1–2% de R, así que STRESS no prueba colas de slippage reales.
- El 60/20/20 previo ya fue examinado; este walk-forward reutiliza el mismo
  histórico y no constituye un OOS intocado.
- Muestra single-slot de ~150–200 trades en ~14 meses: potencia baja.

## Siguiente ola propuesta (requiere aprobación)

1. **Diagnóstico SHORT** prerregistrado: por qué SHORT ≈ 0 (régimen H4, rally,
   setup), con datos posteriores a 2026-09-11 como holdout real, sin tocar el EA.
2. **Vol targeting en PAPER/shadow**: registrar el multiplicador solo a la baja
   en telemetría del Observer, `score_effect=0`, para medirlo en DEMO sin
   cambiar el lotaje.
3. Extender el replay con datos nuevos para obtener un OOS no examinado antes de
   cualquier promoción.
