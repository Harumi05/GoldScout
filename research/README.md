# Historical Replay Engine

Motor offline y exclusivamente diagnóstico para exports de ticks de MT5. No
importa ni ejecuta el EA y todos sus snapshots/outcomes declaran
`observer_only=true` y `score_effect=0`.

## Uso

```powershell
python -m research.tick_historical_replay `
  --input-dir "C:\Users\Juliana\Desktop\Trading bot\Datos historicos"
```

La ruta es solo un ejemplo: `--input-dir` acepta cualquier carpeta y descubre
sus archivos `*.csv`. El manifest atómico
`output/processed_inputs.json` identifica cada archivo mediante path
normalizado, tamaño, mtime y SHA-256. Un CSV `SUCCESS` con tamaño y mtime
idénticos se omite sin volver a abrir su lector de ticks; archivos nuevos o
fallidos se validan y procesan por orden de timestamps reales.

Los estados persistidos son `NEW`, `PROCESSING`, `SUCCESS`, `FAILED`,
`CHANGED_REQUIRES_REBUILD` y, durante la estabilización en watch mode,
`WAITING_FOR_STABLE_FILE`. Un manifest corrupto detiene el proceso: nunca se
descarta para volver a procesar silenciosamente todo el histórico.

Modo continuo (dos observaciones estables de tamaño/mtime antes de procesar):

```powershell
python -m research.tick_historical_replay `
  --input-dir "C:\Users\Juliana\Desktop\Trading bot\Datos historicos" `
  --watch --watch-interval 60
```

## Adopción controlada de un full-run existente

Para inicializar el manifest sin regenerar outputs ni ejecutar el replay, se
deben declarar explícitamente el tamaño y rango ya validados:

```powershell
python -m research.tick_historical_replay `
  --input-dir "C:\Users\Juliana\Desktop\Trading bot\Datos historicos" `
  --adopt-existing-input `
  --expected-size-bytes 5955642667 `
  --expected-first-timestamp "2025-05-15T07:12:07.545" `
  --expected-last-timestamp "2026-09-11T20:58:57.490" `
  --expected-ticks-valid 132193488
```

La adopción exige un único CSV XAUUSD, comprueba sus extremos y estabilidad,
valida la cobertura M1 y los contratos de los seis outputs existentes, y
calcula SHA-256 completo en streaming. El manifest solo se escribe después de
que todas las verificaciones coincidan. `--expected-fingerprint sha256:...`
permite contrastarlo además con un hash obtenido previamente.

Los archivos se fusionan cronológicamente mediante streaming. Cada archivo
individual debe estar ordenado; si aparece una corrupción durante la lectura se
informa el archivo, se descarta su parte restante y se continúa con las demás
fuentes cuando sea seguro. Los rangos temporales superpuestos se anuncian y sus
ticks exactos se deduplican. `LAST` y `VOLUME` pueden estar vacíos. `FLAGS` se
conserva solamente en el objeto de ingesta y en el resumen diagnóstico: no se
interpreta.

Si cambia el fingerprint de un archivo ya exitoso se marca
`CHANGED_REQUIRES_REBUILD` y no se toca ningún output. Los solapamientos entre
archivos nuevos del mismo lote se fusionan y deduplican por tick exacto. Un
archivo nuevo que solape historia ya finalizada solo se acepta automáticamente
si es una copia completa idéntica; cualquier otro solapamiento requiere rebuild
porque unas barras append-only no pueden corregirse con seguridad.

Outputs append-only:

- `output/historical_bars/XAUUSD_M1.jsonl`
- `output/historical_bars/XAUUSD_M15.jsonl`
- `output/historical_bars/XAUUSD_H1.jsonl`
- `output/historical_bars/XAUUSD_H4.jsonl`
- `output/historical_observations.jsonl`
- `output/historical_outcomes.jsonl`
- `output/processed_inputs.json`

El manifest pasa a `PROCESSING` antes de abrir los lectores y a `SUCCESS` solo
después del cierre correcto. Un `PROCESSING` encontrado tras reinicio pasa a
`FAILED` y puede reintentarse; el manifest se sustituye atómicamente. Si un
mismo ID produce contenido diferente, el proceso falla en vez de reescribir
datos.

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
de un lote. Los lotes incrementales no releen CSV exitosos: por ello los
archivos deben llegar en orden posterior al rango finalizado. Datos tardíos que
invadan ese rango se detienen con `CHANGED_REQUIRES_REBUILD`.

Una instalación con outputs creados antes de que existiera el manifest no puede
atribuirlos automáticamente a un CSV con garantías. Su primera ejecución
considerará los inputs como `NEW`; conviene conservar un respaldo de los outputs
legacy y hacer esa inicialización de forma controlada.
