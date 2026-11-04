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
import re
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


class ToolInputError(ValueError):
    """An input or option that cannot produce a valid tool artifact."""

    code = "invalid_input"


class UnsupportedCombinationError(ToolInputError):
    """A well-formed value that this tool intentionally does not support."""

    code = "unsupported_combination"


class JsonArgumentParser(argparse.ArgumentParser):
    """Make command-line validation failures follow the tool error envelope."""

    def error(self, message: str) -> None:  # pragma: no cover - exercised through main
        raise ToolInputError(message)


def translate(dna: str) -> str:
    if len(dna) % 3:
        raise ValueError(f"DNA length is not divisible by three: {len(dna)}")
    try:
        return "".join(CODON_TABLE[dna[i : i + 3]] for i in range(0, len(dna), 3))
    except KeyError as exc:
        raise ValueError(f"Unknown codon: {exc.args[0]}") from exc


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_bytes())


def load_fixture(input_path: str | Path, raw: bytes | None = None) -> dict[str, Any]:
    payload = json.loads(raw if raw is not None else Path(input_path).read_bytes())
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
    return rows_from_bytes(path.read_bytes(), path)


def rows_from_bytes(raw: bytes, rows_path: str | Path | None = None) -> list[dict[str, Any]]:
    """Decode the exact rows bytes used by the CLI invocation."""

    path = Path(rows_path) if rows_path is not None else None
    if path == Path("/dev/null") or not raw.strip():
        return []
    rows = json.loads(raw)
    if not isinstance(rows, list):
        raise ValueError("Rows input must be a JSON list")
    return rows


def _validate_pairwise_sequences(sequences: Any) -> dict[str, str]:
    """Validate the small sequence contract used by the pairwise tool only."""

    if not isinstance(sequences, dict):
        raise ValueError("pairwise-alignment requires sequences to be a JSON object")
    validated: dict[str, str] = {}
    for sequence_id, sequence in sequences.items():
        if not isinstance(sequence_id, str) or not sequence_id:
            raise ValueError("pairwise-alignment sequence ids must be non-empty strings")
        if not isinstance(sequence, str):
            raise ValueError(f"pairwise-alignment sequence {sequence_id!r} must be a string")
        if any(character.isspace() for character in sequence):
            raise ValueError(f"pairwise-alignment sequence {sequence_id!r} must not contain whitespace")
        if "-" in sequence:
            raise ValueError(
                f"pairwise-alignment sequence {sequence_id!r} contains '-' gaps; "
                "gapped alignment is unsupported"
            )
        validated[sequence_id] = sequence
    return validated


_SEQUENCE_METADATA_KEYS = frozenset(
    {
        "object_id",
        "source_record_id",
        "content_sha256",
        "sequence_sha256",
        "molecule_type",
        "molecule",
        "unit",
        "coordinate_system",
        "frame",
        "strand",
    }
)
_TRANSLATION_CONTEXT_KEYS = frozenset(
    {
        "molecule",
        "strand",
        "frame",
        "genetic_code",
        "coordinate_system",
        "partial_codon_policy",
    }
)
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_ALLOWED_STRANDS = frozenset({"as_provided", "forward", "reverse", "unknown"})
_ALLOWED_MOLECULES = frozenset({"DNA", "RNA", "protein", "unknown"})


def _normalize_frame(value: Any, path: str) -> tuple[int, bool]:
    """Accept integer frames and the unambiguous JSON-string forms '0'/'1'/'2'."""

    if isinstance(value, bool):
        raise ValueError(f"{path} must be frame integer 0, 1, or 2; got boolean")
    if isinstance(value, int):
        if value in (0, 1, 2):
            return value, False
        raise ValueError(f"{path} must be frame integer 0, 1, or 2; got {value!r}")
    if isinstance(value, str) and value in {"0", "1", "2"}:
        return int(value), True
    raise ValueError(
        f"{path} must be frame integer 0, 1, or 2, or the exact string '0', '1', or '2'; "
        f"got {value!r}"
    )


def _validate_metadata_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{path} must be a non-empty string")
    return value


def _validate_sha256(value: Any, path: str, expected: str) -> str:
    value = _validate_metadata_string(value, path)
    if not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{path} must be a 64-character SHA-256 hex digest")
    normalized = value.lower()
    if normalized != expected:
        raise ValueError(f"{path} does not match the exact UTF-8 sequence bytes")
    return normalized


