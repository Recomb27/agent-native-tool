"""Canonical seed/library specs derived from the approved regrouping."""

from __future__ import annotations

import re
from dataclasses import replace

from .biology import translate_dna_to_protein
from .models import LibraryDefinition, RoundDefinition, SeedDefinition


ROOT_DNA = (
    "ATGAGTGAAGTGGAGTTCTCGCATGAGTACTGGATGAGGCACGCCCTGACGTTAGCGAAGCGCGCACGTGACGAGCGTGAGGTCCCGGTGGGCGCGGTCCTGGTGCTCAACAACCGCGTCATTGGCGAAGGCTGGAACAGGGCCATCGGCCTACATGACCCAACTGCGCACGCGGAGATCATGGCGGTACGTGATGCGGCGCGCCAGGTGGGGAAGTACCGGTTGCTGGATACGACCCTGTACGTTACGTTCGAACCGTGTGTCATGTGTGCAGGGGCTATGATTCACTCGCGTATCGGGCGAGTGGTGTTCGGGGTGCGTAACTCGAAGCGTGGCGCCGCCGGCTCCTTGATGAACGTCCTGAACTACCCTGGGATGAACCACCGTGTAGAGATCACTGAGGGTATCTTGGCTGACGAGTGTGCCGCGTTGCTGTGTGATTTTTATCGTATGCCACGCCAGGTCTTCAACGCGCAGAAAAAGGCACAATCCTCGATCAAC"
)
MID_DNA = (
    "ATGAGTGAAGTGGAGTTCTCGCATGAGTACTGGATGAGGCACGCCCTGACGTTAGCGAAGCGCGCACGTGACGAGCGTGAGGTCCCGGTGGGCGCGGTCCTGGTGCTCAACAACCGCGTCATAGGCGAAGGCTGGAACAGGATGATCGGCCTAAACGATCCAACTGCACACGCGGAGCTAATGGCGGTACGTGATGCGTGTAACCAGGTGGGGAAGTACCGGTTGCTGGATACGACCCTGTACGTTACGTTCGAACCGTGTGTCATGTGTGCAGGGGCTATGATTCACTCGCGTATCGGGCGAGTGGTGTTCGGGGTGCGTAACTCGAAGCGTGGCGCCGCCGGCTCCTTGATGAACGTCCTGAACTACCCTGGGATGAACCACCGTGTAGAGATCACTGAGGGTATCTTGGCTGACGAGTGTGCCGCGTTGCTGTGTGATTTTTATCGTATGCCACGCCAGGTCTTCAACGCGCAGAAAAAGGCACAATCCTCGATCAAC"
)
R21_DNA = (
    "ATGAGTGAAGTGGAGTTCTCGCATGAGTACTGGATGAGGCACGCCCTGACGTTAGCGAAGCGCGCACGTGACGAGCGTGAGGTCCCGGTGGGCGCGGTCCTGGTGCTCAACAACCGCGTCATAGGCGAAGGCTGGAACAGGATGATCGGCCTAAACGATCCAACTGCACACGCAGAGCTAATGGCGGTACGTGATGCATGTAACCGTCTAGGGAGTTACCGGCTTCTGGAAACAACCATGTACGTTACGTTCGAACCGTGTGTCATGTGTGCAGGGGCTATGATTCACTCGCGTATCGGGCGAGTGGTGTTCGGGGTGCGTAACTCGAAGCGTGGCGCCGCCGGCTCCTTGATGAACGTCCTGAACTACCCTGGGATGAACCACCGTGTAGAGATCACTGAGGGTATCTTGGCTGACGAGTGTGCCGCGTTGCTGTGTGATTTTTATCGTATGCCACGCCAGGTCTTCAACGCGCAGAAAAAGGCACAATCCTCGATCAAC"
)
R22_DNA = (
    "ATGAGTGAAGTGGAGTTCTCGCATGAGTACTGGATGAGGCACGCCCTGACGTTAGCGAAGCGCGCACGTGACGAGCGTGAGGTCCCGGTGGGCGCGGTCCTGGTGCTCAACAACCGCGTCATAGGCGAAGGCTGGAACAGGATGATCGGCCTAAACGATCCAACTGCACACGCAGAGCTAATGGCGGTACGTGATGCATGTAACCGTCTAGGGAGTTACCGGCTTCTGGAAACAACCATGTACGTTACGTTCGAACCGTGTGTCATGTGTGCAGGGGCTATGATTCACTCGCGTATCGGGCGAGTGGTGTTCGGGGTGCGTAACTCGAAGCGTGGCGCCGCCGGCTCCTTGATGAACGTACTGAACTACCCTGGAATGAACCACAGAGTAGAAATCGTAGAGGGTATCTTGGCTGACGAGTGTGCCGCGTTGCTGTGTGATTTTTATCGTATGCCACGCCAAGTCTTCAATAAACAGAAAAAGGCAAAATCCGCAATCAAC"
)
R29_DNA = (
    "ATGAGTGAAGTGGAGTTCTCGCATGAGTACTGGATGAGGCACGCCCTGACGTTAGCGAAGCGCGCACGTGACGAGCGTGAGGTCCCGGTGGGCGCGGTCCTGGTGCTCAACAACCGCGTCATAGGCGAAGGCTGGAACAGGATGATCGGCCTAAACGATCCAACTGCACACGCAGAGCTAATGGCGGTACGTGATGCATGTAACCGTCTAGGGAGTTACCGGCTTCTGGAAACAACCATGTACGTTACGTTCGAACCGTGTGTCATGTGTGCAGGGGCTATGATTCACTCGCGTATCGGGCGAGTGGTGTTCGGGGTGCGTAACTCGAAGCGTGGCGCCGCCGGCTCCTTGATGAACATACTGAACTATCCAGGTATGAACCATAGAGTAGAAATAGTAGAGGGTATCTTAGCTGACGAGTGTGCAGCGTTGTTATGTGATTTTTATCGTATGCCACGCAGAGTCTTTAAAAAACAGAAACGGACAAAATCCGCAATCAAC"
)
R31_DNA = (
    "ATGAGTGAAGTGGAGTTCTCGCATGAGTACTGGATGAGGCACGCCCTGACGTTAGCGAAGCGCGCACGTGACGAGCGTGAGGTCCCGGTGGGCGCGGTCCTGGTGCTCAACAACCGCGTCATAGGCGAAGGCTGGAACAGGATGATCGGCCTAAACGATCCAACTGCACACGCAGAGCTAATGGCGGTACGTGATGCATGTAACCGTCTAGGGAGTTACCGGCTTCTGGAAACAACCATGTACGTTACGTTCGAACCGTGTGTCATGTGTGCAGGGGCTATGATTCACTCGCGTATCGGGCGAGTGGTGTTCGGGGTGCGTAACTCGAAGCGTGGCGCCGCCGGCTCCTTGATGAACGTCCTGAACTATCCAGGTATGAACCATAGAGTTGAAATAGTAGAGGGTATCTTAGCTGACGAGTGTGCAGCGTTGTTATGTGATTTTTATCGTATGCCACACAAAGTCTTTAAAAAACAAAAAAAGAAAAAATCCGCAATCAAC"
)

