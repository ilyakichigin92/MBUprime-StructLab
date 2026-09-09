"""Independent security and streaming contracts for analyzed-run schema 2."""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import stat
import struct
import tracemalloc
import warnings
import zipfile
from contextlib import contextmanager
from dataclasses import replace

import pytest

import analyzed_run_archive as archive
import scientific_metadata as sm
import thermo_engine as te


def _canonical_json(value: object) -> bytes:
    return (json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False) + "\n").encode("utf-8")


def _static_run(count: int = 1):
    """Build a complete, engine-free report from public scientific records."""

    conditions = te.ReactionConditions()
    oligos = tuple(te.Oligo(
        f"oligo-{index}", "GGGAAACCC" if index == 0 else "CCCAAAGGG",
        "primer", conditions.primer_conc,
        ["GGGAAACCC" if index == 0 else "CCCAAAGGG"],
    ) for index in range(count))
    plan = te.plan_ensemble_work(
        list(oligos), mode=te.ENSEMBLE_MODE_REPRESENTATIVE,
        additional_budget=0)
    coverage = te.build_ensemble_coverage(plan, ())
    hairpins = [te.InteractionResult(
        "Hairpin", item.name, item.seq, None, [], 1, item.seq,
        "representative_variant_only", ()) for item in oligos]
    self_dimers = [te.InteractionResult(
        "Self-dimer", item.name, item.seq, item.seq, [], 1, item.seq,
        "representative_variant_only", ()) for item in oligos]
    heterodimers = [te.InteractionResult(
        "Hetero-dimer", f"{left.name} x {right.name}", left.seq, right.seq,
        [], 1, f"{left.seq} / {right.seq}",
        "representative_variant_only", ())
        for left_index, left in enumerate(oligos)
        for right in oligos[left_index + 1:]]
    tms = [te.TmResult(item, 30.0, 29.5, 30.5, 29.0, 28.5, 29.5)
           for item in oligos]
    inputs = [{
        "input_order": index,
        "name": item.name,
        "sequence": item.seq,
        "role": item.role,
        "concentration_nM": float(item.conc_nM),
        "variant_count": item.n_variants,
    } for index, item in enumerate(oligos, 1)]
    manifest = sm.build_manifest(
        normalized_inputs=inputs, conditions=te._manifest_conditions(conditions),
        diagnostics=(), vienna_salt_applied=False, max_variants=1,
        enumeration_pool=1, min_sequence_length=1, max_sequence_length=100,
        engine_search_policies={}, final_union_truncated=False,
        degenerate_structure_scope="engine-free-test",
        severity_policy={}, geometry_policy={}, variant_policy={}, engines={},
        ensemble_plan=plan.to_dict(), ensemble_coverage=coverage.to_dict())
    report = te.AnalysisReport(
        tms, hairpins, self_dimers, heterodimers, manifest, (), plan,
        coverage, True, "complete")
    return oligos, conditions, report


def _entries(payload: bytes) -> list[tuple[str, bytes]]:
    with zipfile.ZipFile(io.BytesIO(payload)) as zipped:
        return [(info.filename, zipped.read(info)) for info in zipped.infolist()]


def _pack(entries: list[tuple[str, bytes]], *, method=zipfile.ZIP_STORED,
          info_hook=None) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", allowZip64=True) as zipped:
        for name, data in entries:
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = method
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            if info_hook is not None:
                info_hook(info)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                zipped.writestr(info, data)
    return output.getvalue()


def _replace_member(payload: bytes, name: str, replacement: bytes, *,
                    record_count: int | None = None) -> bytes:
    members = dict(_entries(payload))
    members[name] = replacement
    index = json.loads(members["index.json"])
    for item in index["members"]:
        data = members[item["name"]]
        item["uncompressed_size"] = len(data)
        item["sha256"] = hashlib.sha256(data).hexdigest()
        if item["name"] == name and record_count is not None:
            item["record_count"] = record_count
    members["index.json"] = _canonical_json(index)
    return _pack([(member, members[member]) for member in archive.ARCHIVE_MEMBERS])


