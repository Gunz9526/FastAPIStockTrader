"""
Feature Importance Analyzer

Extracts and visualizes feature importance from trained ensemble models.
Supports CatBoost, LightGBM, and XGBoost with weighted averaging.

Phase H.4: Bull Regime Model Enhancement
"""
import json
import logging
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class FeatureImportanceAnalyzer:
    """
    Analyzes feature importance across ensemble models.

    Supports:
    - Individual model importance extraction
    - Weighted ensemble importance
    - JSON export for programmatic analysis
    - Visualization (optional, requires matplotlib)
    """

    def __init__(self, model_artifacts_path: str = "model_artifacts"):
        self.model_path = Path(model_artifacts_path)
        self.importance_cache: dict[str, dict[str, float]] = {}

    def extract_importance(
        self,
        ensemble_model,
        feature_names: list[str],
        model_weights: list[float] | None = None
    ) -> dict[str, float]:
        """
        Extract weighted feature importance from ensemble model.

        Args:
            ensemble_model: Trained EnsembleWrapper or VotingRegressor
            feature_names: List of feature column names
            model_weights: Weights for [catboost, lgbm, xgboost]

        Returns:
            Dictionary of {feature_name: importance_score}
        """
        if model_weights is None:
            model_weights = [1/3, 1/3, 1/3]

        importance_matrix = []

        # Extract from each estimator
        estimators = ensemble_model.named_estimators_ if hasattr(
            ensemble_model, 'named_estimators_'
        ) else {}

        for name, weight in zip(['cat', 'lgbm', 'xgb'], model_weights, strict=True):
            estimator = estimators.get(name)
            if estimator is None:
                logger.warning(f"Estimator '{name}' not found in ensemble")
                importance_matrix.append(np.zeros(len(feature_names)))
                continue

            try:
                importance = self._extract_single_model_importance(
                    estimator, name, feature_names
                )
                importance_matrix.append(importance * weight)
            except Exception as e:
                logger.error(f"Failed to extract importance from {name}: {e}")
                importance_matrix.append(np.zeros(len(feature_names)))

        # Sum weighted importances
        total_importance = np.sum(importance_matrix, axis=0)

        # Normalize to sum to 1.0
        if total_importance.sum() > 0:
            total_importance = total_importance / total_importance.sum()

        return dict(zip(feature_names, total_importance, strict=True))

    def _extract_single_model_importance(
        self,
        model,
        model_type: str,
        feature_names: list[str]
    ) -> np.ndarray:
        """Extract feature importance from a single model."""
        n_features = len(feature_names)

        if model_type == 'cat':
            # CatBoost
            if hasattr(model, 'get_feature_importance'):
                importance = model.get_feature_importance()
            elif hasattr(model, 'feature_importances_'):
                importance = model.feature_importances_
            else:
                return np.zeros(n_features)

        elif model_type == 'lgbm':
            # LightGBM
            if hasattr(model, 'feature_importances_'):
                importance = model.feature_importances_
            elif hasattr(model, 'booster_'):
                importance = model.booster_.feature_importance(importance_type='gain')
            else:
                return np.zeros(n_features)

        elif model_type == 'xgb':
            # XGBoost
            if hasattr(model, 'feature_importances_'):
                importance = model.feature_importances_
            elif hasattr(model, 'get_booster'):
                booster = model.get_booster()
                score = booster.get_score(importance_type='gain')
                importance = np.array([
                    score.get(f'f{i}', score.get(name, 0))
                    for i, name in enumerate(feature_names)
                ])
            else:
                return np.zeros(n_features)
        else:
            return np.zeros(n_features)

        # Ensure correct length
        if len(importance) != n_features:
            logger.warning(
                f"{model_type} importance length mismatch: "
                f"{len(importance)} vs {n_features}"
            )
            # Pad or truncate
            if len(importance) < n_features:
                importance = np.pad(importance, (0, n_features - len(importance)))
            else:
                importance = importance[:n_features]

        return np.array(importance, dtype=float)

    def analyze_regime_models(
        self,
        feature_names: list[str],
        regimes: list[str] | None = None
    ) -> dict[str, dict[str, float]]:
        """
        Analyze feature importance for all regime models.

        Args:
            feature_names: List of feature column names
            regimes: List of regime names (default: all 4 regimes)

        Returns:
            Dictionary of {regime: {feature: importance}}
        """
        import joblib

        if regimes is None:
            regimes = ['bull_trending', 'bear_trending',
                       'sideways_volatile', 'sideways_calm']

        results = {}

        for regime in regimes:
            model_file = self.model_path / f"ensemble_model_{regime}.pkl"
            metadata_file = self.model_path / f"ensemble_model_{regime}_metadata.json"

            if not model_file.exists():
                logger.warning(f"Model file not found: {model_file}")
                continue

            try:
                # Load model
                model = joblib.load(model_file)

                # Load weights from metadata
                weights = None
                if metadata_file.exists():
                    with open(metadata_file) as f:
                        metadata = json.load(f)
                        weights = metadata.get('weights')

                # Extract importance
                importance = self.extract_importance(model, feature_names, weights)
                results[regime] = importance
                self.importance_cache[regime] = importance

                logger.info(f"Extracted importance for {regime}: {len(importance)} features")

            except Exception as e:
                logger.error(f"Failed to analyze {regime}: {e}")

        return results

    def get_top_features(
        self,
        importance_dict: dict[str, float],
        top_n: int = 10
    ) -> list[tuple[str, float]]:
        """Get top N most important features."""
        sorted_features = sorted(
            importance_dict.items(),
            key=lambda x: x[1],
            reverse=True
        )
        return sorted_features[:top_n]

    def get_bottom_features(
        self,
        importance_dict: dict[str, float],
        bottom_n: int = 5
    ) -> list[tuple[str, float]]:
        """Get bottom N least important features (removal candidates)."""
        sorted_features = sorted(
            importance_dict.items(),
            key=lambda x: x[1]
        )
        return sorted_features[:bottom_n]

    def compare_regimes(
        self,
        regime_importance: dict[str, dict[str, float]]
    ) -> pd.DataFrame:
        """
        Compare feature importance across regimes.

        Returns:
            DataFrame with features as rows, regimes as columns
        """
        df = pd.DataFrame(regime_importance)
        df = df.sort_values(by=list(df.columns), ascending=False)
        return df

    def save_analysis(
        self,
        regime_importance: dict[str, dict[str, float]],
        output_dir: str = "model_artifacts"
    ) -> str:
        """
        Save analysis results to JSON file.

        Args:
            regime_importance: Analysis results
            output_dir: Output directory

        Returns:
            Path to saved file
        """
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        # Prepare export data
        export_data = {
            'analysis_date': pd.Timestamp.now().isoformat(),
            'regimes': {},
            'summary': {}
        }

        for regime, importance in regime_importance.items():
            top_features = self.get_top_features(importance, 10)
            bottom_features = self.get_bottom_features(importance, 5)

            export_data['regimes'][regime] = {
                'all_features': importance,
                'top_10': dict(top_features),
                'bottom_5': dict(bottom_features)
            }

        # Cross-regime summary
        if regime_importance:
            all_features = list(next(iter(regime_importance.values())).keys())
            avg_importance = {}
            for feature in all_features:
                values = [
                    regime_importance[r].get(feature, 0)
                    for r in regime_importance
                ]
                avg_importance[feature] = sum(values) / len(values)

            export_data['summary']['average_importance'] = dict(
                sorted(avg_importance.items(), key=lambda x: x[1], reverse=True)
            )

        # Save to file
        output_file = output_path / 'feature_importance_analysis.json'
        with open(output_file, 'w') as f:
            json.dump(export_data, f, indent=2)

        logger.info(f"Analysis saved to {output_file}")
        return str(output_file)

    def generate_report(
        self,
        regime_importance: dict[str, dict[str, float]]
    ) -> str:
        """
        Generate human-readable analysis report.

        Returns:
            Markdown-formatted report string
        """
        lines = [
            "# Feature Importance Analysis Report",
            f"**Generated:** {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')}",
            "",
        ]

        for regime, importance in regime_importance.items():
            lines.append(f"## {regime.replace('_', ' ').title()}")
            lines.append("")

            top = self.get_top_features(importance, 10)
            lines.append("### Top 10 Features")
            lines.append("| Rank | Feature | Importance |")
            lines.append("|------|---------|------------|")
            for i, (feat, imp) in enumerate(top, 1):
                lines.append(f"| {i} | `{feat}` | {imp:.4f} |")
            lines.append("")

            bottom = self.get_bottom_features(importance, 5)
            lines.append("### Bottom 5 Features (Removal Candidates)")
            lines.append("| Feature | Importance |")
            lines.append("|---------|------------|")
            for feat, imp in bottom:
                lines.append(f"| `{feat}` | {imp:.4f} |")
            lines.append("")

        return "\n".join(lines)


def analyze_feature_importance_for_regime(
    regime: str,
    feature_names: list[str],
    output_dir: str = "model_artifacts"
) -> dict[str, Any]:
    """
    Convenience function to analyze a single regime.

    Args:
        regime: Regime name (e.g., 'bull_trending')
        feature_names: List of feature names
        output_dir: Directory containing model files

    Returns:
        Dictionary with importance analysis
    """
    analyzer = FeatureImportanceAnalyzer(output_dir)
    results = analyzer.analyze_regime_models(feature_names, [regime])

    if regime in results:
        return {
            'regime': regime,
            'importance': results[regime],
            'top_10': analyzer.get_top_features(results[regime], 10),
            'bottom_5': analyzer.get_bottom_features(results[regime], 5)
        }

    return {'regime': regime, 'error': 'Model not found'}
