"""Transactional GUI report and structure-diagram save contracts."""

from __future__ import annotations

import errno
import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

import analyzed_run_archive as analyzed_archive
import gui_exports
import structure_draw
import thermo_engine as te


def _translator(language, key, **values):
    return f"{language}:{key}".format(**values)


def test_cancel_writes_nothing_and_shows_no_message(monkeypatch):
    calls = []
    monkeypatch.setattr(gui_exports.filedialog, "asksaveasfilename", lambda **_kwargs: "")
    monkeypatch.setattr(gui_exports, "format_export", lambda *_args, **_kwargs:
                        (_ for _ in ()).throw(AssertionError("formatted after cancel")))
    monkeypatch.setattr(gui_exports.messagebox, "showinfo", lambda *args, **kwargs: calls.append("info"))
    monkeypatch.setattr(gui_exports.messagebox, "showerror", lambda *args, **kwargs: calls.append("error"))

    assert gui_exports.save_export(
        "text", [], object(), object(), _translator) is None
    assert calls == []


def test_atomic_replacement_keeps_canonical_utf8_bytes(tmp_path):
    destination = tmp_path / "report.txt"
    destination.write_bytes(b"old bytes")
    canonical = "Tm=62.5\nПример\n".encode("utf-8")

    gui_exports.transactional_write(
        destination, lambda handle: handle.write(canonical))

    assert destination.read_bytes() == canonical
    assert list(tmp_path.iterdir()) == [destination]


@pytest.mark.parametrize("failure_stage", ["write", "flush", "fsync", "replace"])
def test_write_flush_or_replace_failure_preserves_existing_destination(
        tmp_path, monkeypatch, failure_stage):
    destination = tmp_path / "report.txt"
    original = b"byte-identical original"
    destination.write_bytes(original)

    if failure_stage == "write":
        def writer(handle):
            handle.write(b"partial")
            raise OSError(errno.EIO, "injected write failure")
    else:
        writer = lambda handle: handle.write(b"replacement")
    if failure_stage == "fsync":
        monkeypatch.setattr(
            gui_exports.os, "fsync",
            lambda _fd: (_ for _ in ()).throw(OSError(errno.ENOSPC, "full")))
    if failure_stage == "flush":
        monkeypatch.setattr(
            gui_exports, "_flush_and_sync",
            lambda _handle: (_ for _ in ()).throw(OSError(errno.EIO, "flush")))
    if failure_stage == "replace":
        monkeypatch.setattr(
            gui_exports.os, "replace",
            lambda *_args: (_ for _ in ()).throw(PermissionError("locked")))

    with pytest.raises(OSError):
        gui_exports.transactional_write(destination, writer)
    assert destination.read_bytes() == original
    assert list(tmp_path.iterdir()) == [destination]


def test_export_failure_never_shows_success_and_includes_selected_path(
        tmp_path, monkeypatch):
    destination = tmp_path / "report.txt"
    destination.write_bytes(b"original")
    dialogs = []
    monkeypatch.setattr(
        gui_exports.filedialog, "asksaveasfilename",
        lambda **_kwargs: str(destination))
    monkeypatch.setattr(gui_exports, "format_export", lambda *_args, **_kwargs: "new")
    monkeypatch.setattr(
        gui_exports, "transactional_write",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            PermissionError(errno.EACCES, "read-only")))
    monkeypatch.setattr(gui_exports.messagebox, "showerror",
                        lambda title, body: dialogs.append(("error", title, body)))
    monkeypatch.setattr(gui_exports.messagebox, "showinfo",
                        lambda *args, **kwargs: dialogs.append(("success", args, kwargs)))

    assert gui_exports.save_export(
        "text", [], object(), object(), _translator, "en") is None
    assert destination.read_bytes() == b"original"
    assert len(dialogs) == 1 and dialogs[0][0] == "error"
    assert str(destination) in dialogs[0][2]
    assert "Controlled Folder Access" in dialogs[0][2]


