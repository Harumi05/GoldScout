# Quant Exits V1 — prerregistro (2026-10-22)

Investigación offline: `observer_only=true`, `score_effect=0`,
`strategy_validated=false`. Sin cambios al EA, dashboard ni ejecución.

## Plan congelado antes de calcular resultados OOS

Candidatos y stop: exactamente `edge_study.iter_candidates(min_score=74)`,
features causales y `stop_distance` con estructura H1 de 20 barras. Horizonte
máximo 24 h. Datos existentes, sin holdout intacto; el histórico ya fue examinado.

Nueve políticas, sin ajustar parámetros después de observar resultados:

| Nombre | Regla |
|---|---|
| FIXED_1.25 | TP 1,25R, SL inicial |
| FIXED_2 | TP 2R, SL inicial |
| BE_1_TO_2 | Después de un máximo favorable de +1R en vela cerrada, SL al precio de entrada; TP 2R |
| CHANDELIER_1.5 | Extremo favorable desde entrada menos 1,5 ATR14 H1 de entrada; sin TP |
| CHANDELIER_2 | Igual, 2 ATR |
| CHANDELIER_3 | Igual, 3 ATR |
| PARTIAL_1_TO_2 | 50% a 1R, resto a 2R; SL inicial sin mover |
| PARTIAL_1_TO_3 | 50% a 1R, resto a 3R; SL inicial sin mover |
| TIME_8H_TO_2 | A partir de 8 h, salir al primer open disponible si beneficio ejecutable <0,25R; TP 2R hasta 24 h |

ATR chandelier congelado al entrar: no estima volatilidad futura. Stop solo
se estrecha; se actualiza después del cierre M1 y aplica desde el siguiente
open. Breakeven es precio, no beneficio neto cero. Coste por fracción ejecutada:
spread y slippage por lado y comisión proporcional; comisión total una vez.
Una vela que toca stop y cualquier objetivo toma stop primero. Gaps negativos
al open, positivos limitados al nivel objetivo. Sin mover SL dentro de la vela
que activa BE/trailing. Cada parcial paga slippage de salida en su fracción.

Single-slot usa entrada real y reserva hasta cierre de minuto para salidas
intrabar (orden desconocido); gaps al open permiten reutilización en ese open.
Una entrada censurada ocupa su horizonte y no se elimina retrospectivamente.
Las salidas por horizonte conservan la convención de última vela disponible
de `barriers.py`; se declara la limitación de gaps y fines de semana.

Validación: 5 folds expansivos comunes a todas las políticas, mínimo train 150,
purga usando horizonte completo 24 h más un minuto y embargo 24 h. Sin modelo
entrenado ni selección de política por OOS. Slot continuo entre folds. Se
reportan BOTH, LONG_ONLY, SHORT_ONLY como carteras independientes, además de
atribución LONG/SHORT de la cartera BOTH. BASE y STRESS. Bootstrap circular:
20 para eventos solapados, 5 para single-slot, sin reducir esos bloques.

Multiplicidad: 9 políticas × 3 direcciones × 2 costes × 2 modos = 108 variantes;
se añaden las 34 pruebas declaradas en Quant V1: **n_trials=142** en todo DSR.
No incluye estudios antiguos desconocidos. Varianza de Sharpe por operación
estimada solo en el train purgado del primer fold entre las 108 variantes,
con suelo 1/(menor n válido−1). DSR asume independencia temporal y es diagnóstico;
los IC por bloques complementan, no corrigen, esa aproximación.
BH q=0,10 sobre los 108 p-valores OOS de media >0 (no sobre el delta).
PBO CSCV: matriz semanal STRESS de las 27 políticas/direcciones single-slot,
ceros incluidos, 10 particiones, solo OOS; diagnóstico de selección reutilizada,
sin pretender validación independiente ni purga adicional de CSCV.

Gate obligatorio: STRESS single-slot walk-forward, n≥100, IC95 inferior >0,
DSR≥0,95, PF≥1,2 y E[R] superior a los tres benchmarks BASELINE/LONG_ONLY/
SHORT_ONLY de **ambos** TP fijos. Exigir ambos TP evita elegir benchmark post-hoc.
BH/PBO se informan aparte y no reemplazan el gate. Ningún resultado autoriza
PAPER/DEMO ni cambios de estrategia. Cualquier variante adicional será POST-HOC.

