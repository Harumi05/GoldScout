# Historical Replay Engine

## Auditoría de registros reales (solo lectura)

`audit_runtime_trades` inspecciona una carpeta de evidencias MT5 sin importar ni
ejecutar el EA, reparar archivos o enviar órdenes:

```powershell
python -m research.audit_runtime_trades `
  --input-dir "$env:APPDATA\MetaQuotes\Terminal\Common\Files" `
  --as-of-date 2026-10-22 --days 7
```

La fecha final es explícita; la ventana inclusiva del ejemplo es 16–22 de
octubre. Las fechas internas se interpretan como etiquetas de hora de pared
MT5, sin afirmar UTC ni sesiones del broker. No se usa el mtime de una copia
para considerarla reciente. Se informa el hash de los mismos bytes leídos y
se rechazan fuentes modificadas durante su lectura; las cuatro lecturas no
constituyen una captura atómica entre archivos.

El JSON separa eventos legacy, cierres del snapshot sin reconciliar y métricas
V2 de una sola cuenta DEMO identificada en el ledger. Con varias cuentas,
`--account-login` selecciona una; no se suman cuentas. Se proyecta el ledger
completo antes de filtrar cierres por fecha para conservar validaciones de
fills previos. Costes o riesgo inicial desconocidos no producen PnL/R
inventados; la expectativa R indica cuántas muestras conocidas tiene. Un
periodo sin evidencia conserva los importes desconocidos como `null`.

El estado V2 describe la proyección del archivo, no una validación contra
MT5 History, cobertura completa, rentabilidad OOS o autorización de ejecución.
`strategy_validated=false` siempre. Exit 0 significa evidencia V2 de la cuenta
seleccionada dentro de las fechas; exit 2 significa evidencia reciente no
disponible/verificable. El informe omite contenido de eventos, cuentas y rutas
absolutas. Los registros de entrada y los informes generados son privados y no
deben incorporarse al repositorio.

## Quant Research V1 (offline)

`research/quant/` contiene herramientas cuantitativas solo diagnósticas
(`observer_only=true`, `score_effect=0`): features causales, triple barrera M1
con escenarios de coste, walk-forward purgado, meta-labeling, PSR/DSR, BH-FDR y
PBO. Contrato en `research/quant/CONTRACT.md`; resultados e interpretación en
`docs/QUANT_RESEARCH_V1.md`.

```powershell
python -m research.quant.edge_study `
  --input-dir research/output --output-dir research/analysis/quant
```

La compuerta solo puede nominar un challenger PAPER; nunca modifica el EA.

## Replay histórico

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

## Enriquecimiento diagnóstico de decisiones

El replay puede anexar un sidecar por `event_id` sin reescribir las
observaciones ni outcomes originales:

```powershell
python -m research.enrich_historical_decisions `
  --input-dir research/output `
  --point-size 0.01
```

`historical_decisions.jsonl` añade scores LONG/SHORT, candidato, estado de
decisión, contexto H4, estructura H1, timing M15, thresholds y el diagnóstico
H1 de 30/10/5/3 velas. Todo registro conserva `observer_only=true`,
`score_effect=0` y declara `PARTIAL_EXACT_CLOSED_BAR_CORE`: se replica el núcleo
cerrado de EMA/RSI/ADX/ATR, volumen, pivots, bucket estructural y M15, pero no se
simulan silenciosamente patrones, noticias históricas, intrabar ni controles de
cuenta/ejecución. La paridad de inicialización de indicadores aún debe
contrastarse con MT5.

El tiempo de los CSV es hora de pared del servidor. Las sesiones solo se
calculan al proporcionar explícitamente `--server-utc-offset-hours`; sin ese
dato quedan `null`, en vez de inferir una zona horaria. Para analizar el sidecar
con división temporal 60/20/20:

```powershell
python -m research.analyze_historical_dataset `
  --input-dir research/output `
  --output-dir research/analysis
```

Los diagnósticos históricos de decisiones y calidad de stop se ejecutan por
separado y no alimentan el score ni la ejecución:

```powershell
python -m research.analyze_stop_loss_quality `
  --input-dir research/output `
  --tick-input-dir "C:\Users\Juliana\Desktop\Trading bot\Datos historicos" `
  --output-dir research/analysis

python -m research.analyze_adaptive_stop_v2 `
  --input-dir research/output `
  --tick-input-dir "C:\Users\Juliana\Desktop\Trading bot\Datos historicos" `
  --output-dir research/analysis

