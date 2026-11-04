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


MOTIF_SEARCH_SCHEMA_VERSION = 2
MOTIF_SEARCH_CONTRACT_VERSION = "motif-search/v2"
DEFAULT_MOTIFS = ("ATG", "TAA", "TAG", "TGA")
STOP_CODONS = frozenset(("TAA", "TAG", "TGA"))
DNA_ALPHABET = frozenset("ACGT")
DNA_COMPLEMENT = str.maketrans("ACGT", "TGCA")


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
    sequences = _validate_motif_sequences(fixture)
    motifs, strands, frame = _motif_search_options(fixture)
    sequence_identity = {
        sequence_id: {
            "sequence_id": sequence_id,
            "json_pointer": _sequence_json_pointer(sequence_id),
            "sequence_sha256": sha256_bytes(sequence.encode("ascii")),
            "length_nt": len(sequence),
            "alphabet": "DNA",
            "case": "uppercase",
        }
        for sequence_id, sequence in sequences.items()
    }

    motif_hits: dict[str, list[dict[str, Any]]] = {}
    for sequence_id, sequence in sequences.items():
        records: list[dict[str, Any]] = []
        reverse_complement = sequence.translate(DNA_COMPLEMENT)[::-1]
        for strand in strands:
            search_sequence = sequence if strand == "forward" else reverse_complement
            search_space = "input_sequence" if strand == "forward" else "reverse_complement"
            for motif in motifs:
                for search_start in range(len(search_sequence)):
                    if not search_sequence.startswith(motif, search_start):
                        continue
                    if strand == "forward":
                        input_start = search_start
                    else:
                        input_start = len(sequence) - (search_start + len(motif))
                    input_end = input_start + len(motif)
                    records.append(
                        {
                            "hit_kind": "motif_substring",
                            "sequence_id": sequence_id,
                            "sequence_sha256": sequence_identity[sequence_id]["sequence_sha256"],
                            "motif": motif,
                            "matched_sequence": motif,
                            "input_oriented_sequence": sequence[input_start:input_end],
                            "strand": strand,
                            "strand_symbol": "+" if strand == "forward" else "-",
                            "search_space": search_space,
                            "search_interval": {
                                "start": search_start,
                                "end_exclusive": search_start + len(motif),
                            },
                            "input_interval": {
                                "start": input_start,
                                "end_exclusive": input_end,
                            },
                            "coordinate_base": 0,
                            "coordinate_unit": "nt",
                            "range_semantics": "half_open",
                            "search_frame_offset_0based": search_start % 3,
                            "frame_interpretation": "uninterpreted_substring",
                            "overlap_included": True,
                        }
                    )

        # Sort by explicit semantic fields so a custom two-strand panel is
        # deterministic too.
        records.sort(
            key=lambda record: (
                record["strand"],
                record["motif"],
                record["search_interval"]["start"],
            )
        )
        motif_hits[sequence_id] = records

    # This is the V0 projection. It deliberately contains only forward-strand
    # starts, so old consumers see the same shape and values. New consumers
    # must use motif_hits when they request the reverse strand.
    motif_positions = {
        sequence_id: {
            motif: [
                record["input_interval"]["start"]
                for record in motif_hits[sequence_id]
                if record["motif"] == motif and record["strand"] == "forward"
            ]
            for motif in motifs
        }
        for sequence_id in sequences
    }

    if frame is None:
        frame_aware_stop_hits: dict[str, Any] = {
            "status": "not_evaluated",
            "reason_code": "frame_not_supplied",
            "frame": None,
            "stop_codons": [motif for motif in motifs if motif in STOP_CODONS],
            "hits": None,
        }
    else:
        frame_hits: dict[str, list[dict[str, Any]]] = {}
        for sequence_id in sequences:
            hits: list[dict[str, Any]] = []
            for record in motif_hits[sequence_id]:
                if record["strand"] != frame["strand"] or record["motif"] not in STOP_CODONS:
                    continue
                search_start = record["search_interval"]["start"]
                if search_start < frame["offset_0based"]:
                    continue
                if (search_start - frame["offset_0based"]) % 3:
                    continue
                hits.append(
                    {
                        "hit_kind": "frame_aware_stop_codon_candidate",
                        "sequence_id": sequence_id,
                        "sequence_sha256": record["sequence_sha256"],
                        "motif": record["motif"],
                        "strand": record["strand"],
                        "input_interval": record["input_interval"],
                        "coordinate_base": 0,
                        "coordinate_unit": "nt",
                        "range_semantics": "half_open",
                        "frame_offset_0based": frame["offset_0based"],
                        "codon_index_0based": (search_start - frame["offset_0based"]) // 3,
                        "motif_hit_reference": {
                            "motif": record["motif"],
                            "strand": record["strand"],
                            "search_start": search_start,
                        },
                    }
                )
            frame_hits[sequence_id] = hits
        frame_aware_stop_hits = {
            "status": "classified",
            "reason_code": "explicit_frame_supplied",
            "frame": frame,
            "stop_codons": [motif for motif in motifs if motif in STOP_CODONS],
            "hits": frame_hits,
        }

    return {
        "motifs": list(motifs),
        "motif_positions": motif_positions,
        "motif_hits": motif_hits,
        "frame_aware_stop_hits": frame_aware_stop_hits,
        "compatibility": {
            "contract_version": MOTIF_SEARCH_CONTRACT_VERSION,
            "sequence_objects": sequence_identity,
            "motif_identity": {
                "panel": list(motifs),
                "match_type": "literal_exact_substring",
                "alphabet": "uppercase_DNA",
                "motif_lengths_nt": {motif: len(motif) for motif in motifs},
            },
            "coordinates": {
                "base": 0,
                "unit": "nt",
                "range": "half_open",
                "position_semantics": "start_offset_in_input_sequence",
                "legacy_field": "motif_positions[sequence_id][motif]",
                "rich_field": "motif_hits[*].input_interval",
            },
            "strand": {
                "searched": list(strands),
                "symbol_by_strand": {"forward": "+", "reverse_complement": "-"},
                "coordinate_reference": "input_sequence",
                "reverse_complement_mapping": "input_start = sequence_length - search_end_exclusive",
            },
            "overlap": {
                "policy": "all_valid_start_offsets",
                "overlapping_hits_included": True,
                "duplicate_policy": "one_record_per_sequence_motif_strand_start",
            },
            "frame": {
                "status": "not_evaluated" if frame is None else "explicit",
                "origin": None if frame is None else frame["origin"],
                "offset_0based": None if frame is None else frame["offset_0based"],
                "strand": None if frame is None else frame["strand"],
                "motif_hits_are_frame_agnostic": True,
                "stop_codon_hits_require_explicit_frame": True,
                "translation_result_included": False,
            },
            "count_semantics": {
                "numerator": "exact substring start records",
                "denominator": None,
                "denominator_semantics": "not_applicable_no_rate_or_fraction",
            },
            "unknown_semantics": {
                "missing_frame": "frame_aware_stop_hits.status=not_evaluated; no frame is inferred",
                "biological_function": "unknown; motif presence is not a functional annotation",
                "translation": "unknown until a frame-aware translation tool is run",
            },
            "rows": {
                "used_for_scan": False,
                "hash_recorded_in_artifact": True,
            },
        },
    }


