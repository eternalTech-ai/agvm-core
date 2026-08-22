from __future__ import annotations

import argparse
import json
import re
import shutil
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path


DEFAULT_DENYLIST = "repo-policy/private-denylist.txt"
MARKER = ".agvm-public-export-marker"
DEFAULT_SCANNER_MODE = "auto"
SCANNER_NAMES = ("gitleaks", "trufflehog")

REQUIRED_PUBLIC_FILES = (
    "README.md",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "SECURITY.md",
    "CONTRIBUTING.md",
    ".env.example",
    ".github/workflows/ci.yml",
    "Dockerfile.mcp",
    "agvm_api/Dockerfile.core",
    "agvm_cockpit_prototype/Dockerfile.core",
    "sdk/python/pyproject.toml",
    "sdk/python/agvm_sdk/module_manifest.py",
    "sdk/python/agvm_sdk/module_release.py",
    "sdk/python/agvm_sdk/entitlement_lease.py",
    "sdk/python/agvm_sdk/module_runtime_license.py",
    "sdk/python/agvm_sdk/mcp_contract.py",
    "sdk/typescript/package.json",
    "sdk/typescript/src/index.ts",
    "sdk/typescript/src/moduleManifestContracts.ts",
    "sdk/typescript/src/moduleSlots.ts",
    "agvm_cockpit_prototype/src/App.tsx",
    "agvm_cockpit_prototype/tsconfig.app.json",
    "agvm_cockpit_prototype/public/README.md",
    "packages/detwin-design-tokens/package.json",
    "packages/detwin-design-tokens/README.md",
    "packages/detwin-design-tokens/scripts/generate.mjs",
    "packages/detwin-design-tokens/src/tokens.json",
    "packages/detwin-design-tokens/src/generated/tokens.css",
    "packages/detwin-design-tokens/src/generated/tokens.ts",
    "packages/detwin-design-tokens/src/generated/token-manifest.json",
    "docs/cloud-and-pro.md",
    "docs/license-and-notices.md",
    "docs/local-install.md",
    "docs/mcp-codex.md",
    "docs/mcp-claude.md",
    "docs/mcp-cursor.md",
    "docs/modules.md",
    "docs/privacy-local-vs-cloud.md",
    "docs/security-model.md",
)

