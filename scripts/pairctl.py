#!/usr/bin/env python3
"""pairctl.py - deterministic runtime for the ghostrider Agent Skill.

v4 turns the original paired-seat proof of concept into a multi-agent orchestration
engine with Codex, Cline, and Copilot implementing-seat adapters. The LLMs still reason; this runtime owns the deterministic mechanics:

- compact CAVEMAN/1 wire protocol to reduce repeated prose/token use
- Git-ref queue with atomic compare-and-swap claims
- one-command pair orchestration and bounded repair rounds
- isolated Git worktrees for the implementing seat
- repository-aware verification discovery/execution
- leases, process metadata, resume/recovery
- named profiles and repo-local .pairing.toml/.pairing.json overrides
- explicit audit gates, safe merge/cleanup, and revert-based rollback
- machine-readable lifecycle events

Standard library only. Python 3.8+.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

VERSION = "4.0.0"
CHANNEL_REF = "refs/pairing/queue"
CONFIG_DIR = Path(os.environ.get("PAIRING_HOME") or (Path.home() / ".pairing"))
CONFIG_FILE = CONFIG_DIR / "config.json"
NEWLINE = "\n"
PROTOCOL = "caveman-v1"
IMPLEMENTERS = ("codex", "cline", "copilot")

BUILTIN_PROFILES = {
    "fast": {
        "max_rounds": 1,
        "lease_minutes": 25,
        "auto_verify": True,
        "use_worktrees": True,
        "auto_cleanup": True,
        "codex_sandbox": "workspace-write",
        "verification_timeout": 600,
    },
    "standard": {
        "max_rounds": 2,
        "lease_minutes": 45,
        "auto_verify": True,
        "use_worktrees": True,
        "auto_cleanup": True,
        "codex_sandbox": "workspace-write",
        "verification_timeout": 900,
    },
    "deep": {
        "max_rounds": 3,
        "lease_minutes": 90,
        "auto_verify": True,
        "use_worktrees": True,
        "auto_cleanup": True,
        "codex_sandbox": "workspace-write",
        "verification_timeout": 1800,
    },
    "release": {
        "max_rounds": 3,
        "lease_minutes": 90,
        "auto_verify": True,
        "use_worktrees": True,
        "auto_cleanup": True,
        "codex_sandbox": "workspace-write",
        "verification_timeout": 1800,
        "require_clean_tree": True,
    },
}

DEFAULT_CONFIG = {
    "version": 4,
    "enabled": True,
    "auditor": "claude",
    "implementer": "codex",
    "implementer_priority": ["codex", "cline", "copilot"],
    "channel_policy": "local",
    "auto_init_local": True,
    "require_clean_tree": True,
    "codex_command": "codex",
    "codex_sandbox": "workspace-write",
    "codex_extra_args": [],
    "cline_command": "cline",
    "cline_thinking": "medium",
    "cline_auto_approve": True,
    "cline_extra_args": [],
    "copilot_command": "copilot",
    "copilot_model": "auto",
    "copilot_mode": "autopilot",
    "copilot_yolo": False,
    "copilot_extra_args": [],
    "dispatch_output": "log",
    "profile": "standard",
    "max_rounds": 2,
    "lease_minutes": 45,
    "auto_verify": True,
    "use_worktrees": True,
    "auto_cleanup": True,
    "auto_land": False,
    "verification_timeout": 900,
    "protocol": PROTOCOL,
    "profiles": {},
}


class PairingError(Exception):
    """Expected, user-actionable failure."""


class Console:
    def __init__(self, quiet: bool = False):
        self.quiet = quiet

    def say(self, text: str = "") -> None:
        if not self.quiet:
            print(text)

    def step(self, text: str) -> None:
        self.say("  " + text)

    def warn(self, text: str) -> None:
        print("  ! " + text, file=sys.stderr)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_bool(value: str) -> bool:
    v = value.strip().lower()
    if v in ("1", "true", "yes", "on", "enabled"):
        return True
    if v in ("0", "false", "no", "off", "disabled"):
        return False
    raise PairingError("expected on/off or true/false, got %r" % value)


def load_raw_config() -> Dict[str, object]:
    if not CONFIG_FILE.exists():
        return {}
    try:
        raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PairingError("cannot read config %s: %s" % (CONFIG_FILE, exc))
    if not isinstance(raw, dict):
        raise PairingError("config %s must contain a JSON object" % CONFIG_FILE)
    return raw


def load_config() -> Dict[str, object]:
    raw = load_raw_config()
    profile_name = str(raw.get("profile") or DEFAULT_CONFIG["profile"])
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(BUILTIN_PROFILES.get(profile_name, {}))
    custom_profiles = raw.get("profiles", {})
    if isinstance(custom_profiles, dict):
        p = custom_profiles.get(profile_name, {})
        if isinstance(p, dict):
            cfg.update(p)
    cfg.update(raw)
    cfg["profile"] = profile_name
    return cfg


def save_raw_config(raw: Dict[str, object]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(raw, indent=2, sort_keys=True) + NEWLINE, encoding="utf-8")
    os.replace(str(tmp), str(CONFIG_FILE))


def _toml_value(raw: str) -> object:
    raw = raw.strip()
    if not raw:
        return ""
    if raw.startswith('"') and raw.endswith('"'):
        try:
            return json.loads(raw)
        except ValueError:
            return raw[1:-1]
    if raw.lower() in ("true", "false"):
        return raw.lower() == "true"
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    if raw.startswith("[") and raw.endswith("]"):
        try:
            value = json.loads(raw.replace("'", '"'))
            return value
        except ValueError:
            return [x.strip().strip('"\'') for x in raw[1:-1].split(",") if x.strip()]
    return raw.strip('"\'')


def load_repo_settings(repo: Path) -> Dict[str, object]:
    """Read the dependency-free subset of .pairing.toml or .pairing.json we use."""
    js = repo / ".pairing.json"
    if js.exists():
        try:
            data = json.loads(js.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise PairingError("cannot read %s: %s" % (js, exc))
        if not isinstance(data, dict):
            raise PairingError("%s must contain a JSON object" % js)
        return data

    toml = repo / ".pairing.toml"
    if not toml.exists():
        return {}
    out: Dict[str, object] = {}
    section: List[str] = []
    for n, line in enumerate(toml.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            section = [p.strip() for p in stripped[1:-1].split(".") if p.strip()]
            continue
        if "=" not in stripped:
            raise PairingError("%s:%d: expected key = value" % (toml, n))
        key, value = stripped.split("=", 1)
        key = key.strip().replace("-", "_")
        target: Dict[str, object] = out
        for part in section:
            nxt = target.setdefault(part, {})
            if not isinstance(nxt, dict):
                raise PairingError("%s:%d: section collision" % (toml, n))
            target = nxt
        target[key] = _toml_value(value)
    return out


def effective_config(repo: Optional[Path] = None, profile_override: Optional[str] = None) -> Dict[str, object]:
    raw = load_raw_config()
    repo_cfg = load_repo_settings(repo) if repo else {}
    pairing = repo_cfg.get("pairing", {}) if isinstance(repo_cfg, dict) else {}
    profile_name = profile_override or (
        str(pairing.get("profile")) if isinstance(pairing, dict) and pairing.get("profile") else None
    ) or str(raw.get("profile") or DEFAULT_CONFIG["profile"])

    cfg = dict(DEFAULT_CONFIG)
    cfg.update(BUILTIN_PROFILES.get(profile_name, {}))
    custom_profiles = raw.get("profiles", {})
    if isinstance(custom_profiles, dict) and isinstance(custom_profiles.get(profile_name), dict):
        cfg.update(custom_profiles[profile_name])
    repo_profiles = repo_cfg.get("profile", {}) if isinstance(repo_cfg, dict) else {}
    if isinstance(repo_profiles, dict) and isinstance(repo_profiles.get(profile_name), dict):
        cfg.update(repo_profiles[profile_name])
    cfg.update({k: v for k, v in raw.items() if k != "profiles"})
    if isinstance(pairing, dict):
        cfg.update(pairing)
    cfg["profile"] = profile_name
    if isinstance(repo_cfg, dict) and isinstance(repo_cfg.get("verification"), dict):
        cfg["repo_verification"] = repo_cfg["verification"]
    return cfg


def implementer_command_name(agent: str, cfg: Dict[str, object]) -> str:
    if agent == "codex":
        return str(cfg.get("codex_command") or "codex")
    if agent == "cline":
        return str(cfg.get("cline_command") or "cline")
    if agent == "copilot":
        return str(cfg.get("copilot_command") or "copilot")
    raise PairingError("unknown implementer %r" % agent)


def resolve_implementer(cfg: Dict[str, object], override: Optional[str] = None) -> str:
    requested = str(override or cfg.get("implementer") or "codex").lower()
    if requested != "auto":
        if requested not in IMPLEMENTERS:
            raise PairingError("implementer must be one of: auto, %s" % ", ".join(IMPLEMENTERS))
        return requested
    priority = cfg.get("implementer_priority") or list(IMPLEMENTERS)
    if not isinstance(priority, list):
        priority = list(IMPLEMENTERS)
    for item in priority:
        agent = str(item).lower()
        if agent in IMPLEMENTERS and shutil.which(implementer_command_name(agent, cfg)):
            return agent
    raise PairingError("no supported implementing CLI found (checked %s)" % ", ".join(str(x) for x in priority))


def effective_config_for_args(repo: Optional[Path], args) -> Dict[str, object]:
    cfg = effective_config(repo, getattr(args, "profile", None))
    if getattr(args, "implementer", None):
        cfg["implementer"] = args.implementer
    return cfg


def git(repo: Path, *args: str, **kwargs) -> str:
    stdin = kwargs.pop("stdin", None)
    check = kwargs.pop("check", True)
    env = kwargs.pop("env", None)
    if kwargs:
        raise TypeError("unexpected git kwargs: %s" % sorted(kwargs))
    proc = subprocess.run(
        ["git", "-C", str(repo)] + list(args),
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    if check and proc.returncode != 0:
        raise PairingError((proc.stderr or proc.stdout).strip() or "git command failed")
    return proc.stdout.strip()


def repo_root(start: Optional[str] = None) -> Optional[Path]:
    where = Path(start) if start else Path.cwd()
    try:
        top = git(where, "rev-parse", "--show-toplevel")
    except (PairingError, OSError):
        return None
    return Path(top) if top else None


def common_repo_root(repo: Path) -> Path:
    """Return main worktree path when possible; safe fallback is supplied repo."""
    common = git(repo, "rev-parse", "--git-common-dir", check=False)
    if not common:
        return repo
    common_path = Path(common)
    if not common_path.is_absolute():
        common_path = (repo / common_path).resolve()
    # main repo common dir normally ends in .git
    if common_path.name == ".git":
        return common_path.parent
    return repo


def working_tree_clean(repo: Path) -> bool:
    return not bool(git(repo, "status", "--porcelain"))


def current_branch(repo: Path) -> str:
    return git(repo, "branch", "--show-current", check=False)


def channel_ready(repo: Path) -> bool:
    try:
        return bool(git(repo, "rev-parse", "--verify", "--quiet", CHANNEL_REF, check=False))
    except OSError:
        return False


def has_github(repo: Path) -> bool:
    if not shutil.which("gh"):
        return False
    url = git(repo, "remote", "get-url", "origin", check=False)
    if "github.com" not in url.lower():
        return False
    proc = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
    return proc.returncode == 0


def detect_tier(repo: Optional[Path], cfg: Optional[Dict[str, object]] = None) -> Tuple[str, str]:
    cfg = cfg or load_config()
    policy = str(cfg.get("channel_policy", "local"))
    if repo is None:
        return "inline", "not a git repository"
    if channel_ready(repo):
        return "local", "queue ref %s exists" % CHANNEL_REF
    if policy == "local":
        return "local-uninitialized", "channel policy requires local queue"
    if policy in ("auto", "github") and has_github(repo):
        return "github", "GitHub remote and authenticated gh"
    if policy == "github":
        return "inline", "GitHub selected but unavailable"
    return "inline", "no initialized local queue"


def _cat(repo: Path, path: str, default: Optional[str] = None) -> Optional[str]:
    out = git(repo, "cat-file", "-p", "%s:%s" % (CHANNEL_REF, path), check=False)
    return out if out else default


def queue_load(repo: Path) -> Tuple[Dict[str, object], str]:
    oid = git(repo, "rev-parse", "--verify", "--quiet", CHANNEL_REF, check=False)
    if not oid:
        return {"version": 2, "briefs": []}, ""
    raw = _cat(repo, "queue.json", "{}") or "{}"
    try:
        state = json.loads(raw)
    except ValueError:
        raise PairingError("queue.json on %s is not valid JSON" % CHANNEL_REF)
    if not isinstance(state, dict):
        raise PairingError("queue.json must contain a JSON object")
    state.setdefault("version", 2)
    state.setdefault("briefs", [])
    return state, oid


def _git_identity_env(repo: Path) -> Dict[str, str]:
    env = dict(os.environ)
    if not git(repo, "config", "user.name", check=False):
        env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "pairing"
    if not git(repo, "config", "user.email", check=False):
        env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "pairing@localhost"
    return env


def queue_commit(repo: Path, files: Dict[str, str], message: str, seat: str, expect_oid: str) -> str:
    scratch = tempfile.mkdtemp(prefix="pairing-index-")
    idx = os.path.join(scratch, "index")
    env = dict(os.environ)
    env["GIT_INDEX_FILE"] = idx
    try:
        if expect_oid:
            git(repo, "read-tree", "%s^{tree}" % expect_oid, env=env)
        for path, content in sorted(files.items()):
            blob = git(repo, "hash-object", "-w", "--stdin", stdin=content, env=env)
            git(repo, "update-index", "--add", "--cacheinfo",
                "100644,%s,%s" % (blob, path.replace("\\", "/")), env=env)
        tree = git(repo, "write-tree", env=env)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    args = ["commit-tree", tree]
    if expect_oid:
        args += ["-p", expect_oid]
    args += ["-m", "%s\n\nSeat: %s" % (message, seat)]
    proc = subprocess.run(["git", "-C", str(repo)] + args, capture_output=True,
                          text=True, env=_git_identity_env(repo), encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise PairingError((proc.stderr or proc.stdout).strip() or "git commit-tree failed")
    new_oid = proc.stdout.strip()
    cas = subprocess.run(["git", "-C", str(repo), "update-ref", CHANNEL_REF, new_oid, expect_oid],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    if cas.returncode != 0:
        raise PairingError("queue moved concurrently; nothing was written. %s" % (cas.stderr or "").strip())
    return new_oid


def next_id(state: Dict[str, object]) -> int:
    return max([int(b["id"]) for b in state.get("briefs", [])] + [0]) + 1  # type: ignore[index]


def find_brief(state: Dict[str, object], brief_id: str) -> Dict[str, object]:
    for b in state.get("briefs", []):  # type: ignore[assignment]
        if int(b["id"]) == int(brief_id):
            return b
    raise PairingError("no brief #%s in this queue" % brief_id)


def slug(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return (s[:40] or "brief").strip("-")


def event(entry: Dict[str, object], kind: str, seat: str, **fields: object) -> None:
    e: Dict[str, object] = {"type": kind, "at": now(), "seat": seat}
    e.update({k: v for k, v in fields.items() if v is not None})
    entry.setdefault("events", []).append(e)  # type: ignore[union-attr]


# ------------------------------- CAVEMAN/1 compact wire protocol -------------------------------

CVM_KEYS = {
    "G": "goal", "S": "scope", "C": "constraint", "A": "acceptance",
    "F": "file", "T": "test", "X": "exclude", "U": "unknown",
    "D": "delta", "B": "blocker", "Q": "uncertainty", "M": "commit",
    "E": "evidence",
}


def cvm_escape(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace("|", "\\p").replace("\n", "\\n")


def cvm_unescape(value: str) -> str:
    out, i = [], 0
    while i < len(value):
        if value[i] == "\\" and i + 1 < len(value):
            n = value[i + 1]
            if n == "n":
                out.append("\n"); i += 2; continue
            if n == "p":
                out.append("|"); i += 2; continue
            if n == "\\":
                out.append("\\"); i += 2; continue
        out.append(value[i]); i += 1
    return "".join(out)


def cvm_encode(kind: str, fields: List[Tuple[str, object]], verdict: Optional[str] = None) -> str:
    head = "CVM1|%s" % kind
    if verdict:
        head += "|%s" % verdict.upper()[0]
    lines = [head]
    for key, value in fields:
        if value is None or value == "":
            continue
        values = value if isinstance(value, list) else [value]
        for item in values:
            lines.append("%s|%s" % (key, cvm_escape(item)))
    return "\n".join(lines) + "\n"


def cvm_parse(body: str) -> Dict[str, object]:
    lines = body.strip().splitlines()
    if not lines or not lines[0].startswith("CVM1|"):
        raise PairingError("not a CAVEMAN/1 packet")
    head = lines[0].split("|")
    out: Dict[str, object] = {"protocol": "CVM1", "kind": head[1] if len(head) > 1 else "?"}
    if len(head) > 2:
        out["verdict"] = head[2]
    fields: Dict[str, List[str]] = {}
    for line in lines[1:]:
        if "|" not in line:
            continue
        key, val = line.split("|", 1)
        fields.setdefault(key, []).append(cvm_unescape(val))
    out["fields"] = fields
    return out


def brief_from_spec(spec: Dict[str, object]) -> str:
    fields: List[Tuple[str, object]] = []
    mapping = [
        ("G", "goal"), ("S", "scope"), ("C", "constraints"), ("A", "acceptance"),
        ("F", "files"), ("T", "tests"), ("X", "exclude"), ("U", "unknowns"),
    ]
    for key, name in mapping:
        value = spec.get(name)
        if value is not None:
            fields.append((key, value))
    return cvm_encode("B", fields)


def report_from_spec(spec: Dict[str, object], verdict: str) -> str:
    fields: List[Tuple[str, object]] = []
    mapping = [("D", "delta"), ("M", "commit"), ("T", "tests"), ("B", "blockers"),
               ("Q", "uncertainties"), ("E", "evidence")]
    for key, name in mapping:
        value = spec.get(name)
        if value is not None:
            fields.append((key, value))
    return cvm_encode("R", fields, verdict)


def audit_from_spec(spec: Dict[str, object], verdict: str) -> str:
    fields: List[Tuple[str, object]] = []
    mapping = [("E", "evidence"), ("T", "tests"), ("B", "findings"), ("Q", "uncertainties")]
    for key, name in mapping:
        value = spec.get(name)
        if value is not None:
            fields.append((key, value))
    return cvm_encode("A", fields, verdict)


def lint_brief(body: str, strict: bool = False) -> Tuple[bool, List[str]]:
    notes: List[str] = []
    if not body.startswith("CVM1|B"):
        notes.append("legacy prose brief: CAVEMAN/1 saves tokens and enables deterministic linting")
        return (not strict), notes
    parsed = cvm_parse(body)
    fields = parsed.get("fields", {})
    if not isinstance(fields, dict):
        return False, ["invalid CAVEMAN fields"]
    if not fields.get("G"):
        notes.append("missing G| goal")
    if not fields.get("A"):
        notes.append("missing A| acceptance criterion")
    if not fields.get("S") and not fields.get("F"):
        notes.append("missing scope: add S| or F|")
    ok = not any(x.startswith("missing") or x.startswith("invalid") for x in notes)
    return ok, notes


def read_json_file(path: str, what: str) -> Dict[str, object]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PairingError("cannot read %s spec %s: %s" % (what, path, exc))
    if not isinstance(data, dict):
        raise PairingError("%s spec must be a JSON object" % what)
    return data


QUEUE_README = """# Pairing queue\n\nMachine-owned queue on refs/pairing/queue.\nDo not hand-edit. Use pairctl.py.\n"""


