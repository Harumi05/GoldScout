# Historical Replay Engine

Motor offline y exclusivamente diagnóstico para exports de ticks de MT5. No
importa ni ejecuta el EA y todos sus snapshots/outcomes declaran
`observer_only=true` y `score_effect=0`.

## Uso

```powershell
python -m research.tick_historical_replay `
  --input-dir "C:\Users\Juliana\OneDrive\Desktop\Trading bot\Datos historicos"
```

La ruta es solo un ejemplo: `--input-dir` acepta cualquier carpeta y descubre
sus archivos `*.csv`. También se conservan los paths posicionales explícitos.
Antes del replay se inspeccionan los timestamps reales de los extremos y los
archivos se ordenan por contenido, no por nombre.

Los archivos se fusionan cronológicamente mediante streaming. Cada archivo
individual debe estar ordenado; si aparece una corrupción durante la lectura se
informa el archivo, se descarta su parte restante y se continúa con las demás
fuentes cuando sea seguro. Los rangos temporales superpuestos se anuncian y sus
ticks exactos se deduplican. `LAST` y `VOLUME` pueden estar vacíos. `FLAGS` se
conserva solamente en el objeto de ingesta y en el resumen diagnóstico: no se
interpreta.

Outputs append-only:

- `output/historical_bars/XAUUSD_M1.jsonl`
- `output/historical_bars/XAUUSD_M15.jsonl`
- `output/historical_bars/XAUUSD_H1.jsonl`
- `output/historical_bars/XAUUSD_H4.jsonl`
- `output/historical_observations.jsonl`
- `output/historical_outcomes.jsonl`

Una reejecución reconstruye causalmente indicadores y pivots desde el inicio,
pero los IDs deterministas evitan duplicar filas ya persistidas. Si un mismo ID
produce contenido diferente, el proceso falla en vez de reescribir datos.

## Convención temporal y de outcomes

DATE/TIME se preserva como hora de pared del servidor MT5, codificada en
milisegundos desde epoch solo para ordenar y agrupar. No se adivina la zona
horaria del broker.

Los campos base `future_return`, `mfe` y `mae` usan perspectiva LONG XAUUSD:

- `future_return = (último mid hasta el horizonte - mid inicial) / mid inicial`
- `mfe = max(0, (máximo mid - mid inicial) / mid inicial)`
- `mae = min(0, (mínimo mid - mid inicial) / mid inicial)`

Para SHORT: `return=-return`, `mfe=-mae` y `mae=-mfe`. Cada horizonte se emite
solo cuando el reloj del replay lo ha alcanzado; los ticks posteriores al
horizonte nunca entran en sus cálculos.

## Validación M1

`--validate-m1 C:\ruta\XAUUSD_M1.csv --tolerance 0.001` compara sin modificar
datos. Las barras reconstruidas usan `mid`; un export M1 de MT5 basado en BID
puede mostrar una diferencia sistemática aproximada de medio spread.

No se generan barras para gaps ni se persiste la última barra abierta al final
de un archivo. La reanudación evita duplicados, pero en esta primera versión
vuelve a leer los inputs desde el inicio para reconstruir el estado causal.