FORBIDDEN_DOC_NAMES = (
    "AGVM_PRIVATE_OWNER_OPERATING_MANUAL.md",
    "AGVM_PROGRESS.md",
    "AGVM_CLOUD_PLATFORM_ARCHITECTURE.md",
    "AGVM_PUBLICATION_README_AND_REPO_HYGIENE_PLAN.md",
    "AGVM_OPEN_CORE_MODULAR_CLOUD_ROADMAP.md",
    "AGVM_PUBLIC_CORE_EXPORT_GATE.md",
    "AGVM_PUBLIC_CORE_EXPORT_REPORT.md",
    "AGVM_PLATFORM_CONTROL_PLANE_MVP.md",
    "AGVM_PLATFORM_AUTH_ACCOUNT_MODEL.md",
    "AGVM_PLATFORM_ACCOUNT_LIFECYCLE_BOUNDARY.md",
    "AGVM_PLATFORM_PROVIDER_ADAPTER_BOUNDARY.md",
    "AGVM_PLATFORM_BROWSER_LOGIN_ACCOUNT_UI.md",
    "AGVM_PLATFORM_STRIPE_SUBSCRIPTIONS.md",
    "AGVM_PLATFORM_STRIPE_TEST_MODE_READINESS.md",
    "AGVM_PLATFORM_STRIPE_TEST_MODE_EVIDENCE.md",
    "AGVM_PLATFORM_DOCKER_AWS_TOPOLOGY.md",
    "AGVM_PLATFORM_DB_POSTGRES_ADAPTER.md",
    "AGVM_PLATFORM_DB_POSTGRES_RUNTIME_SMOKE.md",
    "AGVM_PLATFORM_AUTH_PROD_SECRET_WIRING.md",
    "AGVM_PLATFORM_AWS_TERRAFORM_RUNNER_AND_IAM.md",
    "AGVM_PLATFORM_AWS_DEPLOY_ROLE_BOOTSTRAP.md",
    "AGVM_PLATFORM_AWS_PLAN_PREFLIGHT.md",
    "AGVM_PLATFORM_ACCOUNT_MODULE_INSTALL_PLAN.md",
    "AGVM_PLATFORM_LOCAL_MODULE_INSTALLER_HANDOFF.md",
    "AGVM_PLATFORM_LOCAL_SUPERVISOR_HANDOFF.md",
    "AGVM_PLATFORM_HOST_SUPERVISOR_EXECUTOR.md",
    "AGVM_PLATFORM_HOST_SUPERVISOR_HANDOFF_UX.md",
    "AGVM_PLATFORM_HOST_SUPERVISOR_EXECUTE_EVIDENCE_UX.md",
    "AGVM_PLATFORM_DESKTOP_HOST_HELPER_CONTRACT.md",
    "AGVM_PLATFORM_DESKTOP_HOST_HELPER_SKELETON.md",
    "AGVM_PLATFORM_DESKTOP_HOST_HELPER_PAIRING_STORE.md",
    "AGVM_PLATFORM_DESKTOP_HOST_HELPER_SIGNED_IPC_ENVELOPE.md",
    "AGVM_PLATFORM_DESKTOP_HOST_HELPER_DISCOVERY_UX.md",
    "AGVM_PLATFORM_DESKTOP_HOST_HELPER_PAIRING_UX.md",
    "AGVM_PLATFORM_DESKTOP_HOST_HELPER_NATIVE_CONFIRMATION_CONTRACT.md",
    "AGVM_GITHUB_PUBLICATION_PREFLIGHT.md",
    "DETWIN_MASTER_DELIVERY_PLAN.md",
    "DETWIN_AWS_DOMAIN_BOOTSTRAP_RUNBOOK.md",
    "DETWIN_PROVIDER_ACCOUNTS_AND_SECRET_CUSTODY_RUNBOOK.md",
    "DETWIN_PRODUCTION_5_SLICE_ROADMAP_2026_06_27.md",
    "AGVM_PRIVATE_MODULE_INSTALL_TOKEN_FLOW.md",
    "AGVM_PRIVATE_MODULE_REGISTRY_REDEMPTION.md",
    "AGVM_PRIVATE_MODULE_RELEASE_PUBLICATION.md",
    "AGVM_PRIVATE_ECR_CREDENTIAL_BROKER.md",
    "AGVM_PRIVATE_ECR_IMAGE_PUBLICATION.md",
    "AGVM_PRIVATE_ECR_INSTALL_PULL_SMOKE.md",
    "AGVM_PRIVATE_ECR_REPOSITORY_BOOTSTRAP.md",
    "AGVM_PRIVATE_ECR_TERRAFORM_AND_DOCKER_BROKER_UX.md",
    "DETWIN_PRODUCT_UX_AND_REPO_SPLIT_ROADMAP.md",
    "DETWIN_VISUAL_SYSTEM_UX0.md",
    "DETWIN_CURRENT_STATE_AND_TEST_MAP.md",
    "AGVM_AWS_MVP_DEPLOYMENT_IAC.md",
    "AGVM_AWS_READINESS_AUDIT.md",
    "AGVM_HOSTED_MCP_GATEWAY.md",
    "AGVM_PRO_MODULE_HARDENING.md",
    "AGVM_PUBLIC_LAUNCH_PACKAGING.md",
    "AGVM_PUBLIC_LICENSE_NOTICE_PACK.md",
    "AGVM_PUBLIC_RELEASE_DRY_RUN_REPORT.md",
    "AGVM_PUBLIC_RELEASE_DOCKER_SCANNERS.md",
    "AGVM_SDK_CONTRACT_EXTRACTION.md",
    "AGVM_CLONE_APP_PRIVATE_MODULE_SPLIT.md",
    "AGVM_CLONE_APP_PRIVATE_MODULE_EXPORT_REPORT.md",
    "AGVM_GROW_MAINTAIN_PRIVATE_MODULE_EXTRACTION.md",
    "AGVM_GROW_MAINTAIN_RUNTIME_PROXY_MIGRATION.md",
    "AGVM_GROW_MAINTAIN_RUNTIME_CONTRACT_EXTRACTION.md",
    "AGVM_GROW_SOURCE_RUNTIME_ORCHESTRATION_EXTRACTION.md",
    "AGVM_GROW_UPLOAD_RUNTIME_ORCHESTRATION_EXTRACTION.md",
    "AGVM_GROW_UPLOAD_DIRECT_SIDECAR_RUNTIME_HANDLER.md",
    "AGVM_GROW_WRITE_CLARIFICATION_RUNTIME_ORCHESTRATION_EXTRACTION.md",
    "AGVM_PRIVATE_UI_BUNDLE_MANIFEST_BOUNDARY.md",
    "AGVM_GROW_UI_SOURCE_MIRROR_BOUNDARY.md",
    "AGVM_MAINTAIN_UI_SOURCE_MIRROR_BOUNDARY.md",
    "AGVM_SHARED_UI_DEPENDENCY_INVENTORY.md",
    "AGVM_PRIVATE_SHARED_UI_SDK_BOUNDARY.md",
    "AGVM_PRIVATE_SOURCE_READER_RUNTIME_BOUNDARY.md",
    "AGVM_PRIVATE_SOURCE_PACKAGE_RUNTIME_BOUNDARY.md",
    "AGVM_PRIVATE_SOURCE_EXTRACTION_RUNTIME_BOUNDARY.md",
    "AGVM_PRIVATE_SOURCE_COMPILER_PREVIEW_RUNTIME_BOUNDARY.md",
    "AGVM_GROW_APPLY_WRITE_PERSISTENCE_ADAPTER_BOUNDARY.md",
    "AGVM_PRIVATE_MEMORY_PERSISTENCE_RUNTIME_BOUNDARY.md",
    "AGVM_CORE_MCP_MODULE_TOOL_REGISTRATION_BOUNDARY.md",
    "AGVM_LOCAL_MCP_MODULE_LEASE_VISIBILITY_BOUNDARY.md",
    "AGVM_HOST_REMOTE_BUNDLE_LOADER_BOUNDARY.md",
    "AGVM_REMOTE_MODULE_UI_ADAPTER_PUBLICATION_BOUNDARY.md",
    "AGVM_NEXT_SLICE_EXECUTION_CONTROL.md",
    "AGVM_MAINTAIN_SLEEP_EVOLVE_RUNTIME_ORCHESTRATION_EXTRACTION.md",
    "AGVM_MAINTAIN_MEMORY_OS_LIST_RUNTIME_ORCHESTRATION_EXTRACTION.md",
    "AGVM_MAINTAIN_MATRIX_RUNTIME_ORCHESTRATION_EXTRACTION.md",
    "AGVM_MAINTAIN_DIRECT_RUNTIME_DISPATCH.md",
    "AGVM_MAINTAIN_MEMORY_OS_DIRECT_RUNTIME_HANDLER.md",
    "AGVM_MAINTAIN_SLEEP_EVOLVE_PREVIEW_DIRECT_RUNTIME_HANDLER.md",
    "AGVM_MAINTAIN_SLEEP_EVOLVE_APPLY_DIRECT_RUNTIME_HANDLER.md",
    "AGVM_MAINTAIN_MATRIX_PREVIEW_DIRECT_RUNTIME_HANDLER.md",
    "AGVM_MAINTAIN_MATRIX_APPLY_DIRECT_RUNTIME_HANDLER.md",
    "AGVM_GROW_MAINTAIN_PRIVATE_MODULE_EXPORT_REPORT.md",
)