def _sequence_json_pointer(sequence_id: str) -> str:
    escaped = sequence_id.replace("~", "~0").replace("/", "~1")
    return f"#/sequences/{escaped}"


def _validate_motif_sequences(fixture: dict[str, Any]) -> dict[str, str]:
    sequences = fixture.get("sequences")
    if not isinstance(sequences, dict):
        raise ValueError("motif-search input must contain a sequences object")
    validated: dict[str, str] = {}
    for sequence_id, sequence in sequences.items():
        if not isinstance(sequence_id, str) or not sequence_id:
            raise ValueError("sequence ids must be non-empty strings")
        if not isinstance(sequence, str):
            raise ValueError(f"sequence {sequence_id!r} must be a DNA string")
        invalid = sorted(set(sequence) - DNA_ALPHABET)
        if invalid:
            raise ValueError(
                f"sequence {sequence_id!r} contains invalid DNA symbols {invalid}; "
                "uppercase A/C/G/T are required"
            )
        validated[sequence_id] = sequence
    return validated


def _motif_search_options(
    fixture: dict[str, Any],
) -> tuple[tuple[str, ...], tuple[str, ...], dict[str, Any] | None]:
    raw_options = fixture.get("motif_search")
    if raw_options is None:
        return DEFAULT_MOTIFS, ("forward",), None
    if not isinstance(raw_options, dict):
        raise ValueError("motif_search must be an object when present")
    unknown = sorted(set(raw_options) - {"motifs", "strands", "frame"})
    if unknown:
        raise ValueError(f"motif_search has unsupported fields: {unknown}")

    raw_motifs = raw_options.get("motifs", list(DEFAULT_MOTIFS))
    if not isinstance(raw_motifs, list):
        raise ValueError("motif_search.motifs must be a list of non-empty DNA strings")
    motifs: list[str] = []
    for motif in raw_motifs:
        if not isinstance(motif, str) or not motif:
            raise ValueError("motif_search.motifs entries must be non-empty strings")
        if motif != motif.upper() or set(motif) - DNA_ALPHABET:
            raise ValueError(f"motif {motif!r} must contain uppercase A/C/G/T only")
        if motif in motifs:
            raise ValueError(f"motif_search.motifs contains duplicate motif {motif!r}")
        motifs.append(motif)

    raw_strands = raw_options.get("strands", ["forward"])
    if not isinstance(raw_strands, list):
        raise ValueError("motif_search.strands must be a list")
    strands: list[str] = []
    for strand in raw_strands:
        if strand not in {"forward", "reverse_complement"}:
            raise ValueError(
                f"unsupported strand {strand!r}; use 'forward' or 'reverse_complement'"
            )
        if strand in strands:
            raise ValueError(f"motif_search.strands contains duplicate strand {strand!r}")
        strands.append(strand)

    raw_frame = raw_options.get("frame")
    if raw_frame is None:
        return tuple(motifs), tuple(strands), None
    if not isinstance(raw_frame, dict):
        raise ValueError("motif_search.frame must be an object or null")
    unknown_frame = sorted(set(raw_frame) - {"origin", "strand", "offset_0based"})
    if unknown_frame:
        raise ValueError(f"motif_search.frame has unsupported fields: {unknown_frame}")
    if raw_frame.get("origin") != "sequence_start":
        raise ValueError("motif_search.frame.origin must be 'sequence_start'")
    frame_strand = raw_frame.get("strand")
    if frame_strand not in strands:
        raise ValueError("motif_search.frame.strand must be one of the searched strands")
    offset = raw_frame.get("offset_0based")
    if isinstance(offset, bool):
        raise ValueError(
            "motif_search.frame.offset_0based must be an integer in {0, 1, 2} "
            "or the exact string '0', '1', or '2'"
        )
    if isinstance(offset, int) and offset in {0, 1, 2}:
        normalized_offset = offset
    elif isinstance(offset, str) and offset in {"0", "1", "2"}:
        # Some campaign input builders serialize numeric frame fields as strings.
        # Only the exact canonical spellings are unambiguous; do not coerce
        # whitespace, signs, decimals, or other string representations.
        normalized_offset = int(offset)
    else:
        raise ValueError(
            "motif_search.frame.offset_0based must be an integer in {0, 1, 2} "
            "or the exact string '0', '1', or '2'"
        )
    return tuple(motifs), tuple(strands), {
        "origin": "sequence_start",
        "strand": frame_strand,
        "offset_0based": normalized_offset,
    }


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