def resolve_repo(args) -> Path:
    root = repo_root(args.repo) if getattr(args, "repo", None) else repo_root()
    if root is None:
        raise PairingError("not inside a git repository; pass --repo")
    return root


def require_local_queue(repo: Path) -> None:
    if not channel_ready(repo):
        raise PairingError("no local queue in %s; run `pairctl.py init` first" % repo)


def read_body(args, what: str) -> str:
    if getattr(args, "spec", None):
        spec = read_json_file(args.spec, what)
        if what == "brief":
            return brief_from_spec(spec)
        if what == "report":
            return report_from_spec(spec, args.verdict)
        if what == "audit":
            return audit_from_spec(spec, args.verdict)
    if getattr(args, "file", None):
        return Path(args.file).read_text(encoding="utf-8")
    if sys.stdin and not sys.stdin.isatty():
        return sys.stdin.read()
    raise PairingError("no %s body: pass --spec/--file or pipe stdin" % what)


# ------------------------------- worktrees -------------------------------

def repo_key(repo: Path) -> str:
    return hashlib.sha1(str(repo.resolve()).encode("utf-8")).hexdigest()[:10]


def worktree_path(repo: Path, entry: Dict[str, object], cfg: Dict[str, object]) -> Path:
    root = CONFIG_DIR / "worktrees"
    custom = cfg.get("worktree_root")
    if custom:
        root = Path(str(custom)).expanduser()
    return root / (repo.name + "-" + repo_key(repo)) / ("%04d-%s" % (int(entry["id"]), slug(str(entry["title"]))))


