# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import ast
import math
from pathlib import Path

import pytest

from agvm_api.brain_profile_v1 import (
    BRAIN_PROFILE_V1_SCHEMA_VERSION,
    CANONICAL_ROUTING_FIELDS,
    DIMENSION_COUNT,
    MAX_DIAGONAL_WEIGHT,
    MAX_DISPLAY_COEFFICIENT_ABS,
    MAX_LOW_RANK_INTENSITY,
    MAX_LOW_RANK_FACTOR_ABS,
    MAX_METRIC_CONDITION_NUMBER,
    MAX_RERANK_INTENSITY,
    MIN_DIAGONAL_WEIGHT,
    BrainProfileValidationError,
    build_brain_profile_v1,
    build_psd_metric_matrix,
    load_brain_profile_v1,
    score_brain_profile_v1,
)
from agvm_api.config import ROUTING_A, ROUTING_FIELDS


def _factor(offset: int = 0) -> tuple[float, ...]:
    return tuple((((index + offset) % 5) - 2) / 2.0 for index in range(DIMENSION_COUNT))


def _assert_structured(exc: BrainProfileValidationError, *, code: str, field: str) -> None:
    payload = exc.as_dict()
    assert payload["schema_version"] == "agvm.brain_profile_validation_error.v1"
    assert payload["code"] == code
    assert payload["field"] == field
    assert payload["message"]
    assert isinstance(payload["details"], dict)


def test_default_profile_uses_current_canonical_contract_and_integrity_fields() -> None:
    profile = build_brain_profile_v1()

    assert profile.schema_version == BRAIN_PROFILE_V1_SCHEMA_VERSION
    assert profile.routing_fields == tuple(ROUTING_FIELDS) == CANONICAL_ROUTING_FIELDS
    assert len(profile.routing_fields) == 12
    assert profile.diagonal_weights == (1.0,) * 12
    assert profile.low_rank_factors == ()
    assert profile.low_rank_intensity == 0.0
    assert profile.rerank_intensity == 0.0
    assert profile.display_projection == tuple(tuple(float(value) for value in row) for row in ROUTING_A)
    assert len(profile.display_projection) == 3
    assert all(len(row) == 12 for row in profile.display_projection)
    assert profile.revision == 1
    assert profile.revision_id.startswith("brain-profile-v1:r1:")
    assert profile.checksum.startswith("sha256:")
    assert profile.signature.startswith("sha256:")
    assert profile.shadow is True


def test_builder_is_deterministic_and_revision_and_shadow_are_signed() -> None:
    kwargs = {
        "diagonal_weights": tuple(1.0 + index / 20.0 for index in range(12)),
        "low_rank_factors": (_factor(0), _factor(1)),
        "low_rank_intensity": 0.25,
        "rerank_intensity": 0.2,
        "revision": 7,
        "shadow": False,
    }
    first = build_brain_profile_v1(**kwargs)
    second = build_brain_profile_v1(**kwargs)
    shadow_variant = build_brain_profile_v1(**{**kwargs, "shadow": True})
    revision_variant = build_brain_profile_v1(**{**kwargs, "revision": 8})

    assert first == second
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert first.checksum != shadow_variant.checksum
    assert first.signature != shadow_variant.signature
    assert first.revision_id != revision_variant.revision_id
    assert first.checksum != revision_variant.checksum


def test_metric_builder_is_symmetric_and_strictly_positive_definite() -> None:
    diagonal = tuple(0.25 + index / 10.0 for index in range(12))
    factors = tuple(_factor(offset) for offset in range(4))
    matrix = build_psd_metric_matrix(diagonal, factors, MAX_LOW_RANK_INTENSITY)

    assert len(matrix) == 12
    assert all(len(row) == 12 for row in matrix)
    for row in range(12):
        for column in range(12):
            assert matrix[row][column] == pytest.approx(matrix[column][row])

    probes = [
        tuple(1.0 if index == axis else 0.0 for index in range(12))
        for axis in range(12)
    ] + [
        tuple(((-1.0) ** (index + seed)) * (index + 1) / 12.0 for index in range(12))
        for seed in range(6)
    ]
    for vector in probes:
        quadratic = sum(
            vector[row] * matrix[row][column] * vector[column]
            for row in range(12)
            for column in range(12)
        )
        assert quadratic > 0.0


