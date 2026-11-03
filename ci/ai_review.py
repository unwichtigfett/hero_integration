"""Automatic OpenAI code review of the changes since the last release.

Reviews the diff ``BASE..HEAD`` (default: origin/hero_int_1.0..HEAD) with the
OpenAI Responses API and structured output, writes a Markdown report (GitHub
job summary) and a JSON report, and exits 1 when a finding reaches the blocking
severity (default P1). Only the standard library is used.

Environment:
  OPENAI_API_KEY       required
  OPENAI_REVIEW_MODEL  model name (default below)
  AI_REVIEW_BLOCK      P1 (default), P2, or "none" for a report-only run
  AI_REVIEW_BASE       base revision (default origin/hero_int_1.0)
  GITHUB_STEP_SUMMARY  Markdown report target (set by GitHub Actions)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API_URL = "https://api.openai.com/v1/responses"
DEFAULT_MODEL = "gpt-5"
SEVERITIES = ("P1", "P2", "P3")
MAX_CHUNK_CHARS = 120_000
MAX_CHUNKS = 8
# Generated, vendored or purely textual files carry no reviewable logic.
SKIPPED_SUFFIXES = (".csv", ".po", ".mo", ".min.js", ".map", ".lock", ".png", ".jpg", ".svg", ".pdf")
SKIPPED_PARTS = ("/public/dist/", "node_modules/")

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


# ---------------------------------------------------------------- diff

def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, text=True, capture_output=True).stdout


def reviewable(path: str) -> bool:
    return not path.endswith(SKIPPED_SUFFIXES) and not any(part in f"/{path}" for part in SKIPPED_PARTS)


def file_diffs(base: str, head: str) -> tuple[list[tuple[str, str]], list[str]]:
    """(path, unified diff) per reviewable file, and the skipped paths."""
    paths = [line for line in git("diff", "--name-only", f"{base}..{head}").splitlines() if line]
    keep = [path for path in paths if reviewable(path)]
    return [(path, git("diff", "--unified=20", f"{base}..{head}", "--", path)) for path in keep], \
        [path for path in paths if path not in keep]


def chunks(diffs: list[tuple[str, str]], limit: int = MAX_CHUNK_CHARS,
           max_chunks: int = MAX_CHUNKS) -> tuple[list[str], list[str]]:
    """Group file diffs into at most ``max_chunks`` requests of about ``limit`` characters.

    A single file larger than the limit is cut and marked as cut; the files of
    groups beyond ``max_chunks`` are returned as unreviewed."""
    groups: list[tuple[list[str], list[str]]] = []  # (paths, diffs)
    for path, diff in diffs:
        if len(diff) > limit:
            diff = diff[:limit] + f"\n[... Diff von {path} gekürzt ...]\n"
        if not groups or sum(map(len, groups[-1][1])) + len(diff) > limit:
            groups.append(([], []))
        groups[-1][0].append(path)
        groups[-1][1].append(diff)
    reviewed = ["".join(texts) for _, texts in groups[:max_chunks]]
    unreviewed = [path for paths, _ in groups[max_chunks:] for path in paths]
    return reviewed, unreviewed


# ---------------------------------------------------------------- OpenAI

def request(api_key: str, model: str, diff: str, opener=urllib.request.urlopen, retries: int = 2) -> dict:
    body = json.dumps({
        "model": model,
        "instructions": INSTRUCTIONS,
        "input": f"Diff (git unified, 20 Zeilen Kontext):\n\n{diff}",
        "text": {"format": {"type": "json_schema", "name": "code_review", "schema": SCHEMA, "strict": True}},
    }).encode()
    for attempt in range(retries + 1):
        req = urllib.request.Request(API_URL, data=body, method="POST", headers={
            "Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
        try:
            with opener(req, timeout=600) as response:
                return parse(json.loads(response.read()))
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")[:500]
            if error.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(10 * (attempt + 1))
                continue
            raise ReviewError(f"OpenAI API HTTP {error.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError) as error:
            if attempt < retries:
                time.sleep(10 * (attempt + 1))
                continue
            raise ReviewError(f"OpenAI API not reachable: {error}") from None
    raise ReviewError("OpenAI API request failed.")


def parse(payload: dict) -> dict:
    """The structured review from a Responses API payload."""
    if payload.get("status") not in (None, "completed"):
        raise ReviewError(f"OpenAI response is {payload.get('status')}: {payload.get('incomplete_details')}")
    for item in payload.get("output") or []:
        contents = (item.get("content") or []) if item.get("type") == "message" else []
        for content in contents:
            if content.get("type") == "refusal":
                raise ReviewError(f"The model refused the review: {content.get('refusal')}")
            if content.get("type") == "output_text":
                review = json.loads(content["text"])
                if not isinstance(review.get("findings"), list):
                    raise ReviewError("The review has no findings list.")
                return review
    raise ReviewError("The OpenAI response contains no review.")


# ---------------------------------------------------------------- report

def blocking(findings: list[dict], block: str) -> list[dict]:
    if block.lower() == "none":
        return []
    limit = SEVERITIES.index(block) if block in SEVERITIES else 0
    return [item for item in findings if SEVERITIES.index(item["severity"]) <= limit]


def markdown(head: str, base: str, model: str, summaries: list[str], findings: list[dict],
             skipped: list[str], unreviewed: list[str], blocked: list[dict], block: str) -> str:
    verdict = (f"❌ **Freigabe gesperrt:** {len(blocked)} Befund(e) ab {block}." if blocked
               else "✅ **Keine sperrenden Befunde.**")
    lines = [f"## OpenAI-Code-Review `{head[:10]}`", "",
             f"Geprüft: `{base[:10]}..{head[:10]}` mit `{model}` · Sperre ab: {block}", "", verdict, ""]
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


def main() -> int:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        print("OPENAI_API_KEY is not set: add it as a GitHub repository secret. The release stays blocked.",
              file=sys.stderr)
        return 2
    model = os.environ.get("OPENAI_REVIEW_MODEL", "").strip() or DEFAULT_MODEL
    block = os.environ.get("AI_REVIEW_BLOCK", "").strip() or "P1"
    base = git("rev-parse", os.environ.get("AI_REVIEW_BASE", "").strip() or "origin/hero_int_1.0").strip()
    head = git("rev-parse", "HEAD").strip()
    diffs, skipped = file_diffs(base, head)
    groups, unreviewed = chunks(diffs)
    summaries, findings = [], []
    for diff in groups:
        review = request(api_key, model, diff)
        summaries.append(review.get("summary", ""))
        findings += review["findings"]
    blocked = blocking(findings, block)
    report = markdown(head, base, model, summaries, findings, skipped, unreviewed, blocked, block)
    print(report)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
            summary.write(report)
    Path("ai-review.json").write_text(json.dumps({"head": head, "base": base, "model": model, "block": block,
                                                  "findings": findings, "unreviewed": unreviewed}, ensure_ascii=False, indent=2))
    return 1 if blocked else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ReviewError as error:
        print(f"AI review failed: {error}. The release stays blocked.", file=sys.stderr)
        sys.exit(2)