SECRET_ASSIGNMENT = re.compile(
    r"\b("
    r"OPENAI_API_KEY|ANTHROPIC_API_KEY|AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|"
    r"STRIPE_SECRET_KEY|AGVM_PLATFORM_STRIPE_SECRET_KEY|AGVM_PLATFORM_STRIPE_WEBHOOK_SECRET|"
    r"AGVM_PLATFORM_AUTH_CLIENT_SECRET|AGVM_PLATFORM_AUTH_ADAPTER_SECRET|"
    r"AGVM_PLATFORM_ADMIN_TOKEN|AGVM_PLATFORM_BROWSER_SESSION_SECRET|"
    r"AGVM_PLATFORM_LEASE_SIGNING_SECRET|AGVM_MODULE_REGISTRY_SIGNING_SECRET|"
    r"JWT_SECRET|DATABASE_URL|GITHUB_TOKEN"
    r")\b[ \t]*[:=][ \t]*[\"']?([^\"'\r\n#\s]*)",
)
TOKEN_LIKE = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")
PROPRIETARY_LICENSE_ID = "LicenseRef-Eternal-Tech-" + "Proprietary"
PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
LOCAL_PATH = re.compile(r"(?:[A-Za-z]:\\Users\\|[A-Za-z]:/Users/)", re.IGNORECASE)