def test_low_rank_correction_matches_outer_product_definition() -> None:
    factor = (1.0, -1.0) + (0.0,) * 10
    matrix = build_psd_metric_matrix((1.0,) * 12, (factor,), 0.2)

    assert matrix[0][0] == pytest.approx(1.2)
    assert matrix[1][1] == pytest.approx(1.2)
    assert matrix[0][1] == pytest.approx(-0.2)
    assert matrix[1][0] == pytest.approx(-0.2)
    assert matrix[2][2] == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("kwargs", "code", "field"),
    [
        ({"routing_fields": tuple(reversed(ROUTING_FIELDS))}, "brain_profile_v1_routing_fields_mismatch", "routing_fields"),
        ({"diagonal_weights": ()}, "brain_profile_v1_invalid_vector_length", "diagonal_weights"),
        ({"diagonal_weights": (1.0,) * 11}, "brain_profile_v1_invalid_vector_length", "diagonal_weights"),
        ({"diagonal_weights": (1.0,) * 11 + (0.0,)}, "brain_profile_v1_non_positive_diagonal", "diagonal_weights[11]"),
        ({"diagonal_weights": (1.0,) * 11 + (-0.1,)}, "brain_profile_v1_non_positive_diagonal", "diagonal_weights[11]"),
        ({"diagonal_weights": (1.0,) * 11 + (math.nan,)}, "brain_profile_v1_non_finite_number", "diagonal_weights[11]"),
        ({"diagonal_weights": (1.0,) * 11 + (MIN_DIAGONAL_WEIGHT / 2,)}, "brain_profile_v1_diagonal_out_of_bounds", "diagonal_weights[11]"),
        ({"diagonal_weights": (1.0,) * 11 + (MAX_DIAGONAL_WEIGHT + 0.01,)}, "brain_profile_v1_diagonal_out_of_bounds", "diagonal_weights[11]"),
        ({"low_rank_factors": (_factor(0),) * 5}, "brain_profile_v1_rank_exceeded", "low_rank_factors"),
        ({"low_rank_factors": ((1.0,) * 11,)}, "brain_profile_v1_invalid_vector_length", "low_rank_factors[0]"),
        ({"low_rank_factors": ((1.0,) * 11 + (math.inf,),)}, "brain_profile_v1_non_finite_number", "low_rank_factors[0][11]"),
        ({"low_rank_factors": ((MAX_LOW_RANK_FACTOR_ABS + 0.01,) + (0.0,) * 11,)}, "brain_profile_v1_coefficient_out_of_bounds", "low_rank_factors[0][0]"),
        ({"low_rank_intensity": -0.01}, "brain_profile_v1_intensity_out_of_bounds", "low_rank_intensity"),
        ({"low_rank_factors": (_factor(),), "low_rank_intensity": 0.350001}, "brain_profile_v1_intensity_out_of_bounds", "low_rank_intensity"),
        ({"low_rank_intensity": 0.1}, "brain_profile_v1_intensity_without_factors", "low_rank_intensity"),
        ({"rerank_intensity": -0.01}, "brain_profile_v1_rerank_intensity_out_of_bounds", "rerank_intensity"),
        ({"rerank_intensity": MAX_RERANK_INTENSITY + 0.000001}, "brain_profile_v1_rerank_intensity_out_of_bounds", "rerank_intensity"),
        ({"display_projection": ((1.0,) * 12,) * 2}, "brain_profile_v1_invalid_projection_rows", "display_projection"),
        ({"display_projection": ()}, "brain_profile_v1_invalid_projection_rows", "display_projection"),
        ({"display_projection": ((1.0,) * 12, (1.0,) * 11, (1.0,) * 12)}, "brain_profile_v1_invalid_vector_length", "display_projection[1]"),
        ({"display_projection": ((1.0,) * 12, (0.0,) * 12, (1.0,) * 12)}, "brain_profile_v1_zero_projection_axis", "display_projection[1]"),
        ({"display_projection": ((MAX_DISPLAY_COEFFICIENT_ABS + 0.01,) + (0.0,) * 11, (0.0, 1.0) + (0.0,) * 10, (0.0, 0.0, 1.0) + (0.0,) * 9)}, "brain_profile_v1_coefficient_out_of_bounds", "display_projection[0][0]"),
        ({"revision": 0}, "brain_profile_v1_invalid_revision", "revision"),
        ({"revision": 1.5}, "brain_profile_v1_invalid_revision", "revision"),
        ({"shadow": "yes"}, "brain_profile_v1_invalid_shadow_flag", "shadow"),
    ],
)
def test_builder_rejects_invalid_profiles_structurally(
    kwargs: dict[str, object],
    code: str,
    field: str,
) -> None:
    with pytest.raises(BrainProfileValidationError) as captured:
        build_brain_profile_v1(**kwargs)
    _assert_structured(captured.value, code=code, field=field)


def test_metric_condition_number_is_conservatively_bounded() -> None:
    factors = ((MAX_LOW_RANK_FACTOR_ABS,) * DIMENSION_COUNT,)
    with pytest.raises(BrainProfileValidationError) as captured:
        build_brain_profile_v1(
            diagonal_weights=(MIN_DIAGONAL_WEIGHT,) * 11 + (MAX_DIAGONAL_WEIGHT,),
            low_rank_factors=factors,
            low_rank_intensity=MAX_LOW_RANK_INTENSITY,
        )
    _assert_structured(
        captured.value,
        code="brain_profile_v1_metric_condition_exceeded",
        field="metric_matrix",
    )
    assert captured.value.details["maximum"] == MAX_METRIC_CONDITION_NUMBER


