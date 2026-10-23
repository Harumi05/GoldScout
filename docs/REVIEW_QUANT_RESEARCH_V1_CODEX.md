# Revisión formal de Quant Research V1 — Codex, 2026-10-22

## Alcance y veredicto

Revisión exacta de `3059016..ae9fce2eac4aead405130a542b53cc2b92c897ae`:
`research/quant/`, sus tests y documentación. No se modificaron los archivos
revisados. Se conserva la conclusión **ningún candidato a PAPER**; los resultados
son investigación descriptiva, no una validación para promoción. Corregir R1/R2
antes de usar esta infraestructura para afirmar que una política supera el gate.

El árbol estaba limpio en `codex/quant-exits-v1`, HEAD ae9fce2. No se hizo pull:
el encargo fija un commit exacto y una rama coordinada; actualizarla cambiaría
el objeto de revisión. No se tocó otra rama/worktree ni se hizo push/merge.

## Hallazgos

### R1 — Alta: el single-slot conoce una salida intrabar antes de que ocurra

- **Ubicación:** `research/quant/barriers.py:349`,
  `research/quant/edge_study.py:170–175`.
- **Evidencia:** una operación entra en t=0; TP se toca dentro del M1 que abre
  en t=60.000. `exit_time_ms=60000` aunque OHLC solo prueba que se tocó antes de
  t=120.000. Un segundo candidato en t=60.000 entra en ese mismo open;
  `non_overlapping` devuelve ambos IDs. Se reprodujo con dos velas sintéticas
  (100/100,1/99,9/100 y 100/102,1/99,9/102), stop=1, TP=2, coste ZERO.
- **Impacto:** usa información futura del minuto para liberar posición. Puede
  alterar n, secuencia, drawdown y expectativa single-slot. También compara
  signal time, no entry time, cuando la primera vela disponible está demorada.
- **Fix propuesto:** distinguir fills conocidos al open de fills intrabar;
  reservar los segundos hasta cierre M1 y usar entry time real al secuenciar.
  Añadir test de colisión de candidato en el open del minuto de salida.

### R2 — Alta: el DSR del gate no tiene una estimación coherente de varianza

- **Ubicación:** `research/quant/edge_study.py:335–339`, `:143–144`, `:435–436`;
  contrato estadístico en `research/quant/stats.py:257–264`.
- **Evidencia:** `rule_matrix` produce sumas **semanales** de R de eventos
  solapados; además elimina semanas con resultado exactamente cero antes de
  calcular cada SR. Su varianza transversal se usa luego contra SR **por
  operación**, incluso en carteras single-slot. La matriz emplea todo el
  histórico, incluyendo los bloques que se presentan como OOS.
- **Impacto:** DSR no calibrado y estimación de penalización informada por OOS.
  Que en esta muestra sea conservador no garantiza la misma dirección del
  error en otra. El bootstrap por bloques no corrige el PSR/DSR iid.
- **Fix propuesto:** misma unidad de observación para SR y su varianza,
  estimación congelada solo en discovery/train, ceros incluidos cuando son
  períodos de calendario; declarar y tratar la dependencia temporal. Recalcular
  el gate después. La documentación ya reconoce parte del problema.

### R3 — Media: `value_at(lag=-1)` permite leer una vela futura

- **Ubicación:** `research/quant/features.py:340–345`.
- **Evidencia:** con closes 100, 101, 102 y una única barra cerrada en t=60.000,
  `value_at('close', 60000)` devuelve 100, mientras que `lag=-1` devuelve 101
  sin `LookAheadError`. La comprobación causal ocurre antes de restar `lag`.
- **Impacto:** incumple el contrato público de causalidad; no se observó este
  acceso en `build_features` ni en el CLI actual. No demuestra fuga en los
  resultados publicados de features.
- **Fix propuesto:** exigir lag entero no negativo y validar el índice final;
  test explícito de lag negativo, booleano y no entero.

### R4 — Media: eliminar censurados antes del slot condiciona admisión al futuro

- **Ubicación:** `research/quant/edge_study.py:182–186`, `:170–175`, `:374–377`.
- **Evidencia:** un primer candidato `INSUFFICIENT_PATH` no reserva slot porque
  `exit_time()` devuelve None; el siguiente candidato es admitido aunque caiga
  dentro de las 24 h reservadas por la primera entrada. Reproducción sintética:
  IDs a en t=0 censurado y b en t=60.000; devuelve únicamente b.
- **Impacto:** sesgo por disponibilidad de etiquetas y fronteras/folds que
  dependen de desenlaces. Principalmente relevante al final del archivo o con
  datos incompletos; no se cuantifica aquí su efecto sobre el informe V1.
- **Fix propuesto:** congelar universo/fronteras con candidatos causales,
  secuenciar entradas antes de filtrar resultados y conservar reservas de
  operaciones censuradas. Informar cobertura por política/coste.

### R5 — Media: el horizonte temporal puede cerrar retrospectivamente en un gap