## Resultados

El prerregistro anterior se escribió en esta sesión antes de ejecutar el CLI
sobre datos reales; no se añadieron políticas POST-HOC. No es un registro
externo con sello temporal independiente. Tras la primera corrida se corrigió
el extremo inicial del chandelier para usar bid/ask ejecutable; se repitió el
mismo grid sin cambiar parámetros. Cambian ligeramente sus resultados y la
varianza de SR; el veredicto permanece igual.
El time stop comprueba edad ≥8 h y beneficio <0,25R en cada open desde las 8 h.

1.427 candidatos (1.030 LONG / 397 SHORT), 1.190 candidatos OOS en cinco folds
comunes de 238; discovery purgado inicial n=202. Las 25.686 simulaciones
(1.427 × 9 × 2) completaron; ninguna censurada. El ledger causal de candidatos
y stops tiene SHA256
`61cbc86c57b82aac793188470a1efa82f3c9d4c153f9ec2bb0a20c457842cc91`.

**Ninguna política supera el gate.** Cero de las 108 hipótesis sobrevive BH.
PBO=0,265873 (252 combinaciones, 27 carteras, STRESS). Varianza de SR estimada
en discovery=0,468290; DSR máximo entre las 108 variantes=2,1340e-36. La
penalización es muy fuerte: mezcla políticas heterogéneas y muestras pequeñas
en discovery, aunque mantiene unidades por operación y evita usar OOS para
estimarla. Estos DSR no deben interpretarse como probabilidades calibradas de
rentabilidad, ni compararse directamente con los DSR publicados de Edge V1.

La parcial 1R→2R solo LONG obtiene n=101, +0,199R, IC95 [0,031; 0,383],
PF=1,597 en STRESS, pero DSR≈3,45e-57 y no pasa BH. Parcial 1R→3R solo LONG
obtiene +0,232R y PF=1,709, con n=90 (también falla tamaño mínimo). Los
intervalos son de medias individuales, no del incremento frente al benchmark;
no prueban mejora de política. Chandelier 3 ATR solo LONG tiene n=98,
IC95 [-0,030; 0,506], tampoco suficiente. Los SHORT permanecen débiles.
No se selecciona una política para promoción.

### Tabla completa: OOS single-slot

BOTH, LONG_ONLY y SHORT_ONLY son carteras secuenciadas independientemente.
Los JSON/CSV ignorados incluyen eventos solapados, atribución LONG/SHORT de
BOTH, resultados por fold, cobertura, BH y cada criterio del gate.