def _base_archive(count: int = 1):
    oligos, conditions, report = _static_run(count)
    return archive.encode_analyzed_run(oligos, report, conditions), (
        oligos, conditions, report)


def _with_ensemble_metadata(report, plan, coverage):
    """Re-sign deliberately modified metadata so identity cannot mask defects."""

    unsigned = replace(
        report.manifest,
        ensemble_plan=tuple(sorted(plan.to_dict().items())),
        ensemble_coverage=tuple(sorted(coverage.to_dict().items())),
        analysis_id="",
    )
    manifest = replace(
        unsigned, analysis_id=archive._manifest_analysis_id(unsigned))
    return replace(
        report, ensemble_plan=plan, ensemble_coverage=coverage,
        manifest=manifest)


def test_schema2_is_deterministic_exact_ten_member_zip():
    payload, source = _base_archive()
    again = archive.encode_analyzed_run(source[0], source[2], source[1])
    assert payload == again
    assert payload.startswith(b"PK")
    assert archive.ANALYZED_RUN_SCHEMA_VERSION == 2
    assert archive.MAX_STREAMED_RECORDS == 5_000_000
    with zipfile.ZipFile(io.BytesIO(payload)) as zipped:
        assert tuple(zipped.namelist()) == archive.ARCHIVE_MEMBERS
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0)
                   for info in zipped.infolist())
        assert all(info.compress_type == zipfile.ZIP_DEFLATED
                   for info in zipped.infolist())
        index = json.loads(zipped.read("index.json"))
    assert [item["name"] for item in index["members"]] == list(
        archive.ARCHIVE_MEMBERS[:-1])
    assert sum(item["record_count"] for item in index["members"]
               if item["name"].endswith(".jsonl")) == 6


def test_packaged_archive_probe_restores_schema2_and_legacy_without_engines(
        monkeypatch):
    monkeypatch.setattr(
        te, "analyze",
        lambda *_args, **_kwargs: pytest.fail(
            "archive self-test must not run scientific analysis"))

    result = archive.packaged_archive_self_test()

    assert result["archive_schema"] == "2"
    assert result["legacy_archive_schema"] == "1"
    assert result["streamed_record_limit"] == "5000000"
    assert len(result["analysis_id"]) == 64


@pytest.mark.parametrize("failed_restore", [None, 1, 2])
def test_packaged_archive_probe_owns_files_through_path_restore(
        tmp_path, monkeypatch, failed_restore):
    create_file = archive.tempfile.NamedTemporaryFile
    load = archive.load_analyzed_run_archive
    created = []
    restored = []

    def tracked_file(*args, **kwargs):
        handle = create_file(*args, dir=tmp_path, **kwargs)
        created.append(Path(handle.name))
        return handle

    def checked_load(path):
        path = Path(path)
        assert path in created
        assert all(item.is_file() for item in created)
        restored.append(path.read_bytes()[:2])
        if len(restored) == failed_restore:
            raise archive.PersistenceError("injected restore failure")
        return load(path)

    monkeypatch.setattr(archive.tempfile, "NamedTemporaryFile", tracked_file)
    monkeypatch.setattr(archive, "load_analyzed_run_archive", checked_load)
    if failed_restore is None:
        assert archive.packaged_archive_self_test()["archive_schema"] == "2"
        assert restored[0] == b"PK"
        assert restored[1] != b"PK"
    else:
        with pytest.raises(archive.PersistenceError, match="injected restore"):
            archive.packaged_archive_self_test()
    assert len(created) == 2
    assert all(not path.exists() for path in created)
    assert not list(tmp_path.iterdir())


def test_packaged_archive_probe_cleans_first_file_if_second_creation_fails(
        tmp_path, monkeypatch):
    create_file = archive.tempfile.NamedTemporaryFile
    created = []

    def failing_second_file(*args, **kwargs):
        if created:
            raise PermissionError("injected second-file creation failure")
        handle = create_file(*args, dir=tmp_path, **kwargs)
        created.append(Path(handle.name))
        return handle

    monkeypatch.setattr(
        archive.tempfile, "NamedTemporaryFile", failing_second_file)
    with pytest.raises(PermissionError, match="second-file creation"):
        archive.packaged_archive_self_test()
    assert len(created) == 1
    assert not created[0].exists()


