"""Canonical GUI exports and localized save-dialog coordination."""

from __future__ import annotations

import csv
import errno
import io
import json
import math
import platform
from collections.abc import Callable, Sequence
from dataclasses import fields
import os
from os import PathLike
from pathlib import Path
import tempfile
from tkinter import filedialog, messagebox
from typing import BinaryIO

import analyzed_run_archive as analyzed_archive
import thermo_engine as te


Translator = Callable[..., str]

CONDITION_PRESET_FORMAT = "mbuprime-structlab-condition-presets"
CONDITION_PRESET_SCHEMA_VERSION = 1
ANALYZED_RUN_FORMAT = "mbuprime-structlab-analyzed-run"
ANALYZED_RUN_SCHEMA_VERSION = analyzed_archive.ANALYZED_RUN_SCHEMA_VERSION
MAX_CONDITION_PRESET_FILE_BYTES = 1024 * 1024
MAX_CONDITION_PRESETS = 100
MAX_PRESET_NAME_LENGTH = 80
MAX_ANALYZED_RUN_BYTES = analyzed_archive.MAX_LEGACY_ANALYZED_RUN_BYTES
MAX_ARCHIVE_COMPRESSED_BYTES = analyzed_archive.MAX_ARCHIVE_COMPRESSED_BYTES
MAX_ARCHIVE_EXPANDED_BYTES = analyzed_archive.MAX_ARCHIVE_EXPANDED_BYTES
MAX_CONTROL_MEMBER_BYTES = analyzed_archive.MAX_CONTROL_MEMBER_BYTES
MAX_JSONL_RECORD_BYTES = analyzed_archive.MAX_JSONL_RECORD_BYTES
MAX_STREAMED_RECORDS = analyzed_archive.MAX_STREAMED_RECORDS
MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 1_000_000
HISTORICAL_ANALYZED_RUN_POLICY = (
    analyzed_archive.HISTORICAL_ANALYZED_RUN_POLICY)
SUPPORTED_ANALYZED_RUN_POLICIES = (
    analyzed_archive.SUPPORTED_ANALYZED_RUN_POLICIES)
_RESERVED_PRESET_NAMES = frozenset(("qPCR / TaqMan", "Custom"))


PersistenceError = analyzed_archive.PersistenceError
LoadedAnalyzedRun = analyzed_archive.LoadedAnalyzedRun