python -m research.analyze_adaptive_stop_final_ab `
  --input-dir research/output `
  --tick-input-dir "C:\Users\Juliana\Desktop\Trading bot\Datos historicos" `
  --output-dir research/analysis

python -m research.analyze_take_profit_and_account_size `
  --input-dir research/output `
  --tick-input-dir "C:\Users\Juliana\Desktop\Trading bot\Datos historicos" `
  --output-dir research/analysis

python -m research.analyze_structure_aware_tp `
  --input-dir research/output `
  --tick-input-dir "C:\Users\Juliana\Desktop\Trading bot\Datos historicos" `
  --output-dir research/analysis

python -m research.analyze_structure_aware_tp_v2 `
  --input-dir research/output `
  --tick-input-dir "C:\Users\Juliana\Desktop\Trading bot\Datos historicos" `
  --output-dir research/analysis
```

El segundo comando compara `CURRENT`, `STRUCTURE`, `ATR_1_0`, `ATR_1_5`,
`ATR_2_0` y `HYBRID`. Un stop prematuro significa que BID/ASK toca el SL y el
mid avanza después al menos 1 ATR en la dirección original dentro del
horizonte. Un stop excesivamente amplio supera 1,5 veces el P90 TRAIN del MAE
previo al primer avance favorable de 1 ATR para el mismo setup/dirección. El
lotaje mostrado es solo una relación conceptual inversa a la distancia para
mantener constante el riesgo monetario; no modifica `RiskPercent`.

Se generan, bajo `analysis/`, `stop_loss_analysis.md`, `stop_loss_by_setup.csv` y
`stop_loss_candidates.csv`. Adaptive Stop v2 genera además
`adaptive_stop_v2.md/.csv` y `adaptive_stop_by_setup.csv`; sus objetivos R solo
cuentan si el tick ejecutable los alcanza antes del SL, y todas las variantes
se comparan con CURRENT conservando el riesgo mediante lotaje inverso a la
distancia. La validación final conservadora genera
`adaptive_stop_final_ab.md/.csv`; el criterio de conservación de +1R/+2R
tolera como máximo 1 punto porcentual de deterioro OOS y nunca sustituye una
prueba PAPER. El análisis conjunto de TP/capital genera
`take_profit_analysis.md`, `take_profit_by_setup.csv`,
`minimum_account_size.md` y `minimum_account_size.csv`. Compara solo los R
solicitados con el TP CURRENT (CHILL 1,25R / GOD 2R), resuelve TP contra SL por
orden BID/ASK y deja censurados los casos sin toque; para Capital.com usa el
contrato XAUUSD reproducible de 100 oz, volumen mínimo/step 0,01 y el peor fill
permitido de 30 puntos. No estrecha el SL para hacer viable una cuenta. El
análisis `structure_aware_tp` conserva ese SL CURRENT y compara los TP
CURRENT, el fijo anterior (CHILL 0,75R / GOD 1,25R) y una política
`STRUCTURE FIRST, R:R SECOND`. Sus niveles proceden únicamente de pivots y
barras H4/H1/M15 ya cerradas al timestamp de la observación. Un soporte o
resistencia mayor anterior impide seleccionar un TP posterior; si el objetivo
estructural ofrece menos de 1,25R, el caso se registra `POOR_REWARD` sin mover
artificialmente el TP. Genera `structure_aware_tp.md/.csv` y
`structure_aware_tp_examples.csv`, siempre con `score_effect=0`.

`structure_aware_tp_v2` parte del fijo anterior (CHILL 0,75R / GOD 1,25R)
y solo recorta antes del primer obstáculo H4/H1/M15 de confianza HIGH/MEDIUM.
Compara buffers 0,10/0,15/0,20 ATR y floors 0,50/0,60/0,70R, aceptando y
rechazando por separado los casos `LOW_REWARD`. Genera
`structure_aware_tp_v2.md/.csv` y `structure_aware_tp_v2_examples.csv`; no
alimenta el EA ni cambia el SL.

Esta carpeta contiene artefactos regenerables y se mantiene fuera de Git.

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
