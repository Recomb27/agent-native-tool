"""Reconstructed offline sequence tools used by the bounded GPT-6.1 composition probe.

This module is intentionally separate from the historical archive. It accepts only
the public fixture copied into each run workspace and emits auditable JSON artifacts.
It is not a re-execution of the missing original BioSkill bundles.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import math
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


CODON_TABLE = {
    "TTT": "F", "TTC": "F", "TTA": "L", "TTG": "L",
    "TCT": "S", "TCC": "S", "TCA": "S", "TCG": "S",
    "TAT": "Y", "TAC": "Y", "TAA": "*", "TAG": "*",
    "TGT": "C", "TGC": "C", "TGA": "*", "TGG": "W",
    "CTT": "L", "CTC": "L", "CTA": "L", "CTG": "L",
    "CCT": "P", "CCC": "P", "CCA": "P", "CCG": "P",
    "CAT": "H", "CAC": "H", "CAA": "Q", "CAG": "Q",
    "CGT": "R", "CGC": "R", "CGA": "R", "CGG": "R",
    "ATT": "I", "ATC": "I", "ATA": "I", "ATG": "M",
    "ACT": "T", "ACC": "T", "ACA": "T", "ACG": "T",
    "AAT": "N", "AAC": "N", "AAA": "K", "AAG": "K",
    "AGT": "S", "AGC": "S", "AGA": "R", "AGG": "R",
    "GTT": "V", "GTC": "V", "GTA": "V", "GTG": "V",
    "GCT": "A", "GCC": "A", "GCA": "A", "GCG": "A",
    "GAT": "D", "GAC": "D", "GAA": "E", "GAG": "E",
    "GGT": "G", "GGC": "G", "GGA": "G", "GGG": "G",
}


SEQUENCE_SLICING_CONTRACT_VERSION = "1.1"
HALF_OPEN_COORDINATE_SYSTEM = "0-based-half-open"
SOURCE_SEQUENCE_FRAME = "source_sequence_absolute"
INPUT_ORIENTATION = "input_orientation"
DEFAULT_TRANSLATION_CONTEXT = {
    "molecule": "DNA",
    "unit": "nt",
    "strand": "forward",
    "frame": 0,
    "genetic_code": "standard",
    "coordinate_system": HALF_OPEN_COORDINATE_SYSTEM,
    "partial_codon_policy": "reject",
}


class ToolInputError(ValueError):
    """A deterministic, user-actionable error in a sequence-slicing input."""

    def __init__(self, code: str, message: str, path: str | None = None):
        super().__init__(message)
        self.code = code
        self.path = path

    def as_dict(self) -> dict[str, Any]:
        error = {"code": self.code, "message": str(self)}
        if self.path is not None:
            error["path"] = self.path
        return error


def _write_json_artifact(output_path: Path, result: dict[str, Any]) -> None:
    """Publish a complete JSON artifact through an atomic same-directory rename."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, output_path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def translate(dna: str) -> str:
    if len(dna) % 3:
        raise ValueError(f"DNA length is not divisible by three: {len(dna)}")
    try:
        return "".join(CODON_TABLE[dna[i : i + 3]] for i in range(0, len(dna), 3))
    except KeyError as exc:
        raise ValueError(f"Unknown codon: {exc.args[0]}") from exc


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_fixture(input_path: str | Path) -> dict[str, Any]:
    payload = read_json(input_path)
    if not isinstance(payload, dict):
        raise ValueError("The tool input must be a JSON object")
    if "sequences" not in payload:
        raise ValueError("The tool input must contain sequences")
    return payload


def gc_fraction(seq: str) -> float:
    return (seq.count("G") + seq.count("C")) / len(seq)


def pairwise(records: dict[str, str]) -> list[dict[str, Any]]:
    keys = list(records)
    out = []
    for i, left in enumerate(keys):
        for right in keys[i + 1 :]:
            a, b = records[left], records[right]
            if len(a) != len(b):
                continue
            positions = [j for j, (x, y) in enumerate(zip(a, b)) if x != y]
            out.append({"left": left, "right": right, "count": len(positions), "positions": positions})
    return out


def rows_from_file(rows_path: str | Path | None) -> list[dict[str, Any]]:
    """Load optional row evidence. Empty files and /dev/null mean no rows."""
    if rows_path is None:
        return []
    path = Path(rows_path)
    raw = path.read_bytes()
    if path == Path("/dev/null") or not raw.strip():
        return []
    rows = json.loads(raw)
    if not isinstance(rows, list):
        raise ToolInputError(
            "invalid_rows",
            "Rows input must be a JSON list.",
            str(path),
        )
    return rows


def _is_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _half_open_bounds(value: Any, path: str, length: int | None = None) -> tuple[int, int]:
    if not isinstance(value, list) or len(value) != 2 or not all(_is_integer(x) for x in value):
        raise ToolInputError(
            "invalid_half_open_bounds",
            "Expected [start, end] integer bounds for a 0-based half-open interval.",
            path,
        )
    lo, hi = value
    if lo < 0 or hi < lo:
        raise ToolInputError(
            "invalid_half_open_bounds",
            f"Expected 0 <= start <= end, got {value!r}.",
            path,
        )
    if length is not None and hi > length:
        raise ToolInputError(
            "aa_window_out_of_bounds",
            f"Interval {value!r} exceeds the source protein length {length}.",
            path,
        )
    return lo, hi


