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


DNA_ALPHABET = frozenset("ACGT")
SEQUENCE_PROPERTIES_RESULT_SCHEMA_VERSION = 2
SEQUENCE_QC_SCHEMA_VERSION = 1
FILTER_SEQUENCE_EXPECTED_LENGTH_NT = 501
_SEQUENCE_METADATA_KEYS = frozenset(
    {
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
)
_MOLECULE_TYPES = frozenset({"DNA", "RNA", "protein", "unknown"})
_STRANDS = frozenset({"as_provided", "forward", "reverse", "unknown"})
_FRAME_STRING_VALUES = {"0": 0, "1": 1, "2": 2}
_COORDINATE_SYSTEM = "0-based-half-open"


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
    sequences = payload["sequences"]
    if not isinstance(sequences, dict) or not sequences:
        raise ValueError("The tool input must contain a non-empty sequences object")
    for sequence_id, sequence in sequences.items():
        if not isinstance(sequence_id, str) or not sequence_id.strip():
            raise ValueError("sequence ids must be non-empty strings")
        if not isinstance(sequence, str):
            raise ValueError(f"sequence {sequence_id!r} must be a string")
        if not sequence:
            raise ValueError(f"sequence {sequence_id!r} must be a non-empty string")
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
    sequence_metadata = _sequence_metadata(fixture, sequences)
    legacy_payload = {
        "sequence_lengths": {key: len(value) for key, value in sequences.items()},
        "dna_alphabet_ok": {key: set(value) <= DNA_ALPHABET for key, value in sequences.items()},
        "gc_fraction": {key: gc_fraction(value) for key, value in sequences.items()},
        "base_counts": {key: dict(collections.Counter(value)) for key, value in sequences.items()},
    }
    legacy_payload.update(
        {
            "sequence_qc_schema_version": SEQUENCE_QC_SCHEMA_VERSION,
            "sequence_qc": {
                sequence_id: _sequence_qc(sequence_id, sequence, sequence_metadata[sequence_id])
                for sequence_id, sequence in sequences.items()
            },
        }
    )
    return legacy_payload


def _sequence_metadata(
    fixture: dict[str, Any], sequences: dict[str, str]
) -> dict[str, dict[str, Any]]:
    """Validate optional declarations and retain their provenance.

    The r2 callers used metadata vocabulary shared by the translation and
    alignment tools.  Alias normalization is deliberately narrow: only the
    exact DNA, nucleotide, and 0-based-half-open declarations are accepted for
    the new compatibility fields.  The legacy molecule_type-only declaration
    keeps its existing QC behavior for non-DNA values.
    """

    raw_metadata = fixture.get("sequence_metadata", {})
    if not isinstance(raw_metadata, dict):
        raise ValueError("sequence_metadata must be an object when present")

    metadata: dict[str, dict[str, Any]] = {}
    for sequence_id, raw_record in raw_metadata.items():
        if sequence_id not in sequences:
            raise ValueError(
                f"sequence_metadata references unknown sequence {sequence_id!r}"
            )
        if not isinstance(raw_record, dict):
            raise ValueError(f"sequence_metadata[{sequence_id!r}] must be an object")
        unknown_keys = sorted(set(raw_record) - _SEQUENCE_METADATA_KEYS)
        if unknown_keys:
            raise ValueError(
                f"sequence_metadata[{sequence_id!r}] has unsupported fields: {unknown_keys}"
            )

        metadata_path = f"sequence_metadata[{sequence_id!r}]"
        molecule_type, molecule_normalized, molecule_sources = _normalize_molecule(
            metadata_path, raw_record
        )

        unit = _normalize_fixed_metadata(
            metadata_path, raw_record, "unit", "nt"
        )

        coordinate_system, coordinate_sources = _normalize_coordinate_system(
            metadata_path, raw_record
        )

        strand = raw_record.get("strand")
        if "strand" in raw_record and (
            not isinstance(strand, str) or strand not in _STRANDS
        ):
            raise ValueError(
                f"sequence_metadata[{sequence_id!r}].strand must be one of "
                f"{sorted(_STRANDS)}"
            )

        frame = _normalize_frame(sequence_id, raw_record)

        for identity_key in ("object_id", "source_record_id"):
            identity = raw_record.get(identity_key)
            if identity_key in raw_record and (
                not isinstance(identity, str) or not identity
            ):
                raise ValueError(
                    f"sequence_metadata[{sequence_id!r}].{identity_key} must be a non-empty string"
                )
        expected_hash = sha256_bytes(sequences[sequence_id].encode("utf-8"))
        for hash_key in ("content_sha256", "sequence_sha256"):
            declared_hash = raw_record.get(hash_key)
            if hash_key in raw_record and (
                not isinstance(declared_hash, str) or len(declared_hash) != 64
            ):
                raise ValueError(
                    f"sequence_metadata[{sequence_id!r}].{hash_key} must be a SHA-256 hex digest"
                )
            if hash_key in raw_record and declared_hash != expected_hash:
                raise ValueError(
                    f"sequence_metadata[{sequence_id!r}].{hash_key} does not match the exact sequence"
                )
        normalized_record = dict(raw_record)
        if "frame" in raw_record:
            normalized_record["frame"] = frame
        if molecule_type is not None:
            normalized_record["molecule_type"] = molecule_type

        normalized_metadata: dict[str, Any] = {}
        if molecule_normalized is not None:
            normalized_metadata["molecule"] = molecule_normalized
        elif molecule_type is not None and "molecule_type" in raw_record:
            normalized_metadata["molecule_type"] = molecule_type
        if unit is not None:
            normalized_metadata["unit"] = unit
        if coordinate_system is not None:
            normalized_metadata["coordinate_system"] = coordinate_system
        for key in ("strand", "frame", "object_id", "source_record_id"):
            if key in normalized_record:
                normalized_metadata[key] = normalized_record[key]
        for hash_key in ("content_sha256", "sequence_sha256"):
            if hash_key in normalized_record:
                normalized_metadata[hash_key] = normalized_record[hash_key]

        alias_sources: dict[str, list[str]] = {}
        if molecule_sources:
            alias_sources["molecule"] = molecule_sources
        if "unit" in raw_record:
            alias_sources["unit"] = ["unit"]
        if coordinate_sources:
            alias_sources["coordinate_system"] = coordinate_sources

        metadata[sequence_id] = {
            **normalized_record,
            "_raw_record": dict(raw_record),
            "_normalized_metadata": normalized_metadata,
            "_alias_sources": alias_sources,
            "_validated_fields": sorted(raw_record),
        }

    empty_metadata = {
        "_raw_record": {},
        "_normalized_metadata": {},
        "_alias_sources": {},
        "_validated_fields": [],
    }
    return {
        sequence_id: metadata.get(sequence_id, dict(empty_metadata))
        for sequence_id in sequences
    }


def _validate_non_empty_metadata_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{path} must be a non-empty string")
    return value


def _normalize_molecule(
    metadata_path: str, raw_record: dict[str, Any]
) -> tuple[str | None, str | None, list[str]]:
    """Normalize the molecule aliases without changing legacy type QC."""

    has_molecule = "molecule" in raw_record
    has_molecule_type = "molecule_type" in raw_record
    if not has_molecule and not has_molecule_type:
        return None, None, []

    if has_molecule:
        molecule = _validate_non_empty_metadata_string(
            raw_record["molecule"], f"{metadata_path}.molecule"
        )
        if has_molecule_type:
            molecule_type = _validate_non_empty_metadata_string(
                raw_record["molecule_type"], f"{metadata_path}.molecule_type"
            )
            if molecule != molecule_type:
                raise ValueError(
                    f"Conflicting {metadata_path}.molecule aliases: "
                    f"molecule={molecule!r}, molecule_type={molecule_type!r}"
                )
            if molecule != "DNA":
                raise ValueError(
                    f"Unsupported {metadata_path}.molecule aliases: expected exact "
                    f"'DNA', got {molecule!r}"
                )
        elif molecule != "DNA":
            raise ValueError(
                f"Unsupported {metadata_path}.molecule: expected exact 'DNA', "
                f"got {molecule!r}"
            )
        return "DNA", "DNA", [key for key in ("molecule", "molecule_type") if key in raw_record]

    molecule_type = _validate_non_empty_metadata_string(
        raw_record["molecule_type"], f"{metadata_path}.molecule_type"
    )
    if molecule_type not in _MOLECULE_TYPES:
        raise ValueError(
            f"{metadata_path}.molecule_type must be one of "
            f"{sorted(_MOLECULE_TYPES)}"
        )
    if molecule_type == "DNA":
        return molecule_type, molecule_type, ["molecule_type"]
    return molecule_type, None, ["molecule_type"]


def _normalize_fixed_metadata(
    metadata_path: str,
    raw_record: dict[str, Any],
    key: str,
    expected: str,
) -> str | None:
    """Accept one exact string declaration, including no implicit default."""

    if key not in raw_record:
        return None
    value = raw_record[key]
    if not isinstance(value, str) or value != expected:
        raise ValueError(
            f"Unsupported {metadata_path}.{key}: expected exact string "
            f"{expected!r}, got {value!r}"
        )
    return expected


def _normalize_coordinate_system(
    metadata_path: str, raw_record: dict[str, Any]
) -> tuple[str | None, list[str]]:
    """Normalize coordinate_system, coordinate_range, and range aliases."""

    keys = ("coordinate_system", "coordinate_range", "range")
    present = [key for key in keys if key in raw_record]
    if not present:
        return None, []
    values: list[tuple[str, str]] = []
    for key in present:
        value = raw_record[key]
        if not isinstance(value, str) or not value:
            raise ValueError(
                f"Unsupported {metadata_path}.{key}: expected a non-empty string, "
                f"got {value!r}"
            )
        values.append((key, value))
    first = values[0][1]
    if any(value != first for _key, value in values[1:]):
        rendered = ", ".join(f"{key}={value!r}" for key, value in values)
        raise ValueError(
            f"Conflicting {metadata_path}.coordinate_system aliases: {rendered}"
        )
    if first != _COORDINATE_SYSTEM:
        raise ValueError(
            f"Unsupported {metadata_path}.coordinate_system aliases: expected exact "
            f"string {_COORDINATE_SYSTEM!r}, got {first!r}"
        )
    return first, present


def _normalize_frame(sequence_id: str, raw_record: dict[str, Any]) -> int | None:
    """Normalize only the exact JSON encodings that unambiguously mean a frame."""

    if "frame" not in raw_record:
        return None
    frame = raw_record["frame"]
    if isinstance(frame, bool):
        raise ValueError(
            f"sequence_metadata[{sequence_id!r}].frame must be integer 0, 1, or 2 "
            "or exact string '0', '1', or '2'"
        )
    if isinstance(frame, int) and frame in {0, 1, 2}:
        return frame
    if isinstance(frame, str) and frame in _FRAME_STRING_VALUES:
        return _FRAME_STRING_VALUES[frame]
    raise ValueError(
        f"sequence_metadata[{sequence_id!r}].frame must be integer 0, 1, or 2 "
        "or exact string '0', '1', or '2'"
    )


def _sequence_qc(
    sequence_id: str, sequence: str, metadata: dict[str, Any]
) -> dict[str, Any]:
    """Return additive, provenance-carrying QC facts for downstream tools."""

    observed_symbols = sorted(set(sequence))
    unknown_symbols = sorted(set(sequence) - DNA_ALPHABET)
    unknown_positions = [
        position for position, symbol in enumerate(sequence) if symbol not in DNA_ALPHABET
    ]
    alphabet_ok = not unknown_symbols
    sequence_length = len(sequence)
    molecule_type = metadata.get("molecule_type")
    declared_strand = metadata.get("strand")
    strand = declared_strand if declared_strand is not None else "as_provided"
    declared_frame = metadata.get("frame")
    effective_frame = declared_frame if declared_frame is not None else 0
    dna_compatible = alphabet_ok and molecule_type in {None, "DNA"}

    type_reasons: list[str] = []
    if not alphabet_ok:
        type_reasons.append("unknown_alphabet_symbols")
    if molecule_type is not None and molecule_type != "DNA":
        type_reasons.append("declared_molecule_type_is_not_DNA")
    if molecule_type is None and dna_compatible:
        type_status = "undeclared_DNA_compatible"
    elif molecule_type is None:
        type_status = "undeclared_type_incompatible"
    elif molecule_type == "DNA" and dna_compatible:
        type_status = "declared_DNA_compatible"
    else:
        type_status = "declared_type_incompatible"

    translation_blocking_reasons: list[str] = []
    if not dna_compatible:
        translation_blocking_reasons.append("DNA_compatibility_failed")
    if sequence_length % 3:
        translation_blocking_reasons.append("length_not_divisible_by_three")
    if declared_frame is not None and declared_frame != 0:
        translation_blocking_reasons.append("declared_frame_is_not_zero")
    if strand == "reverse":
        translation_blocking_reasons.append(
            "reverse_strand_requires_explicit_reverse_complement_conversion"
        )
    elif strand == "unknown":
        translation_blocking_reasons.append("strand_is_unknown")

    gc_count = sequence.count("G") + sequence.count("C")
    gc_denominator_unit = "nt" if alphabet_ok else "sequence_characters"
    stop_qc = _stop_qc(sequence, alphabet_ok)

    frame_ok = effective_frame == 0
    strand_ok = strand in {"as_provided", "forward"}
    filter_shape_compatible = (
        sequence_length == FILTER_SEQUENCE_EXPECTED_LENGTH_NT
        and alphabet_ok
        and dna_compatible
        and frame_ok
        and strand_ok
    )
    filter_blocking_reasons: list[str] = []
    if sequence_length != FILTER_SEQUENCE_EXPECTED_LENGTH_NT:
        filter_blocking_reasons.append("length_does_not_match_filter_contract")
    if not alphabet_ok:
        filter_blocking_reasons.append("alphabet_does_not_match_filter_contract")
    if not dna_compatible:
        filter_blocking_reasons.append("DNA_compatibility_failed")
    if not frame_ok:
        filter_blocking_reasons.append("declared_frame_is_not_zero")
    if strand == "reverse":
        filter_blocking_reasons.append(
            "reverse_strand_requires_explicit_reverse_complement_conversion"
        )
    elif strand == "unknown":
        filter_blocking_reasons.append("strand_is_unknown")
    if stop_qc["status"] != "evaluated":
        filter_blocking_reasons.append("internal_stop_status_not_evaluated")
    elif stop_qc["internal_stop_count"]:
        filter_blocking_reasons.append("internal_stop_codon_present")

    return {
        "sequence_id": sequence_id,
        "metadata": {
            "raw": metadata.get("_raw_record", {}),
            "normalized": metadata.get("_normalized_metadata", {}),
            "validated_fields": metadata.get("_validated_fields", []),
            "alias_sources": metadata.get("_alias_sources", {}),
        },
        "identity": {
            "object_id": metadata.get("object_id"),
            "object_id_status": "declared" if metadata.get("object_id") is not None else "unknown_not_supplied",
            "source_record_id": metadata.get("source_record_id"),
            "source_record_id_status": "declared" if metadata.get("source_record_id") is not None else "unknown_not_supplied",
            "sequence_sha256": sha256_bytes(sequence.encode("utf-8")),
            "hash_algorithm": "sha256",
            "encoding": "utf-8",
            "normalization": "none; exact sequence string as provided",
        },
        "dna_type": {
            "expected_molecule_type": "DNA",
            "declared_molecule_type": molecule_type,
            "status": type_status,
            "dna_compatible": dna_compatible,
            "basis": "uppercase ACGT membership plus optional sequence_metadata declaration",
            "reasons": type_reasons,
        },
        "alphabet": {
            "allowed_symbols": "ACGT",
            "case_sensitive": True,
            "observed_symbols": observed_symbols,
            "unknown_symbols": unknown_symbols,
            "unknown_positions_0based": unknown_positions,
            "valid": alphabet_ok,
        },
        "length": {
            "value": sequence_length,
            "unit": "nt" if alphabet_ok else "sequence_characters",
            "coordinate_system": "0-based half-open",
            "range": [0, sequence_length],
            "modulo_three": sequence_length % 3,
        },
        "gc": {
            "fraction": gc_count / sequence_length,
            "gc_count": gc_count,
            "denominator": sequence_length,
            "denominator_unit": gc_denominator_unit,
            "denominator_definition": "all input sequence characters; unknown symbols are included",
            "denominator_includes_unknown_symbols": bool(unknown_symbols),
            "formula": "(count('G') + count('C')) / len(sequence)",
            "status": "defined",
            "usable_as_DNA_GC": alphabet_ok,
        },
        "translation_input": {
            "consumer": "transcription-translation",
            "sequence_sha256": sha256_bytes(sequence.encode("utf-8")),
            "frame": 0,
            "declared_frame": declared_frame,
            "frame_basis": (
                "declared_sequence_metadata"
                if declared_frame is not None
                else "default_legacy_frame_zero"
            ),
            "frame_coordinate_unit": "nt",
            "strand": strand,
            "strand_basis": (
                "declared_sequence_metadata"
                if declared_strand is not None
                else "default_as_provided"
            ),
            "strand_conversion": "none; consumer receives the sequence as provided",
            "coordinates": "0-based half-open nucleotide offsets",
            "ready_for_frame_zero_translation": not translation_blocking_reasons,
            "blocking_reasons": translation_blocking_reasons,
        },
        "filter_input": {
            "consumer": "filter-sequences",
            "sequence_sha256": sha256_bytes(sequence.encode("utf-8")),
            "required_length_nt": FILTER_SEQUENCE_EXPECTED_LENGTH_NT,
            "required_alphabet": "ACGT",
            "required_frame": 0,
            "declared_frame": declared_frame,
            "frame_ok": frame_ok,
            "strand": strand,
            "strand_ok": strand_ok,
            "stop_rule": "no '*' in frame-zero translation except the terminal position",
            "length_ok": sequence_length == FILTER_SEQUENCE_EXPECTED_LENGTH_NT,
            "alphabet_ok": alphabet_ok,
            "dna_compatible": dna_compatible,
            "internal_stop_status": stop_qc["status"],
            "internal_stop_count": stop_qc["internal_stop_count"],
            "internal_stop_positions_aa_0based": stop_qc[
                "internal_stop_positions_aa_0based"
            ],
            "terminal_stop": stop_qc["terminal_stop"],
            "shape_compatible": filter_shape_compatible,
            "ready_for_filter_decision": (
                filter_shape_compatible
                and stop_qc["status"] == "evaluated"
                and stop_qc["internal_stop_count"] == 0
            ),
            "blocking_reasons": filter_blocking_reasons,
        },
    }


def _stop_qc(sequence: str, alphabet_ok: bool) -> dict[str, Any]:
    """Evaluate the filter-sequences stop predicate when frame-zero translation is defined."""

    if not alphabet_ok or len(sequence) % 3:
        return {
            "status": "not_evaluated",
            "internal_stop_count": None,
            "internal_stop_positions_aa_0based": [],
            "terminal_stop": None,
        }

    protein = translate(sequence)
    internal_stop_positions = [
        position for position, amino_acid in enumerate(protein[:-1]) if amino_acid == "*"
    ]
    return {
        "status": "evaluated",
        "internal_stop_count": len(internal_stop_positions),
        "internal_stop_positions_aa_0based": internal_stop_positions,
        "terminal_stop": bool(protein) and protein[-1] == "*",
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


TOOL_FUNCTIONS = {'sequence-properties': tool_sequence_properties}


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


def _run(args: argparse.Namespace) -> int:
    started = time.time()
    input_bytes = Path(args.input).read_bytes()
    fixture = load_fixture(args.input)
    rows = rows_from_file(args.rows)
    payload = TOOL_FUNCTIONS[args.tool](fixture, rows)
    result = {"tool_id": args.tool}
    if args.tool == "sequence-properties":
        result["result_schema_version"] = SEQUENCE_PROPERTIES_RESULT_SCHEMA_VERSION
    result.update(
        {
            "input_sha256": sha256_bytes(input_bytes),
            "rows_sha256": sha256_bytes(Path(args.rows).read_bytes()) if args.rows else None,
            "payload": payload,
        }
    )
    output_path = Path(args.output)
    run_root = Path(args.run_root)
    run_root.mkdir(parents=True, exist_ok=True)
    _write_json_artifact(output_path, result)
    event = {"tool_id": args.tool, "input": args.input, "rows": args.rows, "output": args.output, "input_sha256": result["input_sha256"], "output_sha256": sha256_bytes(output_path.read_bytes()), "started_epoch": started, "finished_epoch": time.time(), "exit_code": 0}
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
    except (OSError, ValueError, TypeError, KeyError) as exc:
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
