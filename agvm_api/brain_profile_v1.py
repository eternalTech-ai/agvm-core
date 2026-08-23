# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

"""Bounded, deterministic semantic profile for the current AGVM routing space."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

try:  # Support package imports and the API's direct-module runtime.
    from .config import ROUTING_A, ROUTING_FIELDS
except ImportError:  # pragma: no cover - exercised by the direct API runtime.
    from config import ROUTING_A, ROUTING_FIELDS


BRAIN_PROFILE_V1_SCHEMA_VERSION = "agvm.brain_profile.v1"
BRAIN_PROFILE_V1_SIGNATURE_DOMAIN = "agvm.brain_profile.v1.signature"
CANONICAL_ROUTING_FIELDS = tuple(ROUTING_FIELDS)
DIMENSION_COUNT = 12
DISPLAY_DIMENSION_COUNT = 3
MAX_LOW_RANK = 4
MAX_LOW_RANK_INTENSITY = 0.35
MAX_RERANK_INTENSITY = 0.35
MIN_DIAGONAL_WEIGHT = 0.05
MAX_DIAGONAL_WEIGHT = 20.0
MAX_LOW_RANK_FACTOR_ABS = 4.0
MAX_DISPLAY_COEFFICIENT_ABS = 4.0
MAX_METRIC_CONDITION_NUMBER = 500.0

if len(CANONICAL_ROUTING_FIELDS) != DIMENSION_COUNT:  # pragma: no cover - import-time invariant.
    raise RuntimeError("brain_profile_v1_requires_exactly_12_routing_fields")


class BrainProfileValidationError(ValueError):
    """Stable structured failure for profile construction, loading, and scoring."""

    def __init__(
        self,
        *,
        code: str,
        field: str,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.field = field
        self.message = message
        self.details = dict(details or {})
        super().__init__(f"{code}:{field}:{message}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "agvm.brain_profile_validation_error.v1",
            "code": self.code,
            "field": self.field,
            "message": self.message,
            "details": self.details,
        }


def _fail(
    code: str,
    field: str,
    message: str,
    **details: Any,
) -> None:
    raise BrainProfileValidationError(
        code=code,
        field=field,
        message=message,
        details=details,
    )


def _canonical_json_bytes(value: object) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        _fail(
            "brain_profile_v1_non_canonical_value",
            "profile",
            "profile values must have one finite canonical JSON representation",
            error=str(exc),
        )
    return encoded.encode("utf-8")


def _sha256(value: object) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json_bytes(value)).hexdigest()}"


def _finite_float(value: Any, *, field: str) -> float:
    if isinstance(value, bool):
        _fail("brain_profile_v1_invalid_number", field, "boolean values are not numeric profile values")
    try:
        number = float(value)
    except (TypeError, ValueError):
        _fail("brain_profile_v1_invalid_number", field, "value must be numeric", value=repr(value))
    if not math.isfinite(number):
        _fail("brain_profile_v1_non_finite_number", field, "value must be finite", value=repr(value))
    return 0.0 if number == 0.0 else number


def _fixed_vector(
    values: Sequence[Any],
    *,
    field: str,
    length: int = DIMENSION_COUNT,
) -> tuple[float, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        _fail("brain_profile_v1_invalid_vector", field, "value must be a numeric sequence")
    if len(values) != length:
        _fail(
            "brain_profile_v1_invalid_vector_length",
            field,
            f"vector must contain exactly {length} values",
            expected=length,
            actual=len(values),
        )
    return tuple(_finite_float(value, field=f"{field}[{index}]") for index, value in enumerate(values))


def _validate_components(
    *,
    routing_fields: Sequence[str],
    diagonal_weights: Sequence[Any],
    low_rank_factors: Sequence[Sequence[Any]],
    low_rank_intensity: Any,
    rerank_intensity: Any,
    display_projection: Sequence[Sequence[Any]],
    revision: Any,
    shadow: Any,
) -> tuple[
    tuple[str, ...],
    tuple[float, ...],
    tuple[tuple[float, ...], ...],
    float,
    tuple[tuple[float, ...], ...],
    int,
    float,
    bool,
]:
    fields = tuple(str(value) for value in routing_fields)
    if fields != CANONICAL_ROUTING_FIELDS:
        _fail(
            "brain_profile_v1_routing_fields_mismatch",
            "routing_fields",
            "routing fields must exactly match the current canonical order",
            expected=list(CANONICAL_ROUTING_FIELDS),
            actual=list(fields),
        )

    diagonal = _fixed_vector(diagonal_weights, field="diagonal_weights")
    for index, weight in enumerate(diagonal):
        if weight <= 0.0:
            _fail(
                "brain_profile_v1_non_positive_diagonal",
                f"diagonal_weights[{index}]",
                "every diagonal weight must be strictly positive",
                value=weight,
            )
        if weight < MIN_DIAGONAL_WEIGHT or weight > MAX_DIAGONAL_WEIGHT:
            _fail(
                "brain_profile_v1_diagonal_out_of_bounds",
                f"diagonal_weights[{index}]",
                "every diagonal weight must stay within the bounded V1 metric range",
                minimum=MIN_DIAGONAL_WEIGHT,
                maximum=MAX_DIAGONAL_WEIGHT,
                value=weight,
            )

    if isinstance(low_rank_factors, (str, bytes)) or not isinstance(low_rank_factors, Sequence):
        _fail("brain_profile_v1_invalid_factors", "low_rank_factors", "factors must be a sequence")
    if len(low_rank_factors) > MAX_LOW_RANK:
        _fail(
            "brain_profile_v1_rank_exceeded",
            "low_rank_factors",
            f"low-rank correction supports at most rank {MAX_LOW_RANK}",
            maximum=MAX_LOW_RANK,
            actual=len(low_rank_factors),
        )
    factors = tuple(
        _fixed_vector(vector, field=f"low_rank_factors[{index}]")
        for index, vector in enumerate(low_rank_factors)
    )
    _validate_coefficient_bound(
        factors,
        field="low_rank_factors",
        maximum=MAX_LOW_RANK_FACTOR_ABS,
    )

    intensity = _finite_float(low_rank_intensity, field="low_rank_intensity")
    if not 0.0 <= intensity <= MAX_LOW_RANK_INTENSITY:
        _fail(
            "brain_profile_v1_intensity_out_of_bounds",
            "low_rank_intensity",
            f"intensity must be between 0 and {MAX_LOW_RANK_INTENSITY}",
            minimum=0.0,
            maximum=MAX_LOW_RANK_INTENSITY,
            actual=intensity,
        )
    if not factors and intensity != 0.0:
        _fail(
            "brain_profile_v1_intensity_without_factors",
            "low_rank_intensity",
            "intensity must be zero when no low-rank factors are present",
            actual=intensity,
        )

    rerank = _finite_float(rerank_intensity, field="rerank_intensity")
    if not 0.0 <= rerank <= MAX_RERANK_INTENSITY:
        _fail(
            "brain_profile_v1_rerank_intensity_out_of_bounds",
            "rerank_intensity",
            f"rerank intensity must be between 0 and {MAX_RERANK_INTENSITY}",
            minimum=0.0,
            maximum=MAX_RERANK_INTENSITY,
            actual=rerank,
        )

    if isinstance(display_projection, (str, bytes)) or not isinstance(display_projection, Sequence):
        _fail("brain_profile_v1_invalid_projection", "display_projection", "projection must be a matrix")
    if len(display_projection) != DISPLAY_DIMENSION_COUNT:
        _fail(
            "brain_profile_v1_invalid_projection_rows",
            "display_projection",
            "display projection must contain exactly three rows",
            expected=DISPLAY_DIMENSION_COUNT,
            actual=len(display_projection),
        )
    projection = tuple(
        _fixed_vector(row, field=f"display_projection[{index}]")
        for index, row in enumerate(display_projection)
    )
    _validate_coefficient_bound(
        projection,
        field="display_projection",
        maximum=MAX_DISPLAY_COEFFICIENT_ABS,
    )
    for index, row in enumerate(projection):
        if not any(value != 0.0 for value in row):
            _fail(
                "brain_profile_v1_zero_projection_axis",
                f"display_projection[{index}]",
                "each display axis must contain at least one non-zero coefficient",
            )

    if isinstance(revision, bool):
        _fail("brain_profile_v1_invalid_revision", "revision", "revision must be a positive integer")
    try:
        parsed_revision = int(revision)
    except (TypeError, ValueError):
        _fail("brain_profile_v1_invalid_revision", "revision", "revision must be a positive integer")
    if parsed_revision < 1 or parsed_revision != revision:
        _fail(
            "brain_profile_v1_invalid_revision",
            "revision",
            "revision must be a positive integer",
            actual=revision,
        )
    if not isinstance(shadow, bool):
        _fail("brain_profile_v1_invalid_shadow_flag", "shadow", "shadow must be a boolean")

    _validate_metric_condition(diagonal, factors, intensity)

    return fields, diagonal, factors, intensity, projection, parsed_revision, rerank, shadow


def _validate_coefficient_bound(
    rows: Sequence[Sequence[float]],
    *,
    field: str,
    maximum: float,
) -> None:
    for row_index, row in enumerate(rows):
        for column_index, value in enumerate(row):
            if abs(value) > maximum:
                _fail(
                    "brain_profile_v1_coefficient_out_of_bounds",
                    f"{field}[{row_index}][{column_index}]",
                    "coefficient exceeds the bounded V1 range",
                    minimum=-maximum,
                    maximum=maximum,
                    actual=value,
                )


def _validate_metric_condition(
    diagonal: Sequence[float],
    factors: Sequence[Sequence[float]],
    intensity: float,
) -> None:
    # D + scale*U.T@U is positive definite because D is strictly positive.
    # This spectral upper bound is conservative and deterministic; rejecting on
    # it prevents adversarial factors from creating an ill-conditioned scorer.
    minimum_eigenvalue_lower_bound = min(diagonal)
    rank_scale = intensity / len(factors) if factors else 0.0
    correction_upper_bound = rank_scale * sum(
        sum(value * value for value in factor) for factor in factors
    )
    maximum_eigenvalue_upper_bound = max(diagonal) + correction_upper_bound
    condition_upper_bound = maximum_eigenvalue_upper_bound / minimum_eigenvalue_lower_bound
    if condition_upper_bound > MAX_METRIC_CONDITION_NUMBER:
        _fail(
            "brain_profile_v1_metric_condition_exceeded",
            "metric_matrix",
            "metric condition upper bound exceeds the V1 safety limit",
            maximum=MAX_METRIC_CONDITION_NUMBER,
            actual=condition_upper_bound,
        )


def _profile_base_payload(
    *,
    routing_fields: tuple[str, ...],
    diagonal_weights: tuple[float, ...],
    low_rank_factors: tuple[tuple[float, ...], ...],
    low_rank_intensity: float,
    rerank_intensity: float,
    display_projection: tuple[tuple[float, ...], ...],
    revision: int,
    shadow: bool,
) -> dict[str, Any]:
    return {
        "schema_version": BRAIN_PROFILE_V1_SCHEMA_VERSION,
        "routing_fields": list(routing_fields),
        "diagonal_weights": list(diagonal_weights),
        "low_rank_factors": [list(vector) for vector in low_rank_factors],
        "low_rank_intensity": low_rank_intensity,
        "rerank_intensity": rerank_intensity,
        "display_projection": [list(row) for row in display_projection],
        "revision": revision,
        "shadow": shadow,
    }


def _integrity_fields(base_payload: Mapping[str, Any]) -> tuple[str, str, str]:
    base_checksum = _sha256(dict(base_payload))
    revision = int(base_payload["revision"])
    revision_id = f"brain-profile-v1:r{revision}:{base_checksum.removeprefix('sha256:')[:20]}"
    checksum = _sha256({**dict(base_payload), "revision_id": revision_id})
    signature = _sha256(
        {
            "domain": BRAIN_PROFILE_V1_SIGNATURE_DOMAIN,
            "revision_id": revision_id,
            "checksum": checksum,
        }
    )
    return revision_id, checksum, signature


class BrainProfileV1(BaseModel):
    """Immutable profile whose metric is positive definite by construction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["agvm.brain_profile.v1"] = BRAIN_PROFILE_V1_SCHEMA_VERSION
    routing_fields: tuple[str, ...]
    diagonal_weights: tuple[float, ...]
    low_rank_factors: tuple[tuple[float, ...], ...] = ()
    low_rank_intensity: float = 0.0
    rerank_intensity: float = 0.0
    display_projection: tuple[tuple[float, ...], ...]
    revision: int
    revision_id: str
    checksum: str
    signature: str
    shadow: bool = True

    @model_validator(mode="after")
    def validate_profile(self) -> "BrainProfileV1":
        components = _validate_components(
            routing_fields=self.routing_fields,
            diagonal_weights=self.diagonal_weights,
            low_rank_factors=self.low_rank_factors,
            low_rank_intensity=self.low_rank_intensity,
            rerank_intensity=self.rerank_intensity,
            display_projection=self.display_projection,
            revision=self.revision,
            shadow=self.shadow,
        )
        fields, diagonal, factors, intensity, projection, revision, rerank, shadow = components
        expected = _integrity_fields(
            _profile_base_payload(
                routing_fields=fields,
                diagonal_weights=diagonal,
                low_rank_factors=factors,
                low_rank_intensity=intensity,
                rerank_intensity=rerank,
                display_projection=projection,
                revision=revision,
                shadow=shadow,
            )
        )
        actual = (self.revision_id, self.checksum, self.signature)
        if actual != expected:
            names = ("revision_id", "checksum", "signature")
            mismatches = [name for name, value, expected_value in zip(names, actual, expected) if value != expected_value]
            _fail(
                "brain_profile_v1_integrity_mismatch",
                mismatches[0],
                "revision identity, checksum, or signature does not match the canonical profile",
                mismatches=mismatches,
            )
        return self

    def metric_matrix(self) -> tuple[tuple[float, ...], ...]:
        return build_psd_metric_matrix(
            self.diagonal_weights,
            self.low_rank_factors,
            self.low_rank_intensity,
        )

    def score(
        self,
        left: Sequence[float] | Mapping[str, float],
        right: Sequence[float] | Mapping[str, float],
    ) -> float:
        return score_brain_profile_v1(self, left, right)

    def project(self, vector: Sequence[float] | Mapping[str, float]) -> tuple[float, float, float]:
        values = _routing_vector(vector, field="vector")
        return tuple(
            sum(coefficient * value for coefficient, value in zip(row, values))
            for row in self.display_projection
        )  # type: ignore[return-value]


