"""The Gamma 3.1 frozen label file parses and carries every setting the plan relies on."""
from pathlib import Path

import yaml

LABELS = Path(__file__).resolve().parents[1] / "gamma" / "labels_v1.yaml"


def test_frozen_labels_are_complete():
    cfg = yaml.safe_load(LABELS.read_text(encoding="utf-8"))
    assert cfg["label_version"] == "gamma-labels-v1"
    lab = cfg["lasting_14"]
    assert lab["eligibility"]["window"] == [14, 27]
    assert lab["eligibility"]["min_valid_days_in_window"] == 10
    assert set(cfg["measured_lanes"]) == {"unbiased_rank", "unbiased_counter", "panel"}
    assert set(cfg["baselines"]) == {"B0", "B1", "B2", "B3"}
    assert cfg["evaluation"]["embargo_days"] >= lab["eligibility"]["window"][1] + 1
    assert set(cfg["features"]["participation_features"]) <= set(cfg["features"]["descriptors"])
    assert cfg["abstain"]["topic_drift_cosine_to_birth_centroid_below"] == 0.70
