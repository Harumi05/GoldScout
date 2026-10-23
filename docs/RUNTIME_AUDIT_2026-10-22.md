# Revisión de evidencias runtime — 2026-10-22

## Objetivo y alcance

Revisar los trades recientes y preparar mejoras justificadas por evidencia,
sin promover filtros ni cambiar riesgo, SL/TP o autorización de órdenes.
Base: `e539b05`, integración Hali aprobada/fusionada en PR #13.
`CURRENT_TASK.md` todavía mostraba el estado anterior al merge al iniciar esta
tarea; el cierre de GitHub confirma su aceptación el 30 de septiembre.

La ventana solicitada en la auditoría es 16–22 de octubre, usando etiquetas
calendario de hora de pared MT5; el UTC offset del broker no está validado.

## Hali inspeccionado directamente

Se accedió, en lectura, a https://goldscout.duckdns.org/ con la sesión existente
y a sus endpoints documentados `/api/status`, `/api/log` y `/api/chart`.
El snapshot servido es del **19 de octubre a las 10:12:40**. MT5/EA y Observer
marcan **STALE, aproximadamente 3,7 días**. Los badges de broker conectado y
DEMO READY son valores de ese snapshot, no verificación actual de conexión,
posición abierta o autorización. El snapshot no incluye account_login,
risk_budget V2 ni demo_ledger. La UI declara **BROKER HISTORY · PRE-LEDGER V2**;
el artefacto desplegado no está identificado por hash/version.

La página muestra 25 cierres del 22 de septiembre al 15 de octubre, con 16
ganancias, nueve pérdidas, neto reportado **+23,74 USD**, PF **1,07** y R medio
**+0,150**. Son estadísticas de una ventana truncada de hasta 25 cierres/30
días, sin prueba de costes completos ni reconciliación V2. Quince setups son
UNKNOWN. Hay 23 SHORT y dos LONG; ocho pérdidas son SHORT y una LONG.

El log contiene 17 señales desde el 28 de septiembre al 16 de octubre.
Se obtuvieron 16 coincidencias únicas con cierres exigiendo igualdad de hora
de apertura, símbolo, dirección, score y volumen. No es una reconciliación por
broker deal/account ID; las nueve filas restantes conservan contexto desconocido.

| Dirección / H1 de la señal / H4 | Cierres asociados | Ganadores | Perdedores | Neto reportado USD |
|---|---:|---:|---:|---:|
| SHORT / BAJISTA / BAJISTA | 11 | 5 | 6 | -126,57 |
| SHORT / ALCISTA / BAJISTA | 3 | 3 | 0 | +72,45 |
| LONG / ALCISTA / BAJISTA | 2 | 1 | 1 | -9,64 |
| SHORT / desconocido / desconocido | 9 | 7 | 2 | +87,50 |

La hipótesis del propietario, «vendía mientras subía», no se descarta como
movimiento local del precio, pero no equivale a vender con la etiqueta H1
ALCISTA. Las seis pérdidas SHORT con contexto disponible tenían H1/H4 BAJISTA.
Bloquear ventas solo por H1 ALCISTA habría retirado tres ganadoras (+72,45)
sin evitar esas seis pérdidas. Tampoco se concluye que ese grupo sea robusto:
las observaciones son escasas, seleccionadas y correlacionadas.

Ejemplos para investigación causal, sin promover filtros:

- SHORT BREAKOUT del 14 de octubre 13:00:47: score 95, RSI 20, ADX 47,8;
  cierre por SL -31,74. Posible entrada extendida/reversión; no prueba un cutoff.
- SHORT MOMENTUM del 2 de octubre 01:00:06: score 76, ADX 13,8; cierre -35,04.
- SHORT CONTINUATION del 30 de septiembre 00:46:32: RSI 61,9 pese a etiquetas
  H1/H4 BAJISTA; cierre -48,69. Requiere velas causales para estudiar el rebote.

Solicitar 300 barras H1 al endpoint solo devuelve 12 barras, del 18 de octubre
22:00 al 19 09:00. No cubren las entradas anteriores. Hace falta exportar
observaciones y velas H1/H4/M15 alrededor de las entradas desde el runtime,
sin inventar tendencias a partir de velas posteriores. La posición que la UI
presenta abierta procede del 16 de octubre; no se conoce su estado actual.

