#!/usr/bin/env python3
"""Apply the source dependency audit policy to one `make dep-audit` run.

Inputs are the raw scanner outputs that run.sh saved in the run directory.
Outputs are report.json and report.md in the same directory, plus a short
summary on stdout.

Policy: a High or Critical finding with a published fix fails the gate unless
ignore.toml has a current exception for that advisory and package. Every other
finding is reported but does not fail the gate.

Exit status: 0 pass, 1 policy failure, 2 scanner, evidence, or ignore-file error.
"""

import argparse
import datetime as dt
import json
import re
import sys
import tomllib
from pathlib import Path


GRYPE_VERSION = "0.116.1"
BLOCKING_SEVERITIES = ("critical", "high")
SEVERITY_ORDER = ("critical", "high", "medium", "moderate", "low", "negligible", "info", "unknown")
MAX_DB_AGE = dt.timedelta(hours=24)
MAX_CLOCK_SKEW = dt.timedelta(minutes=5)
MAX_IGNORE_DAYS = 90
ADVISORY_ID = re.compile(r"(GHSA(-[23456789cfghjmpqrvwx]{4}){3}|CVE-\d{4}-\d{4,}|RUSTSEC-\d{4}-\d{4}|PYSEC-\d{4}-\d+)\Z")
REQUIREMENT_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?==([^\s;\\]+)")


class AuditError(RuntimeError):
    """A scanner result, input inventory, or ignore entry is not trustworthy."""


def normalize(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def load_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditError(f"cannot read {path.name}: {exc}") from exc


def read_exit(path):
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError) as exc:
        raise AuditError(f"cannot read scanner exit status {path.name}") from exc


# --- ignore file -------------------------------------------------------------

def load_ignores(path, today):
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise AuditError(f"cannot read ignore file: {exc}") from exc
    unknown = set(document) - {"ignore"}
    if unknown:
        raise AuditError(f"ignore file has unknown top-level keys: {sorted(unknown)}")
    entries = document.get("ignore", [])
    if not isinstance(entries, list):
        raise AuditError("ignore file: `ignore` must be an array of tables")
    result = []
    for number, entry in enumerate(entries, start=1):
        where = f"ignore entry {number}"
        if not isinstance(entry, dict):
            raise AuditError(f"{where} is not a table")
        allowed = {"id", "package", "lock", "reason", "reviewed", "expires"}
        if set(entry) - allowed:
            raise AuditError(f"{where} has unknown keys: {sorted(set(entry) - allowed)}")
        for key in ("id", "package", "reason", "reviewed", "expires"):
            if key not in entry:
                raise AuditError(f"{where} has no `{key}`")
        if not isinstance(entry["id"], str) or not ADVISORY_ID.fullmatch(entry["id"]):
            raise AuditError(f"{where}: `id` is not a GHSA, CVE, RUSTSEC, or PYSEC identifier")
        if not isinstance(entry["package"], str) or not entry["package"]:
            raise AuditError(f"{where}: `package` must be a package name")
        if "lock" in entry and (not isinstance(entry["lock"], str) or not entry["lock"]):
            raise AuditError(f"{where}: `lock` must be a repository-relative lock file path")
        if not isinstance(entry["reason"], str) or len(entry["reason"].split()) < 8:
            raise AuditError(f"{where}: `reason` must explain the decision (8 words or more)")
        reviewed, expires = entry["reviewed"], entry["expires"]
        if type(reviewed) is not dt.date or type(expires) is not dt.date:
            raise AuditError(f"{where}: `reviewed` and `expires` must be TOML dates (YYYY-MM-DD)")
        if reviewed > today:
            raise AuditError(f"{where}: `reviewed` is in the future")
        if not reviewed < expires <= reviewed + dt.timedelta(days=MAX_IGNORE_DAYS):
            raise AuditError(
                f"{where}: `expires` must be after `reviewed` and at most {MAX_IGNORE_DAYS} days later")
        result.append({
            "id": entry["id"],
            "package": entry["package"],
            "lock": entry.get("lock"),
            "reason": " ".join(entry["reason"].split()),
            "reviewed": reviewed.isoformat(),
            "expires": expires.isoformat(),
            "expired": expires < today,
            "used": False,
        })
    return result


# --- Grype (Python and Rust) -------------------------------------------------