def test_packaged_archive_probe_does_not_suppress_cleanup_failure(
        tmp_path, monkeypatch):
    create_file = archive.tempfile.NamedTemporaryFile

    @contextmanager
    def failing_cleanup(*args, **kwargs):
        with create_file(*args, dir=tmp_path, **kwargs) as handle:
            yield handle
        raise PermissionError("injected cleanup failure")

    monkeypatch.setattr(archive.tempfile, "NamedTemporaryFile", failing_cleanup)
    with pytest.raises(PermissionError, match="injected cleanup"):
        archive.packaged_archive_self_test()
    assert not list(tmp_path.iterdir())


def test_schema2_path_roundtrip_uses_magic_and_never_read_bytes(
        tmp_path, monkeypatch):
    payload, (oligos, conditions, report) = _base_archive()
    path = tmp_path / "zip-content-with-json-name.json"
    path.write_bytes(payload)
    monkeypatch.setattr(
        Path, "read_bytes",
        lambda _self: (_ for _ in ()).throw(
            AssertionError("schema-2 path import must stream")))
    monkeypatch.setattr(
        te, "analyze",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("archive import must not run engines")))

    loaded = archive.load_analyzed_run_archive(path)

    assert loaded.oligos == oligos
    assert loaded.conditions == conditions
    assert loaded.report == report
    assert loaded.report.manifest.analysis_id == report.manifest.analysis_id


def test_legacy_json_is_selected_by_magic_not_extension(tmp_path):
    oligos, conditions, report = _static_run()
    path = tmp_path / "legacy-with-new-extension.mbusl-run"
    path.write_bytes(archive.encode_legacy_analyzed_run(
        oligos, report, conditions))

    loaded = archive.load_analyzed_run_archive(path)

    assert loaded.report == report
    assert loaded.oligos == oligos


def test_invalid_pk_magic_never_falls_back_to_legacy(tmp_path):
    path = tmp_path / "damaged.mbusl-run"
    path.write_bytes(b"PK this is neither ZIP nor JSON")
    with pytest.raises(archive.PersistenceError, match="damaged|read"):
        archive.load_analyzed_run_archive(path)


@pytest.mark.parametrize("mutation", [
    lambda entries: entries[:-1],
    lambda entries: entries + [("extra.json", b"{}\n")],
    lambda entries: [entries[1], entries[0], *entries[2:]],
    lambda entries: [(entries[0][0], entries[0][1]),
                     (entries[0][0], entries[1][1]), *entries[2:]],
    lambda entries: [("../run.json", entries[0][1]), *entries[1:]],
    lambda entries: [("/run.json", entries[0][1]), *entries[1:]],
    lambda entries: [("run.json/", entries[0][1]), *entries[1:]],
])
def test_container_rejects_missing_extra_reordered_duplicate_or_unsafe_names(
        mutation):
    payload, _source = _base_archive()
    hostile = _pack(mutation(_entries(payload)))
    with pytest.raises(archive.PersistenceError, match="ten ordered"):
        archive.decode_analyzed_run(hostile)


def test_container_rejects_symlink_encryption_and_unsupported_compression():
    payload, _source = _base_archive()
    entries = _entries(payload)

    def symlink_first(info):
        if info.filename == "run.json":
            info.external_attr = (stat.S_IFLNK | 0o777) << 16

    with pytest.raises(archive.PersistenceError, match="unsafe"):
        archive.decode_analyzed_run(_pack(entries, info_hook=symlink_first))
    with pytest.raises(archive.PersistenceError, match="compression method"):
        archive.decode_analyzed_run(_pack(entries, method=zipfile.ZIP_BZIP2))

    encrypted = bytearray(_pack(entries))
    local = encrypted.find(b"PK\x03\x04")
    central = encrypted.find(b"PK\x01\x02")
    struct.pack_into("<H", encrypted, local + 6,
                     struct.unpack_from("<H", encrypted, local + 6)[0] | 1)
    struct.pack_into("<H", encrypted, central + 8,
                     struct.unpack_from("<H", encrypted, central + 8)[0] | 1)
    with pytest.raises(archive.PersistenceError, match="encrypted"):
        archive.decode_analyzed_run(bytes(encrypted))