_TYPE_FIELDS = {
    "ReactionConditions": (
        "mv_conc", "dv_conc", "dntp_conc", "primer_conc", "probe_conc",
        "dg_temp_c", "dg_caution", "dg_problem",
        "near_duplicate_bond_difference", "dimer_max_consecutive_gaps",
        "dimer_max_total_gaps",
    ),
}
_FLAGGED_COLUMNS = (
    "record_type", "manifest_json", "severity", "kind", "site", "label",
    "rank", "discovered_by", "structure_origin",
    "dg_p3_kcal_mol", "dg_vienna_kcal_mol",
    "dg_rnastructure_kcal_mol", "tm_rnastructure_c",
    "dg_seqfold_kcal_mol", "tm_seqfold_c",
    "engine_observations_json", "derivation_lineage_json",
    "tm_c", "assessment",
)
_EXPORT_DIALOG_SPECS = {
    "text": (".txt", "export.text_report", "*.txt"),
    "csv": (".csv", "export.csv_report", "*.csv"),
    "tsv": (".tsv", "export.tsv_report", "*.tsv"),
    "flagged_csv": (".csv", "export.csv_flagged", "*.csv"),
    "matrix_csv": (".csv", "export.csv_matrix", "*.csv"),
}
_SAVE_ERROR_COPY = {
    "en": {
        "access": (
            "Windows denied write access. The folder may be read-only or protected "
            "by Controlled Folder Access. Choose a writable folder or allow this "
            "application in Windows Security."),
        "disk_full": (
            "The destination disk has no free space. Free space or choose another drive."),
        "invalid_destination": (
            "The destination is missing or invalid. Choose an existing folder and a valid file name."),
        "busy": (
            "The file is busy or locked by another application. Close the other application "
            "or choose a different file name."),
        "network_cloud": (
            "The network, cloud, or OneDrive destination is unavailable. Restore the "
            "connection/sync or save to a local folder."),
        "unexpected": (
            "An unexpected save error occurred. Choose another writable local folder and try again."),
        "path": "Selected path",
        "technical": "Technical details",
    },
    "ru": {
        "access": (
            "Windows запретила запись. Папка может быть доступна только для чтения или "
            "защищена функцией «Контролируемый доступ к папкам». Выберите доступную для "
            "записи папку или разрешите приложение в Безопасности Windows."),
        "disk_full": (
            "На диске назначения нет свободного места. Освободите место или выберите другой диск."),
        "invalid_destination": (
            "Папка назначения отсутствует или имя файла недопустимо. Выберите существующую "
            "папку и корректное имя файла."),
        "busy": (
            "Файл занят или заблокирован другим приложением. Закройте это приложение "
            "или выберите другое имя файла."),
        "network_cloud": (
            "Сетевое, облачное или OneDrive-хранилище недоступно. Восстановите подключение "
            "или синхронизацию либо сохраните файл в локальную папку."),
        "unexpected": (
            "Произошла непредвиденная ошибка сохранения. Выберите другую доступную локальную "
            "папку и повторите попытку."),
        "path": "Выбранный путь",
        "technical": "Технические сведения",
    },
}
_PERSISTENCE_ERROR_COPY = {
    "en": {
        "invalid_archive": (
            "The analyzed-run archive is invalid.",
            "Choose an archive exported by MBUprime StructLab and try again."),
        "integrity_failure": (
            "The archive could not be verified.",
            "Export the run again or obtain an undamaged copy."),
        "limit_exceeded": (
            "The archive exceeds a safety limit.",
            "Use a smaller completed analysis or split the oligonucleotide set."),
        "unsupported_format": (
            "This archive version or scientific policy is not supported.",
            "Open it with the version that created it or export it again with a supported version."),
        "io_failure": (
            "The archive file could not be accessed.",
            "Check the folder permissions and available disk space, then try again."),
        "memory_exhausted": (
            "There is not enough memory to process this archive.",
            "Close other applications or use a computer with more available memory."),
        "code": "Error code",
    },
    "ru": {
        "invalid_archive": (
            "Архив выполненного анализа имеет недопустимый формат.",
            "Выберите архив, экспортированный MBUprime StructLab, и повторите попытку."),
        "integrity_failure": (
            "Архив не удалось проверить.",
            "Экспортируйте анализ повторно или получите неповреждённую копию файла."),
        "limit_exceeded": (
            "Архив превышает допустимое ограничение безопасности.",
            "Используйте анализ меньшего размера или разделите набор олигонуклеотидов."),
        "unsupported_format": (
            "Эта версия архива или научная политика не поддерживается.",
            "Откройте архив создавшей его версией или экспортируйте его заново."),
        "io_failure": (
            "Не удалось получить доступ к файлу архива.",
            "Проверьте права доступа к папке и свободное место на диске."),
        "memory_exhausted": (
            "Недостаточно памяти для обработки архива.",
            "Закройте другие приложения или используйте компьютер с большим объёмом доступной памяти."),
        "code": "Код ошибки",
    },
}
_ACCESS_ERRNOS = {errno.EACCES, errno.EPERM, errno.EROFS}
_DISK_FULL_ERRNOS = {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}
_INVALID_ERRNOS = {errno.ENOENT, errno.ENOTDIR, errno.EINVAL,
                   getattr(errno, "ENAMETOOLONG", errno.EINVAL)}
_BUSY_ERRNOS = {errno.EBUSY, errno.EAGAIN,
                getattr(errno, "ETXTBSY", errno.EBUSY)}
_NETWORK_ERRNOS = {
    getattr(errno, name) for name in (
        "ENETDOWN", "ENETUNREACH", "ENOTCONN", "ESTALE", "EHOSTUNREACH")
    if hasattr(errno, name)
}
_ACCESS_WINERRORS = {5, 19, 1314}
_DISK_FULL_WINERRORS = {39, 112}
_INVALID_WINERRORS = {2, 3, 15, 87, 123, 161, 206, 267}
_BUSY_WINERRORS = {32, 33}
_NETWORK_WINERRORS = {53, 54, 64, 67, 121, 362, 369, 1222, 1231, 2250}


