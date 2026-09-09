# GoldScout v1.7.2 — paquete completo

## Dashboard
1. Ejecuta `install_news.bat` una primera vez para instalar las dependencias.
2. Para usos posteriores puedes ejecutar directamente `run_dashboard.bat`.
3. Abre `http://127.0.0.1:8787`.

El servidor consulta noticias inmediatamente al arrancar y luego periódicamente. Si una fuente falla, el resto puede continuar. Desconexiones/cancelaciones del navegador ya no generan tracebacks molestos.

## MetaTrader 5
Copia `MT5/Experts/XAU_GoldScout_H1.mq5` a la carpeta `MQL5/Experts` de MT5.
Ábrelo en MetaEditor y pulsa F7 para compilar.
Colócalo en `XAUUSD`, timeframe `H1`.

### Seguridad
`EnableLiveTrading = false` por defecto. Mantén PAPER MODE mientras validas.

## Riesgo
El EA usa 5% del equity como riesgo objetivo. En una cuenta de $1,000 son $50; en una cuenta de $200 son $10.

## Diagnóstico
- `test_news.bat` prueba el motor de noticias y muestra cuántas fuentes respondieron.
- El dashboard expone `/api/health` para diagnóstico básico.