def parse_timestamp(value):
    if not isinstance(value, str):
        raise AuditError("Grype database build time is missing")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AuditError("Grype database build time is invalid") from exc
    if parsed.tzinfo is None:
        raise AuditError("Grype database build time has no time zone")
    return parsed.astimezone(dt.timezone.utc)


def validate_grype(report, exit_code, now):
    """Check that the report came from the pinned scanner and release policy."""
    if not isinstance(report, dict):
        raise AuditError("Grype report root is not an object")
    descriptor, source = report.get("descriptor"), report.get("source")
    matches, ignored = report.get("matches"), report.get("ignoredMatches", [])
    if not isinstance(descriptor, dict) or not isinstance(source, dict):
        raise AuditError("Grype report has no descriptor or source")
    if not isinstance(matches, list) or not isinstance(ignored, list):
        raise AuditError("Grype report has malformed matches")
    if ignored:
        raise AuditError("Grype ignored matches itself; exceptions belong in ignore.toml only")
    if descriptor.get("name") != "grype" or descriptor.get("version") != GRYPE_VERSION:
        raise AuditError("Grype report is not from the pinned scanner version")
    if source.get("type") != "directory" or source.get("target") != "/inputs":
        raise AuditError("Grype report is not a scan of the staged lock files")
    configuration = descriptor.get("configuration")
    if not isinstance(configuration, dict):
        raise AuditError("Grype report has no effective configuration")
    required = {
        "check-for-app-update": False,
        "only-fixed": False,
        "only-notfixed": False,
        "ignore-wontfix": "",
        "ignore": [],
        "exclude": [],
        "fail-on-severity": "high",
        "vex-documents": [],
        "vex-add": [],
    }
    for field, wanted in required.items():
        if configuration.get(field) != wanted:
            raise AuditError(f"Grype effective configuration changed `{field}`")
    database_config = configuration.get("db")
    required_db = {
        "auto-update": True,
        "validate-by-hash-on-start": True,
        "validate-age": True,
        "max-allowed-built-age": 86_400_000_000_000,
        "require-update-check": True,
    }
    if not isinstance(database_config, dict) or any(
            database_config.get(key) != value for key, value in required_db.items()):
        raise AuditError("Grype database policy is not fail-closed")
    database = descriptor.get("db")
    status = database.get("status") if isinstance(database, dict) else None
    if not isinstance(status, dict) or status.get("valid") is not True:
        raise AuditError("Grype vulnerability database is not valid")
    built = parse_timestamp(status.get("built"))
    if built > now + MAX_CLOCK_SKEW or now - built > MAX_DB_AGE:
        raise AuditError("Grype vulnerability database is older than 24 hours")

    at_threshold = 0
    for match in matches:
        vulnerability = match.get("vulnerability") if isinstance(match, dict) else None
        severity = vulnerability.get("severity") if isinstance(vulnerability, dict) else None
        if not isinstance(severity, str) or not severity:
            raise AuditError("Grype match has no severity")
        if severity.lower() in BLOCKING_SEVERITIES:
            at_threshold += 1
    # grype.yaml sets fail-on-severity: high, so the exit status must agree
    # with the report. Disagreement means the run cannot be trusted.
    if not ((exit_code == 0 and at_threshold == 0) or (exit_code == 2 and at_threshold > 0)):
        raise AuditError(
            f"Grype exit status {exit_code} does not agree with {at_threshold} High/Critical matches")
    return {
        "schema_version": status.get("schemaVersion"),
        "built": status.get("built"),
        "source": status.get("from", "").split("?", 1)[0],
    }


def expected_packages(path):
    """Parse the packages a lock file pins, to prove the scanner read them all."""
    text = path.read_text(encoding="utf-8")
    if path.name == "Cargo.lock":
        packages = set()
        for block in tomllib.loads(text).get("package", []):
            packages.add((normalize(block["name"]), block["version"]))
        return packages
    packages = set()
    for line in text.splitlines():
        match = REQUIREMENT_PIN.match(line)
        if match:
            packages.add((normalize(match.group(1)), match.group(3)))
    return packages