def build_psd_metric_matrix(
    diagonal_weights: Sequence[Any],
    low_rank_factors: Sequence[Sequence[Any]] = (),
    low_rank_intensity: Any = 0.0,
) -> tuple[tuple[float, ...], ...]:
    """Build ``D + intensity/rank * U.T@U`` deterministically."""

    diagonal = _fixed_vector(diagonal_weights, field="diagonal_weights")
    for index, weight in enumerate(diagonal):
        if weight <= 0.0:
            _fail(
                "brain_profile_v1_non_positive_diagonal",
                f"diagonal_weights[{index}]",
                "every diagonal weight must be strictly positive",
                value=weight,
            )
        if weight < MIN_DIAGONAL_WEIGHT or weight > MAX_DIAGONAL_WEIGHT:
            _fail(
                "brain_profile_v1_diagonal_out_of_bounds",
                f"diagonal_weights[{index}]",
                "every diagonal weight must stay within the bounded V1 metric range",
                minimum=MIN_DIAGONAL_WEIGHT,
                maximum=MAX_DIAGONAL_WEIGHT,
                value=weight,
            )
    if len(low_rank_factors) > MAX_LOW_RANK:
        _fail(
            "brain_profile_v1_rank_exceeded",
            "low_rank_factors",
            f"low-rank correction supports at most rank {MAX_LOW_RANK}",
            maximum=MAX_LOW_RANK,
            actual=len(low_rank_factors),
        )
    factors = tuple(
        _fixed_vector(vector, field=f"low_rank_factors[{index}]")
        for index, vector in enumerate(low_rank_factors)
    )
    _validate_coefficient_bound(
        factors,
        field="low_rank_factors",
        maximum=MAX_LOW_RANK_FACTOR_ABS,
    )
    intensity = _finite_float(low_rank_intensity, field="low_rank_intensity")
    if not 0.0 <= intensity <= MAX_LOW_RANK_INTENSITY:
        _fail(
            "brain_profile_v1_intensity_out_of_bounds",
            "low_rank_intensity",
            f"intensity must be between 0 and {MAX_LOW_RANK_INTENSITY}",
            minimum=0.0,
            maximum=MAX_LOW_RANK_INTENSITY,
            actual=intensity,
        )
    if not factors and intensity != 0.0:
        _fail(
            "brain_profile_v1_intensity_without_factors",
            "low_rank_intensity",
            "intensity must be zero when no low-rank factors are present",
            actual=intensity,
        )
    _validate_metric_condition(diagonal, factors, intensity)

    rank_scale = intensity / len(factors) if factors else 0.0
    matrix: list[list[float]] = [
        [diagonal[row] if row == column else 0.0 for column in range(DIMENSION_COUNT)]
        for row in range(DIMENSION_COUNT)
    ]
    for factor in factors:
        for row in range(DIMENSION_COUNT):
            for column in range(DIMENSION_COUNT):
                matrix[row][column] += rank_scale * factor[row] * factor[column]
    return tuple(tuple(value for value in row) for row in matrix)