| Política | Dirección | Coste | n | E[R] | IC95 | PF | DSR | BH | DD R |
|---|---|---|---:|---:|---|---:|---:|---|---:|
| FIXED_1.25 | BOTH | BASE | 194 | 0.082 | [-0.046; 0.217] | 1.182 | 4.40e-129 | False | -8.102 |
| FIXED_1.25 | BOTH | STRESS | 195 | 0.067 | [-0.066; 0.197] | 1.148 | 7.23e-132 | False | -8.440 |
| FIXED_1.25 | LONG_ONLY | BASE | 130 | 0.128 | [-0.027; 0.291] | 1.295 | 6.78e-82 | False | -8.102 |
| FIXED_1.25 | LONG_ONLY | STRESS | 131 | 0.109 | [-0.051; 0.270] | 1.246 | 1.57e-84 | False | -8.440 |
| FIXED_1.25 | SHORT_ONLY | BASE | 64 | -0.011 | [-0.212; 0.205] | 0.976 | 9.37e-48 | False | -5.190 |
| FIXED_1.25 | SHORT_ONLY | STRESS | 64 | -0.017 | [-0.217; 0.200] | 0.964 | 5.59e-48 | False | -5.206 |
| FIXED_2 | BOTH | BASE | 155 | 0.100 | [-0.063; 0.267] | 1.207 | 8.56e-107 | False | -7.978 |
| FIXED_2 | BOTH | STRESS | 152 | 0.093 | [-0.076; 0.258] | 1.193 | 3.13e-105 | False | -8.323 |
| FIXED_2 | LONG_ONLY | BASE | 103 | 0.140 | [-0.074; 0.357] | 1.297 | 3.55e-69 | False | -7.978 |
| FIXED_2 | LONG_ONLY | STRESS | 101 | 0.151 | [-0.064; 0.371] | 1.331 | 3.54e-67 | False | -8.323 |
| FIXED_2 | SHORT_ONLY | BASE | 52 | 0.023 | [-0.216; 0.275] | 1.044 | 2.06e-38 | False | -6.118 |
| FIXED_2 | SHORT_ONLY | STRESS | 51 | -0.024 | [-0.275; 0.241] | 0.955 | 3.10e-38 | False | -6.293 |
| BE_1_TO_2 | BOTH | BASE | 167 | 0.116 | [-0.027; 0.263] | 1.284 | 1.35e-113 | False | -8.990 |
| BE_1_TO_2 | BOTH | STRESS | 166 | 0.099 | [-0.048; 0.252] | 1.238 | 3.32e-114 | False | -8.885 |
| BE_1_TO_2 | LONG_ONLY | BASE | 111 | 0.165 | [-0.031; 0.365] | 1.442 | 1.57e-72 | False | -8.990 |
| BE_1_TO_2 | LONG_ONLY | STRESS | 111 | 0.150 | [-0.048; 0.353] | 1.389 | 1.90e-73 | False | -8.885 |
| BE_1_TO_2 | SHORT_ONLY | BASE | 56 | 0.019 | [-0.210; 0.259] | 1.040 | 2.51e-41 | False | -5.661 |
| BE_1_TO_2 | SHORT_ONLY | STRESS | 55 | -0.006 | [-0.225; 0.241] | 0.988 | 6.26e-41 | False | -5.859 |
| CHANDELIER_1.5 | BOTH | BASE | 261 | 0.063 | [-0.027; 0.160] | 1.230 | 8.21e-195 | False | -8.276 |
| CHANDELIER_1.5 | BOTH | STRESS | 261 | 0.046 | [-0.044; 0.144] | 1.163 | 3.70e-193 | False | -9.362 |
| CHANDELIER_1.5 | LONG_ONLY | BASE | 171 | 0.086 | [-0.029; 0.213] | 1.308 | 2.63e-129 | False | -8.276 |
| CHANDELIER_1.5 | LONG_ONLY | STRESS | 171 | 0.068 | [-0.048; 0.194] | 1.234 | 3.11e-128 | False | -9.362 |
| CHANDELIER_1.5 | SHORT_ONLY | BASE | 90 | 0.020 | [-0.113; 0.150] | 1.074 | 1.46e-66 | False | -5.287 |
| CHANDELIER_1.5 | SHORT_ONLY | STRESS | 90 | 0.005 | [-0.126; 0.136] | 1.019 | 3.79e-66 | False | -5.828 |
| CHANDELIER_2 | BOTH | BASE | 191 | 0.111 | [-0.010; 0.240] | 1.330 | 2.35e-138 | False | -7.391 |
| CHANDELIER_2 | BOTH | STRESS | 191 | 0.095 | [-0.025; 0.224] | 1.275 | 1.78e-138 | False | -8.119 |
| CHANDELIER_2 | LONG_ONLY | BASE | 126 | 0.134 | [-0.022; 0.306] | 1.401 | 1.45e-91 | False | -7.391 |
| CHANDELIER_2 | LONG_ONLY | STRESS | 126 | 0.116 | [-0.039; 0.288] | 1.340 | 1.13e-91 | False | -8.119 |
| CHANDELIER_2 | SHORT_ONLY | BASE | 65 | 0.066 | [-0.127; 0.272] | 1.195 | 4.44e-48 | False | -4.501 |
| CHANDELIER_2 | SHORT_ONLY | STRESS | 65 | 0.053 | [-0.141; 0.258] | 1.151 | 4.14e-48 | False | -4.551 |
| CHANDELIER_3 | BOTH | BASE | 150 | 0.170 | [-0.032; 0.403] | 1.408 | 1.14e-129 | False | -8.916 |
| CHANDELIER_3 | BOTH | STRESS | 150 | 0.139 | [-0.051; 0.350] | 1.324 | 1.15e-126 | False | -9.543 |
| CHANDELIER_3 | LONG_ONLY | BASE | 98 | 0.264 | [-0.007; 0.575] | 1.663 | 5.58e-93 | False | -8.916 |
| CHANDELIER_3 | LONG_ONLY | STRESS | 98 | 0.223 | [-0.030; 0.506] | 1.536 | 1.15e-90 | False | -9.543 |
| CHANDELIER_3 | SHORT_ONLY | BASE | 52 | -0.006 | [-0.230; 0.241] | 0.986 | 1.24e-38 | False | -6.869 |
| CHANDELIER_3 | SHORT_ONLY | STRESS | 52 | -0.017 | [-0.240; 0.231] | 0.963 | 1.68e-38 | False | -7.062 |
| PARTIAL_1_TO_2 | BOTH | BASE | 155 | 0.116 | [-0.019; 0.254] | 1.298 | 8.23e-100 | False | -6.006 |
| PARTIAL_1_TO_2 | BOTH | STRESS | 152 | 0.120 | [-0.016; 0.254] | 1.314 | 4.15e-97 | False | -6.314 |
| PARTIAL_1_TO_2 | LONG_ONLY | BASE | 103 | 0.185 | [0.014; 0.364] | 1.540 | 5.26e-60 | False | -6.006 |
| PARTIAL_1_TO_2 | LONG_ONLY | STRESS | 101 | 0.199 | [0.031; 0.383] | 1.597 | 3.45e-57 | False | -6.314 |
| PARTIAL_1_TO_2 | SHORT_ONLY | BASE | 52 | -0.021 | [-0.227; 0.184] | 0.956 | 3.06e-39 | False | -5.343 |
| PARTIAL_1_TO_2 | SHORT_ONLY | STRESS | 51 | -0.038 | [-0.238; 0.168] | 0.920 | 6.40e-39 | False | -5.502 |
| PARTIAL_1_TO_3 | BOTH | BASE | 140 | 0.126 | [-0.020; 0.277] | 1.331 | 3.17e-92 | False | -6.143 |
| PARTIAL_1_TO_3 | BOTH | STRESS | 138 | 0.134 | [-0.015; 0.284] | 1.355 | 4.56e-90 | False | -6.315 |
| PARTIAL_1_TO_3 | LONG_ONLY | BASE | 92 | 0.215 | [0.038; 0.393] | 1.635 | 1.79e-54 | False | -6.143 |
| PARTIAL_1_TO_3 | LONG_ONLY | STRESS | 90 | 0.232 | [0.050; 0.421] | 1.709 | 9.26e-52 | False | -6.315 |
| PARTIAL_1_TO_3 | SHORT_ONLY | BASE | 48 | -0.043 | [-0.264; 0.170] | 0.907 | 2.13e-36 | False | -5.322 |
| PARTIAL_1_TO_3 | SHORT_ONLY | STRESS | 48 | -0.050 | [-0.269; 0.163] | 0.893 | 1.77e-36 | False | -5.430 |
| TIME_8H_TO_2 | BOTH | BASE | 215 | 0.053 | [-0.072; 0.185] | 1.144 | 1.51e-150 | False | -11.120 |
| TIME_8H_TO_2 | BOTH | STRESS | 217 | 0.039 | [-0.091; 0.167] | 1.104 | 2.33e-153 | False | -11.468 |
| TIME_8H_TO_2 | LONG_ONLY | BASE | 142 | 0.068 | [-0.096; 0.238] | 1.190 | 1.33e-97 | False | -11.120 |
| TIME_8H_TO_2 | LONG_ONLY | STRESS | 144 | 0.050 | [-0.123; 0.228] | 1.138 | 1.52e-100 | False | -11.468 |
| TIME_8H_TO_2 | SHORT_ONLY | BASE | 73 | 0.024 | [-0.165; 0.230] | 1.061 | 1.44e-53 | False | -5.106 |
| TIME_8H_TO_2 | SHORT_ONLY | STRESS | 73 | 0.017 | [-0.171; 0.223] | 1.042 | 1.21e-53 | False | -5.301 |