SEED_RE = re.compile(r"^seed(\d+)$")
LIBRARY_RE = re.compile(r"^seed(\d+)_library(\d+)$")

SEED_START_SEQUENCE_DNA = {
    "seed1": ROOT_DNA,
    "seed2": MID_DNA,
    "seed3": MID_DNA,
    "seed4": R21_DNA,
    "seed5": R22_DNA,
    "seed6": R29_DNA,
    "seed7": R31_DNA,
}

# 0-based inclusive / exclusive AA intervals keyed by historical round id.
ROUND_INTERVALS_AA = {
    "r1": (2, 32),
    "r2": (129, 159),
    "r3": (112, 142),
    "r4": (38, 68),
    "r5": (73, 103),
    "r6": (90, 120),
    "r7": (55, 85),
    "r8": (20, 50),
    "r9": (137, 167),
    "r10": (99, 129),
    "r11": (77, 107),
    "r12": (120, 150),
    "r13": (4, 34),
    "r14": (20, 50),
    "r15": (40, 70),
    "r16": (56, 86),
    "r17": (36, 86),
    "r18": (106, 156),
    "r19": (70, 120),
    "r20": (9, 59),
    "r21": (117, 167),
    "r22": (117, 167),
    "r23": (117, 167),
    "r24": (117, 167),
    "r25": (117, 167),
    "r26": (117, 167),
    "r27": (117, 167),
    "r28": (117, 167),
    "r29": (117, 167),
    "r30": (117, 167),
    "r31": (71, 121),
}

