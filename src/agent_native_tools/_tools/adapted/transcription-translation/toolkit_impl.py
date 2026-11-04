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


TRANSLATION_COMPATIBILITY_SCHEMA = "transcription-translation-compatibility"
TRANSLATION_COMPATIBILITY_VERSION = 1
DNA_ALPHABET = "ACGT"
CODON_LENGTH_NT = 3
STANDARD_GENETIC_CODE_ID = "NCBI-1-standard"
COORDINATE_SYSTEM = "0-based-half-open"
STOP_POLICY = "retain"
SHA256_HEX = re.compile(r"^[0-9a-fA-F]{64}$")
SUPPORTED_FIXTURE_FIELDS = frozenset(
    {"sequences", "aa_windows", "translation_context", "sequence_metadata"}
)


class CliUsageError(ValueError):
    """An invalid command-line invocation that should be reported as JSON."""


def _is_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _normalize_zero(value: Any, path: str) -> int:
    """Accept only an unambiguous frame-zero serialization."""

    if _is_integer(value) and value == 0:
        return 0
    if isinstance(value, str) and value == "0":
        return 0
    raise ValueError(f"{path} must be frame 0 (integer 0 or string '0'); got {value!r}")


def _normalize_alias(
    mapping: dict[str, Any],
    keys: tuple[str, ...],
    path: str,
    normalizer,
) -> Any:
    """Normalize compatible aliases and reject conflicting declarations."""

    values = []
    for key in keys:
        if key in mapping:
            values.append((key, normalizer(mapping[key], f"{path}.{key}")))
    if not values:
        return None
    first = values[0][1]
    if any(value != first for _key, value in values[1:]):
        rendered = ", ".join(f"{key}={value!r}" for key, value in values)
        raise ValueError(f"Conflicting {path} aliases: {rendered}")
    return first


def _normalize_fixed(value: Any, expected: Any, path: str) -> Any:
    if value != expected:
        raise ValueError(f"Unsupported {path}: expected {expected!r}, got {value!r}")
    return expected


def _normalize_genetic_code(value: Any, path: str) -> str:
    aliases = {
        "standard": "standard",
        "NCBI-1": "standard",
        "NCBI-1-standard": "standard",
        "NCBI-standard-1": "standard",
    }
    if not isinstance(value, str) or value not in aliases:
        raise ValueError(
            f"Unsupported {path}: expected standard genetic code (standard, "
            f"NCBI-1, NCBI-1-standard, or NCBI-standard-1); got {value!r}"
        )
    return aliases[value]


def _normalize_partial_codon_policy(value: Any, path: str) -> str:
    if value in ("reject", "complete_codons_only"):
        return "reject"
    raise ValueError(
        f"Unsupported {path}: expected 'reject' or the safe alias "
        f"'complete_codons_only'; got {value!r}. Partial codons are never silently truncated."
    )


def _normalize_stop_policy(value: Any, path: str) -> str:
    if value == STOP_POLICY:
        return STOP_POLICY
    raise ValueError(
        f"Unsupported {path}: expected {STOP_POLICY!r}; stop symbols are retained "
        f"and classified rather than truncated; got {value!r}"
    )


def _normalize_hash(value: Any, path: str) -> str:
    if not isinstance(value, str) or SHA256_HEX.fullmatch(value) is None:
        raise ValueError(f"{path} must be a 64-character SHA-256 hexadecimal string")
    return value.lower()


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