def build_brain_profile_v1(
    *,
    diagonal_weights: Sequence[Any] | None = None,
    low_rank_factors: Sequence[Sequence[Any]] = (),
    low_rank_intensity: Any = 0.0,
    rerank_intensity: Any = 0.0,
    display_projection: Sequence[Sequence[Any]] | None = None,
    revision: int = 1,
    shadow: bool = True,
    routing_fields: Sequence[str] = CANONICAL_ROUTING_FIELDS,
) -> BrainProfileV1:
    """Build one checksummed profile from bounded canonical components."""

    try:
        components = _validate_components(
            routing_fields=routing_fields,
            diagonal_weights=(1.0,) * DIMENSION_COUNT if diagonal_weights is None else diagonal_weights,
            low_rank_factors=low_rank_factors,
            low_rank_intensity=low_rank_intensity,
            rerank_intensity=rerank_intensity,
            display_projection=ROUTING_A if display_projection is None else display_projection,
            revision=revision,
            shadow=shadow,
        )
        fields, diagonal, factors, intensity, projection, parsed_revision, parsed_rerank, parsed_shadow = components
        base = _profile_base_payload(
            routing_fields=fields,
            diagonal_weights=diagonal,
            low_rank_factors=factors,
            low_rank_intensity=intensity,
            rerank_intensity=parsed_rerank,
            display_projection=projection,
            revision=parsed_revision,
            shadow=parsed_shadow,
        )
        revision_id, checksum, signature = _integrity_fields(base)
        return BrainProfileV1(
            **base,
            revision_id=revision_id,
            checksum=checksum,
            signature=signature,
        )
    except BrainProfileValidationError:
        raise
    except ValidationError as exc:
        raise _validation_error(exc) from exc


