# GoldScout Observer para TradingView

`GoldScout_Observer.pine` es un indicador de observación. No es una estrategia, no envía órdenes y no altera el EA ni su scoring. El backend fuerza `score_effect=0` para todos sus eventos.

## Instalación

1. Abra un gráfico cuyo ticker sea exactamente `XAUUSD` o `GOLD`.
2. Abra **Pine Editor**, cree un indicador nuevo y pegue el contenido de `GoldScout_Observer.pine`.
3. Guarde el script y seleccione **Add to chart / Añadir al gráfico**.
4. Use exclusivamente uno de estos intervalos: `15`, `60` o `240` minutos. M30 no está soportado.
5. En la configuración del indicador, introduzca en **GoldScout Webhook Token** el mismo valor local configurado como `GOLDSCOUT_TRADINGVIEW_WEBHOOK_TOKEN` en el servidor GoldScout.

El token no está incluido en el repositorio y el script no lo escribe en Pine Logs. Sí forma parte del body enviado y TradingView guarda una copia de los inputs al crear una alerta; use un token dedicado. Si cambia el token o cualquier parámetro, elimine y vuelva a crear la alerta.

## Creación de la alerta

1. Pulse **Create Alert / Crear alerta**.
2. En **Condition / Condición**, seleccione **GoldScout Observer → Any alert() function call**.
3. Configure la alerta para permanecer activa. El script restringe internamente las llamadas a cierres de vela confirmados.
4. En **Webhook URL**, use una URL HTTPS pública que termine en:

   `https://SU_HOST_SEGURO/api/tradingview/webhook`

5. No sustituya el mensaje dinámico generado por el script. Cada llamada `alert()` construye el JSON completo.

Para cubrir M15, H1 y H4, repita el proceso en gráficos `15`, `60` y `240`. Cada alerta conserva el símbolo, timeframe e inputs existentes en el momento de su creación.

## Eventos

- `structure`: compara los dos últimos pivots altos y bajos confirmados. HH+HL produce `LONG`, LH+LL produce `SHORT` y una combinación mixta produce `NEUTRAL`.
- `breakout`: exige que el cierre cruce el último pivot confirmado y reciente. Una mecha aislada no confirma.
- `momentum`: exige ADX por encima del umbral, dirección DMI coherente y RSI alineado. Se emite al entrar en el estado, no en cada vela que permanezca allí.
- `pullback`: dentro de una estructura válida y del lado correcto de EMA200, detecta un cierre en la zona ATR alrededor de EMA20 o del nivel de breakout conservado.
- `recovery`: después de un pullback registrado, exige un cierre que recupere EMA20 o el nivel roto.

Todos los eventos se evalúan con `barstate.isconfirmed`. Los pivots necesitan las barras derechas configuradas y solo se usan después de quedar confirmados; las líneas se muestran desde la confirmación y no se retrocolocan como si hubieran sido conocidas antes.

## JSON enviado

```json
{
  "token": "VALOR_CONFIGURADO_LOCALMENTE",
  "source": "tradingview",
  "symbol": "XAUUSD",
  "timeframe": "15",
  "event": "breakout",
  "direction": "LONG",
  "price": 4400.25,
  "rsi": 58.4,
  "adx": 27.1,
  "atr": 12.5,
  "ema20": 4392.5,
  "ema200": 4350.1,
  "timestamp": 1789052400000
}
```

El timestamp se envía como `time_close` real de TradingView en milisegundos. El volumen se calcula y queda visible en **Data Window**, pero no se transmite porque la primera versión del contrato del webhook no acepta un campo `volume`; añadirlo aquí sin cambiar el backend violaría la validación acordada.

## Prueba

1. Inicie el dashboard con `GOLDSCOUT_TRADINGVIEW_WEBHOOK_TOKEN` configurado localmente.
2. Exponga el puerto local mediante un proxy HTTPS seguro; TradingView no puede llamar directamente a `127.0.0.1`.
3. Cree una alerta en M15 y espere un evento en tiempo real confirmado.
4. Compruebe `dashboard/data/tradingview_events.jsonl` y la sección **TRADINGVIEW — OBSERVACIÓN**.
5. Repita en H1 y H4. Un gráfico M30 debe quedar marcado como no soportado y no generar alertas.

## Limitaciones

- Las alertas de scripts se disparan en tiempo real; las marcas históricas del indicador no reenvían webhooks pasados.
- Los pivots se conocen con retraso de `Pivot bars right`, necesario para evitar confirmaciones prematuras.
- La estructura es deliberadamente simple y no replica el motor completo de GoldScout.
- Volumen y calidad dependen del feed de datos elegido en TradingView.
- El input del token no es un almacén de secretos: use un token dedicado, rotatorio y transmítalo únicamente mediante HTTPS.
- Cambiar el script o sus inputs no actualiza alertas ya creadas; deben recrearse.