def _validate_translation_context(fixture: dict[str, Any]) -> tuple[str, str]:
    """Validate optional producer metadata without changing the V0 input shape.

    V0 fixtures do not contain ``translation_context``.  In that case the
    frozen V0 assumptions are applied and written explicitly to the output.
    When a producer supplies context, unsupported values fail loudly instead
    of being silently coerced into frame zero or the standard code.
    """

    if "translation_context" not in fixture:
        return "implicit-v0-defaults", STOP_POLICY
    context = fixture["translation_context"]
    if not isinstance(context, dict):
        raise ValueError("translation_context must be a JSON object")

    supported_fields = {
        "molecule",
        "unit",
        "strand",
        "frame",
        "genetic_code",
        "genetic_code_id",
        "coordinate_system",
        "partial_codon_policy",
        "partial_codons",
        "stop_policy",
    }
    unknown_fields = sorted(set(context) - supported_fields)
    if unknown_fields:
        raise ValueError(
            "Unsupported translation_context fields: " + ", ".join(unknown_fields)
        )

    if "molecule" in context:
        _normalize_fixed(context["molecule"], "DNA", "translation_context.molecule")
    if "unit" in context:
        _normalize_fixed(context["unit"], "nt", "translation_context.unit")
    if "strand" in context:
        _normalize_fixed(context["strand"], "forward", "translation_context.strand")
    if "frame" in context:
        _normalize_zero(context["frame"], "translation_context.frame")
    if "coordinate_system" in context:
        _normalize_fixed(
            context["coordinate_system"],
            COORDINATE_SYSTEM,
            "translation_context.coordinate_system",
        )
    _normalize_alias(
        context,
        ("genetic_code", "genetic_code_id"),
        "translation_context.genetic_code",
        _normalize_genetic_code,
    )
    _normalize_alias(
        context,
        ("partial_codon_policy", "partial_codons"),
        "translation_context.partial_codon_policy",
        _normalize_partial_codon_policy,
    )
    stop_policy = STOP_POLICY
    if "stop_policy" in context:
        stop_policy = _normalize_stop_policy(
            context["stop_policy"], "translation_context.stop_policy"
        )
    return "explicit", stop_policy


def _validate_sequence_metadata(
    fixture: dict[str, Any], sequences: dict[str, str]
) -> dict[str, dict[str, Any]]:
    """Validate optional source-object metadata and normalize safe aliases."""

    if "sequence_metadata" not in fixture:
        return {}
    raw_metadata = fixture["sequence_metadata"]
    if not isinstance(raw_metadata, dict):
        raise ValueError("sequence_metadata must be a JSON object when supplied")

    allowed_fields = {
        "molecule",
        "molecule_type",
        "unit",
        "strand",
        "frame",
        "coordinate_system",
        "coordinate_range",
        "range",
        "object_id",
        "source_record_id",
        "content_sha256",
        "sequence_sha256",
    }
    normalized: dict[str, dict[str, Any]] = {}
    for sequence_id, metadata in raw_metadata.items():
        path = f"sequence_metadata.{sequence_id}"
        if sequence_id not in sequences:
            raise ValueError(f"{path} names unknown sequence {sequence_id!r}")
        if not isinstance(metadata, dict):
            raise ValueError(f"{path} must be a JSON object")
        unknown_fields = sorted(set(metadata) - allowed_fields)
        if unknown_fields:
            raise ValueError(
                f"Unsupported {path} fields: " + ", ".join(unknown_fields)
            )

        _normalize_alias(
            metadata,
            ("molecule", "molecule_type"),
            path + ".molecule",
            lambda value, value_path: _normalize_fixed(value, "DNA", value_path),
        )
        if "unit" in metadata:
            _normalize_fixed(metadata["unit"], "nt", f"{path}.unit")
        if "strand" in metadata:
            _normalize_fixed(metadata["strand"], "forward", f"{path}.strand")
        if "frame" in metadata:
            _normalize_zero(metadata["frame"], f"{path}.frame")
        _normalize_alias(
            metadata,
            ("coordinate_system", "coordinate_range", "range"),
            path + ".coordinate_system",
            lambda value, value_path: _normalize_fixed(
                value, COORDINATE_SYSTEM, value_path
            ),
        )

        normalized_record: dict[str, Any] = {
            "molecule": "DNA",
            "unit": "nt",
            "strand": "forward",
            "frame": 0,
            "coordinate_system": COORDINATE_SYSTEM,
        }
        for key in ("object_id", "source_record_id"):
            if key in metadata:
                value = metadata[key]
                if not isinstance(value, str) or not value:
                    raise ValueError(f"{path}.{key} must be a non-empty string")
                normalized_record[key] = value

        declared_hashes = []
        for key in ("content_sha256", "sequence_sha256"):
            if key in metadata:
                declared_hashes.append(
                    (key, _normalize_hash(metadata[key], f"{path}.{key}"))
                )
        if declared_hashes:
            first_hash = declared_hashes[0][1]
            if any(value != first_hash for _key, value in declared_hashes[1:]):
                rendered = ", ".join(f"{key}={value!r}" for key, value in declared_hashes)
                raise ValueError(f"Conflicting {path} hash declarations: {rendered}")
            actual_hash = sha256_bytes(sequences[sequence_id].encode("ascii"))
            if first_hash != actual_hash:
                raise ValueError(
                    f"{path}.content_sha256 does not match the exact DNA for "
                    f"sequence {sequence_id!r}"
                )
            normalized_record["content_sha256"] = first_hash
        normalized[sequence_id] = normalized_record
    return normalized


