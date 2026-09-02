# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional dependency in dev only
    load_dotenv = None


BASE_DIR = Path(__file__).resolve().parent.parent
API_DIR = Path(__file__).resolve().parent

if load_dotenv:
    load_dotenv(BASE_DIR / ".env", override=False)
    load_dotenv(API_DIR / ".env", override=False)


APP_NAME = "AGVM Lab API"
APP_VERSION = "0.5.0"

DATA_DIR = Path(os.getenv("AGVM_LAB_DATA_DIR", API_DIR / "data"))

try:
    from setup_env import load_managed_env_into_process

    load_managed_env_into_process(override=True)
except Exception:  # pragma: no cover - setup env must never block module import.
    pass

SQLITE_PATH = DATA_DIR / "beta_vector_memory.sqlite3"
GRAPH_PATH = DATA_DIR / "beta_vector_memory.graph.json"
INDEX_PATH = DATA_DIR / "beta_vector_memory.index.json"
ATLAS_PATH = DATA_DIR / "beta_vector_memory.atlas.json"
GRAPH_VIEW_PATH = DATA_DIR / "beta_vector_memory.graph.view.json"

GRAPH_VERSION = "agvm_lab_v6_1"
INDEX_VERSION = "agvm_lab_index_v2"
ATLAS_VERSION = "agvm_lab_atlas_v2"
COARSE_BUCKET_SIZE = 0.20
FINE_BUCKET_SIZE = 0.08
BUCKET_SIZE = FINE_BUCKET_SIZE

DEFAULT_OPENAI_MODEL = os.getenv("AGVM_LLM_MODEL", "gpt-5")
DEFAULT_COMPILER_MODEL = os.getenv("AGVM_COMPILER_MODEL", "gpt-4o-mini")
DEFAULT_RETRIEVAL_MODEL = os.getenv("AGVM_RETRIEVAL_MODEL", DEFAULT_OPENAI_MODEL)
DEFAULT_ANSWER_MODEL = os.getenv("AGVM_ANSWER_MODEL", DEFAULT_OPENAI_MODEL)
DEFAULT_SLEEP_MODEL = os.getenv("AGVM_SLEEP_MODEL", DEFAULT_COMPILER_MODEL)
DEFAULT_PLANNER_MODEL = os.getenv("AGVM_PLANNER_MODEL", DEFAULT_RETRIEVAL_MODEL)
# The coordinate planner's strict schema is certified against the compact
# structured-output model.  Keep the rest of Search on the configured
# retrieval model while making a fresh install reliable without extra tuning.
DEFAULT_AI_SPATIAL_MODEL = os.getenv("AGVM_AI_SPATIAL_MODEL", "gpt-5-mini")
DEFAULT_BRANCH_CONTROLLER_MODEL = os.getenv("AGVM_BRANCH_CONTROLLER_MODEL", DEFAULT_RETRIEVAL_MODEL)
DEFAULT_EVIDENCE_JUDGE_MODEL = os.getenv("AGVM_EVIDENCE_JUDGE_MODEL", DEFAULT_RETRIEVAL_MODEL)
DEFAULT_MASTER_MODEL = os.getenv("AGVM_MASTER_MODEL", DEFAULT_ANSWER_MODEL)
DEFAULT_GROW_SEMANTIC_MODEL = os.getenv("AGVM_GROW_SEMANTIC_MODEL", DEFAULT_RETRIEVAL_MODEL)
DEFAULT_CLONE_APP_ARBITER_MODEL = os.getenv("AGVM_CLONE_APP_ARBITER_MODEL", DEFAULT_RETRIEVAL_MODEL)
DEFAULT_CLONE_APP_SUFFICIENCY_MODEL = os.getenv("AGVM_CLONE_APP_SUFFICIENCY_MODEL", DEFAULT_ANSWER_MODEL)
DEFAULT_CLONE_APP_SPEAKER_MODEL = os.getenv("AGVM_CLONE_APP_SPEAKER_MODEL", DEFAULT_ANSWER_MODEL)
DEFAULT_CLONE_APP_PREFETCH_MODEL = os.getenv("AGVM_CLONE_APP_PREFETCH_MODEL", DEFAULT_RETRIEVAL_MODEL)
DEFAULT_CLONE_APP_TEACH_MODEL = os.getenv("AGVM_CLONE_APP_TEACH_MODEL", DEFAULT_COMPILER_MODEL)

ROUTING_FIELDS = [
    "self_core",
    "values",
    "identity_style",
    "projectual",
    "technical",
    "operational",
    "documental",
    "conceptual",
    "meta",
    "relational",
    "emotional",
    "episodic",
]

FACET_FIELDS = [
    "temporal_scope",
    "abstraction_level",
    "planning_horizon",
    "agency",
    "intimacy",
    "institutional_vs_personal",
    "source_reliability",
    "expression_intensity",
    "role_density",
    "modality_bias",
    "identity_centrality",
    "recurrence_strength",
]

ROUTING_A = [
    [0.10, 0.05, 0.00, 0.95, 0.90, 0.75, 0.65, 0.20, 0.10, -0.50, -0.40, -0.10],
    [0.05, 0.15, 0.10, 0.20, -0.55, 0.10, -0.20, 0.25, 0.40, 0.85, 0.30, -0.80],
    [0.85, 0.70, 0.55, 0.25, 0.20, -0.10, -0.75, 0.65, 0.95, 0.10, 0.50, -0.60],
]

GUIDE_AREAS = {
    "Identity": ("identity", "who i am", "myself", "sono"),
    "Projects": ("project", "roadmap", "startup", "build", "product"),
    "Relationships": ("team", "family", "partner", "relationship", "customer", "collaborator"),
    "Expression": ("style", "tone", "voice", "expression", "communication"),
    "Values": ("value", "values", "principle", "principi", "valori", "cosa conta", "what matters"),
    "History": ("history", "story", "timeline", "storia", "passato", "biografia", "background"),
    "Operational": ("workflow", "process", "procedure", "operativo", "execution"),
    "Generation Conditioning": ("rule", "prompt", "instruction", "conditioning"),
    "Media Signals": ("document", "transcript", "interview", "source", "video"),
    "Open Questions": ("unknown", "question", "missing"),
    "Knowledge": ("concept", "framework", "definition", "knowledge", "theory"),
}

CLAIM_MEMORY_TYPES = {
    "fact": "knowledge",
    "identity_claim": "identity",
    "value_claim": "value",
    "style_claim": "identity_style",
    "project_claim": "project",
    "relationship_claim": "relational",
    "event_claim": "episodic",
}

ENTITY_MEMORY_TYPES = {
    "person": "relational",
    "organization": "project",
    "project": "project",
    "document": "document_anchor",
}
