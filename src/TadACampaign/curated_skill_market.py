"""Operator-side preparation of the curated local skill market."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CuratedSkillSpec:
    skill_id: str
    display_name: str
    summary: str
    description: str
    tags: tuple[str, ...]
    source_repo: str
    source_checkout_dir: str
    source_path: str

    @property
    def package_root_relative(self) -> str:
        return f"sources/{self.skill_id}"


FIXED_SKILL_PRICE = 1000.0
MARKET_ID = "TadACampaign-launch21"

LAUNCH_SKILL_SPECS: tuple[CuratedSkillSpec, ...] = (
    CuratedSkillSpec(
        skill_id="pds-protein-qc",
        display_name="Protein QC",
        summary="Filter and rank candidates with protein-design quality-control heuristics.",
        description="Preview card only. Full thresholds, ranking logic, and example workflows unlock after purchase.",
        tags=("protein", "qc", "ranking"),
        source_repo="adaptyvbio/protein-design-skills",
        source_checkout_dir="protein-design-skills",
        source_path="skills/protein-qc",
    ),
    CuratedSkillSpec(
        skill_id="bioskills-geometric-analysis",
        display_name="Geometric Analysis",
        summary="Measure RMSD, distances, angles, contacts, and other structure geometry signals.",
        description="Preview card only. Full local-analysis examples unlock after purchase.",
        tags=("structure", "geometry", "analysis"),
        source_repo="GPTomics/bioSkills",
        source_checkout_dir="bioSkills",
        source_path="structural-biology/geometric-analysis",
    ),
    CuratedSkillSpec(
        skill_id="pds-campaign-manager",
        display_name="Campaign Manager",
        summary="Turn high-level protein-design goals into staged campaign plans and health checks.",
        description="Preview card only. Full campaign orchestration guidance unlocks after purchase.",
        tags=("planning", "campaign", "orchestration"),
        source_repo="adaptyvbio/protein-design-skills",
        source_checkout_dir="protein-design-skills",
        source_path="skills/campaign-manager",
    ),
    CuratedSkillSpec(
        skill_id="pds-binder-design",
        display_name="Binder Design",
        summary="Choose between binder-design routes and tool families for the current target.",
        description="Preview card only. Full route-selection guidance unlocks after purchase.",
        tags=("binder", "workflow", "selection"),
        source_repo="adaptyvbio/protein-design-skills",
        source_checkout_dir="protein-design-skills",
        source_path="skills/binder-design",
    ),
    CuratedSkillSpec(
        skill_id="pds-ipsae",
        display_name="ipSAE Ranking",
        summary="Rank binder candidates with interface-focused confidence and error signals.",
        description="Preview card only. Full ranking guidance unlocks after purchase.",
        tags=("binder", "ranking", "structure"),
        source_repo="adaptyvbio/protein-design-skills",
        source_checkout_dir="protein-design-skills",
        source_path="skills/ipsae",
    ),
    CuratedSkillSpec(
        skill_id="bioskills-sequence-properties",
        display_name="Sequence Properties",
        summary="Compute physicochemical sequence properties for fast offline sanity checks.",
        description="Preview card only. Full property-analysis recipes unlock after purchase.",
        tags=("sequence", "properties", "analysis"),
        source_repo="GPTomics/bioSkills",
        source_checkout_dir="bioSkills",
        source_path="sequence-manipulation/sequence-properties",
    ),
    CuratedSkillSpec(
        skill_id="bioskills-structure-navigation",
        display_name="Structure Navigation",
        summary="Traverse chains, residues, and atoms to inspect interfaces precisely.",
        description="Preview card only. Full structure-navigation examples unlock after purchase.",
        tags=("structure", "inspection", "analysis"),
        source_repo="GPTomics/bioSkills",
        source_checkout_dir="bioSkills",
        source_path="structural-biology/structure-navigation",
    ),
    CuratedSkillSpec(
        skill_id="bioskills-msa-statistics",
        display_name="MSA Statistics",
        summary="Measure conservation, divergence, and alignment quality from local MSAs.",
        description="Preview card only. Full MSA-statistics workflows unlock after purchase.",
        tags=("alignment", "msa", "statistics"),
        source_repo="GPTomics/bioSkills",
        source_checkout_dir="bioSkills",
        source_path="alignment/msa-statistics",
    ),
    CuratedSkillSpec(
        skill_id="bioskills-pairwise-alignment",
        display_name="Pairwise Alignment",
        summary="Compare candidate sequences directly with local pairwise-alignment workflows.",
        description="Preview card only. Full pairwise-alignment recipes unlock after purchase.",
        tags=("alignment", "sequence", "comparison"),
        source_repo="GPTomics/bioSkills",
        source_checkout_dir="bioSkills",
        source_path="alignment/pairwise-alignment",
    ),
    CuratedSkillSpec(
        skill_id="bioskills-motif-search",
        display_name="Motif Search",
        summary="Scan sequences for local motifs, patterns, and risky subsequences.",
        description="Preview card only. Full motif-search workflows unlock after purchase.",
        tags=("sequence", "motif", "screening"),
        source_repo="GPTomics/bioSkills",
        source_checkout_dir="bioSkills",
        source_path="sequence-manipulation/motif-search",
    ),
    CuratedSkillSpec(
        skill_id="pds-protein-design-workflow",
        display_name="Protein Design Workflow",
        summary="Follow an end-to-end protein-design playbook from planning through validation.",
        description="Preview card only. Full workflow guidance unlocks after purchase.",
        tags=("protein", "workflow", "planning"),
        source_repo="adaptyvbio/protein-design-skills",
        source_checkout_dir="protein-design-skills",
        source_path="skills/protein-design-workflow",
    ),
    CuratedSkillSpec(
        skill_id="pds-proteinmpnn",
        display_name="ProteinMPNN",
        summary="Use inverse-folding guidance to turn structures into candidate sequences.",
        description="Preview card only. Full ProteinMPNN usage guidance unlocks after purchase.",
        tags=("protein", "sequence-design", "inverse-folding"),
        source_repo="adaptyvbio/protein-design-skills",
        source_checkout_dir="protein-design-skills",
        source_path="skills/proteinmpnn",
    ),
    CuratedSkillSpec(
        skill_id="pds-esm",
        display_name="ESM",
        summary="Score sequence plausibility and embeddings for triage and filtering.",
        description="Preview card only. Full ESM-based analysis guidance unlocks after purchase.",
        tags=("protein", "sequence", "embeddings"),
        source_repo="adaptyvbio/protein-design-skills",
        source_checkout_dir="protein-design-skills",
        source_path="skills/esm",
    ),
    CuratedSkillSpec(
        skill_id="bioskills-codon-usage",
        display_name="Codon Usage",
        summary="Evaluate codon bias and expression-oriented sequence tradeoffs offline.",
        description="Preview card only. Full codon-usage workflows unlock after purchase.",
        tags=("sequence", "expression", "codon"),
        source_repo="GPTomics/bioSkills",
        source_checkout_dir="bioSkills",
        source_path="sequence-manipulation/codon-usage",
    ),
    CuratedSkillSpec(
        skill_id="tu-computational-biophysics",
        display_name="Computational Biophysics",
        summary="Apply quantitative biophysics templates for binding and tradeoff reasoning.",
        description="Preview card only. Full quantitative reasoning templates unlock after purchase.",
        tags=("biophysics", "quantitative", "reasoning"),
        source_repo="mims-harvard/ToolUniverse",
        source_checkout_dir="ToolUniverse",
        source_path="skills/tooluniverse-computational-biophysics",
    ),
    CuratedSkillSpec(
        skill_id="tu-sequence-analysis",
        display_name="Sequence Analysis",
        summary="Use broad sequence-analysis workflows for offline sequence triage and comparison.",
        description="Preview card only. Full sequence-analysis workflows unlock after purchase.",
        tags=("sequence", "analysis", "support"),
        source_repo="mims-harvard/ToolUniverse",
        source_checkout_dir="ToolUniverse",
        source_path="skills/tooluniverse-sequence-analysis",
    ),
    CuratedSkillSpec(
        skill_id="lsr-research-router",
        display_name="Life Science Router",
        summary="Route broad biology questions to the smallest useful evidence lanes.",
        description="Preview card only. Full routing, entity-normalization, and synthesis guidance unlock after purchase.",
        tags=("research", "routing", "evidence"),
        source_repo="openai/plugins/life-science-research",
        source_checkout_dir="life-science-research",
        source_path="skills/research-router-skill",
    ),
    CuratedSkillSpec(
        skill_id="lsr-uniprot",
        display_name="UniProt Lookup",
        summary="Retrieve concise protein function, accession, and sequence context.",
        description="Preview card only. Full UniProt request patterns unlock after purchase.",
        tags=("protein", "function", "sequence"),
        source_repo="openai/plugins/life-science-research",
        source_checkout_dir="life-science-research",
        source_path="skills/uniprot-skill",
    ),
    CuratedSkillSpec(
        skill_id="lsr-alphafold",
        display_name="AlphaFold Lookup",
        summary="Use predicted structure metadata and annotations for protein-level triage.",
        description="Preview card only. Full AlphaFold lookup patterns unlock after purchase.",
        tags=("protein", "structure", "annotation"),
        source_repo="openai/plugins/life-science-research",
        source_checkout_dir="life-science-research",
        source_path="skills/alphafold-skill",
    ),
    CuratedSkillSpec(
        skill_id="lsr-rcsb-pdb",
        display_name="RCSB PDB Lookup",
        summary="Inspect structure metadata, assemblies, and sequence records from PDB sources.",
        description="Preview card only. Full RCSB request patterns unlock after purchase.",
        tags=("structure", "protein", "metadata"),
        source_repo="openai/plugins/life-science-research",
        source_checkout_dir="life-science-research",
        source_path="skills/rcsb-pdb-skill",
    ),
    CuratedSkillSpec(
        skill_id="lsr-ncbi-blast",
        display_name="NCBI BLAST",
        summary="Compare nucleotide or protein sequences against reference databases.",
        description="Preview card only. Full BLAST submit, poll, and summarization workflow unlock after purchase.",
        tags=("sequence", "similarity", "search"),
        source_repo="openai/plugins/life-science-research",
        source_checkout_dir="life-science-research",
        source_path="skills/ncbi-blast-skill",
    ),
)


def _resolve_source_dir(source_root: Path, spec: CuratedSkillSpec) -> Path:
    source_dir = (source_root / spec.source_checkout_dir / spec.source_path).resolve()
    if not source_dir.is_dir():
        raise FileNotFoundError(f"Missing curated skill source directory: {source_dir}")
    skill_md = source_dir / "SKILL.md"
    if not skill_md.is_file():
        raise FileNotFoundError(f"Curated skill source is missing SKILL.md: {skill_md}")
    return source_dir


def prepare_curated_skill_market(source_root: Path, output_dir: Path, *, force: bool = False) -> Path:
    source_root = source_root.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    if not source_root.is_dir():
        raise FileNotFoundError(f"Curated skill source root does not exist: {source_root}")
    if output_dir.exists():
        if not force:
            raise FileExistsError(f"Curated skill market output already exists: {output_dir}")
        shutil.rmtree(output_dir)

    sources_root = output_dir / "sources"
    sources_root.mkdir(parents=True, exist_ok=True)

    manifest_payload: dict[str, object] = {
        "version": 1,
        "market_id": MARKET_ID,
        "skills": [],
    }
    audit_payload: dict[str, object] = {
        "market_id": MARKET_ID,
        "source_root": str(source_root),
        "skill_count": len(LAUNCH_SKILL_SPECS),
        "skills": [],
    }

    skills_payload = manifest_payload["skills"]
    audit_skills_payload = audit_payload["skills"]
    assert isinstance(skills_payload, list)
    assert isinstance(audit_skills_payload, list)

    for spec in LAUNCH_SKILL_SPECS:
        source_dir = _resolve_source_dir(source_root, spec)
        package_root = sources_root / spec.skill_id
        shutil.copytree(source_dir, package_root)
        skills_payload.append(
            {
                "skill_id": spec.skill_id,
                "display_name": spec.display_name,
                "price": FIXED_SKILL_PRICE,
                "summary": spec.summary,
                "description": spec.description,
                "tags": list(spec.tags),
                "source_repo": spec.source_repo,
                "source_path": spec.source_path,
                "package_root": spec.package_root_relative,
            }
        )
        audit_skills_payload.append(
            {
                "skill_id": spec.skill_id,
                "display_name": spec.display_name,
                "source_repo": spec.source_repo,
                "source_checkout_dir": spec.source_checkout_dir,
                "source_path": spec.source_path,
                "copied_from": str(source_dir),
                "copied_to": str(package_root),
            }
        )

    manifest_path = output_dir / "skill_market.json"
    manifest_path.write_text(json.dumps(manifest_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output_dir / "market_audit.json").write_text(
        json.dumps(audit_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest_path
