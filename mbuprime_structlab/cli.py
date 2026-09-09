"""Stable, pipeline-safe command-line interface."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import signal
import sys
import tempfile
import threading
from typing import BinaryIO, Sequence

from locale_numbers import parse_locale_number

from . import __version__


EXIT_SUCCESS = 0
EXIT_COMMAND = 2
EXIT_INPUT = 3
EXIT_ANALYSIS = 4
EXIT_OUTPUT = 5
EXIT_CANCELLED = 130

_FLOAT_OPTIONS = frozenset({
    "--mv-conc", "--dv-conc", "--dntp-conc", "--primer-conc",
    "--probe-conc", "--dg-temp-c", "--dg-caution", "--dg-problem",
})
_SIGNED_COMMA_NUMBER = re.compile(r"^[+-]?(?:\d+(?:,\d*)?|,\d+)$")

_MESSAGES = {
    "en": {
        "cancelled": "Analysis cancelled.",
        "condition": "Invalid reaction condition: {detail}",
        "input": "Input error: {detail}",
        "preflight": "Scientific-engine preflight failed: {detail}",
        "runtime": "Analysis failed: {detail}",
        "output": "Output write failed: {detail}",
        "complete": "Analysis complete",
        "preparing": "Preparing analysis",
        "tm": "Tm: {name}",
        "hairpin": "Hairpin: {name}",
        "self_dimer": "Self-dimer: {name}",
        "hetero_dimer": "Hetero-dimer: {name}",
    },
    "ru": {
        "cancelled": "Анализ отменен.",
        "condition": "Некорректные условия реакции: {detail}",
        "input": "Ошибка входных данных: {detail}",
        "preflight": "Проверка научных движков не пройдена: {detail}",
        "runtime": "Ошибка анализа: {detail}",
        "output": "Не удалось записать результат: {detail}",
        "complete": "Анализ завершен",
        "preparing": "Подготовка анализа",
        "tm": "Tm: {name}",
        "hairpin": "Шпилька: {name}",
        "self_dimer": "Самодимер: {name}",
        "hetero_dimer": "Гетеродимер: {name}",
    },
}


class CliInputError(ValueError):
    """The input stream does not satisfy the public TSV contract."""


class CliOutputError(OSError):
    """The completed report could not be published."""


class CliCancelled(RuntimeError):
    """Cancellation was observed before the atomic publication boundary."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mbuprime-structlab",
        description="Headless PCR/qPCR oligonucleotide structure analysis.",
    )
    parser.add_argument(
        "--version", action="version", version=__version__,
        help="print the installed MBUprime StructLab version and exit")
    commands = parser.add_subparsers(dest="command", required=True)
    analyze = commands.add_parser(
        "analyze",
        help="analyze a strict UTF-8 TSV oligonucleotide panel",
        description=(
            "Analyze INPUT with mandatory Primer3, ViennaRNA, RNAstructure, "
            "and seqfold engines. INPUT must have the exact TSV header "
            "name<TAB>role<TAB>sequence."
        ),
    )
    analyze.add_argument(
        "input", metavar="INPUT",
        help="strict UTF-8 TSV input path, or - for stdin")
    analyze.add_argument(
        "-o", "--output", default="-", metavar="PATH",
        help="atomically written report path, or - for stdout (default: -)")
    analyze.add_argument(
        "--format", choices=("auto", "text", "csv", "tsv"), default="text",
        help=("report format; auto uses the output filename extension and "
              "falls back to text (default: text)"))
    analyze.add_argument(
        "--progress", choices=("auto", "text", "json", "none"), default="auto",
        help=("stderr progress protocol; auto selects text for an interactive "
              "terminal and none otherwise (default: auto)"))
    analyze.add_argument(
        "--language", choices=("en", "ru"), default="en",
        help=("language for human-readable progress and errors; report field "
              "names stay English (default: en)"))

    analyze.add_argument("--mv-conc", type=parse_locale_number,
                         default=50.0, metavar="MM",
                         help=("monovalent-cation concentration in mM; finite "
                               "and > 0 (default: 50)"))
    analyze.add_argument("--dv-conc", type=parse_locale_number,
                         default=3.0, metavar="MM",
                         help=("divalent-cation concentration in mM; finite, "
                               "> 0, and greater than dNTP concentration "
                               "(default: 3)"))
    analyze.add_argument("--dntp-conc", type=parse_locale_number,
                         default=0.8, metavar="MM",
                         help=("total dNTP concentration in mM; finite, >= 0, "
                               "and below divalent concentration (default: 0.8)"))
    analyze.add_argument("--primer-conc", type=parse_locale_number,
                         default=300.0, metavar="NM",
                         help=("primer strand concentration in nM; finite and "
                               "> 0 (default: 300)"))
    analyze.add_argument("--probe-conc", type=parse_locale_number,
                         default=150.0, metavar="NM",
                         help=("probe strand concentration in nM; finite and "
                               "> 0 (default: 150)"))
    analyze.add_argument("--dg-temp-c", type=parse_locale_number,
                         default=25.0, metavar="C",
                         help=("temperature in degrees C for structure dG "
                               "evaluation; finite and > -273.15 (default: 25)"))
    analyze.add_argument("--dg-caution", type=parse_locale_number,
                         default=-7.0, metavar="KCAL_MOL",
                         help=("finite minimum-dG caution threshold in kcal/mol; "
                               "values at or below it are flagged (default: -7)"))
    analyze.add_argument("--dg-problem", type=parse_locale_number,
                         default=-10.0, metavar="KCAL_MOL",
                         help=("finite minimum-dG problem threshold in kcal/mol; "
                               "must be more negative than --dg-caution and "
                               "distinct at 0.1 kcal/mol decision resolution "
                               "(default: -10)"))
    analyze.add_argument(
        "--near-duplicate-bond-difference", type=int, default=4, metavar="BONDS",
        help=("nonnegative exclusive bond-difference threshold for suppressing "
              "dominated near-duplicates; 0 disables suppression (default: 4)"))
    analyze.add_argument(
        "--dimer-max-consecutive-gaps", type=int, default=0, metavar="BASES",
        help=("maximum consecutive internal gap bases retained in a dimer; "
              "integer >= 0 (default: 0)"))
    analyze.add_argument(
        "--dimer-max-total-gaps", type=int, default=0, metavar="BASES",
        help=("maximum total internal gap bases retained in a dimer; integer "
              ">= 0 (default: 0)"))
    analyze.add_argument(
        "--additional-analysis", action=argparse.BooleanOptionalAction,
        default=True,
        help=("enable budgeted analysis of additional concrete degenerate "
              "contexts; --no-additional-analysis disables it "
              "(default: enabled)"))
    analyze.add_argument(
        "--additional-analysis-size", type=int, default=500, metavar="COUNT",
        help=("nonnegative budget of additional concrete degenerate contexts; "
              "0 keeps representative analysis only (default: 500)"))
    analyze.add_argument(
        "--max-workers", type=int, default=None, metavar="COUNT",
        help=("requested native-search concurrency, integer >= 1 and capped at "
              "8; default uses MBUPRIME_RNASTRUCTURE_WORKERS or 8"))
    return parser


