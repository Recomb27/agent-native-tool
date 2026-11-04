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


DEFAULT_RESTRICTION_ENZYMES = (
    ("EcoRI", "GAATTC"),
    ("BamHI", "GGATCC"),
    ("HindIII", "AAGCTT"),
)
DNA_BASES = frozenset("ACGT")
RESTRICTION_COMPATIBILITY_SCHEMA = "restriction-sites.compatibility.v1"


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


def rows_from_bytes(raw: bytes, rows_path: str | Path | None = None) -> list[dict[str, Any]]:
    """Parse optional row evidence from the exact bytes read by the CLI."""
    if rows_path is not None and Path(rows_path) == Path("/dev/null"):
        return []
    if not raw.strip():
        return []
    rows = json.loads(raw)
    if not isinstance(rows, list):
        raise ValueError("Rows input must be a JSON list")
    return rows


def rows_from_file(rows_path: str | Path | None) -> list[dict[str, Any]]:
    """Load optional row evidence. Empty files and /dev/null mean no rows."""
    if rows_path is None:
        return []
    path = Path(rows_path)
    return rows_from_bytes(path.read_bytes(), path)


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


def tool_msa_statistics(fixture: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    proteins = [translate(seq) for seq in fixture["sequences"].values()]
    columns = []
    for i in range(len(proteins[0])):
        counts = collections.Counter(protein[i] for protein in proteins)
        total = sum(counts.values())
        entropy = -sum((n / total) * math.log2(n / total) for n in counts.values())
        columns.append({"position": i, "consensus": counts.most_common(1)[0][0], "entropy_bits": entropy})
    return {"alignment_length": len(proteins[0]), "columns": columns}


def _restriction_sequences(fixture: dict[str, Any]) -> dict[str, str]:
    if not isinstance(fixture, dict):
        raise ValueError("restriction-sites: input must be a JSON object")
    sequences = fixture.get("sequences")
    if not isinstance(sequences, dict):
        raise ValueError("restriction-sites: input 'sequences' must be an object")

    validated: dict[str, str] = {}
    for sequence_id, sequence in sequences.items():
        if not isinstance(sequence_id, str) or not sequence_id:
            raise ValueError("restriction-sites: every sequence id must be a non-empty string")
        if not isinstance(sequence, str):
            raise ValueError(f"restriction-sites: sequence {sequence_id!r} must be a string")
        invalid = sorted(set(sequence) - DNA_BASES)
        if invalid:
            rendered = ", ".join(repr(base) for base in invalid)
            raise ValueError(
                f"restriction-sites: sequence {sequence_id!r} contains {rendered}; "
                "expected uppercase DNA bases A, C, G, or T"
            )
        # An empty sequence is a valid boundary case and has no matches.
        validated[sequence_id] = sequence
    return validated


def _restriction_enzyme_manifest(fixture: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    if "enzyme_manifest" not in fixture:
        raw_manifest: Any = [
            {"enzyme_id": enzyme_id, "recognition_sequence": recognition_sequence}
            for enzyme_id, recognition_sequence in DEFAULT_RESTRICTION_ENZYMES
        ]
        source = "v0_default"
    else:
        raw_manifest = fixture["enzyme_manifest"]
        source = "input"

    if not isinstance(raw_manifest, list) or not raw_manifest:
        raise ValueError(
            "restriction-sites: enzyme_manifest must be a non-empty list when provided"
        )

    seen: set[str] = set()
    manifest: list[dict[str, Any]] = []
    for index, entry in enumerate(raw_manifest):
        if not isinstance(entry, dict):
            raise ValueError(f"restriction-sites: enzyme_manifest[{index}] must be an object")
        expected_keys = {"enzyme_id", "recognition_sequence"}
        if set(entry) != expected_keys:
            raise ValueError(
                f"restriction-sites: enzyme_manifest[{index}] must contain exactly "
                "enzyme_id and recognition_sequence"
            )

        enzyme_id = entry["enzyme_id"]
        if not isinstance(enzyme_id, str) or not enzyme_id or enzyme_id != enzyme_id.strip():
            raise ValueError(
                f"restriction-sites: enzyme_manifest[{index}].enzyme_id must be a "
                "non-empty identifier without surrounding whitespace"
            )
        if enzyme_id in seen:
            raise ValueError(f"restriction-sites: duplicate enzyme_id {enzyme_id!r}")

        recognition_sequence = entry["recognition_sequence"]
        if not isinstance(recognition_sequence, str) or not recognition_sequence:
            raise ValueError(
                f"restriction-sites: enzyme_manifest[{index}].recognition_sequence "
                "must be a non-empty string"
            )
        invalid = sorted(set(recognition_sequence) - DNA_BASES)
        if invalid or recognition_sequence != recognition_sequence.upper():
            rendered = ", ".join(repr(base) for base in invalid) or "lowercase characters"
            raise ValueError(
                f"restriction-sites: recognition sequence for {enzyme_id!r} contains "
                f"{rendered}; expected uppercase A, C, G, or T only"
            )

        seen.add(enzyme_id)
        manifest.append(
            {
                "enzyme_id": enzyme_id,
                "recognition_sequence": recognition_sequence,
                "recognition_length_nt": len(recognition_sequence),
            }
        )
    return source, manifest


def _restriction_site_record(
    sequence_id: str,
    sequence_sha256: str,
    sequence_length_nt: int,
    enzyme_id: str,
    recognition_sequence: str,
    start: int,
) -> dict[str, Any]:
    return {
        "sequence_id": sequence_id,
        "sequence_sha256": sequence_sha256,
        "sequence_length_nt": sequence_length_nt,
        "enzyme_id": enzyme_id,
        "recognition_sequence": recognition_sequence,
        "match_kind": "recognition_site",
        "strand": "forward",
        "match_start_0based": start,
        "match_end_0based_halfopen": start + len(recognition_sequence),
        "coordinate_system": {
            "base": 0,
            "interval": "half_open",
            "unit": "nt",
            "frame": "linear_input_dna",
        },
        "cleavage": {
            "status": "not_computed",
            "cut_positions": None,
        },
    }


def _restriction_compatibility_contract() -> dict[str, Any]:
    return {
        "schema_version": RESTRICTION_COMPATIBILITY_SCHEMA,
        "sequence_identity": {
            "id_field": "sequence_id",
            "hash_field": "sequence_sha256",
            "hash_algorithm": "sha256",
            "hash_input": "UTF-8 bytes of the exact uppercase sequence string, without whitespace",
        },
        "coordinate_system": {
            "base": 0,
            "range": "[start, end)",
            "interval": "half_open",
            "unit": "nt",
            "frame": "linear_input_dna",
            "origin": "first base of the input sequence",
        },
        "strand": {
            "scan": "forward_input_sequence_only",
            "record_value": "forward",
            "reverse_complement_matches": "not_reported",
        },
        "overlap": {
            "policy": "report_every_matching_start",
            "overlapping_matches": "included",
        },
        "ordering": {
            "sequences": "input JSON object order",
            "enzymes": "manifest order",
            "starts": "ascending within each enzyme",
            "site_records": "sequence order, then enzyme order, then start order",
        },
        "site_semantics": {
            "reported_object": "exact_recognition_sequence_match",
            "recognition_sequence_field": "recognition_sequence",
            "cleavage": "not_computed",
            "fragment_boundaries": "not_computed",
        },
        "unknown_semantics": {
            "ambiguous_input_base": "rejected",
            "reverse_complement_match": "not_reported",
            "cleavage_coordinate": "unknown_not_computed",
        },
        "denominator": {
            "applicable": False,
            "meaning": "site_records are positional events, not a percentage or count estimate",
        },
        "handoff": {
            "records_field": "site_records",
            "consumer_preconditions": [
                "verify sequence_id and sequence_sha256 against the consumer's sequence object",
                "interpret match_start_0based and match_end_0based_halfopen as a raw DNA interval",
                "reject records whose coordinate_system does not match this contract",
                "map to an amino-acid window only after applying the consumer's explicit reading frame",
            ],
        },
    }


def tool_restriction_sites(fixture: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    sequences = _restriction_sequences(fixture)
    manifest_source, manifest = _restriction_enzyme_manifest(fixture)
    restriction_sites: dict[str, dict[str, list[int]]] = {}
    site_records: dict[str, list[dict[str, Any]]] = {}

    for sequence_id, sequence in sequences.items():
        sequence_sha256 = hashlib.sha256(sequence.encode("utf-8")).hexdigest()
        legacy_hits: dict[str, list[int]] = {}
        records: list[dict[str, Any]] = []
        for enzyme in manifest:
            enzyme_id = enzyme["enzyme_id"]
            recognition_sequence = enzyme["recognition_sequence"]
            starts = [
                start
                for start in range(len(sequence))
                if sequence.startswith(recognition_sequence, start)
            ]
            legacy_hits[enzyme_id] = starts
            records.extend(
                _restriction_site_record(
                    sequence_id,
                    sequence_sha256,
                    len(sequence),
                    enzyme_id,
                    recognition_sequence,
                    start,
                )
                for start in starts
            )
        restriction_sites[sequence_id] = legacy_hits
        site_records[sequence_id] = records

    manifest_for_output = [
        {
            "enzyme_id": enzyme["enzyme_id"],
            "recognition_sequence": enzyme["recognition_sequence"],
            "recognition_length_nt": enzyme["recognition_length_nt"],
        }
        for enzyme in manifest
    ]
    return {
        # Keep the V0 payload and its position arrays intact for existing consumers.
        "restriction_sites": restriction_sites,
        "compatibility": _restriction_compatibility_contract(),
        "enzyme_manifest": {
            "source": manifest_source,
            "sha256": digest(manifest_for_output),
            "enzymes": manifest_for_output,
        },
        "site_records": site_records,
    }


TOOL_FUNCTIONS = {'restriction-sites': tool_restriction_sites}


def _append_tool_event(run_root: str | Path, event: dict[str, Any]) -> None:
    """Append one auditable success or error event, creating its parent first."""
    path = Path(run_root) / "tool_calls.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def _write_json_artifact(path: str | Path, result: dict[str, Any]) -> None:
    """Create the output parent and replace the artifact atomically."""
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.",
        suffix=".tmp",
        dir=str(output_path.parent),
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, output_path)
    finally:
        try:
            temporary_path.unlink()
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
    input_bytes: bytes | None = None
    rows_bytes: bytes | None = None
    input_hash: str | None = None
    rows_hash: str | None = None
    output_hash: str | None = None
    try:
        input_bytes = Path(args.input).read_bytes()
        input_hash = sha256_bytes(input_bytes)
        if args.rows is not None:
            rows_bytes = Path(args.rows).read_bytes()
            rows_hash = sha256_bytes(rows_bytes)
        fixture = json.loads(input_bytes)
        rows = rows_from_bytes(rows_bytes, args.rows) if args.rows is not None else []
        payload = TOOL_FUNCTIONS[args.tool](fixture, rows)
        result = {
            "tool_id": args.tool,
            "input_sha256": input_hash,
            "rows_sha256": rows_hash,
            "payload": payload,
        }
        _write_json_artifact(args.output, result)
        output_hash = sha256_bytes(Path(args.output).read_bytes())
        _append_tool_event(
            args.run_root,
            {
                "schema_version": 1,
                "status": "completed",
                "tool_id": args.tool,
                "input": args.input,
                "rows": args.rows,
                "output": args.output,
                "input_sha256": input_hash,
                "rows_sha256": rows_hash,
                "output_sha256": output_hash,
                "started_epoch": started,
                "finished_epoch": time.time(),
                "exit_code": 0,
            },
        )
    except Exception as exc:  # noqa: BLE001 - the CLI must report malformed inputs and I/O failures clearly.
        message = str(exc) or exc.__class__.__name__
        error = {"type": exc.__class__.__name__, "message": message}
        try:
            _append_tool_event(
                args.run_root,
                {
                    "schema_version": 1,
                    "status": "error",
                    "tool_id": args.tool,
                    "input": args.input,
                    "rows": args.rows,
                    "output": args.output,
                    "input_sha256": input_hash,
                    "rows_sha256": rows_hash,
                    "output_sha256": output_hash,
                    "started_epoch": started,
                    "finished_epoch": time.time(),
                    "exit_code": 2,
                    "error": error,
                },
            )
        except OSError as telemetry_error:
            error["telemetry"] = {
                "type": telemetry_error.__class__.__name__,
                "message": str(telemetry_error),
            }
        print(
            json.dumps(
                {"status": "error", "tool_id": args.tool, "error": error},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2

    print(json.dumps({"status": "ok", "tool_id": args.tool, "output": args.output}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
