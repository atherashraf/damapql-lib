from typing import Iterable, Dict, Optional, Union, List
import numpy as np
import pandas as pd
from geopandas import GeoDataFrame
from sklearn.preprocessing import StandardScaler
from sklearn.decomposition import PCA

InvertSpec = Union[Iterable[str], Dict[str, bool]]

class PCAAnalysis:
    """
    Minimal PCA-based ranking:
      - Z-score standardization
      - Optional inversion per column
      - Score strategy: 'pc1' | 'weighted' | 'auto' (PC1 if >=40% var else weighted)
    """

    def __init__(self, cols, invert=None, n_components=None, combine="auto",
                 rank_method="dense", risk_classes=5,
                 feature_weights: Optional[Dict[str, float]] = None):
        self.cols = cols
        self.invert_map = {} if invert is None else (
            {k: bool(v) for k, v in invert.items()} if isinstance(invert, dict) else {c: True for c in invert})
        self.n_components = n_components
        self.combine = combine
        self.rank_method = rank_method
        self.risk_classes = risk_classes
        self.feature_weights = feature_weights or {}  # <-- NEW
        self.scaler_ = None
        self.pca_ = None
        self.explained_variance_ratio_ = None

    def _validate(self, df: pd.DataFrame):
        missing = [c for c in self.cols if c not in df.columns]
        if missing:
            raise KeyError(f"Missing required columns: {missing}")

    def _prepare(self, df, fit: bool):
        X = df[self.cols].copy()
        for c in self.cols:
            if self.invert_map.get(c, False):
                X[c] = -1 * X[c]
        X = X.apply(lambda s: s.fillna(s.median()), axis=0)

        if fit:
            from sklearn.preprocessing import StandardScaler
            self.scaler_ = StandardScaler()
            Z = self.scaler_.fit_transform(X.values)
        else:
            Z = self.scaler_.transform(X.values)

        # ---- NEW: apply variance weights (post Z-score) ----
        if self.feature_weights:
            col_index = {c: i for i, c in enumerate(self.cols)}
            for c, w in self.feature_weights.items():
                if c in col_index and w is not None and w > 0:
                    Z[:, col_index[c]] *= np.sqrt(w)  # w is variance multiplier
        return Z

    def fit(self, df: pd.DataFrame):
        self._validate(df)
        Z = self._prepare(df, fit=True)
        self.pca_ = PCA(n_components=self.n_components, random_state=42)
        self.pca_.fit(Z)
        self.explained_variance_ratio_ = self.pca_.explained_variance_ratio_
        return self

    def _score(self, PCs: np.ndarray) -> np.ndarray:
        vr = self.explained_variance_ratio_
        if self.combine == "pc1":
            return PCs[:, 0]
        if self.combine == "weighted":
            return (PCs * vr[:PCs.shape[1]]).sum(axis=1)
        # auto
        return PCs[:, 0] if vr[0] >= 0.40 else (PCs * vr[:PCs.shape[1]]).sum(axis=1)

    def transform(self, df: pd.DataFrame) -> pd.DataFrame:
        self._validate(df)
        Z = self._prepare(df, fit=False)
        PCs = self.pca_.transform(Z)
        out = df.copy()
        out["RiskIndex"] = self._score(PCs)
        out["RiskPercentile"] = out["RiskIndex"].rank(pct=True)
        out["RiskRank"] = out["RiskIndex"].rank(ascending=False, method=self.rank_method).astype(int)
        # labels = [f"Class {i}" for i in range(1, self.risk_classes + 1)]
        labels = [int(i) for i in range(1, self.risk_classes + 1)]
        out["RiskClass"] = pd.qcut(out["RiskPercentile"], q=self.risk_classes, labels=labels)
        for i in range(min(PCs.shape[1], 4)):  # keep first few PCs for inspection
            out[f"PC{i+1}"] = PCs[:, i]
        return out

    def fit_transform(self, df: pd.DataFrame) -> pd.DataFrame:
        self.fit(df)
        return self.transform(df)



if __name__ == '__main__':
    cols = ["distance_from_river_km", "flood_frequency", "runoff_coefficient",
            "runoff_volume_2rp", "spi", "twi"]

    # Option A: list/array for invert
    an = PCAAnalysis(cols=cols, invert=["distance_from_river_km"], combine="auto")

    # Option B: dict for invert
    # an = PCAAnalysis(cols=cols, invert={"distance_from_river_km": True}, combine="auto")
    gdf = GeoDataFrame.from_file("data/village_ranking/village_ranking.geojson")
    # gdf_ranked = an.fit_transform(gdf)  # GeoDataFrame or DataFrame
    an = PCAAnalysis(
        cols=["distance_from_river_km", "flood_frequency", "runoff_coefficient", "runoff_volume_2rp", "spi", "twi"],
        invert=["distance_from_river_km"],
        combine="auto",
        risk_classes=10,
        n_components=None,  # keep all and let 'combine' decide scoring
        ffeature_weights={"flood_frequency": 2.0},
    )

    out = an.fit_transform(gdf.copy())
    # Inspect
    print("Explained variance ratio:", an.explained_variance_ratio_)
    print("Loadings:\n", an.loadings_.round(3))

    # Plots (optional)
    # an.plot_scree()
    # an.plot_biplot(scale_vectors=1.2)