def _stderr_bytes(payload: bytes) -> None:
    stream = getattr(sys.stderr, "buffer", None)
    if stream is not None:
        stream.write(payload)
        stream.flush()
    else:
        sys.stderr.write(payload.decode("utf-8"))
        sys.stderr.flush()


def _message(language: str, key: str, **values: object) -> str:
    return _MESSAGES[language][key].format(**values)


def _error(language: str, key: str, detail: str = "") -> None:
    values = {"detail": detail}
    _stderr_bytes((_message(language, key, **values) + "\n").encode("utf-8"))


def _read_bytes(source: str) -> bytes:
    try:
        if source == "-":
            stream: BinaryIO | None = getattr(sys.stdin, "buffer", None)
            if stream is None:
                return sys.stdin.read().encode("utf-8")
            return stream.read()
        return Path(source).read_bytes()
    except OSError as exc:
        raise CliInputError(f"cannot read {source!r}: {exc}") from exc


def _parse_input(source: str, conditions: object) -> list[object]:
    """Parse the public TSV contract without permissive recovery.

    Input must be strict UTF-8 with the exact header, three fields per row,
    unique unpadded names, declared roles, and sequences accepted by the
    scientific engine; violations fail before analysis starts.
    """

    import thermo_engine as te

    raw = _read_bytes(source)
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise CliInputError(
            f"{source!r} is not strict UTF-8 (byte {exc.start})") from exc
    if "\x00" in text:
        raise CliInputError("NUL bytes are not allowed")
    lines = text.splitlines()
    if not lines:
        raise CliInputError("input is empty")
    expected_header = "name\trole\tsequence"
    if lines[0] != expected_header:
        raise CliInputError(
            "first row must be exactly 'name<TAB>role<TAB>sequence'")
    names: set[str] = set()
    oligos: list[object] = []
    for line_number, line in enumerate(lines[1:], 2):
        if not line:
            continue
        fields = line.split("\t")
        if len(fields) != 3:
            raise CliInputError(
                f"line {line_number} must contain exactly three TSV fields")
        name, role, raw_sequence = fields
        if not name or name != name.strip():
            raise CliInputError(
                f"line {line_number} has an empty or space-padded name")
        if name in names:
            raise CliInputError(f"line {line_number} duplicates name {name!r}")
        if role not in {"primer", "probe"}:
            raise CliInputError(
                f"line {line_number} role must be 'primer' or 'probe'")
        try:
            sequence = te.clean_sequence(raw_sequence)
            variants = te.expand_iupac(sequence)
        except te.SequenceError as exc:
            raise CliInputError(f"line {line_number} ({name}): {exc}") from exc
        concentration = (
            conditions.primer_conc if role == "primer" else conditions.probe_conc)
        oligos.append(te.Oligo(
            name=name, seq=sequence, role=role,
            conc_nM=concentration, variants=variants))
        names.add(name)
    if not oligos:
        raise CliInputError("input contains no oligonucleotide rows")
    return oligos