def check_inventory(inventory, inputs_dir):
    """Every pinned package in every staged lock file must be in the scan."""
    found = {}
    for component in inventory.get("components", []):
        properties = {
            item.get("name"): item.get("value")
            for item in component.get("properties", [])
            if isinstance(item, dict)
        }
        for key, value in properties.items():
            if isinstance(key, str) and re.fullmatch(r"syft:location:\d+:path", key):
                found.setdefault(value.lstrip("/"), set()).add(
                    (normalize(component.get("name", "")), component.get("version")))
    coverage = []
    for path in sorted(p for p in inputs_dir.rglob("*") if p.is_file()):
        relative = path.relative_to(inputs_dir).as_posix()
        expected = expected_packages(path)
        if not expected:
            raise AuditError(f"{relative} pins no packages; the lock parser or file is wrong")
        missing = expected - found.get(relative, set())
        if missing:
            sample = ", ".join(f"{name}=={version}" for name, version in sorted(missing)[:5])
            raise AuditError(
                f"Grype did not read {len(missing)} of {len(expected)} packages in {relative} ({sample})")
        coverage.append({"lock": relative, "ecosystem": "rust" if path.name == "Cargo.lock" else "python",
                         "packages": len(expected)})
    if not coverage:
        raise AuditError("no Python or Cargo lock files were staged")
    return coverage


def grype_findings(report):
    rows = {}
    for match in report["matches"]:
        vulnerability, artifact = match["vulnerability"], match.get("artifact", {})
        fix = vulnerability.get("fix") or {}
        aliases = sorted({item.get("id") for item in match.get("relatedVulnerabilities", [])
                          if item.get("id") and item.get("id") != vulnerability.get("id")})
        locations = [item.get("path", "").lstrip("/") for item in artifact.get("locations", [])]
        if not locations or not all(locations):
            raise AuditError(f"Grype match {vulnerability.get('id')} has no lock file location")
        for lock in locations:
            key = (lock, normalize(artifact.get("name", "")), artifact.get("version"), vulnerability.get("id"))
            rows[key] = {
                "source": "grype",
                "ecosystem": artifact.get("type"),
                "lock": lock,
                "package": artifact.get("name"),
                "version": artifact.get("version"),
                "id": vulnerability.get("id"),
                "aliases": aliases,
                "severity": vulnerability["severity"].lower(),
                "fixable": fix.get("state") == "fixed",
                "fixed_in": ", ".join(fix.get("versions") or []) or None,
                "url": vulnerability.get("dataSource"),
            }
    return list(rows.values())


# --- npm audit ----------------------------------------------------------------

def npm_name(directory):
    return directory.replace("/", "_").replace(".", "_")


def npm_fixed_in(vulnerable):
    """The first patched version, when the range is one ">=a <b" or "<b" clause."""
    match = re.fullmatch(r"(?:>=?\s*\S+\s+)?<\s*(\S+)", vulnerable.strip())
    return match.group(1) if match else f"outside {vulnerable}"


def npm_findings(run_dir):
    try:
        directories = [line for line in (run_dir / "npm-dirs.txt").read_text().splitlines() if line]
    except OSError as exc:
        raise AuditError("npm lock file list is missing") from exc
    findings, coverage = [], []
    for directory in directories:
        name = npm_name(directory)
        lock_path = directory + "/package-lock.json" if directory != "." else "package-lock.json"
        document = load_json(run_dir / "npm-audit" / f"{name}.json")
        exit_code = read_exit(run_dir / "npm-audit" / f"{name}.exit")
        if not isinstance(document, dict) or "error" in document:
            detail = document.get("error", {}).get("summary") if isinstance(document, dict) else None
            raise AuditError(f"npm audit failed for {lock_path}: {detail or 'no report'}")
        metadata = document.get("metadata")
        vulnerabilities = document.get("vulnerabilities")
        if document.get("auditReportVersion") != 2 or not isinstance(metadata, dict) \
                or not isinstance(vulnerabilities, dict):
            raise AuditError(f"npm audit report for {lock_path} is not audit report version 2")
        totals = metadata.get("vulnerabilities", {}).get("total")
        dependencies = metadata.get("dependencies", {}).get("total")
        if not isinstance(totals, int) or not isinstance(dependencies, int) or dependencies < 1:
            raise AuditError(f"npm audit report for {lock_path} has no dependency count")
        if not ((exit_code == 0 and totals == 0) or (exit_code == 1 and totals > 0)):
            raise AuditError(f"npm audit exit status {exit_code} for {lock_path} does not agree with its report")
        lock = load_json(run_dir / "inputs" / "npm" / lock_path).get("packages", {})
        coverage.append({"lock": lock_path, "ecosystem": "npm", "packages": dependencies})
        for package, entry in vulnerabilities.items():
            versions = sorted({lock.get(node, {}).get("version") for node in entry.get("nodes", [])} - {None})
            for via in entry.get("via", []):
                if not isinstance(via, dict):
                    continue  # propagated from another package; that package has its own entry
                url = via.get("url", "")
                advisory = url.rstrip("/").rsplit("/", 1)[-1]
                if not ADVISORY_ID.fullmatch(advisory):
                    raise AuditError(f"npm advisory for {package} in {lock_path} has no GHSA identifier")
                vulnerable = via.get("range", "")
                # GitHub advisories that have a patched version state an exclusive
                # upper bound ("<1.2.2"). "*" or "<=x" means no patched release.
                fixable = re.search(r"<(?!=)", vulnerable) is not None
                findings.append({
                    "source": "npm-audit",
                    "ecosystem": "npm",
                    "lock": lock_path,
                    "package": package,
                    "version": ", ".join(versions) or None,
                    "id": advisory,
                    "aliases": [],
                    "severity": str(via.get("severity", "unknown")).lower(),
                    "fixable": fixable,
                    "fixed_in": npm_fixed_in(vulnerable) if fixable else None,
                    "url": url,
                })
    return findings, coverage