## Limitaciones y diferencias frente a Edge V1

- Mismo generador de candidatos, ATR y estructura; sin paridad de señales EA.
  TP fijo/BE/trailing/parciales hipotéticos, sin broker ni lotes reales. Los
  parciales asumen divisibilidad del volumen: no modelan el mínimo/step del lote.
- Folds comunes a políticas usando horizonte máximo, no la duración realizada
  de BASE por TP como Edge V1. No hay optimización ni entrenamiento de política;
  los folds definen el tramo evaluado y purgan la estimación inicial del DSR.
- Slot reservado hasta fin de minuto intrabar. Por eso el benchmark STRESS
  1,25R tiene 195 operaciones frente a 196 en V1; 2R conserva 152. No comparar
  los DSR entre estudios: unidad, discovery y n_trials difieren.
- Bootstrap 20/5 fijo (sin tope n/10 de Edge V1). Folds dominados por dirección;
  las operaciones de LONG_ONLY son también una apuesta al rally del oro.
- Conserva cierre temporal de última vela dentro de 24 h, aun si hay gap:
  por ejemplo STRESS BOTH tiene 19 TIME con gap en TP 1,25R/2R. El resultado
  es una convención de comparación, no un fill garantizado al vencimiento.
- Costes supuestos constantes. Distintos costes pueden cambiar secuencia,
  parciales y elegibilidad: STRESS no tiene por qué bajar todas las medias
  frente a BASE. Sin variable spread, financiación, latencia, gaps de ticks
  ni colas de slippage. ATR chandelier congelado, no ATR recalculado intratrade.
