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


DISTANCE_SCHEMA_VERSION = 2
DISTANCE_CONTRACT_VERSION = 1
DISTANCE_ERROR_EXIT_CODE = 2
DNA_ALPHABET = frozenset("ACGT")


class DistanceInputError(ValueError):
    """A deterministic, user-actionable input or contract error."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        field: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.field = field
        self.details = details


def _input_error(
    code: str,
    message: str,
    *,
    field: str | None = None,
    details: dict[str, Any] | None = None,
) -> DistanceInputError:
    return DistanceInputError(code, message, field=field, details=details)


def _validate_distance_sequences(fixture: Any) -> dict[str, str]:
    """Validate the input contract used by distance-calculations.

    The legacy implementation relied on incidental ``KeyError``/``TypeError``
    exceptions and accepted an empty sequence map. Valid V0 fixtures are
    unchanged; invalid fixtures now fail with an object-specific message.
    Empty sequence maps and empty sequence *values* remain valid boundary
    inputs so that zero-pair and zero-length-denominator results are reported
    explicitly rather than hidden.
    """

    if not isinstance(fixture, dict):
        raise _input_error("invalid_input", "The tool input must be a JSON object", field="$" )
    sequences = fixture.get("sequences")
    if not isinstance(sequences, dict):
        raise _input_error(
            "invalid_sequences",
            "The tool input must contain a sequences object",
            field="sequences",
        )

    validated: dict[str, str] = {}
    for sequence_id, dna in sequences.items():
        if not isinstance(sequence_id, str):
            raise _input_error(
                "invalid_sequence_identity",
                "Each sequence object identity must be a string",
                field="sequences",
            )
        if not isinstance(dna, str):
            raise _input_error(
                "invalid_sequence_value",
                f"Sequence {sequence_id!r} must be a DNA string",
                field=f"sequences.{sequence_id}",
            )
        invalid_positions = [(index, base) for index, base in enumerate(dna) if base not in DNA_ALPHABET]
        if invalid_positions:
            index, base = invalid_positions[0]
            raise _input_error(
                "unsupported_dna_symbol",
                f"Sequence {sequence_id!r} contains unsupported DNA symbol {base!r} at 0-based position {index}; "
                "only uppercase A/C/G/T are supported",
                field=f"sequences.{sequence_id}",
                details={"position": index, "symbol": base},
            )
        if len(dna) % 3:
            raise _input_error(
                "partial_codon",
                f"Sequence {sequence_id!r} has DNA length {len(dna)}, which is not divisible by three for frame-zero translation",
                field=f"sequences.{sequence_id}",
                details={"length": len(dna), "codon_size_nt": 3},
            )
        validated[sequence_id] = dna
    return validated


def _normalize_frame_zero(value: Any, field_name: str) -> int:
    """Accept only an unambiguous declaration of the supported frame zero."""

    if isinstance(value, bool):
        raise _input_error("unsupported_frame", f"{field_name} must be frame 0, not a boolean", field=field_name)
    if isinstance(value, int):
        frame = value
    elif isinstance(value, str) and value == "0":
        # Some JSON-producing agents serialize a numeric frame as "0".  This
        # value is unambiguous; other strings must not be guessed.
        frame = 0
    else:
        raise _input_error(
            "unsupported_frame",
            f"{field_name} must be the integer frame 0 or the string '0'",
            field=field_name,
        )
    if frame != 0:
        raise _input_error(
            "unsupported_frame",
            f"{field_name} must be frame 0; received {value!r}",
            field=field_name,
        )
    return 0


def _validate_translation_context(fixture: dict[str, Any]) -> dict[str, Any]:
    """Validate optional context used by the observed V1 composition call.

    V0 inputs omitted this metadata and therefore keep the same default. When
    an agent supplies it, the tool must not silently calculate frame zero while
    the declaration asks for another frame, strand, code, or partial-codon
    policy.
    """

    if "translation_context" not in fixture:
        return {
            "molecule": "DNA",
            "strand": "as_provided",
            "frame": 0,
            "genetic_code": "standard",
            "coordinate_system": "0-based-half-open",
            "partial_codon_policy": "reject",
            "source": "default_v0_frame_zero",
        }
    raw_context = fixture["translation_context"]
    if not isinstance(raw_context, dict):
        raise _input_error(
            "invalid_translation_context",
            "translation_context must be a JSON object when supplied",
            field="translation_context",
        )

    molecule = raw_context.get("molecule", "DNA")
    if molecule != "DNA":
        raise _input_error(
            "unsupported_molecule",
            "translation_context.molecule must be 'DNA'",
            field="translation_context.molecule",
        )

    raw_strand = raw_context.get("strand", "forward")
    if raw_strand not in {"forward", "as_provided"}:
        raise _input_error(
            "unsupported_strand",
            "translation_context.strand must be 'forward' or 'as_provided'; reverse-strand conversion is unsupported",
            field="translation_context.strand",
        )

    frame = _normalize_frame_zero(raw_context.get("frame", 0), "translation_context.frame")

    genetic_code = raw_context.get("genetic_code", "standard")
    if genetic_code not in {"standard", "standard_nuclear"}:
        raise _input_error(
            "unsupported_genetic_code",
            "translation_context.genetic_code must be 'standard' or 'standard_nuclear'",
            field="translation_context.genetic_code",
        )

    coordinate_system = raw_context.get("coordinate_system", "0-based-half-open")
    if coordinate_system != "0-based-half-open":
        raise _input_error(
            "unsupported_coordinate_system",
            "translation_context.coordinate_system must be '0-based-half-open'",
            field="translation_context.coordinate_system",
        )

    partial_codon_policy = raw_context.get("partial_codon_policy", "reject")
    if partial_codon_policy not in {"reject", "error"}:
        raise _input_error(
            "unsupported_partial_codon_policy",
            "translation_context.partial_codon_policy must be 'reject' or 'error'",
            field="translation_context.partial_codon_policy",
        )

    return {
        "molecule": "DNA",
        "strand": "as_provided",
        "frame": frame,
        "genetic_code": "standard",
        "coordinate_system": "0-based-half-open",
        "partial_codon_policy": "reject",
        "source": "input",
    }


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalised_distance(count: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return count / denominator


def _pair_metric(
    left: str,
    right: str,
    records: dict[str, str],
    *,
    unit: str,
    pair_index: int,
    input_sha256: str | None = None,
) -> dict[str, Any]:
    """Return a labelled metric for one ordered-by-input unordered pair."""

    left_value, right_value = records[left], records[right]
    left_length, right_length = len(left_value), len(right_value)
    left_object_identity = f"sequences.{left}"
    right_object_identity = f"sequences.{right}"
    base = {
        "pair_index": pair_index,
        "left": left,
        "right": right,
        "left_object_identity": left_object_identity,
        "right_object_identity": right_object_identity,
        "pair_key": [left_object_identity, right_object_identity],
        "input_sha256": input_sha256,
        "unit": unit,
        "position_base": 0,
        "left_length": left_length,
        "right_length": right_length,
    }
    if left_length != right_length:
        return {
            **base,
            "status": "skipped_unequal_length",
            "reason_code": "unequal_length",
            "distance": None,
            "denominator": None,
            "normalized_distance": None,
            "identity": None,
            "positions": [],
        }

    positions = [index for index, (left_base, right_base) in enumerate(zip(left_value, right_value)) if left_base != right_base]
    count = len(positions)
    denominator = left_length
    normalized = _normalised_distance(count, denominator)
    return {
        **base,
        "status": "included" if denominator else "included_empty",
        "reason_code": "ok" if denominator else "zero_length_denominator",
        "distance": count,
        "denominator": denominator,
        "normalized_distance": normalized,
        "identity": None if normalized is None else 1.0 - normalized,
        "positions": positions,
    }


def _all_pair_metrics(
    records: dict[str, str],
    *,
    unit: str,
    input_sha256: str | None = None,
) -> list[dict[str, Any]]:
    keys = list(records)
    metrics = []
    pair_index = 0
    for i, left in enumerate(keys):
        for right in keys[i + 1 :]:
            metrics.append(
                _pair_metric(
                    left,
                    right,
                    records,
                    unit=unit,
                    pair_index=pair_index,
                    input_sha256=input_sha256,
                )
            )
            pair_index += 1
    return metrics


def _skipped_pairs(metrics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            key: record[key]
            for key in (
                "pair_index",
                "left",
                "right",
                "left_object_identity",
                "right_object_identity",
                "pair_key",
                "input_sha256",
                "unit",
                "left_length",
                "right_length",
                "status",
                "reason_code",
            )
        }
        for record in metrics
        if record["status"] == "skipped_unequal_length"
    ]


def _sequence_metadata(
    sequences: dict[str, str],
    proteins: dict[str, str],
    *,
    input_sha256: str | None = None,
) -> dict[str, dict[str, Any]]:
    return {
        sequence_id: {
            "object_identity": f"sequences.{sequence_id}",
            "sequence_id": sequence_id,
            "input_sha256": input_sha256,
            "dna": {
                "unit": "nt",
                "length": len(dna),
                "sha256": _sha256_text(dna),
                "coordinate_system": "0-based offsets over the provided sequence",
                "frame": 0,
                "strand": "as_provided",
            },
            "translated_aa": {
                "unit": "aa",
                "length": len(proteins[sequence_id]),
                "sha256": _sha256_text(proteins[sequence_id]),
                "coordinate_system": "0-based offsets over the frame-zero translation",
            },
        }
        for sequence_id, dna in sequences.items()
    }


def _metric_summary(metrics: list[dict[str, Any]]) -> dict[str, Any]:
    included = [record for record in metrics if record["status"] in {"included", "included_empty"}]
    usable = [record for record in included if record["normalized_distance"] is not None]
    closest = None
    if usable:
        closest = min(usable, key=lambda record: record["normalized_distance"])
        closest = {
            key: closest[key]
            for key in ("left", "right", "unit", "distance", "denominator", "normalized_distance", "identity")
        }
    return {
        "pair_count": len(metrics),
        "included_pair_count": len(included),
        "skipped_pair_count": len(metrics) - len(included),
        "zero_denominator_pair_count": sum(record["denominator"] == 0 for record in included),
        "closest_pair_by_normalized_distance": closest,
        "tie_policy": "first pair in input insertion order when normalized distances tie",
    }


def _comparison_contract() -> dict[str, Any]:
    return {
        "schema_version": DISTANCE_CONTRACT_VERSION,
        "input_identity": {
            "artifact_join_keys": ["input_sha256", "rows_sha256"],
            "input_sha256": "SHA-256 of the exact input file bytes read before JSON parsing",
            "rows_sha256": "SHA-256 of the exact rows file bytes; null when --rows is omitted",
            "row_index": "zero-based index in the supplied JSON rows list",
        },
        "status_vocabulary": {
            "pair": ["included", "included_empty", "skipped_unequal_length"],
            "row": ["included", "included_empty", "skipped_unequal_length", "rejected"],
            "artifact": ["success", "error"],
        },
        "pair_identity": {
            "object_type": "sequence",
            "identity_key": "input.sequences object key",
            "pair_direction": "unordered; left/right follow input insertion order",
            "pair_index": "zero-based deterministic order over all unordered input pairs",
            "pair_key": "[left_object_identity, right_object_identity]",
            "object_hashes": "payload.sequence_metadata_v1 records DNA and translated-AA SHA-256 values",
            "record_provenance": "each pair metric repeats left/right object identities and input_sha256",
        },
        "position_semantics": {
            "base": 0,
            "range": "half-open [0, length)",
            "dna": "nucleotide positions in the provided DNA string",
            "translated_aa": "amino-acid positions in the frame-zero translation",
        },
        "metric_fields": {
            "distance": "raw mismatch count in the declared unit; equals len(positions) for included pairs",
            "denominator": "number of positions in the compared equal-length range",
            "normalized_distance": "distance / denominator; null when denominator is zero or the pair is skipped",
            "identity": "1 - normalized_distance; null when normalized_distance is null",
        },
        "dna": {
            "unit": "nt",
            "frame": 0,
            "strand": "as_provided",
            "alphabet": "uppercase A/C/G/T",
            "equal_length_policy": "only equal-length pairs are included in the legacy raw list; skipped pairs are explicit in V1 fields",
            "denominator": "equal pair DNA length in nt",
            "normalization": "mismatch_count / denominator; null when denominator is zero",
        },
        "translated_aa": {
            "unit": "aa",
            "translation": {
                "genetic_code": "standard nuclear codon table",
                "codon_size_nt": 3,
                "frame": 0,
                "strand": "as_provided",
                "partial_codon_policy": "reject",
                "invalid_symbol_policy": "error",
                "stop_policy": "preserve '*' and do not filter stop codons",
            },
            "equal_length_policy": "only equal-length translated pairs are included in the legacy raw list; skipped pairs are explicit in V1 fields",
            "denominator": "equal translated-AA length in aa",
            "normalization": "mismatch_count / denominator; null when denominator is zero",
        },
        "rows": {
            "optional": True,
            "schema": {
                "required": ["seed_id", "reference_id", "aa_window_0based_halfopen"],
                "optional": ["round_id", "frame", "strand", "coordinate_unit"],
            },
            "row_direction": "seed_id is left and reference_id is right; requested row order is retained",
            "coordinate_conversion": "AA [lo, hi) maps to DNA [3*lo, 3*hi) under frame-zero translation",
            "row_identity": "row_id is rows[index]; row_provenance repeats index, round_id, object identities, and input/rows hashes",
            "unsupported_relationship": "rows with missing objects, invalid ranges, nonzero frame, reverse strand, or non-AA coordinate units are reported as rejected row records; no guessing or coercion occurs",
            "unequal_length_policy": "a row comparing unequal DNA or translated-AA lengths is skipped as a whole; no truncation, padding, or overlap denominator is inferred",
        },
        "legacy_fields": "dna_hamming_pairs, aa_hamming_pairs, dna_pair_count, and aa_pair_count are retained byte-for-byte for valid V0 fixtures",
        "input_error_policy": "invalid JSON, invalid sequence data, unreadable inputs, and unsupported top-level translation combinations publish a complete error artifact when possible, print JSON on stderr, and exit 2",
    }


def _row_rejection(row_index: int, row: Any, reason_code: str, message: str) -> dict[str, Any]:
    return {
        "row_index": row_index,
        "round_id": row.get("round_id") if isinstance(row, dict) else None,
        "status": "rejected",
        "reason_code": reason_code,
        "message": message,
    }


def _append_row_result(
    comparisons: list[dict[str, Any]],
    record: dict[str, Any],
    *,
    row_index: int,
    row: Any,
    input_sha256: str | None,
    rows_sha256: str | None,
) -> None:
    """Attach stable input, row, and object provenance to every row result."""

    row_id = f"rows[{row_index}]"
    seed_id = row.get("seed_id") if isinstance(row, dict) else None
    reference_id = row.get("reference_id") if isinstance(row, dict) else None
    left = record.get("left", seed_id)
    right = record.get("right", reference_id)
    if isinstance(seed_id, str):
        record.setdefault("seed_id", seed_id)
    if isinstance(reference_id, str):
        record.setdefault("reference_id", reference_id)
    if isinstance(left, str):
        record.setdefault("left_object_identity", f"sequences.{left}")
    if isinstance(right, str):
        record.setdefault("right_object_identity", f"sequences.{right}")
    if isinstance(left, str) and isinstance(right, str):
        record.setdefault("pair_key", [f"sequences.{left}", f"sequences.{right}"])
    record["row_id"] = row_id
    record["input_sha256"] = input_sha256
    record["rows_sha256"] = rows_sha256
    record["row_provenance"] = {
        "row_id": row_id,
        "row_index": row_index,
        "round_id": row.get("round_id") if isinstance(row, dict) else None,
        "seed_id": seed_id,
        "reference_id": reference_id,
        "seed_object_identity": f"sequences.{seed_id}" if isinstance(seed_id, str) else None,
        "reference_object_identity": (
            f"sequences.{reference_id}" if isinstance(reference_id, str) else None
        ),
        "input_sha256": input_sha256,
        "rows_sha256": rows_sha256,
    }
    comparisons.append(record)


def _row_comparisons(
    sequences: dict[str, str],
    proteins: dict[str, str],
    rows: list[dict[str, Any]],
    *,
    input_sha256: str | None = None,
    rows_sha256: str | None = None,
) -> list[dict[str, Any]]:
    comparisons: list[dict[str, Any]] = []
    for row_index, row in enumerate(rows):
        if not isinstance(row, dict):
            _append_row_result(
                comparisons,
                _row_rejection(row_index, row, "row_not_object", "Each requested comparison row must be a JSON object"),
                row_index=row_index,
                row=row,
                input_sha256=input_sha256,
                rows_sha256=rows_sha256,
            )
            continue

        left = row.get("seed_id")
        right = row.get("reference_id")
        if not isinstance(left, str) or not isinstance(right, str):
            _append_row_result(
                comparisons,
                _row_rejection(
                    row_index,
                    row,
                    "missing_pair_identity",
                    "Rows require seed_id and reference_id; no alternate left/right names are inferred",
                ),
                row_index=row_index,
                row=row,
                input_sha256=input_sha256,
                rows_sha256=rows_sha256,
            )
            continue
        if left not in sequences or right not in sequences:
            _append_row_result(
                comparisons,
                {
                    **_row_rejection(
                        row_index,
                        row,
                        "unknown_sequence_id",
                        f"Row references unknown sequence object(s): {left!r}, {right!r}",
                    ),
                    "left": left,
                    "right": right,
                },
                row_index=row_index,
                row=row,
                input_sha256=input_sha256,
                rows_sha256=rows_sha256,
            )
            continue

        raw_frame = row.get("frame", 0)
        try:
            _normalize_frame_zero(raw_frame, f"rows[{row_index}].frame")
        except ValueError:
            _append_row_result(
                comparisons,
                {
                    **_row_rejection(
                        row_index,
                        row,
                        "unsupported_frame",
                        "Only frame 0 is supported; use integer 0 or the unambiguous string '0'",
                    ),
                    "left": left,
                    "right": right,
                },
                row_index=row_index,
                row=row,
                input_sha256=input_sha256,
                rows_sha256=rows_sha256,
            )
            continue
        if row.get("strand", "as_provided") not in {"as_provided", "forward"}:
            _append_row_result(
                comparisons,
                {
                    **_row_rejection(
                        row_index,
                        row,
                        "unsupported_strand",
                        "Only the provided strand is supported; no reverse-complement conversion is inferred",
                    ),
                    "left": left,
                    "right": right,
                },
                row_index=row_index,
                row=row,
                input_sha256=input_sha256,
                rows_sha256=rows_sha256,
            )
            continue
        if row.get("coordinate_unit", "aa") != "aa":
            _append_row_result(
                comparisons,
                {
                    **_row_rejection(
                        row_index,
                        row,
                        "unsupported_coordinate_unit",
                        "Rows must declare AA coordinates; nucleotide coordinates are not guessed",
                    ),
                    "left": left,
                    "right": right,
                },
                row_index=row_index,
                row=row,
                input_sha256=input_sha256,
                rows_sha256=rows_sha256,
            )
            continue

        window = row.get("aa_window_0based_halfopen")
        if (
            not isinstance(window, list)
            or len(window) != 2
            or any(isinstance(value, bool) or not isinstance(value, int) for value in window)
        ):
            _append_row_result(
                comparisons,
                {
                    **_row_rejection(
                        row_index,
                        row,
                        "invalid_aa_window",
                        "aa_window_0based_halfopen must contain two integer bounds",
                    ),
                    "left": left,
                    "right": right,
                },
                row_index=row_index,
                row=row,
                input_sha256=input_sha256,
                rows_sha256=rows_sha256,
            )
            continue
        aa_start, aa_end = window
        if aa_start < 0 or aa_end < aa_start or aa_end > len(proteins[left]) or aa_end > len(proteins[right]):
            _append_row_result(
                comparisons,
                {
                    **_row_rejection(
                        row_index,
                        row,
                        "aa_window_out_of_range",
                        f"AA window {window!r} must satisfy 0 <= start <= end <= both translated sequence lengths",
                    ),
                    "left": left,
                    "right": right,
                    "left_aa_length": len(proteins[left]),
                    "right_aa_length": len(proteins[right]),
                },
                row_index=row_index,
                row=row,
                input_sha256=input_sha256,
                rows_sha256=rows_sha256,
            )
            continue

        dna_start, dna_end = 3 * aa_start, 3 * aa_end
        left_dna, right_dna = sequences[left], sequences[right]
        left_aa, right_aa = proteins[left], proteins[right]
        if len(left_dna) != len(right_dna):
            _append_row_result(
                comparisons,
                {
                    "row_index": row_index,
                    "round_id": row.get("round_id"),
                    "left": left,
                    "right": right,
                    "status": "skipped_unequal_length",
                    "reason_code": "unequal_length",
                    "aa_window_0based_halfopen": [aa_start, aa_end],
                    "dna_window_0based_halfopen": [dna_start, dna_end],
                    "dna": {
                        "unit": "nt",
                        "position_base": 0,
                        "left_length": len(left_dna),
                        "right_length": len(right_dna),
                        "status": "skipped_unequal_length",
                        "reason_code": "unequal_length",
                        "distance": None,
                        "denominator": None,
                        "normalized_distance": None,
                        "identity": None,
                        "positions": [],
                    },
                    "translated_aa": {
                        "unit": "aa",
                        "position_base": 0,
                        "left_length": len(left_aa),
                        "right_length": len(right_aa),
                        "status": "skipped_unequal_length",
                        "reason_code": "unequal_length",
                        "distance": None,
                        "denominator": None,
                        "normalized_distance": None,
                        "identity": None,
                        "positions": [],
                    },
                },
                row_index=row_index,
                row=row,
                input_sha256=input_sha256,
                rows_sha256=rows_sha256,
            )
            continue

        dna_left_window = left_dna[dna_start:dna_end]
        dna_right_window = right_dna[dna_start:dna_end]
        aa_left_window = left_aa[aa_start:aa_end]
        aa_right_window = right_aa[aa_start:aa_end]
        dna_positions = [
            dna_start + index
            for index, (left_base, right_base) in enumerate(zip(dna_left_window, dna_right_window))
            if left_base != right_base
        ]
        aa_positions = [
            aa_start + index
            for index, (left_base, right_base) in enumerate(zip(aa_left_window, aa_right_window))
            if left_base != right_base
        ]
        dna_denominator = len(dna_left_window)
        aa_denominator = len(aa_left_window)
        dna_normalized = _normalised_distance(len(dna_positions), dna_denominator)
        aa_normalized = _normalised_distance(len(aa_positions), aa_denominator)
        _append_row_result(
            comparisons,
            {
                "row_index": row_index,
                "round_id": row.get("round_id"),
                "left": left,
                "right": right,
                "status": "included" if aa_denominator else "included_empty",
                "reason_code": "ok" if aa_denominator else "zero_length_denominator",
                "aa_window_0based_halfopen": [aa_start, aa_end],
                "dna_window_0based_halfopen": [dna_start, dna_end],
                "dna": {
                    "unit": "nt",
                    "position_base": 0,
                    "distance": len(dna_positions),
                    "denominator": dna_denominator,
                    "normalized_distance": dna_normalized,
                    "identity": None if dna_normalized is None else 1.0 - dna_normalized,
                    "positions": dna_positions,
                },
                "translated_aa": {
                    "unit": "aa",
                    "position_base": 0,
                    "distance": len(aa_positions),
                    "denominator": aa_denominator,
                    "normalized_distance": aa_normalized,
                    "identity": None if aa_normalized is None else 1.0 - aa_normalized,
                    "positions": aa_positions,
                },
            },
            row_index=row_index,
            row=row,
            input_sha256=input_sha256,
            rows_sha256=rows_sha256,
        )
    return comparisons


def rows_from_file(rows_path: str | Path | None) -> list[dict[str, Any]]:
    """Load optional row evidence; /dev/null is the only empty-file sentinel."""
    if rows_path is None:
        return []
    path = Path(rows_path)
    raw = path.read_bytes()
    if path == Path("/dev/null"):
        return []
    if not raw.strip():
        raise _input_error(
            "invalid_rows_input",
            "Rows input is an empty file; write JSON [] or use /dev/null",
            field="rows",
        )
    try:
        rows = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _input_error(
            "invalid_rows_json",
            f"Rows input is not valid JSON: {exc.msg}",
            field="rows",
            details={"line": exc.lineno, "column": exc.colno},
        ) from exc
    except UnicodeDecodeError as exc:
        raise _input_error(
            "invalid_rows_encoding",
            "Rows input is not valid UTF-8 JSON",
            field="rows",
            details={"encoding": "utf-8", "reason": str(exc)},
        ) from exc
    if not isinstance(rows, list):
        raise _input_error("invalid_rows_input", "Rows input must be a JSON list", field="rows")
    return rows


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


def tool_sequence_slicing(fixture: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    sequences = fixture["sequences"]
    proteins = {key: translate(value) for key, value in sequences.items()}
    mappings = []
    for window_id, bounds in fixture["aa_windows"].items():
        lo, hi = bounds
        mappings.append({"window_id": window_id, "aa_window": [lo, hi], "nt_window": [3 * lo, 3 * hi]})
    localized = []
    for row in rows:
        seed = sequences[row["seed_id"]]
        reference = sequences[row["reference_id"]]
        protein_seed = proteins[row["seed_id"]]
        protein_reference = proteins[row["reference_id"]]
        lo, hi = row["aa_window_0based_halfopen"]
        nt_lo, nt_hi = 3 * lo, 3 * hi
        nt_positions = [i for i, (x, y) in enumerate(zip(seed[nt_lo:nt_hi], reference[nt_lo:nt_hi])) if x != y]
        aa_positions = [i for i, (x, y) in enumerate(zip(protein_seed[lo:hi], protein_reference[lo:hi])) if x != y]
        localized.append({
            "round_id": row["round_id"],
            "seed_id": row["seed_id"],
            "reference_id": row["reference_id"],
            "aa_window": [lo, hi],
            "nt_window": [nt_lo, nt_hi],
            "correct_nt_difference_count": len(nt_positions),
            "correct_aa_substitution_count": len(aa_positions),
            "correct_changed_codon_positions": sorted({position // 3 for position in nt_positions}),
        })
    return {"aa_to_nt_mappings": mappings, "historical_row_localization": localized}


def tool_distance_calculations(
    fixture: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    input_sha256: str | None = None,
    rows_sha256: str | None = None,
) -> dict[str, Any]:
    sequences = _validate_distance_sequences(fixture)
    translation_context = _validate_translation_context(fixture)
    proteins = {key: translate(value) for key, value in sequences.items()}
    # Keep these four legacy fields untouched. New consumers should use the
    # explicitly labelled V1 records below rather than inferring units or
    # denominators from the old count-only dictionaries.
    dna_pairs = pairwise(sequences)
    aa_pairs = pairwise(proteins)
    dna_metrics = _all_pair_metrics(sequences, unit="nt", input_sha256=input_sha256)
    aa_metrics = _all_pair_metrics(proteins, unit="aa", input_sha256=input_sha256)
    return {
        "dna_hamming_pairs": dna_pairs,
        "aa_hamming_pairs": aa_pairs,
        "dna_pair_count": len(dna_pairs),
        "aa_pair_count": len(aa_pairs),
        "contract_v1": _comparison_contract(),
        "translation_context_v1": translation_context,
        "sequence_metadata_v1": _sequence_metadata(
            sequences,
            proteins,
            input_sha256=input_sha256,
        ),
        "pair_metrics_v1": {
            "schema_version": DISTANCE_CONTRACT_VERSION,
            "dna": dna_metrics,
            "translated_aa": aa_metrics,
        },
        "skipped_dna_pairs": _skipped_pairs(dna_metrics),
        "skipped_aa_pairs": _skipped_pairs(aa_metrics),
        "decision_summary_v1": {
            "input_sequence_count": len(sequences),
            "possible_unordered_pair_count": len(sequences) * (len(sequences) - 1) // 2,
            "dna": _metric_summary(dna_metrics),
            "translated_aa": _metric_summary(aa_metrics),
            "consumer_rule": "Compare only records with status included or included_empty; keep DNA nt and translated-AA aa metrics separate and verify object_identity before joining artifacts",
        },
        "row_comparisons_v1": _row_comparisons(
            sequences,
            proteins,
            rows,
            input_sha256=input_sha256,
            rows_sha256=rows_sha256,
        ),
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


TOOL_FUNCTIONS = {'distance-calculations': tool_distance_calculations}


def _write_json_artifact(path: str | Path, value: dict[str, Any]) -> Path:
    """Atomically write an artifact after creating its requested parent."""

    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    temporary_path: Path | None = None
    try:
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            dir=output_path.parent,
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, output_path)
    except Exception:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass
        raise
    return output_path


def _append_tool_event(run_root: str | Path, event: dict[str, Any]) -> None:
    """Best-effort append-only telemetry recording."""

    try:
        path = Path(run_root) / "tool_calls.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        # The output artifact and stderr remain useful evidence if telemetry
        # cannot be written to a caller-supplied root.
        return


def _input_json_error(exc: json.JSONDecodeError) -> DistanceInputError:
    return _input_error(
        "invalid_input_json",
        f"Input is not valid JSON: {exc.msg}",
        field="input",
        details={"line": exc.lineno, "column": exc.colno},
    )


def _error_record(exc: Exception) -> dict[str, Any]:
    """Serialize input failures without losing their stable contract fields."""

    if isinstance(exc, DistanceInputError):
        record: dict[str, Any] = {
            "type": type(exc).__name__,
            "code": exc.code,
            "message": str(exc),
        }
        if exc.field is not None:
            record["field"] = exc.field
        if exc.details is not None:
            record["details"] = exc.details
        return record
    return {
        "type": type(exc).__name__,
        "code": "tool_error",
        "message": str(exc),
    }


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
    try:
        try:
            input_bytes = Path(args.input).read_bytes()
        except OSError as exc:
            raise _input_error(
                "unreadable_input",
                f"Unable to read input file {args.input!r}: {exc.strerror or str(exc)}",
                field="input",
                details={"path": args.input},
            ) from exc
        if args.rows:
            try:
                rows_bytes = Path(args.rows).read_bytes()
            except OSError as exc:
                raise _input_error(
                    "unreadable_rows",
                    f"Unable to read rows file {args.rows!r}: {exc.strerror or str(exc)}",
                    field="rows",
                    details={"path": args.rows},
                ) from exc
        try:
            fixture = json.loads(input_bytes)
        except json.JSONDecodeError as exc:
            raise _input_json_error(exc) from exc
        except UnicodeDecodeError as exc:
            raise _input_error(
                "invalid_input_encoding",
                "Input is not valid UTF-8 JSON",
                field="input",
                details={"encoding": "utf-8", "reason": str(exc)},
            ) from exc
        rows = rows_from_file(args.rows)
        input_sha256 = sha256_bytes(input_bytes)
        rows_sha256 = sha256_bytes(rows_bytes) if rows_bytes is not None else None
        if args.tool == "distance-calculations":
            payload = tool_distance_calculations(
                fixture,
                rows,
                input_sha256=input_sha256,
                rows_sha256=rows_sha256,
            )
        else:
            payload = TOOL_FUNCTIONS[args.tool](fixture, rows)
        if args.tool == "distance-calculations":
            result: dict[str, Any] = {
                "schema_version": DISTANCE_SCHEMA_VERSION,
                "status": "success",
                "tool_id": args.tool,
                "tool_version": DISTANCE_SCHEMA_VERSION,
                "input_sha256": input_sha256,
                "rows_sha256": rows_sha256,
                "payload": payload,
            }
        else:
            # Keep the shared toolkit's other reconstructed tools unchanged.
            result = {
                "tool_id": args.tool,
                "input_sha256": input_sha256,
                "rows_sha256": rows_sha256,
                "payload": payload,
            }
        output_path = _write_json_artifact(args.output, result)
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
        if args.tool == "distance-calculations":
            event.update(
                {
                    "schema_version": DISTANCE_SCHEMA_VERSION,
                    "status": "success",
                    "tool_version": DISTANCE_SCHEMA_VERSION,
                    "rows_sha256": result["rows_sha256"],
                }
            )
        _append_tool_event(args.run_root, event)
        print(json.dumps({"status": "ok", "tool_id": args.tool, "output": args.output}, ensure_ascii=False))
        return 0
    except Exception as exc:  # noqa: BLE001 - distance errors are structured below.
        if args.tool != "distance-calculations":
            raise
        result = {
            "schema_version": DISTANCE_SCHEMA_VERSION,
            "status": "error",
            "tool_id": args.tool,
            "tool_version": DISTANCE_SCHEMA_VERSION,
            "input_sha256": sha256_bytes(input_bytes) if input_bytes is not None else None,
            "rows_sha256": sha256_bytes(rows_bytes) if rows_bytes is not None else None,
            "exit_code": DISTANCE_ERROR_EXIT_CODE,
            "error": _error_record(exc),
        }
        output_path: Path | None = None
        output_write_error: dict[str, str] | None = None
        try:
            output_path = _write_json_artifact(args.output, result)
        except Exception as write_exc:  # noqa: BLE001 - report unusable output paths clearly.
            output_write_error = {
                "type": type(write_exc).__name__,
                "message": str(write_exc),
            }
        event = {
            "schema_version": DISTANCE_SCHEMA_VERSION,
            "status": "error",
            "tool_id": args.tool,
            "tool_version": DISTANCE_SCHEMA_VERSION,
            "input": args.input,
            "rows": args.rows,
            "output": args.output,
            "input_sha256": result["input_sha256"],
            "rows_sha256": result["rows_sha256"],
            "output_sha256": (
                sha256_bytes(output_path.read_bytes()) if output_path is not None else None
            ),
            "started_epoch": started,
            "finished_epoch": time.time(),
            "exit_code": DISTANCE_ERROR_EXIT_CODE,
            "error": result["error"],
        }
        if output_write_error is not None:
            event["output_write_error"] = output_write_error
        _append_tool_event(args.run_root, event)
        error_message = result["error"]
        if output_write_error is not None:
            error_message = {
                **error_message,
                "output_write_error": output_write_error,
            }
        print(
            json.dumps(
                {
                    "status": "error",
                    "tool_id": args.tool,
                    "exit_code": DISTANCE_ERROR_EXIT_CODE,
                    "error": error_message,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return DISTANCE_ERROR_EXIT_CODE


if __name__ == "__main__":
    raise SystemExit(main())
