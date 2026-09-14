#!/usr/bin/env python3
"""End-to-end standard-library smoke tests for ghostrider v4."""

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "install.py"
PAIRCTL = ROOT / "scripts" / "pairctl.py"


def run(args, *, env, cwd=None, input_text=None, expect=0):
    proc = subprocess.run(
        [sys.executable] + [str(x) for x in args],
        cwd=str(cwd) if cwd else None,
        env=env,
        input=input_text,
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != expect:
        raise AssertionError(
            "command failed (%s != %s): %s\nstdout:\n%s\nstderr:\n%s"
            % (proc.returncode, expect, args, proc.stdout, proc.stderr)
        )
    return proc


FAKE_AGENT = r'''#!/usr/bin/env python3
import json, os, re, subprocess, sys, tempfile
from pathlib import Path
agent = Path(sys.argv[0]).stem.lower()
args = sys.argv[1:]
if agent == "codex":
    if not args or args[0] != "exec": raise SystemExit(2)
    wt = Path(args[args.index("-C") + 1])
    prompt = args[-1]
elif agent == "cline":
    wt = Path(args[args.index("--cwd") + 1])
    prompt = args[-1]
elif agent == "copilot":
    wt = Path(args[args.index("-C") + 1])
    prompt = args[args.index("-p") + 1]
else:
    raise SystemExit(9)
m = re.search(r'python "([^"]*pairctl\.py)" show (\d+) --wire --repo "([^"]+)"', prompt)
if not m:
    print(prompt, file=sys.stderr)
    raise SystemExit(4)
ctl, bid, main = m.group(1), m.group(2), m.group(3)
log = subprocess.run(["git", "-C", str(wt), "log", "--oneline", "--grep=fake-round"], text=True, capture_output=True).stdout
round_no = len([x for x in log.splitlines() if x.strip()]) + 1
with open(wt / "base.txt", "a", encoding="utf-8") as fh:
    if os.environ.get("FAKE_REPAIR") == "1" and round_no == 1:
        fh.write("wrong\n")
    else:
        fh.write("paired\n")
subprocess.run(["git", "-C", str(wt), "add", "base.txt"], check=True)
subprocess.run(["git", "-C", str(wt), "commit", "-qm", "fake-round-%d" % round_no], check=True)
sha = subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"], text=True, capture_output=True, check=True).stdout.strip()
spec = {"delta":["%s implementation round %d" % (agent, round_no)], "commit":sha, "tests":[], "blockers":[], "uncertainties":[], "evidence":["base.txt"]}
fd, path = tempfile.mkstemp(suffix=".json")
os.close(fd)
Path(path).write_text(json.dumps(spec), encoding="utf-8")
try:
    rc = subprocess.run([sys.executable, ctl, "report", bid, "--verdict", "pass", "--spec", path, "--repo", main, "--seat", agent]).returncode
finally:
    os.unlink(path)
raise SystemExit(rc)
'''


class PairingV4Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.home = self.base / "home"
        self.pairing_home = self.base / "pairing"
        self.repo = self.base / "repo"
        self.bin = self.base / "bin"
        for p in (self.home, self.pairing_home, self.repo, self.bin):
            p.mkdir()
        self.env = dict(os.environ)
        self.env["HOME"] = str(self.home)
        self.env["PAIRING_HOME"] = str(self.pairing_home)
        self.env["PATH"] = str(self.bin) + os.pathsep + self.env.get("PATH", "")
        self.env["COPILOT_HOME"] = str(self.home / ".copilot")

        subprocess.run(["git", "-C", str(self.repo), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Smoke"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "smoke@example.invalid"], check=True)
        (self.repo / "base.txt").write_text("base\n", encoding="utf-8")
        (self.repo / "Makefile").write_text("test:\n\tgrep -q paired base.txt\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "base"], check=True)

        for name in ("codex", "cline", "copilot"):
            exe = self.bin / ((name + ".exe") if os.name == "nt" else name)
            exe.write_text(FAKE_AGENT, encoding="utf-8")
            if os.name != "nt":
                exe.chmod(exe.stat().st_mode | stat.S_IXUSR)

    def tearDown(self):
        self.tmp.cleanup()

    def brief_spec(self):
        path = self.base / "brief.json"
        path.write_text(json.dumps({
            "goal": "make base.txt contain paired",
            "scope": ["base.txt only"],
            "constraints": ["preserve existing base line"],
            "acceptance": ["make test passes"],
            "files": ["base.txt", "Makefile:test"],
            "tests": ["make test"],
            "exclude": ["no unrelated files"],
            "unknowns": [],
        }), encoding="utf-8")
        return path

    def audit_spec(self):
        path = self.base / "audit.json"
        path.write_text(json.dumps({
            "evidence": ["diff inspected"], "tests": ["make test => 0"],
            "findings": [], "uncertainties": []
        }), encoding="utf-8")
        return path

    def pair(self, implementer="codex", env=None):
        return run([
            PAIRCTL, "pair", "Smoke", "--spec", self.brief_spec(), "--repo", self.repo,
            "--profile", "standard", "--implementer", implementer
        ], env=env or self.env)

    def test_install_all_and_protocol(self):
        run([INSTALL, "--target", "all", "--enabled", "on"], env=self.env)
        self.assertTrue((self.home / ".claude/skills/ghostrider/SKILL.md").is_file())
        self.assertTrue((self.home / ".agents/skills/ghostrider/SKILL.md").is_file())
        self.assertTrue((self.home / ".cline/skills/ghostrider/SKILL.md").is_file())
        self.assertTrue((self.home / ".copilot/skills/ghostrider/SKILL.md").is_file())
        tmpl = run([PAIRCTL, "protocol", "brief-template"], env=self.env).stdout
        self.assertIn('"acceptance"', tmpl)

    def test_project_install_all(self):
        run([INSTALL, "--target", "all", "--scope", "project", "--project", self.repo], env=self.env)
        self.assertTrue((self.repo / ".claude/skills/ghostrider/SKILL.md").is_file())
        self.assertTrue((self.repo / ".agents/skills/ghostrider/SKILL.md").is_file())
        self.assertTrue((self.repo / ".cline/skills/ghostrider/SKILL.md").is_file())
        self.assertTrue((self.repo / ".github/skills/ghostrider/SKILL.md").is_file())

    def test_compact_queue_and_events(self):
        run([PAIRCTL, "init", "--repo", self.repo], env=self.env)
        run([PAIRCTL, "brief", "Smoke", "--spec", self.brief_spec(), "--strict", "--repo", self.repo], env=self.env)
        wire = run([PAIRCTL, "show", "1", "--wire", "--repo", self.repo], env=self.env).stdout
        self.assertIn("CVM1|B", wire)
        self.assertIn("G|make base.txt contain paired", wire)
        ev = run([PAIRCTL, "events", "1", "--ndjson", "--repo", self.repo], env=self.env).stdout
        self.assertIn('"type": "brief"', ev)
        status = subprocess.run(["git", "-C", str(self.repo), "status", "--porcelain"], text=True, capture_output=True, check=True).stdout
        self.assertEqual("", status)

    def test_pair_worktree_verify_audit_land_and_rollback(self):
        proc = self.pair("codex")
        self.assertIn("AUDIT-READY", proc.stdout)
        shown = run([PAIRCTL, "show", "1", "--json", "--repo", self.repo], env=self.env).stdout
        entry = json.loads(shown)
        self.assertEqual("codex", entry["implementer"])
        self.assertEqual("audit-ready", entry["phase"])
        self.assertTrue(entry["worktree"])
        self.assertTrue(Path(entry["worktree"]).exists())
        self.assertNotIn("paired", (self.repo / "base.txt").read_text(encoding="utf-8"))

        run([PAIRCTL, "audit", "1", "--verdict", "pass", "--spec", self.audit_spec(), "--repo", self.repo, "--seat", "claude"], env=self.env)
        run([PAIRCTL, "land", "1", "--repo", self.repo, "--seat", "claude"], env=self.env)
        self.assertIn("paired", (self.repo / "base.txt").read_text(encoding="utf-8"))
        landed = json.loads(run([PAIRCTL, "show", "1", "--json", "--repo", self.repo], env=self.env).stdout)
        self.assertEqual("landed", landed["state"])
        self.assertFalse(landed.get("worktree"))

        run([PAIRCTL, "rollback", "1", "--repo", self.repo, "--seat", "claude"], env=self.env)
        self.assertNotIn("paired", (self.repo / "base.txt").read_text(encoding="utf-8"))
        rolled = json.loads(run([PAIRCTL, "show", "1", "--json", "--repo", self.repo], env=self.env).stdout)
        self.assertEqual("rolled-back", rolled["state"])

    def test_automatic_verification_repair_round(self):
        env = dict(self.env)
        env["FAKE_REPAIR"] = "1"
        proc = self.pair("codex", env=env)
        self.assertIn("automatic repair round", proc.stdout)
        entry = json.loads(run([PAIRCTL, "show", "1", "--json", "--repo", self.repo], env=env).stdout)
        self.assertEqual(2, entry["round"])
        self.assertEqual("audit-ready", entry["phase"])
        self.assertGreaterEqual(len(entry.get("verification", [])), 2)

    def test_cline_adapter_end_to_end(self):
        proc = self.pair("cline")
        self.assertIn("[cline]", proc.stdout)
        entry = json.loads(run([PAIRCTL, "show", "1", "--json", "--repo", self.repo], env=self.env).stdout)
        self.assertEqual("cline", entry["implementer"])
        self.assertTrue(str(entry["branch"]).startswith("cline/"))
        self.assertEqual("audit-ready", entry["phase"])

    def test_copilot_adapter_end_to_end(self):
        proc = self.pair("copilot")
        self.assertIn("[copilot]", proc.stdout)
        entry = json.loads(run([PAIRCTL, "show", "1", "--json", "--repo", self.repo], env=self.env).stdout)
        self.assertEqual("copilot", entry["implementer"])
        self.assertTrue(str(entry["branch"]).startswith("copilot/"))
        self.assertEqual("audit-ready", entry["phase"])

    def test_auto_routes_to_priority(self):
        run([PAIRCTL, "config", "implementer", "auto"], env=self.env)
        run([PAIRCTL, "config", "implementer-priority", '["cline","copilot","codex"]'], env=self.env)
        proc = self.pair("auto")
        self.assertIn("[cline]", proc.stdout)
        entry = json.loads(run([PAIRCTL, "show", "1", "--json", "--repo", self.repo], env=self.env).stdout)
        self.assertEqual("cline", entry["implementer"])


if __name__ == "__main__":
    unittest.main()