def _check_declared_value(
    mapping: dict[str, Any],
    keys: tuple[str, ...],
    expected: Any,
    path: str,
    code: str,
) -> None:
    for key in keys:
        if key in mapping and mapping[key] != expected:
            raise ToolInputError(
                code,
                f"{path}.{key} must be {expected!r}; got {mapping[key]!r}.",
                f"{path}.{key}",
            )


def _is_zero_frame(value: Any) -> bool:
    """Accept only the unambiguous frame-zero JSON spellings."""

    return (_is_integer(value) and value == 0) or value == "0"


def _check_declared_frame(
    mapping: dict[str, Any],
    keys: tuple[str, ...],
    path: str,
) -> None:
    """Validate frame declarations without coercing ambiguous values."""

    for key in keys:
        if key in mapping and not _is_zero_frame(mapping[key]):
            raise ToolInputError(
                "unsupported_frame",
                f"{path}.{key} must be 0; got {mapping[key]!r}.",
                f"{path}.{key}",
            )


def _is_sha256_hex(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdefABCDEF" for character in value)
    )


def _check_declared_alias(
    mapping: dict[str, Any],
    keys: tuple[str, ...],
    path: str,
    aliases: dict[str, str],
    code: str,
) -> None:
    """Accept documented aliases while rejecting conflicting declarations."""

    normalized: list[tuple[str, str]] = []
    for key in keys:
        if key not in mapping:
            continue
        value = mapping[key]
        canonical = aliases.get(value) if isinstance(value, str) else None
        if canonical is None:
            raise ToolInputError(
                code,
                f"{path}.{key} must be one of {sorted(aliases)}; got {value!r}.",
                f"{path}.{key}",
            )
        normalized.append((key, canonical))
    if normalized and len({value for _key, value in normalized}) != 1:
        rendered = ", ".join(f"{key}={value!r}" for key, value in normalized)
        raise ToolInputError(code, f"{path} has conflicting aliases: {rendered}.", path)


def _validate_coordinate_metadata(metadata: Any, path: str) -> None:
    if metadata is None:
        raise ToolInputError(
            "invalid_coordinate_metadata",
            "coordinate_metadata must be an object when supplied; null is ambiguous.",
            path,
        )
    if not isinstance(metadata, dict):
        raise ToolInputError("invalid_coordinate_metadata", "Coordinate metadata must be a JSON object.", path)
    supported_fields = {
        "range",
        "aa_range",
        "aa_coordinate_system",
        "coordinate_system",
        "nt_range",
        "nt_coordinate_system",
        "aa_unit",
        "nt_unit",
        "coordinate_frame",
        "frame",
        "aa_frame",
        "nt_frame",
        "strand",
        "aa_strand",
        "nt_strand",
    }
    unknown = sorted(set(metadata) - supported_fields)
    if unknown:
        raise ToolInputError(
            "unsupported_coordinate_metadata",
            f"{path} has unsupported fields: {unknown}.",
            path,
        )
    _check_declared_value(
        metadata,
        ("range", "aa_range", "aa_coordinate_system", "coordinate_system"),
        HALF_OPEN_COORDINATE_SYSTEM,
        path,
        "unsupported_coordinate_system",
    )
    _check_declared_value(
        metadata,
        ("nt_range", "nt_coordinate_system"),
        HALF_OPEN_COORDINATE_SYSTEM,
        path,
        "unsupported_coordinate_system",
    )
    _check_declared_value(metadata, ("aa_unit",), "aa", path, "unsupported_unit")
    _check_declared_value(metadata, ("nt_unit",), "nt", path, "unsupported_unit")
    _check_declared_value(metadata, ("coordinate_frame",), SOURCE_SEQUENCE_FRAME, path, "unsupported_coordinate_frame")
    _check_declared_frame(metadata, ("frame", "aa_frame", "nt_frame"), path)
    _check_declared_value(metadata, ("strand", "aa_strand", "nt_strand"), "forward", path, "unsupported_strand")


def _validate_translation_context(context: Any, path: str) -> tuple[str, dict[str, Any]]:
    """Validate and normalize the frame-zero context shared with translation."""

    if context is None:
        raise ToolInputError(
            "invalid_translation_context",
            "translation_context must be an object when supplied; null is ambiguous.",
            path,
        )
    if not isinstance(context, dict):
        raise ToolInputError("invalid_translation_context", "translation_context must be a JSON object.", path)
    supported = {
        "molecule": "DNA",
        "unit": "nt",
        "strand": "forward",
        "coordinate_system": HALF_OPEN_COORDINATE_SYSTEM,
    }
    supported_fields = set(supported) | {
        "frame",
        "genetic_code",
        "genetic_code_id",
        "partial_codon_policy",
        "partial_codons",
    }
    unknown = sorted(set(context) - supported_fields)
    if unknown:
        raise ToolInputError(
            "unsupported_translation_context",
            f"{path} has unsupported fields: {unknown}.",
            path,
        )
    _check_declared_frame(context, ("frame",), path)
    _check_declared_value(context, ("molecule",), supported["molecule"], path, "unsupported_molecule")
    _check_declared_value(context, ("unit",), supported["unit"], path, "unsupported_unit")
    _check_declared_value(context, ("strand",), supported["strand"], path, "unsupported_strand")
    _check_declared_value(
        context,
        ("coordinate_system",),
        supported["coordinate_system"],
        path,
        "unsupported_coordinate_system",
    )
    _check_declared_alias(
        context,
        ("genetic_code", "genetic_code_id"),
        path,
        {
            "standard": "standard",
            "NCBI-1": "standard",
            "NCBI-1-standard": "standard",
            "NCBI-standard-1": "standard",
        },
        "unsupported_genetic_code",
    )
    _check_declared_alias(
        context,
        ("partial_codon_policy", "partial_codons"),
        path,
        {"reject": "reject", "complete_codons_only": "reject"},
        "unsupported_partial_codon_policy",
    )
    normalized = dict(DEFAULT_TRANSLATION_CONTEXT)
    if "frame" in context:
        normalized["frame"] = 0
    if "genetic_code" in context or "genetic_code_id" in context:
        normalized["genetic_code"] = "standard"
    if "partial_codon_policy" in context or "partial_codons" in context:
        normalized["partial_codon_policy"] = "reject"
    return "validated", normalized


