"""
Detect whether a repo offers distinct prod (self-host/deploy) vs dev (source build)
deployment paths, and extract the sequential commands for each.

Detection sources (in priority order):
  1. README section analysis — split by headers, score against keyword sets, extract code blocks
  2. Docker Compose variant files — docker-compose.yml vs docker-compose.dev.yml
  3. Root-level setup / deploy scripts — setup.sh, install.sh, deploy.sh, etc.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from repofix.detection.readme_util import find_readme_path


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class CommandStep:
    command: str
    interactive: bool = False  # requires terminal stdin passthrough (setup wizards)
    daemon: bool = False        # exits immediately; background process (-d flag)
    label: str = ""             # human-readable description


@dataclass
class DeployMode:
    key: str                              # "prod" | "dev"
    label: str                            # "Self-Host (Production)" | "Local Development"
    description: str
    steps: list[CommandStep] = field(default_factory=list)
    prerequisites: list[str] = field(default_factory=list)
    source: str = "readme"               # "readme" | "docker_compose" | "scripts"
    confidence: float = 0.5


@dataclass
class DeployModeOptions:
    modes: list[DeployMode] = field(default_factory=list)

    def has_multiple(self) -> bool:
        keys = {m.key for m in self.modes}
        return "prod" in keys and "dev" in keys

    def get(self, key: str) -> DeployMode | None:
        return next((m for m in self.modes if m.key == key), None)


# ── Public entry point ────────────────────────────────────────────────────────

def detect(repo_path: Path) -> DeployModeOptions:
    """
    Return available deployment modes for the repo at repo_path.

    Only returns modes with extracted commands.  Returns an empty
    DeployModeOptions (has_multiple() == False) when the repo has a single
    obvious deployment path — the caller should not prompt in that case.
    """
    modes: list[DeployMode] = []

    # 1. README (highest signal)
    readme_modes = _from_readme(repo_path)
    if readme_modes:
        modes.extend(readme_modes)

    # 2. Docker Compose variants (only fills gaps not covered by README)
    existing_keys = {m.key for m in modes}
    dc_modes = _from_docker_compose(repo_path)
    for m in dc_modes:
        if m.key not in existing_keys:
            modes.append(m)
            existing_keys.add(m.key)

    # 3. Scripts (last resort)
    if not modes:
        modes.extend(_from_scripts(repo_path))

    # Deduplicate by key, keeping highest-confidence entry
    best: dict[str, DeployMode] = {}
    for m in modes:
        if m.key not in best or m.confidence > best[m.key].confidence:
            best[m.key] = m

    # Sort: prod first, then dev
    ordered = sorted(best.values(), key=lambda m: (m.key != "prod", -m.confidence))
    return DeployModeOptions(modes=ordered)


# ── README-based detection ────────────────────────────────────────────────────

# Keywords that identify a "production / self-hosting" section header
_PROD_HEADER_KEYWORDS: frozenset[str] = frozenset([
    "self-host", "self-hosting", "self host",
    "deploy", "deployment", "production", "prod",
    "quick start", "quick install",
    "install", "installation",
    "hosted", "hosting",
    "one-click",
])

# Keywords that identify a "local development" section header
_DEV_HEADER_KEYWORDS: frozenset[str] = frozenset([
    "local development", "local dev", "dev setup",
    "development", "developing", "dev",
    "contribute", "contributing",
    "run locally", "running locally",
    "building", "from source",
    "hacking", "hackers",
])

_HEADER_RE      = re.compile(r'^(#{1,4})\s+(.+)$', re.MULTILINE)
_FENCED_CODE_RE = re.compile(r'```(?:\w+)?\n(.*?)```', re.DOTALL)
_PREREQ_RE      = re.compile(
    r'\*{0,2}(?:prerequisite[s]?|requirements?|requires?)\*{0,2}[:\s]+([^\n]+)',
    re.IGNORECASE,
)

# Lines that are clearly instructional prose, not shell commands
_SKIP_LINE_PREFIXES: tuple[str, ...] = (
    "open ", "visit ", "go to ", "browse to ", "navigate to ",
    "open http", "open https",
    "note:", "warning:", "tip:", "info:", "prerequisite",
    "then ", "now ", "next ", "after ",
    "// ", "/* ",
)

# Prefixes that strongly indicate a shell command
_COMMAND_PREFIXES: tuple[str, ...] = (
    "npm ", "npx ", "pnpm ", "yarn ", "bun ",
    "docker ", "docker-compose ",
    "kubectl ", "helm ", "terraform ",
    "make ",
    "pip ", "pip3 ", "uv ",
    "python ", "python3 ",
    "node ", "deno ",
    "cargo ", "go build", "go run",
    "ruby ", "bundle ",
    "brew ", "apt ", "apt-get ", "yum ", "dnf ", "apk ",
    "sudo ",
    "./", "bash ", "sh ",
    "curl ", "wget ",
    "export ", "source ",
    "cd ",
)

# Command fragments that imply interactive stdin is required
_INTERACTIVE_MARKERS: tuple[str, ...] = (
    " setup", " init", " configure", " config",
    "npm run setup", "npm run init",
    "pnpm run setup", "pnpm setup",
    "pnpm exec setup",
    "setup --", "init --",
)

# Flags that indicate a background/daemon process (exits immediately)
_DAEMON_FLAGS = (" -d", " --detach", " --daemon")


def _from_readme(repo_path: Path) -> list[DeployMode]:
    readme = _find_readme(repo_path)
    if not readme:
        return []

    content = readme.read_text(encoding="utf-8-sig", errors="replace")
    sections = _split_sections(content)

    prod_section: dict | None = None
    dev_section:  dict | None = None
    best_prod = 0
    best_dev  = 0

    for sec in sections:
        title_lower = sec["title"].lower()
        pscore = _keyword_score(title_lower, _PROD_HEADER_KEYWORDS)
        dscore = _keyword_score(title_lower, _DEV_HEADER_KEYWORDS)

        if pscore > best_prod and pscore > dscore:
            best_prod    = pscore
            prod_section = sec

        if dscore > best_dev and dscore >= pscore:
            best_dev    = dscore
            dev_section = sec

    modes: list[DeployMode] = []

    if prod_section:
        steps = _extract_steps(prod_section)
        if steps:
            modes.append(DeployMode(
                key="prod",
                label=prod_section["title"],
                description="Deploy using the recommended self-hosting / production method",
                steps=steps,
                prerequisites=_extract_prereqs(prod_section["body"]),
                source="readme",
                confidence=min(0.55 + best_prod * 0.1, 0.95),
            ))

    if dev_section and dev_section is not prod_section:
        steps = _extract_steps(dev_section)
        if steps:
            modes.append(DeployMode(
                key="dev",
                label=dev_section["title"],
                description="Build and run from source (for local development / contributing)",
                steps=steps,
                prerequisites=_extract_prereqs(dev_section["body"]),
                source="readme",
                confidence=min(0.55 + best_dev * 0.1, 0.95),
            ))

    return modes


def _find_readme(repo_path: Path) -> Path | None:
    return find_readme_path(repo_path)


def _split_sections(content: str) -> list[dict]:
    matches = list(_HEADER_RE.finditer(content))
    sections = []
    for i, m in enumerate(matches):
        start = m.end()
        end   = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        sections.append({
            "level": len(m.group(1)),
            "title": m.group(2).strip(),
            "body":  content[start:end],
        })
    return sections


def _keyword_score(text: str, keywords: frozenset[str]) -> int:
    return sum(1 for kw in keywords if kw in text)


def _extract_steps(section: dict) -> list[CommandStep]:
    """Extract command steps from fenced code blocks (or indented blocks) in a section."""
    body  = section["body"]
    steps: list[CommandStep] = []

    blocks = _FENCED_CODE_RE.findall(body)

    for block in blocks:
        for line in block.splitlines():
            step = _parse_command_line(line)
            if step:
                steps.append(step)

    # Fallback: indented code blocks if no fenced blocks found
    if not steps:
        for line in body.splitlines():
            stripped = line.strip()
            if stripped and any(stripped.startswith(p) for p in _COMMAND_PREFIXES):
                step = _parse_command_line(stripped)
                if step:
                    steps.append(step)

    return steps


def _parse_command_line(raw: str) -> CommandStep | None:
    """Return a CommandStep for a line, or None if it doesn't look like a command."""
    line = raw.strip()
    if not line:
        return None

    # Strip trailing inline comments (two or more spaces before #)
    if "  #" in line:
        line = line[: line.index("  #")].rstrip()
    if not line:
        return None

    line_lower = line.lower()

    # Skip instructional prose
    if any(line_lower.startswith(skip) for skip in _SKIP_LINE_PREFIXES):
        return None

    # Skip pure comment lines (but keep shebangs)
    if line.startswith("#") and not line.startswith("#!"):
        return None

    # Require that the line starts with a known command prefix
    if not any(line.startswith(p) or line_lower.startswith(p) for p in _COMMAND_PREFIXES):
        return None

    # Skip bare "git clone ..." — repofix handles git
    if line_lower.startswith("git clone "):
        return None

    # Skip standalone "cd <dir>" without chaining
    if line_lower.startswith("cd ") and "&&" not in line:
        return None

    interactive = any(m in line_lower for m in _INTERACTIVE_MARKERS)
    daemon      = any(flag in line for flag in _DAEMON_FLAGS)

    return CommandStep(command=line, interactive=interactive, daemon=daemon)


