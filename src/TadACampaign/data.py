"""Data preparation and loading."""

from __future__ import annotations

import json
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from .models import LibraryDefinition, RoundDefinition, SeedDefinition
from .specs import (
    build_round_definitions,
    library_id_for_round_id,
    library_sort_key,
    raw_domain_id_to_round_id,
    round_sort_key,
    seed_id_for_round_id,
    seed_sort_key,
)


VARIANT_TO_RAW = {
    "dna": "DNA",
    "protein": "AA",
}


def _load_variant_rows(zip_path: Path, variant: str) -> pa.Table:
    raw_code = VARIANT_TO_RAW[variant]
    zf = zipfile.ZipFile(zip_path)

    sequences: list[str] = []
    values: list[float] = []
    source_splits: list[str] = []
    raw_domain_ids: list[list[int]] = []
    round_ids: list[list[str]] = []

    for split in ("train", "val", "test"):
        parquet_name = f"data/all.{raw_code}.{split}-00000-of-00001.parquet"
        table = pq.read_table(pa.BufferReader(zf.read(parquet_name)), columns=["Sequence", "Value", "Domain"])

        split_sequences = table["Sequence"].to_pylist()
        split_values = table["Value"].to_pylist()
        split_domains = table["Domain"].to_pylist()

        for sequence, value, domains in zip(split_sequences, split_values, split_domains, strict=True):
            normalized_raw_domain_ids = sorted({int(entry) for entry in domains})
            normalized_round_ids = [raw_domain_id_to_round_id(entry) for entry in normalized_raw_domain_ids]

            sequences.append(sequence)
            values.append(float(value))
            source_splits.append(split)
            raw_domain_ids.append(normalized_raw_domain_ids)
            round_ids.append(normalized_round_ids)

    return pa.table(
        {
            "sequence": pa.array(sequences, type=pa.string()),
            "value": pa.array(values, type=pa.float64()),
            "source_split": pa.array(source_splits, type=pa.string()),
            "raw_domain_ids": pa.array(raw_domain_ids, type=pa.list_(pa.int16())),
            "round_ids": pa.array(round_ids, type=pa.list_(pa.string())),
        }
    )


def _derive_start_sequence_observations(
    table: pa.Table,
    round_definitions: list[RoundDefinition],
) -> dict[str, list[str]]:
    targets: dict[str, list[str]] = {}
    for definition in round_definitions:
        targets.setdefault(definition.start_sequence, []).append(definition.round_id)
    observations: dict[str, set[str]] = {definition.round_id: set() for definition in round_definitions}

    sequences = table["sequence"].to_pylist()
    round_lists = table["round_ids"].to_pylist()

    for sequence, membership in zip(sequences, round_lists, strict=True):
        round_ids = targets.get(sequence)
        if round_ids is None:
            continue
        for round_id in round_ids:
            observations[round_id].update(membership)

    return {
        round_id: sorted(memberships, key=round_sort_key)
        for round_id, memberships in observations.items()
        if memberships
    }


def _resolve_parent_rounds(
    round_definitions: list[RoundDefinition],
    observations: dict[str, list[str]],
) -> list[RoundDefinition]:
    resolved: list[RoundDefinition] = []
    for definition in round_definitions:
        observed_rounds = observations.get(definition.round_id, [])
        if definition.round_id not in observed_rounds:
            raise ValueError(
                f"Start sequence for {definition.round_id} was found in raw data, but its own round "
                f"is absent from observed memberships: {observed_rounds}"
            )

        if not definition.parent_candidates:
            evidence = {
                "rule": "root_round",
                "observed_rounds": observed_rounds,
            }
            resolved.append(replace(definition, parent_round_id=None, parent_resolution_evidence=evidence))
            continue

        if len(definition.parent_candidates) == 1:
            chosen_parent = definition.parent_candidates[0]
            evidence = {
                "rule": "single_candidate",
                "candidates": list(definition.parent_candidates),
                "observed_rounds": observed_rounds,
            }
            resolved.append(replace(definition, parent_round_id=chosen_parent, parent_resolution_evidence=evidence))
            continue

        earlier_candidates = [
            candidate
            for candidate in definition.parent_candidates
            if candidate in observed_rounds and round_sort_key(candidate) < round_sort_key(definition.round_id)
        ]
        if not earlier_candidates:
            earlier_candidates = [candidate for candidate in definition.parent_candidates if candidate in observed_rounds]
        if not earlier_candidates:
            raise ValueError(
                f"Unable to resolve parent for {definition.round_id}: "
                f"candidates={definition.parent_candidates}, observed_rounds={observed_rounds}"
            )

        chosen_parent = sorted(earlier_candidates, key=round_sort_key)[-1]
        evidence = {
            "rule": "latest_candidate_seen_in_start_sequence_membership",
            "candidates": list(definition.parent_candidates),
            "observed_rounds": observed_rounds,
        }
        resolved.append(replace(definition, parent_round_id=chosen_parent, parent_resolution_evidence=evidence))

    return resolved