def _validate_input_metadata(
    fixture: dict[str, Any],
    sequences: dict[str, str],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any] | None, list[str]]:
    """Validate metadata used for object joins without changing raw comparison semantics."""

    raw_metadata = fixture.get("sequence_metadata", {})
    if raw_metadata is None or not isinstance(raw_metadata, dict):
        raise ValueError("sequence_metadata must be a JSON object when supplied")

    metadata: dict[str, dict[str, Any]] = {}
    normalizations: list[str] = []
    object_ids: dict[str, str] = {}
    for sequence_id, raw_record in raw_metadata.items():
        path = f"sequence_metadata[{sequence_id!r}]"
        if sequence_id not in sequences:
            raise ValueError(f"{path} references an unknown sequence object")
        if not isinstance(raw_record, dict):
            raise ValueError(f"{path} must be a JSON object")
        unknown = sorted(set(raw_record) - _SEQUENCE_METADATA_KEYS)
        if unknown:
            raise ValueError(f"{path} has unsupported fields: {', '.join(unknown)}")

        sequence_hash = sha256_bytes(sequences[sequence_id].encode("utf-8"))
        record: dict[str, Any] = {}
        for key, value in raw_record.items():
            field_path = f"{path}.{key}"
            if key in {"object_id", "source_record_id", "molecule_type", "molecule", "unit", "coordinate_system"}:
                record[key] = _validate_metadata_string(value, field_path)
            elif key == "strand":
                strand = _validate_metadata_string(value, field_path)
                if strand not in _ALLOWED_STRANDS:
                    raise ValueError(
                        f"{field_path} must be one of {sorted(_ALLOWED_STRANDS)}; got {strand!r}"
                    )
                record[key] = strand
            elif key == "frame":
                frame, normalized = _normalize_frame(value, field_path)
                record[key] = frame
                if normalized:
                    normalizations.append(f"{field_path}: string {value!r} normalized to integer {frame}")
            elif key in {"content_sha256", "sequence_sha256"}:
                record[key] = _validate_sha256(value, field_path, sequence_hash)
            else:  # pragma: no cover - guarded by the unknown-field check above
                raise ValueError(f"{field_path} is unsupported")

        declared_hashes = [record[key] for key in ("content_sha256", "sequence_sha256") if key in record]
        if len(set(declared_hashes)) > 1:
            raise ValueError(f"{path}.content_sha256 and sequence_sha256 disagree")
        if "molecule" in record and "molecule_type" in record and record["molecule"] != record["molecule_type"]:
            raise ValueError(f"{path}.molecule and molecule_type disagree")
        if "object_id" in record:
            previous_id = object_ids.get(record["object_id"])
            if previous_id is not None:
                raise ValueError(
                    f"{path}.object_id duplicates sequence_metadata[{previous_id!r}].object_id"
                )
            object_ids[record["object_id"]] = sequence_id
        metadata[sequence_id] = record

    raw_context = fixture.get("translation_context")
    if raw_context is None:
        context = None
    else:
        if not isinstance(raw_context, dict):
            raise ValueError("translation_context must be a JSON object when supplied")
        unknown = sorted(set(raw_context) - _TRANSLATION_CONTEXT_KEYS)
        if unknown:
            raise ValueError(f"translation_context has unsupported fields: {', '.join(unknown)}")
        context = {}
        for key, value in raw_context.items():
            path = f"translation_context.{key}"
            if key == "frame":
                frame, normalized = _normalize_frame(value, path)
                context[key] = frame
                if normalized:
                    normalizations.append(f"{path}: string {value!r} normalized to integer {frame}")
            else:
                context[key] = _validate_metadata_string(value, path)
        if "strand" in context and context["strand"] not in _ALLOWED_STRANDS:
            raise ValueError(
                "translation_context.strand must be one of "
                f"{sorted(_ALLOWED_STRANDS)}; got {context['strand']!r}"
            )
        for key in ("molecule",):
            if key in context and context[key] not in _ALLOWED_MOLECULES:
                raise ValueError(
                    f"translation_context.{key} must be one of {sorted(_ALLOWED_MOLECULES)}; "
                    f"got {context[key]!r}"
                )
    return metadata, context, normalizations