def ensure_worktree(repo: Path, entry: Dict[str, object], cfg: Dict[str, object]) -> Path:
    if not cfg.get("use_worktrees", True):
        return repo
    existing = entry.get("worktree")
    if existing and Path(str(existing)).exists():
        return Path(str(existing))
    path = worktree_path(repo, entry, cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    implementer = str(entry.get("implementer") or cfg.get("implementer") or "codex")
    branch = str(entry.get("branch") or ("%s/pair-%s-%s" % (implementer, entry["id"], slug(str(entry["title"])))))
    base_branch = current_branch(repo)
    base_oid = git(repo, "rev-parse", "HEAD")
    if path.exists():
        raise PairingError("worktree path already exists but is not registered: %s" % path)
    exists = bool(git(repo, "show-ref", "--verify", "--quiet", "refs/heads/%s" % branch, check=False))
    if exists:
        git(repo, "worktree", "add", str(path), branch)
    else:
        git(repo, "worktree", "add", "-b", branch, str(path), base_oid)
    entry["worktree"] = str(path)
    entry["branch"] = branch
    entry["base_branch"] = base_branch
    entry["base_oid"] = base_oid
    entry["phase"] = "worktree-ready"
    event(entry, "worktree", "runtime", path=str(path), branch=branch, base=base_oid)
    return path


def cleanup_worktree(repo: Path, entry: Dict[str, object], delete_branch: bool = True) -> None:
    path_s = entry.get("worktree")
    if path_s:
        path = Path(str(path_s))
        if path.exists():
            if not working_tree_clean(path):
                raise PairingError("refusing to remove dirty worktree %s" % path)
            git(repo, "worktree", "remove", str(path))
    branch = str(entry.get("branch") or "")
    if delete_branch and branch and git(repo, "show-ref", "--verify", "--quiet", "refs/heads/%s" % branch, check=False):
        git(repo, "branch", "-d", branch)
    entry["worktree"] = None
    entry["phase"] = "cleaned" if entry.get("state") == "landed" else entry.get("phase")


# ------------------------------- verification -------------------------------

def detect_verification(repo: Path, cfg: Dict[str, object]) -> List[str]:
    custom = cfg.get("repo_verification", {})
    if isinstance(custom, dict):
        commands = custom.get("commands")
        if isinstance(commands, list) and all(isinstance(x, str) for x in commands):
            return list(commands)
    commands: List[str] = []
    package = repo / "package.json"
    if package.exists():
        try:
            pkg = json.loads(package.read_text(encoding="utf-8"))
            scripts = pkg.get("scripts", {}) if isinstance(pkg, dict) else {}
        except Exception:
            scripts = {}
        pm = "pnpm" if (repo / "pnpm-lock.yaml").exists() else ("yarn" if (repo / "yarn.lock").exists() else "npm")
        if isinstance(scripts, dict) and "test" in scripts:
            commands.append("%s %s" % (pm, "test" if pm != "npm" else "test"))
        if isinstance(scripts, dict) and "build" in scripts:
            commands.append("%s %s" % (pm, "build" if pm == "yarn" else "run build"))
    if (repo / "pyproject.toml").exists() or (repo / "pytest.ini").exists() or (repo / "tests").is_dir():
        commands.append("%s -m pytest -q" % shlex.quote(sys.executable))
    if (repo / "Cargo.toml").exists():
        commands.append("cargo test")
    if (repo / "go.mod").exists():
        commands.append("go test ./...")
    if list(repo.glob("*.sln")) or list(repo.glob("*.csproj")):
        commands.append("dotnet test --nologo")
    if (repo / "pom.xml").exists():
        commands.append("mvn -q test")
    if (repo / "gradlew").exists():
        commands.append("./gradlew test")
    elif (repo / "gradlew.bat").exists():
        commands.append("gradlew.bat test")
    makefile = repo / "Makefile"
    if makefile.exists() and re.search(r"(?m)^test\s*:", makefile.read_text(encoding="utf-8", errors="replace")):
        commands.append("make test")
    # de-duplicate while preserving order
    seen, unique = set(), []
    for cmd in commands:
        if cmd not in seen:
            unique.append(cmd); seen.add(cmd)
    return unique


def command_parts(command: str) -> List[str]:
    return shlex.split(command, posix=(os.name != "nt"))


def log_dir(repo: Path, brief_id: object) -> Path:
    d = CONFIG_DIR / "logs" / (repo.name + "-" + repo_key(repo)) / str(brief_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def run_verification(repo: Path, entry: Optional[Dict[str, object]], cfg: Dict[str, object], console: Console) -> Dict[str, object]:
    commands = detect_verification(repo, cfg)
    result: Dict[str, object] = {"at": now(), "commands": [], "passed": True, "available": True}
    if not commands:
        result["passed"] = None
        result["available"] = False
        result["reason"] = "no verification commands detected; semantic audit required"
        return result
    timeout = int(cfg.get("verification_timeout") or 900)
    bid = entry["id"] if entry else "adhoc"
    for idx, command in enumerate(commands, 1):
        console.step("verify: %s" % command)
        started = datetime.now(timezone.utc)
        try:
            proc = subprocess.run(resolve_launch_command(command_parts(command)), cwd=str(repo),
                                  capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=timeout)
            output = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
            code = proc.returncode
        except subprocess.TimeoutExpired as exc:
            output = ((exc.stdout or "") if isinstance(exc.stdout, str) else "") + "\nTIMEOUT"
            code = 124
        except (FileNotFoundError, NotADirectoryError, PermissionError, OSError) as exc:
            # A verification tool that is absent or unlaunchable is a failed gate, not a
            # crash of the runtime. Record it so the round reports instead of aborting.
            output = "verification command could not be launched: %s" % exc
            code = 127
        duration = round((datetime.now(timezone.utc) - started).total_seconds(), 3)
        digest = hashlib.sha256(output.encode("utf-8", errors="replace")).hexdigest()[:16]
        path = log_dir(common_repo_root(repo), bid) / ("%s-%02d.log" % (datetime.now().strftime("%Y%m%d-%H%M%S"), idx))
        path.write_text(output, encoding="utf-8")
        item = {"cmd": command, "exit": code, "seconds": duration, "sha256_16": digest, "log": str(path)}
        result["commands"].append(item)  # type: ignore[union-attr]
        if code != 0:
            result["passed"] = False
    return result


def verification_wire(result: Dict[str, object]) -> str:
    fields: List[Tuple[str, object]] = []
    for item in result.get("commands", []):  # type: ignore[assignment]
        fields.append(("T", "%s => %s [%s]" % (item.get("cmd"), item.get("exit"), item.get("sha256_16"))))
    if result.get("reason"):
        fields.append(("B", result["reason"]))
    verdict = "pass" if result.get("passed") is True else ("fail" if result.get("passed") is False else "unknown")
    return cvm_encode("V", fields, verdict)


# ------------------------------- queue commands -------------------------------

def cmd_init(args, console: Console) -> int:
    repo = resolve_repo(args)
    if channel_ready(repo):
        console.say("  queue already exists in %s" % repo); return 0
    state = {"version": 2, "created": now(), "briefs": []}
    if args.dry_run:
        console.step("would create %s" % CHANNEL_REF); return 0
    queue_commit(repo, {"queue.json": json.dumps(state, indent=2) + NEWLINE, "README.md": QUEUE_README},
                 "pairing: create queue", args.seat or "claude", "")
    console.say("  queue created in %s" % repo)
    return 0


def create_brief(repo: Path, title: str, body: str, seat: str, strict: bool = False) -> int:
    require_local_queue(repo)
    ok, notes = lint_brief(body, strict=strict)
    if not ok:
        raise PairingError("brief quality gate failed: " + "; ".join(notes))
    state, oid = queue_load(repo)
    bid = next_id(state)
    path = "briefs/%04d-%s.cvm" % (bid, slug(title)) if body.startswith("CVM1|") else "briefs/%04d-%s.md" % (bid, slug(title))
    entry: Dict[str, object] = {
        "id": bid, "title": title, "state": "ready", "phase": "briefed", "path": path,
        "seat": None, "created": now(), "reports": [], "events": [], "round": 0,
        "protocol": PROTOCOL if body.startswith("CVM1|") else "legacy",
    }
    event(entry, "brief", seat, lint=notes)
    state["briefs"].append(entry)  # type: ignore[index]
    queue_commit(repo, {"queue.json": json.dumps(state, indent=2) + NEWLINE, path: body},
                 "pairing: brief #%d %s" % (bid, title), seat, oid)
    return bid


def cmd_brief(args, console: Console) -> int:
    repo = resolve_repo(args)
    body = read_body(args, "brief")
    if args.dry_run:
        ok, notes = lint_brief(body, strict=args.strict)
        console.say("  lint: %s %s" % ("PASS" if ok else "FAIL", "; ".join(notes))); return 0 if ok else 1
    bid = create_brief(repo, args.title, body, args.seat or "claude", strict=args.strict)
    console.say("  posted #%d  %s" % (bid, args.title))
    return 0


def claim_entry(repo: Path, entry: Dict[str, object], state: Dict[str, object], oid: str,
                seat: str, cfg: Dict[str, object], pid: Optional[int] = None, dispatch_id: Optional[str] = None) -> None:
    if entry["state"] != "ready":
        raise PairingError("#%s is %s, not ready" % (entry["id"], entry["state"]))
    lease_minutes = int(cfg.get("lease_minutes") or 45)
    expires = datetime.now(timezone.utc) + timedelta(minutes=lease_minutes)
    entry["state"] = "claimed"; entry["phase"] = "implementing"; entry["seat"] = seat
    entry["round"] = int(entry.get("round") or 0) + 1
    entry["claim"] = {"seat": seat, "at": now(), "lease_expires": expires.isoformat(timespec="seconds"),
                      "pid": pid, "host": socket.gethostname(), "dispatch_id": dispatch_id}
    event(entry, "claim", seat, round=entry["round"], lease_expires=entry["claim"]["lease_expires"])
    queue_commit(repo, {"queue.json": json.dumps(state, indent=2) + NEWLINE},
                 "pairing: claim #%s round %s" % (entry["id"], entry["round"]), seat, oid)


def cmd_claim(args, console: Console) -> int:
    repo = resolve_repo(args); require_local_queue(repo)
    cfg = effective_config(repo, getattr(args, "profile", None)); state, oid = queue_load(repo)
    if args.id:
        entry = find_brief(state, args.id)
    else:
        ready = [b for b in state.get("briefs", []) if b["state"] == "ready"]  # type: ignore[index]
        if not ready:
            console.say("  nothing ready to claim"); return 0
        entry = ready[0]
    if args.dry_run:
        console.step("would claim #%s" % entry["id"]); return 0
    seat = args.seat or resolve_implementer(cfg, getattr(args, "implementer", None))
    claim_entry(repo, entry, state, oid, seat, cfg)
    console.say("  claimed #%s" % entry["id"])
    return 0


def requeue_entry(repo: Path, entry: Dict[str, object], state: Dict[str, object], oid: str,
                  seat: str, note: Optional[str]) -> None:
    if entry["state"] == "landed":
        raise PairingError("#%s is landed" % entry["id"])
    entry["state"] = "ready"; entry["phase"] = "ready"; entry["seat"] = None; entry.pop("claim", None)
    event(entry, "requeue", seat, note=note)
    queue_commit(repo, {"queue.json": json.dumps(state, indent=2) + NEWLINE},
                 "pairing: requeue #%s" % entry["id"], seat, oid)


def cmd_requeue(args, console: Console) -> int:
    repo = resolve_repo(args); require_local_queue(repo); state, oid = queue_load(repo); entry = find_brief(state, args.id)
    if entry["state"] == "ready": console.say("  already ready"); return 0
    if args.dry_run: console.step("would requeue #%s" % entry["id"]); return 0
    requeue_entry(repo, entry, state, oid, args.seat or "claude", args.note)
    console.say("  requeued #%s" % entry["id"]); return 0


def cmd_report(args, console: Console) -> int:
    repo = resolve_repo(args); require_local_queue(repo); body = read_body(args, "report")
    state, oid = queue_load(repo); entry = find_brief(state, args.id)
    if entry["state"] not in ("claimed", "reported"):
        raise PairingError("#%s is %s; claim it before reporting" % (entry["id"], entry["state"]))
    seat = args.seat or str(entry.get("seat") or entry.get("implementer") or "codex"); reports = entry.setdefault("reports", []); n = len(reports) + 1  # type: ignore[arg-type]
    ext = "cvm" if body.startswith("CVM1|") else "md"; path = "reports/%04d-%02d-%s.%s" % (int(entry["id"]), n, seat, ext)
    reports.append({"path": path, "seat": seat, "verdict": args.verdict, "at": now(), "round": entry.get("round")})  # type: ignore[union-attr]
    entry["state"] = "blocked" if args.verdict == "blocked" else "reported"
    entry["phase"] = "blocked" if args.verdict == "blocked" else "reported"
    event(entry, "report", seat, verdict=args.verdict, round=entry.get("round"))
    if args.dry_run: console.step("would report %s" % args.verdict); return 0
    queue_commit(repo, {"queue.json": json.dumps(state, indent=2) + NEWLINE, path: body},
                 "pairing: report #%s %s" % (entry["id"], args.verdict), seat, oid)
    console.say("  reported %s on #%s" % (args.verdict, entry["id"])); return 0


def wire_packet(repo: Path, entry: Dict[str, object]) -> str:
    parts = ["I|%s|R%s|%s" % (entry["id"], entry.get("round", 0), entry.get("phase", entry.get("state")))]
    parts.append(_cat(repo, str(entry["path"]), "") or "")
    # only latest report + latest audit/verification failure: delta context, not full history
    reports = entry.get("reports", [])
    if reports:
        latest = reports[-1]
        parts.append(_cat(repo, str(latest["path"]), "") or "")
    for e in reversed(entry.get("events", [])):
        if e.get("type") in ("audit", "verification") and e.get("wire"):
            parts.append(str(e["wire"])); break
    return "\n".join(x.strip() for x in parts if x and x.strip()) + "\n"


def cmd_show(args, console: Console) -> int:
    repo = resolve_repo(args); require_local_queue(repo); state, _ = queue_load(repo); entry = find_brief(state, args.id)
    if args.wire:
        console.say(wire_packet(repo, entry).rstrip()); return 0
    if args.json:
        console.say(json.dumps(entry, indent=2, sort_keys=True)); return 0
    console.say("  #%s %s\n  state=%s phase=%s round=%s seat=%s" %
                (entry["id"], entry["title"], entry["state"], entry.get("phase"), entry.get("round", 0), entry.get("seat") or "-"))
    if entry.get("branch"): console.say("  branch=%s" % entry["branch"])
    if entry.get("worktree"): console.say("  worktree=%s" % entry["worktree"])
    console.say("\n" + (_cat(repo, str(entry["path"]), "(missing)") or ""))
    return 0


def cmd_queue(args, console: Console) -> int:
    repo = resolve_repo(args)
    if not channel_ready(repo): console.say("  no local queue"); return 0
    state, _ = queue_load(repo); briefs = state.get("briefs", [])
    if args.json: console.say(json.dumps(briefs, indent=2)); return 0
    if not briefs: console.say("  queue is empty"); return 0
    console.say("  %-5s %-12s %-16s %-5s %s" % ("id", "state", "phase", "rnd", "title"))
    for b in briefs:  # type: ignore[assignment]
        console.say("  %-5s %-12s %-16s %-5s %s" % ("#%s" % b["id"], b["state"], b.get("phase", "-"), b.get("round", 0), b["title"]))
    return 0


# ------------------------------- dispatch/orchestration -------------------------------

def _dispatch_prompt(repo: Path, entry: Dict[str, object], pairctl: Path, agent: str) -> str:
    # Deliberately tiny. Stable policy lives in the installed skill; show --wire carries only
    # this brief's delta context. Each CLI gets its native skill invocation hint.
    invoke = {
        "codex": "$ghostrider",
        "cline": "Use the ghostrider skill.",
        "copilot": "/ghostrider",
    }[agent]
    # Single line, deliberately. On Windows the implementer CLIs are npm .CMD shims
    # (codex.CMD, cline.CMD) and cmd.exe truncates a multi-line argv element at the
    # first newline, so a multi-line prompt reached the agent as just "$ghostrider"
    # with the brief pointer silently dropped. One line on every platform keeps the
    # dispatched prompt identical everywhere.
    return " :: ".join([
        invoke,
        "CVM1 implement I#{id} R{round} seat={agent}.",
        "CTX|python \"{ctl}\" show {id} --wire --repo \"{repo}\"",
        "RULE|read repo instructions; edit only scope; commit; test; never merge.",
        "OUT|report compactly with pairctl report --spec --seat {agent}.",
    ]).format(invoke=invoke, id=entry["id"], round=entry.get("round", 0), agent=agent,
              ctl=str(pairctl.resolve()), repo=str(repo))


def build_dispatch_command(agent: str, cfg: Dict[str, object], wt: Path, prompt: str, main: Optional[Path] = None) -> List[str]:
    command_name = implementer_command_name(agent, cfg)
    if agent == "codex":
        sandbox = str(cfg.get("codex_sandbox") or "workspace-write")
        extra = list(cfg.get("codex_extra_args") or [])
        # A linked worktree keeps HEAD, index, objects and refs in the MAIN repository's
        # .git directory, which sits outside the sandboxed worktree. Without write access
        # there the agent can edit files but cannot commit, and the round is lost.
        writable: List[str] = []
        if main is not None:
            git_dir = Path(main) / ".git"
            if git_dir.exists() and Path(wt).resolve() != Path(main).resolve():
                writable = ["--add-dir", str(git_dir)]
        return ([command_name, "exec", "--sandbox", sandbox, "-C", str(wt)]
                + writable + extra + [prompt])
    if agent == "cline":
        thinking = str(cfg.get("cline_thinking") or "medium")
        extra = list(cfg.get("cline_extra_args") or [])
        cmd = [command_name, "--cwd", str(wt), "--thinking", thinking, "--json"]
        if bool(cfg.get("cline_auto_approve", True)):
            cmd += ["--auto-approve", "true"]
        return cmd + extra + [prompt]
    if agent == "copilot":
        model = str(cfg.get("copilot_model") or "auto")
        mode = str(cfg.get("copilot_mode") or "autopilot")
        extra = list(cfg.get("copilot_extra_args") or [])
        cmd = [command_name, "-C", str(wt), "-p", prompt, "--mode", mode,
               "--no-ask-user", "--output-format=json", "--model", model]
        if bool(cfg.get("copilot_yolo", False)):
            cmd.append("--yolo")
        return cmd + extra
    raise PairingError("unsupported implementer %s" % agent)


def resolve_launch_command(command: List[str]) -> List[str]:
    """Resolve argv[0] to a concrete path before spawning.

    Windows CreateProcess does not consult PATHEXT, so a bare name such as
    "codex" fails with FileNotFoundError even though shutil.which resolves it
    to codex.CMD. npm-installed CLIs (codex, cline) are always .CMD shims, so
    without this every dispatch on Windows dies before the agent starts.
    """
    if not command:
        return command
    resolved = shutil.which(command[0])
    return [resolved] + list(command[1:]) if resolved else command


def update_dispatch_process(repo: Path, brief_id: str, pid: int, dispatch_id: str, seat: str) -> None:
    state, oid = queue_load(repo); entry = find_brief(state, brief_id); claim = entry.setdefault("claim", {})
    claim["pid"] = pid; claim["dispatch_id"] = dispatch_id; claim["host"] = socket.gethostname()
    event(entry, "process", seat, pid=pid, dispatch_id=dispatch_id)
    queue_commit(repo, {"queue.json": json.dumps(state, indent=2) + NEWLINE},
                 "pairing: process #%s pid %s" % (brief_id, pid), seat, oid)


def dispatch_once(repo: Path, brief_id: str, cfg: Dict[str, object], console: Console,
                  allow_dirty: bool = False, force: bool = False, implementer_override: Optional[str] = None) -> int:
    if not cfg.get("enabled") and not force:
        raise PairingError("pairing is OFF")
    require_local_queue(repo)
    main = common_repo_root(repo)
    if cfg.get("require_clean_tree") and not working_tree_clean(main) and not allow_dirty:
        raise PairingError("main working tree is not clean")
    state, oid = queue_load(repo); entry = find_brief(state, brief_id)
    agent = resolve_implementer(cfg, implementer_override or str(entry.get("implementer") or "") or None)
    command_name = implementer_command_name(agent, cfg)
    if not shutil.which(command_name):
        raise PairingError("%s CLI %r not found" % (agent, command_name))

    if entry["state"] == "ready":
        entry["implementer"] = agent
        wt = ensure_worktree(main, entry, cfg)
        queue_commit(main, {"queue.json": json.dumps(state, indent=2) + NEWLINE},
                     "pairing: worktree #%s" % entry["id"], "runtime", oid)
        state, oid = queue_load(main); entry = find_brief(state, brief_id)
        claim_entry(main, entry, state, oid, agent, cfg, dispatch_id=str(uuid.uuid4()))
        state, _ = queue_load(main); entry = find_brief(state, brief_id)
    elif entry["state"] == "claimed":
        claim = entry.get("claim") if isinstance(entry.get("claim"), dict) else {}
        claimed_seat = str(claim.get("seat") or entry.get("seat") or agent)
        if claimed_seat != agent and not force:
            raise PairingError("#%s is claimed by %s, not %s" % (entry["id"], claimed_seat, agent))
        wt = Path(str(entry.get("worktree") or main))
    else:
        raise PairingError("#%s is %s; only ready/claimed can dispatch" % (entry["id"], entry["state"]))

    prompt = _dispatch_prompt(main, entry, Path(__file__), agent)
    command = build_dispatch_command(agent, cfg, wt, prompt, main)
    command = resolve_launch_command(command)
    console.say("  dispatch #%s R%s -> %s [%s]" % (entry["id"], entry.get("round", 0), wt, agent))
    dispatch_id = str((entry.get("claim") or {}).get("dispatch_id") or uuid.uuid4())
    out_mode = str(cfg.get("dispatch_output") or "log")
    log_path = log_dir(main, entry["id"]) / ("dispatch-R%s-%s.log" % (entry.get("round", 0), agent))
    if out_mode == "inherit":
        proc = subprocess.Popen(command, text=True, encoding="utf-8", errors="replace")
        update_dispatch_process(main, str(entry["id"]), proc.pid, dispatch_id, agent)
        code = proc.wait()
    else:
        with open(log_path, "w", encoding="utf-8", newline="\n") as fh:
            proc = subprocess.Popen(command, text=True, stdout=fh, stderr=subprocess.STDOUT,
                                    encoding="utf-8", errors="replace")
            update_dispatch_process(main, str(entry["id"]), proc.pid, dispatch_id, agent)
            code = proc.wait()
        console.step("%s output -> %s" % (agent, log_path))
    state_after, _ = queue_load(main); after = find_brief(state_after, brief_id)
    if code != 0:
        raise PairingError("%s exited %d; #%s remains %s. Use resume/requeue after inspection." % (agent, code, brief_id, after["state"]))
    if after["state"] == "claimed":
        raise PairingError("%s exited 0 without report for #%s" % (agent, brief_id))
    return code


def cmd_dispatch(args, console: Console) -> int:
    repo = resolve_repo(args); cfg = effective_config_for_args(common_repo_root(repo), args)
    agent = resolve_implementer(cfg, getattr(args, "implementer", None))
    if args.dry_run:
        console.say("  would dispatch #%s profile=%s implementer=%s" % (args.id, cfg.get("profile"), agent)); return 0
    return dispatch_once(repo, args.id, cfg, console, args.allow_dirty, args.force, agent)


def record_verification(repo: Path, brief_id: str, result: Dict[str, object], seat: str = "runtime") -> None:
    state, oid = queue_load(repo); entry = find_brief(state, brief_id)
    entry.setdefault("verification", []).append(result)  # type: ignore[union-attr]
    entry["phase"] = "verification-failed" if result.get("passed") is False else "audit-ready"
    wire = verification_wire(result)
    event(entry, "verification", seat, passed=result.get("passed"), wire=wire)
    queue_commit(repo, {"queue.json": json.dumps(state, indent=2) + NEWLINE},
                 "pairing: verify #%s %s" % (brief_id, "pass" if result.get("passed") else "fail"), seat, oid)


def cmd_verify(args, console: Console) -> int:
    repo = resolve_repo(args); main = common_repo_root(repo); cfg = effective_config(main, args.profile)
    entry = None; target = repo
    if args.id:
        state, _ = queue_load(main); entry = find_brief(state, args.id); target = Path(str(entry.get("worktree") or main))
    if args.list:
        for cmd in detect_verification(target, cfg): console.say(cmd)
        return 0
    result = run_verification(target, entry, cfg, console)
    if entry: record_verification(main, str(entry["id"]), result)
    if args.json: console.say(json.dumps(result, indent=2))
    else:
        label = "PASS" if result.get("passed") is True else ("FAIL" if result.get("passed") is False else "NONE DETECTED")
        console.say("  verification: %s" % label)
    return 0 if result.get("passed") is True else (1 if result.get("passed") is False else 2)


def cmd_pair(args, console: Console) -> int:
    repo = resolve_repo(args); main = common_repo_root(repo); cfg = effective_config_for_args(main, args)
    agent = resolve_implementer(cfg, getattr(args, "implementer", None))
    if not channel_ready(main):
        ns = argparse.Namespace(repo=str(main), seat="claude", dry_run=False); cmd_init(ns, Console(quiet=True))
    if args.id:
        bid = int(args.id)
    else:
        body = read_body(args, "brief")
        bid = create_brief(main, args.title, body, args.seat or "claude", strict=True)
        console.say("  brief #%s created" % bid)
    max_rounds = int(cfg.get("max_rounds") or 1)
    while True:
        state, _ = queue_load(main); entry = find_brief(state, str(bid))
        if entry["state"] in ("audit-ready", "audit-passed", "landed"):
            break
        if int(entry.get("round") or 0) >= max_rounds and entry["state"] == "ready":
            raise PairingError("#%s reached max_rounds=%s" % (bid, max_rounds))
        if entry["state"] == "reported":
            pass
        elif entry["state"] == "claimed":
            raise PairingError("#%s is already claimed; use resume to inspect/recover it" % bid)
        else:
            dispatch_once(main, str(bid), cfg, console, args.allow_dirty, args.force, agent)
            state, _ = queue_load(main); entry = find_brief(state, str(bid))
        if entry["state"] == "blocked":
            console.warn("%s blocked #%s" % (entry.get("implementer") or agent, bid)); return 1
        if not cfg.get("auto_verify", True):
            break
        target = Path(str(entry.get("worktree") or main))
        result = run_verification(target, entry, cfg, console); record_verification(main, str(bid), result)
        if result.get("passed") is not False:
            break
        state, oid = queue_load(main); entry = find_brief(state, str(bid))
        if int(entry.get("round") or 0) >= max_rounds:
            entry["state"] = "blocked"; entry["phase"] = "max-rounds"
            event(entry, "blocked", "runtime", reason="verification failed at max rounds")
            queue_commit(main, {"queue.json": json.dumps(state, indent=2) + NEWLINE},
                         "pairing: block #%s max rounds" % bid, "runtime", oid)
            return 1
        requeue_entry(main, entry, state, oid, "runtime", "deterministic verification failed; see latest V packet")
        console.say("  verification failed; automatic repair round")
    console.say("\n  #%s is AUDIT-READY. Claude must independently inspect diff + verification evidence." % bid)
    return 0


# ------------------------------- audit / land / rollback -------------------------------

def cmd_audit(args, console: Console) -> int:
    repo = common_repo_root(resolve_repo(args)); require_local_queue(repo); body = read_body(args, "audit")
    state, oid = queue_load(repo); entry = find_brief(state, args.id); cfg = effective_config_for_args(repo, args)
    path = "audits/%04d-%02d.cvm" % (int(entry["id"]), len([e for e in entry.get("events", []) if e.get("type") == "audit"]) + 1)
    if args.verdict == "pass":
        entry["state"] = "audit-passed"; entry["phase"] = "audit-passed"
    elif args.verdict == "block":
        entry["state"] = "blocked"; entry["phase"] = "blocked"
    else:
        if int(entry.get("round") or 0) >= int(cfg.get("max_rounds") or 1):
            entry["state"] = "blocked"; entry["phase"] = "max-rounds"
        else:
            entry["state"] = "ready"; entry["phase"] = "repair-ready"; entry["seat"] = None; entry.pop("claim", None)
    event(entry, "audit", args.seat or "claude", verdict=args.verdict, wire=body)
    queue_commit(repo, {"queue.json": json.dumps(state, indent=2) + NEWLINE, path: body},
                 "pairing: audit #%s %s" % (entry["id"], args.verdict), args.seat or "claude", oid)
    console.say("  audit %s -> %s" % (args.verdict, entry["state"]))
    if args.verdict == "fail" and args.redispatch and entry["state"] == "ready":
        return dispatch_once(repo, str(entry["id"]), cfg, console, args.allow_dirty, args.force,
                             getattr(args, "implementer", None) or str(entry.get("implementer") or "") or None)
    return 0


def cmd_land(args, console: Console) -> int:
    repo = common_repo_root(resolve_repo(args)); require_local_queue(repo); cfg = effective_config(repo, args.profile)
    if cfg.get("require_clean_tree") and not working_tree_clean(repo):
        raise PairingError("main worktree must be clean before land")
    state, oid = queue_load(repo); entry = find_brief(state, args.id)
    if entry["state"] != "audit-passed" and not args.force:
        raise PairingError("#%s must be audit-passed before land" % entry["id"])
    branch = str(entry.get("branch") or "")
    if not branch: raise PairingError("#%s has no implementation branch" % entry["id"])
    expected = str(entry.get("base_branch") or "")
    if expected and current_branch(repo) != expected and not args.force:
        raise PairingError("main repo is on %s, expected %s" % (current_branch(repo), expected))
    backup_ref = "refs/pairing/backups/%s/%s" % (entry["id"], datetime.now().strftime("%Y%m%d-%H%M%S"))
    git(repo, "update-ref", backup_ref, git(repo, "rev-parse", "HEAD"))
    try:
        git(repo, "merge", "--no-ff", branch, "-m", "pairing: land #%s %s" % (entry["id"], entry["title"]))
    except PairingError:
        git(repo, "merge", "--abort", check=False)
        raise
    merge_commit = git(repo, "rev-parse", "HEAD")
    entry["state"] = "landed"; entry["phase"] = "landed"; entry["closed"] = now()
    entry["merge"] = {"commit": merge_commit, "backup_ref": backup_ref, "at": now()}
    event(entry, "land", args.seat or "claude", commit=merge_commit, backup_ref=backup_ref)
    queue_commit(repo, {"queue.json": json.dumps(state, indent=2) + NEWLINE},
                 "pairing: landed #%s" % entry["id"], args.seat or "claude", oid)
    if cfg.get("auto_cleanup", True) and not args.keep_worktree:
        state, oid = queue_load(repo); entry = find_brief(state, args.id)
        cleanup_worktree(repo, entry, delete_branch=True)
        event(entry, "cleanup", "runtime")
        queue_commit(repo, {"queue.json": json.dumps(state, indent=2) + NEWLINE},
                     "pairing: cleanup #%s" % entry["id"], "runtime", oid)
    console.say("  landed #%s at %s" % (entry["id"], merge_commit))
    return 0


def cmd_rollback(args, console: Console) -> int:
    repo = common_repo_root(resolve_repo(args)); require_local_queue(repo)
    if not working_tree_clean(repo): raise PairingError("working tree must be clean before rollback")
    state, oid = queue_load(repo); entry = find_brief(state, args.id); merge = entry.get("merge") or {}
    commit = merge.get("commit") if isinstance(merge, dict) else None
    if not commit: raise PairingError("#%s has no recorded merge commit" % entry["id"])
    try:
        git(repo, "revert", "-m", "1", str(commit), "--no-edit")
    except PairingError:
        git(repo, "revert", "--abort", check=False); raise
    revert_commit = git(repo, "rev-parse", "HEAD")
    entry["state"] = "rolled-back"; entry["phase"] = "rolled-back"
    entry["rollback"] = {"commit": revert_commit, "at": now()}
    event(entry, "rollback", args.seat or "claude", commit=revert_commit)
    queue_commit(repo, {"queue.json": json.dumps(state, indent=2) + NEWLINE},
                 "pairing: rollback #%s" % entry["id"], args.seat or "claude", oid)
    console.say("  rolled back #%s with %s" % (entry["id"], revert_commit)); return 0


def cmd_cleanup(args, console: Console) -> int:
    repo = common_repo_root(resolve_repo(args)); state, oid = queue_load(repo); entry = find_brief(state, args.id)
    cleanup_worktree(repo, entry, delete_branch=not args.keep_branch); event(entry, "cleanup", args.seat or "runtime")
    queue_commit(repo, {"queue.json": json.dumps(state, indent=2) + NEWLINE},
                 "pairing: cleanup #%s" % entry["id"], args.seat or "runtime", oid)
    console.say("  cleaned #%s" % entry["id"]); return 0


# ------------------------------- recovery / status / history -------------------------------

def process_alive(pid: object) -> bool:
    try:
        p = int(pid)
        if p <= 0: return False
        os.kill(p, 0); return True
    except (ValueError, TypeError, OSError):
        return False


def claim_status(entry: Dict[str, object]) -> str:
    claim = entry.get("claim")
    if not isinstance(claim, dict): return "none"
    if claim.get("host") == socket.gethostname() and claim.get("pid"):
        if process_alive(claim.get("pid")): return "alive"
        return "dead"
    exp = parse_time(str(claim.get("lease_expires") or ""))
    if exp and datetime.now(timezone.utc) > exp: return "stale"
    return "unknown"


def cmd_resume(args, console: Console) -> int:
    repo = common_repo_root(resolve_repo(args)); cfg = effective_config_for_args(repo, args); state, oid = queue_load(repo); entry = find_brief(state, args.id)
    cs = claim_status(entry)
    console.say("  #%s state=%s phase=%s claim=%s round=%s" % (entry["id"], entry["state"], entry.get("phase"), cs, entry.get("round", 0)))
    if entry["state"] == "reported":
        console.say("  implementation reported; audit it"); return 0
    if entry["state"] == "claimed" and cs == "alive":
        console.say("  %s process still appears alive; not touching claim" % (entry.get("implementer") or entry.get("seat") or "implementer")); return 0
    if entry["state"] == "claimed" and (cs in ("stale", "dead") or args.force):
        if not args.requeue and not args.redispatch:
            console.say("  stale claim; use --requeue or --redispatch"); return 1
        requeue_entry(repo, entry, state, oid, args.seat or "claude", "resume recovered stale claim")
        if args.redispatch:
            return dispatch_once(repo, args.id, cfg, console, args.allow_dirty, args.force,
                                 getattr(args, "implementer", None) or str(entry.get("implementer") or "") or None)
        return 0
    if args.redispatch and entry["state"] == "ready":
        return dispatch_once(repo, args.id, cfg, console, args.allow_dirty, args.force,
                             getattr(args, "implementer", None) or str(entry.get("implementer") or "") or None)
    return 0


def cmd_events(args, console: Console) -> int:
    repo = resolve_repo(args); state, _ = queue_load(repo); entry = find_brief(state, args.id); events = entry.get("events", [])
    if args.ndjson:
        for e in events: console.say(json.dumps(e, sort_keys=True))
    else: console.say(json.dumps(events, indent=2, sort_keys=True))
    return 0


def cmd_status(args, console: Console) -> int:
    root = repo_root(args.repo) if getattr(args, "repo", None) else repo_root(); main = common_repo_root(root) if root else None
    cfg = effective_config_for_args(main, args) if main else load_config()
    if getattr(args, "implementer", None): cfg["implementer"] = args.implementer
    try:
        agent = resolve_implementer(cfg, getattr(args, "implementer", None))
    except PairingError:
        agent = str(cfg.get("implementer") or "codex")
    console.say("ghostrider %s" % VERSION)
    console.say("  enabled=%s profile=%s protocol=%s max_rounds=%s implementer=%s" %
                ("ON" if cfg.get("enabled") else "OFF", cfg.get("profile"), cfg.get("protocol"), cfg.get("max_rounds"), agent))
    if agent == "codex": console.say("  codex sandbox=%s" % cfg.get("codex_sandbox"))
    elif agent == "cline": console.say("  cline thinking=%s auto_approve=%s" % (cfg.get("cline_thinking"), cfg.get("cline_auto_approve")))
    elif agent == "copilot": console.say("  copilot model=%s mode=%s yolo=%s" % (cfg.get("copilot_model"), cfg.get("copilot_mode"), cfg.get("copilot_yolo")))
    if main:
        tier, why = detect_tier(main, cfg); console.say("  repo=%s\n  tier=%s (%s)" % (main, tier, why))
        if args.id and channel_ready(main):
            state, _ = queue_load(main); e = find_brief(state, args.id)
            console.say("  #%s state=%s phase=%s round=%s claim=%s implementer=%s" %
                        (e["id"], e["state"], e.get("phase"), e.get("round", 0), claim_status(e), e.get("implementer") or "-"))
            if e.get("branch"): console.say("  branch=%s" % e["branch"])
            if e.get("worktree"): console.say("  worktree=%s" % e["worktree"])
    return 0


def cmd_channel(args, console: Console) -> int:
    root = repo_root(args.repo) if getattr(args, "repo", None) else repo_root(); cfg = effective_config(root) if root else load_config(); tier, why = detect_tier(root, cfg)
    console.say("  tier=%s repo=%s reason=%s" % (tier, root or "-", why))
    if args.ensure and root and not channel_ready(root):
        ns = argparse.Namespace(repo=str(root), seat=args.seat, dry_run=args.dry_run); return cmd_init(ns, console)
    return 0


def cmd_doctor(args, console: Console) -> int:
    cfg = load_config()
    selected = None
    try:
        selected = resolve_implementer(cfg, getattr(args, "implementer", None))
    except PairingError:
        selected = str(getattr(args, "implementer", None) or cfg.get("implementer") or "codex")
    checks = [
        ("python>=3.8", sys.version_info >= (3, 8), sys.version.split()[0], True),
        ("git", bool(shutil.which("git")), shutil.which("git") or "missing", True),
    ]
    for agent in IMPLEMENTERS:
        cmd = implementer_command_name(agent, cfg)
        required = (selected == agent) or (str(cfg.get("implementer") or "") == "auto" and agent == selected)
        checks.append((agent, bool(shutil.which(cmd)), shutil.which(cmd) or "missing", required))
    checks.append(("gh optional", bool(shutil.which("gh")), shutil.which("gh") or "missing", False))
    failed = False
    for name_, ok, detail, required in checks:
        if required and not ok:
            failed = True
        tag = "REQ" if required else "OPT"
        console.say("  %-12s %-4s %-3s %s" % (name_, "OK" if ok else "MISS", tag, detail))
    console.say("  selected implementer: %s" % selected)
    if selected == "copilot" and not bool(cfg.get("copilot_yolo", False)):
        console.say("  note: copilot-yolo is OFF; programmatic edits may require persisted permissions or enabling it explicitly")
    return 2 if failed else 0


def cmd_config(args, console: Console) -> int:
    raw = load_raw_config(); cfg = load_config()
    if not args.key: console.say(json.dumps(cfg, indent=2, sort_keys=True)); return 0
    key = args.key.replace("-", "_")
    if args.value is None: console.say(json.dumps(cfg.get(key))); return 0
    value: object = args.value
    if key in ("enabled", "auto_init_local", "require_clean_tree", "auto_verify", "use_worktrees", "auto_cleanup", "auto_land", "cline_auto_approve", "copilot_yolo"):
        value = parse_bool(args.value)
    elif key in ("max_rounds", "lease_minutes", "verification_timeout"):
        value = int(args.value)
    elif key in ("codex_extra_args", "cline_extra_args", "copilot_extra_args", "implementer_priority"):
        value = json.loads(args.value)
        if not isinstance(value, list): raise PairingError("%s must be a JSON array" % key.replace("_", "-"))
    if key == "codex_sandbox" and value not in ("read-only", "workspace-write", "danger-full-access"):
        raise PairingError("invalid sandbox")
    if key == "implementer" and str(value).lower() not in ("auto",) + IMPLEMENTERS:
        raise PairingError("implementer must be auto, codex, cline, or copilot")
    if key == "cline_thinking" and value not in ("none", "low", "medium", "high", "xhigh"):
        raise PairingError("invalid Cline thinking level")
    if key == "copilot_mode" and value not in ("interactive", "plan", "autopilot"):
        raise PairingError("invalid Copilot mode")
    if key == "dispatch_output" and value not in ("log", "inherit"):
        raise PairingError("dispatch-output must be log or inherit")
    raw[key] = value; save_raw_config(raw); console.say("  %s=%s" % (key, json.dumps(value))); return 0


def cmd_switch(args, console: Console, enabled: bool) -> int:
    raw = load_raw_config(); raw["enabled"] = enabled; save_raw_config(raw); console.say("  pairing is %s" % ("ON" if enabled else "OFF")); return 0


def cmd_profile(args, console: Console) -> int:
    raw = load_raw_config()
    if args.action == "list":
        names = sorted(set(BUILTIN_PROFILES) | set((raw.get("profiles") or {}).keys() if isinstance(raw.get("profiles"), dict) else []))
        for n in names: console.say(("* " if n == str(raw.get("profile") or DEFAULT_CONFIG["profile"]) else "  ") + n)
        return 0
    name = args.name or str(raw.get("profile") or DEFAULT_CONFIG["profile"])
    if args.action == "show":
        cfg = effective_config(None, name); console.say(json.dumps({k: cfg[k] for k in ("profile","implementer","max_rounds","lease_minutes","auto_verify","use_worktrees","auto_cleanup","codex_sandbox","cline_thinking","copilot_model","copilot_mode","verification_timeout")}, indent=2)); return 0
    if args.action == "use":
        if name not in BUILTIN_PROFILES and not (isinstance(raw.get("profiles"), dict) and name in raw["profiles"]): raise PairingError("unknown profile %s" % name)
        raw["profile"] = name; save_raw_config(raw); console.say("  profile=%s" % name); return 0
    if args.action == "set":
        if not args.key or args.value is None: raise PairingError("profile set needs name key value")
        profiles = raw.setdefault("profiles", {})
        if not isinstance(profiles, dict): raise PairingError("profiles config is not an object")
        p = profiles.setdefault(name, {})
        if not isinstance(p, dict): raise PairingError("profile %s is not an object" % name)
        val: object = args.value
        if args.key in ("max_rounds","lease_minutes","verification_timeout"): val = int(args.value)
        elif args.key in ("auto_verify","use_worktrees","auto_cleanup","cline_auto_approve","copilot_yolo"): val = parse_bool(args.value)
        p[args.key.replace("-","_")] = val; save_raw_config(raw); console.say("  profile %s updated" % name); return 0
    raise PairingError("unknown profile action")


def cmd_protocol(args, console: Console) -> int:
    if args.action == "brief-template":
        console.say(json.dumps({"goal":"", "scope":[], "constraints":[], "acceptance":[], "files":[], "tests":[], "exclude":[], "unknowns":[]}, indent=2)); return 0
    if args.action == "report-template":
        console.say(json.dumps({"delta":[], "commit":"", "tests":[], "blockers":[], "uncertainties":[], "evidence":[]}, indent=2)); return 0
    if args.action == "audit-template":
        console.say(json.dumps({"evidence":[], "tests":[], "findings":[], "uncertainties":[]}, indent=2)); return 0
    body = Path(args.file).read_text(encoding="utf-8") if args.file else (sys.stdin.read() if not sys.stdin.isatty() else "")
    if args.action == "lint":
        ok, notes = lint_brief(body, strict=True); console.say(("PASS" if ok else "FAIL") + (": " + "; ".join(notes) if notes else "")); return 0 if ok else 1
    if args.action == "decode": console.say(json.dumps(cvm_parse(body), indent=2)); return 0
    if args.action == "stats":
        # Character/word counts are intentionally labeled proxies, not claimed token counts.
        console.say(json.dumps({"chars": len(body), "words_proxy": len(body.split()), "lines": len(body.splitlines())}, indent=2)); return 0
    raise PairingError("unknown protocol action")


def cmd_purge_queue(args, console: Console) -> int:
    repo = resolve_repo(args)
    if not args.yes: raise PairingError("purge deletes queue history; pass --yes")
    git(repo, "update-ref", "-d", CHANNEL_REF); console.say("  queue removed"); return 0


# ------------------------------- CLI -------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pairctl.py", description="Multi-agent ghostrider runtime for Claude, Codex, Cline, and Copilot")
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--repo"); parser.add_argument("--seat"); parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--profile"); parser.add_argument("--implementer", choices=("auto",) + IMPLEMENTERS)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repo", default=argparse.SUPPRESS); common.add_argument("--seat", default=argparse.SUPPRESS)
    common.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS)
    common.add_argument("--profile", default=argparse.SUPPRESS)
    common.add_argument("--implementer", choices=("auto",) + IMPLEMENTERS, default=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command")
    def c(name: str, help_: str) -> argparse.ArgumentParser: return sub.add_parser(name, help=help_, parents=[common])

    p = c("status", "configuration and lifecycle status"); p.add_argument("id", nargs="?")
    c("doctor", "check local prerequisites")
    p = c("config", "show/set config"); p.add_argument("key", nargs="?"); p.add_argument("value", nargs="?")
    c("on", "enable pairing"); c("off", "disable pairing")
    p = c("profile", "manage profiles"); p.add_argument("action", choices=("list","show","use","set")); p.add_argument("name", nargs="?"); p.add_argument("key", nargs="?"); p.add_argument("value", nargs="?")
    p = c("protocol", "CAVEMAN/1 helpers"); p.add_argument("action", choices=("brief-template","report-template","audit-template","lint","decode","stats")); p.add_argument("--file")
    p = c("channel", "detect coordination channel"); p.add_argument("--ensure", action="store_true")
    c("init", "initialize local queue")
    p = c("brief", "post brief"); p.add_argument("title"); p.add_argument("--file"); p.add_argument("--spec"); p.add_argument("--strict", action="store_true")
    p = c("queue", "list briefs"); p.add_argument("--json", action="store_true")
    p = c("claim", "claim brief"); p.add_argument("id", nargs="?")
    p = c("requeue", "requeue brief"); p.add_argument("id"); p.add_argument("--note")
    p = c("report", "implementation report"); p.add_argument("id"); p.add_argument("--verdict", required=True, choices=("pass","fail","blocked")); p.add_argument("--file"); p.add_argument("--spec")
    p = c("show", "show brief"); p.add_argument("id"); p.add_argument("--wire", action="store_true"); p.add_argument("--json", action="store_true")
    p = c("events", "machine-readable lifecycle events"); p.add_argument("id"); p.add_argument("--ndjson", action="store_true")
    p = c("dispatch", "dispatch selected implementing CLI"); p.add_argument("id"); p.add_argument("--allow-dirty", action="store_true"); p.add_argument("--force", action="store_true")
    p = c("verify", "discover/run verification"); p.add_argument("id", nargs="?"); p.add_argument("--list", action="store_true"); p.add_argument("--json", action="store_true")
    p = c("pair", "create/resume a brief and run implement+verify repair loop"); p.add_argument("title", nargs="?"); p.add_argument("--id"); p.add_argument("--file"); p.add_argument("--spec"); p.add_argument("--allow-dirty", action="store_true"); p.add_argument("--force", action="store_true")
    p = c("audit", "record independent auditor verdict"); p.add_argument("id"); p.add_argument("--verdict", required=True, choices=("pass","fail","block")); p.add_argument("--file"); p.add_argument("--spec"); p.add_argument("--redispatch", action="store_true"); p.add_argument("--allow-dirty", action="store_true"); p.add_argument("--force", action="store_true")
    p = c("land", "merge only audit-passed work"); p.add_argument("id"); p.add_argument("--force", action="store_true"); p.add_argument("--keep-worktree", action="store_true")
    p = c("rollback", "revert a recorded landed merge"); p.add_argument("id")
    p = c("cleanup", "remove worktree/branch"); p.add_argument("id"); p.add_argument("--keep-branch", action="store_true")
    p = c("resume", "recover/resume interrupted round"); p.add_argument("id"); p.add_argument("--requeue", action="store_true"); p.add_argument("--redispatch", action="store_true"); p.add_argument("--allow-dirty", action="store_true"); p.add_argument("--force", action="store_true")
    p = c("purge-queue", "delete queue history"); p.add_argument("--yes", action="store_true")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser(); args = parser.parse_args(argv)
    for attr, default in (("repo",None),("seat",None),("dry_run",False),("profile",None),("implementer",None)):
        if not hasattr(args, attr): setattr(args, attr, default)
    if not args.command: parser.print_help(); return 0
    console = Console()
    handlers = {
        "status": cmd_status, "doctor": cmd_doctor, "config": cmd_config,
        "on": lambda a,c: cmd_switch(a,c,True), "off": lambda a,c: cmd_switch(a,c,False),
        "profile": cmd_profile, "protocol": cmd_protocol, "channel": cmd_channel, "init": cmd_init,
        "brief": cmd_brief, "queue": cmd_queue, "claim": cmd_claim, "requeue": cmd_requeue,
        "report": cmd_report, "show": cmd_show, "events": cmd_events, "dispatch": cmd_dispatch,
        "verify": cmd_verify, "pair": cmd_pair, "audit": cmd_audit, "land": cmd_land,
        "rollback": cmd_rollback, "cleanup": cmd_cleanup, "resume": cmd_resume, "purge-queue": cmd_purge_queue,
    }
    try:
        if args.command == "pair" and not args.id and not args.title:
            raise PairingError("pair requires TITLE for a new brief, or --id to resume")
        return handlers[args.command](args, console)
    except PairingError as exc:
        console.warn(str(exc)); return 1
    except (PermissionError, OSError) as exc:
        console.warn("%s: %s" % (type(exc).__name__, exc)); return 2


if __name__ == "__main__":
    raise SystemExit(main())
