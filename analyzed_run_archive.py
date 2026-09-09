"""Bounded, deterministic persistence for completed analyzed runs.

Schema 2 is a ZIP64-capable, line-streamed container.  The public path APIs
avoid retaining a second JSON representation of a live scientific report;
the byte APIs exist for compatibility tests and legacy schema-1 imports.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import math
from contextvars import ContextVar
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, fields
from itertools import zip_longest
from pathlib import Path, PurePosixPath
import stat
import tempfile
from typing import BinaryIO
import zipfile

import thermo_engine as te


class ArchiveLoadCancelled(Exception):
    """A requested archive load stopped before publishing any result."""


_load_cancel_event = ContextVar("archive_load_cancel_event", default=None)


def _check_load_cancelled() -> None:
    event = _load_cancel_event.get()
    if event is not None and event.is_set():
        raise ArchiveLoadCancelled()


ANALYZED_RUN_FORMAT = "mbuprime-structlab-analyzed-run"
ANALYZED_RUN_SCHEMA_VERSION = 2
LEGACY_ANALYZED_RUN_SCHEMA_VERSION = 1
MAX_LEGACY_ANALYZED_RUN_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_COMPRESSED_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_EXPANDED_BYTES = 512 * 1024 * 1024
MAX_CONTROL_MEMBER_BYTES = 8 * 1024 * 1024
MAX_JSONL_RECORD_BYTES = 16 * 1024 * 1024
MAX_JSON_DEPTH = 64
MAX_JSON_VALUES_PER_RECORD = 250_000
MAX_STREAMED_RECORDS = 5_000_000
MAX_COMPRESSION_RATIO = 250
MAX_LEGACY_JSON_VALUES = 3_500_000
HISTORICAL_ANALYZED_RUN_POLICY = (
    "2026-08-28-context-truth-and-nonmonotonic-gap-fail-closed-1")
SUPPORTED_ANALYZED_RUN_POLICIES = frozenset((
    te.sm.SCIENTIFIC_POLICY_VERSION,
    HISTORICAL_ANALYZED_RUN_POLICY,
    "2026-09-02-exact-engine-and-concrete-coverage-1",
))

_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_JSONL_MEMBERS = frozenset((
    "oligos.jsonl", "tms.jsonl", "diagnostics.jsonl",
    "ensemble-contexts.jsonl", "hairpins.jsonl", "self-dimers.jsonl",
    "heterodimers.jsonl",
))
ARCHIVE_MEMBERS = (
    "run.json",
    "oligos.jsonl",
    "tms.jsonl",
    "diagnostics.jsonl",
    "ensemble-contexts.jsonl",
    "manifest.json",
    "hairpins.jsonl",
    "self-dimers.jsonl",
    "heterodimers.jsonl",
    "index.json",
)
_INDEXED_MEMBERS = ARCHIVE_MEMBERS[:-1]
_MANIFEST_DERIVED_FIELDS = frozenset((
    "normalized_inputs", "conditions", "diagnostics", "ensemble_plan",
    "ensemble_coverage",
))
_ENSEMBLE_KINDS = ("hairpin", "self_dimer", "hetero_dimer")
_ENSEMBLE_ENGINE_SUPPORT = (
    ("Primer3", frozenset(_ENSEMBLE_KINDS)),
    ("ViennaRNA", frozenset(_ENSEMBLE_KINDS)),
    ("RNAstructure", frozenset(_ENSEMBLE_KINDS)),
    ("seqfold", frozenset(("hairpin",))),
    ("LocalEnumerator", frozenset(_ENSEMBLE_KINDS)),
)


class PersistenceError(ValueError):
    """An analyzed-run operation failed with a stable presentation code."""

    CODES = frozenset((
        "invalid_archive", "integrity_failure", "limit_exceeded",
        "unsupported_format", "io_failure", "memory_exhausted",
    ))

    def __init__(self, message: str, *, code: str | None = None) -> None:
        resolved = code or self._classify(message)
        if resolved not in self.CODES:
            raise ValueError(f"unknown persistence error code: {resolved}")
        self.code = resolved
        super().__init__(message)

    @staticmethod
    def _classify(message: str) -> str:
        text = str(message).casefold()
        if "memory" in text:
            return "memory_exhausted"
        if any(marker in text for marker in (
                "limit", "exceeds", "too large", "record count")):
            return "limit_exceeded"
        if any(marker in text for marker in (
                "checksum", "damaged", "truncated", "crc", "final lf")):
            return "integrity_failure"
        if any(marker in text for marker in (
                "not supported", "incompatible", "schema", "policy",
                "not an mbuprime")):
            return "unsupported_format"
        if any(marker in text for marker in (
                "cannot read", "cannot create", "cannot write")):
            return "io_failure"
        return "invalid_archive"


@dataclass(frozen=True)
class LoadedAnalyzedRun:
    """Validated archive content for review before application-state restoration.

    The frozen envelope does not make its contained analysis report immutable.
    """

    oligos: tuple[te.Oligo, ...]
    conditions: te.ReactionConditions
    report: te.AnalysisReport
    ensemble_mode: str
    ensemble_additional_budget: int


@dataclass(frozen=True)
class _ArchiveEncodingContext:
    oligos: tuple[te.Oligo, ...]
    indices: dict[int, int]


@dataclass(frozen=True)
class _DecodedLegacyEnvelope:
    payload: dict[str, object]
    application_version: str
    scientific_policy_version: str


@dataclass(frozen=True)
class _MemberDigest:
    name: str
    record_count: int
    uncompressed_size: int
    sha256: str


@dataclass
class _ReadBudget:
    expanded_bytes: int = 0
    records: int = 0

    def add_bytes(self, amount: int) -> None:
        self.expanded_bytes += amount
        if self.expanded_bytes > MAX_ARCHIVE_EXPANDED_BYTES:
            raise PersistenceError(
                "expanded archive data exceeds the 512 MiB limit")

    def add_record(self) -> None:
        self.records += 1
        if self.records > MAX_STREAMED_RECORDS:
            raise PersistenceError(
                "streamed record count exceeds the limit of 5000000")


@dataclass
class _WriteBudget:
    expanded_bytes: int = 0

    def add_bytes(self, amount: int) -> None:
        self.expanded_bytes += amount
        if self.expanded_bytes > MAX_ARCHIVE_EXPANDED_BYTES:
            raise PersistenceError(
                "expanded archive data exceeds the 512 MiB limit")


_TYPE_FIELDS = {
    "ReactionConditions": (
        "mv_conc", "dv_conc", "dntp_conc", "primer_conc", "probe_conc",
        "dg_temp_c", "dg_caution", "dg_problem",
        "near_duplicate_bond_difference", "dimer_max_consecutive_gaps",
        "dimer_max_total_gaps",
    ),
    "Oligo": ("name", "seq", "role", "conc_nM", "variants"),
    "TmResult": (
        "oligo", "tm_mean", "tm_min", "tm_max", "tm_owczarzy_mean",
        "tm_owczarzy_min", "tm_owczarzy_max",
    ),
    "CanonicalGeometry": ("kind", "sequence_a", "sequence_b", "pairs"),
    "EngineObservation": (
        "engine", "engine_version", "status", "interaction_type",
        "search_mode", "geometry", "dg_kcal_mol", "tm_c",
        "energy_definition", "tm_definition", "structure_scope",
        "structure_text", "warning", "provenance", "dh_kcal_mol",
        "ds_cal_mol_k", "tm_status", "tm_bracket_c", "tm_search_range_c",
        "tm_numerical_tolerance_c", "thermo_path_identity", "tm_method",
        "dg_temperature_c",
    ),
    "PeerStructure": (
        "kind", "label", "found", "geometry", "structure", "rank",
        "discovered_by", "dg_p3", "tm_p3_c", "dg_vienna", "tm_vienna_c",
        "severity", "assessment", "n_combos", "representative_variant",
        "involves_3prime", "engine_observations", "external_diagnostics",
        "external_metric_states", "thermo_model", "pair_classes",
        "geometry_diagnostics", "derivation_lineage",
    ),
    "InteractionResult": (
        "kind", "label", "sequence_a", "sequence_b", "structures",
        "n_combos", "representative_variant", "variant_coverage",
        "external_diagnostics",
    ),
    "EnsembleContext": (
        "kind", "oligo_a", "variant_a", "sequence_a", "oligo_b",
        "variant_b", "sequence_b", "baseline",
    ),
    "EnsembleKindPlan": ("baseline", "additional", "allocated", "total"),
    "EnsembleWorkPlan": (
        "mode", "configured_additional_budget", "baseline_contexts",
        "additional_contexts", "allocated_contexts", "total_contexts",
        "by_kind", "contexts",
    ),
    "EnsembleCoverageEntry": ("supported", "evaluated", "total", "failed"),
    "EnsembleCoverage": (
        "mode", "ensemble_complete", "evaluated_contexts",
        "allocated_contexts", "total_contexts", "engines",
    ),
    "ScientificDiagnostic": ("code", "engine", "quantity", "message"),
    "ScientificManifest": (
        "schema_version", "application_version", "scientific_policy_version",
        "python_version", "primer3_py_version", "primer3_core_version",
        "viennarna_version", "vienna_parameter_set", "vienna_salt_applied",
        "operating_system", "architecture", "source_commit",
        "source_tree_hash", "dependency_lock_hash", "build_date",
        "release_artifact_hash", "max_variants", "enumeration_pool",
        "min_sequence_length", "max_sequence_length", "engine_search_policies",
        "final_union_truncated", "degenerate_structure_scope",
        "severity_policy", "geometry_policy", "variant_policy",
        "dg_decision_resolution", "tm_decision_resolution", "salt_formula_id",
        "normalized_inputs", "conditions", "diagnostics", "engines",
        "analysis_id", "ensemble_plan", "ensemble_coverage",
    ),
    "AnalysisReport": (
        "tms", "hairpins", "self_dimers", "hetero_dimers", "manifest",
        "diagnostics", "ensemble_plan", "ensemble_coverage", "complete", "phase",
    ),
}
_TYPE_CLASSES = {
    "ReactionConditions": te.ReactionConditions,
    "Oligo": te.Oligo,
    "TmResult": te.TmResult,
    "CanonicalGeometry": te.ee.CanonicalGeometry,
    "EngineObservation": te.ee.EngineObservation,
    "PeerStructure": te.PeerStructure,
    "InteractionResult": te.InteractionResult,
    "EnsembleContext": te.EnsembleContext,
    "EnsembleKindPlan": te.EnsembleKindPlan,
    "EnsembleWorkPlan": te.EnsembleWorkPlan,
    "EnsembleCoverageEntry": te.EnsembleCoverageEntry,
    "EnsembleCoverage": te.EnsembleCoverage,
    "ScientificDiagnostic": te.sm.ScientificDiagnostic,
    "ScientificManifest": te.sm.ScientificManifest,
    "AnalysisReport": te.AnalysisReport,
}
_CLASS_TYPES = {value: key for key, value in _TYPE_CLASSES.items()}


def _canonical_bytes(value: object) -> bytes:
    try:
        text = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise PersistenceError(
            f"data cannot be represented safely: {exc}") from exc
    return (text + "\n").encode("utf-8")


def _exact_keys(value: object, expected: set[str], context: str) -> dict:
    if not isinstance(value, dict):
        raise PersistenceError(f"{context} must be a JSON object")
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unknown:
            details.append("unknown " + ", ".join(unknown))
        raise PersistenceError(
            f"{context} fields are invalid ({'; '.join(details)})")
    return value


def _json_container_deltas(text: str) -> Iterable[int]:
    in_string = False
    escaped = False
    for offset, character in enumerate(text):
        if offset % 4096 == 0:
            _check_load_cancelled()
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            yield 1
        elif character in "]}":
            yield -1


def _validate_json_nesting(text: str) -> None:
    depth = 0
    for delta in _json_container_deltas(text):
        depth += delta
        if depth > MAX_JSON_DEPTH:
            raise PersistenceError(
                f"JSON nesting exceeds the limit of {MAX_JSON_DEPTH}")
        if depth < 0:
            break


def _quoted_token_end(text: str, opening: int) -> int:
    """Return the closing quote index, or len(text) for an unfinished token."""

    index = opening + 1
    length = len(text)
    escaped = False
    while index < length:
        if index % 4096 == 0:
            _check_load_cancelled()
        character = text[index]
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == '"':
            break
        index += 1
    return index


def _count_json_values(text: str, maximum: int | None = None) -> int:
    count = 0
    index = 0
    length = len(text)
    next_checkpoint = 0
    while index < length:
        if index >= next_checkpoint:
            _check_load_cancelled()
            next_checkpoint = index + 4096
        character = text[index]
        if character in "[{":
            count += 1
        elif character == '"':
            index = _quoted_token_end(text, index)
            following = index + 1
            while following < length and text[following].isspace():
                following += 1
            if following >= length or text[following] != ":":
                count += 1
        elif character in "-0123456789tfn":
            previous = index - 1
            while previous >= 0 and text[previous].isspace():
                previous -= 1
            if previous < 0 or text[previous] in "[,:":
                count += 1
        if maximum is not None and count > maximum:
            raise PersistenceError(
                f"JSON value count exceeds the limit of {maximum}")
        index += 1
    return count


def _reject_duplicate_keys(pairs):
    _check_load_cancelled()
    result = {}
    for key, value in pairs:
        if key in result:
            raise PersistenceError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _validate_json_tree(value: object, maximum_values: int) -> None:
    depth_stack = [(value, 0)]
    while depth_stack:
        _check_load_cancelled()
        current, depth = depth_stack.pop()
        if depth > MAX_JSON_DEPTH:
            raise PersistenceError(
                f"JSON nesting exceeds the limit of {MAX_JSON_DEPTH}")
        if isinstance(current, dict):
            depth_stack.extend(
                (item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            depth_stack.extend((item, depth + 1) for item in current)

    nodes = 0
    stack = [(value, 0)]
    while stack:
        _check_load_cancelled()
        current, depth = stack.pop()
        nodes += 1
        if nodes > maximum_values:
            raise PersistenceError(
                f"JSON value count exceeds the limit of {maximum_values}")
        if isinstance(current, dict):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
        elif isinstance(current, float) and not math.isfinite(current):
            raise PersistenceError("non-finite numbers are not allowed")


def _parse_json_bytes(
    payload: bytes, *, maximum: int, description: str,
    maximum_values: int = MAX_JSON_VALUES_PER_RECORD,
):
    if len(payload) > maximum:
        mib = maximum // (1024 * 1024) or 1
        raise PersistenceError(f"{description} exceeds the {mib} MiB limit")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PersistenceError(f"{description} is not valid UTF-8") from exc
    _validate_json_nesting(text)
    _count_json_values(text, maximum_values)

    def parse_float(raw: str) -> float:
        value = float(raw)
        if not math.isfinite(value):
            raise PersistenceError("non-finite numbers are not allowed")
        return value

    try:
        value = json.loads(
            text, object_pairs_hook=_reject_duplicate_keys,
            parse_float=parse_float,
            parse_constant=lambda raw: (_ for _ in ()).throw(
                PersistenceError(f"non-finite number is not allowed: {raw}")))
    except PersistenceError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise PersistenceError(
            f"{description} contains invalid JSON: {exc}") from exc
    _validate_json_tree(value, maximum_values)
    return value


def _encode_sequence_value(
    value: list | tuple, tag: str, context: _ArchiveEncodingContext | None,
) -> dict:
    return {"$type": tag, "items": [
        _encode_value(item, context) for item in value]}


def _encode_mapping_value(
    value: dict, context: _ArchiveEncodingContext | None,
) -> dict:
    if not all(isinstance(key, str) for key in value):
        raise PersistenceError("only string-keyed mappings can be archived")
    return {"$type": "dict", "items": [
        [key, _encode_value(value[key], context)] for key in sorted(value)]}


def _encode_oligo_reference(
    oligo: te.Oligo, context: _ArchiveEncodingContext | None,
) -> dict:
    if context is None:
        raise PersistenceError("TmResult refers to a non-canonical oligo")
    index = context.indices.get(id(oligo))
    if index is None:
        matches = [item_index for item_index, item in enumerate(
            context.oligos) if item == oligo]
        if len(matches) != 1:
            raise PersistenceError("TmResult refers to a non-canonical oligo")
        index = matches[0]
    return {"$type": "OligoRef", "index": index}


def _encode_dataclass_value(
    value: object, tag: str, context: _ArchiveEncodingContext | None,
) -> dict:
    expected = _TYPE_FIELDS[tag]
    actual = tuple(item.name for item in fields(type(value)))
    if actual != expected:
        raise PersistenceError(
            f"{tag} fields changed without an archive schema migration")
    encoded = {"$type": tag}
    for name in expected:
        field_value = getattr(value, name)
        if tag == "TmResult" and name == "oligo":
            encoded[name] = _encode_oligo_reference(field_value, context)
        else:
            encoded[name] = _encode_value(field_value, context)
    return encoded


def _encode_value(
    value: object, context: _ArchiveEncodingContext | None = None,
):
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise PersistenceError("non-finite numbers cannot be archived")
        return value
    if isinstance(value, list):
        return _encode_sequence_value(value, "list", context)
    if isinstance(value, te.sm._FrozenSequence):
        return _encode_sequence_value(value, "list", context)
    if isinstance(value, te.sm._FrozenMapping):
        return _encode_mapping_value(dict(value), context)
    if isinstance(value, tuple):
        return _encode_sequence_value(value, "tuple", context)
    if isinstance(value, dict):
        return _encode_mapping_value(value, context)
    tag = _CLASS_TYPES.get(type(value))
    if tag is None:
        raise PersistenceError(
            f"unsupported archive value type: {type(value).__name__}")
    return _encode_dataclass_value(value, tag, context)


def _decode_sequence_value(value: dict, tag: str,
                           oligos: tuple[te.Oligo, ...]):
    obj = _exact_keys(value, {"$type", "items"}, tag)
    if not isinstance(obj["items"], list):
        raise PersistenceError(f"{tag} items must be an array")
    items = [_decode_value(item, oligos) for item in obj["items"]]
    return items if tag == "list" else tuple(items)


def _decode_mapping_value(value: dict,
                          oligos: tuple[te.Oligo, ...]) -> dict:
    obj = _exact_keys(value, {"$type", "items"}, "dict")
    if not isinstance(obj["items"], list):
        raise PersistenceError("dict items must be an array")
    result = {}
    for item in obj["items"]:
        if (not isinstance(item, list) or len(item) != 2
                or not isinstance(item[0], str)):
            raise PersistenceError("dict entries must be [string, value]")
        key = item[0]
        if key in result:
            raise PersistenceError(f"duplicate archived mapping key: {key}")
        result[key] = _decode_value(item[1], oligos)
    return result


def _decode_oligo_reference(value: dict, oligos: tuple[te.Oligo, ...]):
    obj = _exact_keys(value, {"$type", "index"}, "OligoRef")
    index = obj["index"]
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise PersistenceError(
            "oligo reference index must be a non-negative integer")
    try:
        return oligos[index]
    except IndexError as exc:
        raise PersistenceError("oligo reference is out of range") from exc


def _decode_dataclass_value(value: dict, tag: str,
                            oligos: tuple[te.Oligo, ...]):
    cls = _TYPE_CLASSES.get(tag)
    if cls is None:
        raise PersistenceError(f"unsupported archive type tag: {tag}")
    expected = _TYPE_FIELDS[tag]
    _exact_keys(value, {"$type", *expected}, tag)
    kwargs = {name: _decode_value(value[name], oligos) for name in expected}
    if tag == "TmResult":
        for name in expected:
            if name == "oligo":
                continue
            number = kwargs[name]
            if name.startswith("tm_owczarzy_") and number is None:
                continue
            try:
                finite_number = (not isinstance(number, bool)
                                 and isinstance(number, (int, float))
                                 and math.isfinite(number))
            except OverflowError:
                finite_number = False
            if not finite_number:
                raise PersistenceError(
                    f"invalid TmResult: {name} must be a finite number")
    try:
        return cls(**kwargs)
    except (TypeError, ValueError, te.SequenceError) as exc:
        raise PersistenceError(f"invalid {tag}: {exc}") from exc


def _decode_value(value: object, oligos: tuple[te.Oligo, ...] = ()):
    _check_load_cancelled()
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise PersistenceError("non-finite numbers are not allowed")
        return value
    if not isinstance(value, dict):
        raise PersistenceError("archive values must use declared type tags")
    tag = value.get("$type")
    if not isinstance(tag, str):
        raise PersistenceError("archive value has no valid type tag")
    if tag in {"list", "tuple"}:
        return _decode_sequence_value(value, tag, oligos)
    if tag == "dict":
        return _decode_mapping_value(value, oligos)
    if tag == "OligoRef":
        return _decode_oligo_reference(value, oligos)
    return _decode_dataclass_value(value, tag, oligos)


def _validate_archived_conditions(conditions: te.ReactionConditions) -> None:
    numeric_fields = (
        "mv_conc", "dv_conc", "dntp_conc", "primer_conc", "probe_conc",
        "dg_temp_c", "dg_caution", "dg_problem",
    )
    for name in numeric_fields:
        value = getattr(conditions, name)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value)):
            raise PersistenceError(
                f"archive condition {name} must be a finite number")


def _manifest_analysis_id(manifest: te.sm.ScientificManifest) -> str:
    return te.sm.analysis_identity_sha256(manifest)


def _validate_decoded_oligo(oligo: te.Oligo) -> None:
    if (not isinstance(oligo.name, str) or not oligo.name.strip()
            or "\n" in oligo.name or "\r" in oligo.name):
        raise PersistenceError("archive oligo name is invalid")
    if oligo.role not in {"primer", "probe"}:
        raise PersistenceError("archive oligo role is invalid")
    if (isinstance(oligo.conc_nM, bool)
            or not isinstance(oligo.conc_nM, (int, float))
            or not math.isfinite(oligo.conc_nM) or oligo.conc_nM <= 0):
        raise PersistenceError("archive oligo concentration is invalid")
    if oligo.variants != te.expand_iupac(oligo.seq):
        raise PersistenceError("archive oligo variants do not match its sequence")


def _expected_plan_numbers(
    oligos: tuple[te.Oligo, ...], mode: str, budget: int,
) -> tuple[dict[str, te.EnsembleKindPlan], int, int, int]:
    if mode not in {
            te.ENSEMBLE_MODE_REPRESENTATIVE,
            te.ENSEMBLE_MODE_BUDGETED_COMPLETE}:
        raise PersistenceError("archive ensemble mode is invalid")
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 0:
        raise PersistenceError("archive ensemble budget is invalid")
    counts = [len(oligo.variants) for oligo in oligos]
    oligo_count = len(oligos)
    baseline = {
        "hairpin": oligo_count,
        "self_dimer": oligo_count,
        "hetero_dimer": oligo_count * (oligo_count - 1) // 2,
    }
    total = {
        "hairpin": sum(counts),
        "self_dimer": sum(counts),
        "hetero_dimer": sum(
            counts[left] * counts[right]
            for left in range(oligo_count)
            for right in range(left + 1, oligo_count)),
    }
    remaining = budget if mode == te.ENSEMBLE_MODE_BUDGETED_COMPLETE else 0
    by_kind: dict[str, te.EnsembleKindPlan] = {}
    for kind in _ENSEMBLE_KINDS:
        additional = total[kind] - baseline[kind]
        extra = min(additional, remaining)
        remaining -= extra
        by_kind[kind] = te.EnsembleKindPlan(
            baseline[kind], additional, baseline[kind] + extra, total[kind])
    baseline_count = sum(baseline.values())
    total_count = sum(total.values())
    allocated_count = sum(item.allocated for item in by_kind.values())
    return by_kind, baseline_count, allocated_count, total_count


def _validate_ensemble_plan(
    oligos: tuple[te.Oligo, ...], report: te.AnalysisReport,
    plan: te.EnsembleWorkPlan,
) -> None:
    by_kind, baseline, allocated, total = _expected_plan_numbers(
        oligos, plan.mode, plan.configured_additional_budget)
    expected_by_kind = tuple((kind, by_kind[kind]) for kind in _ENSEMBLE_KINDS)
    if plan.by_kind != expected_by_kind:
        raise PersistenceError("archive ensemble per-kind plan is inconsistent")
    if (plan.baseline_contexts != baseline
            or plan.additional_contexts != total - baseline
            or plan.allocated_contexts != allocated
            or plan.total_contexts != total
            or len(plan.contexts) != allocated):
        raise PersistenceError("archive ensemble plan arithmetic is inconsistent")
    def matches(expected: Iterable[te.EnsembleContext]) -> bool:
        sentinel = object()
        return all(
            stored is not sentinel and candidate is not sentinel
            and stored == candidate
            for stored, candidate in zip_longest(
                plan.contexts, expected, fillvalue=sentinel))

    resolved = te.iter_resolved_ensemble_contexts(list(oligos), report, plan)
    if matches(resolved):
        return
    allocated_extra = {
        kind: item.allocated - item.baseline for kind, item in plan.by_kind}
    planned = te.iter_planned_ensemble_contexts(
        list(oligos), allocated_extra)
    if not matches(planned):
        raise PersistenceError(
            "archive ensemble context order or identity is inconsistent")


def _validate_engine_coverage_entry(
    engine: str, kind: str, entry: te.EnsembleCoverageEntry,
    supported_kinds: frozenset[str],
    plan_by_kind: dict[str, te.EnsembleKindPlan], *, ensemble_complete: bool,
) -> None:
    """Check one entry against its engine support and concrete-context bound."""

    if not isinstance(entry.supported, bool):
        raise PersistenceError(
            "archive engine coverage support flag is invalid")
    if kind not in supported_kinds:
        if (entry.supported or entry.evaluated is not None
                or entry.total is not None or entry.failed is not None):
            raise PersistenceError(
                "archive unsupported engine coverage must be empty")
        return
    allocated = plan_by_kind[kind].allocated
    total = plan_by_kind[kind].total
    # Primer3 evaluates every concrete variant independently of the
    # bounded peer-engine schedule; other engines are allocation-bound.
    bound = total if engine == "Primer3" else allocated
    values = (entry.evaluated, entry.failed)
    if (not entry.supported or entry.total != total
            or any(isinstance(value, bool) or not isinstance(value, int)
                   for value in values)
            or not 0 <= entry.evaluated <= bound
            or not 0 <= entry.failed <= bound):
        raise PersistenceError(
            "archive supported engine coverage is inconsistent")
    if (ensemble_complete
            and (entry.evaluated != total or entry.failed != 0)):
        raise PersistenceError(
            "archive complete engine coverage is inconsistent")


def _validate_ensemble_coverage(
    plan: te.EnsembleWorkPlan, coverage: te.EnsembleCoverage,
) -> None:
    if (not isinstance(coverage.ensemble_complete, bool)
            or coverage.mode != plan.mode
            or coverage.allocated_contexts != plan.allocated_contexts
            or coverage.total_contexts != plan.total_contexts
            or isinstance(coverage.evaluated_contexts, bool)
            or not isinstance(coverage.evaluated_contexts, int)
            or not 0 <= coverage.evaluated_contexts <= plan.allocated_contexts):
        raise PersistenceError("archive ensemble coverage totals are inconsistent")
    if coverage.ensemble_complete != (
            coverage.evaluated_contexts == plan.total_contexts):
        raise PersistenceError("archive ensemble completion flag is inconsistent")
    if coverage.ensemble_complete and plan.allocated_contexts != plan.total_contexts:
        raise PersistenceError("archive complete coverage has a partial work plan")
    plan_by_kind = dict(plan.by_kind)
    if tuple(engine for engine, _kinds in coverage.engines) != tuple(
            engine for engine, _supported in _ENSEMBLE_ENGINE_SUPPORT):
        raise PersistenceError("archive ensemble coverage engine order is invalid")
    support_by_engine = dict(_ENSEMBLE_ENGINE_SUPPORT)
    for engine, kinds in coverage.engines:
        if tuple(kind for kind, _entry in kinds) != _ENSEMBLE_KINDS:
            raise PersistenceError("archive ensemble coverage kind order is invalid")
        supported_kinds = support_by_engine[engine]
        for kind, entry in kinds:
            _validate_engine_coverage_entry(
                engine, kind, entry, supported_kinds, plan_by_kind,
                ensemble_complete=coverage.ensemble_complete)


def _context_document(context: te.EnsembleContext) -> dict[str, object]:
    _check_load_cancelled()
    return {
        "kind": context.kind,
        "oligo_a": context.oligo_a,
        "variant_a": context.variant_a,
        "sequence_a": context.sequence_a,
        "oligo_b": context.oligo_b,
        "variant_b": context.variant_b,
        "sequence_b": context.sequence_b,
        "baseline": context.baseline,
    }


def _validate_manifest_plan_copy(
    stored: dict[str, object], plan: te.EnsembleWorkPlan,
) -> None:
    values = dict(stored)
    expected_keys = {
        "mode", "configured_additional_budget", "baseline_contexts",
        "additional_contexts", "allocated_contexts", "total_contexts",
        "by_kind", "contexts",
    }
    if set(values) != expected_keys:
        raise PersistenceError("archive manifest ensemble plan fields are invalid")
    for name in expected_keys - {"by_kind", "contexts"}:
        if values[name] != getattr(plan, name):
            raise PersistenceError(
                "archive ensemble plan disagrees with the scientific manifest")
    expected_by_kind = {
        kind: {
            "baseline": item.baseline, "additional": item.additional,
            "allocated": item.allocated, "total": item.total,
        }
        for kind, item in plan.by_kind
    }
    if values["by_kind"] != expected_by_kind:
        raise PersistenceError(
            "archive ensemble plan disagrees with the scientific manifest")
    contexts = values["contexts"]
    if not isinstance(contexts, list):
        raise PersistenceError("archive manifest ensemble contexts are invalid")
    sentinel = object()
    for stored_context, context in zip_longest(
            contexts, plan.contexts, fillvalue=sentinel):
        if (stored_context is sentinel or context is sentinel
                or stored_context != _context_document(context)):
            raise PersistenceError(
                "archive ensemble plan disagrees with the scientific manifest")


def _coverage_document(coverage: te.EnsembleCoverage) -> dict[str, object]:
    return {
        "mode": coverage.mode,
        "ensemble_complete": coverage.ensemble_complete,
        "evaluated_contexts": coverage.evaluated_contexts,
        "allocated_contexts": coverage.allocated_contexts,
        "total_contexts": coverage.total_contexts,
        "engines": {
            engine: {
                kind: {
                    "supported": entry.supported,
                    "evaluated": entry.evaluated,
                    "total": entry.total,
                    "failed": entry.failed,
                }
                for kind, entry in kinds
            }
            for engine, kinds in coverage.engines
        },
    }

def _validate_interaction_identity(
    interaction: te.InteractionResult, kind: str, label: str,
    variants_a: list[str], variants_b: list[str] | None,
) -> None:
    if interaction.kind != kind or interaction.label != label:
        raise PersistenceError(
            "archive interaction order or label is inconsistent")
    if interaction.sequence_a not in variants_a:
        raise PersistenceError("archive interaction sequence A is inconsistent")
    if variants_b is None:
        if interaction.sequence_b is not None:
            raise PersistenceError("archive hairpin sequence B is inconsistent")
    elif interaction.sequence_b not in variants_b:
        raise PersistenceError("archive interaction sequence B is inconsistent")


def _validate_structure_ownership(
    peer: te.PeerStructure, kind: str, expected_geometry_kind: str,
    variants_a: list[str], variants_b: list[str] | None,
) -> None:
    if peer.kind != kind:
        raise PersistenceError("archive structure ownership is inconsistent")
    geometry = peer.geometry
    if geometry is None:
        if any(observation.geometry is not None
               for observation in peer.engine_observations):
            raise PersistenceError(
                "archive engine observation geometry is inconsistent")
        return
    if geometry.kind != expected_geometry_kind:
        raise PersistenceError("archive structure geometry is inconsistent")
    if geometry.sequence_a not in variants_a:
        raise PersistenceError("archive structure geometry is inconsistent")
    if kind == "Hairpin":
        valid_b = geometry.sequence_b is None
    elif kind == "Self-dimer":
        valid_b = geometry.sequence_b in variants_a
    else:
        valid_b = variants_b is not None and geometry.sequence_b in variants_b
    if not valid_b:
        raise PersistenceError("archive structure geometry is inconsistent")
    for observation in peer.engine_observations:
        if observation.geometry is not None and observation.geometry != geometry:
            raise PersistenceError(
                "archive engine observation geometry is inconsistent")


def _validate_interaction(
    interaction: te.InteractionResult, kind: str, label: str,
    variants_a: list[str], variants_b: list[str] | None,
) -> None:
    _validate_interaction_identity(
        interaction, kind, label, variants_a, variants_b)
    geometry_kind = {
        "Hairpin": "hairpin", "Self-dimer": "self-dimer",
        "Hetero-dimer": "hetero-dimer",
    }[kind]
    for peer in interaction.structures:
        _check_load_cancelled()
        _validate_structure_ownership(
            peer, kind, geometry_kind, variants_a, variants_b)


def _validate_report_interactions(
    oligos: tuple[te.Oligo, ...], report: te.AnalysisReport,
) -> None:
    if len(report.hairpins) != len(oligos):
        raise PersistenceError("archive hairpin coverage is inconsistent")
    if len(report.self_dimers) != len(oligos):
        raise PersistenceError("archive self-dimer coverage is inconsistent")
    expected = len(oligos) * (len(oligos) - 1) // 2
    if len(report.hetero_dimers) != expected:
        raise PersistenceError("archive heterodimer coverage is inconsistent")
    for oligo, interaction in zip(oligos, report.hairpins):
        _validate_interaction(
            interaction, "Hairpin", oligo.name, oligo.variants, None)
    for oligo, interaction in zip(oligos, report.self_dimers):
        _validate_interaction(
            interaction, "Self-dimer", oligo.name,
            oligo.variants, oligo.variants)
    offset = 0
    for left in range(len(oligos)):
        for right in range(left + 1, len(oligos)):
            first, second = oligos[left], oligos[right]
            _validate_interaction(
                report.hetero_dimers[offset], "Hetero-dimer",
                f"{first.name} x {second.name}", first.variants,
                second.variants)
            offset += 1


def _validate_manifest_closure(
    oligos: tuple[te.Oligo, ...], conditions: te.ReactionConditions,
    report: te.AnalysisReport,
) -> None:
    manifest = report.manifest
    plan = report.ensemble_plan
    coverage = report.ensemble_coverage
    assert manifest is not None and plan is not None and coverage is not None
    expected_inputs = tuple(tuple(sorted({
        "input_order": index,
        "name": oligo.name,
        "sequence": oligo.seq,
        "role": oligo.role,
        "concentration_nM": float(oligo.conc_nM),
        "variant_count": oligo.n_variants,
    }.items())) for index, oligo in enumerate(oligos, 1))
    if manifest.normalized_inputs != expected_inputs:
        raise PersistenceError(
            "archive oligos disagree with the scientific manifest")
    expected_conditions = tuple(sorted(te._manifest_conditions(conditions).items()))
    if manifest.conditions != expected_conditions:
        raise PersistenceError(
            "archive conditions disagree with the scientific manifest")
    manifest_document = manifest.to_dict()
    _validate_manifest_plan_copy(manifest_document["ensemble_plan"], plan)
    if manifest_document["ensemble_coverage"] != _coverage_document(coverage):
        raise PersistenceError(
            "archive ensemble coverage disagrees with the scientific manifest")
    if manifest.diagnostics != report.diagnostics:
        raise PersistenceError(
            "archive diagnostics disagree with the scientific manifest")
    try:
        actual_id = _manifest_analysis_id(manifest)
    except (TypeError, ValueError) as exc:
        raise PersistenceError(
            "archive scientific manifest identity cannot be verified") from exc
    if (not isinstance(manifest.analysis_id, str)
            or len(manifest.analysis_id) != 64
            or not hmac.compare_digest(manifest.analysis_id, actual_id)):
        raise PersistenceError("archive scientific manifest identity is invalid")


def _validate_report_closure(
    oligos: tuple[te.Oligo, ...], conditions: te.ReactionConditions,
    report: te.AnalysisReport, *, require_canonical_tm_identity: bool = True,
) -> None:
    if not report.complete or report.phase != "complete":
        raise PersistenceError("archive report is not a complete final analysis")
    if (report.manifest is None or report.ensemble_plan is None
            or report.ensemble_coverage is None):
        raise PersistenceError(
            "archive report lacks manifest, ensemble plan, or ensemble coverage")
    if len(report.tms) != len(oligos):
        raise PersistenceError("archive Tm-to-oligo identity/order is invalid")
    for index, tm in enumerate(report.tms):
        if tm.oligo is oligos[index]:
            continue
        matches = [candidate for candidate, oligo in enumerate(oligos)
                   if oligo == tm.oligo]
        if (require_canonical_tm_identity or len(matches) != 1
                or matches[0] != index):
            raise PersistenceError(
                "archive Tm-to-oligo identity/order is invalid")
    _validate_archived_conditions(conditions)
    _validate_manifest_closure(oligos, conditions, report)
    _validate_report_interactions(oligos, report)
    _validate_ensemble_plan(oligos, report, report.ensemble_plan)
    _validate_ensemble_coverage(
        report.ensemble_plan, report.ensemble_coverage)


def _validate_source(
    oligos: Sequence[te.Oligo], report: te.AnalysisReport,
    conditions: te.ReactionConditions, ensemble_mode: str | None,
    ensemble_additional_budget: int | None,
) -> tuple[tuple[te.Oligo, ...], _ArchiveEncodingContext, str, int]:
    canonical = tuple(oligos)
    if not canonical:
        raise PersistenceError("archive requires at least one oligo")
    indices = {id(oligo): index for index, oligo in enumerate(canonical)}
    if len(indices) != len(canonical):
        raise PersistenceError("archive oligos must have distinct identities")
    for oligo in canonical:
        _validate_decoded_oligo(oligo)
    _validate_report_closure(
        canonical, conditions, report, require_canonical_tm_identity=False)
    manifest = report.manifest
    plan = report.ensemble_plan
    coverage = report.ensemble_coverage
    assert manifest is not None and plan is not None and coverage is not None
    if manifest.schema_version != te.sm.MANIFEST_SCHEMA_VERSION:
        raise PersistenceError(
            "archive manifest scientific identity is incompatible")
    if manifest.scientific_policy_version not in SUPPORTED_ANALYZED_RUN_POLICIES:
        raise PersistenceError("archive scientific policy is incompatible")
    if not manifest.application_version:
        raise PersistenceError("archive application version is invalid")
    mode = plan.mode if ensemble_mode is None else ensemble_mode
    budget = (plan.configured_additional_budget
              if ensemble_additional_budget is None
              else ensemble_additional_budget)
    if (mode != plan.mode or budget != plan.configured_additional_budget
            or coverage.mode != mode):
        raise PersistenceError(
            "ensemble controls do not match the completed report")
    return canonical, _ArchiveEncodingContext(canonical, indices), mode, budget


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=_ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    return info


def _write_member(
    archive: zipfile.ZipFile, name: str, chunks: Iterable[bytes],
    record_count: int, budget: _WriteBudget,
) -> _MemberDigest:
    digest = hashlib.sha256()
    size = 0
    try:
        with archive.open(_zip_info(name), "w") as member:
            for chunk in chunks:
                size += len(chunk)
                budget.add_bytes(len(chunk))
                digest.update(chunk)
                member.write(chunk)
    except PersistenceError:
        raise
    except (OSError, RuntimeError, zipfile.LargeZipFile) as exc:
        raise PersistenceError(
            f"cannot write analyzed-run member {name}: {exc}") from exc
    return _MemberDigest(name, record_count, size, digest.hexdigest())


def _control_chunks(value: object) -> Iterable[bytes]:
    _validate_json_tree(value, MAX_JSON_VALUES_PER_RECORD)
    payload = _canonical_bytes(value)
    if len(payload) > MAX_CONTROL_MEMBER_BYTES:
        raise PersistenceError("archive control member exceeds the 8 MiB limit")
    yield payload


def _record_bytes(ordinal: int, owner: object, value: object) -> bytes:
    envelope = {
        "ordinal": ordinal,
        "owner": owner,
        "value": value,
    }
    _validate_json_tree(envelope, MAX_JSON_VALUES_PER_RECORD)
    payload = _canonical_bytes(envelope)
    if len(payload) - 1 > MAX_JSONL_RECORD_BYTES:
        raise PersistenceError(
            "archive JSONL record exceeds the 16 MiB limit")
    return payload


def _jsonl_chunks(records: Iterable[tuple[object, object]]) -> Iterable[bytes]:
    for ordinal, (owner, value) in enumerate(records):
        yield _record_bytes(ordinal, owner, value)


def _oligo_records(
    oligos: tuple[te.Oligo, ...], context: _ArchiveEncodingContext,
) -> Iterable[tuple[object, object]]:
    for index, oligo in enumerate(oligos):
        yield {"oligo": index}, _encode_value(oligo, context)


def _tm_records(
    report: te.AnalysisReport, context: _ArchiveEncodingContext,
) -> Iterable[tuple[object, object]]:
    for index, result in enumerate(report.tms):
        yield {"oligo": index}, _encode_value(result, context)


def _diagnostic_records(
    report: te.AnalysisReport, context: _ArchiveEncodingContext,
) -> Iterable[tuple[object, object]]:
    for index, diagnostic in enumerate(report.diagnostics):
        yield {"diagnostic": index}, _encode_value(diagnostic, context)


def _context_records(
    report: te.AnalysisReport, context: _ArchiveEncodingContext,
) -> Iterable[tuple[object, object]]:
    assert report.ensemble_plan is not None
    for item in report.ensemble_plan.contexts:
        owner = {
            "kind": item.kind,
            "oligo_a": item.oligo_a,
            "oligo_b": item.oligo_b,
        }
        yield owner, _encode_value(item, context)


def _single_interaction_records(
    interactions: Sequence[te.InteractionResult],
    context: _ArchiveEncodingContext,
) -> Iterable[tuple[object, object]]:
    for index, interaction in enumerate(interactions):
        yield ({"oligo_a": index, "oligo_b": None},
               _encode_value(interaction, context))


def _heterodimer_records(
    oligos: tuple[te.Oligo, ...], report: te.AnalysisReport,
    context: _ArchiveEncodingContext,
) -> Iterable[tuple[object, object]]:
    offset = 0
    for left in range(len(oligos)):
        for right in range(left + 1, len(oligos)):
            yield ({"oligo_a": left, "oligo_b": right},
                   _encode_value(report.hetero_dimers[offset], context))
            offset += 1


def _partial_plan_document(
    plan: te.EnsembleWorkPlan, context: _ArchiveEncodingContext,
) -> dict[str, object]:
    return {
        name: _encode_value(getattr(plan, name), context)
        for name in _TYPE_FIELDS["EnsembleWorkPlan"] if name != "contexts"
    }


def _partial_manifest_document(
    manifest: te.sm.ScientificManifest,
    context: _ArchiveEncodingContext,
) -> dict[str, object]:
    return {
        "fields": {
            name: _encode_value(getattr(manifest, name), context)
            for name in _TYPE_FIELDS["ScientificManifest"]
            if name not in _MANIFEST_DERIVED_FIELDS
        }
    }


def _run_document(
    report: te.AnalysisReport, conditions: te.ReactionConditions,
    context: _ArchiveEncodingContext, mode: str, budget: int,
) -> dict[str, object]:
    manifest = report.manifest
    plan = report.ensemble_plan
    coverage = report.ensemble_coverage
    assert manifest is not None and plan is not None and coverage is not None
    return {
        "format": ANALYZED_RUN_FORMAT,
        "schema_version": ANALYZED_RUN_SCHEMA_VERSION,
        "application_version": manifest.application_version,
        "scientific_schema_version": te.sm.MANIFEST_SCHEMA_VERSION,
        "scientific_policy_version": manifest.scientific_policy_version,
        "conditions": _encode_value(conditions, context),
        "ensemble_mode": mode,
        "ensemble_additional_budget": budget,
        "report_complete": report.complete,
        "report_phase": report.phase,
        "ensemble_plan": _partial_plan_document(plan, context),
        "ensemble_coverage": _encode_value(coverage, context),
    }


def _index_document(members: Sequence[_MemberDigest]) -> dict[str, object]:
    return {
        "format": ANALYZED_RUN_FORMAT,
        "schema_version": ANALYZED_RUN_SCHEMA_VERSION,
        "members": [{
            "name": member.name,
            "record_count": member.record_count,
            "uncompressed_size": member.uncompressed_size,
            "sha256": member.sha256,
        } for member in members],
    }


def _preflight_streamed_record_count(
    oligos: tuple[te.Oligo, ...], report: te.AnalysisReport,
    plan: te.EnsembleWorkPlan,
) -> int:
    count = sum((
        len(oligos), len(report.tms), len(report.diagnostics),
        len(plan.contexts), len(report.hairpins), len(report.self_dimers),
        len(report.hetero_dimers),
    ))
    if count > MAX_STREAMED_RECORDS:
        raise PersistenceError(
            "streamed record count exceeds the limit of 5000000")
    return count


def _validate_written_compression(infos: Sequence[zipfile.ZipInfo]) -> None:
    expanded = sum(info.file_size for info in infos)
    compressed = sum(info.compress_size for info in infos)
    for info in infos:
        if (info.file_size and (
                info.compress_size == 0
                or info.file_size > info.compress_size * MAX_COMPRESSION_RATIO)):
            raise PersistenceError(
                f"archive member {info.filename} exceeds the 250:1 compression ratio")
    if expanded and (
            compressed == 0 or expanded > compressed * MAX_COMPRESSION_RATIO):
        raise PersistenceError("archive exceeds the 250:1 compression ratio")


def _write_analyzed_run_archive(
    handle: BinaryIO, oligos: Sequence[te.Oligo], report: te.AnalysisReport,
    conditions: te.ReactionConditions, *, ensemble_mode: str | None = None,
    ensemble_additional_budget: int | None = None,
) -> None:
    """Stream one complete schema-2 run into a caller-owned binary handle."""

    canonical, context, mode, budget = _validate_source(
        oligos, report, conditions, ensemble_mode, ensemble_additional_budget)
    manifest = report.manifest
    plan = report.ensemble_plan
    assert manifest is not None and plan is not None
    _preflight_streamed_record_count(canonical, report, plan)
    members: list[_MemberDigest] = []
    budget_state = _WriteBudget()
    try:
        start_offset = handle.tell()
        with zipfile.ZipFile(
            handle, "w", compression=zipfile.ZIP_DEFLATED,
            compresslevel=6, allowZip64=True, strict_timestamps=True,
        ) as archive:
            members.append(_write_member(
                archive, "run.json",
                _control_chunks(_run_document(
                    report, conditions, context, mode, budget)), 1,
                budget_state))
            record_sources: tuple[
                tuple[str, int, Iterable[tuple[object, object]]], ...
            ] = (
                ("oligos.jsonl", len(canonical),
                 _oligo_records(canonical, context)),
                ("tms.jsonl", len(report.tms), _tm_records(report, context)),
                ("diagnostics.jsonl", len(report.diagnostics),
                 _diagnostic_records(report, context)),
                ("ensemble-contexts.jsonl", len(plan.contexts),
                 _context_records(report, context)),
            )
            for name, count, records in record_sources:
                members.append(_write_member(
                    archive, name, _jsonl_chunks(records), count,
                    budget_state))
            members.append(_write_member(
                archive, "manifest.json",
                _control_chunks(_partial_manifest_document(manifest, context)),
                1, budget_state))
            interaction_sources = (
                ("hairpins.jsonl", len(report.hairpins),
                 _single_interaction_records(report.hairpins, context)),
                ("self-dimers.jsonl", len(report.self_dimers),
                 _single_interaction_records(report.self_dimers, context)),
                ("heterodimers.jsonl", len(report.hetero_dimers),
                 _heterodimer_records(canonical, report, context)),
            )
            for name, count, records in interaction_sources:
                members.append(_write_member(
                    archive, name, _jsonl_chunks(records), count,
                    budget_state))
            if tuple(member.name for member in members) != _INDEXED_MEMBERS:
                raise AssertionError("archive member plan is inconsistent")
            expanded_size = sum(member.uncompressed_size for member in members)
            if expanded_size > MAX_ARCHIVE_EXPANDED_BYTES:
                raise PersistenceError(
                    "expanded archive data exceeds the 512 MiB limit")
            streamed_records = sum(
                member.record_count for member in members
                if member.name in _JSONL_MEMBERS)
            if streamed_records > MAX_STREAMED_RECORDS:
                raise PersistenceError(
                    "streamed record count exceeds the limit of 5000000")
            _write_member(
                archive, "index.json",
                _control_chunks(_index_document(members)), 1, budget_state)
            _validate_written_compression(archive.infolist())
        if handle.tell() - start_offset > MAX_ARCHIVE_COMPRESSED_BYTES:
            raise PersistenceError("compressed archive exceeds the 256 MiB limit")
    except PersistenceError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile,
            zipfile.LargeZipFile, MemoryError) as exc:
        if isinstance(exc, MemoryError):
            message = "available memory was exhausted while writing the archive"
        else:
            message = f"cannot create analyzed-run archive: {exc}"
        raise PersistenceError(message) from exc


def write_analyzed_run_archive(
    handle: BinaryIO, oligos: Sequence[te.Oligo], report: te.AnalysisReport,
    conditions: te.ReactionConditions, *, ensemble_mode: str | None = None,
    ensemble_additional_budget: int | None = None,
) -> None:
    """Stream one complete schema-2 run into a caller-owned binary handle."""

    try:
        _write_analyzed_run_archive(
            handle, oligos, report, conditions, ensemble_mode=ensemble_mode,
            ensemble_additional_budget=ensemble_additional_budget)
    except PersistenceError:
        raise
    except MemoryError as exc:
        raise PersistenceError(
            "available memory was exhausted while writing the archive") from exc
    except (OSError, KeyError, TypeError, ValueError, AttributeError,
            IndexError, AssertionError, RuntimeError,
            zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise PersistenceError(
            f"analyzed-run data cannot be archived safely: {exc}") from exc


def encode_analyzed_run(
    oligos: Sequence[te.Oligo], report: te.AnalysisReport,
    conditions: te.ReactionConditions, *, ensemble_mode: str | None = None,
    ensemble_additional_budget: int | None = None,
) -> bytes:
    """Return schema-2 bytes for compatibility tests; production uses paths."""

    handle = io.BytesIO()
    write_analyzed_run_archive(
        handle, oligos, report, conditions, ensemble_mode=ensemble_mode,
        ensemble_additional_budget=ensemble_additional_budget)
    payload = handle.getvalue()
    if len(payload) > MAX_ARCHIVE_COMPRESSED_BYTES:
        raise PersistenceError("compressed archive exceeds the 256 MiB limit")
    return payload


def _member_path_is_safe(info: zipfile.ZipInfo) -> bool:
    name = info.filename
    if not name or "\\" in name or name.startswith(("/", "\\")):
        return False
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return False
    if info.is_dir() or name.endswith("/"):
        return False
    if info.create_system == 3 and stat.S_ISLNK(info.external_attr >> 16):
        return False
    return True


def _validate_central_directory(
    archive: zipfile.ZipFile, compressed_size: int,
) -> dict[str, zipfile.ZipInfo]:
    infos = archive.infolist()
    names = tuple(info.filename for info in infos)
    if len(infos) != len(ARCHIVE_MEMBERS) or names != ARCHIVE_MEMBERS:
        raise PersistenceError(
            "archive must contain exactly the ten ordered schema-2 members")
    if len(set(names)) != len(names):
        raise PersistenceError("archive contains duplicate member names")
    expanded = 0
    member_compressed = 0
    for info in infos:
        if not _member_path_is_safe(info):
            raise PersistenceError(f"archive member path is unsafe: {info.filename}")
        if info.flag_bits & 0x1:
            raise PersistenceError("encrypted archive members are not supported")
        if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
            raise PersistenceError("archive compression method is not supported")
        if info.file_size < 0 or info.compress_size < 0:
            raise PersistenceError("archive member size is invalid")
        if (info.filename not in _JSONL_MEMBERS
                and info.file_size > MAX_CONTROL_MEMBER_BYTES):
            raise PersistenceError(
                f"archive control member {info.filename} exceeds 8 MiB")
        if (info.file_size and (
                info.compress_size == 0
                or info.file_size > info.compress_size * MAX_COMPRESSION_RATIO)):
            raise PersistenceError(
                f"archive member {info.filename} exceeds the 250:1 compression ratio")
        expanded += info.file_size
        member_compressed += info.compress_size
    if expanded > MAX_ARCHIVE_EXPANDED_BYTES:
        raise PersistenceError("expanded archive data exceeds the 512 MiB limit")
    if (expanded and (member_compressed == 0
                      or expanded > member_compressed * MAX_COMPRESSION_RATIO)):
        raise PersistenceError("archive exceeds the 250:1 compression ratio")
    if compressed_size > MAX_ARCHIVE_COMPRESSED_BYTES:
        raise PersistenceError("compressed archive exceeds the 256 MiB limit")
    return {info.filename: info for info in infos}


def _read_member_bytes(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo, budget: _ReadBudget,
    maximum: int,
) -> tuple[bytes, str]:
    data = bytearray()
    digest = hashlib.sha256()
    try:
        with archive.open(info, "r") as member:
            while True:
                chunk = member.read(min(1024 * 1024, maximum + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                digest.update(chunk)
                budget.add_bytes(len(chunk))
                if len(data) > maximum:
                    raise PersistenceError(
                        f"archive member {info.filename} exceeds its size limit")
    except PersistenceError:
        raise
    except (OSError, EOFError, RuntimeError, zipfile.BadZipFile) as exc:
        raise PersistenceError(
            f"archive member {info.filename} is damaged or truncated") from exc
    return bytes(data), digest.hexdigest()


def _parse_control_member(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo, budget: _ReadBudget,
    description: str,
) -> tuple[dict, int, str]:
    raw, digest = _read_member_bytes(
        archive, info, budget, MAX_CONTROL_MEMBER_BYTES)
    value = _parse_json_bytes(
        raw, maximum=MAX_CONTROL_MEMBER_BYTES, description=description)
    if not isinstance(value, dict):
        raise PersistenceError(f"{description} must be a JSON object")
    return value, len(raw), digest


def _parse_index(
    archive: zipfile.ZipFile, infos: dict[str, zipfile.ZipInfo],
    budget: _ReadBudget,
) -> dict[str, _MemberDigest]:
    document, _size, _digest = _parse_control_member(
        archive, infos["index.json"], budget, "archive index")
    obj = _exact_keys(
        document, {"format", "schema_version", "members"}, "archive index")
    if obj["format"] != ANALYZED_RUN_FORMAT:
        raise PersistenceError("this is not an MBUprime StructLab analyzed run")
    if obj["schema_version"] != ANALYZED_RUN_SCHEMA_VERSION:
        raise PersistenceError("analyzed-run archive schema is not supported")
    raw_members = obj["members"]
    if not isinstance(raw_members, list) or len(raw_members) != len(_INDEXED_MEMBERS):
        raise PersistenceError("archive index member list is invalid")
    result: dict[str, _MemberDigest] = {}
    for expected_name, raw in zip(_INDEXED_MEMBERS, raw_members):
        item = _exact_keys(
            raw, {"name", "record_count", "uncompressed_size", "sha256"},
            f"archive index entry {expected_name}")
        name = item["name"]
        count = item["record_count"]
        size = item["uncompressed_size"]
        digest = item["sha256"]
        if name != expected_name:
            raise PersistenceError("archive index member order is invalid")
        if (isinstance(count, bool) or not isinstance(count, int) or count < 0
                or isinstance(size, bool) or not isinstance(size, int) or size < 0):
            raise PersistenceError("archive index count or size is invalid")
        if (not isinstance(digest, str) or len(digest) != 64
                or any(character not in "0123456789abcdef"
                       for character in digest)):
            raise PersistenceError("archive index checksum is invalid")
        if size != infos[name].file_size:
            raise PersistenceError("archive index size does not match the member")
        if name in _JSONL_MEMBERS:
            if count > MAX_STREAMED_RECORDS:
                raise PersistenceError(
                    "archive index record count exceeds the supported limit")
        elif count != 1:
            raise PersistenceError("archive control member count must be one")
        result[name] = _MemberDigest(name, count, size, digest)
    if sum(item.record_count for name, item in result.items()
           if name in _JSONL_MEMBERS) > MAX_STREAMED_RECORDS:
        raise PersistenceError(
            "streamed record count exceeds the limit of 5000000")
    return result


def _verify_member_digest(
    expected: _MemberDigest, actual_size: int, actual_digest: str,
) -> None:
    if actual_size != expected.uncompressed_size:
        raise PersistenceError(
            f"archive member {expected.name} size does not match its index")
    if not hmac.compare_digest(actual_digest, expected.sha256):
        raise PersistenceError(
            f"archive member {expected.name} checksum does not match; "
            "the file may be damaged")


def _read_control_checked(
    archive: zipfile.ZipFile, infos: dict[str, zipfile.ZipInfo],
    index: dict[str, _MemberDigest], budget: _ReadBudget, name: str,
    description: str,
) -> dict:
    value, size, digest = _parse_control_member(
        archive, infos[name], budget, description)
    _verify_member_digest(index[name], size, digest)
    return value


def _iter_jsonl_records(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo,
    expected: _MemberDigest, budget: _ReadBudget,
) -> Iterable[tuple[int, object, object]]:
    digest = hashlib.sha256()
    size = 0
    count = 0
    try:
        with archive.open(info, "r") as member:
            while True:
                line = member.readline(MAX_JSONL_RECORD_BYTES + 2)
                if not line:
                    break
                size += len(line)
                digest.update(line)
                budget.add_bytes(len(line))
                if len(line) > MAX_JSONL_RECORD_BYTES + 1:
                    raise PersistenceError(
                        f"archive record in {info.filename} exceeds 16 MiB")
                if not line.endswith(b"\n"):
                    raise PersistenceError(
                        f"archive member {info.filename} lacks its final LF")
                record_bytes = line[:-1]
                if not record_bytes or record_bytes.endswith(b"\r"):
                    raise PersistenceError(
                        f"archive member {info.filename} contains a blank or non-LF record")
                budget.add_record()
                record = _parse_json_bytes(
                    record_bytes, maximum=MAX_JSONL_RECORD_BYTES,
                    maximum_values=MAX_JSON_VALUES_PER_RECORD,
                    description=f"record {count} in {info.filename}")
                obj = _exact_keys(
                    record, {"ordinal", "owner", "value"},
                    f"record {count} in {info.filename}")
                ordinal = obj["ordinal"]
                if (isinstance(ordinal, bool) or not isinstance(ordinal, int)
                        or ordinal != count):
                    raise PersistenceError(
                        f"archive member {info.filename} record order is invalid")
                yield ordinal, obj["owner"], obj["value"]
                count += 1
    except PersistenceError:
        raise
    except (OSError, EOFError, RuntimeError, zipfile.BadZipFile) as exc:
        raise PersistenceError(
            f"archive member {info.filename} is damaged or truncated") from exc
    if count != expected.record_count:
        raise PersistenceError(
            f"archive member {info.filename} record count does not match its index")
    _verify_member_digest(expected, size, digest.hexdigest())


def _expect_owner(actual: object, expected: dict[str, object], name: str) -> None:
    if actual != expected:
        raise PersistenceError(f"archive record owner is invalid in {name}")


def _decode_oligos(
    archive: zipfile.ZipFile, infos: dict[str, zipfile.ZipInfo],
    index: dict[str, _MemberDigest], budget: _ReadBudget,
) -> tuple[te.Oligo, ...]:
    result: list[te.Oligo] = []
    for ordinal, owner, value in _iter_jsonl_records(
            archive, infos["oligos.jsonl"], index["oligos.jsonl"], budget):
        _expect_owner(owner, {"oligo": ordinal}, "oligos.jsonl")
        oligo = _decode_value(value)
        if not isinstance(oligo, te.Oligo):
            raise PersistenceError("archive oligo entry has the wrong type")
        _validate_decoded_oligo(oligo)
        result.append(oligo)
    if not result:
        raise PersistenceError("archive requires at least one oligo")
    return tuple(result)


def _decode_tms(
    archive: zipfile.ZipFile, infos: dict[str, zipfile.ZipInfo],
    index: dict[str, _MemberDigest], budget: _ReadBudget,
    oligos: tuple[te.Oligo, ...],
) -> list[te.TmResult]:
    result: list[te.TmResult] = []
    for ordinal, owner, value in _iter_jsonl_records(
            archive, infos["tms.jsonl"], index["tms.jsonl"], budget):
        _expect_owner(owner, {"oligo": ordinal}, "tms.jsonl")
        decoded = _decode_value(value, oligos)
        if not isinstance(decoded, te.TmResult):
            raise PersistenceError("archive Tm entry has the wrong type")
        result.append(decoded)
    return result


def _decode_diagnostics(
    archive: zipfile.ZipFile, infos: dict[str, zipfile.ZipInfo],
    index: dict[str, _MemberDigest], budget: _ReadBudget,
    oligos: tuple[te.Oligo, ...],
) -> tuple[te.sm.ScientificDiagnostic, ...]:
    result: list[te.sm.ScientificDiagnostic] = []
    for ordinal, owner, value in _iter_jsonl_records(
            archive, infos["diagnostics.jsonl"],
            index["diagnostics.jsonl"], budget):
        _expect_owner(owner, {"diagnostic": ordinal}, "diagnostics.jsonl")
        decoded = _decode_value(value, oligos)
        if not isinstance(decoded, te.sm.ScientificDiagnostic):
            raise PersistenceError("archive diagnostic entry has the wrong type")
        result.append(decoded)
    return tuple(result)


def _decode_contexts(
    archive: zipfile.ZipFile, infos: dict[str, zipfile.ZipInfo],
    index: dict[str, _MemberDigest], budget: _ReadBudget,
    oligos: tuple[te.Oligo, ...],
) -> tuple[te.EnsembleContext, ...]:
    result: list[te.EnsembleContext] = []
    for _ordinal, owner, value in _iter_jsonl_records(
            archive, infos["ensemble-contexts.jsonl"],
            index["ensemble-contexts.jsonl"], budget):
        decoded = _decode_value(value, oligos)
        if not isinstance(decoded, te.EnsembleContext):
            raise PersistenceError("archive ensemble context has the wrong type")
        _expect_owner(owner, {
            "kind": decoded.kind,
            "oligo_a": decoded.oligo_a,
            "oligo_b": decoded.oligo_b,
        }, "ensemble-contexts.jsonl")
        result.append(decoded)
    return tuple(result)


def _decode_plan(
    value: object, contexts: tuple[te.EnsembleContext, ...],
    oligos: tuple[te.Oligo, ...],
) -> te.EnsembleWorkPlan:
    expected = set(_TYPE_FIELDS["EnsembleWorkPlan"]) - {"contexts"}
    obj = _exact_keys(value, expected, "archive ensemble plan")
    kwargs = {name: _decode_value(obj[name], oligos) for name in expected}
    kwargs["contexts"] = contexts
    try:
        plan = te.EnsembleWorkPlan(**kwargs)
    except (TypeError, ValueError, te.SequenceError) as exc:
        raise PersistenceError(f"archive ensemble plan is invalid: {exc}") from exc
    if len(contexts) != plan.allocated_contexts:
        raise PersistenceError("archive ensemble context count is inconsistent")
    return plan


def _derived_manifest_fields(
    oligos: tuple[te.Oligo, ...], conditions: te.ReactionConditions,
    diagnostics: tuple[te.sm.ScientificDiagnostic, ...],
    plan: te.EnsembleWorkPlan, coverage: te.EnsembleCoverage,
) -> dict[str, object]:
    normalized_inputs = [{
        "input_order": index,
        "name": oligo.name,
        "sequence": oligo.seq,
        "role": oligo.role,
        "concentration_nM": float(oligo.conc_nM),
        "variant_count": oligo.n_variants,
    } for index, oligo in enumerate(oligos, 1)]
    return {
        "normalized_inputs": tuple(
            tuple((key, item[key]) for key in sorted(item))
            for item in normalized_inputs),
        "conditions": tuple(sorted(te._manifest_conditions(conditions).items())),
        "diagnostics": diagnostics,
        "ensemble_plan": tuple(sorted(plan.to_dict().items())),
        "ensemble_coverage": tuple(sorted(coverage.to_dict().items())),
    }


def _decode_manifest(
    document: object, oligos: tuple[te.Oligo, ...],
    conditions: te.ReactionConditions,
    diagnostics: tuple[te.sm.ScientificDiagnostic, ...],
    plan: te.EnsembleWorkPlan, coverage: te.EnsembleCoverage,
) -> te.sm.ScientificManifest:
    obj = _exact_keys(document, {"fields"}, "archive manifest member")
    stored_names = set(_TYPE_FIELDS["ScientificManifest"]) - _MANIFEST_DERIVED_FIELDS
    stored = _exact_keys(obj["fields"], stored_names, "archive manifest fields")
    kwargs = {name: _decode_value(stored[name], oligos) for name in stored_names}
    kwargs.update(_derived_manifest_fields(
        oligos, conditions, diagnostics, plan, coverage))
    try:
        return te.sm.ScientificManifest(**kwargs)
    except (TypeError, ValueError) as exc:
        raise PersistenceError(f"archive scientific manifest is invalid: {exc}") from exc


def _decode_single_interactions(
    archive: zipfile.ZipFile, infos: dict[str, zipfile.ZipInfo],
    index: dict[str, _MemberDigest], budget: _ReadBudget,
    oligos: tuple[te.Oligo, ...], name: str,
) -> list[te.InteractionResult]:
    result: list[te.InteractionResult] = []
    for ordinal, owner, value in _iter_jsonl_records(
            archive, infos[name], index[name], budget):
        _expect_owner(
            owner, {"oligo_a": ordinal, "oligo_b": None}, name)
        decoded = _decode_value(value, oligos)
        if not isinstance(decoded, te.InteractionResult):
            raise PersistenceError(f"archive interaction has the wrong type in {name}")
        result.append(decoded)
    return result


def _decode_heterodimers(
    archive: zipfile.ZipFile, infos: dict[str, zipfile.ZipInfo],
    index: dict[str, _MemberDigest], budget: _ReadBudget,
    oligos: tuple[te.Oligo, ...],
) -> list[te.InteractionResult]:
    owners = (
        {"oligo_a": left, "oligo_b": right}
        for left in range(len(oligos))
        for right in range(left + 1, len(oligos))
    )
    records = _iter_jsonl_records(
        archive, infos["heterodimers.jsonl"],
        index["heterodimers.jsonl"], budget)
    result: list[te.InteractionResult] = []
    for expected_owner, (_ordinal, owner, value) in zip(owners, records):
        _expect_owner(owner, expected_owner, "heterodimers.jsonl")
        decoded = _decode_value(value, oligos)
        if not isinstance(decoded, te.InteractionResult):
            raise PersistenceError("archive heterodimer has the wrong type")
        result.append(decoded)
    # Exhaust once more so surplus records cannot be hidden by zip().
    try:
        next(iter(records))
    except StopIteration:
        pass
    else:
        raise PersistenceError("archive heterodimer owner order is invalid")
    return result


def _decode_schema2_archive(
    source: BinaryIO | Path, compressed_size: int,
) -> LoadedAnalyzedRun:
    budget = _ReadBudget()
    try:
        with zipfile.ZipFile(source, "r", allowZip64=True) as archive:
            infos = _validate_central_directory(archive, compressed_size)
            index = _parse_index(archive, infos, budget)
            run = _read_control_checked(
                archive, infos, index, budget, "run.json", "archive run metadata")
            run = _exact_keys(run, {
                "format", "schema_version", "application_version",
                "scientific_schema_version", "scientific_policy_version",
                "conditions", "ensemble_mode", "ensemble_additional_budget",
                "report_complete", "report_phase", "ensemble_plan",
                "ensemble_coverage",
            }, "archive run metadata")
            if run["format"] != ANALYZED_RUN_FORMAT:
                raise PersistenceError(
                    "this is not an MBUprime StructLab analyzed run")
            if run["schema_version"] != ANALYZED_RUN_SCHEMA_VERSION:
                raise PersistenceError(
                    "analyzed-run archive schema is not supported")
            if run["scientific_schema_version"] != te.sm.MANIFEST_SCHEMA_VERSION:
                raise PersistenceError("archive scientific schema is incompatible")
            policy = run["scientific_policy_version"]
            if (not isinstance(policy, str)
                    or policy not in SUPPORTED_ANALYZED_RUN_POLICIES):
                raise PersistenceError("archive scientific policy is incompatible")
            application_version = run["application_version"]
            if not isinstance(application_version, str) or not application_version:
                raise PersistenceError("archive application version is invalid")
            if run["report_complete"] is not True or run["report_phase"] != "complete":
                raise PersistenceError(
                    "archive report is not a complete final analysis")
            oligos = _decode_oligos(archive, infos, index, budget)
            conditions = _decode_value(run["conditions"], oligos)
            if not isinstance(conditions, te.ReactionConditions):
                raise PersistenceError("archive conditions have the wrong type")
            _validate_archived_conditions(conditions)
            tms = _decode_tms(archive, infos, index, budget, oligos)
            diagnostics = _decode_diagnostics(
                archive, infos, index, budget, oligos)
            contexts = _decode_contexts(archive, infos, index, budget, oligos)
            plan = _decode_plan(run["ensemble_plan"], contexts, oligos)
            coverage = _decode_value(run["ensemble_coverage"], oligos)
            if not isinstance(coverage, te.EnsembleCoverage):
                raise PersistenceError("archive ensemble coverage has the wrong type")
            mode = run["ensemble_mode"]
            additional_budget = run["ensemble_additional_budget"]
            if mode not in {
                    te.ENSEMBLE_MODE_REPRESENTATIVE,
                    te.ENSEMBLE_MODE_BUDGETED_COMPLETE}:
                raise PersistenceError("archive ensemble mode is invalid")
            if (isinstance(additional_budget, bool)
                    or not isinstance(additional_budget, int)
                    or additional_budget < 0):
                raise PersistenceError("archive ensemble budget is invalid")
            if (plan.mode != mode
                    or plan.configured_additional_budget != additional_budget
                    or coverage.mode != mode):
                raise PersistenceError(
                    "archive ensemble controls disagree with the report")
            manifest_document = _read_control_checked(
                archive, infos, index, budget, "manifest.json",
                "archive scientific manifest")
            manifest = _decode_manifest(
                manifest_document, oligos, conditions, diagnostics, plan, coverage)
            if (manifest.schema_version != te.sm.MANIFEST_SCHEMA_VERSION
                    or manifest.application_version != application_version
                    or manifest.scientific_policy_version != policy):
                raise PersistenceError(
                    "archive envelope and manifest identities disagree")
            hairpins = _decode_single_interactions(
                archive, infos, index, budget, oligos, "hairpins.jsonl")
            self_dimers = _decode_single_interactions(
                archive, infos, index, budget, oligos, "self-dimers.jsonl")
            heterodimers = _decode_heterodimers(
                archive, infos, index, budget, oligos)
            report = te.AnalysisReport(
                tms=tms,
                hairpins=hairpins,
                self_dimers=self_dimers,
                hetero_dimers=heterodimers,
                manifest=manifest,
                diagnostics=diagnostics,
                ensemble_plan=plan,
                ensemble_coverage=coverage,
                complete=True,
                phase="complete",
            )
            _validate_report_closure(oligos, conditions, report)
            return LoadedAnalyzedRun(
                oligos, conditions, report, mode, additional_budget)
    except PersistenceError:
        raise
    except MemoryError as exc:
        raise PersistenceError(
            "available memory is insufficient to restore this analyzed run") from exc
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError,
            UnicodeError, json.JSONDecodeError, RuntimeError, OSError) as exc:
        raise PersistenceError(
            f"analyzed-run archive is damaged or cannot be read: {exc}") from exc


def _legacy_document(
    oligos: Sequence[te.Oligo], report: te.AnalysisReport,
    conditions: te.ReactionConditions, *, ensemble_mode: str | None = None,
    ensemble_additional_budget: int | None = None,
) -> dict[str, object]:
    canonical, context, mode, budget = _validate_source(
        oligos, report, conditions, ensemble_mode, ensemble_additional_budget)
    manifest = report.manifest
    assert manifest is not None
    payload = {
        "oligos": [_encode_value(item, context) for item in canonical],
        "conditions": _encode_value(conditions, context),
        "report": _encode_value(report, context),
        "ensemble_mode": mode,
        "ensemble_additional_budget": budget,
    }
    canonical_payload = _canonical_bytes(payload).rstrip(b"\n")
    document = {
        "format": ANALYZED_RUN_FORMAT,
        "schema_version": LEGACY_ANALYZED_RUN_SCHEMA_VERSION,
        "application_version": manifest.application_version,
        "scientific_schema_version": te.sm.MANIFEST_SCHEMA_VERSION,
        "scientific_policy_version": manifest.scientific_policy_version,
        "payload_sha256": hashlib.sha256(canonical_payload).hexdigest(),
        "payload": payload,
    }
    _validate_json_tree(document, MAX_LEGACY_JSON_VALUES)
    return document


def encode_legacy_analyzed_run(
    oligos: Sequence[te.Oligo], report: te.AnalysisReport,
    conditions: te.ReactionConditions, *, ensemble_mode: str | None = None,
    ensemble_additional_budget: int | None = None,
) -> bytes:
    """Encode legacy schema 1 for compatibility tests, never new GUI exports."""

    encoded = _canonical_bytes(_legacy_document(
        oligos, report, conditions, ensemble_mode=ensemble_mode,
        ensemble_additional_budget=ensemble_additional_budget))
    if len(encoded) > MAX_LEGACY_ANALYZED_RUN_BYTES:
        raise PersistenceError("legacy analyzed run exceeds the 64 MiB limit")
    return encoded


def _decode_legacy_envelope(payload: bytes) -> _DecodedLegacyEnvelope:
    document = _parse_json_bytes(
        payload, maximum=MAX_LEGACY_ANALYZED_RUN_BYTES,
        maximum_values=MAX_LEGACY_JSON_VALUES,
        description="legacy analyzed-run archive")
    obj = _exact_keys(document, {
        "format", "schema_version", "application_version",
        "scientific_schema_version", "scientific_policy_version",
        "payload_sha256", "payload",
    }, "legacy analyzed-run archive")
    if obj["format"] != ANALYZED_RUN_FORMAT:
        raise PersistenceError("this is not an MBUprime StructLab analyzed run")
    if obj["schema_version"] != LEGACY_ANALYZED_RUN_SCHEMA_VERSION:
        raise PersistenceError("analyzed-run archive schema is not supported")
    if obj["scientific_schema_version"] != te.sm.MANIFEST_SCHEMA_VERSION:
        raise PersistenceError("archive scientific schema is incompatible")
    policy = obj["scientific_policy_version"]
    if not isinstance(policy, str) or policy not in SUPPORTED_ANALYZED_RUN_POLICIES:
        raise PersistenceError("archive scientific policy is incompatible")
    application_version = obj["application_version"]
    if not isinstance(application_version, str) or not application_version:
        raise PersistenceError("archive application version is invalid")
    expected_digest = obj["payload_sha256"]
    if (not isinstance(expected_digest, str) or len(expected_digest) != 64
            or any(character not in "0123456789abcdef"
                   for character in expected_digest)):
        raise PersistenceError("archive checksum is invalid")
    # Stream canonical hashing: the C encoder's single large allocation holds
    # the GIL long enough to stall Tk even when called by the import worker.
    # iterencode preserves the exact bytes while allowing cancellation between
    # values and avoids retaining a second complete legacy JSON document.
    digest = hashlib.sha256()
    encoder = json.JSONEncoder(
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False)
    for index, chunk in enumerate(encoder.iterencode(obj["payload"])):
        if index % 1024 == 0:
            _check_load_cancelled()
        digest.update(chunk.encode("utf-8"))
    if not hmac.compare_digest(
            digest.hexdigest(), expected_digest):
        raise PersistenceError(
            "archive checksum does not match; the file may be damaged")
    run = _exact_keys(obj["payload"], {
        "oligos", "conditions", "report", "ensemble_mode",
        "ensemble_additional_budget",
    }, "legacy analyzed-run payload")
    return _DecodedLegacyEnvelope(run, application_version, policy)


def _decode_legacy_analyzed_run(payload: bytes) -> LoadedAnalyzedRun:
    envelope = _decode_legacy_envelope(payload)
    run = envelope.payload
    raw_oligos = run["oligos"]
    if not isinstance(raw_oligos, list) or not raw_oligos:
        raise PersistenceError("archive oligos must be a non-empty array")
    oligos = tuple(_decode_value(item) for item in raw_oligos)
    if not all(isinstance(item, te.Oligo) for item in oligos):
        raise PersistenceError("archive oligo entry has the wrong type")
    for oligo in oligos:
        _validate_decoded_oligo(oligo)
    conditions = _decode_value(run["conditions"], oligos)
    report = _decode_value(run["report"], oligos)
    if not isinstance(conditions, te.ReactionConditions):
        raise PersistenceError("archive conditions have the wrong type")
    if not isinstance(report, te.AnalysisReport):
        raise PersistenceError("archive report has the wrong type")
    mode = run["ensemble_mode"]
    budget = run["ensemble_additional_budget"]
    if mode not in {
            te.ENSEMBLE_MODE_REPRESENTATIVE,
            te.ENSEMBLE_MODE_BUDGETED_COMPLETE}:
        raise PersistenceError("archive ensemble mode is invalid")
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 0:
        raise PersistenceError("archive ensemble budget is invalid")
    if (report.manifest is None or report.ensemble_plan is None
            or report.ensemble_coverage is None):
        raise PersistenceError(
            "archive report lacks manifest, ensemble plan, or ensemble coverage")
    if (report.manifest.application_version != envelope.application_version
            or report.manifest.scientific_policy_version
            != envelope.scientific_policy_version):
        raise PersistenceError(
            "archive envelope and manifest identities disagree")
    if (report.ensemble_plan.mode != mode
            or report.ensemble_plan.configured_additional_budget != budget
            or report.ensemble_coverage.mode != mode):
        raise PersistenceError(
            "archive ensemble controls disagree with the report")
    _validate_report_closure(oligos, conditions, report)
    return LoadedAnalyzedRun(oligos, conditions, report, mode, budget)


def decode_analyzed_run(payload: bytes) -> LoadedAnalyzedRun:
    """Decode schema 2 bytes or a legacy schema-1 JSON document."""

    try:
        if not isinstance(payload, bytes):
            raise PersistenceError("analyzed-run archive must be bytes")
        if payload[:2] == b"PK":
            return _decode_schema2_archive(io.BytesIO(payload), len(payload))
        return _decode_legacy_analyzed_run(payload)
    except PersistenceError:
        raise
    except MemoryError as exc:
        raise PersistenceError(
            "available memory is insufficient to restore this analyzed run") from exc
    except (KeyError, TypeError, ValueError, AttributeError, IndexError,
            AssertionError) as exc:
        raise PersistenceError(
            f"analyzed-run data is inconsistent or unsupported: {exc}") from exc


def load_analyzed_run_archive(path: str | Path, *, cancel_event=None
                              ) -> LoadedAnalyzedRun:
    """Load on the calling thread, checking optional cancellation while decoding.

    Cancellation raises ArchiveLoadCancelled and closes all file handles. A
    returned run is fully validated; no partial result is returned or shared.
    Context-local cancellation permits independent concurrent callers.
    """
    token = _load_cancel_event.set(cancel_event)
    try:
        _check_load_cancelled()
        loaded = _load_analyzed_run_archive(path)
        _check_load_cancelled()
        return loaded
    finally:
        _load_cancel_event.reset(token)


def _load_analyzed_run_archive(path: str | Path) -> LoadedAnalyzedRun:
    """Load an analyzed run by magic without reading schema 2 into memory."""

    source = Path(path)
    try:
        size = source.stat().st_size
        if size > MAX_ARCHIVE_COMPRESSED_BYTES:
            raise PersistenceError("compressed archive exceeds the 256 MiB limit")
        with source.open("rb") as handle:
            magic = handle.read(4)
            handle.seek(0)
            if magic[:2] == b"PK":
                return _decode_schema2_archive(handle, size)
            if size > MAX_LEGACY_ANALYZED_RUN_BYTES:
                raise PersistenceError(
                    "legacy analyzed-run JSON exceeds the 64 MiB limit; "
                    "export it as a compressed .mbusl-run archive")
            payload = handle.read(MAX_LEGACY_ANALYZED_RUN_BYTES + 1)
        return _decode_legacy_analyzed_run(payload)
    except PersistenceError:
        raise
    except MemoryError as exc:
        raise PersistenceError(
            "available memory is insufficient to restore this analyzed run") from exc
    except (KeyError, TypeError, ValueError, AttributeError, IndexError,
            AssertionError) as exc:
        raise PersistenceError(
            f"analyzed-run data is inconsistent or unsupported: {exc}") from exc
    except OSError as exc:
        raise PersistenceError(f"cannot read analyzed-run archive: {exc}") from exc


def packaged_archive_self_test() -> dict[str, str]:
    """Exercise schema-2 and legacy path restore without invoking engines."""

    conditions = te.ReactionConditions()
    oligo = te.Oligo(
        "Archive self test", "GGGAAACCC", "primer",
        conditions.primer_conc, ["GGGAAACCC"])
    oligos = (oligo,)
    plan = te.plan_ensemble_work(
        list(oligos), mode=te.ENSEMBLE_MODE_REPRESENTATIVE,
        additional_budget=0)
    coverage = te.build_ensemble_coverage(plan, ())
    manifest = te.sm.build_manifest(
        normalized_inputs=[{
            "input_order": 1, "name": oligo.name,
            "sequence": oligo.seq, "role": oligo.role,
            "concentration_nM": float(oligo.conc_nM),
            "variant_count": oligo.n_variants,
        }],
        conditions=te._manifest_conditions(conditions), diagnostics=(),
        vienna_salt_applied=False, max_variants=1, enumeration_pool=1,
        min_sequence_length=1, max_sequence_length=100,
        engine_search_policies={}, final_union_truncated=False,
        degenerate_structure_scope="archive-codec-self-test",
        severity_policy={}, geometry_policy={}, variant_policy={}, engines={},
        ensemble_plan=plan.to_dict(), ensemble_coverage=coverage.to_dict())
    def empty_interaction(kind: str) -> te.InteractionResult:
        return te.InteractionResult(
            kind, oligo.name, oligo.seq,
            oligo.seq if kind == "Self-dimer" else None, [], 1, oligo.seq,
            "representative_variant_only", ())
    report = te.AnalysisReport(
        [te.TmResult(oligo, 30.0, 30.0, 30.0, 29.0, 29.0, 29.0)],
        [empty_interaction("Hairpin")],
        [empty_interaction("Self-dimer")], [], manifest, (), plan, coverage,
        True, "complete")
    # Keep path-based restore checks without creating a private child directory:
    # Windows' mode-0700 directory ACL excludes an AppContainer's package SID.
    # Deferred unlink permits reopening these files on Windows while each
    # context still owns cleanup, including when a restore fails.
    with tempfile.NamedTemporaryFile(
            mode="w+b", prefix="mbuprime-archive-probe-",
            suffix=".mbusl-run", delete_on_close=False) as schema2_file, \
            tempfile.NamedTemporaryFile(
                mode="w+b", prefix="mbuprime-archive-probe-",
                suffix=".json", delete_on_close=False) as legacy_file:
        write_analyzed_run_archive(schema2_file, oligos, report, conditions)
        schema2_file.flush()
        legacy_file.write(encode_legacy_analyzed_run(
            oligos, report, conditions))
        legacy_file.flush()
        restored = (
            load_analyzed_run_archive(schema2_file.name),
            load_analyzed_run_archive(legacy_file.name),
        )
    if any(item.oligos != oligos or item.conditions != conditions
           or item.report != report for item in restored):
        raise RuntimeError("analyzed-run archive self-test did not round-trip")
    return {
        "archive_schema": str(ANALYZED_RUN_SCHEMA_VERSION),
        "legacy_archive_schema": str(LEGACY_ANALYZED_RUN_SCHEMA_VERSION),
        "streamed_record_limit": str(MAX_STREAMED_RECORDS),
        "analysis_id": manifest.analysis_id,
    }
