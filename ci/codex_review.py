"""Code review with Codex (ChatGPT subscription) before a push.

Runs on vps1, where the Codex CLI is signed in with the owner's ChatGPT account.
Reviews ``BASE..HEAD`` (default: origin/hero_int_1.0..hero_int_dev) in a
temporary, detached worktree with ``codex exec`` in the read-only sandbox and a
strict JSON schema, stores the report in ~/hero-reviews/<head>.{md,json} and
exits 1 when a finding reaches the blocking severity (default P1).
The Mac push script calls it and does not push a blocked commit.

    python3 ci/codex_review.py [--head REV] [--base REV] [--block P1|P2|none] [--force]

Exit codes: 0 no blocking finding, 1 blocked, 2 the review could not run.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SEVERITIES = ("P1", "P2", "P3")
REPORTS = Path(os.environ.get("HERO_REVIEW_DIR") or Path.home() / "hero-reviews")
TIMEOUT_SECONDS = 45 * 60
CODEX = os.environ.get("CODEX_BIN") or shutil.which("codex") or str(Path.home() / ".local/bin/codex")

INSTRUCTIONS = """Du bist ein strenger Senior-Reviewer für die Frappe/ERPNext-App hero_integration
(Import und Rückschreiben von HERO-Daten: Kunden, Projekte, Aufgaben, Termine, Artikel, Dateien;
mehrere HERO-Quellen je Site; Release-Pipeline GitHub → Woodpecker → hero_int_1.0).

Prüfe ausschließlich die Änderungen im übergebenen Diff. Melde nur Fehler, die du aus dem Code
belegen kannst, mit einem konkreten Ablauf (Eingabe/Zustand → falsches Ergebnis). Keine Stilfragen,
keine Vermutungen ohne Beleg, keine Wiederholung bereits korrekt behandelter Fälle.

Schweregrade:
- P1: Datenverlust oder -korruption, Vermischung von HERO-Quellen, falsche Schreibvorgänge nach HERO
  oder ERPNext, Sicherheitslücken (Rechte, Geheimnisse, Pfade als root), kaputte Installation/Migration,
  eine Release-Sperre, die umgangen werden kann.
- P2: falsches Verhalten in einem realistischen Teilfall, fehlende Prüfung mit begrenzter Wirkung,
  Tests, die den behaupteten Fall nicht prüfen.
- P3: kleine Fehler mit geringer Wirkung.