ROUND_LIBRARY_IDS = {
    "r1": "seed1_library1",
    "r2": "seed1_library8",
    "r3": "seed1_library7",
    "r4": "seed1_library3",
    "r5": "seed1_library5",
    "r6": "seed1_library6",
    "r7": "seed1_library4",
    "r8": "seed1_library2",
    "r9": "seed2_library8",
    "r10": "seed2_library6",
    "r11": "seed2_library5",
    "r12": "seed2_library7",
    "r13": "seed2_library1",
    "r14": "seed2_library2",
    "r15": "seed2_library3",
    "r16": "seed2_library4",
    "r17": "seed3_library2",
    "r18": "seed3_library4",
    "r19": "seed3_library3",
    "r20": "seed3_library1",
    "r21": "seed4_library1",
    "r22": "seed5_library1",
    "r23": "seed5_library2",
    "r24": "seed5_library3",
    "r25": "seed5_library4",
    "r26": "seed5_library5",
    "r27": "seed5_library6",
    "r28": "seed5_library7",
    "r29": "seed6_library1",
    "r30": "seed6_library2",
    "r31": "seed7_library1",
}

ROUND_PARENT_CANDIDATES = {
    "r1": (),
    "r2": (),
    "r3": (),
    "r4": (),
    "r5": (),
    "r6": (),
    "r7": (),
    "r8": (),
    "r9": ("r4",),
    "r10": ("r4",),
    "r11": ("r4",),
    "r12": ("r4",),
    "r13": ("r4",),
    "r14": ("r4",),
    "r15": ("r4",),
    "r16": ("r4",),
    "r17": ("r4",),
    "r18": ("r4",),
    "r19": ("r4",),
    "r20": ("r4",),
    "r21": ("r16",),
    "r22": ("r21",),
    "r23": ("r21",),
    "r24": ("r21",),
    "r25": ("r21",),
    "r26": ("r21",),
    "r27": ("r21",),
    "r28": ("r21",),
    "r29": tuple(f"r{index}" for index in range(22, 29)),
    "r30": tuple(f"r{index}" for index in range(22, 29)),
    "r31": ("r29", "r30"),
}


def round_sort_key(round_id: str) -> int:
    return int(round_id.removeprefix("r"))


def seed_sort_key(seed_id: str) -> int:
    match = SEED_RE.match(seed_id)
    if match is None:
        raise ValueError(f"Unsupported seed id {seed_id!r}")
    return int(match.group(1))


def library_sort_key(library_id: str) -> tuple[int, int]:
    match = LIBRARY_RE.match(library_id)
    if match is None:
        raise ValueError(f"Unsupported library id {library_id!r}")
    return int(match.group(1)), int(match.group(2))


def raw_domain_id_to_round_id(raw_domain_id: int) -> str:
    return f"r{raw_domain_id + 1}"


def round_id_to_raw_domain_id(round_id: str) -> int:
    return round_sort_key(round_id) - 1


def parent_candidates_for_round(round_id: str) -> tuple[str, ...]:
    try:
        return ROUND_PARENT_CANDIDATES[round_id]
    except KeyError as exc:
        raise ValueError(f"Unsupported round id {round_id!r}") from exc


def seed_id_for_round_id(round_id: str) -> str:
    round_no = round_sort_key(round_id)
    if 1 <= round_no <= 8:
        return "seed1"
    if 9 <= round_no <= 16:
        return "seed2"
    if 17 <= round_no <= 20:
        return "seed3"
    if round_no == 21:
        return "seed4"
    if 22 <= round_no <= 28:
        return "seed5"
    if round_no in (29, 30):
        return "seed6"
    if round_no == 31:
        return "seed7"
    raise ValueError(f"Unsupported round id {round_id!r}")


def library_id_for_round_id(round_id: str) -> str:
    try:
        return ROUND_LIBRARY_IDS[round_id]
    except KeyError as exc:
        raise ValueError(f"Unsupported round id {round_id!r}") from exc


def parent_seed_candidates_for_seed(seed_id: str) -> tuple[str, ...]:
    mapping = {
        "seed1": (),
        "seed2": ("seed1",),
        "seed3": ("seed1",),
        "seed4": ("seed2",),
        "seed5": ("seed4",),
        "seed6": ("seed5",),
        "seed7": ("seed6",),
    }
    try:
        return mapping[seed_id]
    except KeyError as exc:
        raise ValueError(f"Unsupported seed id {seed_id!r}") from exc