@pytest.mark.parametrize(
    "error,category", [
        (PermissionError(errno.EACCES, "denied"), "access"),
        (OSError(errno.ENOSPC, "full"), "disk_full"),
        (FileNotFoundError(errno.ENOENT, "missing"), "invalid_destination"),
        (OSError(errno.EBUSY, "busy"), "busy"),
        (OSError(getattr(errno, "ENETUNREACH", errno.EIO), "network"), "network_cloud"),
        (RuntimeError("surprise"), "unexpected"),
    ],
)
def test_save_error_classification_and_en_ru_recovery(error, category):
    assert gui_exports.classify_save_error(error) == category
    english = gui_exports.localized_save_error("C:/chosen/report.txt", error, "en")
    russian = gui_exports.localized_save_error("C:/chosen/report.txt", error, "ru")
    assert "C:/chosen/report.txt" in english and "Selected path" in english
    assert "C:/chosen/report.txt" in russian and "Выбранный путь" in russian
    assert english != russian


def test_diagram_save_is_transactional_and_localized(tmp_path, monkeypatch):
    destination = tmp_path / "diagram.svg"
    destination.write_bytes(b"old diagram")

    class Figure:
        def savefig(self, handle, **options):
            assert options["format"] == "svg"
            handle.write(b"partial diagram")
            raise OSError(errno.ENOSPC, "disk full")

    with pytest.raises(OSError):
        structure_draw.save_figure_transactionally(Figure(), str(destination))
    assert destination.read_bytes() == b"old diagram"
    assert list(tmp_path.iterdir()) == [destination]
    body = structure_draw._diagram_save_error(
        str(destination), OSError(errno.ENOSPC, "disk full"), "ru")
    assert str(destination) in body
    assert "свободного места" in body


def _completed_run():
    conditions = te.ReactionConditions(
        mv_conc=42.5, dv_conc=2.5, dntp_conc=0.5,
        primer_conc=275.0, probe_conc=125.0, dg_temp_c=30.0,
        dg_caution=-6.5, dg_problem=-11.25,
        near_duplicate_bond_difference=3,
        dimer_max_consecutive_gaps=1, dimer_max_total_gaps=2)
    oligos = te.build_oligo_list([
        ("alpha", "GGGAAACCC"), ("beta", "CCCAAAGGG")], conditions)
    report = te.analyze(
        oligos, conditions, ensemble_mode=te.ENSEMBLE_MODE_REPRESENTATIVE,
        ensemble_additional_budget=0)
    return oligos, conditions, report