def _extract_prereqs(body: str) -> list[str]:
    prereqs: list[str] = []
    for m in _PREREQ_RE.finditer(body):
        parts = re.split(r'[,\+]|\band\b', m.group(1), flags=re.IGNORECASE)
        for p in parts:
            p = p.strip().strip("*").strip()
            if 2 <= len(p) <= 60:
                prereqs.append(p)
    return prereqs[:6]


# ── Docker Compose variant detection ─────────────────────────────────────────

def _from_docker_compose(repo_path: Path) -> list[DeployMode]:
    dc_main = repo_path / "docker-compose.yml"
    dc_dev  = repo_path / "docker-compose.dev.yml"
    dc_over = repo_path / "docker-compose.override.yml"

    if not dc_main.exists():
        # Also check .yaml extension
        dc_main = repo_path / "docker-compose.yaml"
        if not dc_main.exists():
            return []

    modes: list[DeployMode] = []
    main_content = dc_main.read_text(errors="replace")
    has_build    = "build:" in main_content

    dev_file = (
        "docker-compose.dev.yml"  if dc_dev.exists()
        else "docker-compose.override.yml" if dc_over.exists()
        else None
    )

    if dev_file:
        # Explicit prod/dev split via separate compose files
        modes.append(DeployMode(
            key="prod",
            label="Self-Host (Docker)",
            description="Run with production Docker Compose — uses pre-built images",
            steps=[
                CommandStep("docker compose up -d", daemon=True, label="Start all containers"),
            ],
            source="docker_compose",
            confidence=0.75,
        ))
        modes.append(DeployMode(
            key="dev",
            label="Local Development (Docker)",
            description=f"Run the dev stack via {dev_file} — includes hot-reload and dev databases",
            steps=[
                CommandStep(
                    f"docker compose -f {dev_file} up -d",
                    daemon=True,
                    label="Start dev dependencies (DB, cache, etc.)",
                ),
            ],
            source="docker_compose",
            confidence=0.75,
        ))
    elif not has_build:
        # docker-compose.yml uses pre-built images only → prod self-host
        modes.append(DeployMode(
            key="prod",
            label="Self-Host (Docker)",
            description="Run pre-built Docker images via docker compose",
            steps=[
                CommandStep("docker compose up -d", daemon=True),
            ],
            source="docker_compose",
            confidence=0.6,
        ))

    return modes


# ── Script-based detection ────────────────────────────────────────────────────

_PROD_SCRIPTS = ["setup.sh", "install.sh", "deploy.sh", "start.sh", "run.sh"]
_DEV_SCRIPTS  = ["dev.sh", "start-dev.sh", "run-dev.sh", "develop.sh"]


def _from_scripts(repo_path: Path) -> list[DeployMode]:
    modes: list[DeployMode] = []

    for script in _PROD_SCRIPTS:
        p = repo_path / script
        if p.exists():
            modes.append(DeployMode(
                key="prod",
                label=f"Self-Host (via ./{script})",
                description=f"Run the bundled setup / install script",
                steps=[
                    CommandStep(f"./{script}", interactive=True, label=f"Run {script}"),
                ],
                source="scripts",
                confidence=0.55,
            ))
            break

    for script in _DEV_SCRIPTS:
        p = repo_path / script
        if p.exists():
            modes.append(DeployMode(
                key="dev",
                label=f"Local Dev (via ./{script})",
                description="Start the development environment via script",
                steps=[
                    CommandStep(f"./{script}", label=f"Run {script}"),
                ],
                source="scripts",
                confidence=0.55,
            ))
            break

    return modes