def load_brain_profile_v1(value: Mapping[str, Any]) -> BrainProfileV1:
    """Validate untrusted serialized profile data and preserve structured failures."""

    try:
        return BrainProfileV1.model_validate(dict(value))
    except BrainProfileValidationError:
        raise
    except ValidationError as exc:
        raise _validation_error(exc) from exc


def _validation_error(exc: ValidationError) -> BrainProfileValidationError:
    errors = exc.errors(include_url=False, include_context=False)
    first = errors[0] if errors else {}
    location = ".".join(str(item) for item in first.get("loc", ())) or "profile"
    return BrainProfileValidationError(
        code="brain_profile_v1_invalid",
        field=location,
        message=str(first.get("msg") or "profile validation failed"),
        details={"errors": errors},
    )


def _routing_vector(
    value: Sequence[float] | Mapping[str, float],
    *,
    field: str,
) -> tuple[float, ...]:
    if isinstance(value, Mapping):
        actual = set(value)
        expected = set(CANONICAL_ROUTING_FIELDS)
        if actual != expected:
            _fail(
                "brain_profile_v1_score_fields_mismatch",
                field,
                "score mappings must contain every canonical routing field exactly once",
                missing=sorted(expected - actual),
                unexpected=sorted(actual - expected),
            )
        return tuple(_finite_float(value[name], field=f"{field}.{name}") for name in CANONICAL_ROUTING_FIELDS)
    return _fixed_vector(value, field=field)


