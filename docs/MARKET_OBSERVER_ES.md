# Market Observer — convención de resultados futuros

`market_observations.jsonl` permanece inmutable. Los resultados se escriben en
`market_outcomes.jsonl`, también append-only, y se relacionan mediante
`event_id`.

Dentro de MT5 ambos nombres se resuelven en `Terminal\Common\Files` por las
restricciones del sandbox del terminal. El dashboard puede proyectar ese mismo
archivo como `dashboard/data/market_outcomes.jsonl`, igual que hace con las
observaciones.

El etiquetador añade como máximo tres registros por observación, identificados
de forma determinista por `outcome_id = event_id|15m`, `event_id|1h` y
`event_id|4h`. El consumidor debe agrupar por `event_id` y tomar el registro
terminal de cada `horizon`; no debe suponer que el último registro repite los
valores de horizontes terminados antes de un reinicio.

## Convención numérica

Todos los valores son retornos decimales desde la perspectiva LONG de XAUUSD:

- `future_return`: `(cierre_futuro - precio_inicial) / precio_inicial`.
- `mfe`: `max(0, (máximo_posterior - precio_inicial) / precio_inicial)`.
- `mae`: `min(0, (mínimo_posterior - precio_inicial) / precio_inicial)`.

Por tanto, `0.01` equivale a una subida de oro del 1%, `mfe` nunca es negativo
y `mae` nunca es positivo. Para evaluar una hipótesis SHORT se invierte el signo
del retorno; los datos almacenados no presuponen una operación ni una dirección.

## Tiempo y datos permitidos

- Un cierre normal usa como ancla el cierre exacto de su vela M15, H1 o H4.
- Un evento intrabar usa `captured_at` como ancla.
- Solo se incorporan barras M1 completamente cerradas y posteriores al ancla.
- Mientras el histórico M1 aún no esté sincronizado o no haya avanzado más allá
  del rango requerido, el horizonte permanece pendiente.
- Si la serie M1 sincronizada ya avanzó más allá del rango y faltan minutos, se
  persiste una etiqueta terminal `status="UNRESOLVABLE_GAP"` con los valores de
  ese horizonte en `null`. No se inventan precios ni se vuelve a intentar.
- Los registros se emiten progresivamente al completar 15 minutos, 1 hora y
  4 horas. No se modifica la observación original.

Esta información es exclusivamente de dataset: `observer_only=true` y
`score_effect=0`. Ningún resultado se lee desde scoring o ejecución.

La recuperación al iniciar inspecciona una ventana reciente de 32 MiB de cada
JSONL. Es suficiente para el horizonte operativo de 4 horas en uso normal; una
observación excepcionalmente antigua que quede fuera de esa ventana no se
reconstruye automáticamente.
