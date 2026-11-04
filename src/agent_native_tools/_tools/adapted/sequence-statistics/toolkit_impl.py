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


_SEQUENCE_METADATA_FIELDS = frozenset(
    {
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
)
_COORDINATE_SYSTEM_ALIASES = {
    "0-based-half-open": "0-based-half-open",
    "0-based half-open": "0-based-half-open",
}
_STRAND_ALIASES = {
    "as_provided": "as supplied",
    "as_supplied": "as supplied",
    "forward": "as supplied",
}


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _metadata_alias(
    metadata: dict[str, Any], keys: tuple[str, ...], path: str
) -> tuple[str | None, Any | None]:
    supplied = [(key, metadata[key]) for key in keys if key in metadata]
    if not supplied:
        return None, None
    first_key, first_value = supplied[0]
    if any(value != first_value for _, value in supplied[1:]):
        raise ValueError(f"{path} contains conflicting aliases: {[key for key, _ in supplied]}")
    return first_key, first_value


def _validate_digest(value: Any, path: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{path} must be a lowercase SHA-256 hex string")
    return value


def validate_sequence_metadata(
    fixture: dict[str, Any], sequences: dict[str, str]
) -> dict[str, dict[str, Any]]:
    """Validate optional metadata without guessing a frame or object join."""
    raw_metadata = fixture.get("sequence_metadata")
    if raw_metadata is None:
        return {}
    if not isinstance(raw_metadata, dict):
        raise ValueError("sequence_metadata must be a JSON object")

    normalized: dict[str, dict[str, Any]] = {}
    seen_object_ids: dict[str, str] = {}
    for record_id, raw_record in raw_metadata.items():
        path = f"sequence_metadata[{record_id!r}]"
        if not isinstance(record_id, str) or record_id not in sequences:
            raise ValueError(f"{path} must refer to an ID in sequences")
        if not isinstance(raw_record, dict):
            raise ValueError(f"{path} must be a JSON object")
        unsupported = sorted(set(raw_record) - _SEQUENCE_METADATA_FIELDS)
        if unsupported:
            raise ValueError(f"{path} has unsupported fields: {unsupported}")

        molecule_key, molecule = _metadata_alias(raw_record, ("molecule", "molecule_type"), path)
        if molecule is not None and molecule != "DNA":
            raise ValueError(f"{path}.{molecule_key} must be 'DNA' for this tool")

        if "unit" in raw_record and raw_record["unit"] != "nt":
            raise ValueError(f"{path}.unit must be 'nt'")

        coordinate_key, coordinate_system = _metadata_alias(
            raw_record, ("coordinate_system", "coordinate_range", "range"), path
        )
        normalized_coordinate_system = None
        if coordinate_system is not None:
            if not isinstance(coordinate_system, str) or coordinate_system not in _COORDINATE_SYSTEM_ALIASES:
                raise ValueError(
                    f"{path}.{coordinate_key} must be '0-based-half-open' or '0-based half-open'"
                )
            normalized_coordinate_system = _COORDINATE_SYSTEM_ALIASES[coordinate_system]

        frame = 0
        frame_declared = "frame" in raw_record
        if frame_declared:
            raw_frame = raw_record["frame"]
            if isinstance(raw_frame, bool):
                raise ValueError(f"{path}.frame must be integer 0 or exact string '0'")
            if isinstance(raw_frame, int):
                frame = raw_frame
            elif isinstance(raw_frame, str) and raw_frame == "0":
                # The real agents sometimes serialize a clear numeric frame as
                # a string. Normalize only the exact unambiguous spelling.
                frame = 0
            else:
                raise ValueError(f"{path}.frame must be integer 0 or exact string '0'")
            if frame != 0:
                raise ValueError(f"{path}.frame={frame!r} is unsupported; only frame 0 is accepted")

        strand = "as supplied"
        strand_declared = "strand" in raw_record
        if strand_declared:
            raw_strand = raw_record["strand"]
            if raw_strand not in _STRAND_ALIASES:
                raise ValueError(
                    f"{path}.strand must be one of {sorted(_STRAND_ALIASES)}; reverse/unknown strands are unsupported"
                )
            strand = _STRAND_ALIASES[raw_strand]

        object_id = raw_record.get("object_id")
        if object_id is not None:
            if (
                not isinstance(object_id, str)
                or not object_id
                or object_id != object_id.strip()
                or any(ord(character) < 32 for character in object_id)
            ):
                raise ValueError(f"{path}.object_id must be a non-empty safe string")
            previous_record_id = seen_object_ids.get(object_id)
            if previous_record_id is not None and previous_record_id != record_id:
                raise ValueError(
                    f"{path}.object_id {object_id!r} is already assigned to {previous_record_id!r}"
                )
            seen_object_ids[object_id] = record_id

        source_record_id = raw_record.get("source_record_id")
        if source_record_id is not None:
            if (
                not isinstance(source_record_id, str)
                or not source_record_id
                or source_record_id != source_record_id.strip()
                or any(ord(character) < 32 for character in source_record_id)
            ):
                raise ValueError(f"{path}.source_record_id must be a non-empty safe string")

        expected_dna_sha256 = sha256_bytes(sequences[record_id].encode("utf-8"))
        digest_values = []
        for digest_key in ("content_sha256", "sequence_sha256"):
            if digest_key in raw_record:
                digest_values.append((digest_key, _validate_digest(raw_record[digest_key], f"{path}.{digest_key}")))
        if digest_values and any(value != digest_values[0][1] for _, value in digest_values[1:]):
            raise ValueError(f"{path} contains conflicting content_sha256 and sequence_sha256")
        if digest_values and digest_values[0][1] != expected_dna_sha256:
            raise ValueError(f"{path} sequence hash does not match the exact supplied DNA string")

        normalized[record_id] = {
            "frame": frame,
            "frame_declared": frame_declared,
            "strand": strand,
            "strand_declared": strand_declared,
            "coordinate_system": normalized_coordinate_system,
            "object_id": object_id,
            "source_record_id": source_record_id,
        }
    return normalized


def translate(dna: str) -> str:
    if not isinstance(dna, str):
        raise ValueError("DNA sequence must be a string")
    if len(dna) % 3:
        raise ValueError(f"DNA length is not divisible by three: {len(dna)}")
    try:
        return "".join(CODON_TABLE[dna[i : i + 3]] for i in range(0, len(dna), 3))
    except KeyError as exc:
        raise ValueError(f"Unknown codon: {exc.args[0]}") from exc


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_fixture(input_path: str | Path) -> dict[str, Any]:
    return validate_fixture(read_json(input_path))


def validate_fixture(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("The tool input must be a JSON object")
    if "sequences" not in payload:
        raise ValueError("The tool input must contain sequences")
    sequences = payload["sequences"]
    if not isinstance(sequences, dict):
        raise ValueError("The tool input sequences must be a JSON object")
    for record_id, dna in sequences.items():
        if not isinstance(record_id, str):
            raise ValueError("sequence record IDs must be strings")
        if not isinstance(dna, str):
            raise ValueError(f"sequence {record_id!r} must be a string")
    validate_sequence_metadata(payload, sequences)
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


def rows_from_bytes(raw: bytes, rows_path: str | Path) -> list[dict[str, Any]]:
    """Parse rows from the exact bytes whose hash is recorded in the artifact."""
    path = Path(rows_path)
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
    return {"codon_counts": {key: dict(collections.Counter(seq[i : i + 3] for i in range(0, len(seq), 3))) for key, seq in fixture["sequences"].items()}}


def tool_reverse_complement(fixture: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    complement = str.maketrans("ACGT", "TGCA")
    return {"reverse_complement_sha256": {key: hashlib.sha256(seq.translate(complement)[::-1].encode()).hexdigest() for key, seq in fixture["sequences"].items()}}


def tool_sequence_statistics(fixture: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    sequences = fixture["sequences"]
    proteins = {sequence_id: translate(sequence) for sequence_id, sequence in sequences.items()}
    length_distribution = collections.Counter(map(len, sequences.values()))
    return {
        "n_sequences": len(sequences),
        "unique_dna_sequences": len(set(sequences.values())),
        "unique_proteins": len(set(proteins.values())),
        "length_distribution": dict(sorted(length_distribution.items())),
    }


def _hash_groups(values: dict[str, str]) -> list[dict[str, Any]]:
    """Return exact-value duplicate groups with deterministic hash ordering."""
    groups: dict[str, list[str]] = collections.defaultdict(list)
    for record_id in sorted(values):
        groups[values[record_id]].append(record_id)
    ordered_groups = sorted(
        groups.items(),
        key=lambda item: (sha256_bytes(item[0].encode("utf-8")), item[0]),
    )
    return [
        {
            "sha256": sha256_bytes(value.encode("utf-8")),
            "record_ids": record_ids,
            "count": len(record_ids),
        }
        for value, record_ids in ordered_groups
        if len(record_ids) > 1
    ]


def sequence_statistics_compatibility(
    fixture: dict[str, Any], rows: list[dict[str, Any]], payload: dict[str, Any]
) -> dict[str, Any]:
    """Build additive identity and denominator metadata for sequence-statistics.

    This is intentionally separate from the legacy payload. The default command
    still computes the same summary and still fails as a whole on an invalid DNA
    record; the metadata makes those existing assumptions consumable by a later
    tool instead of asking that tool to infer them from four aggregate fields.
    """
    sequences = fixture["sequences"]
    metadata_by_id = validate_sequence_metadata(fixture, sequences)
    proteins = {record_id: translate(sequence) for record_id, sequence in sequences.items()}
    record_ids = sorted(sequences)
    record_index = []
    sample_identity = []
    seen_dna_object_ids: dict[str, str] = {}
    for record_id in record_ids:
        dna = sequences[record_id]
        protein = proteins[record_id]
        metadata = metadata_by_id.get(record_id, {})
        dna_sha256 = sha256_bytes(dna.encode("utf-8"))
        protein_sha256 = sha256_bytes(protein.encode("utf-8"))
        dna_object_id = metadata.get("object_id") or f"dna:{record_id}:{dna_sha256}"
        previous_record_id = seen_dna_object_ids.get(dna_object_id)
        if previous_record_id is not None and previous_record_id != record_id:
            raise ValueError(
                f"DNA object id {dna_object_id!r} is assigned to both {previous_record_id!r} and {record_id!r}"
            )
        seen_dna_object_ids[dna_object_id] = record_id
        record_index.append(
            {
                "record_id": record_id,
                "source_record_id": metadata.get("source_record_id"),
                "source_record_id_status": (
                    "declared" if metadata.get("source_record_id") is not None else "unknown_not_supplied"
                ),
                "dna_object_id": dna_object_id,
                "dna_object_id_status": "declared" if metadata.get("object_id") is not None else "generated",
                "dna_sha256": dna_sha256,
                "dna_length_nt": len(dna),
                "protein_object_id": f"protein:translation:{record_id}:{protein_sha256}",
                "protein_sha256": protein_sha256,
                "protein_length_aa": len(protein),
                "frame": metadata.get("frame", 0),
                "strand": metadata.get("strand", "as supplied"),
            }
        )
        sample_identity.append({"record_id": record_id, "dna_sha256": dna_sha256})

    record_count = len(sequences)
    unique_dna_count = payload["unique_dna_sequences"]
    unique_protein_count = payload["unique_proteins"]
    return {
        "contract_version": "sequence-statistics-compat-v1",
        "sample": {
            "scope": "fixture.sequences",
            "record_id_field": "mapping key",
            "record_id_order": "lexicographic",
            "record_count": record_count,
            "record_ids": record_ids,
            "sample_set_sha256": digest(sample_identity),
        },
        "denominators": {
            "n_sequences": {
                "value": record_count,
                "unit": "records",
                "definition": "one mapping entry in fixture.sequences",
            },
            "unique_dna_sequences": {
                "value": unique_dna_count,
                "unit": "exact DNA values",
                "definition": "cardinality of the DNA value set across all records",
            },
            "unique_proteins": {
                "value": unique_protein_count,
                "unit": "translated protein values",
                "definition": "cardinality of frame-zero translated values across all valid records",
            },
            "length_distribution": {
                "count_sum": sum(payload["length_distribution"].values()),
                "denominator": "record_count",
                "unit": "nucleotides per record",
                "scope": "all valid records in fixture.sequences",
            },
        },
        "identity_rules": {
            "dna": "exact supplied DNA string; no case folding, trimming, or reverse-complement normalization",
            "protein": "exact translated string including '*' stop symbols",
            "record_identity": "record_id plus dna_sha256; sample_set_sha256 is order-independent over sorted record IDs",
        },
        "object_semantics": {
            "dna": {
                "object_id_field": "record_index[].dna_object_id",
                "object_id_rule": "metadata object_id when supplied; otherwise dna:{record_id}:{dna_sha256}",
                "unit": "nt",
                "coordinate_system": "0-based-half-open",
                "frame": 0,
                "strand": "as supplied",
                "metadata_input": "optional sequence_metadata[record_id]",
            },
            "protein": {
                "object_id_field": "record_index[].protein_object_id",
                "object_id_rule": "protein:translation:{record_id}:{protein_sha256}",
                "unit": "aa",
                "coordinate_system": "0-based-half-open",
                "frame": 0,
                "derived_from": "the corresponding dna_object_id by frame-zero standard translation",
            },
        },
        "translation": {
            "molecule": "DNA",
            "frame": 0,
            "frame_status": "explicit frame 0 metadata is validated when supplied; otherwise legacy frame-zero default",
            "strand": "as supplied",
            "strand_status": (
                "declared as forward/as-provided; no orientation is inferred"
                if any(metadata.get("strand_declared") for metadata in metadata_by_id.values())
                else "not declared by this input; no orientation is inferred"
            ),
            "coordinate_unit": "nucleotide",
            "coordinate_system": "0-based-half-open",
            "codon_table": "standard table encoded by this tool",
            "accepted_alphabet": "uppercase ACGT",
            "length_requirement": "divisible by three",
            "stop_symbol": "*",
            "unknown_value_policy": "error",
            "invalid_record_policy": "fail whole input before writing an artifact; no partial summary",
        },
        "missing_value_semantics": {
            "valid_record_count": record_count,
            "invalid_record_count": 0,
            "absent_record_policy": "an absent mapping key is outside the sample; no placeholder record is created",
            "null_or_unknown_policy": "no null or unknown value is imputed; an invalid DNA record aborts the whole call",
        },
        "filtering": {
            "scope": "all records in fixture.sequences",
            "rows_argument_consumed": False,
            "observed_row_count": len(rows),
            "selection_rule": "none; rows are retained only in the input hash and are not a filter",
        },
        "range_semantics": {
            "reported_ranges": False,
            "aa_windows_consumed": False,
            "scope": "whole-record aggregates only; no window-level or coordinate-range selection",
        },
        "record_index": record_index,
        "duplicate_groups": {
            "dna": _hash_groups(sequences),
            "protein": _hash_groups(proteins),
        },
    }


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


TOOL_FUNCTIONS = {'sequence-statistics': tool_sequence_statistics}


def write_json_atomically(path: Path, value: dict[str, Any]) -> None:
    """Create a complete artifact before replacing the requested output path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


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
        fixture = validate_fixture(json.loads(input_bytes))
        rows_path = Path(args.rows) if args.rows else None
        rows_bytes = rows_path.read_bytes() if rows_path else None
        rows = rows_from_bytes(rows_bytes, rows_path) if rows_path else []
        payload = TOOL_FUNCTIONS[args.tool](fixture, rows)
        result = {
            "tool_id": args.tool,
            "input_sha256": sha256_bytes(input_bytes),
            "rows_sha256": sha256_bytes(rows_bytes) if rows_bytes is not None else None,
            "payload": payload,
        }
        if args.tool == "sequence-statistics":
            # Keep the V0 payload untouched. The compatibility contract is a
            # versioned sibling so strict consumers can continue reading payload.
            result["compatibility"] = sequence_statistics_compatibility(fixture, rows, payload)
        input_path = Path(args.input)
        rows_path = Path(args.rows) if args.rows else None
        output_path = Path(args.output)
        run_root_path = Path(args.run_root)
        if not run_root_path.is_dir():
            raise ValueError("run-root must be an existing directory")
        if output_path.exists() and not output_path.is_file():
            raise ValueError("output path must be a regular file when it already exists")
        resolved_output = output_path.resolve()
        forbidden_outputs = {input_path.resolve(), run_root_path.resolve() / "tool_calls.jsonl"}
        if rows_path is not None:
            forbidden_outputs.add(rows_path.resolve())
        if resolved_output in forbidden_outputs:
            raise ValueError("output path must not overwrite an input, rows file, or tool_calls.jsonl")
        write_json_atomically(output_path, result)
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
        with (run_root_path / "tool_calls.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        print(json.dumps({"status": "ok", "tool_id": args.tool, "output": args.output}, ensure_ascii=False))
        return 0
    except (OSError, TypeError, ValueError) as exc:
        # Invalid inputs must not leave a partial artifact or a success record.
        print(
            json.dumps(
                {
                    "status": "error",
                    "tool_id": args.tool,
                    "error": {"type": exc.__class__.__name__, "message": str(exc) or exc.__class__.__name__},
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