def _localized_progress_message(language: str, message: str) -> str:
    if language == "en":
        return message
    if message == "Preparing analysis":
        return _message(language, "preparing")
    prefixes = (
        ("Tm: ", "tm"),
        ("Hairpin: ", "hairpin"),
        ("Self-dimer: ", "self_dimer"),
        ("Hetero-dimer: ", "hetero_dimer"),
    )
    for prefix, key in prefixes:
        if message.startswith(prefix):
            return _message(language, key, name=message[len(prefix):])
    return message


class ProgressWriter:
    def __init__(self, mode: str, language: str) -> None:
        if mode == "auto":
            mode = "text" if sys.stderr.isatty() else "none"
        self.mode = mode
        self.language = language
        self.completed = 0
        self.total = 0

    def __call__(self, progress: object) -> None:
        self.completed = int(progress.completed)
        self.total = int(progress.total)
        if self.mode == "none":
            return
        message = _localized_progress_message(
            self.language, str(progress.message))
        if self.mode == "json":
            payload = json.dumps({
                "event": "progress",
                "completed": progress.completed,
                "total": progress.total,
                "message": message,
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        else:
            payload = f"[{progress.completed}/{progress.total}] {message}"
        _stderr_bytes((payload + "\n").encode("utf-8"))

    def complete(self) -> None:
        if self.mode == "none":
            return
        message = _message(self.language, "complete")
        if self.mode == "json":
            payload = json.dumps(
                {"event": "complete", "completed": self.completed,
                 "total": self.total, "message": message},
                ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        else:
            payload = message
        _stderr_bytes((payload + "\n").encode("utf-8"))


def _conditions(args: argparse.Namespace) -> object:
    import thermo_engine as te

    return te.ReactionConditions(
        mv_conc=args.mv_conc,
        dv_conc=args.dv_conc,
        dntp_conc=args.dntp_conc,
        primer_conc=args.primer_conc,
        probe_conc=args.probe_conc,
        dg_temp_c=args.dg_temp_c,
        dg_caution=args.dg_caution,
        dg_problem=args.dg_problem,
        near_duplicate_bond_difference=args.near_duplicate_bond_difference,
        dimer_max_consecutive_gaps=args.dimer_max_consecutive_gaps,
        dimer_max_total_gaps=args.dimer_max_total_gaps,
    )


def _output_format(requested: str, destination: str) -> str:
    if requested != "auto":
        return requested
    suffix = Path(destination).suffix.lower() if destination != "-" else ""
    return {".csv": "csv", ".tsv": "tsv", ".tab": "tsv"}.get(suffix, "text")


def _render(format_name: str, oligos: list[object], report: object,
            conditions: object) -> str:
    import thermo_engine as te

    formatter = {
        "text": te.format_text_report,
        "csv": te.format_csv_report,
        "tsv": te.format_tsv_report,
    }[format_name]
    rendered = formatter(oligos, report, conditions)
    return rendered.replace("\r\n", "\n").replace("\r", "\n")


def _write_output(destination: str, rendered: str,
                  cancel_event: threading.Event | None = None) -> None:
    payload = rendered.encode("utf-8")
    try:
        if destination == "-":
            stream = getattr(sys.stdout, "buffer", None)
            if stream is None:
                sys.stdout.write(rendered)
                sys.stdout.flush()
            else:
                stream.write(payload)
                stream.flush()
            return
        path = Path(destination)
        parent = path.parent.resolve()
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            if cancel_event is not None and cancel_event.is_set():
                raise CliCancelled("Analysis cancelled.")
            os.replace(temporary_name, path)
        except BaseException:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
            raise
    except OSError as exc:
        raise CliOutputError(f"cannot write {destination!r}: {exc}") from exc


def _validate_command_values(args: argparse.Namespace) -> str | None:
    if args.additional_analysis_size < 0:
        return "--additional-analysis-size must be >= 0"
    if args.max_workers is not None and args.max_workers < 1:
        return "--max-workers must be >= 1"
    return None


def _normalized_argv(argv: Sequence[str] | None) -> list[str]:
    """Let argparse recognize signed comma decimals as option values."""

    values = list(sys.argv[1:] if argv is None else argv)
    for index in range(1, len(values)):
        if (values[index - 1] in _FLOAT_OPTIONS
                and _SIGNED_COMMA_NUMBER.fullmatch(values[index])):
            values[index] = values[index].replace(",", ".")
    return values


def _failed_mandatory_engine_outcomes(report: object) -> list[str]:
    """Return failed mandatory-engine context groups, excluding budget limits."""

    coverage = getattr(report, "ensemble_coverage", None)
    failures: list[str] = []
    if coverage is None:
        return ["ensemble coverage is unavailable"]
    for engine, kinds in coverage.engines:
        for kind, entry in kinds:
            if entry.supported and entry.failed:
                failures.append(f"{engine}/{kind}: {entry.failed} failed")
    return failures


def _run_analyze(args: argparse.Namespace) -> int:
    language = args.language
    invalid = _validate_command_values(args)
    if invalid:
        _error(language, "condition", invalid)
        return EXIT_COMMAND
    try:
        import external_engines as ee
        import thermo_engine as te
        import vienna_backend as vb
        from rnastructure_native import NativeIntegrityError
    except (ImportError, OSError) as exc:
        _error(language, "preflight", f"{type(exc).__name__}: {exc}")
        return EXIT_INPUT

    try:
        conditions = _conditions(args)
    except te.SequenceError as exc:
        _error(language, "condition", str(exc))
        return EXIT_COMMAND
    try:
        oligos = _parse_input(args.input, conditions)
    except CliInputError as exc:
        _error(language, "input", str(exc))
        return EXIT_INPUT

    cancel_event = threading.Event()
    previous_handler = signal.getsignal(signal.SIGINT)

    def request_cancel(_signum: int, _frame: object) -> None:
        cancel_event.set()

    signal.signal(signal.SIGINT, request_cancel)
    progress = ProgressWriter(args.progress, language)
    mode = (
        te.ENSEMBLE_MODE_BUDGETED_COMPLETE
        if args.additional_analysis else te.ENSEMBLE_MODE_REPRESENTATIVE)
    try:
        report = te.analyze(
            oligos,
            conditions,
            progress_callback=progress,
            cancel_event=cancel_event,
            max_external_workers=args.max_workers,
            ensemble_mode=mode,
            ensemble_additional_budget=args.additional_analysis_size,
        )
        if cancel_event.is_set():
            raise te.AnalysisCancelled("Analysis cancelled.")
        if not report.complete or report.phase != "complete":
            raise te.AnalysisIncompleteError(
                f"analysis ended in non-final phase {report.phase!r}")
        failed_outcomes = _failed_mandatory_engine_outcomes(report)
        if failed_outcomes:
            raise ee.RequiredScientificEngineError(
                "mandatory engine calculations did not complete: "
                + "; ".join(failed_outcomes))
        rendered = _render(
            _output_format(args.format, args.output), oligos, report, conditions)
        if cancel_event.is_set():
            raise te.AnalysisCancelled("Analysis cancelled.")
        _write_output(args.output, rendered, cancel_event)
    except (te.AnalysisCancelled, ee.ExternalEngineCancelled, CliCancelled,
            KeyboardInterrupt):
        _error(language, "cancelled")
        return EXIT_CANCELLED
    except (ee.RequiredScientificEngineError, NativeIntegrityError,
            vb.ViennaBackendError) as exc:
        _error(language, "preflight", str(exc))
        return EXIT_INPUT
    except CliOutputError as exc:
        _error(language, "output", str(exc))
        return EXIT_OUTPUT
    except Exception as exc:
        _error(language, "runtime", f"{type(exc).__name__}: {exc}")
        return EXIT_ANALYSIS
    finally:
        signal.signal(signal.SIGINT, previous_handler)
    progress.complete()
    return EXIT_SUCCESS


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(_normalized_argv(argv))
    if args.command == "analyze":
        return _run_analyze(args)
    return EXIT_COMMAND