PLACEHOLDER_VALUES = {
    "",
    "<empty>",
    "<path-to-agvm-core>",
    "<set>",
    "<set-on-backend>",
    "<your-key>",
    "your-key-here",
    "replace-me",
    "changeme",
    "example",
}


@dataclass(frozen=True)
class DenyPolicy:
    denied: tuple[str, ...]
    exceptions: tuple[str, ...]

    def matches(self, path: str) -> bool:
        normalized = normalize_path(path)
        if any(pattern_matches(pattern, normalized) for pattern in self.exceptions):
            return False
        return any(pattern_matches(pattern, normalized) for pattern in self.denied)


def normalize_path(value: str | Path) -> str:
    text = str(value).replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text.strip("/")


def pattern_matches(pattern: str, path: str) -> bool:
    pattern = normalize_path(pattern)
    path = normalize_path(path)
    if pattern.endswith("/**"):
        prefix = pattern[:-3].rstrip("/")
        return path == prefix or path.startswith(prefix + "/")
    return fnmatchcase(path, pattern)


def parse_denylist(path: Path) -> DenyPolicy:
    denied: list[str] = []
    exceptions: list[str] = []
    for index, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("!"):
            pattern = normalize_path(line[1:])
            if not pattern:
                raise ValueError(f"{path}:{index}: empty denylist exception")
            exceptions.append(pattern)
        else:
            denied.append(normalize_path(line))
    return DenyPolicy(denied=tuple(denied), exceptions=tuple(exceptions))


def is_binary(data: bytes) -> bool:
    return b"\0" in data[:4096]


def read_text(path: Path) -> str | None:
    data = path.read_bytes()
    if is_binary(data):
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1", errors="replace")


def is_placeholder(value: str) -> bool:
    clean = value.strip().strip("\"'").strip()
    if clean.lower() in PLACEHOLDER_VALUES:
        return True
    if clean.startswith("<") and clean.endswith(">"):
        return True
    if clean.startswith("${"):
        return True
    if "..." in clean:
        return True
    return False


def scan_text(relative: str, text: str) -> list[str]:
    findings: list[str] = []
    if PROPRIETARY_LICENSE_ID in text:
        findings.append(f"{relative}: proprietary license marker")
    if PRIVATE_KEY.search(text):
        findings.append(f"{relative}: private key block")
    if TOKEN_LIKE.search(text):
        findings.append(f"{relative}: token-like secret")
    if LOCAL_PATH.search(text):
        findings.append(f"{relative}: local Windows user path")
    for match in SECRET_ASSIGNMENT.finditer(text):
        name = match.group(1)
        value = match.group(2)
        if not is_placeholder(value):
            findings.append(f"{relative}: non-placeholder secret assignment for {name}")
    return findings