@pytest.mark.parametrize("field", ["revision_id", "checksum", "signature"])
def test_loader_rejects_each_integrity_tamper_structurally(field: str) -> None:
    payload = build_brain_profile_v1(
        low_rank_factors=(_factor(),),
        low_rank_intensity=0.2,
    ).model_dump(mode="json")
    payload[field] = "sha256:" + "0" * 64 if field != "revision_id" else "brain-profile-v1:r1:tampered"

    with pytest.raises(BrainProfileValidationError) as captured:
        load_brain_profile_v1(payload)
    assert captured.value.code == "brain_profile_v1_invalid"
    assert "integrity" in captured.value.message.lower() or "integrity" in str(captured.value.details).lower()


def test_loader_rejects_extra_fields_and_noncanonical_shapes() -> None:
    payload = build_brain_profile_v1().model_dump(mode="json")
    payload["unexpected"] = True

    with pytest.raises(BrainProfileValidationError) as captured:
        load_brain_profile_v1(payload)
    assert captured.value.code == "brain_profile_v1_invalid"
    assert captured.value.as_dict()["details"]["errors"]


def test_json_round_trip_preserves_the_exact_signed_revision() -> None:
    profile = build_brain_profile_v1(
        diagonal_weights=tuple(0.5 + index / 20.0 for index in range(12)),
        low_rank_factors=(_factor(1), _factor(3), _factor(4)),
        low_rank_intensity=0.3,
        revision=11,
        shadow=False,
    )

    loaded = load_brain_profile_v1(profile.model_dump(mode="json"))

    assert loaded == profile
    assert loaded.revision_id == profile.revision_id
    assert loaded.checksum == profile.checksum
    assert loaded.signature == profile.signature


def test_score_is_normalized_deterministic_and_supports_sequences_and_mappings() -> None:
    profile = build_brain_profile_v1(
        diagonal_weights=tuple(1.0 + index / 10.0 for index in range(12)),
        low_rank_factors=(_factor(0), _factor(2)),
        low_rank_intensity=0.35,
    )
    left = tuple((index + 1) / 12.0 for index in range(12))
    right = tuple((12 - index) / 12.0 for index in range(12))
    left_mapping = dict(zip(ROUTING_FIELDS, left))
    right_mapping = dict(zip(ROUTING_FIELDS, right))

    score = score_brain_profile_v1(profile, left, right)
    assert 0.0 <= score <= 1.0
    assert score == pytest.approx(profile.score(left_mapping, right_mapping))
    assert profile.score(left, left) == pytest.approx(1.0)
    assert profile.score((0.0,) * 12, right) == 0.0
    assert profile.score(left, tuple(-value for value in left)) == 0.0

    for seed in range(20):
        first = tuple(math.sin(seed + index) for index in range(12))
        second = tuple(math.cos(seed * 0.5 + index) for index in range(12))
        assert 0.0 <= profile.score(first, second) <= 1.0


def test_score_and_projection_reject_invalid_vectors_structurally() -> None:
    profile = build_brain_profile_v1()

    with pytest.raises(BrainProfileValidationError) as short_score:
        profile.score((1.0,) * 11, (1.0,) * 12)
    _assert_structured(
        short_score.value,
        code="brain_profile_v1_invalid_vector_length",
        field="left",
    )

    with pytest.raises(BrainProfileValidationError) as bad_mapping:
        profile.score({ROUTING_FIELDS[0]: 1.0}, {field: 1.0 for field in ROUTING_FIELDS})
    _assert_structured(
        bad_mapping.value,
        code="brain_profile_v1_score_fields_mismatch",
        field="left",
    )

    with pytest.raises(BrainProfileValidationError) as non_finite:
        profile.project((1.0,) * 11 + (math.inf,))
    _assert_structured(
        non_finite.value,
        code="brain_profile_v1_non_finite_number",
        field="vector[11]",
    )


def test_display_projection_is_exactly_three_by_twelve_and_deterministic() -> None:
    projection = (
        (1.0,) + (0.0,) * 11,
        (0.0, 1.0) + (0.0,) * 10,
        (0.0, 0.0, 1.0) + (0.0,) * 9,
    )
    profile = build_brain_profile_v1(display_projection=projection)
    vector = tuple(float(index + 1) for index in range(12))

    assert profile.project(vector) == (1.0, 2.0, 3.0)
    assert profile.project(dict(zip(ROUTING_FIELDS, vector))) == (1.0, 2.0, 3.0)


def test_module_has_no_forbidden_architecture_imports() -> None:
    source_path = Path(__file__).resolve().parents[1] / "agvm_api" / "brain_profile_v1.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported_modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.append(node.module)

    assert all("control_plane" not in module for module in imported_modules)
    assert all(not module.lower().endswith("v2") for module in imported_modules)