def _resign_archive(document: dict) -> bytes:
    canonical = json.dumps(
        document["payload"], ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")
    document["payload_sha256"] = hashlib.sha256(canonical).hexdigest()
    return (json.dumps(
        document, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _encode_legacy_run(oligos, report, conditions) -> bytes:
    """Create schema-1 JSON only for compatibility/security regressions."""

    return analyzed_archive.encode_legacy_analyzed_run(
        oligos, report, conditions)


def test_condition_preset_crud_roundtrips_every_field_and_is_atomic(tmp_path):
    path = tmp_path / "condition-presets.json"
    conditions = te.ReactionConditions(
        mv_conc=42.5, dv_conc=2.5, dntp_conc=0.5,
        primer_conc=275.0, probe_conc=125.0, dg_temp_c=30.0,
        dg_caution=-6.5, dg_problem=-11.25,
        near_duplicate_bond_difference=3,
        dimer_max_consecutive_gaps=1, dimer_max_total_gaps=2)

    saved = gui_exports.upsert_condition_preset("  My   mix  ", conditions, path=path)
    assert saved == {"My mix": conditions}
    assert gui_exports.load_condition_presets(path) == saved
    assert list(tmp_path.glob("*.tmp")) == []
    assert gui_exports.delete_condition_preset("my mix", path=path) == {}


def test_application_copy_change_preserves_condition_preset_storage_contract(tmp_path):
    """Russian UI terminology must not migrate the persisted preset API."""

    path = tmp_path / "condition-presets.json"
    conditions = te.ReactionConditions(mv_conc=75.0, primer_conc=350.0)
    gui_exports.save_condition_presets({"Мои параметры": conditions}, path)
    document = json.loads(path.read_text(encoding="utf-8"))

    assert gui_exports.CONDITION_PRESET_FORMAT == (
        "mbuprime-structlab-condition-presets")
    assert gui_exports.CONDITION_PRESET_SCHEMA_VERSION == 1
    assert set(document) == {"format", "schema_version", "presets"}
    assert document["format"] == gui_exports.CONDITION_PRESET_FORMAT
    assert document["schema_version"] == 1
    assert gui_exports.load_condition_presets(path) == {
        "Мои параметры": conditions}
    with pytest.raises(gui_exports.PersistenceError):
        gui_exports.normalize_preset_name("Custom")


@pytest.mark.parametrize("name", ["", "Custom", "qPCR / TaqMan", "x" * 81])
def test_condition_preset_names_are_bounded_and_reserved(name):
    with pytest.raises(gui_exports.PersistenceError):
        gui_exports.normalize_preset_name(name)


def test_condition_preset_corruption_bounds_and_platform_paths(tmp_path):
    corrupt = tmp_path / "bad.json"
    corrupt.write_text('{"format":"x","format":"y"}', encoding="utf-8")
    with pytest.raises(gui_exports.PersistenceError, match="duplicate"):
        gui_exports.load_condition_presets(corrupt)
    oversized = tmp_path / "large.json"
    oversized.write_bytes(b" " * (gui_exports.MAX_CONDITION_PRESET_FILE_BYTES + 1))
    with pytest.raises(gui_exports.PersistenceError, match="size"):
        gui_exports.load_condition_presets(oversized)
    assert gui_exports.condition_preset_path(
        environment={"LOCALAPPDATA": "C:/Local"}, home="C:/Users/u",
        system_name="Windows") == Path(
            "C:/Local/MBUprime StructLab/condition-presets.json")
    assert gui_exports.condition_preset_path(
        environment={"XDG_CONFIG_HOME": "/cfg"}, home="/home/u",
        system_name="Linux") == Path(
            "/cfg/mbuprime-structlab/condition-presets.json")
    assert gui_exports.condition_preset_path(
        environment={}, home="/Users/u", system_name="Darwin") == Path(
            "/Users/u/Library/Application Support/MBUprime StructLab/condition-presets.json")
    assert gui_exports.condition_preset_path(
        environment={"LOCALAPPDATA": ""}, home="C:/Users/u",
        system_name="Windows") == Path(
            "C:/Users/u/AppData/Local/MBUprime StructLab/condition-presets.json")
    assert gui_exports.condition_preset_path(
        environment={"XDG_CONFIG_HOME": ""}, home="/home/u",
        system_name="Linux") == Path(
            "/home/u/.config/mbuprime-structlab/condition-presets.json")


def test_condition_preset_missing_partial_schema_count_and_failed_write(tmp_path,
                                                                        monkeypatch):
    path = tmp_path / "condition-presets.json"
    assert gui_exports.load_condition_presets(path) == {}

    path.write_text(json.dumps({
        "format": gui_exports.CONDITION_PRESET_FORMAT,
        "schema_version": 999,
        "presets": [],
    }), encoding="utf-8")
    with pytest.raises(gui_exports.PersistenceError, match="schema"):
        gui_exports.load_condition_presets(path)

    path.write_text(json.dumps({
        "format": gui_exports.CONDITION_PRESET_FORMAT,
        "schema_version": gui_exports.CONDITION_PRESET_SCHEMA_VERSION,
        "presets": [{"name": "partial"}],
    }), encoding="utf-8")
    with pytest.raises(gui_exports.PersistenceError, match="missing"):
        gui_exports.load_condition_presets(path)

    too_many = {
        f"preset {index}": te.ReactionConditions()
        for index in range(gui_exports.MAX_CONDITION_PRESETS + 1)}
    original = b"preserved preset bytes"
    path.write_bytes(original)
    with pytest.raises(gui_exports.PersistenceError, match="no more than"):
        gui_exports.save_condition_presets(too_many, path)
    assert path.read_bytes() == original

    monkeypatch.setattr(
        gui_exports, "_flush_and_sync",
        lambda _handle: (_ for _ in ()).throw(OSError(errno.EIO, "flush")))
    with pytest.raises(gui_exports.PersistenceError, match="cannot save"):
        gui_exports.save_condition_presets(
            {"valid": te.ReactionConditions()}, path)
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


def test_analyzed_run_is_deterministic_exact_and_projection_equivalent():
    oligos, conditions, report = _completed_run()
    first = gui_exports.encode_analyzed_run(oligos, report, conditions)
    second = gui_exports.encode_analyzed_run(oligos, report, conditions)
    assert first == second

    loaded = gui_exports.decode_analyzed_run(first)
    assert loaded.conditions == conditions
    assert loaded.report == report
    assert loaded.oligos == tuple(oligos)
    assert all(tm.oligo is loaded.oligos[index]
               for index, tm in enumerate(loaded.report.tms))
    for kind in ("text", "csv", "tsv", "flagged_csv", "matrix_csv"):
        assert gui_exports.format_export(
            kind, loaded.oligos, loaded.report, loaded.conditions) == (
                gui_exports.format_export(kind, oligos, report, conditions))


def test_analyzed_run_roundtrips_complete_degenerate_variant_union():
    conditions = te.ReactionConditions()
    oligos = te.build_oligo_list([("degenerate", "GGGAAANCC")], conditions)
    report = te.analyze(
        oligos, conditions,
        ensemble_mode=te.ENSEMBLE_MODE_BUDGETED_COMPLETE,
        ensemble_additional_budget=500)

    representative_sequences = {
        interaction.sequence_a
        for interaction in (*report.hairpins, *report.self_dimers)
    }
    retained_sequences = {
        peer.geometry.sequence_a
        for interaction in (*report.hairpins, *report.self_dimers)
        for peer in interaction.structures
        if peer.geometry is not None
    }
    assert retained_sequences - representative_sequences

    encoded = gui_exports.encode_analyzed_run(oligos, report, conditions)
    loaded = gui_exports.decode_analyzed_run(encoded)
    assert loaded.oligos == tuple(oligos)
    assert loaded.conditions == conditions
    assert loaded.report == report


def test_analyzed_run_rejects_tamper_versions_unknown_fields_and_depth():
    oligos, conditions, report = _completed_run()
    encoded = _encode_legacy_run(oligos, report, conditions)
    changed_manifest = replace(
        report.manifest, application_version="999.0", analysis_id="")
    changed_manifest = replace(
        changed_manifest,
        analysis_id=analyzed_archive._manifest_analysis_id(changed_manifest))
    changed_report = replace(report, manifest=changed_manifest)
    document = json.loads(
        _encode_legacy_run(oligos, changed_report, conditions))
    document["application_version"] = "999.0"
    assert gui_exports.decode_analyzed_run(_resign_archive(document)).report

    tampered = json.loads(encoded)
    tampered["payload"]["ensemble_additional_budget"] = 1
    with pytest.raises(gui_exports.PersistenceError, match="checksum"):
        gui_exports.decode_analyzed_run(json.dumps(tampered).encode())

    for field, value, match in (
        ("schema_version", 999, "archive schema"),
        ("scientific_schema_version", "999", "scientific schema"),
        ("scientific_policy_version", "other", "scientific policy"),
    ):
        incompatible = json.loads(encoded)
        incompatible[field] = value
        with pytest.raises(gui_exports.PersistenceError, match=match):
            gui_exports.decode_analyzed_run(json.dumps(incompatible).encode())

    unknown = json.loads(encoded)
    unknown["unexpected"] = True
    with pytest.raises(gui_exports.PersistenceError, match="unknown"):
        gui_exports.decode_analyzed_run(json.dumps(unknown).encode())
    deep = ("[" * 65 + "0" + "]" * 65).encode()
    with pytest.raises(gui_exports.PersistenceError, match="nesting"):
        gui_exports.decode_analyzed_run(deep)


def test_json_value_preflight_is_exact_and_rejects_before_json_loads(monkeypatch):
    text = r'{"a":[1,{"b":"escaped \" [,]: text"},null],"c":true}'
    assert gui_exports._count_json_values(text) == 7
    assert gui_exports._count_json_values(
        '{"empty":[],"object":{},"scalar":1}') == 4
    assert gui_exports._parse_json_bytes(
        b'[0,1]', maximum=100, maximum_values=3,
        description="test JSON") == [0, 1]

    called = False

    def unexpected_loads(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("json.loads must not run after preflight overflow")

    monkeypatch.setattr(gui_exports.json, "loads", unexpected_loads)
    with pytest.raises(gui_exports.PersistenceError, match="limit of 2"):
        gui_exports._parse_json_bytes(
            b'[0,1]', maximum=100, maximum_values=2,
            description="test JSON")
    assert called is False


def test_analyzed_run_known_historical_policy_is_exactly_restored():
    oligos, conditions, report = _completed_run()
    manifest = replace(
        report.manifest,
        application_version="2.3.5",
        scientific_policy_version=gui_exports.HISTORICAL_ANALYZED_RUN_POLICY,
        analysis_id="")
    manifest = replace(
        manifest, analysis_id=analyzed_archive._manifest_analysis_id(manifest))
    historical_report = replace(report, manifest=manifest)
    historical = gui_exports.encode_analyzed_run(
        oligos, historical_report, conditions)

    loaded = gui_exports.decode_analyzed_run(historical)

    assert loaded.report.manifest.application_version == "2.3.5"
    assert loaded.report.manifest.scientific_policy_version == (
        gui_exports.HISTORICAL_ANALYZED_RUN_POLICY)
    restored_again = gui_exports.decode_analyzed_run(
        gui_exports.encode_analyzed_run(
            loaded.oligos, loaded.report, loaded.conditions,
            ensemble_mode=loaded.ensemble_mode,
            ensemble_additional_budget=loaded.ensemble_additional_budget))
    assert restored_again.report == loaded.report


def test_analyzed_run_rejects_envelope_manifest_identity_mismatch():
    oligos, conditions, report = _completed_run()
    document = json.loads(
        _encode_legacy_run(oligos, report, conditions))
    document["application_version"] = "999.0"
    with pytest.raises(gui_exports.PersistenceError, match="identities disagree"):
        gui_exports.decode_analyzed_run(_resign_archive(document))


@pytest.mark.parametrize("invalid_policy", ([], {}))
def test_analyzed_run_rejects_non_string_policy_without_raw_type_error(
        invalid_policy):
    oligos, conditions, report = _completed_run()
    document = json.loads(
        _encode_legacy_run(oligos, report, conditions))
    document["scientific_policy_version"] = invalid_policy
    with pytest.raises(gui_exports.PersistenceError, match="scientific policy"):
        gui_exports.decode_analyzed_run(_resign_archive(document))

    document = json.loads(
        _encode_legacy_run(oligos, report, conditions))
    document["scientific_policy_version"] = (
        gui_exports.HISTORICAL_ANALYZED_RUN_POLICY)
    with pytest.raises(gui_exports.PersistenceError, match="identities disagree"):
        gui_exports.decode_analyzed_run(_resign_archive(document))


def test_analyzed_run_encoder_and_decoder_share_value_admissibility(monkeypatch):
    oligos, conditions, report = _completed_run()
    monkeypatch.setattr(analyzed_archive, "MAX_LEGACY_JSON_VALUES", 1)
    with pytest.raises(gui_exports.PersistenceError, match="limit of 1"):
        analyzed_archive.encode_legacy_analyzed_run(
            oligos, report, conditions)




def test_analyzed_run_rejects_incomplete_or_oversized_data():
    with pytest.raises(gui_exports.PersistenceError, match="complete final"):
        gui_exports.encode_analyzed_run(
            [te.Oligo("x", "GGGAAACCC", "primer", 300.0)],
            te.AnalysisReport(complete=False, phase="core_complete"),
            te.ReactionConditions())
    with pytest.raises(gui_exports.PersistenceError, match="exceeds"):
        gui_exports.decode_analyzed_run(
            b" " * (gui_exports.MAX_ANALYZED_RUN_BYTES + 1))


def test_analyzed_run_rejects_duplicate_nested_unknown_missing_and_negative_ref():
    oligos, conditions, report = _completed_run()
    encoded = _encode_legacy_run(oligos, report, conditions)
    duplicate = b'{"format":"duplicate",' + encoded[1:]
    with pytest.raises(gui_exports.PersistenceError, match="duplicate JSON field"):
        gui_exports.decode_analyzed_run(duplicate)

    unknown_tag = json.loads(encoded)
    unknown_tag["payload"]["conditions"] = {"$type": "UnknownConditions"}
    with pytest.raises(gui_exports.PersistenceError, match="unsupported archive type tag"):
        gui_exports.decode_analyzed_run(_resign_archive(unknown_tag))

    unknown_field = json.loads(encoded)
    unknown_field["payload"]["conditions"]["future_field"] = 1
    with pytest.raises(gui_exports.PersistenceError, match="unknown future_field"):
        gui_exports.decode_analyzed_run(_resign_archive(unknown_field))

    for field, message in (
        ("manifest", "lacks manifest"),
        ("ensemble_plan", "ensemble plan"),
        ("ensemble_coverage", "ensemble coverage"),
    ):
        missing = json.loads(encoded)
        missing["payload"]["report"][field] = None
        with pytest.raises(gui_exports.PersistenceError, match=message):
            gui_exports.decode_analyzed_run(_resign_archive(missing))

    incompatible_manifest = json.loads(encoded)
    incompatible_manifest["payload"]["report"]["manifest"][
        "scientific_policy_version"] = "other-policy"
    with pytest.raises(gui_exports.PersistenceError, match="identities disagree"):
        gui_exports.decode_analyzed_run(_resign_archive(incompatible_manifest))

    negative_ref = json.loads(encoded)
    negative_ref["payload"]["report"]["tms"]["items"][0]["oligo"]["index"] = -1
    with pytest.raises(gui_exports.PersistenceError, match="non-negative"):
        gui_exports.decode_analyzed_run(_resign_archive(negative_ref))


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda document: document["payload"]["conditions"].__setitem__(
            "dg_temp_c", 31.0), "conditions disagree"),
        (lambda document: document["payload"]["oligos"][0].__setitem__(
            "name", "tampered"), "oligos disagree|interaction order"),
        (lambda document: document["payload"]["conditions"].__setitem__(
            "mv_conc", True), "must be a finite number"),
    ],
)
def test_analyzed_run_rejects_rehashed_cross_object_inconsistency(
        mutate, message):
    oligos, conditions, report = _completed_run()
    document = json.loads(
        _encode_legacy_run(oligos, report, conditions))
    mutate(document)
    with pytest.raises(gui_exports.PersistenceError, match=message):
        gui_exports.decode_analyzed_run(_resign_archive(document))


def test_analyzed_run_rejects_rehashed_observation_geometry_mismatch():
    oligos, conditions, report = _completed_run()
    document = json.loads(
        _encode_legacy_run(oligos, report, conditions))
    observation_geometry = document["payload"]["report"]["hairpins"][
        "items"][0]["structures"]["items"][0]["engine_observations"][
            "items"][0]["geometry"]
    observation_geometry["sequence_a"] = "CCCCCCCCC"
    with pytest.raises(
            gui_exports.PersistenceError,
            match="engine observation geometry is inconsistent"):
        gui_exports.decode_analyzed_run(_resign_archive(document))


def test_analyzed_run_rejects_manifest_conflicts_in_contract_order():
    oligos, conditions, report = _completed_run()
    document = json.loads(
        _encode_legacy_run(oligos, report, conditions))
    document["payload"]["oligos"][0]["name"] = "tampered"
    document["payload"]["conditions"]["dg_temp_c"] = 31.0

    with pytest.raises(
            gui_exports.PersistenceError,
            match="oligos disagree with the scientific manifest|interaction order"):
        gui_exports.decode_analyzed_run(_resign_archive(document))


def test_analyzed_run_rejects_interaction_conflicts_in_contract_order():
    oligos, conditions, report = _completed_run()
    document = json.loads(
        _encode_legacy_run(oligos, report, conditions))
    interaction = document["payload"]["report"]["hairpins"]["items"][0]
    interaction["label"] = "tampered"
    interaction["sequence_a"] = "CCCCCCCCC"

    with pytest.raises(
            gui_exports.PersistenceError,
            match="interaction order or label is inconsistent"):
        gui_exports.decode_analyzed_run(_resign_archive(document))


def test_failed_atomic_analyzed_run_write_preserves_existing_file(tmp_path,
                                                                  monkeypatch):
    oligos, conditions, report = _completed_run()
    destination = tmp_path / "run.json"
    original = b"preserved archive bytes"
    destination.write_bytes(original)
    dialogs = []
    monkeypatch.setattr(
        gui_exports.filedialog, "asksaveasfilename",
        lambda **_kwargs: str(destination))
    monkeypatch.setattr(
        gui_exports, "_flush_and_sync",
        lambda _handle: (_ for _ in ()).throw(OSError(errno.EIO, "flush")))
    monkeypatch.setattr(
        gui_exports.messagebox, "showerror",
        lambda *args: dialogs.append(("error", args)))
    monkeypatch.setattr(
        gui_exports.messagebox, "showinfo",
        lambda *args: dialogs.append(("info", args)))

    assert gui_exports.save_analyzed_run(
        oligos, report, conditions, _translator) is None
    assert destination.read_bytes() == original
    assert list(tmp_path.iterdir()) == [destination]
    assert [kind for kind, _args in dialogs] == ["error"]


@pytest.mark.parametrize("language", ["en", "ru"])
def test_archive_import_errors_are_actionable_and_never_leak_raw_english(
        monkeypatch, language):
    """Presentation maps persistence failures to localized stable categories."""

    raw = "archive member index.json checksum does not match; file may be damaged"
    shown = []

    def translator(selected, key, **values):
        if key == "archive.import_failed_body":
            prefix = ("The archive could not be verified."
                      if selected == "en" else
                      "РђСЂС…РёРІ РЅРµ СѓРґР°Р»РѕСЃСЊ РїСЂРѕРІРµСЂРёС‚СЊ.")
            return f"{prefix}\n{values}"
        return key

    monkeypatch.setattr(
        gui_exports.filedialog, "askopenfilename", lambda **_kwargs: "run.mbusl-run")
    monkeypatch.setattr(
        gui_exports, "load_analyzed_run_archive",
        lambda _path: (_ for _ in ()).throw(gui_exports.PersistenceError(raw)))
    monkeypatch.setattr(
        gui_exports.messagebox, "showerror",
        lambda _title, body: shown.append(body))

    assert gui_exports.choose_analyzed_run(translator, language) is None
    assert len(shown) == 1
    if language == "ru":
        assert raw not in shown[0]
        assert "checksum" not in shown[0]
    else:
        assert "could not be verified" in shown[0]
