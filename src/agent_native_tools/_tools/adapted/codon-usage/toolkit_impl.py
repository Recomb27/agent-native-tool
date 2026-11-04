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


CODON_USAGE_COMPATIBILITY_SCHEMA = "codon-usage-compatibility/1"
STANDARD_GENETIC_CODE_ID = "NCBI-1"
DNA_ALPHABET = frozenset("ACGT")
SUPPORTED_PARTIAL_CODON_POLICY = "exclude_trailing"
SUPPORTED_STRAND = "plus"
SEQUENCE_REFERENCE_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
SEQUENCE_METADATA_KEYS = frozenset(
    {
        "molecule_type",
        "strand",
        "frame",
        "object_id",
        "source_record_id",
        "content_sha256",
        "sequence_sha256",
    }
)


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


def _validate_sequences(fixture: dict[str, Any]) -> dict[str, str]:
    sequences = fixture["sequences"]
    if not isinstance(sequences, dict):
        raise ValueError("The sequences field must be a JSON object mapping sequence IDs to DNA strings")
    validated: dict[str, str] = {}
    for sequence_id, sequence in sequences.items():
        if not isinstance(sequence_id, str) or not sequence_id:
            raise ValueError("Every sequence ID must be a non-empty string")
        if not isinstance(sequence, str):
            raise ValueError(f"Sequence {sequence_id!r} must be a DNA string")
        invalid = sorted(set(sequence) - DNA_ALPHABET)
        if invalid:
            symbols = ", ".join(repr(symbol) for symbol in invalid)
            raise ValueError(
                f"Sequence {sequence_id!r} contains unsupported DNA symbols: {symbols}; "
                "expected uppercase A/C/G/T and ambiguous bases are rejected"
            )
        validated[sequence_id] = sequence
    return validated


def _sha256_sequence(sequence: str) -> str:
    return hashlib.sha256(sequence.encode("ascii")).hexdigest()


def _normalize_frame_offset(value: Any, field_name: str) -> tuple[int, str]:
    """Accept only integer frames or exact JSON string encodings of them."""

    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be the integer 0, 1, or 2")
    if isinstance(value, int) and value in (0, 1, 2):
        return value, "integer"
    if isinstance(value, str) and value in {"0", "1", "2"}:
        return int(value), "numeric_string_normalized"
    raise ValueError(
        f"{field_name} must be the integer 0, 1, or 2, or the exact string "
        "'0', '1', or '2'"
    )


def _validate_sequence_reference_hash(
    sequence_id: str, field_name: str, declared_hash: Any, actual_hash: str
) -> None:
    if not isinstance(declared_hash, str) or not SEQUENCE_REFERENCE_HASH_RE.fullmatch(
        declared_hash
    ):
        raise ValueError(
            f"sequence_metadata[{sequence_id!r}].{field_name} must be a lowercase "
            "SHA-256 hex digest"
        )
    if declared_hash != actual_hash:
        raise ValueError(
            f"sequence_metadata[{sequence_id!r}].{field_name} does not match the exact sequence"
        )