def _sequence_metadata(fixture: dict[str, Any], sequence_ids: set[str]) -> dict[str, dict[str, Any]]:
    raw = fixture.get("sequence_metadata", {})
    if raw is None:
        raise ToolInputError("invalid_sequence_metadata", "sequence_metadata must be a JSON object when supplied.", "sequence_metadata")
    if not isinstance(raw, dict):
        raise ToolInputError("invalid_sequence_metadata", "sequence_metadata must be a JSON object.", "sequence_metadata")
    output: dict[str, dict[str, Any]] = {}
    for sequence_id, metadata in raw.items():
        path = f"sequence_metadata.{sequence_id}"
        if sequence_id not in sequence_ids:
            raise ToolInputError(
                "unknown_sequence_metadata_id",
                f"Metadata names unknown sequence {sequence_id!r}.",
                path,
            )
        if not isinstance(metadata, dict):
            raise ToolInputError("invalid_sequence_metadata", "Each sequence metadata value must be a JSON object.", path)
        supported_fields = {
            "molecule",
            "molecule_type",
            "unit",
            "coordinate_system",
            "coordinate_range",
            "range",
            "frame",
            "strand",
            "object_id",
            "source_record_id",
            "content_sha256",
            "sequence_sha256",
        }
        unknown = sorted(set(metadata) - supported_fields)
        if unknown:
            raise ToolInputError(
                "unsupported_sequence_metadata",
                f"{path} has unsupported fields: {unknown}.",
                path,
            )
        _check_declared_alias(
            metadata,
            ("molecule", "molecule_type"),
            path,
            {"DNA": "DNA"},
            "unsupported_molecule",
        )
        _check_declared_value(metadata, ("unit",), "nt", path, "unsupported_unit")
        _check_declared_alias(
            metadata,
            ("coordinate_system", "coordinate_range", "range"),
            path,
            {HALF_OPEN_COORDINATE_SYSTEM: HALF_OPEN_COORDINATE_SYSTEM},
            "unsupported_coordinate_system",
        )
        _check_declared_frame(metadata, ("frame",), path)
        _check_declared_value(metadata, ("strand",), "forward", path, "unsupported_strand")
        for key in ("object_id", "source_record_id"):
            if key in metadata and metadata[key] is None:
                raise ToolInputError(
                    "invalid_object_identity",
                    f"{path}.{key} must be omitted or a non-empty string.",
                    f"{path}.{key}",
                )
            if key in metadata and not isinstance(metadata[key], str):
                raise ToolInputError("invalid_object_identity", f"{path}.{key} must be a string when supplied.", f"{path}.{key}")
            if key in metadata and metadata[key] == "":
                raise ToolInputError("invalid_object_identity", f"{path}.{key} cannot be empty.", f"{path}.{key}")
        declared_hashes: list[tuple[str, str]] = []
        for key in ("content_sha256", "sequence_sha256"):
            declared_hash = metadata.get(key)
            if key in metadata and not _is_sha256_hex(declared_hash):
                raise ToolInputError(
                    "invalid_sequence_hash",
                    f"{path}.{key} must be a SHA-256 hex digest when supplied.",
                    f"{path}.{key}",
                )
            if declared_hash is not None:
                declared_hashes.append((key, declared_hash.lower()))
        if declared_hashes and len({value for _key, value in declared_hashes}) != 1:
            rendered = ", ".join(f"{key}={value!r}" for key, value in declared_hashes)
            raise ToolInputError(
                "conflicting_sequence_hashes",
                f"{path} has conflicting hash declarations: {rendered}.",
                path,
            )
        output[sequence_id] = metadata
    return output


def _source_object(sequence_id: str, dna: str, metadata: dict[str, Any]) -> dict[str, Any]:
    content_sha256 = sha256_bytes(dna.encode("utf-8"))
    object_id = metadata.get("object_id") or f"dna:{sequence_id}:{content_sha256}"
    source_record_id = metadata.get("source_record_id")
    return {
        "object_id": object_id,
        "object_kind": "source_sequence",
        "sequence_id": sequence_id,
        "content_sha256": content_sha256,
        "length": len(dna),
        "unit": "nt",
        "coordinate_system": HALF_OPEN_COORDINATE_SYSTEM,
        "strand": metadata.get("strand", INPUT_ORIENTATION),
        "strand_status": "declared" if "strand" in metadata else "not_inferred",
        "frame": 0,
        "source_record_id": source_record_id,
        "source_record_id_status": "declared" if source_record_id is not None else "unknown_not_supplied",
    }


