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
    return {"codon_counts": {key: dict(collections.Counter(seq[i : i + 3] for i in range(0, len(seq), 3))) for key, seq in fixture["sequences"].items()}}


_DNA_COMPLEMENT = str.maketrans("ACGT", "TGCA")
_DNA_ALPHABET = frozenset("ACGT")
_REVERSE_COMPLEMENT_SCHEMA = "reverse-complement.compatibility.v1"


def _is_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _normalize_frame_zero(value: Any) -> int | None:
    """Normalize only the unambiguous JSON spellings of frame zero.

    The r1 agent serialized a numeric frame as the string ``"0"``.  Accepting
    that exact spelling preserves the intended coordinate contract while
    rejecting values such as booleans, floats, whitespace, and alternate
    numeric spellings that could hide a producer bug.
    """

    if _is_integer(value) and value == 0:
        return 0
    if isinstance(value, str) and value == "0":
        return 0
    return None


def _validate_reverse_complement_input(fixture: Any) -> tuple[dict[str, str], dict[str, list[int]]]:
    """Validate only the reverse-complement-specific input contract.

    The shared fixture loader intentionally remains unchanged for the other
    tools. Reverse-complement needs a stricter contract because an ambiguous
    base or an unlabelled coordinate range cannot be safely handed to a
    downstream consumer.
    """

    if not isinstance(fixture, dict):
        raise ValueError("reverse-complement input must be a JSON object")

    sequences = fixture.get("sequences")
    if not isinstance(sequences, dict):
        raise ValueError("reverse-complement input field 'sequences' must be an object mapping IDs to DNA strings")

    validated_sequences: dict[str, str] = {}
    for sequence_id, sequence in sequences.items():
        if not isinstance(sequence_id, str) or not sequence_id:
            raise ValueError("reverse-complement sequence IDs must be non-empty strings")
        if not isinstance(sequence, str):
            raise ValueError(f"reverse-complement sequence '{sequence_id}' must be a DNA string")
        invalid = sorted(set(sequence) - _DNA_ALPHABET)
        if invalid:
            symbols = ", ".join(repr(symbol) for symbol in invalid)
            raise ValueError(
                f"reverse-complement sequence '{sequence_id}' contains unsupported symbol(s): {symbols}; "
                "only uppercase A, C, G, T are accepted"
            )
        validated_sequences[sequence_id] = sequence

    raw_windows = fixture.get("aa_windows", {})
    if not isinstance(raw_windows, dict):
        raise ValueError("reverse-complement input field 'aa_windows' must be an object of [start, end) ranges")

    validated_windows: dict[str, list[int]] = {}
    for window_id, bounds in raw_windows.items():
        if not isinstance(window_id, str) or not window_id:
            raise ValueError("reverse-complement AA window IDs must be non-empty strings")
        if not isinstance(bounds, (list, tuple)) or len(bounds) != 2 or not all(_is_integer(value) for value in bounds):
            raise ValueError(
                f"reverse-complement AA window '{window_id}' must be a two-element integer range [start, end)"
            )
        start, end = int(bounds[0]), int(bounds[1])
        if start < 0 or end < start:
            raise ValueError(
                f"reverse-complement AA window '{window_id}' must satisfy 0 <= start <= end; got [{start}, {end})"
            )
        validated_windows[window_id] = [start, end]

    coordinate_metadata = fixture.get("coordinate_metadata")
    if coordinate_metadata is not None and not isinstance(coordinate_metadata, dict):
        raise ValueError("reverse-complement coordinate_metadata must be an object when provided")
    if coordinate_metadata and "aa_windows" not in coordinate_metadata and validated_windows:
        raise ValueError(
            "reverse-complement coordinate_metadata.aa_windows is required to transform aa_windows; "
            "the tool will not infer unit, frame, or strand"
        )
    if coordinate_metadata and "aa_windows" in coordinate_metadata:
        metadata = coordinate_metadata["aa_windows"]
        if not isinstance(metadata, dict):
            raise ValueError("reverse-complement coordinate_metadata.aa_windows must be an object")
        required = {
            "unit": "amino_acid",
            "range": "0-based-half-open",
            "strand": "source",
            "reference": "sequences",
            "codon_width_nt": 3,
        }
        mismatches = {
            key: {"expected": expected, "received": metadata.get(key)}
            for key, expected in required.items()
            if metadata.get(key) != expected
        }
        if _normalize_frame_zero(metadata.get("frame")) is None:
            mismatches["frame"] = {"expected": 0, "received": metadata.get("frame")}
        if mismatches:
            raise ValueError(
                "reverse-complement coordinate_metadata.aa_windows does not match the required contract: "
                + json.dumps(mismatches, ensure_ascii=False, sort_keys=True)
            )
    return validated_sequences, validated_windows