def _resolve_sequence_metadata(
    fixture: dict[str, Any], sequences: dict[str, str]
) -> dict[str, Any] | None:
    """Normalize the producer metadata emitted by the real TadACampaign agents."""

    if "sequence_metadata" not in fixture:
        return None
    raw_metadata = fixture["sequence_metadata"]
    if not isinstance(raw_metadata, dict):
        raise ValueError("sequence_metadata must be a JSON object when supplied")

    unexpected_ids = sorted(set(raw_metadata) - set(sequences))
    if unexpected_ids:
        raise ValueError(
            "sequence_metadata references unknown sequence(s): "
            + ", ".join(unexpected_ids)
        )

    references: dict[str, dict[str, str]] = {}
    frame_values: list[int] = []
    frame_serializations: list[str] = []
    seen_object_ids: set[str] = set()
    for sequence_id, sequence in sequences.items():
        record = raw_metadata.get(sequence_id, {})
        if not isinstance(record, dict):
            raise ValueError(f"sequence_metadata[{sequence_id!r}] must be a JSON object")
        unsupported = sorted(set(record) - SEQUENCE_METADATA_KEYS)
        if unsupported:
            raise ValueError(
                f"sequence_metadata[{sequence_id!r}] has unsupported fields: {unsupported}"
            )

        molecule_type = record.get("molecule_type")
        if molecule_type is not None and molecule_type != "DNA":
            raise ValueError(
                f"sequence_metadata[{sequence_id!r}].molecule_type must be 'DNA'"
            )

        raw_strand = record.get("strand")
        if raw_strand is None:
            strand = SUPPORTED_STRAND
        elif isinstance(raw_strand, str) and raw_strand in {"forward", SUPPORTED_STRAND}:
            strand = SUPPORTED_STRAND
        else:
            raise ValueError(
                f"sequence_metadata[{sequence_id!r}].strand {raw_strand!r} is unsupported; "
                "use 'forward'"
            )

        if "frame" in record:
            frame, frame_serialization = _normalize_frame_offset(
                record["frame"], f"sequence_metadata[{sequence_id!r}].frame"
            )
        else:
            frame, frame_serialization = 0, "legacy_default"
        frame_values.append(frame)
        frame_serializations.append(frame_serialization)

        object_id = record.get("object_id", sequence_id)
        if not isinstance(object_id, str) or not object_id:
            raise ValueError(
                f"sequence_metadata[{sequence_id!r}].object_id must be a non-empty string"
            )
        if object_id in seen_object_ids:
            raise ValueError(f"sequence object_id {object_id!r} is used more than once")
        seen_object_ids.add(object_id)

        actual_hash = _sha256_sequence(sequence)
        declared_hashes = [
            (field_name, record[field_name])
            for field_name in ("content_sha256", "sequence_sha256")
            if field_name in record
        ]
        for field_name, declared_hash in declared_hashes:
            _validate_sequence_reference_hash(
                sequence_id, field_name, declared_hash, actual_hash
            )
        if len(declared_hashes) == 2 and declared_hashes[0][1] != declared_hashes[1][1]:
            raise ValueError(
                f"sequence_metadata[{sequence_id!r}] content_sha256 and sequence_sha256 disagree"
            )

        for identity_field in ("source_record_id",):
            identity = record.get(identity_field)
            if identity is not None and (not isinstance(identity, str) or not identity):
                raise ValueError(
                    f"sequence_metadata[{sequence_id!r}].{identity_field} must be a non-empty string"
                )
        references[sequence_id] = {
            "object_id": object_id,
            "sequence_sha256": actual_hash,
        }

    if len(set(frame_values)) > 1:
        raise ValueError(
            "sequence_metadata frame values must agree across sequences; "
            "use codon_usage_context only when one frame applies to all sequences"
        )
    if "numeric_string_normalized" in frame_serializations:
        frame_serialization = "numeric_string_normalized"
    elif "integer" in frame_serializations:
        frame_serialization = "integer"
    else:
        frame_serialization = "legacy_default"
    return {
        "frame_offset_nt": frame_values[0] if frame_values else 0,
        "frame_input_serialization": frame_serialization,
        "strand": SUPPORTED_STRAND,
        "references": references,
    }