- **Ubicación:** `research/quant/barriers.py:250–251`, `:319–330`.
- **Evidencia:** M1 en t=0 y t=3 h, horizonte 1 h, sin toque de barreras:
  devuelve `OK`, `TIME`, salida en t=60.000. La barra de 3 h basta para marcar
  cobertura pese a que no existe precio ejecutable al vencimiento.
- **Impacto:** precio/hora de cierre anteriores a una decisión de expiración,
  exposición artificialmente corta y riesgo de gap omitido. Fin de semana y
  datos perdidos no se distinguen. Está documentado como limitación en V1,
  pero no es una convención conservadora universal.
- **Fix propuesto:** elegir previamente censura por cobertura insuficiente o
  salida al primer open ejecutable posterior al plazo; informar gaps y probar
  ambos sentidos con open adverso. Exits V1 conserva la convención solicitada,
  expone `horizon_gap_ms` y no la interpreta como paridad ejecutable.

### R6 — Media: multiplicidad/PBO cubren una familia menor que la exploración

- **Ubicación:** `research/quant/edge_study.py:330–340`, `:287`, `:388–392`.
- **Evidencia:** n_trials=18 reglas simples+16 variantes meta=34. No incluye
  estudios anteriores ni las 21 features × 5 quintiles × 2 TP analizadas.
  BH se aplica por TP (105 hipótesis cada uno), luego se unen supervivientes:
  no es control BH global de las 210 hipótesis. PBO solo usa 18 reglas simples
  BASE de eventos solapados, no las selecciones meta single-slot del gate.
- **Impacto:** no se puede atribuir DSR/PBO al procedimiento de selección
  completo. Datos ya vistos y dirección/regímenes dominantes limitan el
  significado de OOS; la variante DIR_NEUTRAL sí está marcada POST-HOC.
- **Fix propuesto:** registro global de variantes, BH sobre la familia conjunta
  cuando se formula una conclusión conjunta y PBO de políticas comparables con
  la selección real. Mantener las advertencias de holdout reutilizado. La
  verificación de signo posterior no es una réplica estadística independiente:
  `conditional_edge` tampoco purga etiquetas en su división 60/40 (`:261–262`).

### R7 — Media: el gate admite comparaciones con benchmarks ausentes

- **Ubicación:** `research/quant/edge_study.py:430–436`.
- **Evidencia:** `max` descarta benchmarks None. Con BASELINE=0,1, sin
  LONG_ONLY/SHORT_ONLY y META_TOP50=0,5 (n=150, CI inferior=0,1, DSR=0,99,
  PF=1,5), `verdict` nomina `tp2.0_META_TOP50_STRESS` a PAPER.
- **Impacto:** no demuestra superar los tres benchmarks que exige el contrato;
  puede fallar si una dirección no tiene resultados evaluables. No cambia el
  resultado NONE de la muestra publicada.
- **Fix propuesto:** exigir presencia y valor conocido de los tres antes de
  comparar. Exits V1 lo exige para sus seis benchmarks y prueba la ausencia.

## Verificaciones positivas y límites

- Features actuales: índices por `close_timestamp <= observed_at`, indicadores
  recursivos causales; `structure_distance` usa ventanas cerradas. Sus tests
  comparan histórico completo, truncado y futuro perturbado. No se identificó
  look-ahead en la ruta actual de construcción de las 22 features.
- Walk-forward del modelo: train estrictamente anterior, purga por label end y
  embargo 24 h; imputación/estandarización, cortes de probabilidad y objetivo de
  volatilidad se ajustan con train. Modelo L2 determinista, sin tuning OOS
  automático. Los problemas R1–R6 limitan inferencia, no prueban fuga de features.
- Fills fijos: ask/bid, slip adverso por lado, comisión una vez, stop primero
  si ambos niveles se tocan y gaps negativos al open. Los gaps positivos solo
  acreditan TP. Spread asumido fijo por escenario, no el spread histórico;
  el docstring inicial de barriers aún dice casi constante 0,30, en conflicto
  con el contrato corregido. STRESS no modela colas de ejecución.
- Estadística básica: bootstrap circular y su p-valor centrado, PF, momentos,
  fórmula PSR/DSR, BH y rangos CSCV tienen tests sintéticos coherentes. El uso
  del helper DSR en el CLI, y no su fórmula, es el problema R2.
- Alcance protegido: el diff exacto contiene únicamente cinco módulos Python,
  `__init__`, contrato, tests y tres documentos. Ningún MQL, dashboard, input,
  riesgo, scoring, umbral ni autorización cambia; no hay imports del paquete
  desde MT5/dashboard. REAL/DEMO y EnableLiveTrading siguen fuera de alcance.

## Validación

85 tests originales de Quant V1 pasan con Python 3.14.7. Se ejecutaron además
las cinco reproducciones sintéticas descritas en R1/R3/R4/R5/R7 sin editar los
módulos auditados. No se repitió el replay histórico ni se reentrenó edge_study.
Los resultados publicados de V1 no se presentan como reconstruidos por esta
revisión. La validación de la suite completa y de Exits V1 se registra en
`docs/QUANT_EXITS_V1.md`. MQL no cambia: compilación MT5 no requerida.
