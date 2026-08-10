"""MODEL LAB — laboratorio de modelos SEPARADO del runtime productivo.

El modelo actual (bundle en artifacts/model_bundle.joblib) queda CONGELADO como
CHAMPION. Aquí se construyen datasets point-in-time, validación temporal seria,
métricas, calibración y challengers; NADA de este paquete se importa desde
`betbot scan`, el screener ni el value engine.

Reglas del laboratorio:
- Ningún challenger llega a producción sin promoción EXPLÍCITA del usuario tras
  el criterio pre-registrado (docs/MODEL_LAB_FASE1.md §11).
- El año de test sellado (splits.test_year) está protegido por SealedTestGuard:
  el laboratorio se niega a servir sus filas salvo desbloqueo explícito, y cada
  desbloqueo queda registrado.
- Artefactos propios en artifacts/model_lab/ — jamás se escribe sobre
  model_bundle.joblib ni features_all.parquet.
"""