def _derived_protein_object(sequence_id: str, protein: str, source_object: dict[str, Any]) -> dict[str, Any]:
    content_sha256 = sha256_bytes(protein.encode("utf-8"))
    return {
        "object_id": f"protein:translation:{sequence_id}:{content_sha256}",
        "object_kind": "derived_protein_sequence",
        "sequence_id": sequence_id,
        "content_sha256": content_sha256,
        "length": len(protein),
        "unit": "aa",
        "coordinate_system": HALF_OPEN_COORDINATE_SYSTEM,
        "source_object_id": source_object["object_id"],
        "derivation": {
            "operation": "translate",
            "genetic_code": "standard",
            "frame": 0,
            "strand": source_object["strand"],
            "source_range": [0, source_object["length"]],
        },
    }


def _validate_row_metadata(row: dict[str, Any], path: str) -> None:
    supported_fields = {
        "round_id",
        "library_id",
        "seed_id",
        "reference_id",
        "source_record_id",
        "window",
        "aa_window",
        "aa_window_0based_halfopen",
        "nt_window",
        "range",
        "coordinate_system",
        "aa_coordinate_system",
        "nt_coordinate_system",
        "coordinate_frame",
        "aa_coordinate_frame",
        "nt_coordinate_frame",
        "unit",
        "aa_unit",
        "nt_unit",
        "coordinate_unit",
        "frame",
        "aa_frame",
        "nt_frame",
        "strand",
        "aa_strand",
        "nt_strand",
    }
    unknown = sorted(set(row) - supported_fields)
    if unknown:
        raise ToolInputError(
            "unsupported_row_fields",
            f"{path} has unsupported fields: {unknown}.",
            path,
        )
    _check_declared_value(
        row,
        ("range", "coordinate_system", "aa_coordinate_system", "nt_coordinate_system"),
        HALF_OPEN_COORDINATE_SYSTEM,
        path,
        "unsupported_coordinate_system",
    )
    _check_declared_value(
        row,
        ("coordinate_frame", "aa_coordinate_frame", "nt_coordinate_frame"),
        SOURCE_SEQUENCE_FRAME,
        path,
        "unsupported_coordinate_frame",
    )
    _check_declared_value(row, ("unit", "aa_unit"), "aa", path, "unsupported_unit")
    _check_declared_value(row, ("coordinate_unit",), "aa", path, "unsupported_unit")
    _check_declared_value(row, ("nt_unit",), "nt", path, "unsupported_unit")
    _check_declared_frame(row, ("frame", "aa_frame", "nt_frame"), path)
    _check_declared_value(row, ("strand", "aa_strand", "nt_strand"), "forward", path, "unsupported_strand")


def _compatibility_contract() -> dict[str, Any]:
    return {
        "schema_version": SEQUENCE_SLICING_CONTRACT_VERSION,
        "legacy_fields_preserved": [
            "aa_to_nt_mappings",
            "historical_row_localization",
            "correct_nt_difference_count",
            "correct_aa_substitution_count",
            "correct_changed_codon_positions",
        ],
        "coordinate_contract": {
            "aa": {
                "unit": "aa",
                "range": HALF_OPEN_COORDINATE_SYSTEM,
                "frame": "protein index derived from DNA frame 0",
                "coordinate_frame": SOURCE_SEQUENCE_FRAME,
            },
            "nt": {
                "unit": "nt",
                "range": HALF_OPEN_COORDINATE_SYSTEM,
                "strand": INPUT_ORIENTATION,
                "coordinate_frame": SOURCE_SEQUENCE_FRAME,
            },
            "mapping_rule": "nt_start = 3 * aa_start; nt_end = 3 * aa_end",
            "codon_width_nt": 3,
            "mapping_window_space": SOURCE_SEQUENCE_FRAME,
        },
        "identity_contract": {
            "source_identity": "object_id plus content_sha256",
            "derived_identity": "derived protein object references source_object_id and derivation",
            "source_registry_id": "source_record_id is preserved when supplied; otherwise status is unknown_not_supplied",
        },
        "localization_contract": {
            "row_window": "aa_window and nt_window are absolute coordinates on the full source sequence; the explicit absolute_*_window aliases remove naming ambiguity",
            "difference_positions": "both window-local and source-absolute positions are emitted",
            "codon_positions": "0-based codon indices, with local and source-absolute forms",
            "row_identity": "row_object_id and row_content_sha256 identify the normalized logical row; row_index preserves its position in the supplied rows list",
            "denominators": {
                "nt_difference_rate": "nt_window length; null for an empty window",
                "aa_substitution_rate": "aa_window length; null for an empty window",
            },
        },
        "codon_boundary_contract": {
            "translation_frame": 0,
            "codon_width_nt": 3,
            "aa_window_is_codon_index_range": True,
            "nt_boundaries_are_multiples_of_three": True,
            "partial_codons": "rejected",
        },
        "unsupported_or_unknown": [
            "Alignment-aware indels and gaps are rejected; historical row localization rejects unequal-length source sequences, while standalone mappings require only that each window fit each translated source.",
            "The tool does not infer a strand or reverse-complement input; translation uses the supplied DNA in frame 0.",
            "A missing source_record_id does not establish external provenance; it is reported as unknown_not_supplied.",
            "A missing translation_context is reported as not_supplied; the normalized context in the artifact records the fixed default used for the calculation.",
        ],
    }


