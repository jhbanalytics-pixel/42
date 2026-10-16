# Gamma 3.1

Research and design package for the release after Beta. Start with [GAMMA-3.1-PLAN.md](GAMMA-3.1-PLAN.md).

The product review's screenshots (fixture data only) are kept in the project's shared folder, 42-gamma/research/screens, rather than in the repo. The prototype in `prototype/` uses illustrative data only.

Code that belongs to this package:
- `core/setup/history_audit.py`: read-only BigQuery history audit for Albert's PC (aggregates only).
- `core/eval/gamma/labels_v1.yaml`: frozen labels, features, baselines, evaluation and abstention settings.