## Evidencia realmente disponible

Solo se encontraron registros locales en MT5 `Common/Files` y logs del terminal
que terminan en septiembre. No son evidencia del estado actual del VPS.

| Fuente | Contenido | Última etiqueta interna |
|---|---|---|
| `market_execution_events.jsonl` | 54 eventos legacy, sin esquema V2 | 2026-09-23 19:14:18 |
| `market_observations.jsonl` | 960 observaciones | 2026-09-28 16:30:00, campo timestamp |
| `market_outcomes.jsonl` | 2.867 outcomes | 2026-09-28 16:47:00, campo evaluated_at |
| `xau_goldscout_dashboard.json` | snapshot con diez cierres | 2026-09-28 16:48:29 |

El ledger contiene 13 señales, 12 solicitudes, 12 fills, 12 aperturas y cinco
marcadores de cierre. El snapshot muestra diez cierres entre el 21 y el 25 de
septiembre. La diferencia de conteos no prueba por sí sola qué trades faltan:
las fuentes legacy carecen de identidad de cuenta/esquema V2 para reconciliar.
El resultado reproducido por la nueva auditoría es `NO_V2_EVIDENCE`,
`recent_v2_evidence_available=false`, con métricas validadas `null`.

El hash SHA-256 del ledger leído es
`114439e484c7bfd3ce8fefe0a8c1d84949fdee2d89c659b979d8754e2754c2d8`.
El snapshot leído tiene hash
`53b80f23e60e883aa0668c12fe2745447697cc439e862473f5a60904f22ff53b`.
El informe JSON del CLI permite identificar también observaciones/outcomes.

## Hallazgos para investigar

1. **Trazabilidad incompleta:** seis de diez cierres del snapshot tienen
   `setup=UNKNOWN`. En las coincidencias candidatas por posición, sus fills
   dicen `CONTINUATION SHORT`. No se modifica ni completa el ledger con esta
   inferencia: falta confirmar cuenta, versión y MT5 History.
2. **Hora de apertura inconsistente:** una coincidencia candidata muestra fill
   el 23 de septiembre 19:14:18, pero apertura del snapshot el 24 a las 22:20:39.
   Es una diferencia de 27h06m21s, no una prueba de la duración real del trade.
   Contrastar recuperación/reinicio y hora broker con los deals originales.
3. **TP pequeño:** un cierre GOD reporta `0.063R`. Diagnosticar objetivo
   seleccionado, SL/riesgo inicial, costes y modificaciones. Un único trade
   no justifica promover un floor ni cambiar la política TP.
4. **Estado operativo histórico:** el snapshot reporta cuenta DEMO, ejecución
   habilitada por input pero bloqueada con `TRADING_NOT_ALLOWED`, sin posiciones
   abiertas, y sin `risk_budget` V2. Esto describe ese snapshot antiguo, no el
   estado actual del VPS ni una regresión probada en main.
5. **Protecciones activas:** hay 76 registros `RELEVANT_REJECTION`: 39 por lote
   mínimo/step, 36 por spread y uno por reserva H1. Son registros de telemetría,
   no oportunidades únicas. No se relajan los guards para aumentar frecuencia.

Como descripción exclusiva del snapshot legacy, sus diez filas reportan siete
ganancias y tres pérdidas, suma neta 58,92, PF 1,549 y R medio 0,25338. Estas
cifras no son resultados V2 reconciliados, no prueban costes completos ni
robustez y no se convierten en métricas validadas del auditor. La muestra de
una sola operación LONG tampoco permite concluir especialización por SHORT.

## Research existente reutilizado

Se consultaron los outputs locales existentes de Entry Quality V1 y TP V2.1,
sin repetir el replay de 132,2 millones de ticks ni modificar sus archivos.

- Entry Quality V1: diferencias descriptivas por extensión EMA20 y distancia
  al obstáculo; no valida filtros. La proximidad al obstáculo no fue
  consistentemente peor. Los outcomes son proxies de precio, no R realizado.