def _resolve_codon_usage_context(
    fixture: dict[str, Any], sequences: dict[str, str]
) -> tuple[dict[str, Any], dict[str, dict[str, str]]]:
    """Validate optional context and bind each sequence to an identity."""

    metadata_context = _resolve_sequence_metadata(fixture, sequences)
    if "codon_usage_context" not in fixture:
        raw_context: dict[str, Any] = {}
        context_source = (
            "sequence_metadata" if metadata_context is not None else "legacy_default"
        )
    else:
        raw_context = fixture["codon_usage_context"]
        if not isinstance(raw_context, dict):
            raise ValueError("codon_usage_context must be a JSON object when supplied")
        context_source = "explicit"

    required = {"frame_offset_nt", "strand", "genetic_code_id", "partial_codon_policy"}
    if context_source == "explicit":
        missing = sorted(required - raw_context.keys())
        if missing:
            raise ValueError(
                "codon_usage_context is missing required field(s): " + ", ".join(missing)
            )

    allowed = required | {"sequence_references"}
    unsupported = sorted(set(raw_context) - allowed)
    if unsupported:
        raise ValueError(
            "codon_usage_context contains unsupported field(s): " + ", ".join(unsupported)
        )

    raw_frame_offset = raw_context.get(
        "frame_offset_nt",
        metadata_context["frame_offset_nt"] if metadata_context is not None else 0,
    )
    frame_offset_nt, frame_input_serialization = _normalize_frame_offset(
        raw_frame_offset, "frame_offset_nt"
    )
    if (
        metadata_context is not None
        and frame_offset_nt != metadata_context["frame_offset_nt"]
    ):
        raise ValueError(
            "codon_usage_context.frame_offset_nt conflicts with sequence_metadata.frame"
        )

    strand = raw_context.get(
        "strand",
        metadata_context["strand"] if metadata_context is not None else SUPPORTED_STRAND,
    )
    if strand != SUPPORTED_STRAND:
        raise ValueError(
            f"strand {strand!r} is unsupported; provide a plus-strand or reverse-complemented sequence"
        )

    genetic_code_id = raw_context.get("genetic_code_id", STANDARD_GENETIC_CODE_ID)
    if not isinstance(genetic_code_id, str) or not genetic_code_id.strip():
        raise ValueError("genetic_code_id must be a non-empty string")

    partial_codon_policy = raw_context.get(
        "partial_codon_policy", SUPPORTED_PARTIAL_CODON_POLICY
    )
    if partial_codon_policy != SUPPORTED_PARTIAL_CODON_POLICY:
        raise ValueError(
            f"partial_codon_policy {partial_codon_policy!r} is unsupported; "
            f"use {SUPPORTED_PARTIAL_CODON_POLICY!r}"
        )

    if metadata_context is not None and "frame_offset_nt" not in raw_context:
        frame_input_serialization = metadata_context["frame_input_serialization"]

    raw_references = raw_context.get("sequence_references")
    if raw_references is not None:
        if not isinstance(raw_references, dict):
            raise ValueError("sequence_references must map input sequence IDs to reference objects")
        expected_ids = set(sequences)
        actual_ids = set(raw_references)
        missing_ids = sorted(expected_ids - actual_ids)
        extra_ids = sorted(actual_ids - expected_ids)
        if missing_ids or extra_ids:
            details = []
            if missing_ids:
                details.append("missing " + ", ".join(missing_ids))
            if extra_ids:
                details.append("unexpected " + ", ".join(extra_ids))
            raise ValueError(
                "sequence_references must cover exactly the sequences ("
                + "; ".join(details)
                + ")"
            )
    elif metadata_context is not None:
        raw_references = metadata_context["references"]
    else:
        raw_references = {
            sequence_id: {
                "object_id": sequence_id,
                "sequence_sha256": _sha256_sequence(sequence),
            }
            for sequence_id, sequence in sequences.items()
        }

    references: dict[str, dict[str, str]] = {}
    seen_object_ids: set[str] = set()
    for sequence_id, sequence in sequences.items():
        reference = raw_references[sequence_id]
        if not isinstance(reference, dict):
            raise ValueError(f"Reference for sequence {sequence_id!r} must be a JSON object")
        if set(reference) != {"object_id", "sequence_sha256"}:
            raise ValueError(
                f"Reference for sequence {sequence_id!r} must contain exactly object_id and sequence_sha256"
            )
        object_id = reference["object_id"]
        expected_hash = reference["sequence_sha256"]
        if not isinstance(object_id, str) or not object_id:
            raise ValueError(f"Reference for sequence {sequence_id!r} has an invalid object_id")
        if not isinstance(expected_hash, str) or not SEQUENCE_REFERENCE_HASH_RE.fullmatch(expected_hash):
            raise ValueError(
                f"Reference for sequence {sequence_id!r} has an invalid sequence_sha256; "
                "expected a lowercase SHA-256 hex digest"
            )
        actual_hash = _sha256_sequence(sequence)
        if expected_hash != actual_hash:
            raise ValueError(
                f"Reference hash mismatch for sequence {sequence_id!r}: "
                f"expected {expected_hash}, observed {actual_hash}"
            )
        if object_id in seen_object_ids:
            raise ValueError(f"sequence object_id {object_id!r} is used more than once")
        seen_object_ids.add(object_id)
        references[sequence_id] = {
            "object_id": object_id,
            "sequence_sha256": actual_hash,
        }

        if metadata_context is not None:
            metadata_reference = metadata_context["references"][sequence_id]
            if references[sequence_id] != metadata_reference:
                raise ValueError(
                    f"codon_usage_context sequence reference for {sequence_id!r} "
                    "conflicts with sequence_metadata"
                )

    context = {
        "context_source": context_source,
        "frame_offset_nt": frame_offset_nt,
        "frame_input_serialization": frame_input_serialization,
        "strand": strand,
        "genetic_code_id": genetic_code_id,
        "partial_codon_policy": partial_codon_policy,
    }
    return context, references


