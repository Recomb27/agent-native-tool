"""Biology helpers used by the benchmark."""

from __future__ import annotations

CODON_TABLE = {
    "TTT": "F",
    "TTC": "F",
    "TTA": "L",
    "TTG": "L",
    "CTT": "L",
    "CTC": "L",
    "CTA": "L",
    "CTG": "L",
    "ATT": "I",
    "ATC": "I",
    "ATA": "I",
    "ATG": "M",
    "GTT": "V",
    "GTC": "V",
    "GTA": "V",
    "GTG": "V",
    "TCT": "S",
    "TCC": "S",
    "TCA": "S",
    "TCG": "S",
    "CCT": "P",
    "CCC": "P",
    "CCA": "P",
    "CCG": "P",
    "ACT": "T",
    "ACC": "T",
    "ACA": "T",
    "ACG": "T",
    "GCT": "A",
    "GCC": "A",
    "GCA": "A",
    "GCG": "A",
    "TAT": "Y",
    "TAC": "Y",
    "TAA": "*",
    "TAG": "*",
    "CAT": "H",
    "CAC": "H",
    "CAA": "Q",
    "CAG": "Q",
    "AAT": "N",
    "AAC": "N",
    "AAA": "K",
    "AAG": "K",
    "GAT": "D",
    "GAC": "D",
    "GAA": "E",
    "GAG": "E",
    "TGT": "C",
    "TGC": "C",
    "TGA": "*",
    "TGG": "W",
    "CGT": "R",
    "CGC": "R",
    "CGA": "R",
    "CGG": "R",
    "AGT": "S",
    "AGC": "S",
    "AGA": "R",
    "AGG": "R",
    "GGT": "G",
    "GGC": "G",
    "GGA": "G",
    "GGG": "G",
}


def translate_dna_to_protein(sequence: str) -> str:
    rendered = sequence.strip().upper()
    if len(rendered) % 3 != 0:
        raise ValueError(f"DNA length must be divisible by 3, received {len(rendered)}")
    amino_acids: list[str] = []
    for offset in range(0, len(rendered), 3):
        codon = rendered[offset : offset + 3]
        if codon not in CODON_TABLE:
            raise ValueError(f"Unsupported codon {codon!r}")
        aa = CODON_TABLE[codon]
        if aa == "*":
            raise ValueError("Internal stop codon is not supported in benchmark start sequences")
        amino_acids.append(aa)
    return "".join(amino_acids)


def aa_interval_to_nt_bounds(start_aa: int, end_aa: int) -> tuple[int, int]:
    return start_aa * 3, end_aa * 3