def _flush_and_sync(handle: BinaryIO) -> None:
    handle.flush()
    os.fsync(handle.fileno())


def transactional_write(path: str | PathLike[str],
                        writer: Callable[[BinaryIO], object]) -> None:
    """Write through an fsynced sibling and atomically replace the target."""

    destination = Path(path)
    if not destination.name or destination.exists() and destination.is_dir():
        raise ValueError(f"invalid file destination: {destination}")
    parent = destination.parent
    if not parent.is_dir():
        raise FileNotFoundError(errno.ENOENT, "destination folder is missing",
                                str(parent))
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=str(parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w+b") as handle:
            writer(handle)
            _flush_and_sync(handle)
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _canonical_bytes(value: object) -> bytes:
    try:
        text = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise PersistenceError(f"data cannot be represented safely: {exc}") from exc
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
        raise PersistenceError(f"{context} fields are invalid ({'; '.join(details)})")
    return value


def _json_container_deltas(text: str):
    in_string = False
    escaped = False
    for character in text:
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
    """Count JSON containers and scalar values without allocating the tree."""

    count = 0
    index = 0
    length = len(text)
    while index < length:
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
    result = {}
    for key, value in pairs:
        if key in result:
            raise PersistenceError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _validate_json_tree(value, maximum_values: int) -> None:
    nodes = 0
    stack = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > maximum_values:
            raise PersistenceError(
                f"JSON value count exceeds the limit of {maximum_values}")
        if depth > MAX_JSON_DEPTH:
            raise PersistenceError(
                f"JSON nesting exceeds the limit of {MAX_JSON_DEPTH}")
        if isinstance(current, dict):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
        elif isinstance(current, float) and not math.isfinite(current):
            raise PersistenceError("non-finite numbers are not allowed")


def _parse_json_bytes(
        payload: bytes, *, maximum: int, description: str,
        maximum_values: int = MAX_JSON_NODES):
    if len(payload) > maximum:
        raise PersistenceError(
            f"{description} exceeds the {maximum // (1024 * 1024) or 1} MiB limit")
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
        raise PersistenceError(f"{description} contains invalid JSON: {exc}") from exc

    _validate_json_tree(value, maximum_values)
    return value


def _read_bounded(path: str | PathLike[str], maximum: int,
                  description: str) -> bytes:
    source = Path(path)
    try:
        size = source.stat().st_size
        if size > maximum:
            raise PersistenceError(
                f"{description} exceeds the supported size limit")
        with source.open("rb") as handle:
            payload = handle.read(maximum + 1)
        if len(payload) > maximum:
            raise PersistenceError(
                f"{description} exceeds the supported size limit")
        return payload
    except PersistenceError:
        raise
    except OSError as exc:
        raise PersistenceError(f"cannot read {description}: {exc}") from exc


def _encode_conditions(conditions: te.ReactionConditions) -> dict:
    expected = _TYPE_FIELDS["ReactionConditions"]
    actual = tuple(item.name for item in fields(te.ReactionConditions))
    if actual != expected:
        raise PersistenceError(
            "ReactionConditions fields changed without a preset schema migration")
    encoded = {"$type": "ReactionConditions"}
    for name in expected:
        value = getattr(conditions, name)
        if isinstance(value, float) and not math.isfinite(value):
            raise PersistenceError("non-finite numbers cannot be saved in presets")
        encoded[name] = value
    return encoded


def _decode_conditions(value: object) -> te.ReactionConditions:
    expected = _TYPE_FIELDS["ReactionConditions"]
    obj = _exact_keys(
        value, {"$type", *expected}, "ReactionConditions")
    if obj["$type"] != "ReactionConditions":
        raise PersistenceError("preset conditions have the wrong type")
    try:
        return te.ReactionConditions(**{
            name: obj[name] for name in expected})
    except (TypeError, ValueError, te.SequenceError) as exc:
        raise PersistenceError(f"invalid ReactionConditions: {exc}") from exc


def condition_preset_path(*, environment: dict[str, str] | None = None,
                          home: str | Path | None = None,
                          system_name: str | None = None) -> Path:
    """Return the documented per-user preset path for the current platform."""

    env = os.environ if environment is None else environment
    user_home = Path.home() if home is None else Path(home)
    system = platform.system() if system_name is None else system_name
    if system == "Windows":
        configured = str(env.get("LOCALAPPDATA", "")).strip()
        root = (Path(configured) if configured
                else user_home / "AppData" / "Local")
        return root / "MBUprime StructLab" / "condition-presets.json"
    if system == "Darwin":
        return (user_home / "Library" / "Application Support" /
                "MBUprime StructLab" / "condition-presets.json")
    configured = str(env.get("XDG_CONFIG_HOME", "")).strip()
    root = Path(configured) if configured else user_home / ".config"
    return root / "mbuprime-structlab" / "condition-presets.json"


def normalize_preset_name(name: object) -> str:
    """Collapse whitespace and validate a nonreserved preset name.

    Raise PersistenceError for invalid type, blank, overlong, or reserved names."""
    if not isinstance(name, str):
        raise PersistenceError("preset name must be text")
    normalized = " ".join(name.split())
    if not normalized:
        raise PersistenceError("preset name cannot be blank")
    if len(normalized) > MAX_PRESET_NAME_LENGTH:
        raise PersistenceError(
            f"preset name cannot exceed {MAX_PRESET_NAME_LENGTH} characters")
    if normalized.casefold() in {
            item.casefold() for item in _RESERVED_PRESET_NAMES}:
        raise PersistenceError(f"'{normalized}' is a reserved preset name")
    return normalized


def _preset_document(presets: dict[str, te.ReactionConditions]) -> dict:
    if len(presets) > MAX_CONDITION_PRESETS:
        raise PersistenceError(
            f"no more than {MAX_CONDITION_PRESETS} custom presets are allowed")
    normalized = {}
    folded = set()
    for raw_name, conditions in presets.items():
        name = normalize_preset_name(raw_name)
        if name.casefold() in folded:
            raise PersistenceError(f"duplicate preset name: {name}")
        if not isinstance(conditions, te.ReactionConditions):
            raise PersistenceError(f"preset '{name}' has invalid conditions")
        folded.add(name.casefold())
        normalized[name] = conditions
    return {
        "format": CONDITION_PRESET_FORMAT,
        "schema_version": CONDITION_PRESET_SCHEMA_VERSION,
        "presets": [
            {"name": name, "conditions": _encode_conditions(normalized[name])}
            for name in sorted(normalized, key=str.casefold)
        ],
    }


def load_condition_presets(
        path: str | PathLike[str] | None = None) -> dict[str, te.ReactionConditions]:
    """Load validated presets from the supplied or per-user default path.

    A missing file returns an empty mapping; malformed, oversized, or
    unsupported documents raise PersistenceError instead of being discarded."""
    source = condition_preset_path() if path is None else Path(path)
    if not source.exists():
        return {}
    document = _parse_json_bytes(
        _read_bounded(source, MAX_CONDITION_PRESET_FILE_BYTES, "preset file"),
        maximum=MAX_CONDITION_PRESET_FILE_BYTES, description="preset file")
    obj = _exact_keys(document, {"format", "schema_version", "presets"},
                      "preset document")
    if obj["format"] != CONDITION_PRESET_FORMAT:
        raise PersistenceError("this is not an MBUprime StructLab preset file")
    if obj["schema_version"] != CONDITION_PRESET_SCHEMA_VERSION:
        raise PersistenceError("preset file schema is not supported")
    if not isinstance(obj["presets"], list):
        raise PersistenceError("presets must be an array")
    if len(obj["presets"]) > MAX_CONDITION_PRESETS:
        raise PersistenceError(
            f"preset file contains more than {MAX_CONDITION_PRESETS} presets")
    result = {}
    folded = set()
    for index, raw in enumerate(obj["presets"]):
        item = _exact_keys(raw, {"name", "conditions"}, f"preset {index + 1}")
        name = normalize_preset_name(item["name"])
        if name.casefold() in folded:
            raise PersistenceError(f"duplicate preset name: {name}")
        conditions = _decode_conditions(item["conditions"])
        folded.add(name.casefold())
        result[name] = conditions
    return result


def save_condition_presets(
        presets: dict[str, te.ReactionConditions],
        path: str | PathLike[str] | None = None) -> Path:
    """Validate and transactionally replace the supplied or default preset file.

    Create missing parent directories and return the destination. Validation
    and handled filesystem failures raise PersistenceError."""
    destination = condition_preset_path() if path is None else Path(path)
    payload = _canonical_bytes(_preset_document(presets))
    if len(payload) > MAX_CONDITION_PRESET_FILE_BYTES:
        raise PersistenceError("preset data exceeds the 1 MiB limit")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        transactional_write(destination, lambda handle: handle.write(payload))
    except (OSError, ValueError) as exc:
        raise PersistenceError(f"cannot save presets: {exc}") from exc
    return destination


def upsert_condition_preset(
        name: str, conditions: te.ReactionConditions, *,
        path: str | PathLike[str] | None = None) -> dict[str, te.ReactionConditions]:
    normalized = normalize_preset_name(name)
    presets = load_condition_presets(path)
    existing = next((key for key in presets if key.casefold() == normalized.casefold()), None)
    if existing is not None and existing != normalized:
        del presets[existing]
    presets[normalized] = conditions
    save_condition_presets(presets, path)
    return presets


def delete_condition_preset(
        name: str, *, path: str | PathLike[str] | None = None
) -> dict[str, te.ReactionConditions]:
    normalized = normalize_preset_name(name)
    presets = load_condition_presets(path)
    existing = next((key for key in presets if key.casefold() == normalized.casefold()), None)
    if existing is None:
        raise PersistenceError(f"preset '{normalized}' does not exist")
    del presets[existing]
    save_condition_presets(presets, path)
    return presets


# Schema 2 is owned by the dependency-light archive module.  Keep these names
# here as the stable GUI/public import surface used by existing callers.
encode_analyzed_run = analyzed_archive.encode_analyzed_run
decode_analyzed_run = analyzed_archive.decode_analyzed_run
write_analyzed_run_archive = analyzed_archive.write_analyzed_run_archive
load_analyzed_run_archive = analyzed_archive.load_analyzed_run_archive
# Compatibility for callers that re-sign legacy schema-1 fixtures.
_manifest_analysis_id = analyzed_archive._manifest_analysis_id


def localized_persistence_error(
        exc: PersistenceError, language: str = "en") -> str:
    """Return actionable localized copy without exposing raw diagnostics."""

    selected = language if language in _PERSISTENCE_ERROR_COPY else "en"
    copy = _PERSISTENCE_ERROR_COPY[selected]
    summary, recovery = copy.get(exc.code, copy["invalid_archive"])
    return f"{summary}\n\n{recovery}\n\n{copy['code']}: {exc.code}"


def save_analyzed_run(
        oligos: Sequence[te.Oligo], report: te.AnalysisReport,
        conditions: te.ReactionConditions, translator: Translator,
        language: str = "en") -> str | None:
    """Ask for a destination and transactionally save a completed analyzed run.

    Return its path on success; cancellation or a displayed save error returns
    None. Archive validation is delegated to write_analyzed_run_archive."""
    path = filedialog.asksaveasfilename(
        title=translator(language, "archive.save_title"),
        defaultextension=".mbusl-run",
        filetypes=[
            (translator(language, "archive.file_type"), "*.mbusl-run"),
            (translator(language, "export.all_files"), "*.*"),
        ])
    if not path:
        return None
    try:
        transactional_write(
            path,
            lambda handle: write_analyzed_run_archive(
                handle, oligos, report, conditions))
    except PersistenceError as exc:
        messagebox.showerror(
            translator(language, "archive.export_failed"),
            translator(language, "archive.export_failed_body",
                       path=path,
                       error=localized_persistence_error(exc, language)))
        return None
    except Exception as exc:
        messagebox.showerror(
            translator(language, "archive.export_failed"),
            localized_save_error(path, exc, language))
        return None
    messagebox.showinfo(
        translator(language, "archive.exported"),
        translator(language, "archive.exported_body", path=path))
    return str(path)


def choose_analyzed_run_path(translator: Translator, language: str = "en"
                             ) -> str:
    """Choose a path on the Tk thread without reading or validating the file."""
    return filedialog.askopenfilename(
        title=translator(language, "archive.open_title"),
        filetypes=[
            (translator(language, "archive.file_type"), "*.mbusl-run"),
            (translator(language, "archive.file_type_legacy"), "*.json"),
            (translator(language, "export.all_files"), "*.*"),
        ])


def choose_analyzed_run(translator: Translator, language: str = "en"
                        ) -> LoadedAnalyzedRun | None:
    """Synchronous compatibility API; GUI imports use the cancellable worker."""
    path = choose_analyzed_run_path(translator, language)
    if not path:
        return None
    try:
        return load_analyzed_run_archive(path)
    except PersistenceError as exc:
        messagebox.showerror(
            translator(language, "archive.import_failed"),
            translator(language, "archive.import_failed_body",
                       path=path,
                       error=localized_persistence_error(exc, language)))
        return None
    except Exception as exc:
        messagebox.showerror(
            translator(language, "archive.import_failed"),
            translator(language, "archive.import_failed_body",
                       path=path,
                       error=localized_save_error(path, exc, language)))
        return None


def classify_save_error(exc: BaseException) -> str:
    """Map filesystem failures to actionable, stable presentation classes."""

    if isinstance(exc, (TypeError, ValueError)):
        return "invalid_destination"
    error_number = getattr(exc, "errno", None)
    windows_error = getattr(exc, "winerror", None)
    message = str(exc).casefold()
    if (error_number in _ACCESS_ERRNOS or windows_error in _ACCESS_WINERRORS
            or "controlled folder access" in message or "read-only" in message):
        return "access"
    if error_number in _DISK_FULL_ERRNOS or windows_error in _DISK_FULL_WINERRORS:
        return "disk_full"
    if error_number in _INVALID_ERRNOS or windows_error in _INVALID_WINERRORS:
        return "invalid_destination"
    if error_number in _BUSY_ERRNOS or windows_error in _BUSY_WINERRORS:
        return "busy"
    if (error_number in _NETWORK_ERRNOS or windows_error in _NETWORK_WINERRORS
            or any(marker in message for marker in (
                "onedrive", "cloud", "sync provider", "network"))):
        return "network_cloud"
    return "unexpected"


def localized_save_error(path: str | PathLike[str], exc: BaseException,
                         language: str = "en") -> str:
    """Return localized recovery text that always identifies the selected path."""

    catalog = _SAVE_ERROR_COPY.get(language, _SAVE_ERROR_COPY["en"])
    category = classify_save_error(exc)
    return (f"{catalog[category]}\n\n{catalog['path']}:\n{path}\n\n"
            f"{catalog['technical']}:\n{type(exc).__name__}: {exc}")


def _format_optional(value: object) -> str:
    return "" if value is None else f"{float(value):.2f}"


def matrix_marker(finding: te.ReportFinding) -> str:
    """Return the locale-independent compact matrix marker."""

    marker = {"problem": "**", "caution": "!", "ok": ""}.get(
        finding.severity, "")
    site = " 3'" if finding.involves_3prime else ""
    dg = finding.effective_dg
    dg_text = "-" if dg is None else f"{dg:.1f}"
    return f"{dg_text} {marker}{site}".strip()


def format_flagged_csv(oligos: Sequence[te.Oligo],
                       report: te.AnalysisReport,
                       conditions: te.ReactionConditions) -> str:
    """Format flagged rows under ``te.ensure_report_manifest``'s contract."""

    out = io.StringIO(newline="")
    writer = csv.DictWriter(
        out, fieldnames=_FLAGGED_COLUMNS, lineterminator="\n")
    writer.writeheader()
    manifest = te.ensure_report_manifest(oligos, report, conditions)
    writer.writerow({
        "record_type": "analysis_manifest",
        "manifest_json": manifest.canonical_json(),
    })
    findings = te.sorted_report_findings(te.iter_report_findings(report))
    for finding in findings:
        if finding.severity not in {"problem", "caution"}:
            continue
        engine_metrics = te.ee.engine_metrics(finding.engine_observations)
        writer.writerow({
            "record_type": "structure",
            "severity": finding.severity,
            "kind": finding.kind,
            "site": finding.site,
            "label": finding.label,
            "rank": finding.rank,
            "discovered_by": ";".join(finding.discovered_by),
            "structure_origin": te.structure_origin(finding.structure, ";"),
            "dg_p3_kcal_mol": _format_optional(finding.dg_p3),
            "dg_vienna_kcal_mol": _format_optional(finding.dg_vienna),
            **{
                f"{quantity}_{stem}_{unit}": _format_optional(value)
                for engine, stem in {
                    "RNAstructure": "rnastructure",
                    "seqfold": "seqfold",
                }.items()
                for quantity, unit, value in (
                    ("dg", "kcal_mol", engine_metrics[engine][0]),
                    ("tm", "c", engine_metrics[engine][1]),
                )
            },
            "engine_observations_json": te.ee.observations_json(
                finding.engine_observations),
            "derivation_lineage_json": te.sm.canonical_json(list(
                getattr(finding.structure, "derivation_lineage", ()))),
            "tm_c": _format_optional(
                finding.tm_c if finding.found else None),
            "assessment": finding.assessment,
        })
    return out.getvalue()


def format_matrix_csv(oligos: Sequence[te.Oligo],
                      report: te.AnalysisReport,
                      conditions: te.ReactionConditions, *,
                      flagged_only: bool = False) -> str:
    """Format the pairwise matrix under ``te.ensure_report_manifest``'s contract."""

    out = io.StringIO(newline="")
    writer = csv.writer(out, lineterminator="\n")
    manifest = te.ensure_report_manifest(oligos, report, conditions)
    writer.writerow(["analysis_manifest_json", manifest.canonical_json()])
    writer.writerow([])
    names = [oligo.name for oligo in oligos]
    matrix = te.hetero_dimer_matrix(oligos, report)
    writer.writerow([""] + names)
    for row_name in names:
        row = [row_name]
        for column_name in names:
            cell = matrix.get((row_name, column_name))
            finding = cell.finding if cell else None
            if (finding is not None and flagged_only
                    and finding.severity == "ok"):
                finding = None
            row.append(matrix_marker(finding) if finding else "")
        writer.writerow(row)
    return out.getvalue()


def format_export(kind: str, oligos: Sequence[te.Oligo],
                  report: te.AnalysisReport,
                  conditions: te.ReactionConditions, *,
                  matrix_flagged_only: bool = False) -> str:
    """Format a locale-independent export using ``te.ensure_report_manifest``.

    See that central contract for report mutation and rejection conditions.
    """

    if kind == "text":
        return te.format_text_report(oligos, report, conditions)
    if kind == "csv":
        return te.format_csv_report(oligos, report, conditions)
    if kind == "tsv":
        return te.format_tsv_report(oligos, report, conditions)
    if kind == "flagged_csv":
        return format_flagged_csv(oligos, report, conditions)
    if kind == "matrix_csv":
        return format_matrix_csv(
            oligos, report, conditions, flagged_only=matrix_flagged_only)
    raise ValueError(f"Unknown export kind: {kind}")


def save_export(kind: str, oligos: Sequence[te.Oligo],
                report: te.AnalysisReport,
                conditions: te.ReactionConditions, translator: Translator,
                language: str = "en", *,
                matrix_flagged_only: bool = False) -> str | None:
    """Coordinate a localized save dialog around locale-neutral export bytes."""

    try:
        extension, type_key, pattern = _EXPORT_DIALOG_SPECS[kind]
    except KeyError as exc:
        raise ValueError(f"Unknown export kind: {kind}") from exc
    path = filedialog.asksaveasfilename(
        title=translator(language, "export.save_title"),
        defaultextension=extension,
        filetypes=[
            (translator(language, type_key), pattern),
            (translator(language, "export.all_files"), "*.*"),
        ],
    )
    if not path:
        return None
    try:
        text = format_export(
            kind, oligos, report, conditions,
            matrix_flagged_only=matrix_flagged_only)
        payload = text.encode("utf-8")
        transactional_write(path, lambda handle: handle.write(payload))
    except Exception as exc:
        messagebox.showerror(
            translator(language, "dialog.export_failed"),
            localized_save_error(path, exc, language))
        return None
    messagebox.showinfo(
        translator(language, "dialog.report_saved"),
        translator(language, "dialog.report_saved_body", path=path),
    )
    return str(path)