def _validate_transcription_input(
    fixture: dict[str, Any],
) -> tuple[dict[str, str], dict[str, list[int]], str, dict[str, dict[str, Any]]]:
    """Validate the part of the public fixture consumed by this tool."""

    if not isinstance(fixture, dict):
        raise ValueError("The tool input must be a JSON object")
    if "sequences" not in fixture:
        raise ValueError("The tool input must contain sequences")
    sequences = fixture["sequences"]
    if not isinstance(sequences, dict):
        raise ValueError("sequences must be a JSON object mapping IDs to DNA strings")

    normalized_sequences: dict[str, str] = {}
    for sequence_id, dna in sequences.items():
        if not isinstance(sequence_id, str) or not sequence_id:
            raise ValueError("sequence IDs must be non-empty strings")
        if not isinstance(dna, str):
            raise ValueError(f"DNA for sequence {sequence_id!r} must be a string")
        if len(dna) % CODON_LENGTH_NT:
            raise ValueError(
                f"DNA length for sequence {sequence_id!r} is not divisible by three: {len(dna)}"
            )
        invalid = sorted(set(dna) - set(DNA_ALPHABET))
        if invalid:
            raise ValueError(
                f"DNA alphabet for sequence {sequence_id!r} contains unsupported symbols: "
                f"{invalid}; only {DNA_ALPHABET} are accepted"
            )
        normalized_sequences[sequence_id] = dna

    # AA windows are optional: translation remains useful when a caller only
    # needs complete protein sequences. The campaign rows can supply legacy
    # windows, which are normalized by the CLI caller when present.
    raw_windows = fixture.get("aa_windows", {})
    if not isinstance(raw_windows, dict):
        raise ValueError("aa_windows must be a JSON object of window IDs to [lo, hi]")

    windows: dict[str, list[int]] = {}
    for window_id, bounds in raw_windows.items():
        if not isinstance(window_id, str) or not window_id:
            raise ValueError("AA window IDs must be non-empty strings")
        if not isinstance(bounds, (list, tuple)) or len(bounds) != 2:
            raise ValueError(
                f"AA window {window_id!r} must be a two-element [lo, hi] interval"
            )
        lo, hi = bounds
        if not _is_integer(lo) or not _is_integer(hi):
            raise ValueError(f"AA window {window_id!r} bounds must be integers")
        if lo < 0 or hi < lo:
            raise ValueError(
                f"AA window {window_id!r} must satisfy 0 <= lo <= hi; got {bounds!r}"
            )
        windows[window_id] = [lo, hi]

    context_source, stop_policy = _validate_translation_context(fixture)
    metadata = _validate_sequence_metadata(fixture, normalized_sequences)
    return normalized_sequences, windows, context_source, metadata, stop_policy


def _validate_window_bounds(
    windows: dict[str, list[int]], protein_lengths: dict[str, int]
) -> None:
    """Reject a window that would otherwise be silently clipped by slicing."""

    if not protein_lengths:
        return
    min_length = min(protein_lengths.values())
    for window_id, (_lo, hi) in windows.items():
        if hi > min_length:
            raise ValueError(
                f"AA window {window_id!r} ends at {hi}, outside the shared protein "
                f"range [0, {min_length})"
            )