def parent_library_candidates_for_seed(seed_id: str) -> tuple[str, ...]:
    mapping = {
        "seed1": (),
        "seed2": ("seed1_library3",),
        "seed3": ("seed1_library3",),
        "seed4": ("seed2_library4",),
        "seed5": ("seed4_library1",),
        "seed6": tuple(f"seed5_library{index}" for index in range(1, 8)),
        "seed7": ("seed6_library1", "seed6_library2"),
    }
    try:
        return mapping[seed_id]
    except KeyError as exc:
        raise ValueError(f"Unsupported seed id {seed_id!r}") from exc


def resolved_parent_library_id_for_seed(seed_id: str) -> str | None:
    mapping = {
        "seed1": None,
        "seed2": "seed1_library3",
        "seed3": "seed1_library3",
        "seed4": "seed2_library4",
        "seed5": "seed4_library1",
        "seed6": "seed5_library7",
        "seed7": "seed6_library2",
    }
    try:
        return mapping[seed_id]
    except KeyError as exc:
        raise ValueError(f"Unsupported seed id {seed_id!r}") from exc


def build_round_definitions(variant: str) -> list[RoundDefinition]:
    if variant not in {"dna", "protein"}:
        raise ValueError(f"Unsupported variant {variant!r}")

    definitions: list[RoundDefinition] = []
    for round_id in sorted(ROUND_INTERVALS_AA, key=round_sort_key):
        seed_id = seed_id_for_round_id(round_id)
        interval_start_aa, interval_end_aa = ROUND_INTERVALS_AA[round_id]
        start_sequence_dna = SEED_START_SEQUENCE_DNA[seed_id]
        start_sequence = start_sequence_dna if variant == "dna" else translate_dna_to_protein(start_sequence_dna)
        definitions.append(
            RoundDefinition(
                round_id=round_id,
                raw_domain_id=round_id_to_raw_domain_id(round_id),
                start_sequence=start_sequence,
                interval_start_aa=interval_start_aa,
                interval_end_aa=interval_end_aa,
                parent_candidates=parent_candidates_for_round(round_id),
            )
        )

    return sorted(definitions, key=lambda item: round_sort_key(item.round_id))


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


def build_seed_definitions(variant: str) -> list[SeedDefinition]:
    if variant not in {"dna", "protein"}:
        raise ValueError(f"Unsupported variant {variant!r}")

    definitions: list[SeedDefinition] = []
    ordered_round_ids = sorted(ROUND_INTERVALS_AA, key=round_sort_key)
    for seed_id in sorted(SEED_START_SEQUENCE_DNA, key=seed_sort_key):
        round_ids = tuple(round_id for round_id in ordered_round_ids if seed_id_for_round_id(round_id) == seed_id)
        start_sequence_dna = SEED_START_SEQUENCE_DNA[seed_id]
        start_sequence = start_sequence_dna if variant == "dna" else translate_dna_to_protein(start_sequence_dna)
        libraries = tuple(
            LibraryDefinition(
                seed_id=seed_id,
                library_id=library_id_for_round_id(round_id),
                raw_domain_id=round_id_to_raw_domain_id(round_id),
                historical_round_id=round_id,
                start_sequence=start_sequence,
                interval_start_aa=ROUND_INTERVALS_AA[round_id][0],
                interval_end_aa=ROUND_INTERVALS_AA[round_id][1],
            )
            for round_id in round_ids
        )
        parent_seed_candidates = parent_seed_candidates_for_seed(seed_id)
        parent_library_candidates = parent_library_candidates_for_seed(seed_id)
        parent_seed_id = parent_seed_candidates[0] if len(parent_seed_candidates) == 1 else None
        parent_library_id = resolved_parent_library_id_for_seed(seed_id)
        evidence = {
            "rule": "canonical_library_spec",
            "seed_candidates": list(parent_seed_candidates),
            "library_candidates": list(parent_library_candidates),
            "resolved_parent_library_id": parent_library_id,
        }
        if not parent_seed_candidates:
            evidence = {"rule": "root_seed"}
        definitions.append(
            SeedDefinition(
                seed_id=seed_id,
                start_sequence=start_sequence,
                parent_seed_candidates=parent_seed_candidates,
                parent_seed_id=parent_seed_id,
                parent_library_candidates=parent_library_candidates,
                parent_library_id=parent_library_id,
                parent_resolution_evidence=evidence,
                historical_round_ids=tuple(round_ids),
                libraries=libraries,
            )
        )

    return sorted(_attach_unlock_edges(definitions), key=lambda item: seed_sort_key(item.seed_id))