def _codon_sequence_metadata(
    sequence_id: str,
    sequence: str,
    reference: dict[str, str],
    context: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, int]]:
    length_nt = len(sequence)
    requested_frame = context["frame_offset_nt"]
    frame_start = min(requested_frame, length_nt)
    remaining_nt = max(0, length_nt - requested_frame)
    complete_codon_count = remaining_nt // 3
    eligible_end = frame_start + complete_codon_count * 3
    trailing_nt = length_nt - eligible_end
    codon_counts = dict(
        collections.Counter(
            sequence[index : index + 3]
            for index in range(frame_start, eligible_end, 3)
        )
    )
    metadata: dict[str, Any] = {
        "input_sequence_id": sequence_id,
        "object_id": reference["object_id"],
        "sequence_sha256": reference["sequence_sha256"],
        "sequence_length_nt": length_nt,
        "coordinate_system": "0-based-half-open",
        "coordinate_unit": "nucleotide",
        "strand": context["strand"],
        "frame_offset_nt": requested_frame,
        "frame_offset_applied_nt": frame_start,
        "frame": f"frame_{requested_frame}",
        "codon_size_nt": 3,
        "partial_codon_policy": context["partial_codon_policy"],
        "eligible_range_nt": [frame_start, eligible_end],
        "leading_excluded_nt": frame_start,
        "trailing_excluded_nt": trailing_nt,
        "complete_codon_count": complete_codon_count,
        "count_denominator": {
            "name": "complete_codon_count",
            "value": complete_codon_count,
            "unit": "codons",
            "definition": "sum(codon_counts.values())",
        },
        "unknown_base_policy": "reject",
    }
    return metadata, codon_counts


def _translation_handoff(
    sequence_metadata: dict[str, dict[str, Any]], context: dict[str, Any]
) -> dict[str, Any]:
    per_sequence: dict[str, dict[str, Any]] = {}
    standard_code = context["genetic_code_id"] == STANDARD_GENETIC_CODE_ID
    for sequence_id, metadata in sequence_metadata.items():
        if not standard_code:
            status = "requires_matching_genetic_code"
        elif metadata["frame_offset_nt"] == 0 and metadata["trailing_excluded_nt"] == 0:
            status = "directly_compatible"
        else:
            status = "requires_eligible_range_slice"
        per_sequence[sequence_id] = {
            "status": status,
            "object_id": metadata["object_id"],
            "sequence_sha256": metadata["sequence_sha256"],
            "eligible_range_nt": metadata["eligible_range_nt"],
            "expected_protein_length": metadata["complete_codon_count"],
        }

    statuses = {item["status"] for item in per_sequence.values()}
    if not standard_code:
        overall_status = "requires_matching_genetic_code"
    elif statuses <= {"directly_compatible"}:
        overall_status = "directly_compatible"
    else:
        overall_status = "requires_eligible_range_slice"
    return {
        "consumer_tool": "transcription-translation",
        "status": overall_status,
        "frozen_consumer_assumption": "standard NCBI-1 code, plus strand, frame 0, and length divisible by three",
        "same_object_rule": "consumer input must preserve object_id and sequence_sha256",
        "eligible_range_rule": "translate sequence[eligible_range_nt[0]:eligible_range_nt[1]]",
        "genetic_code_id": context["genetic_code_id"],
        "per_sequence": per_sequence,
    }