def _stop_semantics(dna: str, protein: str) -> dict[str, Any]:
    stop_codons = []
    for aa_index, amino_acid in enumerate(protein):
        if amino_acid != "*":
            continue
        nt_window = [CODON_LENGTH_NT * aa_index, CODON_LENGTH_NT * (aa_index + 1)]
        stop_codons.append(
            {
                "codon": dna[nt_window[0] : nt_window[1]],
                "aa_index": aa_index,
                "nt_window": nt_window,
                "classification": (
                    "terminal" if aa_index == len(protein) - 1 else "internal"
                ),
            }
        )

    internal = [x for x in stop_codons if x["classification"] == "internal"]
    terminal = next(
        (x for x in stop_codons if x["classification"] == "terminal"), None
    )
    return {
        "stop_symbol": "*",
        "stop_codon_count": len(stop_codons),
        "terminal_stop_present": terminal is not None,
        "terminal_stop_aa_index": terminal["aa_index"] if terminal else None,
        "terminal_stop_nt_window": terminal["nt_window"] if terminal else None,
        "internal_stop_count": len(internal),
        "internal_stop_aa_indices": [x["aa_index"] for x in internal],
        "internal_stop_nt_windows": [x["nt_window"] for x in internal],
        "stop_codons": stop_codons,
    }


