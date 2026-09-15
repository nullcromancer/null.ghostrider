#!/usr/bin/env python3
"""Install/uninstall the ghostrider skill for Claude, Codex, Cline, and Copilot.

No third-party dependencies. Python 3.8+.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple


def _force_remove(path: Path) -> None:
    """Remove a tree even when it contains read-only files.

    Git marks objects in .git/objects read-only. On Windows os.unlink refuses
    those with WinError 5, so a plain shutil.rmtree aborts the install halfway
    and leaves some targets on the old version.
    """
    def on_error(func, target, _exc):
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            pass

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=lambda f, t, e: on_error(f, t, e))
    else:
        shutil.rmtree(path, onerror=lambda f, t, e: on_error(f, t, e))

SKILL_NAME = "ghostrider"
SOURCE = Path(__file__).resolve().parent
TARGETS = ("claude", "codex", "cline", "copilot")


class InstallError(Exception):
    pass


def copytree_atomicish(source: Path, destination: Path, force: bool, dry_run: bool) -> None:
    if destination.exists() and not force:
        raise InstallError("destination already exists: %s (use --force to replace it)" % destination)
    print("  install -> %s" % destination)
    if dry_run:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(destination.name + ".installing")
    if staging.exists():
        _force_remove(staging)
    shutil.copytree(source, staging, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
    if destination.exists():
        backup = destination.with_name(destination.name + ".previous")
        if backup.exists():
            _force_remove(backup)
        destination.rename(backup)
        try:
            staging.rename(destination)
        except Exception:
            backup.rename(destination)
            raise
        _force_remove(backup)
    else:
        staging.rename(destination)


def selected_targets(target: str) -> List[str]:
    if target == "both":
        return ["claude", "codex"]
    if target == "all":
        return list(TARGETS)
    return [target]


def user_destinations(target: str) -> List[Tuple[str, Path]]:
    out: List[Tuple[str, Path]] = []
    for name in selected_targets(target):
        if name == "claude":
            root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
            out.append((name, root / "skills" / SKILL_NAME))
        elif name == "codex":
            # Codex user skills use the shared Agent Skills location.
            out.append((name, Path.home() / ".agents" / "skills" / SKILL_NAME))
        elif name == "cline":
            out.append((name, Path.home() / ".cline" / "skills" / SKILL_NAME))
        elif name == "copilot":
            root = Path(os.environ.get("COPILOT_HOME") or (Path.home() / ".copilot"))
            out.append((name, root / "skills" / SKILL_NAME))
    return out


def project_destinations(target: str, project: Path) -> List[Tuple[str, Path]]:
    out: List[Tuple[str, Path]] = []
    for name in selected_targets(target):
        if name == "claude":
            out.append((name, project / ".claude" / "skills" / SKILL_NAME))
        elif name == "codex":
            out.append((name, project / ".agents" / "skills" / SKILL_NAME))
        elif name == "cline":
            out.append((name, project / ".cline" / "skills" / SKILL_NAME))
        elif name == "copilot":
            out.append((name, project / ".github" / "skills" / SKILL_NAME))
    return out


def set_initial_config(args) -> None:
    updates: Dict[str, object] = {}
    if args.enabled is not None:
        updates["enabled"] = args.enabled == "on"
    if args.implementer is not None:
        updates["implementer"] = args.implementer
    if args.sandbox is not None:
        updates["codex_sandbox"] = args.sandbox
    if args.cline_thinking is not None:
        updates["cline_thinking"] = args.cline_thinking
    if args.copilot_model is not None:
        updates["copilot_model"] = args.copilot_model
    if args.copilot_yolo is not None:
        updates["copilot_yolo"] = args.copilot_yolo == "on"
    if not updates:
        return

    pairing_home = Path(os.environ.get("PAIRING_HOME") or (Path.home() / ".pairing"))
    config_file = pairing_home / "config.json"
    config: Dict[str, object] = {}
    if config_file.exists():
        try:
            loaded = json.loads(config_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise InstallError("cannot update %s: %s" % (config_file, exc))
        if not isinstance(loaded, dict):
            raise InstallError("%s must contain a JSON object" % config_file)
        config = loaded
    config.update(updates)
    print("  config  -> %s" % config_file)
    if args.dry_run:
        return
    pairing_home.mkdir(parents=True, exist_ok=True)
    config_file.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def uninstall(destinations: List[Tuple[str, Path]], dry_run: bool) -> int:
    removed = 0
    for label, dest in destinations:
        if not dest.exists():
            print("  %-7s not installed: %s" % (label, dest))
            continue
        print("  %-7s remove -> %s" % (label, dest))
        if not dry_run:
            _force_remove(dest)
        removed += 1
    print("\n  removed %d skill installation(s)" % removed)
    print("  shared pairing config and repository queue history were left intact")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Install ghostrider for Claude Code, Codex, Cline, GitHub Copilot, or all four."
    )
    parser.add_argument("--target", choices=TARGETS + ("both", "all"), default="all",
                        help="both = Claude+Codex; all = Claude+Codex+Cline+Copilot")
    parser.add_argument("--scope", choices=("user", "project"), default="user")
    parser.add_argument("--project", help="project root for --scope project (default: current directory)")
    parser.add_argument("--force", action="store_true", help="replace an existing installed copy")
    parser.add_argument("--uninstall", action="store_true", help="remove the skill from selected target(s)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--enabled", choices=("on", "off"), help="initial shared pairing state")
    parser.add_argument("--implementer", choices=("auto", "codex", "cline", "copilot"),
                        help="default implementing CLI")
    parser.add_argument("--sandbox", choices=("read-only", "workspace-write", "danger-full-access"),
                        help="Codex sandbox used by pairctl dispatch")
    parser.add_argument("--cline-thinking", choices=("none", "low", "medium", "high", "xhigh"))
    parser.add_argument("--copilot-model", help="Copilot CLI model name, e.g. auto")
    parser.add_argument("--copilot-yolo", choices=("on", "off"),
                        help="allow Copilot non-interactive runs all permissions; OFF by default")
    args = parser.parse_args(argv)

    if not (SOURCE / "SKILL.md").exists() or not (SOURCE / "scripts" / "pairctl.py").exists():
        raise InstallError("run install.py from an intact ghostrider skill directory")

    if args.scope == "user":
        destinations = user_destinations(args.target)
    else:
        project = Path(args.project or Path.cwd()).resolve()
        destinations = project_destinations(args.target, project)

    if args.uninstall:
        return uninstall(destinations, args.dry_run)

    print("ghostrider skill v4\n")
    for label, dest in destinations:
        print("  target: %-7s %s" % (label, dest))
    print()
    for _, dest in destinations:
        copytree_atomicish(SOURCE, dest, args.force, args.dry_run)
    set_initial_config(args)

    print("\n  installed")
    chosen = selected_targets(args.target)
    if "claude" in chosen:
        print("  Claude Code:    /ghostrider status")
    if "codex" in chosen:
        print("  Codex CLI:      $ghostrider status")
    if "cline" in chosen:
        print("  Cline:          ask it to use the ghostrider skill")
        print("                  (Cline Skills may need to be enabled in Cline settings)")
    if "copilot" in chosen:
        print("  Copilot CLI:    /ghostrider status")
    print("\n  Run: python scripts/pairctl.py doctor")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except InstallError as exc:
        print("  ! %s" % exc, file=sys.stderr)
        raise SystemExit(1)
