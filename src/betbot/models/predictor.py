"""Wrapper simétrico de modelos y utilidades de matriz de features.

SymmetricModel garantiza p(A,B) = 1 - p(B,A) EXACTAMENTE promediando la
predicción con la de los bandos intercambiados (las features DIFF y el logit de
mercado se niegan; las CTX se conservan).

Para mercados por-jugador no simétricos (gana >=1 set, gana 2-0) el mismo
modelo se evalúa desde la perspectiva de cada jugador: perspective="a" usa x,
perspective="b" usa swap(x).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression

from betbot.features.builder import CTX_FEATURES, DIFF_FEATURES, MARKET_FEATURE, assert_whitelist


def make_matrix(df: pd.DataFrame, features: list[str]) -> np.ndarray:
    assert_whitelist(features)
    return df[features].to_numpy(dtype=float)


def swap_matrix(X: np.ndarray, features: list[str]) -> np.ndarray:
    """Niega las columnas antisimétricas (DIFF + market_logit); conserva CTX."""
    Xs = X.copy()
    anti = {f: i for i, f in enumerate(features) if f in DIFF_FEATURES or f == MARKET_FEATURE}
    for _, i in anti.items():
        Xs[:, i] = -Xs[:, i]
    return Xs


class SymmetricModel:
    """Modelo binario con salida simétrica exacta bajo intercambio A/B."""

    def __init__(self, features: list[str], C: float = 1.0) -> None:
        assert_whitelist(features)
        self.features = list(features)
        self.lr = LogisticRegression(C=C, solver="lbfgs", max_iter=2000)

    def fit(self, df: pd.DataFrame, y: np.ndarray) -> "SymmetricModel":
        X = make_matrix(df, self.features)
        # augmentación con bandos intercambiados: fuerza coeficientes simétricos
        Xs = swap_matrix(X, self.features)
        X2 = np.vstack([X, Xs])
        y2 = np.concatenate([y, 1 - y]).astype(int)
        self.lr.fit(X2, y2)
        return self

    def predict(self, df: pd.DataFrame) -> np.ndarray:
        X = make_matrix(df, self.features)
        p_fwd = self.lr.predict_proba(X)[:, 1]
        p_bwd = self.lr.predict_proba(swap_matrix(X, self.features))[:, 1]
        return 0.5 * (p_fwd + (1.0 - p_bwd))


class PerspectiveModel:
    """Modelo por-jugador (target no simétrico, p.ej. 'gana >=1 set').

    Se entrena con el target referido al jugador A; la predicción para B usa el
    swap de features. No se promedia (el evento de B no es el complementario)."""

    def __init__(self, features: list[str], C: float = 1.0) -> None:
        assert_whitelist(features)
        self.features = list(features)
        self.lr = LogisticRegression(C=C, solver="lbfgs", max_iter=2000)

    def fit(self, df: pd.DataFrame, y_a: np.ndarray, y_b: np.ndarray | None = None) -> "PerspectiveModel":
        X = make_matrix(df, self.features)
        if y_b is not None:
            # augmentación: el mismo partido visto desde B duplica la muestra
            Xs = swap_matrix(X, self.features)
            X = np.vstack([X, Xs])
            y_a = np.concatenate([y_a, y_b])
        self.lr.fit(X, y_a.astype(int))
        return self

    def predict(self, df: pd.DataFrame, perspective: str = "a") -> np.ndarray:
        X = make_matrix(df, self.features)
        if perspective == "b":
            X = swap_matrix(X, self.features)
        return self.lr.predict_proba(X)[:, 1]


class SymmetricEventModel:
    """Evento simétrico del partido (p.ej. 'habrá 3 sets'): f(x) y f(swap(x))
    predicen lo MISMO, así que se promedian para exactitud simétrica."""

    def __init__(self, features: list[str], C: float = 1.0) -> None:
        assert_whitelist(features)
        self.features = list(features)
        self.lr = LogisticRegression(C=C, solver="lbfgs", max_iter=2000)

    def fit(self, df: pd.DataFrame, y: np.ndarray) -> "SymmetricEventModel":
        X = make_matrix(df, self.features)
        Xs = swap_matrix(X, self.features)
        self.lr.fit(np.vstack([X, Xs]), np.concatenate([y, y]).astype(int))
        return self

    def predict(self, df: pd.DataFrame) -> np.ndarray:
        X = make_matrix(df, self.features)
        p1 = self.lr.predict_proba(X)[:, 1]
        p2 = self.lr.predict_proba(swap_matrix(X, self.features))[:, 1]
        return 0.5 * (p1 + p2)