TOOL_FUNCTIONS = {'motif-search': tool_motif_search}


def _append_tool_event(run_root: str | Path, event: dict[str, Any]) -> None:
    """Best-effort local event recording for both success and input errors."""

    try:
        path = Path(run_root) / "tool_calls.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        # The computation result and its stderr remain the primary evidence if
        # the caller supplied an unusable telemetry root.
        return


def _write_json_artifact(output_path: Path, result: dict[str, Any]) -> None:
    """Create the output tree and publish a complete artifact atomically."""

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
    input_bytes: bytes | None = None
    rows_bytes: bytes | None = None
    input_hash: str | None = None
    rows_hash: str | None = None
    try:
        input_bytes = Path(args.input).read_bytes()
        input_hash = sha256_bytes(input_bytes)
        if args.rows:
            rows_bytes = Path(args.rows).read_bytes()
            rows_hash = sha256_bytes(rows_bytes)
        fixture = json.loads(input_bytes)
        if not isinstance(fixture, dict):
            raise ValueError("The tool input must be a JSON object")
        rows = rows_from_file(args.rows)
        payload = TOOL_FUNCTIONS[args.tool](fixture, rows)
        result: dict[str, Any] = {
            "tool_id": args.tool,
            "input_sha256": input_hash,
            "rows_sha256": rows_hash,
            "payload": payload,
        }
        if args.tool == "motif-search":
            result = {
                "schema_version": MOTIF_SEARCH_SCHEMA_VERSION,
                "status": "success",
                **result,
            }
        output_path = Path(args.output)
        _write_json_artifact(output_path, result)
        event = {
            "schema_version": MOTIF_SEARCH_SCHEMA_VERSION if args.tool == "motif-search" else 1,
            "status": "completed",
            "tool_id": args.tool,
            "input": args.input,
            "rows": args.rows,
            "output": args.output,
            "input_sha256": input_hash,
            "rows_sha256": rows_hash,
            "output_sha256": sha256_bytes(output_path.read_bytes()),
            "started_epoch": started,
            "finished_epoch": time.time(),
            "exit_code": 0,
        }
        _append_tool_event(args.run_root, event)
        print(json.dumps({"status": "ok", "tool_id": args.tool, "output": args.output}, ensure_ascii=False))
        return 0
    except Exception as exc:  # noqa: BLE001 - the CLI must report malformed fixtures clearly.
        message = str(exc) or exc.__class__.__name__
        _append_tool_event(
            args.run_root,
            {
                "schema_version": MOTIF_SEARCH_SCHEMA_VERSION if args.tool == "motif-search" else 1,
                "status": "error",
                "tool_id": args.tool,
                "input": args.input,
                "rows": args.rows,
                "output": args.output,
                "input_sha256": input_hash,
                "rows_sha256": rows_hash,
                "started_epoch": started,
                "finished_epoch": time.time(),
                "exit_code": 2,
                "error": {"type": exc.__class__.__name__, "message": message},
            },
        )
        print(
            json.dumps(
                {
                    "status": "error",
                    "tool_id": args.tool,
                    "error": {"type": exc.__class__.__name__, "message": message},
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