def score_brain_profile_v1(
    profile: BrainProfileV1,
    left: Sequence[float] | Mapping[str, float],
    right: Sequence[float] | Mapping[str, float],
) -> float:
    """Return bounded metric cosine similarity, with zero evidence scoring zero."""

    if not isinstance(profile, BrainProfileV1):
        _fail("brain_profile_v1_invalid_profile_type", "profile", "profile must be BrainProfileV1")
    left_vector = _routing_vector(left, field="left")
    right_vector = _routing_vector(right, field="right")
    matrix = profile.metric_matrix()

    def bilinear(first: tuple[float, ...], second: tuple[float, ...]) -> float:
        return sum(
            first[row] * matrix[row][column] * second[column]
            for row in range(DIMENSION_COUNT)
            for column in range(DIMENSION_COUNT)
        )

    left_norm = bilinear(left_vector, left_vector)
    right_norm = bilinear(right_vector, right_vector)
    if left_norm <= 0.0 or right_norm <= 0.0:
        return 0.0
    cosine = bilinear(left_vector, right_vector) / math.sqrt(left_norm * right_norm)
    return min(1.0, max(0.0, cosine))


__all__ = [
    "BRAIN_PROFILE_V1_SCHEMA_VERSION",
    "CANONICAL_ROUTING_FIELDS",
    "DIMENSION_COUNT",
    "DISPLAY_DIMENSION_COUNT",
    "MAX_LOW_RANK",
    "MAX_LOW_RANK_INTENSITY",
    "MAX_LOW_RANK_FACTOR_ABS",
    "MAX_DISPLAY_COEFFICIENT_ABS",
    "MAX_DIAGONAL_WEIGHT",
    "MIN_DIAGONAL_WEIGHT",
    "MAX_METRIC_CONDITION_NUMBER",
    "MAX_RERANK_INTENSITY",
    "BrainProfileV1",
    "BrainProfileValidationError",
    "build_brain_profile_v1",
    "build_psd_metric_matrix",
    "load_brain_profile_v1",
    "score_brain_profile_v1",
]