def _compatibility_contract(
    sequences: dict[str, str],
    proteins: dict[str, str],
    windows: dict[str, list[int]],
    context_source: str,
    metadata: dict[str, dict[str, Any]],
    stop_policy: str = STOP_POLICY,
) -> dict[str, Any]:
    sequence_records = {}
    seen_object_ids: dict[str, str] = {}
    for sequence_id, dna in sequences.items():
        protein = proteins[sequence_id]
        stop_semantics = _stop_semantics(dna, protein)
        source_metadata = metadata.get(sequence_id, {})
        dna_sha256 = sha256_bytes(dna.encode("ascii"))
        object_id = source_metadata.get("object_id", f"dna:{sequence_id}:{dna_sha256}")
        previous_sequence_id = seen_object_ids.get(object_id)
        if previous_sequence_id is not None:
            raise ValueError(
                f"sequence object_id {object_id!r} is used by both "
                f"{previous_sequence_id!r} and {sequence_id!r}"
            )
        seen_object_ids[object_id] = sequence_id
        sequence_records[sequence_id] = {
            "sequence_id": sequence_id,
            "source_path": ["sequences", sequence_id],
            "dna_sha256": dna_sha256,
            "dna_length_nt": len(dna),
            "dna_coordinate_range": [0, len(dna)],
            "protein_sha256": sha256_bytes(protein.encode("ascii")),
            "protein_length_aa": len(protein),
            "protein_coordinate_range": [0, len(protein)],
            "object_id": object_id,
            "object_id_status": (
                "declared" if "object_id" in source_metadata else "derived"
            ),
            "source_record_id": source_metadata.get("source_record_id"),
            "source_record_id_status": (
                "declared" if "source_record_id" in source_metadata else "unknown_not_supplied"
            ),
            "molecule": source_metadata.get("molecule", "DNA"),
            "unit": source_metadata.get("unit", "nt"),
            "coordinate_system": source_metadata.get(
                "coordinate_system", COORDINATE_SYSTEM
            ),
            "translation": {
                "molecule": source_metadata.get("molecule", "DNA"),
                "strand": source_metadata.get("strand", "forward"),
                "frame": source_metadata.get("frame", 0),
                "frame_nt_offset": 0,
                "genetic_code": STANDARD_GENETIC_CODE_ID,
                "codon_length_nt": CODON_LENGTH_NT,
                "partial_codon_policy": "reject",
                "unknown_symbol_policy": "reject",
            },
            "stop_semantics": stop_semantics,
            "legacy_internal_stop_count": protein[:-1].count("*"),
        }

    window_verification = []
    for sequence_id, dna in sequences.items():
        protein = proteins[sequence_id]
        source_record = sequence_records[sequence_id]
        stop_records = source_record["stop_semantics"]["stop_codons"]
        for window_id, (lo, hi) in windows.items():
            nt_lo, nt_hi = CODON_LENGTH_NT * lo, CODON_LENGTH_NT * hi
            window_stops = [x for x in stop_records if lo <= x["aa_index"] < hi]
            matches = translate(dna[nt_lo:nt_hi]) == protein[lo:hi]
            window_verification.append(
                {
                    "sequence_id": sequence_id,
                    "window_id": window_id,
                    "source_dna_sha256": source_record["dna_sha256"],
                    "source_protein_sha256": source_record["protein_sha256"],
                    "aa_window": [lo, hi],
                    "nt_window": [nt_lo, nt_hi],
                    "aa_unit": "aa",
                    "nt_unit": "nt",
                    "aa_coordinate_system": COORDINATE_SYSTEM,
                    "nt_coordinate_system": COORDINATE_SYSTEM,
                    "frame": 0,
                    "strand": "forward",
                    "genetic_code": STANDARD_GENETIC_CODE_ID,
                    "translated_window_matches": matches,
                    "stop_count_in_window": len(window_stops),
                    "stop_classifications_in_window": [
                        x["classification"] for x in window_stops
                    ],
                    "status": "verified" if matches else "mismatch",
                }
            )

    return {
        "schema": TRANSLATION_COMPATIBILITY_SCHEMA,
        "version": TRANSLATION_COMPATIBILITY_VERSION,
        "context_source": context_source,
        "stop_policy": stop_policy,
        "source": {
            "object": "fixture.sequences",
            "identity_rule": "sequence_id plus dna_sha256",
            "dna_encoding_for_hash": "ASCII bytes of the exact DNA string",
            "sequence_scope": "each sequence_id is an independent source object",
        },
        "translation": {
            "input_molecule": "DNA",
            "output_molecule": "protein",
            "strand": "forward",
            "frame": 0,
            "frame_nt_offset": 0,
            "genetic_code": STANDARD_GENETIC_CODE_ID,
            "codon_length_nt": CODON_LENGTH_NT,
            "partial_codon_policy": "reject",
            "unknown_symbol_policy": "reject",
        },
        "coordinates": {
            "aa": COORDINATE_SYSTEM,
            "nt": COORDINATE_SYSTEM,
            "mapping": "nt_start = 3 * aa_start; nt_end = 3 * aa_end",
            "window_scope": "every aa_windows entry is checked against every sequence",
        },
        "stop_semantics": {
            "stop_symbol": "*",
            "terminal_definition": "the final translated codon only",
            "internal_definition": "any stop codon before the final translated codon",
            "no_terminal_stop_is_valid": True,
            "legacy_field_note": (
                "payload.internal_stop_counts preserves the V0 field; use "
                "sequence_records[*].stop_semantics.internal_stop_count for the "
                "explicit V1 definition"
            ),
        },
        "sequence_records": sequence_records,
        "window_verification": window_verification,
    }


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
        raise ValueError("Rows input must be a JSON list")
    return rows