Antworte auf Deutsch. Wenn du nichts Belegbares findest, gib eine leere Liste zurück."""

TASK = """Prüfe die Änderungen `git diff {base}..{head}` in diesem Repository (Commits:
`git log --oneline {base}..{head}`). Lies den Diff und bei Bedarf den umgebenden Code und die Tests.
Übersetzungsdateien (*.csv) und gebaute Bundles (public/dist) brauchst du nicht zu prüfen.
Gib das Ergebnis ausschließlich im vorgegebenen JSON-Format zurück."""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "findings"],
    "properties": {
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["severity", "title", "file", "line", "scenario", "fix"],
                "properties": {
                    "severity": {"type": "string", "enum": list(SEVERITIES)},
                    "title": {"type": "string"},
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "scenario": {"type": "string"},
                    "fix": {"type": "string"},
                },
            },
        },
    },
}


class ReviewError(RuntimeError):
    pass


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, text=True, capture_output=True).stdout.strip()


def run_codex(worktree: Path, base: str, head: str, runner=subprocess.run) -> dict:
    """One read-only, ephemeral Codex run in ``worktree``; returns the structured review."""
    with tempfile.TemporaryDirectory(prefix="hero-review-") as tmp:
        schema, output = Path(tmp) / "schema.json", Path(tmp) / "review.json"
        schema.write_text(json.dumps(SCHEMA))
        prompt = INSTRUCTIONS + "\n\n" + TASK.format(base=base, head=head)
        command = [CODEX, "exec", "--sandbox", "read-only", "--ephemeral", "--cd", str(worktree),
                   "--output-schema", str(schema), "--output-last-message", str(output), "-"]
        try:
            result = runner(command, input=prompt, text=True, capture_output=True, timeout=TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            raise ReviewError(f"Codex did not finish within {TIMEOUT_SECONDS // 60} minutes.") from None
        except FileNotFoundError:
            raise ReviewError(f"Codex CLI not found ({CODEX}).") from None
        if result.returncode != 0:
            tail = (result.stderr or result.stdout or "").strip().splitlines()[-5:]
            raise ReviewError("codex exec failed: " + " | ".join(tail))
        try:
            review = json.loads(output.read_text())
        except (OSError, ValueError):
            raise ReviewError("Codex returned no structured review.") from None
    if not isinstance(review, dict) or not isinstance(review.get("findings"), list):
        raise ReviewError("The Codex review has no findings list.")
    for item in review["findings"]:
        if not isinstance(item, dict) or item.get("severity") not in SEVERITIES:
            raise ReviewError("The Codex review contains an invalid finding.")
    return review


def review_commit(repo: Path, base: str, head: str, runner=subprocess.run) -> dict:
    """Review exactly ``head`` (not the working copy) in a temporary detached worktree."""
    worktree = Path(tempfile.mkdtemp(prefix="hero-review-wt-"))
    worktree.rmdir()
    git(repo, "worktree", "add", "--quiet", "--detach", str(worktree), head)
    try:
        return run_codex(worktree, base, head, runner)
    finally:
        subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(worktree)],
                       capture_output=True)


def blocking(findings: list[dict], block: str) -> list[dict]:
    if block.lower() == "none":
        return []
    limit = SEVERITIES.index(block) if block in SEVERITIES else 0
    return [item for item in findings if SEVERITIES.index(item["severity"]) <= limit]


def markdown(head: str, base: str, model: str, summaries: list[str], findings: list[dict],
             skipped: list[str], unreviewed: list[str], blocked: list[dict], block: str) -> str:
    verdict = (f"❌ **Freigabe gesperrt:** {len(blocked)} Befund(e) ab {block}." if blocked
               else "✅ **Keine sperrenden Befunde.**")
    lines = [f"## Codex-Code-Review `{head[:10]}`", "",
             f"Geprüft: `{base[:10]}..{head[:10]}` mit Codex (ChatGPT-Abo) · Sperre ab: {block}", "", verdict, ""]
    lines += [f"> {summary}" for summary in summaries if summary]
    if findings:
        lines += ["", "| Stufe | Befund | Stelle |", "|---|---|---|"]
        lines += [f"| {f['severity']} | {f['title'].replace('|', '/')} | `{f['file']}:{f['line']}` |"
                  for f in sorted(findings, key=lambda f: SEVERITIES.index(f["severity"]))]
        for f in sorted(findings, key=lambda f: SEVERITIES.index(f["severity"])):
            lines += ["", f"### {f['severity']} · {f['title']}", f"`{f['file']}:{f['line']}`", "",
                      f"**Ablauf:** {f['scenario']}", "", f"**Korrektur:** {f['fix']}"]
    if unreviewed:
        lines += ["", f"⚠️ **Nicht geprüft** (Diff zu groß): {', '.join(f'`{p}`' for p in unreviewed)}"]
    if skipped:
        lines += ["", f"Übersprungen (Übersetzungen, Bundles, Binärdateien): {len(skipped)} Datei(en)."]
    return "\n".join(lines) + "\n"


def main(argv=None, runner=subprocess.run) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--head", default="hero_int_dev")
    parser.add_argument("--base", default="origin/hero_int_1.0")
    parser.add_argument("--block", default="P1")
    parser.add_argument("--force", action="store_true", help="review again even if a report exists")
    args = parser.parse_args(argv)
    repo = args.repo.resolve()
    if args.base.startswith("origin/"):
        branch = args.base.split("/", 1)[1]
        git(repo, "fetch", "--quiet", "--no-tags", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}")
    base, head = git(repo, "rev-parse", f"{args.base}^{{commit}}"), git(repo, "rev-parse", f"{args.head}^{{commit}}")
    REPORTS.mkdir(mode=0o700, exist_ok=True)
    stored = REPORTS / f"{head}.json"
    cached = json.loads(stored.read_text()) if stored.exists() and not args.force else None
    if cached and cached.get("base") == base:
        review, reused = cached["review"], True
    elif base == head or not git(repo, "diff", "--name-only", f"{base}..{head}"):
        review, reused = {"summary": "Keine Änderungen seit dem letzten Release.", "findings": []}, False
    else:
        review, reused = review_commit(repo, base, head, runner), False
    findings = review["findings"]
    blocked = blocking(findings, args.block)
    report = markdown(head, base, "codex", [review.get("summary", "")], findings, [], [], blocked, args.block)
    if not reused:
        stored.write_text(json.dumps({"base": base, "head": head, "review": review}, ensure_ascii=False, indent=2))
        (REPORTS / f"{head}.md").write_text(report)
    print(report + (f"\n(Gespeicherter Bericht: {REPORTS / (head + '.md')})" if reused else
                    f"\nBericht: {REPORTS / (head + '.md')}"))
    return 1 if blocked else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ReviewError, subprocess.CalledProcessError) as error:
        detail = getattr(error, "stderr", "") or ""
        print(f"Codex review failed: {error} {detail}".strip() + " Nothing is pushed.", file=sys.stderr)
        sys.exit(2)
