# GoldScout — instrucciones del agente

## Objetivo
Mantener y mejorar el sistema GoldScout para XAUUSD H1, incluyendo EA de MetaTrader 5, dashboard, noticias/macro, sesiones de mercado y monitoreo intrabar.

## Reglas críticas
- No activar trading real automáticamente.
- No aumentar el riesgo configurado sin autorización explícita del propietario.
- Mantener PAPER/SAFE mode durante desarrollo y validación.
- No modificar credenciales, API keys, tokens o secretos.
- No subir secretos al repositorio.
- No eliminar filtros de riesgo para aumentar la frecuencia de operaciones.
- Toda modificación debe conservar compatibilidad entre EA, dashboard y servicios.
- XAUUSD es el instrumento principal y H1 el marco de decisión.

## Desarrollo
1. Revisar la implementación existente antes de modificarla.
2. Identificar la causa raíz.
3. Cambiar lo mínimo necesario.
4. Ejecutar pruebas/validaciones disponibles.
5. Documentar cambios importantes.
6. Crear commits descriptivos o PRs.

## Trading
- El monitoreo intrabar puede reevaluar una vela H1 mientras está abierta.
- Máximo una entrada por vela H1.
- El riesgo actual objetivo es 5% del equity, pero no debe elevarse automáticamente.
- SL basado en ATR + estructura; TP adaptativo según estructura/RR.
- Noticias y macro son contexto y deben influir direccionalmente cuando la evidencia lo justifique.
- Asia debe tratarse por centros individuales (Tokio, Seúl, Shanghái, Hong Kong, Singapur, Mumbai y Dubái), además de Londres, Nueva York/COMEX y ventanas LBMA.
- Los horarios de interfaz deben mostrarse en Colombia (UTC-5), respetando DST cuando corresponda.
- Una apertura fuerte no implica automáticamente un pullback.

## Seguridad
El agente puede analizar, probar y modificar código. No puede convertir el sistema a trading real ni aumentar parámetros de riesgo sin autorización explícita.

## Eficiencia de contexto
- No releer archivos no relacionados con la tarea actual.
- No repetir arquitectura, reglas o contexto ya conocido.
- Preferir cambios pequeños y diffs mínimos.
- Limitar la investigación al alcance solicitado.
- No refactorizar código fuera del problema actual.
- Leer primero AGENTS.md y luego solo los archivos estrictamente necesarios.
- Después de cada cambio, reportar solo: causa raíz, archivos modificados, pruebas y resultado.
- Si aparece un problema no relacionado, mencionarlo brevemente pero no corregirlo sin autorización.