def _statistics_handoff(sequence_metadata: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "consumer_tool": "sequence-statistics",
        "status": "joinable_via_input_sha256",
        "join_rule": "consumer input_sha256 must equal this artifact input_sha256",
        "sequence_identity_rule": "use object_id and sequence_sha256 from sequences",
        "codon_count_unit": "codons",
        "codon_count_denominator": "complete_codon_count",
        "statistics_length_unit": "nucleotides",
        "statistics_length_distribution_key": "string(sequence_length_nt)",
        "per_sequence_expected_lengths_nt": {
            metadata["object_id"]: metadata["sequence_length_nt"]
            for metadata in sequence_metadata.values()
        },
    }


def _codon_usage_compatibility(
    sequence_metadata: dict[str, dict[str, Any]], context: dict[str, Any]
) -> dict[str, Any]:
    return {
        "schema_version": CODON_USAGE_COMPATIBILITY_SCHEMA,
        "context_source": context["context_source"],
        "input_context": {
            "frame_offset_nt": context["frame_offset_nt"],
            "frame": f"frame_{context['frame_offset_nt']}",
            "frame_input_serialization": context["frame_input_serialization"],
            "strand": context["strand"],
            "genetic_code_id": context["genetic_code_id"],
            "partial_codon_policy": context["partial_codon_policy"],
        },
        "semantics": {
            "coordinate_system": "0-based-half-open",
            "coordinate_unit": "nucleotide",
            "codon_size_nt": 3,
            "counts_include": "complete codons in eligible_range_nt",
            "count_unit": "codons",
            "count_denominator": "complete_codon_count",
            "genetic_code_used_for_counting": False,
            "unknown_base_policy": "reject",
        },
        "sequences": sequence_metadata,
        "translation_handoff": _translation_handoff(sequence_metadata, context),
        "statistics_handoff": _statistics_handoff(sequence_metadata),
    }


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
    del rows
    sequences = _validate_sequences(fixture)
    context, references = _resolve_codon_usage_context(fixture, sequences)
    sequence_metadata: dict[str, dict[str, Any]] = {}
    codon_counts: dict[str, dict[str, int]] = {}
    for sequence_id, sequence in sequences.items():
        metadata, counts = _codon_sequence_metadata(
            sequence_id, sequence, references[sequence_id], context
        )
        sequence_metadata[sequence_id] = metadata
        codon_counts[sequence_id] = counts
    return {
        "codon_counts": codon_counts,
        "compatibility": _codon_usage_compatibility(sequence_metadata, context),
    }


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


TOOL_FUNCTIONS = {'codon-usage': tool_codon_usage}


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tool", required=True, choices=sorted(TOOL_FUNCTIONS))
    parser.add_argument("--input", required=True)
    parser.add_argument("--rows")
    parser.add_argument("--output", required=True)
    parser.add_argument("--run-root", required=True)
    args = parser.parse_args(argv)
    started = time.time()
    try:
        input_bytes = Path(args.input).read_bytes()
        fixture = load_fixture(args.input)
        rows = rows_from_file(args.rows)
        tool_result = TOOL_FUNCTIONS[args.tool](fixture, rows)
        compatibility = None
        if args.tool == "codon-usage":
            compatibility = tool_result.pop("compatibility")
        payload = tool_result
        result = {
            "tool_id": args.tool,
            "input_sha256": sha256_bytes(input_bytes),
            "rows_sha256": sha256_bytes(Path(args.rows).read_bytes()) if args.rows else None,
            "payload": payload,
        }
        if compatibility is not None:
            result["compatibility"] = compatibility
        output_path = Path(args.output)
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
        with (Path(args.run_root) / "tool_calls.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        print(json.dumps({"status": "ok", "tool_id": args.tool, "output": args.output}, ensure_ascii=False))
        return 0
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