def _pairwise_comparison(
    left_id: str,
    right_id: str,
    left: str,
    right: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the legacy row and an explicit audit row for one ungapped prefix."""

    compared_length = min(len(left), len(right))
    if compared_length == 0:
        raise ValueError(
            f"pairwise-alignment cannot compute identity for {left_id!r} and {right_id!r}: "
            "the compared prefix is empty"
        )
    difference_positions = [
        position
        for position, (left_symbol, right_symbol) in enumerate(zip(left, right))
        if left_symbol != right_symbol
    ]
    matches = compared_length - len(difference_positions)
    identity = matches / compared_length if compared_length else None

    # Keep this object byte-for-byte compatible in field names and values with V0.
    legacy = {
        "left": left_id,
        "right": right_id,
        "length": compared_length,
        "matches": matches,
        "identity": identity,
    }
    audit = {
        "left_id": left_id,
        "right_id": right_id,
        "left_sequence_sha256": sha256_bytes(left.encode("utf-8")),
        "right_sequence_sha256": sha256_bytes(right.encode("utf-8")),
        "left_length": len(left),
        "right_length": len(right),
        "length_delta": len(left) - len(right),
        "compared_length": compared_length,
        "compared_ranges": {
            "left": [0, compared_length],
            "right": [0, compared_length],
        },
        "uncompared_suffix_ranges": {
            "left": [compared_length, len(left)],
            "right": [compared_length, len(right)],
        },
        "matches": matches,
        "difference_positions": difference_positions,
        "identity": identity,
        "identity_denominator": "compared_length",
        "identity_status": (
            "equal_length"
            if len(left) == len(right)
            else "prefix_only_unequal_length"
        ),
        "comparable_equal_length": len(left) == len(right),
        "coordinate_system": "0-based half-open raw sequence-symbol offsets",
        "orientation": "as-provided; left/right are display order, not query/reference",
    }
    return legacy, audit


def _global_alignment(
    left_id: str,
    right_id: str,
    left: str,
    right: str,
) -> dict[str, Any]:
    """Build a deterministic global alignment without changing the V0 result.

    The scoring is deliberately small and explicit: match=1, mismatch=-1, and
    gap=-1.  Backtracking prefers diagonal, then a left-symbol gap, then a
    right-symbol gap whenever scores tie.  The resulting columns retain raw
    coordinates for both sequences; a gap coordinate is the zero-width
    boundary at which the missing symbol would be inserted.
    """

    match_score = 1
    mismatch_score = -1
    gap_score = -1
    n, m = len(left), len(right)
    scores = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        scores[i][0] = i * gap_score
    for j in range(1, m + 1):
        scores[0][j] = j * gap_score

    for i in range(1, n + 1):
        for j in range(1, m + 1):
            diagonal = scores[i - 1][j - 1] + (
                match_score if left[i - 1] == right[j - 1] else mismatch_score
            )
            left_gap = scores[i - 1][j] + gap_score
            right_gap = scores[i][j - 1] + gap_score
            # min() makes the tie rule part of the contract rather than an
            # implementation accident caused by dictionary or loop order.
            scores[i][j] = max((diagonal, left_gap, right_gap))

    reverse_columns: list[tuple[int | None, int | None]] = []
    i, j = n, m
    while i or j:
        diagonal = (
            scores[i - 1][j - 1]
            + (match_score if left[i - 1] == right[j - 1] else mismatch_score)
            if i and j
            else None
        )
        left_gap = scores[i - 1][j] + gap_score if i else None
        right_gap = scores[i][j - 1] + gap_score if j else None

        # Tie priority is diagonal > left-symbol gap > right-symbol gap.
        if diagonal is not None and scores[i][j] == diagonal:
            reverse_columns.append((i - 1, j - 1))
            i -= 1
            j -= 1
        elif left_gap is not None and scores[i][j] == left_gap:
            reverse_columns.append((i - 1, None))
            i -= 1
        elif right_gap is not None and scores[i][j] == right_gap:
            reverse_columns.append((None, j - 1))
            j -= 1
        else:  # pragma: no cover - the recurrence and backtrack share scores
            raise AssertionError("alignment backtrack could not follow the score matrix")

    coordinate_columns = list(reversed(reverse_columns))
    columns: list[dict[str, Any]] = []
    aligned_left: list[str] = []
    aligned_right: list[str] = []
    left_cursor = 0
    right_cursor = 0
    for alignment_column, (left_index, right_index) in enumerate(coordinate_columns):
        left_boundary = left_cursor
        right_boundary = right_cursor
        left_symbol = left[left_index] if left_index is not None else None
        right_symbol = right[right_index] if right_index is not None else None
        if left_index is None:
            operation = "insertion"
            inserted_side = "right"
        elif right_index is None:
            operation = "insertion"
            inserted_side = "left"
        elif left_symbol == right_symbol:
            operation = "match"
            inserted_side = None
        else:
            operation = "mismatch"
            inserted_side = None

        if left_index is not None:
            left_cursor += 1
            left_range = [left_boundary, left_cursor]
            aligned_left.append(left_symbol or "")
        else:
            left_range = [left_boundary, left_boundary]
            aligned_left.append("-")
        if right_index is not None:
            right_cursor += 1
            right_range = [right_boundary, right_cursor]
            aligned_right.append(right_symbol or "")
        else:
            right_range = [right_boundary, right_boundary]
            aligned_right.append("-")

        column: dict[str, Any] = {
            "alignment_column": alignment_column,
            "operation": operation,
            "left_index": left_index,
            "right_index": right_index,
            "left_symbol": left_symbol,
            "right_symbol": right_symbol,
            "left_range_0based_halfopen": left_range,
            "right_range_0based_halfopen": right_range,
            "coordinate_system": "0-based half-open raw sequence-symbol offsets",
        }
        if inserted_side is not None:
            column["inserted_sequence_id"] = left_id if inserted_side == "left" else right_id
            column["gap_sequence_id"] = right_id if inserted_side == "left" else left_id
            column["event_type"] = f"insertion_in_{inserted_side}"
        columns.append(column)

    gap_events: list[dict[str, Any]] = []
    insertions: list[dict[str, Any]] = []
    deletions: list[dict[str, Any]] = []
    current_gap: dict[str, Any] | None = None
    for column in columns:
        if column["operation"] != "insertion":
            current_gap = None
            continue
        inserted_side = "left" if column["inserted_sequence_id"] == left_id else "right"
        if (
            current_gap is not None
            and current_gap["inserted_side"] == inserted_side
            and current_gap["alignment_columns_0based_halfopen"][1] == column["alignment_column"]
        ):
            current_gap["alignment_columns_0based_halfopen"][1] += 1
            source_range_key = "left_range_0based_halfopen" if inserted_side == "left" else "right_range_0based_halfopen"
            other_range_key = "right_range_0based_halfopen" if inserted_side == "left" else "left_range_0based_halfopen"
            current_gap["source_range_0based_halfopen"][1] = column[source_range_key][1]
            current_gap["relative_boundary_0based"] = column[other_range_key]
            current_gap["source_symbols"] += left[column["left_index"]] if inserted_side == "left" else right[column["right_index"]]
            continue

        source_range_key = "left_range_0based_halfopen" if inserted_side == "left" else "right_range_0based_halfopen"
        other_range_key = "right_range_0based_halfopen" if inserted_side == "left" else "left_range_0based_halfopen"
        source_id = left_id if inserted_side == "left" else right_id
        other_id = right_id if inserted_side == "left" else left_id
        source_range = list(column[source_range_key])
        relative_boundary = list(column[other_range_key])
        source_symbol = column["left_symbol"] if inserted_side == "left" else column["right_symbol"]
        current_gap = {
            "inserted_side": inserted_side,
            "event_index": len(gap_events),
            "type": "insertion",
            "event_type": f"insertion_in_{inserted_side}",
            "sequence_id": source_id,
            "relative_to_sequence_id": other_id,
            "source_range_0based_halfopen": source_range,
            "source_symbols": source_symbol or "",
            "relative_boundary_0based": relative_boundary,
            "left_range_0based_halfopen": list(column["left_range_0based_halfopen"]),
            "right_range_0based_halfopen": list(column["right_range_0based_halfopen"]),
            "alignment_columns_0based_halfopen": [
                column["alignment_column"],
                column["alignment_column"] + 1,
            ],
            "coordinate_system": "0-based half-open raw sequence-symbol offsets",
        }
        gap_events.append(current_gap)

    for event in gap_events:
        inserted_side = event["inserted_side"]
        source_id = event["sequence_id"]
        other_id = event["relative_to_sequence_id"]
        source_range = list(event["source_range_0based_halfopen"])
        relative_boundary = list(event["relative_boundary_0based"])
        deletion = {
            "type": "deletion",
            "event_type": f"deletion_in_{'right' if inserted_side == 'left' else 'left'}",
            "sequence_id": other_id,
            "relative_to_sequence_id": source_id,
            "coordinate_0based_halfopen": relative_boundary,
            "deleted_symbol_count": 0,
            "alignment_columns_0based_halfopen": list(event["alignment_columns_0based_halfopen"]),
            "coordinate_system": "0-based half-open raw sequence-symbol offsets; deletion is a zero-width boundary",
        }
        event["deletion"] = deletion
        insertions.append({key: value for key, value in event.items() if key != "inserted_side"})
        deletions.append(deletion)

    match_count = sum(column["operation"] == "match" for column in columns)
    mismatch_count = sum(column["operation"] == "mismatch" for column in columns)
    gap_column_count = sum(column["operation"] == "insertion" for column in columns)
    alignment_length = len(columns)
    return {
        "method": "global_needleman_wunsch",
        "scoring": {"match": match_score, "mismatch": mismatch_score, "gap": gap_score},
        "tie_break": ["diagonal", "left_symbol_gap", "right_symbol_gap"],
        "score": scores[n][m],
        "aligned_left": "".join(aligned_left),
        "aligned_right": "".join(aligned_right),
        "alignment_length": alignment_length,
        "matches": match_count,
        "mismatches": mismatch_count,
        "gap_columns": gap_column_count,
        "identity": match_count / alignment_length if alignment_length else None,
        "identity_denominator": "alignment_columns",
        "coordinate_system": "0-based half-open raw sequence-symbol offsets",
        "left_sequence_id": left_id,
        "right_sequence_id": right_id,
        "columns": columns,
        "gap_events": [event for event in gap_events],
        "insertions": insertions,
        "deletions": deletions,
        "gap_policy": "input gaps are rejected; output gaps are inferred only by this global alignment",
    }


def _validate_aa_window(
    row_index: int,
    row: dict[str, Any],
    query_id: str,
    reference_id: str,
    query: str,
    reference: str,
    query_metadata: dict[str, Any] | None = None,
    reference_metadata: dict[str, Any] | None = None,
    translation_context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Validate and explicitly project an optional frame-zero AA window."""

    if "aa_window_0based_halfopen" not in row:
        return None

    for sequence_id, metadata in (
        (query_id, query_metadata or {}),
        (reference_id, reference_metadata or {}),
    ):
        frame = metadata.get("frame", 0)
        if frame != 0:
            raise ValueError(
                f"rows[{row_index}] AA window requires frame zero; "
                f"sequence_metadata[{sequence_id!r}].frame is {frame!r}"
            )
        strand = metadata.get("strand")
        if strand in {"reverse", "unknown"}:
            raise ValueError(
                f"rows[{row_index}] AA window requires forward/as-provided strand; "
                f"sequence_metadata[{sequence_id!r}].strand is {strand!r}"
            )
        molecule = metadata.get("molecule", metadata.get("molecule_type"))
        if molecule is not None and molecule != "DNA":
            raise ValueError(
                f"rows[{row_index}] AA window requires DNA objects; "
                f"sequence_metadata[{sequence_id!r}] declares {molecule!r}"
            )
        unit = metadata.get("unit")
        if unit is not None and unit not in {"nt", "nucleotide", "raw sequence symbols"}:
            raise ValueError(
                f"rows[{row_index}] AA window requires nucleotide units; "
                f"sequence_metadata[{sequence_id!r}].unit is {unit!r}"
            )

    if translation_context is not None:
        if translation_context.get("frame", 0) != 0:
            raise ValueError(
                f"rows[{row_index}] AA window requires translation_context.frame to be 0"
            )
        if translation_context.get("molecule") not in {None, "DNA"}:
            raise ValueError(
                f"rows[{row_index}] AA window requires translation_context.molecule to be 'DNA'"
            )
        if translation_context.get("strand") in {"reverse", "unknown"}:
            raise ValueError(
                f"rows[{row_index}] AA window requires forward/as-provided translation strand"
            )
        if translation_context.get("coordinate_system") not in {
            None,
            "0-based-half-open",
            "0-based half-open",
        }:
            raise ValueError(
                f"rows[{row_index}] AA window requires a 0-based half-open translation coordinate system"
            )
    bounds = row["aa_window_0based_halfopen"]
    if (
        not isinstance(bounds, list)
        or len(bounds) != 2
        or any(isinstance(value, bool) or not isinstance(value, int) for value in bounds)
        or bounds[0] < 0
        or bounds[0] > bounds[1]
    ):
        raise ValueError(
            f"rows[{row_index}].aa_window_0based_halfopen must be a "
            "non-negative integer [start, end)"
        )
    if any(set(sequence) - set("ACGT") for sequence in (query, reference)):
        raise ValueError(
            f"rows[{row_index}] AA window requires uppercase ungapped DNA for "
            f"query {query_id!r} and reference {reference_id!r}"
        )
    if len(query) % 3 or len(reference) % 3:
        raise ValueError(
            f"rows[{row_index}] AA window requires frame-zero DNA lengths divisible by three"
        )
    nt_window = [3 * bounds[0], 3 * bounds[1]]
    if nt_window[1] > len(query) or nt_window[1] > len(reference):
        raise ValueError(
            f"rows[{row_index}] AA window {bounds} projects to nucleotide range "
            f"{nt_window}, outside the query/reference sequences"
        )
    return {
        "aa_window_0based_halfopen": list(bounds),
        "nt_window_0based_halfopen": nt_window,
        "aa_unit": "aa",
        "nt_unit": "nt",
        "coordinate_system": "0-based half-open",
        "frame": 0,
        "strand": "as-provided; no reverse-complement conversion",
        "projection": "frame-zero DNA codons: [aa_start, aa_end) -> [3*start, 3*end)",
    }


def _requested_relations(
    sequences: dict[str, str],
    rows: list[dict[str, Any]],
    metadata_by_id: dict[str, dict[str, Any]] | None = None,
    translation_context: dict[str, Any] | None = None,
    sequence_identity: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Resolve optional directional query/reference rows without guessing."""

    relations: list[dict[str, Any]] = []
    for row_index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"rows[{row_index}] must be a JSON object")

        has_query = "query_id" in row or "seed_id" in row
        has_reference = "reference_id" in row
        if not has_query and not has_reference:
            raise UnsupportedCombinationError(
                f"rows[{row_index}] must declare query_id (or seed_id) and reference_id; "
                "a row without a directional relation is unsupported"
            )
        if not has_query or not has_reference:
            raise ValueError(
                f"rows[{row_index}] must provide both query_id (or seed_id) and reference_id"
            )
        if "query_id" in row and "seed_id" in row and row["query_id"] != row["seed_id"]:
            raise ValueError(f"rows[{row_index}] query_id and seed_id disagree")

        query_id = row.get("query_id", row.get("seed_id"))
        reference_id = row["reference_id"]
        if not isinstance(query_id, str) or not query_id:
            raise ValueError(f"rows[{row_index}] query_id must be a non-empty string")
        if not isinstance(reference_id, str) or not reference_id:
            raise ValueError(f"rows[{row_index}] reference_id must be a non-empty string")
        if query_id not in sequences:
            raise ValueError(f"rows[{row_index}] unknown query/reference object: {query_id!r}")
        if reference_id not in sequences:
            raise ValueError(f"rows[{row_index}] unknown query/reference object: {reference_id!r}")
        if "round_id" in row and row["round_id"] is not None:
            if not isinstance(row["round_id"], str) or not row["round_id"]:
                raise ValueError(f"rows[{row_index}] round_id must be a non-empty string when supplied")

        query = sequences[query_id]
        reference = sequences[reference_id]
        legacy, audit = _pairwise_comparison(query_id, reference_id, query, reference)
        alignment = _global_alignment(query_id, reference_id, query, reference)
        query_provenance = (sequence_identity or {}).get(query_id, {})
        reference_provenance = (sequence_identity or {}).get(reference_id, {})
        relation: dict[str, Any] = {
            "row_index": row_index,
            "row_sha256": digest(row),
            "status": "ok",
            "round_id": row.get("round_id"),
            "query_id": query_id,
            "reference_id": reference_id,
            "query_sequence_sha256": audit["left_sequence_sha256"],
            "reference_sequence_sha256": audit["right_sequence_sha256"],
            "query_object_id": query_provenance.get("object_id"),
            "reference_object_id": reference_provenance.get("object_id"),
            "query_source_record_id": query_provenance.get("source_record_id"),
            "reference_source_record_id": reference_provenance.get("source_record_id"),
            "query_provenance": query_provenance,
            "reference_provenance": reference_provenance,
            "query_length": audit["left_length"],
            "reference_length": audit["right_length"],
            "compared_range_query": audit["compared_ranges"]["left"],
            "compared_range_reference": audit["compared_ranges"]["right"],
            "matches": legacy["matches"],
            "identity": legacy["identity"],
            "identity_denominator": "compared_length",
            "identity_status": audit["identity_status"],
            "difference_positions_query": audit["difference_positions"],
            "difference_positions_reference": audit["difference_positions"],
            "coordinate_system": audit["coordinate_system"],
            "orientation": "directional query/reference as declared in this row",
            "alignment": alignment,
        }
        window = _validate_aa_window(
            row_index,
            row,
            query_id,
            reference_id,
            query,
            reference,
            (metadata_by_id or {}).get(query_id),
            (metadata_by_id or {}).get(reference_id),
            translation_context,
        )
        if window is not None:
            relation["requested_coordinate"] = window
        relations.append(relation)
    return relations


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
    sequences = _validate_pairwise_sequences(fixture.get("sequences"))
    metadata_by_id, translation_context, metadata_normalizations = _validate_input_metadata(
        fixture, sequences
    )
    sequence_identity = {
        sequence_id: {
            "sequence_id": sequence_id,
            "sequence_sha256": sha256_bytes(sequence.encode("utf-8")),
            "length": len(sequence),
            "unit": "raw sequence symbols",
            "object_id": metadata_by_id.get(sequence_id, {}).get("object_id"),
            "object_id_status": (
                "declared" if "object_id" in metadata_by_id.get(sequence_id, {}) else "unknown_not_supplied"
            ),
            "source_record_id": metadata_by_id.get(sequence_id, {}).get("source_record_id"),
            "source_record_id_status": (
                "declared"
                if "source_record_id" in metadata_by_id.get(sequence_id, {})
                else "unknown_not_supplied"
            ),
            "declared_content_sha256": metadata_by_id.get(sequence_id, {}).get("content_sha256"),
            "declared_sequence_sha256": metadata_by_id.get(sequence_id, {}).get("sequence_sha256"),
            "metadata": metadata_by_id.get(sequence_id, {}),
        }
        for sequence_id, sequence in sequences.items()
    }

    aligned = []
    pair_metadata = []
    keys = list(sequences)
    pair_index = 0
    for index, left in enumerate(keys):
        for right in keys[index + 1 :]:
            a, b = sequences[left], sequences[right]
            legacy, audit = _pairwise_comparison(left, right, a, b)
            alignment = _global_alignment(left, right, a, b)
            left_provenance = sequence_identity[left]
            right_provenance = sequence_identity[right]
            audit.update(
                {
                    "pair_index": pair_index,
                    "left_object_id": left_provenance["object_id"],
                    "right_object_id": right_provenance["object_id"],
                    "left_source_record_id": left_provenance["source_record_id"],
                    "right_source_record_id": right_provenance["source_record_id"],
                    "left_provenance": left_provenance,
                    "right_provenance": right_provenance,
                    "legacy_prefix": {
                        "length": legacy["length"],
                        "matches": legacy["matches"],
                        "identity": legacy["identity"],
                        "identity_denominator": "compared_length",
                    },
                    "alignment": alignment,
                    "alignment_identity": alignment["identity"],
                    "alignment_identity_denominator": "alignment_columns",
                }
            )
            aligned.append(legacy)
            pair_metadata.append(audit)
            pair_index += 1

    return {
        "pairwise_alignments": aligned,
        "compatibility": {
            "schema_version": 1,
            "method": "ungapped_prefix_comparison",
            "sequence_identity": sequence_identity,
            "coordinate_system": "0-based half-open raw sequence-symbol offsets",
            "sequence_unit": "raw sequence symbols; DNA bases when input is DNA",
            "strand": "as-provided; no reverse-complement conversion",
            "frame": "raw sequence comparison; no translation frame applied",
            "gap_policy": (
                "input '-' gaps are rejected; the additive alignment view may infer gap columns "
                "with explicit insertion and zero-width deletion coordinates"
            ),
            "identity_definition": "matches / compared_length",
            "unequal_length_policy": (
                "legacy prefix comparison uses compared_length=min(left_length,right_length); "
                "the additive global alignment covers both complete sequences and exposes "
                "uncompared legacy suffixes plus gap events"
            ),
            "alignment_method": "global_needleman_wunsch",
            "alignment_identity_definition": "alignment matches / alignment_columns",
            "alignment_identity_note": (
                "alignment_identity is additive and gap-aware; the legacy identity field remains "
                "the V0 prefix value"
            ),
            "input_metadata": {
                "sequence_metadata": metadata_by_id,
                "translation_context": translation_context,
                "normalizations": metadata_normalizations,
                "interpretation": (
                    "Metadata is provenance for object joins. Pairwise comparison uses the raw "
                    "sequence strings in JSON insertion order and performs no frame, unit, strand, "
                    "or object-id conversion."
                ),
            },
            "pair_metadata": pair_metadata,
            "requested_relations": _requested_relations(
                sequences,
                rows,
                metadata_by_id,
                translation_context,
                sequence_identity,
            ),
        },
    }


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


TOOL_FUNCTIONS = {'pairwise-alignment': tool_pairwise_alignment}


def _atomic_publish(path: Path, raw: bytes) -> str:
    """Publish bytes with a same-directory temporary file and an atomic replace."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        try:
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory_descriptor = None
        if directory_descriptor is not None:
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        return sha256_bytes(raw)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _append_event(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    with path.open("ab") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def _error_document(tool_id: str | None, exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, json.JSONDecodeError):
        code = "invalid_json"
    elif isinstance(exc, FileNotFoundError):
        code = "input_not_found"
    elif isinstance(exc, OSError):
        code = "io_error"
    elif isinstance(exc, UnsupportedCombinationError):
        code = exc.code
    elif isinstance(exc, (ToolInputError, TypeError, ValueError, KeyError)):
        code = getattr(exc, "code", "invalid_input")
    else:
        code = "internal_error"
    return {
        "status": "error",
        "tool_id": tool_id,
        "error": {
            "code": code,
            "message": str(exc),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = JsonArgumentParser()
    parser.add_argument("--tool", required=True, choices=sorted(TOOL_FUNCTIONS))
    parser.add_argument("--input", required=True)
    parser.add_argument("--rows")
    parser.add_argument("--output", required=True)
    parser.add_argument("--run-root", required=True)
    try:
        args = parser.parse_args(argv)
    except ToolInputError as exc:
        print(json.dumps(_error_document(None, exc), ensure_ascii=False), file=sys.stderr)
        return 2
    started = time.time()
    try:
        input_bytes = Path(args.input).read_bytes()
        fixture = load_fixture(args.input, input_bytes)
        rows_bytes = Path(args.rows).read_bytes() if args.rows else None
        rows = rows_from_bytes(rows_bytes, args.rows) if rows_bytes is not None else []
        payload = TOOL_FUNCTIONS[args.tool](fixture, rows)
        input_sha256 = sha256_bytes(input_bytes)
        rows_sha256 = sha256_bytes(rows_bytes) if rows_bytes is not None else None
        if args.tool == "pairwise-alignment":
            compatibility = payload["compatibility"]
            compatibility["artifact_provenance"] = {
                "input_sha256": input_sha256,
                "rows_sha256": rows_sha256,
                "rows_supplied": args.rows is not None,
                "input_sequence_order": list(fixture["sequences"]),
                "row_count": len(rows),
                "coordinate_join_keys": [
                    "input_sha256",
                    "rows_sha256",
                    "sequence_id",
                    "sequence_sha256",
                    "row_index",
                    "row_sha256",
                ],
            }
        result = {
            "tool_id": args.tool,
            "input_sha256": input_sha256,
            "rows_sha256": rows_sha256,
            "payload": payload,
        }

        output_path = Path(args.output)
        run_root = Path(args.run_root)
        output_bytes = (
            json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        ).encode("utf-8")
        output_sha256 = _atomic_publish(output_path, output_bytes)
        event = {
            "tool_id": args.tool,
            "input": args.input,
            "rows": args.rows,
            "output": args.output,
            "input_sha256": result["input_sha256"],
            "rows_sha256": result["rows_sha256"],
            "output_sha256": output_sha256,
            "started_epoch": started,
            "finished_epoch": time.time(),
            "exit_code": 0,
        }
        _append_event(
            run_root / "tool_calls.jsonl",
            event,
        )
        print(json.dumps({"status": "ok", "tool_id": args.tool, "output": args.output}, ensure_ascii=False))
        return 0
    except (OSError, TypeError, ValueError, KeyError) as exc:
        print(json.dumps(_error_document(args.tool, exc), ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