@pytest.mark.parametrize(("field", "value", "message"), [
    ("uncompressed_size", 1, "size"),
    ("record_count", 2, "count"),
    ("sha256", "0" * 64, "checksum"),
])
def test_index_size_count_and_hash_are_enforced(field, value, message):
    payload, _source = _base_archive()
    members = dict(_entries(payload))
    index = json.loads(members["index.json"])
    index["members"][0][field] = value
    members["index.json"] = _canonical_json(index)
    hostile = _pack([(name, members[name]) for name in archive.ARCHIVE_MEMBERS])
    with pytest.raises(archive.PersistenceError, match=message):
        archive.decode_analyzed_run(hostile)


def test_member_crc_corruption_is_actionable():
    payload, _source = _base_archive()
    stored = bytearray(_pack(_entries(payload)))
    with zipfile.ZipFile(io.BytesIO(stored)) as zipped:
        info = zipped.getinfo("run.json")
        offset = info.header_offset
        name_size, extra_size = struct.unpack_from("<HH", stored, offset + 26)
        data_offset = offset + 30 + name_size + extra_size
    stored[data_offset] ^= 1
    with pytest.raises(archive.PersistenceError, match="damaged|truncated"):
        archive.decode_analyzed_run(bytes(stored))


def test_declared_five_million_record_limit_is_inclusive():
    payload, _source = _base_archive()
    members = dict(_entries(payload))
    index = json.loads(members["index.json"])
    diagnostics = next(item for item in index["members"]
                       if item["name"] == "diagnostics.jsonl")
    diagnostics["record_count"] = 4_999_994
    members["index.json"] = _canonical_json(index)
    exact = _pack([(name, members[name]) for name in archive.ARCHIVE_MEMBERS])
    with pytest.raises(archive.PersistenceError, match="record count"):
        archive.decode_analyzed_run(exact)

    diagnostics["record_count"] += 1
    members["index.json"] = _canonical_json(index)
    over = _pack([(name, members[name]) for name in archive.ARCHIVE_MEMBERS])
    with pytest.raises(archive.PersistenceError, match="5000000"):
        archive.decode_analyzed_run(over)


@pytest.mark.parametrize(("mutate", "message"), [
    (lambda line: line[:-1], "final LF"),
    (lambda line: b"\n" + line, "blank|non-LF"),
    (lambda line: line[:-1] + b"\r\n", "blank|non-LF"),
    (lambda line: b"\xff" + line[1:], "UTF-8"),
    (lambda line: b'{"ordinal":0,' + line[1:], "duplicate JSON field"),
    (lambda line: line.replace(b'"ordinal":0', b'"ordinal":NaN'),
     "non-finite"),
])
def test_jsonl_framing_and_json_defenses(mutate, message):
    payload, _source = _base_archive()
    line = dict(_entries(payload))["oligos.jsonl"]
    hostile = _replace_member(payload, "oligos.jsonl", mutate(line))
    with pytest.raises(archive.PersistenceError, match=message):
        archive.decode_analyzed_run(hostile)


def test_jsonl_depth_value_and_record_size_limits(monkeypatch):
    payload, _source = _base_archive()
    line = json.loads(dict(_entries(payload))["oligos.jsonl"])
    nested: object = 0
    for _ in range(65):
        nested = [nested]
    line["owner"] = nested
    deep = _replace_member(
        payload, "oligos.jsonl", _canonical_json(line))
    with pytest.raises(archive.PersistenceError, match="nesting"):
        archive.decode_analyzed_run(deep)

    monkeypatch.setattr(archive, "MAX_JSON_VALUES_PER_RECORD", 2)
    with pytest.raises(archive.PersistenceError, match="limit of 2"):
        archive.decode_analyzed_run(payload)
    monkeypatch.setattr(archive, "MAX_JSON_VALUES_PER_RECORD", 250_000)
    monkeypatch.setattr(archive, "MAX_JSONL_RECORD_BYTES", 32)
    with pytest.raises(archive.PersistenceError, match="exceeds 16 MiB"):
        archive.decode_analyzed_run(payload)