- TP V2.1: los comparativos tienen censura/cobertura incompleta y escenarios de
  coste; no son fills runtime. El OOS existente ya está examinado y no debe
  presentarse como un nuevo conjunto intocado al seleccionar challengers.

Ambos permanecen diagnósticos, `score_effect=0`; no se ajustan umbrales con los
diez cierres legacy ni se promueve una variante de salida.

## Trabajo realizado y trabajo dependiente

- Auditoría local y revisión de research: realizadas con los límites anteriores.
- Mejora implementada: CLI offline con hashes, cobertura temporal, separación
  legacy/V2, cuenta explícita, control de corrupción/conflictos y costes
  desconocidos; pruebas sintéticas en `test_runtime_trade_audit.py`.
- Corrección reproducible en código actual: el escritor MQL agrega separadores
  en blanco para preservar la frontera de append tras EOF/reinicio. El lector
  Python los marcaba `CORRUPT_LEDGER`. Con un archivo sintético del mismo
  formato, el lector de `e539b05` devuelve `CORRUPT_LEDGER` y el corregido `OK`.
  Se ignoran únicamente líneas vacías/whitespace; JSON inválido/truncado sigue
  siendo corrupción. Solo cambia la proyección del dashboard, no el writer
  MQL, ledger persistido ni autorización de nuevas órdenes.
- Compatibilidad del auditor con el serializer real: RESERVED, algunos
  RECONCILED y POSITION_CLOSED no incluyen account_mode. Solo se admite su
  modo ausente con evidencia DEMO del mismo trade/cuenta en otros eventos;
  sin ella se mantiene desconocido. Las líneas separadoras se admiten también
  en el auditor; el formato no se deduce únicamente de los fixtures antiguos.
- Coordinación multiagente: no iniciada. La revisión automática rechazó la
  invocación de creación de Run/worker y luego la ayuda de worker-start con
  `approval required by policy, but AskForApproval is set to Never`.
  No se sustituyó Orca por agentes sin su trazabilidad ni se afirma revisión
  independiente de este cambio.
- Fuente remota: Hali fue inspeccionado después de que el propietario facilitó
  la URL. Los registros locales de septiembre siguen separados de ese
  snapshot remoto. Falta evidencia runtime actual y velas de las entradas.

## Validación del cambio local

- 19 pruebas nuevas del auditor y 56 pruebas del módulo ledger: aprobadas.
- Suite completa: **826/826**, sin fallos/skips.
- Reproducción diferencial del separador: lector base CORRUPT_LEDGER,
  lector corregido OK, con el mismo archivo sintético.
- `git diff --check`: limpio. MQL sin cambios; compilación no repetida.
- Sin modificación de archivos runtime, inputs, órdenes, VPS o datasets.
- Revisión independiente pendiente; no hubo agentes lanzados.

## Siguiente ola al disponer de evidencia actual

1. Identificar versión/hash del EA desplegado, inputs efectivos, reloj del
   broker y cuenta DEMO; capturar en solo lectura ledger, snapshot, logs e
   History de los últimos 7–14 días con una frontera de captura explícita.
   No reemplazar binarios, reiniciar procesos ni alterar posiciones.
2. Auditor de ejecución: contrastar fills/cierres, duplicados, parciales,
   recuperación, costes y PnL/R con History. Separar anomalía confirmada de
   falta de evidencia y de un guard correcto.
3. Auditor de entradas: segmentar setup/dirección/clase y contexto H1/H4/M15
   solo con datos disponibles antes de cada entrada; reportar tamaños de
   muestra. No optimizar un filtro sobre la pequeña muestra reciente.
4. Implementador: corregir únicamente fallos reproducibles o añadir un
   challenger offline registrado previamente. Revisor distinto valida el diff
   y las pruebas; cambios de ejecución/riesgo/SL/TP requieren suite completa,
   compilación MQL real y revisión humana antes de merge/despliegue.

La activación de estrategia, Risk Budget Phase 2 y cualquier cambio a las
restricciones protegidas quedan fuera de esta tarea.