def tool_sequence_properties(fixture: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    sequences = fixture["sequences"]
    return {
        "sequence_lengths": {key: len(value) for key, value in sequences.items()},
        "dna_alphabet_ok": {key: set(value) <= set("ACGT") for key, value in sequences.items()},
        "gc_fraction": {key: gc_fraction(value) for key, value in sequences.items()},
        "base_counts": {key: dict(collections.Counter(value)) for key, value in sequences.items()},
    }


def _derive_windows_from_rows(rows: list[dict[str, Any]]) -> dict[str, list[int]]:
    derived: dict[str, list[int]] = {}
    for index, raw_row in enumerate(rows):
        if not isinstance(raw_row, dict):
            raise ValueError(f"rows[{index}] must be a JSON object")
        present_fields = [
            field
            for field in ("aa_window_0based_halfopen", "aa_window", "window")
            if field in raw_row
        ]
        if not present_fields:
            continue
        normalized_bounds = []
        for field in present_fields:
            bounds = raw_row[field]
            if not isinstance(bounds, (list, tuple)) or len(bounds) != 2:
                raise ValueError(
                    f"rows[{index}].{field} must be a two-element [lo, hi] interval"
                )
            lo, hi = bounds
            if not _is_integer(lo) or not _is_integer(hi):
                raise ValueError(f"rows[{index}].{field} bounds must be integers")
            if lo < 0 or hi < lo:
                raise ValueError(
                    f"rows[{index}].{field} must satisfy 0 <= lo <= hi; got {bounds!r}"
                )
            normalized_bounds.append((field, [lo, hi]))
        bounds = normalized_bounds[0][1]
        if any(candidate != bounds for _field, candidate in normalized_bounds[1:]):
            rendered = ", ".join(f"{field}={value!r}" for field, value in normalized_bounds)
            raise ValueError(f"rows[{index}] contains conflicting window aliases: {rendered}")

        name = raw_row.get("round_id")
        if name in (None, ""):
            name = raw_row.get("library_id")
        if name in (None, ""):
            name = f"row_{index}"
        if not isinstance(name, str) or not name:
            raise ValueError(f"rows[{index}] window ID must be a non-empty string")
        if name in derived and derived[name] != bounds:
            raise ValueError(
                f"rows[{index}] redefines window {name!r} with different bounds"
            )
        derived[name] = bounds
    return derived


def tool_transcription_translation(fixture: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    if isinstance(fixture, dict) and "aa_windows" not in fixture:
        derived = _derive_windows_from_rows(rows)
        if derived:
            fixture = dict(fixture)
            fixture["aa_windows"] = derived
    sequences, windows, context_source, metadata, stop_policy = _validate_transcription_input(fixture)
    proteins = {key: translate(value) for key, value in sequences.items()}
    protein_lengths = {key: len(value) for key, value in proteins.items()}
    _validate_window_bounds(windows, protein_lengths)
    checks = []
    for sequence_id, dna in sequences.items():
        protein = proteins[sequence_id]
        for window_id, bounds in windows.items():
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
        "protein_lengths": protein_lengths,
        "internal_stop_counts": {key: value[:-1].count("*") for key, value in proteins.items()},
        "window_translation_checks": checks,
        "compatibility": _compatibility_contract(
            sequences, proteins, windows, context_source, metadata, stop_policy
        ),
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


TOOL_FUNCTIONS = {'transcription-translation': tool_transcription_translation}


def _write_json_atomic(path: str | Path, value: dict[str, Any]) -> bytes:
    """Write a complete, parseable artifact, creating nested parents safely."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    serialized = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode(
        "utf-8"
    )
    fd, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent)
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        written = destination.read_bytes()
        json.loads(written)
        return written
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _run(args: argparse.Namespace) -> int:
    started = time.time()
    input_bytes = Path(args.input).read_bytes()
    fixture = json.loads(input_bytes)
    rows = rows_from_file(args.rows)
    payload = TOOL_FUNCTIONS[args.tool](fixture, rows)
    result = {"tool_id": args.tool, "input_sha256": sha256_bytes(input_bytes), "rows_sha256": sha256_bytes(Path(args.rows).read_bytes()) if args.rows else None, "payload": payload}
    output_bytes = _write_json_atomic(args.output, result)
    run_root = Path(args.run_root)
    run_root.mkdir(parents=True, exist_ok=True)
    event = {"tool_id": args.tool, "input": args.input, "rows": args.rows, "output": args.output, "input_sha256": result["input_sha256"], "output_sha256": sha256_bytes(output_bytes), "started_epoch": started, "finished_epoch": time.time(), "exit_code": 0}
    with (run_root / "tool_calls.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    print(json.dumps({"status": "ok", "tool_id": args.tool, "output": args.output}, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tool", required=True, choices=sorted(TOOL_FUNCTIONS))
    parser.add_argument("--input", required=True)
    parser.add_argument("--rows")
    parser.add_argument("--output", required=True)
    parser.add_argument("--run-root", required=True)
    args = parser.parse_args(argv)
    try:
        return _run(args)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        print(
            json.dumps(
                {"status": "error", "tool_id": args.tool, "error": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