@pytest.mark.parametrize(("member", "mutate", "message"), [
    ("oligos.jsonl", lambda obj: obj.__setitem__("ordinal", 1), "order"),
    ("oligos.jsonl", lambda obj: obj.__setitem__("owner", {"oligo": 1}),
     "owner"),
    ("tms.jsonl", lambda obj: obj["value"]["oligo"].__setitem__("index", -1),
     "non-negative"),
    ("run.json", lambda obj: obj.__setitem__("schema_version", 999), "schema"),
    ("run.json", lambda obj: obj.__setitem__("scientific_policy_version", "x"),
     "policy"),
    ("manifest.json", lambda obj: obj["fields"].__setitem__(
        "analysis_id", "0" * 64), "identity"),
    ("hairpins.jsonl", lambda obj: obj["value"].__setitem__(
        "label", "wrong"), "order or label"),
])
def test_record_references_ownership_identity_and_order(
        member, mutate, message):
    payload, _source = _base_archive()
    raw = dict(_entries(payload))[member]
    document = json.loads(raw)
    mutate(document)
    hostile = _replace_member(payload, member, _canonical_json(document))
    with pytest.raises(archive.PersistenceError, match=message):
        archive.decode_analyzed_run(hostile)


def test_interaction_cardinality_is_checked_after_valid_streaming():
    payload, _source = _base_archive(count=2)
    hostile = _replace_member(
        payload, "heterodimers.jsonl", b"", record_count=0)
    with pytest.raises(archive.PersistenceError, match="heterodimer coverage"):
        archive.decode_analyzed_run(hostile)


def test_five_million_record_budget_is_inclusive_without_allocating_records():
    budget = archive._ReadBudget(records=archive.MAX_STREAMED_RECORDS - 1)
    budget.add_record()
    assert budget.records == 5_000_000
    with pytest.raises(archive.PersistenceError, match="5000000"):
        budget.add_record()


@pytest.mark.parametrize("mutation", [
    "impossible_total",
    "mode",
    "budget",
    "arithmetic",
    "context_order",
    "by_kind",
    "coverage_bounds",
    "completeness",
    "engine_matrix",
])
def test_writer_rejects_scientifically_impossible_ensemble_metadata(mutation):
    """The planner is the independent oracle, not the archive's stored ID."""

    oligos, conditions, report = _static_run(count=2)
    plan, coverage = report.ensemble_plan, report.ensemble_coverage
    assert plan is not None and coverage is not None
    if mutation == "impossible_total":
        plan = replace(plan, total_contexts=999)
        coverage = replace(
            coverage, total_contexts=999, evaluated_contexts=999,
            ensemble_complete=True)
    elif mutation == "mode":
        plan = replace(plan, mode="invented")
        coverage = replace(coverage, mode="invented")
    elif mutation == "budget":
        plan = replace(plan, configured_additional_budget=-1)
    elif mutation == "arithmetic":
        plan = replace(plan, additional_contexts=1)
    elif mutation == "context_order":
        plan = replace(plan, contexts=tuple(reversed(plan.contexts)))
    elif mutation == "by_kind":
        plan = replace(plan, by_kind=tuple(reversed(plan.by_kind)))
    elif mutation == "coverage_bounds":
        coverage = replace(
            coverage, evaluated_contexts=coverage.allocated_contexts + 1)
    elif mutation == "completeness":
        coverage = replace(coverage, ensemble_complete=True)
    else:
        engines = list(coverage.engines)
        engines[0] = (engines[0][0], tuple(reversed(engines[0][1])))
        coverage = replace(coverage, engines=tuple(engines))
    hostile = _with_ensemble_metadata(report, plan, coverage)

    with pytest.raises(archive.PersistenceError, match="ensemble|mode|budget"):
        archive.encode_analyzed_run(oligos, hostile, conditions)


