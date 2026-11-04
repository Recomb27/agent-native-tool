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


def tool_reverse_complement(fixture: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    complement = str.maketrans("ACGT", "TGCA")
    return {"reverse_complement_sha256": {key: hashlib.sha256(seq.translate(complement)[::-1].encode()).hexdigest() for key, seq in fixture["sequences"].items()}}


def tool_sequence_statistics(fixture: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    sequences = fixture["sequences"]
    return {"n_sequences": len(sequences), "unique_dna_sequences": len(set(sequences.values())), "unique_proteins": len({translate(seq) for seq in sequences.values()}), "length_distribution": dict(collections.Counter(map(len, sequences.values())))}


DEFAULT_FILTER_PREDICATES = {
    "length_nt": {"operator": "eq", "expected": 501},
    "alphabet": {
        "operator": "subset_of",
        "allowed_symbols": "ACGT",
        "case_sensitive": True,
    },
    "internal_stop_codons": {
        "frame": 0,
        "exclude_terminal_codon": True,
        "max_count": 0,
    },
}


def _copy_predicates(predicates: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(predicates))


def _require_exact_keys(value: Any, expected: set[str], path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be an object")
    actual = set(value)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing keys: {', '.join(missing)}")
        if extra:
            details.append(f"unknown keys: {', '.join(extra)}")
        raise ValueError(f"{path} has an invalid contract ({'; '.join(details)})")
    return value


def _validated_filter_predicates(fixture: dict[str, Any]) -> tuple[dict[str, Any], str]:
    if "filter_predicates" not in fixture:
        return _copy_predicates(DEFAULT_FILTER_PREDICATES), "default_v0"

    raw = _require_exact_keys(
        fixture["filter_predicates"],
        {"length_nt", "alphabet", "internal_stop_codons"},
        "filter_predicates",
    )
    length = _require_exact_keys(raw["length_nt"], {"operator", "expected"}, "filter_predicates.length_nt")
    if length["operator"] != "eq":
        raise ValueError("filter_predicates.length_nt.operator must be 'eq'")
    if isinstance(length["expected"], bool) or not isinstance(length["expected"], int) or length["expected"] < 0:
        raise ValueError("filter_predicates.length_nt.expected must be a non-negative integer")

    alphabet = _require_exact_keys(
        raw["alphabet"],
        {"operator", "allowed_symbols", "case_sensitive"},
        "filter_predicates.alphabet",
    )
    if alphabet["operator"] != "subset_of":
        raise ValueError("filter_predicates.alphabet.operator must be 'subset_of'")
    allowed = alphabet["allowed_symbols"]
    if not isinstance(allowed, str) or not allowed or allowed != allowed.upper() or len(set(allowed)) != len(allowed):
        raise ValueError("filter_predicates.alphabet.allowed_symbols must be a non-empty uppercase string without duplicates")
    if set(allowed) - set("ACGT"):
        raise ValueError("filter_predicates.alphabet.allowed_symbols must be a subset of canonical DNA symbols ACGT")
    if alphabet["case_sensitive"] is not True:
        raise ValueError("filter_predicates.alphabet.case_sensitive must be true; lowercase coercion is unsupported")

    stops = _require_exact_keys(
        raw["internal_stop_codons"],
        {"frame", "exclude_terminal_codon", "max_count"},
        "filter_predicates.internal_stop_codons",
    )
    frame = stops["frame"]
    if isinstance(frame, bool):
        raise ValueError(
            "filter_predicates.internal_stop_codons.frame must be 0; boolean values are unsupported"
        )
    if isinstance(frame, int):
        if frame != 0:
            raise ValueError(
                "filter_predicates.internal_stop_codons.frame must be 0; other frames are unsupported"
            )
        normalized_frame = 0
    elif isinstance(frame, str) and frame == "0":
        # Some JSON-producing agents serialize numeric frame fields as strings.
        # Only the exact canonical spelling is unambiguous enough to normalize.
        normalized_frame = 0
    else:
        raise ValueError(
            "filter_predicates.internal_stop_codons.frame must be 0; "
            "only integer 0 or the exact string '0' is supported"
        )
    if stops["exclude_terminal_codon"] is not True:
        raise ValueError("filter_predicates.internal_stop_codons.exclude_terminal_codon must be true")
    if isinstance(stops["max_count"], bool) or not isinstance(stops["max_count"], int) or stops["max_count"] < 0:
        raise ValueError("filter_predicates.internal_stop_codons.max_count must be a non-negative integer")

    normalized = _copy_predicates(raw)
    normalized["internal_stop_codons"]["frame"] = normalized_frame
    return normalized, "explicit_input"


def _validated_filter_fixture(fixture: Any) -> dict[str, Any]:
    if not isinstance(fixture, dict):
        raise ValueError("The filter-sequences input must be a JSON object")
    if "sequences" not in fixture:
        raise ValueError("The filter-sequences input must contain sequences")
    sequences = fixture["sequences"]
    if not isinstance(sequences, dict):
        raise ValueError("sequences must be an object mapping IDs to DNA strings")
    for sequence_id, sequence in sequences.items():
        if not isinstance(sequence_id, str):
            raise ValueError("sequence IDs must be strings")
        if not isinstance(sequence, str):
            raise ValueError(f"sequence {sequence_id!r} must be a string; ambiguous object/reference data is unsupported")
    _validated_filter_predicates(fixture)
    return fixture


def _sequence_collection_identity(sequences: dict[str, str]) -> str:
    return digest([{"id": sequence_id, "sequence": sequence} for sequence_id, sequence in sequences.items()])


def _filter_sequence_record(
    sequence_id: str,
    sequence: str,
    predicates: dict[str, Any],
) -> dict[str, Any]:
    expected_length = predicates["length_nt"]["expected"]
    allowed_symbols = set(predicates["alphabet"]["allowed_symbols"])
    stop_parameters = predicates["internal_stop_codons"]
    unknown_symbols = sorted(set(sequence) - allowed_symbols)
    length_ok = len(sequence) == expected_length
    alphabet_ok = not unknown_symbols
    reasons: list[str] = []
    reason_details: list[dict[str, Any]] = []

    if not length_ok:
        reasons.append("length_not_expected")
        reason_details.append({
            "code": "length_not_expected",
            "expected_nt": expected_length,
            "observed_nt": len(sequence),
        })
    if not alphabet_ok:
        reasons.append("invalid_dna_alphabet")
        reason_details.append({
            "code": "invalid_dna_alphabet",
            "allowed_symbols": predicates["alphabet"]["allowed_symbols"],
            "unknown_symbols": unknown_symbols,
            "case_sensitive": predicates["alphabet"]["case_sensitive"],
        })

    stop_count: int | None = None
    stop_codon_positions: list[int] = []
    stop_check_status = "checked"
    if not alphabet_ok:
        stop_check_status = "not_evaluated_invalid_alphabet"
    elif len(sequence) % 3:
        stop_check_status = "not_evaluated_frame_length"
        reasons.append("length_not_divisible_by_3")
        reason_details.append({
            "code": "length_not_divisible_by_3",
            "frame": stop_parameters["frame"],
            "observed_nt": len(sequence),
            "remainder": len(sequence) % 3,
        })
    else:
        protein = translate(sequence)
        stop_count = protein[:-1].count("*")
        stop_codon_positions = [
            index for index, amino_acid in enumerate(protein[:-1]) if amino_acid == "*"
        ]
        if stop_count > stop_parameters["max_count"]:
            reasons.append("internal_stop_codon")
            reason_details.append({
                "code": "internal_stop_codon",
                "max_count": stop_parameters["max_count"],
                "observed_count": stop_count,
                "codon_positions_0based": stop_codon_positions,
                "nt_start_positions_0based": [3 * position for position in stop_codon_positions],
            })

    retained = not reasons
    return {
        "id": sequence_id,
        "input_index": None,
        "sequence_sha256": sha256_bytes(sequence.encode()),
        "length_nt": len(sequence),
        "retained": retained,
        "checks": {
            "length_nt": {
                "expected": expected_length,
                "observed": len(sequence),
                "passed": length_ok,
            },
            "alphabet": {
                "allowed_symbols": predicates["alphabet"]["allowed_symbols"],
                "observed_symbols": sorted(set(sequence)),
                "unknown_symbols": unknown_symbols,
                "passed": alphabet_ok,
            },
            "internal_stop_codons": {
                "frame": stop_parameters["frame"],
                "exclude_terminal_codon": stop_parameters["exclude_terminal_codon"],
                "max_count": stop_parameters["max_count"],
                "observed_count": stop_count,
                "codon_positions_0based": stop_codon_positions,
                "status": stop_check_status,
                "passed": stop_count is not None and stop_count <= stop_parameters["max_count"],
            },
        },
        "reject_reasons": reasons,
        "reason_details": reason_details,
    }


def tool_filter_sequences(fixture: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    fixture = _validated_filter_fixture(fixture)
    sequences = fixture["sequences"]
    predicates, predicate_source = _validated_filter_predicates(fixture)
    input_ids = list(sequences)
    collection_identity = _sequence_collection_identity(sequences)
    records = []
    passing_ids: list[str] = []
    rejected_ids: list[str] = []
    rejection_reasons: dict[str, list[str]] = {}
    for input_index, sequence_id in enumerate(input_ids):
        record = _filter_sequence_record(sequence_id, sequences[sequence_id], predicates)
        record["input_index"] = input_index
        records.append(record)
        if record["retained"]:
            passing_ids.append(sequence_id)
        else:
            rejected_ids.append(sequence_id)
            rejection_reasons[sequence_id] = record["reject_reasons"]

    return {
        # These fields retain the V0 payload and its insertion-order behavior.
        "passing_ids": passing_ids,
        "rejected_ids": rejected_ids,
        # Additive compatibility contract for downstream consumers.
        "schema_version": 2,
        "retained_ids": passing_ids,
        "expected_length": predicates["length_nt"]["expected"],
        "predicate_parameters": predicates,
        "predicate_source": predicate_source,
        "evaluated_ids": input_ids,
        "rejection_reasons": rejection_reasons,
        "sequence_records": records,
        "ordering": {
            "input_ids": input_ids,
            "passing_ids": passing_ids,
            "rejected_ids": rejected_ids,
            "rule": "JSON sequences object insertion order; no sorting or deduplication",
        },
        "source_collection": {
            "field": "sequences",
            "object_type": "ordered_mapping<string, DNA string>",
            "sequence_collection_sha256": collection_identity,
            "input_count": len(input_ids),
            "retained_count": len(passing_ids),
            "rejected_count": len(rejected_ids),
            "retained_ids": passing_ids,
            "rejected_ids": rejected_ids,
            "lossy_selection": bool(rejected_ids),
            "consumer_handoff": {
                "select_ids_from_same_input_field": True,
                "required_source_identity": collection_identity,
                "sequence_semantics": {
                    "molecule": "DNA",
                    "strand": "as provided",
                    "alphabet": predicates["alphabet"]["allowed_symbols"],
                    "coordinate_unit": "nucleotide",
                    "coordinate_range": "0-based half-open only where positions are reported",
                    "translation_frame": predicates["internal_stop_codons"]["frame"],
                },
            },
        },
    }


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


TOOL_FUNCTIONS = {'filter-sequences': tool_filter_sequences}


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
    input_bytes = Path(args.input).read_bytes()
    try:
        fixture = json.loads(input_bytes)
        rows = rows_from_file(args.rows)
        payload = TOOL_FUNCTIONS[args.tool](fixture, rows)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        if args.tool == "filter-sequences":
            print(f"filter-sequences: {exc}", file=sys.stderr)
            return 2
        raise
    result = {"tool_id": args.tool, "input_sha256": sha256_bytes(input_bytes), "rows_sha256": sha256_bytes(Path(args.rows).read_bytes()) if args.rows else None, "payload": payload}
    if args.tool == "filter-sequences":
        result["schema_version"] = 2
    output_path = Path(args.output)
    _write_json_artifact(output_path, result)
    event = {"tool_id": args.tool, "input": args.input, "rows": args.rows, "output": args.output, "input_sha256": result["input_sha256"], "output_sha256": sha256_bytes(output_path.read_bytes()), "started_epoch": started, "finished_epoch": time.time(), "exit_code": 0}
    run_root_path = Path(args.run_root)
    run_root_path.mkdir(parents=True, exist_ok=True)
    with (run_root_path / "tool_calls.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    print(json.dumps({"status": "ok", "tool_id": args.tool, "output": args.output}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
