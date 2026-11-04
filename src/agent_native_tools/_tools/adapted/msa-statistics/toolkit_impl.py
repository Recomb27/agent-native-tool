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

MSA_DNA_ALPHABET = frozenset("ACGT")
MSA_CONTRACT_VERSION = "msa-statistics.compatibility.v1"
MSA_COORDINATE_SYSTEM = "0-based-half-open"
MSA_SEQUENCE_METADATA_FIELDS = frozenset(
    {
        "content_sha256",
        "coordinate_system",
        "frame",
        "molecule",
        "molecule_type",
        "object_id",
        "sequence_sha256",
        "source_record_id",
        "strand",
        "unit",
    }
)
MSA_TRANSLATION_CONTEXT_FIELDS = (
    "coordinate_system",
    "frame",
    "genetic_code",
    "molecule",
    "partial_codon_policy",
    "strand",
)
MSA_TRANSLATION_CONTEXT_FIELD_SET = frozenset(MSA_TRANSLATION_CONTEXT_FIELDS)
MSA_INVALID_INPUT_EXIT_CODE = 2


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


def rows_from_file(rows_path: str | Path | None) -> list[Any]:
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


def _row_join_fields(row: Any) -> dict[str, Any]:
    """Expose stable, conventional ID fields without assuming a row schema."""

    if not isinstance(row, dict):
        return {}
    return {
        key: row[key]
        for key in sorted(row)
        if isinstance(key, str) and (key == "id" or key.endswith("_id"))
    }


def _row_provenance(
    rows: list[Any],
    rows_sha256: str | None,
    rows_supplied: bool,
) -> dict[str, Any]:
    """Describe accepted rows without allowing their opaque schema to affect MSA."""

    return {
        "source": "rows_argument" if rows_supplied else "argument_not_supplied",
        "provided": rows_supplied,
        "used_for_statistics": False,
        "row_count": len(rows),
        "rows_sha256": rows_sha256,
        "join_basis": ["row_index", "canonical_row_sha256"],
        "row_schema": "opaque_json_values; conventional *_id fields are echoed",
        "rows": [
            {
                "row_index": index,
                "row_sha256": digest(row),
                "join_fields": _row_join_fields(row),
            }
            for index, row in enumerate(rows)
        ],
    }


def _normalise_frame(value: Any, path: str) -> int:
    """Accept only frame-zero values that survive JSON stringification safely."""

    if isinstance(value, bool):
        raise ValueError(
            f"{path} must be frame 0; accepted forms are integer 0 or string '0', "
            f"got {value!r}"
        )
    if isinstance(value, int):
        frame = value
    elif isinstance(value, str) and value == "0":
        frame = 0
    else:
        raise ValueError(
            f"{path} must be frame 0; accepted forms are integer 0 or string '0', "
            f"got {value!r}"
        )
    if frame != 0:
        raise ValueError(f"{path} must be frame 0; got {value!r}")
    return 0


def _check_declared_value(
    mapping: dict[str, Any],
    keys: tuple[str, ...],
    expected: Any,
    path: str,
) -> Any:
    """Validate aliases without allowing conflicting duplicate declarations."""

    supplied = [(key, mapping[key]) for key in keys if key in mapping and mapping[key] is not None]
    if not supplied:
        return None
    first_key, first_value = supplied[0]
    if any(value != first_value for _key, value in supplied[1:]):
        raise ValueError(f"{path} contains conflicting declarations for {', '.join(keys)}")
    if first_value != expected:
        raise ValueError(f"{path}.{first_key} must be {expected!r}; got {first_value!r}")
    return expected