def test_coverage_retry_failures_do_not_consume_success_allocation():
    """Failed attempts and successful terminal evaluations are separate ledgers."""

    oligos, conditions, report = _static_run(count=2)
    plan, coverage = report.ensemble_plan, report.ensemble_coverage
    assert plan is not None and coverage is not None
    engines = list(coverage.engines)
    engine, raw_kinds = engines[0]
    kinds = list(raw_kinds)
    kind, entry = kinds[0]
    allocation = plan.kind(kind).allocated
    kinds[0] = (kind, replace(
        entry, evaluated=allocation, failed=1))
    engines[0] = (engine, tuple(kinds))
    coverage = replace(coverage, engines=tuple(engines))
    retry_report = _with_ensemble_metadata(report, plan, coverage)

    restored = archive.decode_analyzed_run(archive.encode_analyzed_run(
        oligos, retry_report, conditions))

    assert restored.report.ensemble_coverage == coverage


def test_primer3_full_coverage_can_exceed_partial_peer_allocation():
    conditions = te.ReactionConditions()
    oligos = tuple(te.Oligo(
        f"degenerate-{index}", "AAAAN", "primer", conditions.primer_conc)
        for index in range(2))
    plan = te.plan_ensemble_work(
        list(oligos), mode=te.ENSEMBLE_MODE_BUDGETED_COMPLETE,
        additional_budget=13)
    assert plan.kind("hetero_dimer").allocated == 2
    assert plan.kind("hetero_dimer").total == 16
    _source_oligos, _source_conditions, source_report = _static_run(count=2)
    coverage = te.build_ensemble_coverage(plan, ())
    engines = []
    for engine, raw_kinds in coverage.engines:
        kinds = []
        for kind, entry in raw_kinds:
            if entry.supported:
                evaluated = (entry.total if engine == "Primer3"
                             else plan.kind(kind).allocated)
                entry = replace(entry, evaluated=evaluated)
            kinds.append((kind, entry))
        engines.append((engine, tuple(kinds)))
    coverage = replace(
        coverage, evaluated_contexts=plan.allocated_contexts,
        engines=tuple(engines))
    interactions = tuple(te.InteractionResult(
        "Hairpin", oligo.name, oligo.variants[0], None, [], oligo.n_variants,
        oligo.variants[0], "budgeted_complete", ()) for oligo in oligos)
    self_dimers = tuple(te.InteractionResult(
        "Self-dimer", oligo.name, oligo.variants[0], oligo.variants[0], [],
        oligo.n_variants, oligo.variants[0], "budgeted_complete", ())
        for oligo in oligos)
    heterodimer = te.InteractionResult(
        "Hetero-dimer", f"{oligos[0].name} x {oligos[1].name}",
        oligos[0].variants[0], oligos[1].variants[0], [], 16,
        f"{oligos[0].variants[0]} / {oligos[1].variants[0]}",
        "budgeted_complete", ())
    report = replace(
        source_report,
        tms=tuple(te.TmResult(item, 30.0, 29.0, 31.0) for item in oligos),
        hairpins=interactions, self_dimers=self_dimers,
        hetero_dimers=(heterodimer,), ensemble_plan=plan,
        ensemble_coverage=coverage)
    inputs = [{
        "input_order": index, "name": item.name, "sequence": item.seq,
        "role": item.role, "concentration_nM": float(item.conc_nM),
        "variant_count": item.n_variants,
    } for index, item in enumerate(oligos, 1)]
    manifest = sm.build_manifest(
        normalized_inputs=inputs, conditions=te._manifest_conditions(conditions),
        diagnostics=(), vienna_salt_applied=False, max_variants=256,
        enumeration_pool=1, min_sequence_length=1, max_sequence_length=100,
        engine_search_policies={}, final_union_truncated=False,
        degenerate_structure_scope="coverage-contract", severity_policy={},
        geometry_policy={}, variant_policy={}, engines={},
        ensemble_plan=plan.to_dict(), ensemble_coverage=coverage.to_dict())
    report = replace(report, manifest=manifest)

    restored = archive.decode_analyzed_run(archive.encode_analyzed_run(
        oligos, report, conditions))

    assert restored.report.ensemble_coverage == coverage