# --- policy and report --------------------------------------------------------

def apply_policy(findings, ignores):
    for finding in findings:
        finding["ignored_by"] = None
        names = {finding["id"], *finding["aliases"]}
        if finding["severity"] not in BLOCKING_SEVERITIES or not finding["fixable"]:
            finding["status"] = "reported"
            continue
        finding["status"] = "blocking"
        for entry in ignores:
            if entry["id"] in names and normalize(entry["package"]) == normalize(finding["package"]) \
                    and entry["lock"] in (None, finding["lock"]):
                if entry["expired"]:
                    finding["ignore_expired"] = entry["expires"]
                    continue
                entry["used"] = True
                finding["status"] = "ignored"
                finding["ignored_by"] = {"reason": entry["reason"], "expires": entry["expires"]}
                break
    return findings


def severity_rank(finding):
    try:
        return SEVERITY_ORDER.index(finding["severity"])
    except ValueError:
        return len(SEVERITY_ORDER)


def write_markdown(path, report):
    lines = [
        "# Source dependency audit",
        "",
        f"- Result: **{report['result'].upper()}**",
        f"- Run at: {report['generated_at']}",
        f"- Grype: {report['scanners']['grype']['image']}",
        f"- Grype database: schema {report['scanners']['grype']['database']['schema_version']}, "
        f"built {report['scanners']['grype']['database']['built']}",
        f"- npm: {report['scanners']['npm_audit']['npm_version']} "
        f"({report['scanners']['npm_audit']['image']})",
        "- Policy: a fixable High or Critical finding fails unless "
        "`development/dep-audit/ignore.toml` has a current exception.",
        "",
        "## Inputs",
        "",
        "| Lock file | Ecosystem | Packages |",
        "|---|---|---:|",
    ]
    lines += [f"| `{row['lock']}` | {row['ecosystem']} | {row['packages']} |" for row in report["inputs"]]
    lines += ["", "## Findings by severity", "", "| Severity | Fixable | Not fixable |", "|---|---:|---:|"]
    for severity, counts in report["counts"].items():
        lines.append(f"| {severity} | {counts['fixable']} | {counts['not_fixable']} |")
    high = [row for row in report["findings"] if row["severity"] in BLOCKING_SEVERITIES]
    lines += ["", "## High and Critical findings", ""]
    if high:
        lines += ["| Status | Severity | Package | Version | Advisory | Fixed in | Lock file |",
                  "|---|---|---|---|---|---|---|"]
        for row in high:
            status = row["status"]
            if row["ignored_by"]:
                status += f" until {row['ignored_by']['expires']}"
            if not row["fixable"]:
                status += " (no fix)"
            lines.append(
                f"| {status} | {row['severity']} | {row['package']} | {row['version']} | "
                f"{row['id']} | {row['fixed_in'] or '-'} | `{row['lock']}` |")
    else:
        lines.append("None.")
    lines += ["", "## Exceptions", ""]
    if report["ignores"]:
        for entry in report["ignores"]:
            state = "expired" if entry["expired"] else ("used" if entry["used"] else "unused")
            lines.append(f"- {entry['id']} `{entry['package']}` ({state}, expires {entry['expires']}): "
                         f"{entry['reason']}")
    else:
        lines.append("None.")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--ignore", required=True, type=Path)
    parser.add_argument("--grype-image", required=True)
    parser.add_argument("--grype-policy-sha256", required=True)
    parser.add_argument("--report-path", required=True, help="host path, for the summary only")
    args = parser.parse_args(argv)
    run_dir = args.run_dir
    now = dt.datetime.now(dt.timezone.utc)

    ignores = load_ignores(args.ignore, now.date())
    grype_report = load_json(run_dir / "grype.json")
    database = validate_grype(grype_report, read_exit(run_dir / "grype.exit"), now)
    coverage = check_inventory(load_json(run_dir / "inventory.cdx.json"), run_dir / "inputs" / "grype")
    npm_rows, npm_coverage = npm_findings(run_dir)
    findings = apply_policy(grype_findings(grype_report) + npm_rows, ignores)
    findings.sort(key=lambda row: (row["status"] != "blocking", severity_rank(row),
                                   row["package"].lower(), row["lock"], row["id"]))

    counts = {}
    for row in findings:
        bucket = counts.setdefault(row["severity"], {"fixable": 0, "not_fixable": 0})
        bucket["fixable" if row["fixable"] else "not_fixable"] += 1
    counts = dict(sorted(counts.items(), key=lambda item: severity_rank({"severity": item[0]})))
    blocking = [row for row in findings if row["status"] == "blocking"]
    report = {
        "schema": "robot-vault.dep-audit/1",
        "generated_at": now.isoformat(timespec="seconds"),
        "result": "fail" if blocking else "pass",
        "policy": {
            "fail_on": list(BLOCKING_SEVERITIES),
            "fixable_only": True,
            "ignore_file": "development/dep-audit/ignore.toml",
            "max_ignore_days": MAX_IGNORE_DAYS,
        },
        "scanners": {
            "grype": {"image": args.grype_image, "version": GRYPE_VERSION,
                      "policy_sha256": args.grype_policy_sha256, "database": database},
            "npm_audit": {
                "image": "NODE_CHECK_IMAGE from the Makefile",
                "npm_version": (run_dir / "npm-audit" / "npm-version.txt").read_text().strip(),
            },
        },
        "inputs": coverage + npm_coverage,
        "counts": counts,
        "findings": findings,
        "ignores": ignores,
    }
    (run_dir / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_markdown(run_dir / "report.md", report)

    high = [row for row in findings if row["severity"] in BLOCKING_SEVERITIES]
    print(f"dep-audit: {len(report['inputs'])} lock files, "
          f"{sum(row['packages'] for row in report['inputs'])} package entries, "
          f"Grype DB built {database['built']}")
    print(f"dep-audit: High/Critical: {len(blocking)} blocking, "
          f"{sum(row['status'] == 'ignored' for row in high)} ignored, "
          f"{sum(not row['fixable'] for row in high)} without a fix; "
          f"{len(findings) - len(high)} lower-severity findings reported")
    for row in blocking:
        expired = f" (exception expired {row['ignore_expired']})" if row.get("ignore_expired") else ""
        print(f"  BLOCKING {row['severity']:8} {row['package']} {row['version']} {row['id']} "
              f"fixed in {row['fixed_in']} [{row['lock']}]{expired}")
    for entry in ignores:
        if entry["expired"]:
            print(f"  warning: exception {entry['id']} for {entry['package']} expired {entry['expires']}")
        elif not entry["used"]:
            print(f"  warning: exception {entry['id']} for {entry['package']} matched nothing; remove it")
    print(f"dep-audit: {report['result'].upper()} - report {args.report_path}")
    return 1 if blocking else 0


def write_error(argv, error):
    """Leave an error report so a failed run still has a saved result."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--run-dir", type=Path)
    known, _ = parser.parse_known_args(argv)
    if known.run_dir and known.run_dir.is_dir():
        document = {"schema": "robot-vault.dep-audit/1", "result": "error", "error": str(error),
                    "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
        (known.run_dir / "report.json").write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AuditError as error:
        write_error(sys.argv[1:], error)
        print(f"dep-audit: ERROR: {error}", file=sys.stderr)
        raise SystemExit(2)
