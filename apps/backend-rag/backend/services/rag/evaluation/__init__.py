"""
Nuzantara RAG - Evaluation and A/B Testing Module

This module provides A/B testing capabilities, metrics tracking, RAGAS
evaluation and benchmarking for the RAG system.

Components:
- ABTestManager: Core A/B testing logic with statistical significance
- MetricsTracker: Persistent storage for experiment metrics

Usage:
    from backend.services.rag.evaluation import (
        ABTestManager,
        MetricsTracker,
    )

    # A/B Testing
    ab_manager = ABTestManager()
    variant = ab_manager.assign_variant(user_id="user123", experiment="hybrid_vs_dense")

    # Record metrics
    await metrics_tracker.record_metric(
        experiment="hybrid_vs_dense",
        variant=variant,
        metric="ctr",
        value=1.0,
        user_id="user123",
        query_id="query456"
    )
"""

from backend.services.rag.evaluation.ab_testing import (
    ABTestManager,
    ExperimentConfig,
    Variant,
)
from backend.services.rag.evaluation.benchmark import (
    BenchmarkResult,
    RAGBenchmark,
    run_weekly_benchmark,
)
from backend.services.rag.evaluation.metrics_tracker import (
    MetricsTracker,
    QueryMetric,
)
from backend.services.rag.evaluation.ragas_evaluator import (
    EvaluationResult,
    RAGASEvaluator,
    get_ragas_evaluator,
)

__all__ = [
    # A/B Testing
    "ABTestManager",
    "BenchmarkResult",
    "EvaluationResult",
    "ExperimentConfig",
    # Metrics Tracking
    "MetricsTracker",
    "QueryMetric",
    # RAGAS Evaluation
    "RAGASEvaluator",
    # Benchmark
    "RAGBenchmark",
    "Variant",
    "get_ragas_evaluator",
    "run_weekly_benchmark",
]