- PBO semanal conserva ceros y contabiliza cada parcial en su semana de salida;
  CSCV no es walk-forward ni aplica purga entre particiones. Es diagnóstico,
  no probabilidad calibrada de pérdida o prueba independiente del gate.
- n_trials=142 es un mínimo declarado de esta familia y V1, no de toda la
  investigación histórica. No existe holdout intacto ni validación prospectiva.
- El prerregistro fija las reglas pero se hizo después de conocer resultados
  de Edge V1. No permite borrar el sesgo de investigación acumulado.

## Reproducción y validación

```powershell
python -m research.quant.exits_study `
  --input-dir "<checkout de datos>/research/output" `
  --output-dir research/analysis/quant_exits_v1 --seed 0
python -m unittest tests.test_quant_exits -v
python -m unittest discover -s tests
python -m compileall -q research/quant/exits.py research/quant/exits_study.py tests/test_quant_exits.py
git diff --check
```

Validación ejecutada: **37 tests nuevos**, 85 tests originales Quant V1,
**948 tests en suite completa (911 previos +37), todos OK**. Los tests incluyen
360 comparaciones sintéticas de fills fijos con barriers.py, ambos sentidos y
tres costes, ambigüedad, gaps, BE causal, trailing causal, parciales, censura,
single-slot, frontera de folds, DSR solo discovery y gate contra ambos TP.
Compileall y CLI `--help` correctos. Gramática Python 3.12 comprobada con AST;
la ejecución usó **Python 3.14.7**, ya que 3.12 no estaba disponible en el entorno.
No se afirma compatibilidad runtime 3.12 verificada.

La primera suite completa falló por `requests` ausente (módulo existente de
dashboard). La corrida final usó `requests 2.33.0` de un entorno local existente,
añadido solo al PYTHONPATH del proceso. No se modificó dicho entorno ni el
dashboard. Los módulos nuevos solo importan biblioteca estándar y el paquete
quant existente; no requieren requests.

Outputs JSON/CSV/Markdown en `research/analysis/quant_exits_v1/`, ignorados y
sin commit de datos. Revisión ae9fce2 en `REVIEW_QUANT_RESEARCH_V1_CODEX.md`;
sus hallazgos se reportaron sin tocar los módulos revisados. Sin cambios MQL:
compilación MT5 no requerida. Sin push, merge, PR ni despliegue; revisión
independiente de estas adiciones y aprobación del propietario pendientes.

El commit local solicitado quedó bloqueado por permisos del filesystem:
`.git` del worktree apunta a metadata bajo OneDrive, mientras el perfil solo
permite escribir este worktree y el archivo `.git` de Orca. `git add` no puede
crear `index.lock`. No se alteró la configuración ni se intentó eludir esa
restricción. HEAD sigue en ae9fce2; los cinco archivos nuevos están sin staging.
Se entrega también un parche y mensaje de commit en el directorio de análisis
ignorado para su incorporación por el coordinador en un entorno autorizado.