def test_writer_enforces_json_value_and_depth_caps(monkeypatch):
    oligos, conditions, report = _static_run()
    monkeypatch.setattr(archive, "MAX_JSON_VALUES_PER_RECORD", 2)
    with pytest.raises(archive.PersistenceError, match="limit of 2"):
        archive.encode_analyzed_run(oligos, report, conditions)

    monkeypatch.setattr(archive, "MAX_JSON_VALUES_PER_RECORD", 250_000)
    nested: object = "leaf"
    for _ in range(archive.MAX_JSON_DEPTH + 1):
        nested = [nested]
    with pytest.raises(archive.PersistenceError, match="nesting"):
        archive._record_bytes(0, {"oligo": 0}, nested)


@pytest.mark.parametrize(("constant", "value", "message"), [
    ("MAX_CONTROL_MEMBER_BYTES", 64, "control member"),
    ("MAX_JSONL_RECORD_BYTES", 32, "JSONL record"),
    ("MAX_ARCHIVE_EXPANDED_BYTES", 128, "expanded|expanded-size"),
    ("MAX_STREAMED_RECORDS", 5, "record count"),
])
def test_writer_never_emits_an_archive_beyond_configured_limits(
        monkeypatch, constant, value, message):
    oligos, conditions, report = _static_run()
    monkeypatch.setattr(archive, constant, value)
    with pytest.raises(archive.PersistenceError, match=message):
        archive.encode_analyzed_run(oligos, report, conditions)


def test_writer_rejects_a_high_ratio_repetitive_diagnostics_archive():
    oligos, conditions, report = _static_run()
    diagnostics = tuple(sm.ScientificDiagnostic(
        "engine_exception", "RNAstructure", "dG", "A" * 1_000_000)
        for _ in range(50))
    manifest = replace(report.manifest, diagnostics=diagnostics, analysis_id="")
    manifest = replace(
        manifest, analysis_id=archive._manifest_analysis_id(manifest))
    report = replace(report, diagnostics=diagnostics, manifest=manifest)

    with pytest.raises(archive.PersistenceError, match="250:1"):
        archive.encode_analyzed_run(oligos, report, conditions)


def test_legacy_and_streamed_formats_preserve_the_exact_same_analysis_id():
    oligos, conditions, report = _static_run(count=2)
    legacy = archive.decode_analyzed_run(archive.encode_legacy_analyzed_run(
        oligos, report, conditions))
    streamed = archive.decode_analyzed_run(archive.encode_analyzed_run(
        oligos, report, conditions))
    assert legacy.report.manifest.analysis_id == report.manifest.analysis_id
    assert streamed.report.manifest.analysis_id == report.manifest.analysis_id
    assert legacy.report.manifest.analysis_id == streamed.report.manifest.analysis_id