def scan_export(path: Path, deny_policy: DenyPolicy) -> tuple[list[str], list[str]]:
    findings: list[str] = []
    warnings: list[str] = []
    if not path.exists() or not path.is_dir():
        return [f"export path does not exist or is not a directory: {path}"], warnings

    for required in REQUIRED_PUBLIC_FILES:
        if not (path / required).is_file():
            findings.append(f"missing required public file: {required}")

    if (path / ".git").exists():
        findings.append("export already contains .git; scan must run before public git init")

    for file_path in sorted(item for item in path.rglob("*") if item.is_file()):
        relative = normalize_path(file_path.relative_to(path))
        if relative == MARKER:
            continue
        if deny_policy.matches(relative):
            findings.append(f"denylisted path present: {relative}")
        if file_path.name in FORBIDDEN_DOC_NAMES:
            findings.append(f"forbidden internal doc present: {relative}")
        text = read_text(file_path)
        if text is None:
            warnings.append(f"binary file skipped by text scanner: {relative}")
            continue
        findings.extend(scan_text(relative, text))
    return findings, warnings


def scanner_availability(scanner_mode: str = DEFAULT_SCANNER_MODE) -> tuple[dict[str, bool], dict[str, str]]:
    mode = normalize_scanner_mode(scanner_mode)
    docker_present = shutil.which("docker") is not None
    availability: dict[str, bool] = {}
    modes: dict[str, str] = {}
    for name in SCANNER_NAMES:
        host_present = shutil.which(name) is not None
        if mode == "host":
            availability[name] = host_present
            modes[name] = "host" if host_present else "missing"
        elif mode == "docker":
            availability[name] = docker_present
            modes[name] = "docker" if docker_present else "missing"
        elif host_present:
            availability[name] = True
            modes[name] = "host"
        elif docker_present:
            availability[name] = True
            modes[name] = "docker"
        else:
            availability[name] = False
            modes[name] = "missing"
    return availability, modes


def normalize_scanner_mode(value: str) -> str:
    mode = str(value or DEFAULT_SCANNER_MODE).strip().lower()
    if mode not in {"auto", "host", "docker"}:
        raise ValueError(f"unsupported scanner mode: {value}")
    return mode


def main() -> int:
    parser = argparse.ArgumentParser(description="Check an AGVM public core export tree.")
    parser.add_argument("--path", required=True, help="public export directory")
    parser.add_argument("--denylist", default=DEFAULT_DENYLIST)
    parser.add_argument("--json", action="store_true", help="print machine-readable result")
    parser.add_argument(
        "--release",
        action="store_true",
        help="fail when external scanners are missing, not only on internal findings",
    )
    parser.add_argument(
        "--scanner-mode",
        choices=("auto", "host", "docker"),
        default=DEFAULT_SCANNER_MODE,
        help="how to consider external scanner availability for release gating",
    )
    args = parser.parse_args()

    repo = Path(__file__).resolve().parents[1]
    export_path = Path(args.path).resolve()
    deny_policy = parse_denylist(repo / args.denylist)
    findings, warnings = scan_export(export_path, deny_policy)
    scanners, scanner_modes = scanner_availability(args.scanner_mode)
    release_blockers = [
        f"external scanner missing: {name}" for name, present in scanners.items() if not present
    ]
    passed = not findings
    release_ready = passed and not release_blockers
    result = {
        "path": str(export_path),
        "passed": passed,
        "release_ready": release_ready,
        "findings": findings,
        "warnings": warnings,
        "external_scanners": scanners,
        "external_scanner_modes": scanner_modes,
        "scanner_mode": args.scanner_mode,
        "release_blockers": release_blockers,
    }

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"passed={str(passed).lower()} release_ready={str(release_ready).lower()}")
        for finding in findings:
            print(f"finding: {finding}")
        for warning in warnings:
            print(f"warning: {warning}")
        for blocker in release_blockers:
            print(f"release_blocker: {blocker}")

    if findings:
        return 2
    if args.release and release_blockers:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
