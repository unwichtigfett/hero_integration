"""Reject failed, empty or incomplete CI reports, including skipped regressions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET


def read_cases(path):
    # Frappe writes one XML document per test category to the same output file.
    # xmlrunner also emits a single testsuites document for the offline runner.
    text = Path(path).read_text(encoding="utf-8")
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise ValueError("DTD/entity declarations are not accepted")
    documents = re.split(r"<\?xml\s[^?]*\?>", text)
    cases = {}
    for document in documents:
        if not document.strip():
            continue
        root = ET.fromstring(document)
        if root.tag not in {"testsuite", "testsuites"}:
            raise ValueError(f"Unexpected report root: {root.tag}")
        for case in root.iter("testcase"):
            identity = f"{case.attrib['classname']}.{case.attrib['name']}"
            outcome = "passed"
            for state in ("error", "failure", "skipped"):
                if case.find(state) is not None:
                    outcome = state
                    break
            # Imported TestCase classes can legitimately be rediscovered by
            # Frappe/unittest. Count an identity only once and keep its worst
            # outcome: a second success must never hide a failure or skip.
            severity = {"passed": 0, "skipped": 1, "failure": 2, "error": 3}
            previous = cases.get(identity, "passed")
            cases[identity] = max((previous, outcome), key=severity.__getitem__)
    if not cases:
        raise ValueError("Report contains no test cases")
    return cases


def check_report(report, manifest, phase, minimum):
    if minimum < 1:
        raise ValueError("Minimum successful test count must be positive")
    cases = read_cases(report)
    groups = json.loads(Path(manifest).read_text(encoding="utf-8"))[phase]
    if not groups or any(not tests for tests in groups.values()):
        raise ValueError("Required regression groups must not be empty")
    counts = {state: sum(value == state for value in cases.values())
              for state in ("passed", "skipped", "failure", "error")}
    problems = []
    for identity, outcome in sorted(cases.items()):
        if outcome != "passed":
            print(f"{outcome.upper()}: {identity}")
        if outcome in {"failure", "error"}:
            problems.append(f"{outcome}: {identity}")
    for group, identities in groups.items():
        for identity in identities:
            outcome = cases.get(identity, "missing")
            if outcome != "passed":
                problems.append(f"{group}: required test {identity} is {outcome}")
        print(f"REQUIRED {group}: " + (
            "PASS" if all(cases.get(identity) == "passed" for identity in identities) else "FAIL"
        ))
    print(f"{phase}: " + ", ".join(f"{count} {state}" for state, count in counts.items()))
    if counts["passed"] < minimum:
        problems.append(f"Only {counts['passed']} successful tests; minimum is {minimum}")
    for problem in problems:
        print(f"GATE FAILED: {problem}", file=sys.stderr)
    return not problems


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True)
    parser.add_argument("--manifest", default=str(Path(__file__).with_name("required-tests.json")))
    parser.add_argument("--phase", choices=("offline", "site"), required=True)
    parser.add_argument("--minimum", type=int, default=1000)
    args = parser.parse_args()
    try:
        return 0 if check_report(args.report, args.manifest, args.phase, args.minimum) else 1
    except (OSError, ValueError, KeyError, TypeError, ET.ParseError) as error:
        print(f"GATE FAILED: invalid or missing {args.phase} report: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