def test_45450_context_export_validates_incrementally_under_32_mib(
        tmp_path):
    """45,450 is derived from 5 baseline + 45,445 additional contexts."""

    conditions = te.ReactionConditions()
    oligos = tuple(te.Oligo(
        f"large-degenerate-{index}", "ANNNN", "primer",
        conditions.primer_conc) for index in range(2))
    plan = te.plan_ensemble_work(
        list(oligos), mode=te.ENSEMBLE_MODE_BUDGETED_COMPLETE,
        additional_budget=45_445)
    assert plan.allocated_contexts == 45_450
    coverage = te.build_ensemble_coverage(plan, ())
    def interaction(kind, oligo):
        representative = oligo.variants[0]
        return te.InteractionResult(
            kind, oligo.name, representative,
            None if kind == "Hairpin" else representative, [], oligo.n_variants,
            representative, "budgeted_complete", ())
    tms = [te.TmResult(
        oligo, 30.0, 29.5, 30.5, 29.0, 28.5, 29.5) for oligo in oligos]
    inputs = [{
        "input_order": index, "name": oligo.name, "sequence": oligo.seq,
        "role": oligo.role, "concentration_nM": float(oligo.conc_nM),
        "variant_count": oligo.n_variants,
    } for index, oligo in enumerate(oligos, 1)]
    hairpins = [interaction("Hairpin", item) for item in oligos]
    self_dimers = [interaction("Self-dimer", item) for item in oligos]
    heterodimers = [te.InteractionResult(
        "Hetero-dimer", f"{oligos[0].name} x {oligos[1].name}",
        oligos[0].variants[0], oligos[1].variants[0], [], 65_536,
        f"{oligos[0].variants[0]} / {oligos[1].variants[0]}",
        "budgeted_complete", ())]
    provisional = te.AnalysisReport(
        tms, hairpins, self_dimers, heterodimers, None, (), plan, coverage,
        True, "complete")
    plan = replace(plan, contexts=tuple(
        te.iter_resolved_ensemble_contexts(list(oligos), provisional, plan)))
    coverage = te.build_ensemble_coverage(plan, ())
    manifest = sm.build_manifest(
        normalized_inputs=inputs, conditions=te._manifest_conditions(conditions),
        diagnostics=(), vienna_salt_applied=False, max_variants=256,
        enumeration_pool=1, min_sequence_length=1, max_sequence_length=100,
        engine_search_policies={}, final_union_truncated=False,
        degenerate_structure_scope="memory-contract", severity_policy={},
        geometry_policy={}, variant_policy={}, engines={},
        ensemble_plan=plan.to_dict(), ensemble_coverage=coverage.to_dict())
    report = te.AnalysisReport(
        tms, hairpins, self_dimers, heterodimers,
        manifest, (), plan, coverage, True, "complete")
    destination = tmp_path / "large.mbusl-run"

    tracemalloc.start()
    with destination.open("wb") as handle:
        archive.write_analyzed_run_archive(
            handle, oligos, report, conditions,
            ensemble_mode=te.ENSEMBLE_MODE_BUDGETED_COMPLETE,
            ensemble_additional_budget=45_445)
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert peak < 32 * 1024 * 1024
    assert archive.load_analyzed_run_archive(
        destination).report.manifest.analysis_id == manifest.analysis_id


def test_expanded_and_compressed_capacity_checks_use_public_decoder(monkeypatch):
    payload, _source = _base_archive()
    expanded = sum(len(data) for _name, data in _entries(payload))
    monkeypatch.setattr(archive, "MAX_ARCHIVE_COMPRESSED_BYTES", len(payload))
    monkeypatch.setattr(archive, "MAX_ARCHIVE_EXPANDED_BYTES", expanded)
    assert archive.decode_analyzed_run(payload).report.complete
    monkeypatch.setattr(archive, "MAX_ARCHIVE_COMPRESSED_BYTES", len(payload) - 1)
    with pytest.raises(archive.PersistenceError, match="256 MiB"):
        archive.decode_analyzed_run(payload)
    monkeypatch.setattr(archive, "MAX_ARCHIVE_COMPRESSED_BYTES", 256 * 1024 * 1024)
    monkeypatch.setattr(archive, "MAX_ARCHIVE_EXPANDED_BYTES", expanded - 1)
    with pytest.raises(archive.PersistenceError, match="512 MiB"):
        archive.decode_analyzed_run(payload)


def test_zip64_is_accepted_and_excessive_compression_ratio_is_rejected():
    payload, (oligos, conditions, report) = _base_archive()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", allowZip64=True) as zipped:
        for name, data in _entries(payload):
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            with zipped.open(info, "w", force_zip64=True) as member:
                member.write(data)
    loaded = archive.decode_analyzed_run(output.getvalue())
    assert (loaded.oligos, loaded.conditions, loaded.report) == (
        oligos, conditions, report)

    entries = _entries(payload)
    hostile = [(name, b"0" * 100_000 if name == "diagnostics.jsonl" else data)
               for name, data in entries]
    with pytest.raises(archive.PersistenceError, match="250:1"):
        archive.decode_analyzed_run(_pack(
            hostile, method=zipfile.ZIP_DEFLATED))
