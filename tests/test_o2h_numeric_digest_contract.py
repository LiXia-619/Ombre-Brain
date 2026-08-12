import json
from pathlib import Path

import pytest

from tools.recall_digest import (
    DIGEST_PROFILE,
    canonical_json,
    normalize_nullable_affect,
    sha256,
)


VECTORS = json.loads(
    (Path(__file__).parent / "fixtures" / "o2h_numeric_digest_vectors.json").read_text(
        encoding="utf-8"
    )
)


def test_shared_python_typescript_numeric_digest_vectors():
    assert DIGEST_PROFILE == "ombre-fixed6-numeric-v1"
    assert len(VECTORS) == 4
    for vector in VECTORS:
        assert canonical_json(vector["value"]) == vector["canonical"], vector["id"]
        assert sha256(vector["value"]) == vector["sha256"], vector["id"]


def test_affect_normalization_is_bounded_and_erases_negative_zero():
    assert normalize_nullable_affect(-0.0, field="valence") == 0.0
    assert canonical_json({"affect": {"valence": -0.0}}) == (
        '{"affect":{"valence":0.000000}}'
    )
    with pytest.raises(ValueError, match="protocol range"):
        normalize_nullable_affect(1.000001, field="arousal")
    with pytest.raises(ValueError, match="finite number"):
        normalize_nullable_affect(True, field="valence")


def test_non_profile_protocol_numbers_remain_safe_integers():
    with pytest.raises(ValueError, match="safe integers"):
        canonical_json({"estimated_tokens": 0.5})
    with pytest.raises(ValueError, match="safe integers"):
        canonical_json({"count": 9_007_199_254_740_992})