def _validate_translation_context(fixture: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Validate the optional producer context used to interpret the DNA."""

    defaults = {
        "coordinate_system": MSA_COORDINATE_SYSTEM,
        "frame": 0,
        "genetic_code": "standard",
        "molecule": "DNA",
        "partial_codon_policy": "reject",
        "strand": "forward",
    }
    if "translation_context" not in fixture:
        return defaults, "implicit_v0_defaults"

    context = fixture["translation_context"]
    if not isinstance(context, dict):
        raise ValueError("translation_context must be a JSON object")
    unknown_fields = sorted(set(context) - MSA_TRANSLATION_CONTEXT_FIELD_SET)
    if unknown_fields:
        raise ValueError(
            "Unsupported translation_context fields: " + ", ".join(unknown_fields)
        )

    normalised = dict(defaults)
    if "frame" in context:
        normalised["frame"] = _normalise_frame(
            context["frame"], "translation_context.frame"
        )
    for key in MSA_TRANSLATION_CONTEXT_FIELDS:
        if key == "frame":
            continue
        if key in context and context[key] != defaults[key]:
            raise ValueError(
                f"Unsupported translation_context.{key}: expected {defaults[key]!r}, "
                f"got {context[key]!r}"
            )
    return normalised, "explicit"


def _validate_sequence_metadata(
    fixture: dict[str, Any],
    sequence_ids: list[str],
    sequences: dict[str, str],
) -> tuple[dict[str, dict[str, Any]], str]:
    """Validate optional identity/type declarations and return safe summaries."""

    if "sequence_metadata" not in fixture:
        raw_metadata: dict[str, Any] = {}
        metadata_source = "implicit_v0_defaults"
    else:
        raw_metadata = fixture["sequence_metadata"]
        if not isinstance(raw_metadata, dict):
            raise ValueError("sequence_metadata must be a JSON object")
        metadata_source = "explicit"

    identity_by_id: dict[str, dict[str, Any]] = {}
    object_owner: dict[str, str] = {}
    for sequence_id, raw_record in raw_metadata.items():
        path = f"sequence_metadata[{sequence_id!r}]"
        if sequence_id not in sequences:
            raise ValueError(f"{path} references unknown sequence")
        if not isinstance(raw_record, dict):
            raise ValueError(f"{path} must be a JSON object")
        unknown_fields = sorted(set(raw_record) - MSA_SEQUENCE_METADATA_FIELDS)
        if unknown_fields:
            raise ValueError(f"{path} has unsupported fields: {unknown_fields}")

        _check_declared_value(
            raw_record,
            ("molecule_type", "molecule"),
            "DNA",
            path,
        )
        _check_declared_value(raw_record, ("unit",), "nt", path)
        if "coordinate_system" in raw_record and raw_record["coordinate_system"] is not None:
            coordinate_system = raw_record["coordinate_system"]
            if not isinstance(coordinate_system, str) or coordinate_system not in {
                MSA_COORDINATE_SYSTEM,
                "0-based half-open",
            }:
                raise ValueError(
                    f"{path}.coordinate_system must be a 0-based half-open system; "
                    f"got {coordinate_system!r}"
                )
        if "frame" in raw_record and raw_record["frame"] is not None:
            _normalise_frame(raw_record["frame"], f"{path}.frame")
        if "strand" in raw_record and raw_record["strand"] is not None:
            if raw_record["strand"] != "forward":
                raise ValueError(
                    f"{path}.strand must be 'forward'; got {raw_record['strand']!r}"
                )

        for identity_key in ("object_id", "source_record_id"):
            identity = raw_record.get(identity_key)
            if identity is not None and (not isinstance(identity, str) or not identity):
                raise ValueError(f"{path}.{identity_key} must be a non-empty string")

        declared_hashes = [
            (key, raw_record[key])
            for key in ("content_sha256", "sequence_sha256")
            if key in raw_record and raw_record[key] is not None
        ]
        expected_hash = sha256_bytes(sequences[sequence_id].encode("utf-8"))
        for hash_key, declared_hash in declared_hashes:
            if (
                not isinstance(declared_hash, str)
                or len(declared_hash) != 64
                or any(character not in "0123456789abcdefABCDEF" for character in declared_hash)
            ):
                raise ValueError(f"{path}.{hash_key} must be a SHA-256 hex digest")
            if declared_hash.lower() != expected_hash:
                raise ValueError(
                    f"{path}.{hash_key} does not match the exact sequence content"
                )
        if len({declared_hash.lower() for _key, declared_hash in declared_hashes}) > 1:
            raise ValueError(f"{path} contains conflicting sequence hash declarations")

        object_id = raw_record.get("object_id")
        if object_id is not None:
            previous_owner = object_owner.get(object_id)
            if previous_owner is not None and previous_owner != sequence_id:
                raise ValueError(
                    f"{path}.object_id {object_id!r} is already assigned to "
                    f"sequence_metadata[{previous_owner!r}]"
                )
            object_owner[object_id] = sequence_id

        identity_by_id[sequence_id] = {
            "object_id": object_id,
            "object_id_status": "declared" if object_id is not None else "unknown_not_supplied",
            "source_record_id": raw_record.get("source_record_id"),
            "source_record_id_status": (
                "declared" if raw_record.get("source_record_id") is not None else "unknown_not_supplied"
            ),
            "sequence_sha256": expected_hash,
            "content_sha256_status": "verified" if declared_hashes else "not_supplied",
            "frame": _normalise_frame(raw_record["frame"], f"{path}.frame")
            if raw_record.get("frame") is not None
            else 0,
            "strand": raw_record.get("strand", "as_supplied"),
        }

    for sequence_id in sequence_ids:
        if sequence_id not in identity_by_id:
            identity_by_id[sequence_id] = {
                "object_id": None,
                "object_id_status": "unknown_not_supplied",
                "source_record_id": None,
                "source_record_id_status": "unknown_not_supplied",
                "sequence_sha256": sha256_bytes(sequences[sequence_id].encode("utf-8")),
                "content_sha256_status": "not_supplied",
                "frame": 0,
                "strand": "as_supplied",
            }
    return {sequence_id: identity_by_id[sequence_id] for sequence_id in sequence_ids}, metadata_source


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
    return {"codon_counts": {key: dict(collections.Counter(seq[i : i + 3] for i in range(0, len(seq), 3))) for key, seq in fixture["sequences"].items()}}


def tool_reverse_complement(fixture: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    complement = str.maketrans("ACGT", "TGCA")
    return {"reverse_complement_sha256": {key: hashlib.sha256(seq.translate(complement)[::-1].encode()).hexdigest() for key, seq in fixture["sequences"].items()}}


def tool_sequence_statistics(fixture: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    sequences = fixture["sequences"]
    return {"n_sequences": len(sequences), "unique_dna_sequences": len(set(sequences.values())), "unique_proteins": len({translate(seq) for seq in sequences.values()}), "length_distribution": dict(collections.Counter(map(len, sequences.values())))}


def tool_filter_sequences(fixture: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"passing_ids": [key for key, seq in fixture["sequences"].items() if len(seq) == 501 and set(seq) <= set("ACGT") and "*" not in translate(seq)[:-1]], "rejected_ids": []}


def tool_msa_statistics(
    fixture: dict[str, Any],
    rows: list[Any],
    *,
    rows_sha256: str | None = None,
    rows_supplied: bool = False,
) -> dict[str, Any]:
    if not isinstance(fixture, dict):
        raise ValueError("The tool input must be a JSON object")
    raw_sequences = fixture.get("sequences")
    if not isinstance(raw_sequences, dict) or not raw_sequences:
        raise ValueError("msa-statistics requires a non-empty sequences object")

    sequence_ids = list(raw_sequences)
    sequences: dict[str, str] = {}
    for sequence_id in sequence_ids:
        if not isinstance(sequence_id, str) or not sequence_id.strip():
            raise ValueError("msa-statistics sequence ids must be non-empty strings")
        sequence = raw_sequences[sequence_id]
        if not isinstance(sequence, str) or not sequence:
            raise ValueError(f"msa-statistics sequence {sequence_id!r} must be a non-empty DNA string")
        if any(character.isspace() for character in sequence):
            raise ValueError(f"msa-statistics sequence {sequence_id!r} must not contain whitespace")
        if sequence != sequence.upper():
            raise ValueError(f"msa-statistics sequence {sequence_id!r} must use uppercase DNA symbols")
        invalid_symbols = sorted(set(sequence) - MSA_DNA_ALPHABET)
        if invalid_symbols:
            if "-" in invalid_symbols:
                raise ValueError(
                    f"msa-statistics requires ungapped DNA; sequence {sequence_id!r} contains gap symbol '-'")
            raise ValueError(
                f"msa-statistics sequence {sequence_id!r} contains unsupported DNA symbols: {invalid_symbols}")
        sequences[sequence_id] = sequence

    lengths = {sequence_id: len(sequence) for sequence_id, sequence in sequences.items()}
    if len(set(lengths.values())) != 1:
        raise ValueError(
            "msa-statistics requires equal-length ungapped DNA sequences before translation; "
            f"got lengths {lengths}")
    alignment_length_nt = next(iter(lengths.values()))
    if alignment_length_nt % 3:
        raise ValueError(
            "msa-statistics equal-length DNA must be divisible by three; "
            f"got length {alignment_length_nt}")

    translation_context, translation_context_source = _validate_translation_context(fixture)
    sequence_identity_metadata, sequence_metadata_source = _validate_sequence_metadata(
        fixture,
        sequence_ids,
        sequences,
    )

    proteins_by_id = {sequence_id: translate(sequence) for sequence_id, sequence in sequences.items()}
    proteins = [proteins_by_id[sequence_id] for sequence_id in sequence_ids]
    columns = []
    for i in range(len(proteins[0])):
        counts = collections.Counter(protein[i] for protein in proteins)
        total = sum(counts.values())
        entropy = -sum((n / total) * math.log2(n / total) for n in counts.values())
        maximum = max(counts.values())
        consensus = next(
            symbol for symbol in (protein[i] for protein in proteins) if counts[symbol] == maximum
        )
        columns.append({"position": i, "consensus": consensus, "entropy_bits": entropy})

    sequence_set_sha256 = digest({"sequences": {key: sequences[key] for key in sorted(sequences)}})
    sequence_order_sha256 = digest(sequence_ids)
    column_coordinates = [
        {
            "position": position,
            "protein_range_0based_halfopen": [position, position + 1],
            "dna_range_0based_halfopen": [3 * position, 3 * (position + 1)],
            "source_sequence_ids": sequence_ids,
        }
        for position in range(len(proteins[0]))
    ]
    stop_positions = {
        sequence_id: [position for position, symbol in enumerate(proteins_by_id[sequence_id]) if symbol == "*"]
        for sequence_id in sequence_ids
    }
    sequence_records = []
    for sequence_index, sequence_id in enumerate(sequence_ids):
        identity = sequence_identity_metadata[sequence_id]
        sequence_records.append(
            {
                "sequence_index": sequence_index,
                "sequence_id": sequence_id,
                "object_id": identity["object_id"],
                "object_id_status": identity["object_id_status"],
                "source_record_id": identity["source_record_id"],
                "source_record_id_status": identity["source_record_id_status"],
                "sequence_sha256": identity["sequence_sha256"],
                "dna_length_nt": len(sequences[sequence_id]),
                "protein_length_aa": len(proteins_by_id[sequence_id]),
                "dna_range_0based_halfopen": [0, len(sequences[sequence_id])],
                "protein_range_0based_halfopen": [0, len(proteins_by_id[sequence_id])],
            }
        )
    return {
        "alignment_length": len(proteins[0]),
        "columns": columns,
        "compatibility": {
            "contract_version": MSA_CONTRACT_VERSION,
            "sequence_identity": {
                "sequence_ids": sequence_ids,
                "sequence_count": len(sequence_ids),
                "sequence_set_sha256": sequence_set_sha256,
                "sequence_order_sha256": sequence_order_sha256,
                "sequence_sha256_by_id": {
                    sequence_id: sha256_bytes(sequences[sequence_id].encode("utf-8"))
                    for sequence_id in sequence_ids
                },
                "object_identity_by_sequence_id": sequence_identity_metadata,
                "sequence_records": sequence_records,
            },
            "alignment": {
                "kind": "ungapped_translated_protein",
                "coordinate_system": "protein_0based_halfopen",
                "source_coordinate_system": "input_dna_0based_halfopen",
                "aa_unit": "aa",
                "nt_unit": "nt",
                "source_sequence_ids": sequence_ids,
                "frame": 0,
                "strand": "as_supplied",
                "reverse_complement_applied": False,
                "codon_size_nt": 3,
                "column_to_source": column_coordinates,
            },
            "gaps": {
                "accepted": False,
                "symbol": "-",
                "policy": "reject",
                "alignment_gaps_introduced": False,
            },
            "stops": {
                "symbol": "*",
                "policy": "include_as_a_regular_column_symbol",
                "denominator": "all_input_sequences",
                "positions_by_sequence_id": stop_positions,
            },
            "unknown_or_missing_symbols": {
                "accepted": False,
                "policy": "reject",
                "accepted_dna_symbols": "ACGT",
            },
            "consensus": {
                "tie_break": "first_sequence_order_in_input",
                "entropy_log_base": 2,
                "rows_input_used": False,
            },
            "row_provenance": _row_provenance(rows, rows_sha256, rows_supplied),
            "input_context": {
                "translation_context_source": translation_context_source,
                "translation_context": translation_context,
                "sequence_metadata_source": sequence_metadata_source,
                "sequence_metadata_scope": "one record per input sequence; absent identities remain unknown_not_supplied",
            },
        },
    }


def tool_restriction_sites(fixture: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    enzymes = {"EcoRI": "GAATTC", "BamHI": "GGATCC", "HindIII": "AAGCTT"}
    return {"restriction_sites": {key: {enzyme: [i for i in range(len(seq)) if seq.startswith(site, i)] for enzyme, site in enzymes.items()} for key, seq in fixture["sequences"].items()}}


TOOL_FUNCTIONS = {'msa-statistics': tool_msa_statistics}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tool", required=True, choices=sorted(TOOL_FUNCTIONS))
    parser.add_argument("--input", required=True)
    parser.add_argument("--rows")
    parser.add_argument("--output", required=True)
    parser.add_argument("--run-root", required=True)
    args = parser.parse_args(argv)
    started = time.time()
    output_path = Path(args.output)
    temporary_output: Path | None = None
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        Path(args.run_root).mkdir(parents=True, exist_ok=True)
        input_bytes = Path(args.input).read_bytes()
        fixture = json.loads(input_bytes)
        rows = rows_from_file(args.rows)
        rows_bytes = Path(args.rows).read_bytes() if args.rows else None
        if args.tool == "msa-statistics":
            payload = tool_msa_statistics(
                fixture,
                rows,
                rows_sha256=sha256_bytes(rows_bytes) if rows_bytes is not None else None,
                rows_supplied=args.rows is not None,
            )
        else:
            payload = TOOL_FUNCTIONS[args.tool](fixture, rows)
        result = {
            "tool_id": args.tool,
            "input_sha256": sha256_bytes(input_bytes),
            "rows_sha256": sha256_bytes(rows_bytes) if rows_bytes is not None else None,
            "payload": payload,
        }
        serialized = (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_output = Path(temporary.name)
            temporary.write(serialized)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_output, output_path)
        temporary_output = None
        event = {"tool_id": args.tool, "input": args.input, "rows": args.rows, "output": args.output, "input_sha256": result["input_sha256"], "rows_sha256": result["rows_sha256"], "output_sha256": sha256_bytes(serialized), "started_epoch": started, "finished_epoch": time.time(), "exit_code": 0}
        with (Path(args.run_root) / "tool_calls.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        print(json.dumps({"status": "ok", "tool_id": args.tool, "output": args.output}, ensure_ascii=False))
        return 0
    except Exception as exc:
        if temporary_output is not None:
            try:
                temporary_output.unlink()
            except FileNotFoundError:
                pass
        if args.tool != "msa-statistics":
            raise
        print(
            json.dumps(
                {
                    "status": "error",
                    "tool_id": args.tool,
                    "error": {"type": "invalid_input", "message": str(exc)},
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return MSA_INVALID_INPUT_EXIT_CODE


if __name__ == "__main__":
    raise SystemExit(main())