def _reverse_complement_records(sequences: dict[str, str]) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for sequence_id, sequence in sequences.items():
        reverse_complement = sequence.translate(_DNA_COMPLEMENT)[::-1]
        length_nt = len(sequence)
        source_hash = hashlib.sha256(sequence.encode("utf-8")).hexdigest()
        result_hash = hashlib.sha256(reverse_complement.encode("utf-8")).hexdigest()
        records[sequence_id] = {
            "source": {
                "object_id": sequence_id,
                "sequence_sha256": source_hash,
                "sequence_length_nt": length_nt,
                "strand": "as-provided",
                "coordinate_system": {
                    "unit": "nucleotide",
                    "range": "0-based-half-open",
                    "frame": "ungapped",
                    "range_values": [0, length_nt],
                },
            },
            "result": {
                "object_id": f"{sequence_id}::reverse-complement",
                "sequence": reverse_complement,
                "sequence_sha256": result_hash,
                "sequence_length_nt": length_nt,
                "strand": "opposite-of-source",
                "coordinate_system": {
                    "unit": "nucleotide",
                    "range": "0-based-half-open",
                    "frame": "ungapped",
                    "range_values": [0, length_nt],
                },
            },
            "relation": "reverse_complement_of",
            "base_complement": {"A": "T", "C": "G", "G": "C", "T": "A"},
            "coordinate_mapping": {
                "source_to_result_index": "result_index = sequence_length_nt - 1 - source_index",
                "source_to_result_interval": "[start, end) -> [sequence_length_nt - end, sequence_length_nt - start)",
                "source_range": [0, length_nt],
                "result_range": [0, length_nt],
                "unit": "nucleotide",
                "source_range_semantics": "0-based-half-open",
                "result_range_semantics": "0-based-half-open",
                "orientation": "reversed",
            },
            "unknown_semantics": {
                "biological_plus_minus": "unspecified; source strand is treated as the input orientation",
                "ambiguous_bases": "rejected; only uppercase A, C, G, T are accepted",
                "gaps": "rejected",
                "translation_frame": "not applicable; this operation does not translate DNA",
                "denominator": "not applicable; this operation does not calculate a ratio",
            },
        }
    return records


def _coordinate_transforms(
    sequences: dict[str, str],
    aa_windows: dict[str, list[int]],
    coordinate_metadata: Any,
) -> dict[str, Any]:
    if not aa_windows:
        return {"status": "not_applicable", "reason_code": "no_aa_windows"}
    metadata = coordinate_metadata.get("aa_windows") if isinstance(coordinate_metadata, dict) else None
    if metadata is None:
        return {
            "status": "not_derived",
            "reason_code": "missing_coordinate_metadata",
            "required_field": "coordinate_metadata.aa_windows",
            "required_contract": {
                "unit": "amino_acid",
                "range": "0-based-half-open",
                "frame": 0,
                "strand": "source",
                "reference": "sequences",
                "codon_width_nt": 3,
            },
            "windows": [],
        }

    windows: list[dict[str, Any]] = []
    for sequence_id, sequence in sequences.items():
        length_nt = len(sequence)
        for window_id, (aa_start, aa_end) in aa_windows.items():
            nt_start, nt_end = 3 * aa_start, 3 * aa_end
            if nt_end > length_nt:
                raise ValueError(
                    f"reverse-complement AA window '{window_id}' [{aa_start}, {aa_end}) "
                    f"maps beyond sequence '{sequence_id}' length {length_nt} nt"
                )
            windows.append(
                {
                    "source_id": sequence_id,
                    "window_id": window_id,
                    "source": {
                        "unit": "amino_acid",
                        "range": "0-based-half-open",
                        "frame": 0,
                        "strand": "source",
                        "aa_range": [aa_start, aa_end],
                        "nt_range": [nt_start, nt_end],
                    },
                    "result": {
                        "unit": "nucleotide",
                        "range": "0-based-half-open",
                        "frame": "ungapped",
                        "strand": "opposite-of-source",
                        "nt_range": [length_nt - nt_end, length_nt - nt_start],
                    },
                    "mapping": "[nt_start, nt_end) -> [sequence_length_nt - nt_end, sequence_length_nt - nt_start)",
                }
            )
    return {
        "status": "derived",
        "precondition": "coordinate_metadata.aa_windows explicitly declares frame-zero source AA ranges",
        "windows": windows,
    }


def tool_reverse_complement(fixture: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    sequences, aa_windows = _validate_reverse_complement_input(fixture)
    records = _reverse_complement_records(sequences)
    return {
        # Preserve the V0 scientific payload exactly for valid inputs.
        "reverse_complement_sha256": {
            sequence_id: record["result"]["sequence_sha256"] for sequence_id, record in records.items()
        },
        "compatibility": {
            "schema_version": 1,
            "schema_id": _REVERSE_COMPLEMENT_SCHEMA,
            "operation": "reverse_complement",
            "identity_rule": "source sequence ID plus SHA-256 of the exact UTF-8 DNA string",
            "records": records,
            "coordinate_transforms": _coordinate_transforms(
                sequences,
                aa_windows,
                fixture.get("coordinate_metadata"),
            ),
            "rows_semantics": "rows are accepted for the frozen CLI shape but are not used by this operation",
        },
    }


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


TOOL_FUNCTIONS = {'reverse-complement': tool_reverse_complement}


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
    fixture = json.loads(input_bytes)
    rows = rows_from_file(args.rows)
    payload = TOOL_FUNCTIONS[args.tool](fixture, rows)
    result = {"tool_id": args.tool, "input_sha256": sha256_bytes(input_bytes), "rows_sha256": sha256_bytes(Path(args.rows).read_bytes()) if args.rows else None, "payload": payload}
    output_path = Path(args.output)
    _write_json_artifact(output_path, result)
    event = {"tool_id": args.tool, "input": args.input, "rows": args.rows, "output": args.output, "input_sha256": result["input_sha256"], "output_sha256": sha256_bytes(output_path.read_bytes()), "started_epoch": started, "finished_epoch": time.time(), "exit_code": 0}
    with (Path(args.run_root) / "tool_calls.jsonl").open("a", encoding="utf-8") as handle:
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