def tool_sequence_properties(fixture: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    sequences = fixture["sequences"]
    return {
        "sequence_lengths": {key: len(value) for key, value in sequences.items()},
        "dna_alphabet_ok": {key: set(value) <= set("ACGT") for key, value in sequences.items()},
        "gc_fraction": {key: gc_fraction(value) for key, value in sequences.items()},
        "base_counts": {key: dict(collections.Counter(value)) for key, value in sequences.items()},
    }


def tool_transcription_translation(fixture: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    sequences = fixture["sequences"]
    proteins = {key: translate(value) for key, value in sequences.items()}
    checks = []
    for sequence_id, dna in sequences.items():
        protein = proteins[sequence_id]
        for window_id, bounds in fixture["aa_windows"].items():
            lo, hi = bounds
            checks.append({
                "sequence_id": sequence_id,
                "window_id": window_id,
                "aa_window": [lo, hi],
                "nt_window": [3 * lo, 3 * hi],
                "translated_window_matches": translate(dna[3 * lo : 3 * hi]) == protein[lo:hi],
            })
    return {
        "protein_sequences": proteins,
        "protein_lengths": {key: len(value) for key, value in proteins.items()},
        "internal_stop_counts": {key: value[:-1].count("*") for key, value in proteins.items()},
        "window_translation_checks": checks,
    }


def _normalize_legacy_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Accept the compact row shape emitted by TadACampaign skill analysis."""
    normalized: list[dict[str, Any]] = []
    for index, raw_row in enumerate(rows):
        if not isinstance(raw_row, dict):
            raise ToolInputError(
                "invalid_row",
                "Each row must be a JSON object.",
                f"rows[{index}]",
            )
        row = dict(raw_row)
        if "round_id" in row and "library_id" in row and row["round_id"] != row["library_id"]:
            raise ToolInputError(
                "conflicting_row_identity_aliases",
                f"rows[{index}] has conflicting round_id/library_id declarations.",
                f"rows[{index}]",
            )
        if "round_id" not in row:
            row["round_id"] = str(row.get("library_id", f"row_{index}"))
        # TadACampaign's compact historical rows used ``window`` or
        # ``aa_window`` and implicitly compared a seed against itself.  Keep
        # that narrow compatibility path, while preserving strict validation
        # for the formal row schema where reference_id is required.
        compact_window = (
            "aa_window_0based_halfopen" not in row
            and ("window" in row or "aa_window" in row)
        )
        if compact_window and "reference_id" not in row and "seed_id" in row:
            row["reference_id"] = row["seed_id"]
        window_aliases = [
            (field, row[field])
            for field in ("aa_window_0based_halfopen", "window", "aa_window")
            if field in row
        ]
        if window_aliases:
            _first_field, first_bounds = window_aliases[0]
            if any(bounds != first_bounds for _field, bounds in window_aliases[1:]):
                rendered = ", ".join(f"{field}={bounds!r}" for field, bounds in window_aliases)
                raise ToolInputError(
                    "conflicting_row_window_aliases",
                    f"rows[{index}] has conflicting window declarations: {rendered}.",
                    f"rows[{index}]",
                )
            if "aa_window_0based_halfopen" not in row:
                row["aa_window_0based_halfopen"] = first_bounds
        bounds = row.get("aa_window_0based_halfopen")
        if "nt_window" not in row and isinstance(bounds, list) and len(bounds) == 2:
            row["nt_window"] = [3 * bounds[0], 3 * bounds[1]]
        normalized.append(row)
    return normalized


def tool_sequence_slicing(fixture: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    sequences = fixture.get("sequences")
    if not isinstance(sequences, dict):
        raise ToolInputError("invalid_sequences", "The tool input must contain a JSON object at sequences.", "sequences")
    if not sequences:
        raise ToolInputError("invalid_sequences", "The sequences object must contain at least one DNA sequence.", "sequences")
    for sequence_id in sequences:
        if not isinstance(sequence_id, str) or not sequence_id:
            raise ToolInputError(
                "invalid_sequence_id",
                "Sequence identifiers must be non-empty strings.",
                f"sequences.{sequence_id}",
            )
    if "coordinate_metadata" in fixture:
        _validate_coordinate_metadata(fixture["coordinate_metadata"], "coordinate_metadata")
    if "translation_context" in fixture:
        translation_context_status, normalized_translation_context = _validate_translation_context(
            fixture["translation_context"],
            "translation_context",
        )
    else:
        translation_context_status = "not_supplied"
        normalized_translation_context = dict(DEFAULT_TRANSLATION_CONTEXT)
    metadata_by_id = _sequence_metadata(fixture, set(sequences))
    source_objects: dict[str, dict[str, Any]] = {}
    proteins: dict[str, str] = {}
    protein_objects: dict[str, dict[str, Any]] = {}
    object_ids: dict[str, str] = {}
    for sequence_id, dna in sequences.items():
        path = f"sequences.{sequence_id}"
        if not isinstance(dna, str):
            raise ToolInputError("invalid_dna_sequence", "Each sequence must be a DNA string.", path)
        if not dna:
            raise ToolInputError("invalid_dna_sequence", "DNA sequences must not be empty.", path)
        try:
            proteins[sequence_id] = translate(dna)
        except (TypeError, ValueError) as exc:
            raise ToolInputError("invalid_dna_sequence", str(exc), path) from exc
        expected_sequence_hash = sha256_bytes(dna.encode("utf-8"))
        for hash_key in ("content_sha256", "sequence_sha256"):
            declared_hash = metadata_by_id.get(sequence_id, {}).get(hash_key)
            if declared_hash is not None and declared_hash.lower() != expected_sequence_hash:
                raise ToolInputError(
                    "sequence_hash_mismatch",
                    f"{path} does not match sequence_metadata.{sequence_id}.{hash_key}.",
                    f"sequence_metadata.{sequence_id}.{hash_key}",
                )
        source_objects[sequence_id] = _source_object(sequence_id, dna, metadata_by_id.get(sequence_id, {}))
        object_id = source_objects[sequence_id]["object_id"]
        if object_id in object_ids:
            raise ToolInputError(
                "duplicate_object_id",
                f"Object id {object_id!r} is used by both {object_ids[object_id]!r} and {sequence_id!r}.",
                path,
            )
        object_ids[object_id] = sequence_id
        protein_objects[sequence_id] = _derived_protein_object(sequence_id, proteins[sequence_id], source_objects[sequence_id])
    rows = _normalize_legacy_rows(rows)
    if "aa_windows" not in fixture:
        aa_windows = {}
        for index, row in enumerate(rows):
            bounds = row.get("aa_window_0based_halfopen")
            if isinstance(bounds, list) and len(bounds) == 2:
                window_id = str(row.get("round_id", row.get("library_id", f"row_{index}")))
                if window_id in aa_windows:
                    raise ToolInputError(
                        "duplicate_window_id",
                        f"Rows derive duplicate window id {window_id!r}.",
                        f"rows[{index}].round_id",
                    )
                aa_windows[window_id] = bounds
    else:
        raw_windows = fixture["aa_windows"]
        if not isinstance(raw_windows, dict):
            raise ToolInputError("invalid_field", "aa_windows must be a JSON object when supplied.", "aa_windows")
        aa_windows = raw_windows
    mappings = []
    for window_id, bounds in aa_windows.items():
        lo, hi = _half_open_bounds(bounds, f"aa_windows.{window_id}")
        for sequence_id, protein in proteins.items():
            _half_open_bounds(bounds, f"aa_windows.{window_id}", len(protein))
        mappings.append(
            {
                "window_id": window_id,
                "aa_window": [lo, hi],
                "nt_window": [3 * lo, 3 * hi],
                "codon_window": [lo, hi],
                "codon_width_nt": 3,
                "codon_boundary_status": "aligned_frame_zero",
                "aa_unit": "aa",
                "nt_unit": "nt",
                "aa_coordinate_system": HALF_OPEN_COORDINATE_SYSTEM,
                "nt_coordinate_system": HALF_OPEN_COORDINATE_SYSTEM,
                "aa_coordinate_frame": SOURCE_SEQUENCE_FRAME,
                "nt_coordinate_frame": SOURCE_SEQUENCE_FRAME,
                "translation_frame": 0,
                "strand": INPUT_ORIENTATION,
                "source_sequence_object_ids": [source_objects[key]["object_id"] for key in sequences],
                "derived_protein_object_ids": [protein_objects[key]["object_id"] for key in sequences],
                "source_sequence_object_ids_by_sequence": {
                    key: source_objects[key]["object_id"] for key in sequences
                },
                "derived_protein_object_ids_by_sequence": {
                    key: protein_objects[key]["object_id"] for key in sequences
                },
                "source_sequence_content_sha256_by_sequence": {
                    key: source_objects[key]["content_sha256"] for key in sequences
                },
                "derived_protein_content_sha256_by_sequence": {
                    key: protein_objects[key]["content_sha256"] for key in sequences
                },
            }
        )
    localized = []
    for row_index, row in enumerate(rows):
        path = f"rows[{row_index}]"
        if not isinstance(row, dict):
            raise ToolInputError("invalid_row", "Each row must be a JSON object.", path)
        for field in ("round_id", "seed_id", "reference_id", "aa_window_0based_halfopen"):
            if field not in row:
                raise ToolInputError("row_missing_field", f"Row is missing required field {field!r}.", path)
        for field in ("round_id", "seed_id", "reference_id"):
            if not isinstance(row[field], str) or not row[field]:
                raise ToolInputError(
                    "invalid_row_identity",
                    f"{path}.{field} must be a non-empty string.",
                    f"{path}.{field}",
                )
        _validate_row_metadata(row, path)
        seed_id = row["seed_id"]
        reference_id = row["reference_id"]
        if seed_id not in sequences:
            raise ToolInputError("unknown_sequence_id", f"Row seed_id {seed_id!r} is not in sequences.", f"{path}.seed_id")
        if reference_id not in sequences:
            raise ToolInputError("unknown_sequence_id", f"Row reference_id {reference_id!r} is not in sequences.", f"{path}.reference_id")
        seed = sequences[seed_id]
        reference = sequences[reference_id]
        protein_seed = proteins[seed_id]
        protein_reference = proteins[reference_id]
        if len(seed) != len(reference):
            raise ToolInputError(
                "unequal_sequence_lengths",
                "Historical row localization requires equal-length DNA source sequences; alignment is not inferred.",
                path,
            )
        if len(protein_seed) != len(protein_reference):
            raise ToolInputError(
                "unequal_protein_lengths",
                "Historical row localization requires equal-length translated proteins.",
                path,
            )
        lo, hi = _half_open_bounds(row["aa_window_0based_halfopen"], f"{path}.aa_window_0based_halfopen", len(protein_seed))
        nt_lo, nt_hi = 3 * lo, 3 * hi
        if "nt_window" in row:
            supplied_nt_window = row["nt_window"]
            if not isinstance(supplied_nt_window, list) or len(supplied_nt_window) != 2 or not all(
                _is_integer(value) for value in supplied_nt_window
            ):
                raise ToolInputError(
                    "invalid_nt_window",
                    f"{path}.nt_window must contain two integer bounds.",
                    f"{path}.nt_window",
                )
            if supplied_nt_window != [nt_lo, nt_hi]:
                raise ToolInputError(
                    "incompatible_nt_window",
                    f"{path}.nt_window does not match the required AA-to-NT mapping {[nt_lo, nt_hi]}.",
                    f"{path}.nt_window",
                )
        nt_positions = [i for i, (x, y) in enumerate(zip(seed[nt_lo:nt_hi], reference[nt_lo:nt_hi])) if x != y]
        aa_positions = [i for i, (x, y) in enumerate(zip(protein_seed[lo:hi], protein_reference[lo:hi])) if x != y]
        local_codon_positions = sorted({position // 3 for position in nt_positions})
        nt_denominator = nt_hi - nt_lo
        aa_denominator = hi - lo
        row_source_record_id = row.get("source_record_id")
        if row_source_record_id is not None and (not isinstance(row_source_record_id, str) or not row_source_record_id):
            raise ToolInputError("invalid_row_identity", "source_record_id must be a non-empty string when supplied.", f"{path}.source_record_id")
        localized.append({
            "row_index": row_index,
            "round_id": row["round_id"],
            "seed_id": seed_id,
            "reference_id": reference_id,
            "aa_window": [lo, hi],
            "nt_window": [nt_lo, nt_hi],
            "absolute_aa_window": [lo, hi],
            "absolute_nt_window": [nt_lo, nt_hi],
            "window_coordinate_space": SOURCE_SEQUENCE_FRAME,
            "codon_window": [lo, hi],
            "codon_width_nt": 3,
            "codon_boundary_status": "aligned_frame_zero",
            "correct_nt_difference_count": len(nt_positions),
            "correct_aa_substitution_count": len(aa_positions),
            "correct_changed_codon_positions": local_codon_positions,
            "row_object_id": f"row:{digest(row)}",
            "row_content_sha256": digest(row),
            "source_record_id": row_source_record_id,
            "source_record_id_status": "declared" if row_source_record_id is not None else "unknown_not_supplied",
            "seed_source_object_id": source_objects[seed_id]["object_id"],
            "reference_source_object_id": source_objects[reference_id]["object_id"],
            "seed_derived_protein_object_id": protein_objects[seed_id]["object_id"],
            "reference_derived_protein_object_id": protein_objects[reference_id]["object_id"],
            "aa_unit": "aa",
            "nt_unit": "nt",
            "aa_coordinate_system": HALF_OPEN_COORDINATE_SYSTEM,
            "nt_coordinate_system": HALF_OPEN_COORDINATE_SYSTEM,
            "aa_coordinate_frame": SOURCE_SEQUENCE_FRAME,
            "nt_coordinate_frame": SOURCE_SEQUENCE_FRAME,
            "translation_frame": 0,
            "strand": INPUT_ORIENTATION,
            "local_nt_difference_positions": nt_positions,
            "absolute_nt_difference_positions": [nt_lo + position for position in nt_positions],
            "local_aa_substitution_positions": aa_positions,
            "absolute_aa_substitution_positions": [lo + position for position in aa_positions],
            "local_changed_codon_positions": local_codon_positions,
            "absolute_changed_codon_positions": [lo + position for position in local_codon_positions],
            "nt_difference_denominator": nt_denominator,
            "aa_substitution_denominator": aa_denominator,
            "nt_difference_rate": len(nt_positions) / nt_denominator if nt_denominator else None,
            "aa_substitution_rate": len(aa_positions) / aa_denominator if aa_denominator else None,
            "indel_or_gap_normalization": "unsupported_equal_length_ungapped_only",
        })
    return {
        "aa_to_nt_mappings": mappings,
        "historical_row_localization": localized,
        "source_sequence_objects": source_objects,
        "derived_protein_objects": protein_objects,
        "compatibility": {
            **_compatibility_contract(),
            "translation_context": {
                "status": translation_context_status,
                "normalized": normalized_translation_context,
            },
        },
    }


def tool_distance_calculations(fixture: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    sequences = fixture["sequences"]
    proteins = {key: translate(value) for key, value in sequences.items()}
    return {
        "dna_hamming_pairs": pairwise(sequences),
        "aa_hamming_pairs": pairwise(proteins),
        "dna_pair_count": len(pairwise(sequences)),
        "aa_pair_count": len(pairwise(proteins)),
    }


def tool_motif_search(fixture: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    sequences = fixture["sequences"]
    motifs = ["ATG", "TAA", "TAG", "TGA"]
    return {"motif_positions": {key: {motif: [i for i in range(len(seq)) if seq.startswith(motif, i)] for motif in motifs} for key, seq in sequences.items()}}


def tool_pairwise_alignment(fixture: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    sequences = fixture["sequences"]
    aligned = []
    keys = list(sequences)
    for i, left in enumerate(keys):
        for right in keys[i + 1 :]:
            a, b = sequences[left], sequences[right]
            matches = sum(x == y for x, y in zip(a, b))
            aligned.append({"left": left, "right": right, "length": min(len(a), len(b)), "matches": matches, "identity": matches / min(len(a), len(b))})
    return {"pairwise_alignments": aligned}


def tool_codon_usage(fixture: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"codon_counts": {key: dict(collections.Counter(seq[i : i + 3] for i in range(0, len(seq), 3))) for key, seq in fixture["sequences"].items()}}


def tool_reverse_complement(fixture: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    complement = str.maketrans("ACGT", "TGCA")
    return {"reverse_complement_sha256": {key: hashlib.sha256(seq.translate(complement)[::-1].encode()).hexdigest() for key, seq in fixture["sequences"].items()}}


def tool_sequence_statistics(fixture: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    sequences = fixture["sequences"]
    return {"n_sequences": len(sequences), "unique_dna_sequences": len(set(sequences.values())), "unique_proteins": len({translate(seq) for seq in sequences.values()}), "length_distribution": dict(collections.Counter(map(len, sequences.values())))}


def tool_filter_sequences(fixture: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"passing_ids": [key for key, seq in fixture["sequences"].items() if len(seq) == 501 and set(seq) <= set("ACGT") and "*" not in translate(seq)[:-1]], "rejected_ids": []}


def tool_msa_statistics(fixture: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    proteins = [translate(seq) for seq in fixture["sequences"].values()]
    columns = []
    for i in range(len(proteins[0])):
        counts = collections.Counter(protein[i] for protein in proteins)
        total = sum(counts.values())
        entropy = -sum((n / total) * math.log2(n / total) for n in counts.values())
        columns.append({"position": i, "consensus": counts.most_common(1)[0][0], "entropy_bits": entropy})
    return {"alignment_length": len(proteins[0]), "columns": columns}


def tool_restriction_sites(fixture: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    enzymes = {"EcoRI": "GAATTC", "BamHI": "GGATCC", "HindIII": "AAGCTT"}
    return {"restriction_sites": {key: {enzyme: [i for i in range(len(seq)) if seq.startswith(site, i)] for enzyme, site in enzymes.items()} for key, seq in fixture["sequences"].items()}}


TOOL_FUNCTIONS = {'sequence-slicing': tool_sequence_slicing}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tool", required=True, choices=sorted(TOOL_FUNCTIONS))
    parser.add_argument("--input", required=True)
    parser.add_argument("--rows")
    parser.add_argument("--output", required=True)
    parser.add_argument("--run-root", required=True)
    args = parser.parse_args(argv)
    started = time.time()
    input_bytes: bytes | None = None
    rows_bytes: bytes | None = None
    input_hash: str | None = None
    rows_hash: str | None = None
    error_path = str(args.input)

    def emit_error(error: dict[str, Any]) -> int:
        print(
            json.dumps({"status": "error", "tool_id": args.tool, "error": error}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2

    try:
        input_bytes = Path(args.input).read_bytes()
        input_hash = sha256_bytes(input_bytes)
        fixture = json.loads(input_bytes)
        if not isinstance(fixture, dict):
            raise ToolInputError("invalid_fixture", "The tool input must be a JSON object.", "input")
        if args.tool == "sequence-slicing" and "sequences" not in fixture:
            raise ToolInputError("missing_field", "The tool input must contain sequences.", "sequences")
        if args.rows:
            error_path = str(args.rows)
            rows_bytes = Path(args.rows).read_bytes()
            rows_hash = sha256_bytes(rows_bytes)
        rows = rows_from_file(args.rows)
        payload = TOOL_FUNCTIONS[args.tool](fixture, rows)
        result = {
            "tool_id": args.tool,
            "input_sha256": input_hash,
            "rows_sha256": rows_hash,
            "payload": payload,
        }
        if args.tool == "sequence-slicing":
            result["contract_version"] = SEQUENCE_SLICING_CONTRACT_VERSION
        output_path = Path(args.output)
        error_path = str(output_path)
        _write_json_artifact(output_path, result)
        event = {
            "tool_id": args.tool,
            "input": args.input,
            "rows": args.rows,
            "output": args.output,
            "input_sha256": result["input_sha256"],
            "output_sha256": sha256_bytes(output_path.read_bytes()),
            "started_epoch": started,
            "finished_epoch": time.time(),
            "exit_code": 0,
        }
        run_root = Path(args.run_root) / "tool_calls.jsonl"
        run_root.parent.mkdir(parents=True, exist_ok=True)
        with run_root.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        print(json.dumps({"status": "ok", "tool_id": args.tool, "output": args.output}, ensure_ascii=False))
        return 0
    except json.JSONDecodeError as exc:
        return emit_error({"code": "invalid_json", "message": str(exc), "path": error_path})
    except ToolInputError as exc:
        return emit_error(exc.as_dict())
    except OSError as exc:
        return emit_error({"code": "io_error", "message": str(exc), "path": error_path})
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        return emit_error({"code": "input_error", "message": str(exc), "path": error_path})
    except Exception as exc:  # noqa: BLE001 - malformed tool input must never leak a traceback.
        return emit_error({"code": "tool_error", "message": str(exc) or exc.__class__.__name__, "path": error_path})


if __name__ == "__main__":
    raise SystemExit(main())