def _attach_unlock_edges(seed_definitions: list[SeedDefinition]) -> list[SeedDefinition]:
    child_seed_ids_by_parent_library: dict[str, list[str]] = {}
    for definition in seed_definitions:
        if definition.parent_library_id is None:
            continue
        child_seed_ids_by_parent_library.setdefault(definition.parent_library_id, []).append(definition.seed_id)
    for child_seed_ids in child_seed_ids_by_parent_library.values():
        child_seed_ids.sort(key=seed_sort_key)

    enriched: list[SeedDefinition] = []
    for definition in seed_definitions:
        libraries = tuple(
            replace(
                library,
                unlock_child_seed_ids=tuple(child_seed_ids_by_parent_library.get(library.library_id, ())),
            )
            for library in sorted(definition.libraries, key=lambda item: library_sort_key(item.library_id))
        )
        enriched.append(replace(definition, libraries=libraries))
    return enriched


def _collapse_rounds_to_seeds(round_definitions: list[RoundDefinition]) -> list[SeedDefinition]:
    grouped: dict[str, list[RoundDefinition]] = {}
    for definition in round_definitions:
        grouped.setdefault(seed_id_for_round_id(definition.round_id), []).append(definition)

    seeds: list[SeedDefinition] = []
    for seed_id, definitions in sorted(grouped.items(), key=lambda item: seed_sort_key(item[0])):
        definitions.sort(key=lambda item: round_sort_key(item.round_id))
        start_sequence = definitions[0].start_sequence
        parent_seed_candidates = tuple(
            sorted(
                {seed_id_for_round_id(candidate) for definition in definitions for candidate in definition.parent_candidates},
                key=seed_sort_key,
            )
        )
        parent_library_candidates = tuple(
            sorted(
                {library_id_for_round_id(candidate) for definition in definitions for candidate in definition.parent_candidates},
                key=library_sort_key,
            )
        )
        resolved_parent_seed_ids = {
            seed_id_for_round_id(definition.parent_round_id)
            for definition in definitions
            if definition.parent_round_id is not None
        }
        if len(resolved_parent_seed_ids) > 1:
            raise ValueError(f"Seed {seed_id} collapses from rounds with conflicting parents: {resolved_parent_seed_ids}")
        resolved_parent_library_ids = {
            library_id_for_round_id(definition.parent_round_id)
            for definition in definitions
            if definition.parent_round_id is not None
        }
        if len(resolved_parent_library_ids) > 1:
            raise ValueError(
                f"Seed {seed_id} collapses from rounds with conflicting parent libraries: {resolved_parent_library_ids}"
            )

        parent_seed_id = next(iter(resolved_parent_seed_ids), None)
        parent_library_id = next(iter(resolved_parent_library_ids), None)
        rule = "collapsed_root_seed" if parent_seed_id is None else "collapsed_from_round_resolution"
        evidence = {
            "rule": rule,
            "historical_round_ids": [definition.round_id for definition in definitions],
            "round_parent_resolution": {
                definition.round_id: definition.parent_resolution_evidence for definition in definitions
            },
            "resolved_parent_library_id": parent_library_id,
        }
        libraries = tuple(
            LibraryDefinition(
                seed_id=seed_id,
                library_id=library_id_for_round_id(definition.round_id),
                raw_domain_id=definition.raw_domain_id,
                historical_round_id=definition.round_id,
                start_sequence=start_sequence,
                interval_start_aa=definition.interval_start_aa,
                interval_end_aa=definition.interval_end_aa,
            )
            for definition in definitions
        )
        seeds.append(
            SeedDefinition(
                seed_id=seed_id,
                start_sequence=start_sequence,
                parent_seed_candidates=parent_seed_candidates,
                parent_seed_id=parent_seed_id,
                parent_library_candidates=parent_library_candidates,
                parent_library_id=parent_library_id,
                parent_resolution_evidence=evidence,
                historical_round_ids=tuple(definition.round_id for definition in definitions),
                libraries=libraries,
            )
        )
    return _attach_unlock_edges(seeds)


def _seed_mapping(seed_definitions: list[SeedDefinition]) -> dict[str, dict[str, Any]]:
    mapping: dict[str, dict[str, Any]] = {}
    for seed_definition in seed_definitions:
        for library in seed_definition.libraries:
            mapping[str(library.raw_domain_id)] = {
                "historical_round_id": library.historical_round_id,
                "seed_id": seed_definition.seed_id,
                "library_id": library.library_id,
            }
    return mapping


def _library_to_payload(library: LibraryDefinition) -> dict[str, Any]:
    return {
        "library_id": library.library_id,
        "seed_id": library.seed_id,
        "historical_round_id": library.historical_round_id,
        "raw_domain_id": library.raw_domain_id,
        "start_sequence": library.start_sequence,
        "interval_start_aa": library.interval_start_aa,
        "interval_end_aa": library.interval_end_aa,
        "unlock_child_seed_ids": list(library.unlock_child_seed_ids),
    }


def _seed_to_payload(seed_definition: SeedDefinition) -> dict[str, Any]:
    return {
        "seed_id": seed_definition.seed_id,
        "start_sequence": seed_definition.start_sequence,
        "parent_seed_candidates": list(seed_definition.parent_seed_candidates),
        "parent_seed_id": seed_definition.parent_seed_id,
        "parent_library_candidates": list(seed_definition.parent_library_candidates),
        "parent_library_id": seed_definition.parent_library_id,
        "parent_resolution_evidence": seed_definition.parent_resolution_evidence,
        "historical_round_ids": list(seed_definition.historical_round_ids),
        "libraries": [
            _library_to_payload(library)
            for library in sorted(seed_definition.libraries, key=lambda item: library_sort_key(item.library_id))
        ],
    }


def _write_manifest(
    output_root: Path,
    zip_path: Path,
    variant_to_seeds: dict[str, list[SeedDefinition]],
) -> Path:
    payload = {
        "version": 3,
        "raw_source": str(zip_path),
        "domain_mapping": {str(index): raw_domain_id_to_round_id(index) for index in range(31)},
        "variants": {},
    }

    for variant, seed_definitions in variant_to_seeds.items():
        payload["variants"][variant] = {
            "data_path": f"{variant}/merged.parquet",
            "seed_mapping": _seed_mapping(seed_definitions),
            "seeds": [_seed_to_payload(seed_definition) for seed_definition in seed_definitions],
        }

    manifest_path = output_root / "manifest.json"
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest_path


def prepare_data(zip_path: Path, output_root: Path) -> Path:
    zip_path = zip_path.expanduser().resolve()
    output_root = output_root.expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    variant_to_seeds: dict[str, list[SeedDefinition]] = {}
    for variant in ("dna", "protein"):
        table = _load_variant_rows(zip_path, variant=variant)
        variant_dir = output_root / variant
        variant_dir.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, variant_dir / "merged.parquet", compression="zstd")

        round_definitions = build_round_definitions(variant)
        observations = _derive_start_sequence_observations(table, round_definitions)
        resolved_rounds = _resolve_parent_rounds(round_definitions, observations)
        resolved_seeds = _collapse_rounds_to_seeds(resolved_rounds)
        variant_to_seeds[variant] = resolved_seeds

        summary_payload = {
            "variant": variant,
            "rows": table.num_rows,
            "columns": table.column_names,
            "seeds": len(resolved_seeds),
            "libraries": sum(seed_definition.library_count for seed_definition in resolved_seeds),
        }
        (variant_dir / "summary.json").write_text(
            json.dumps(summary_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    _write_manifest(output_root, zip_path, variant_to_seeds)
    return output_root


def load_manifest(prepared_root: Path) -> dict[str, Any]:
    manifest_path = prepared_root / "manifest.json"
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def load_seed_definitions(prepared_root: Path, variant: str) -> list[SeedDefinition]:
    manifest = load_manifest(prepared_root)
    variant_payload = manifest["variants"][variant]

    definitions: list[SeedDefinition] = []
    for payload in variant_payload["seeds"]:
        libraries = tuple(
            LibraryDefinition(
                library_id=str(library_payload["library_id"]),
                seed_id=str(library_payload["seed_id"]),
                historical_round_id=str(
                    library_payload["historical_round_id"]
                    if "historical_round_id" in library_payload
                    else library_payload["legacy_round_id"]
                ),
                raw_domain_id=int(library_payload["raw_domain_id"]),
                start_sequence=str(library_payload["start_sequence"]),
                interval_start_aa=int(library_payload["interval_start_aa"]),
                interval_end_aa=int(library_payload["interval_end_aa"]),
                unlock_child_seed_ids=tuple(str(item) for item in library_payload.get("unlock_child_seed_ids", [])),
            )
            for library_payload in payload["libraries"]
        )
        definitions.append(
            SeedDefinition(
                seed_id=str(payload["seed_id"]),
                start_sequence=str(payload["start_sequence"]),
                parent_seed_candidates=tuple(str(item) for item in payload.get("parent_seed_candidates", [])),
                parent_seed_id=str(payload["parent_seed_id"]) if payload.get("parent_seed_id") is not None else None,
                parent_library_candidates=tuple(str(item) for item in payload.get("parent_library_candidates", [])),
                parent_library_id=(
                    str(payload["parent_library_id"]) if payload.get("parent_library_id") is not None else None
                ),
                parent_resolution_evidence=dict(payload.get("parent_resolution_evidence", {})),
                historical_round_ids=tuple(
                    str(item) for item in payload.get("historical_round_ids", payload.get("legacy_round_ids", []))
                ),
                libraries=libraries,
            )
        )
    return sorted(definitions, key=lambda item: seed_sort_key(item.seed_id))


def load_prepared_table(prepared_root: Path, variant: str) -> pa.Table:
    manifest = load_manifest(prepared_root)
    data_path = prepared_root / manifest["variants"][variant]["data_path"]
    return pq.read_table(data_path)
