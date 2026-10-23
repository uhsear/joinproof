#!/usr/bin/env python
"""Prove a join before you trust it: NULLed class, repeated left keys, sentinels.

A left join never fails. It keeps every left row and fills the right-hand columns with NULL when
nothing matched, so the three ways a join goes wrong all exit clean:

    1. A whole class of left rows finds no partner and comes back NULL (or blank).
    2. Left keys repeat, so one real thing appears on several rows.
    3. A placeholder such as "Not Found" is stored where a value should be. It is not NULL, so a
       "WHERE value IS NOT NULL" filter keeps it and counts it as present.

Each of these is one GROUP BY. The value is the combination: a placeholder in the class counts
as a NULL in disguise, and a small overall NULL rate can hide a class that lost every row.
Two more checks stop a join that mostly failed from passing: a lookup key listed on more than
one lookup row (a real join returns extra rows), and a share of absent rows above --max-absent.

joinproof reads two CSV exports (a left table and a lookup table), performs the left join in
memory, and refuses when a check fails. Nothing is written unless you pass --apply, and then it
writes only the one CSV that --out names, and only if that file does not exist yet.

    python joinproof.py --self-test
    python joinproof.py --left parcels.csv --right lookup.csv --key PARCEL --value SUBDIV
    python joinproof.py --left parcels.csv --right lookup.csv --key PARCEL --value SUBDIV \\
        --class-col FLAG --class-values Y,S
    python joinproof.py --left parcels.csv --right lookup.csv --key PARCEL --value SUBDIV \\
        --out flagged.csv --apply

Exit codes: 0 pass, 1 refused, 2 argparse usage error (including a refused flag prefix),
3 the --out write failed, 4 nothing joined (the check compared nothing), 64 usage error.
"""

from __future__ import print_function

import argparse
import csv
import importlib.util
import io
import os
import shutil
import sys
import tempfile

# =============================================================================
# CONFIGURATION. Deliberately not flags. Change here, not at the call site.
# =============================================================================

# Values that mean "absent" but are not NULL. Compared after trimming, ignoring case.
# Add your own with --sentinel. "0" is not here: it is a real value in many numeric columns.
DEFAULT_SENTINELS = ("not found", "n/a", "na", "none", "null", "<null>", "nan",
                     "unknown", "tbd", "-", "-9999", "-999")

# Largest share of present-looking joined values that may be sentinels before the run refuses.
DEFAULT_MAX_SENTINEL = 0.05

# Largest share of the class rows that may come back absent (no match, blank, sentinel,
# ambiguous or blank key). Zero: one lost class row refuses.
DEFAULT_MAX_CLASS_ABSENT = 0.0

# Extra rows (rows beyond the first for a key) the left table may carry before the run refuses.
DEFAULT_ALLOW_REPEATS = 0

# Largest share of ALL left rows that may come back absent (no match, blank key, blank value,
# sentinel or ambiguous) before the run refuses. A coarse net: a class check is the sharp tool.
DEFAULT_MAX_ABSENT = 0.5

# Column added to the --out file.
REASON_COLUMN = "joinproof_reason"

# =============================================================================
# End of CONFIGURATION.
# =============================================================================

PASS = "PASS"
REFUSE = "REFUSE"
NOTHING_JOINED = "NOTHING JOINED"

EXIT_FOR = {PASS: 0, REFUSE: 1, NOTHING_JOINED: 4}
EXIT_WRITE_FAILED = 3
EXIT_USAGE = 64

STATUSES = ("ok", "sentinel", "blank-value", "ambiguous", "no-match", "blank-key")


# -----------------------------------------------------------------------------
# Decision core. Pure: lists in, numbers and verdicts out. No files, no output.
# -----------------------------------------------------------------------------

class Decision(object):
    def __init__(self, verdict, reasons):
        self.verdict = verdict
        self.reasons = reasons

    @property
    def exit_code(self):
        return EXIT_FOR[self.verdict]


def require_columns(header, names, label):
    for name in names:
        if name not in header:
            raise ValueError("%s table has no column %r (columns: %s)"
                             % (label, name, ", ".join(header)))


def status_of(key, lookup, sentinels):
    """Say what the left join hands back for one left key."""
    if key == "":
        return "blank-key"
    if key not in lookup:
        return "no-match"
    values = lookup[key]
    if len(values) > 1:
        return "ambiguous"
    value = next(iter(values))
    if value == "":
        return "blank-value"
    if value.lower() in sentinels:
        return "sentinel"
    return "ok"


def analyse(lheader, lrows, rheader, rrows, key, rkey, value,
            class_col=None, class_values=(), sentinels=DEFAULT_SENTINELS):
    """Join in memory and count what comes back. Rows are lists of strings."""
    require_columns(lheader, [key] + ([class_col] if class_col is not None else []), "left")
    require_columns(rheader, [rkey, value], "right")
    sentinels = frozenset(s.strip().lower() for s in sentinels)
    classes = frozenset(c.strip().lower() for c in class_values)
    ki = lheader.index(key)
    ci = lheader.index(class_col) if class_col is not None else None
    rki = rheader.index(rkey)
    rvi = rheader.index(value)

    lookup = {}
    lookup_n = {}
    for row in rrows:
        # A blank key is stored but never read: status_of tests for a blank left key first.
        rk = row[rki].strip()
        lookup.setdefault(rk, set()).add(row[rvi].strip())
        lookup_n[rk] = lookup_n.get(rk, 0) + 1

    counts = {}
    for row in lrows:
        lk = row[ki].strip()
        if lk != "":
            counts[lk] = counts.get(lk, 0) + 1
    repeated = dict((k, n) for k, n in counts.items() if n > 1)

    by_status = dict((s, 0) for s in STATUSES)
    class_by_status = dict((s, 0) for s in STATUSES)
    class_total = other_total = other_absent = 0
    fanout_rows = fanout_extra = 0
    flagged = []
    for idx, row in enumerate(lrows):
        lk = row[ki].strip()
        status = status_of(lk, lookup, sentinels)
        by_status[status] += 1
        in_class = ci is not None and row[ci].strip().lower() in classes
        reasons = []
        if status != "ok":
            reasons.append(status)
        if lk in repeated:
            reasons.append("repeated-key")
        # A key listed on n lookup rows turns one left row into n in a real join. An ambiguous
        # key (different values) is counted as ambiguous, not here.
        fan = 0 if status == "ambiguous" else lookup_n.get(lk, 1) - 1
        if fan:
            fanout_rows += 1
            fanout_extra += fan
            reasons.append("repeated-lookup-key")
        if in_class:
            class_total += 1
            class_by_status[status] += 1
            if status != "ok":
                reasons.append("class")
        elif ci is not None:
            other_total += 1
            other_absent += status != "ok"
        if reasons:
            flagged.append((idx, reasons))

    total = len(lrows)
    present = by_status["ok"] + by_status["sentinel"]
    return {
        "total": total,
        "matched": total - by_status["no-match"] - by_status["blank-key"],
        "by_status": by_status,
        "repeated_keys": len(repeated),
        "extra_rows": sum(n - 1 for n in repeated.values()),
        "fanout_rows": fanout_rows,
        "fanout_extra": fanout_extra,
        "class_col": class_col,
        "class_total": class_total,
        "class_absent": class_total - class_by_status["ok"],
        "class_by_status": class_by_status,
        "other_total": other_total,
        "other_absent": other_absent,
        "sentinel_frac": by_status["sentinel"] / float(present) if present else 0.0,
        "flagged": flagged,
    }


def decide(rep, max_sentinel=DEFAULT_MAX_SENTINEL,
           max_class_absent=DEFAULT_MAX_CLASS_ABSENT,
           allow_repeats=DEFAULT_ALLOW_REPEATS, max_absent=DEFAULT_MAX_ABSENT):
    if rep["matched"] == 0:
        return Decision(NOTHING_JOINED, [
            "no left row matched a lookup row, so no check compared anything. Look for a key "
            "written two ways (H-001 and H-1, 12 and 12.0), a wrong --key, or an empty lookup"])
    reasons = []
    if rep["extra_rows"] > allow_repeats:
        reasons.append("left keys repeat: %d key(s), %d extra row(s), more than the %d allowed "
                       "by --allow-repeats" % (rep["repeated_keys"], rep["extra_rows"],
                                               allow_repeats))
    if rep["by_status"]["ambiguous"]:
        reasons.append("%d left row(s) hit a lookup key that carries more than one value, so a "
                       "join fans out or keeps an arbitrary one" % rep["by_status"]["ambiguous"])
    if rep["fanout_rows"]:
        reasons.append("%d left row(s) hit a lookup key that is listed on more than one lookup "
                       "row, so a real join returns %d extra row(s)"
                       % (rep["fanout_rows"], rep["fanout_extra"]))
    absent = rep["total"] - rep["by_status"]["ok"]
    if absent / float(rep["total"]) > max_absent:
        reasons.append("%d of %d left row(s) have no usable joined value (%.1f%%), more than the "
                       "%.1f%% allowed by --max-absent"
                       % (absent, rep["total"], 100.0 * absent / rep["total"], 100 * max_absent))
    if rep["class_col"] is not None:
        if rep["class_total"] == 0:
            reasons.append("no left row is in the class, so the class check compared nothing "
                           "(check --class-col and --class-values)")
        else:
            share = rep["class_absent"] / float(rep["class_total"])
            if share > max_class_absent:
                reasons.append("%d of %d class row(s) came back absent (%.1f%%), more than the "
                               "%.1f%% allowed" % (rep["class_absent"], rep["class_total"],
                                                   100 * share, 100 * max_class_absent))
    if rep["sentinel_frac"] > max_sentinel:
        reasons.append("sentinels are %.1f%% of the values that look present, more than the "
                       "%.1f%% allowed" % (100 * rep["sentinel_frac"], 100 * max_sentinel))
    return Decision(REFUSE if reasons else PASS, reasons)


def describe(rep, decision):
    st = rep["by_status"]
    lines = [
        "left rows:           %d (%d matched a lookup key)" % (rep["total"], rep["matched"]),
        "joined value:        %d ok, %d sentinel, %d blank, %d ambiguous, %d no match, "
        "%d blank key" % (st["ok"], st["sentinel"], st["blank-value"], st["ambiguous"],
                          st["no-match"], st["blank-key"]),
        "repeated left keys:  %d key(s), %d extra row(s)" % (rep["repeated_keys"],
                                                           rep["extra_rows"]),
        "repeated lookup:     %d left row(s) hit a repeated lookup key, %d extra row(s)"
        % (rep["fanout_rows"], rep["fanout_extra"]),
    ]
    if rep["class_col"] is not None:
        cs = rep["class_by_status"]
        lines.append("class %-14s %d row(s): %d absent (%d no match, %d blank, %d sentinel, "
                     "%d ambiguous, %d blank key)" % (rep["class_col"], rep["class_total"],
                                                      rep["class_absent"], cs["no-match"],
                                                      cs["blank-value"], cs["sentinel"],
                                                      cs["ambiguous"], cs["blank-key"]))
        lines.append("outside the class:  %d row(s): %d absent" % (rep["other_total"],
                                                                 rep["other_absent"]))
    for reason in decision.reasons:
        lines.append("  REFUSED: %s" % reason if decision.verdict == REFUSE
                     else "  NOTHING JOINED: %s" % reason)
    lines.append("VERDICT: %s" % decision.verdict)
    return lines


# -----------------------------------------------------------------------------
# I/O. The only code that touches files. Writing needs --apply and a new file name.
# -----------------------------------------------------------------------------

def read_table(path):
    """Read a CSV. Return (header, rows). Raise ValueError or OSError, never guess."""
    with io.open(path, "r", encoding="utf-8-sig", newline="") as fh:
        try:
            raw = list(csv.reader(fh, strict=True))
        except csv.Error as exc:
            raise ValueError("%s: not readable as CSV (%s)" % (path, exc))
        except UnicodeDecodeError as exc:
            raise ValueError("%s: not UTF-8 (%s). Re-save the export as UTF-8" % (path, exc))
    if not raw:
        raise ValueError("%s: empty file, no header row" % path)
    header = [c.strip() for c in raw[0]]
    if len(set(header)) != len(header):
        raise ValueError("%s: duplicate column names in the header" % path)
    rows = []
    for n, row in enumerate(raw[1:], 1):
        if not row:
            continue  # a blank line, usually trailing
        if len(row) != len(header):
            raise ValueError("%s: data row %d has %d cell(s), the header has %d"
                             % (path, n, len(row), len(header)))
        rows.append(row)
    return header, rows


def write_flagged(path, header, rows, flagged):
    """Write the flagged left rows to a NEW file. Never overwrites ('x' mode)."""
    if REASON_COLUMN in header:
        raise ValueError("the left table already has a column named %s" % REASON_COLUMN)
    with io.open(path, "x", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(header + [REASON_COLUMN])
        for idx, reasons in flagged:
            writer.writerow(rows[idx] + [";".join(reasons)])


# -----------------------------------------------------------------------------
# Command line
# -----------------------------------------------------------------------------

def _parse(argv):
    ap = argparse.ArgumentParser(
        prog="joinproof.py",
        allow_abbrev=False,
        description="Prove a left join before you trust it: a NULLed class, repeated left "
                    "keys and sentinel values.",
        epilog="Nothing is written without --apply, and --apply writes only the new file "
               "that --out names.")
    ap.add_argument("--left", help="CSV of the left table (the rows you keep)")
    ap.add_argument("--right", help="CSV of the lookup table")
    ap.add_argument("--key", help="join column in the left table")
    ap.add_argument("--right-key", dest="right_key", default=None,
                    help="join column in the lookup table (default: same as --key)")
    ap.add_argument("--value", help="lookup column the join brings across")
    ap.add_argument("--class-col", dest="class_col", default=None,
                    help="left column that marks the class of rows that must not be lost")
    ap.add_argument("--class-values", dest="class_values", default="",
                    help="comma separated --class-col values that put a row in the class")
    ap.add_argument("--sentinel", action="append", default=None,
                    help="extra placeholder value to treat as absent (repeatable)")
    ap.add_argument("--max-sentinel", dest="max_sentinel", type=float,
                    default=DEFAULT_MAX_SENTINEL,
                    help="largest sentinel share of present values (default %.2f)"
                         % DEFAULT_MAX_SENTINEL)
    ap.add_argument("--max-class-absent", dest="max_class_absent", type=float,
                    default=DEFAULT_MAX_CLASS_ABSENT,
                    help="largest share of class rows that may come back absent (default %.2f)"
                         % DEFAULT_MAX_CLASS_ABSENT)
    ap.add_argument("--max-absent", dest="max_absent", type=float, default=DEFAULT_MAX_ABSENT,
                    help="largest share of all left rows that may have no usable joined value "
                         "(default %.2f)" % DEFAULT_MAX_ABSENT)
    ap.add_argument("--allow-repeats", dest="allow_repeats", type=int,
                    default=DEFAULT_ALLOW_REPEATS,
                    help="extra left rows tolerated for repeated keys (default %d)"
                         % DEFAULT_ALLOW_REPEATS)
    ap.add_argument("--out", help="new CSV for the flagged left rows; written only with --apply")
    ap.add_argument("--apply", action="store_true",
                    help="write the --out file. Without it nothing is written.")
    ap.add_argument("--self-test", dest="self_test", action="store_true",
                    help="run the offline assertions and exit")
    return ap.parse_args(argv)


def _usage_problem(args):
    for flag in ("left", "right", "key", "value"):
        if getattr(args, flag) is None:
            return "--%s is required. Use --self-test to check the tool without data." % flag
    for flag in ("max_sentinel", "max_class_absent", "max_absent"):
        if not 0.0 <= getattr(args, flag) <= 1.0:
            return "--%s must be between 0 and 1." % flag.replace("_", "-")
    if args.allow_repeats < 0:
        return "--allow-repeats cannot be negative."
    if (args.class_col is None) != (args.class_values.strip() == ""):
        return "--class-col and --class-values go together."
    if args.apply and args.out is None:
        return "--apply needs --out: it is the only file joinproof writes."
    return None


def main(argv=None):
    args = _parse(sys.argv[1:] if argv is None else argv)
    if args.self_test:
        return self_test()
    problem = _usage_problem(args)
    if problem:
        print("error: %s" % problem, file=sys.stderr)
        return EXIT_USAGE
    class_values = [v for v in args.class_values.split(",") if v.strip()]
    try:
        lheader, lrows = read_table(args.left)
        rheader, rrows = read_table(args.right)
        rep = analyse(lheader, lrows, rheader, rrows, args.key, args.right_key or args.key,
                      args.value, args.class_col, class_values,
                      DEFAULT_SENTINELS + tuple(args.sentinel or ()))
    except (OSError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return EXIT_USAGE
    decision = decide(rep, args.max_sentinel, args.max_class_absent, args.allow_repeats,
                      args.max_absent)
    for line in describe(rep, decision):
        print(line)
    if args.out is not None:
        if not args.apply:
            print("Check only. %d flagged row(s) would go to %s; re-run with --apply."
                  % (len(rep["flagged"]), args.out))
        else:
            try:
                write_flagged(args.out, lheader, lrows, rep["flagged"])
            except (OSError, ValueError) as exc:
                print("error: %s" % exc, file=sys.stderr)
                return EXIT_WRITE_FAILED
            print("Wrote %d flagged row(s) to %s." % (len(rep["flagged"]), args.out))
    return decision.exit_code


# -----------------------------------------------------------------------------
# Self-test. Synthetic data, temporary files only, a local scratch directory, no network.
# -----------------------------------------------------------------------------

class Harness(object):
    def __init__(self, out):
        self.out = out
        self.passed = 0
        self.failed = []

    def check(self, cond, label):
        if cond:
            self.passed += 1
            print("PASS  %s" % label, file=self.out)
        else:
            self.failed.append(label)
            print("FAIL  %s" % label, file=self.out)

    def raises(self, fn, label, exc=ValueError):
        try:
            fn()
        except exc:
            self.check(True, label)
        except Exception as other:
            self.check(False, "%s (wrong exception %r)" % (label, other))
        else:
            self.check(False, "%s (no error raised)" % label)


def _run(argv):
    """Run main() and return (exit code, stdout text, stderr text)."""
    old = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        code = main(argv)
        return code, sys.stdout.getvalue(), sys.stderr.getvalue()
    finally:
        sys.stdout, sys.stderr = old


def _put(path, text):
    with io.open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _refusal_code(argv):
    """Parse argv and return the SystemExit code argparse raised, or None if it parsed."""
    code = None
    old = sys.stderr
    sys.stderr = io.StringIO()
    try:
        _parse(argv)
    except SystemExit as exc:
        code = exc.code
    finally:
        sys.stderr = old
    return code


LEFT_HEADER = ["PARCEL", "FLAG", "OWNER"]
RIGHT_HEADER = ["PARCEL", "SUBDIV"]


def _fixture():
    """Ten left rows, a lookup that lost the flagged ones. Synthetic."""
    left = [["P01", "N", "a"], ["P02", "N", "b"], ["P03", "Y", "c"], ["P04", "S", "d"],
            ["P05", "N", "e"], ["P06", "N", "f"], ["P07", "N", "g"], ["P08", "N", "h"],
            ["P09", "N", "i"], ["P09", "N", "j"]]
    right = [["P01", "Oak"], ["P02", "Oak"], ["P05", "Elm"], ["P06", "Not Found"],
             ["P07", "  "], ["P08", "Elm"], ["P09", "Elm"], ["", "Orphan"]]
    return left, right


def self_test():
    h = Harness(sys.stdout)
    check, raises = h.check, h.raises
    print("joinproof self-test: temporary files only, no network, no credentials")
    print("-" * 68)

    sent = frozenset(DEFAULT_SENTINELS)

    # ---- status_of: what one key gets back
    lookup = {"A": {"Oak"}, "B": {""}, "C": {"Not Found"}, "D": {"x", "y"}, "E": {"", "x"}}
    check(status_of("A", lookup, sent) == "ok", "a matched real value is ok")
    check(status_of("B", lookup, sent) == "blank-value", "a matched blank is blank-value")
    check(status_of("C", lookup, sent) == "sentinel", "Not Found is a sentinel, not a value")
    check(status_of("D", lookup, sent) == "ambiguous", "two values for one key is ambiguous")
    check(status_of("E", lookup, sent) == "ambiguous",
          "a blank and a value for one key is ambiguous")
    check(status_of("Z", lookup, sent) == "no-match", "a missing key is no-match")
    check(status_of("", lookup, sent) == "blank-key",
          "a blank left key is blank-key, never a match  <-- pinned defect")
    check(status_of("A", {"A": {"NOT FOUND"}}, sent) == "sentinel",
          "sentinels ignore case (the lookup compares lower case)")

    # ---- the combined story: one fixture, every check
    left, right = _fixture()
    rep = analyse(LEFT_HEADER, left, RIGHT_HEADER, right, "PARCEL", "PARCEL", "SUBDIV",
                  "FLAG", ["Y", "S"])
    st = rep["by_status"]
    check(rep["total"] == 10, "the report counts ten left rows")
    check(st["ok"] == 6 and st["sentinel"] == 1 and st["blank-value"] == 1,
          "six ok, one sentinel, one blank value")
    check(st["no-match"] == 2 and st["blank-key"] == 0, "two class rows found no partner")
    check(rep["matched"] == 8, "eight left rows matched a lookup key")
    check(rep["repeated_keys"] == 1 and rep["extra_rows"] == 1,
          "one key repeats, one extra row")
    check(rep["class_total"] == 2 and rep["class_absent"] == 2,
          "both class rows are lost  <-- pinned defect")
    check(rep["other_total"] == 8 and rep["other_absent"] == 2,
          "the rate outside the class is a quarter: the overall rate hides the class")
    check(abs(rep["sentinel_frac"] - 1.0 / 7) < 1e-9, "one sentinel in seven present values")
    dec = decide(rep)
    check(dec.verdict == REFUSE and dec.exit_code == 1, "the combined fixture is refused")
    check(len(dec.reasons) == 3, "it is refused for repeats, the class and sentinels")
    check(decide(rep, allow_repeats=1, max_sentinel=0.2).reasons[0].startswith("2 of 2 class"),
          "with repeats allowed and sentinels tolerated only the class check remains")
    check(decide(rep, max_class_absent=1.0, allow_repeats=1,
                 max_sentinel=1.0).verdict == PASS,
          "every limit raised to its ceiling passes the same data")

    # ---- the check each alone would miss
    left2 = [["P%02d" % i, "N", "o"] for i in range(1, 41)] + [["Q1", "Y", "o"]]
    right2 = [["P%02d" % i, "Oak"] for i in range(1, 41)]
    rep2 = analyse(LEFT_HEADER, left2, RIGHT_HEADER, right2, "PARCEL", "PARCEL", "SUBDIV",
                   "FLAG", ["Y"])
    check(rep2["by_status"]["no-match"] == 1 and rep2["sentinel_frac"] == 0.0,
          "one lost row in 41 is a 2.4% NULL rate and no sentinel")
    check(decide(rep2).verdict == REFUSE and "1 of 1 class" in decide(rep2).reasons[0],
          "a 2.4% overall NULL rate still refuses because the class lost its only row")
    rep2b = analyse(LEFT_HEADER, left2, RIGHT_HEADER, right2, "PARCEL", "PARCEL", "SUBDIV")
    check(decide(rep2b).verdict == PASS,
          "without a class the same data passes, which is why the class check exists")

    # ---- a class row that joins cleanly is not flagged
    rep2c = analyse(LEFT_HEADER, [["P01", "Y", "o"]], RIGHT_HEADER, [["P01", "Oak"]], "PARCEL",
                    "PARCEL", "SUBDIV", "FLAG", ["Y"])
    check(rep2c["class_total"] == 1 and rep2c["class_absent"] == 0 and not rep2c["flagged"],
          "a class row that finds its partner is counted and not flagged")
    check(decide(rep2c).verdict == PASS, "and a class with no loss passes")

    # ---- a sentinel in the class is a NULL in disguise
    left3 = [["A", "Y", "o"], ["B", "N", "o"]]
    right3 = [["A", "N/A"], ["B", "Oak"]]
    rep3 = analyse(LEFT_HEADER, left3, RIGHT_HEADER, right3, "PARCEL", "PARCEL", "SUBDIV",
                   "FLAG", ["Y"])
    check(rep3["class_absent"] == 1 and rep3["class_by_status"]["sentinel"] == 1,
          "a class row holding N/A counts as absent  <-- pinned defect")
    check(rep3["sentinel_frac"] == 0.5, "the sentinel share is one of two present values")

    # ---- repeats: blank keys are not a repeated key
    left4 = [["", "N", "a"], ["", "N", "b"], ["K", "N", "c"]]
    rep4 = analyse(LEFT_HEADER, left4, RIGHT_HEADER, [["K", "Oak"]], "PARCEL", "PARCEL",
                   "SUBDIV")
    check(rep4["repeated_keys"] == 0 and rep4["by_status"]["blank-key"] == 2,
          "two blank left keys are two blank-key rows, not one repeated key  <-- pinned defect")
    check(analyse(LEFT_HEADER, left4, RIGHT_HEADER, [["", "Oak"]], "PARCEL", "PARCEL",
                  "SUBDIV")["matched"] == 0,
          "a blank lookup key never matches a blank left key (NULL does not equal NULL)")
    rep4b = analyse(LEFT_HEADER, [["K", "N", "a"]] * 3, RIGHT_HEADER, [["K", "Oak"]],
                    "PARCEL", "PARCEL", "SUBDIV")
    check(rep4b["extra_rows"] == 2 and decide(rep4b).verdict == REFUSE,
          "three rows for one key are two extra rows and refuse")
    check(decide(rep4b, allow_repeats=2).verdict == PASS, "--allow-repeats 2 accepts them")
    check("repeated-key" in [r for _, rs in rep4b["flagged"] for r in rs],
          "every row of a repeated key is flagged")

    # ---- ambiguous lookup keys
    rep5 = analyse(LEFT_HEADER, [["K", "N", "a"]], RIGHT_HEADER,
                   [["K", "Oak"], ["K", "Elm"]], "PARCEL", "PARCEL", "SUBDIV")
    check(rep5["by_status"]["ambiguous"] == 1, "a lookup key with two values is ambiguous")
    check(decide(rep5).verdict == REFUSE and "more than one value" in decide(rep5).reasons[0],
          "an ambiguous lookup key refuses")
    rep5b = analyse(LEFT_HEADER, [["K", "N", "a"]], RIGHT_HEADER,
                    [["K", "Oak"], ["K", "Oak "]], "PARCEL", "PARCEL", "SUBDIV")
    check(rep5b["by_status"]["ok"] == 1,
          "the same value twice (after trimming) is not ambiguous")

    # ---- nothing joined is exit 4, not a clean pass  <-- pinned defect
    rep6 = analyse(LEFT_HEADER, [["H-1", "N", "a"]], RIGHT_HEADER, [["H-001", "Oak"]],
                   "PARCEL", "PARCEL", "SUBDIV")
    dec6 = decide(rep6)
    check(dec6.verdict == NOTHING_JOINED and dec6.exit_code == 4,
          "a key written two ways joins nothing and exits 4, not 0  <-- pinned defect")
    check(decide(analyse(LEFT_HEADER, [], RIGHT_HEADER, [["K", "x"]], "PARCEL", "PARCEL",
                         "SUBDIV")).verdict == NOTHING_JOINED,
          "an empty left table compared nothing")

    # ---- the class check must not pass vacuously  <-- pinned defect
    rep7 = analyse(LEFT_HEADER, [["K", "N", "a"]], RIGHT_HEADER, [["K", "Oak"]], "PARCEL",
                   "PARCEL", "SUBDIV", "FLAG", ["Yes"])
    check(decide(rep7).verdict == REFUSE and "compared nothing" in decide(rep7).reasons[0],
          "a class value that matches no row refuses instead of passing 0 of 0  <-- pinned defect")

    # ---- custom sentinels
    rep8 = analyse(LEFT_HEADER, [["K", "N", "a"]], RIGHT_HEADER, [["K", "0"]], "PARCEL",
                   "PARCEL", "SUBDIV")
    check(rep8["by_status"]["ok"] == 1, "0 is a real value by default")
    rep8b = analyse(LEFT_HEADER, [["K", "N", "a"]], RIGHT_HEADER, [["K", "0"]], "PARCEL",
                    "PARCEL", "SUBDIV", sentinels=("0",))
    check(rep8b["by_status"]["sentinel"] == 1, "a custom sentinel 0 is honoured")

    # ---- a join that lost most rows must not pass because a few matched  <-- pinned defect
    lost_left = [["P%d" % i, "N", "o"] for i in range(1, 101)]
    rep9 = analyse(LEFT_HEADER, lost_left, RIGHT_HEADER, [["P1", "Oak"]], "PARCEL", "PARCEL",
                   "SUBDIV")
    dec9 = decide(rep9)
    check(rep9["matched"] == 1 and dec9.verdict == REFUSE and "--max-absent" in dec9.reasons[0],
          "99 of 100 rows lost with no class refuses on the overall share  <-- pinned defect")
    check(decide(rep9, max_absent=1.0).verdict == PASS, "--max-absent 1.0 accepts the same data")
    rep9b = analyse(LEFT_HEADER, [["P1", "Y", "o"]] + lost_left[1:], RIGHT_HEADER,
                    [["P1", "Oak"]], "PARCEL", "PARCEL", "SUBDIV", "FLAG", ["Y"])
    check(rep9b["class_absent"] == 0 and decide(rep9b).verdict == REFUSE,
          "a clean class row does not hide 99 lost rows outside it  <-- pinned defect")
    rep9c = analyse(LEFT_HEADER, [["K%d" % i, "N", "o"] for i in range(1, 4)], RIGHT_HEADER,
                    [["K%d" % i, ""] for i in range(1, 4)], "PARCEL", "PARCEL", "SUBDIV")
    check(rep9c["matched"] == 3 and rep9c["by_status"]["ok"] == 0
          and rep9c["sentinel_frac"] == 0.0 and decide(rep9c).verdict == REFUSE,
          "a value column that is blank on every matched row refuses and has no sentinel share"
          "  <-- pinned defect")
    keys = ["%d" % i for i in range(1, 21)]
    rep9d = analyse(LEFT_HEADER, [[k, "N", "o"] for k in keys], RIGHT_HEADER,
                    [["1", "Oak"]] + [["%d.0" % i, "Oak"] for i in range(2, 21)], "PARCEL",
                    "PARCEL", "SUBDIV")
    check(rep9d["matched"] == 1 and decide(rep9d).verdict == REFUSE,
          "a key written 12 on one side and 12.0 on the other for 19 of 20 rows refuses"
          "  <-- pinned defect")
    half = analyse(LEFT_HEADER, [["A", "N", "o"], ["B", "N", "o"]], RIGHT_HEADER, [["A", "x"]],
                   "PARCEL", "PARCEL", "SUBDIV")
    check(decide(half).verdict == PASS and decide(half, max_absent=0.49).verdict == REFUSE,
          "exactly the allowed absent share passes and a hair under it refuses")

    # ---- a lookup key listed twice with the same value still fans a real join out
    rep10 = analyse(LEFT_HEADER, [["K", "N", "a"]], RIGHT_HEADER, [["K", "Oak"], ["K", "Oak"]],
                    "PARCEL", "PARCEL", "SUBDIV")
    dec10 = decide(rep10)
    check(rep10["by_status"]["ok"] == 1 and rep10["fanout_rows"] == 1
          and rep10["fanout_extra"] == 1,
          "a lookup key on two rows with one value is ok but counted as a fan-out"
          "  <-- pinned defect")
    check(dec10.verdict == REFUSE and "listed on more than one lookup row" in dec10.reasons[0]
          and rep10["flagged"] == [(0, ["repeated-lookup-key"])],
          "the fan-out refuses and the left row is flagged repeated-lookup-key  <-- pinned defect")
    rep10b = analyse(LEFT_HEADER, [["K", "N", "a"], ["K", "N", "b"]], RIGHT_HEADER,
                     [["K", "Oak"]] * 3, "PARCEL", "PARCEL", "SUBDIV")
    check(rep10b["fanout_rows"] == 2 and rep10b["fanout_extra"] == 4,
          "three lookup rows for a key used by two left rows add four extra rows")
    check(rep5["fanout_rows"] == 0,
          "an ambiguous key is counted as ambiguous, not as a fan-out as well")
    rep10c = analyse(LEFT_HEADER, [["A", "N", "a"]], RIGHT_HEADER,
                     [["A", "Oak"], ["Z", "Elm"], ["Z", "Elm"]], "PARCEL", "PARCEL", "SUBDIV")
    check(rep10c["fanout_rows"] == 0 and decide(rep10c).verdict == PASS,
          "a repeated lookup key that no left row uses does not refuse (see Limits)")

    # ---- keys, sentinels and class values are trimmed before they are compared
    rep11 = analyse(LEFT_HEADER, [[" K ", "N", "a"], ["K", "N", "b"]], RIGHT_HEADER,
                    [[" K", "Oak"]], "PARCEL", "PARCEL", "SUBDIV")
    check(rep11["by_status"]["ok"] == 2 and rep11["repeated_keys"] == 1,
          "a key with spaces matches a lookup key with other spaces and still counts as repeated"
          "  <-- pinned defect")
    rep11b = analyse(LEFT_HEADER, [["A", "N", "a"], ["B", "N", "b"]], RIGHT_HEADER,
                     [["A", "foo"], ["B", "FOO"]], "PARCEL", "PARCEL", "SUBDIV",
                     sentinels=(" Foo ",))
    check(rep11b["by_status"]["sentinel"] == 2,
          "a --sentinel is trimmed and matched without regard to case  <-- pinned defect")
    rep11c = analyse(LEFT_HEADER, [["A", " y ", "a"]], RIGHT_HEADER, [["A", "Oak"]], "PARCEL",
                     "PARCEL", "SUBDIV", "FLAG", ["  Y"])
    check(rep11c["class_total"] == 1,
          "a class cell and a class value are trimmed and matched without regard to case"
          "  <-- pinned defect")
    rep11d = analyse(LEFT_HEADER, [["A", "Y", "o"]] * 3 + [["B", "Y", "o"]], RIGHT_HEADER,
                     [["A", "Oak"]], "PARCEL", "PARCEL", "SUBDIV", "FLAG", ["Y"])
    check(decide(rep11d, max_class_absent=0.25, allow_repeats=2).verdict == PASS
          and decide(rep11d, max_class_absent=0.2, allow_repeats=2).verdict == REFUSE,
          "exactly the allowed class share passes and a hair under it refuses")
    sent19 = analyse(LEFT_HEADER, [["K%d" % i, "N", "o"] for i in range(20)], RIGHT_HEADER,
                     [["K%d" % i, "Oak"] for i in range(19)] + [["K19", "Not Found"]], "PARCEL",
                     "PARCEL", "SUBDIV")
    check(sent19["sentinel_frac"] == 0.05 and decide(sent19).verdict == PASS,
          "sentinels at exactly the allowed 5.0% pass")
    sent18 = analyse(LEFT_HEADER, [["K%d" % i, "N", "o"] for i in range(19)], RIGHT_HEADER,
                     [["K%d" % i, "Oak"] for i in range(18)] + [["K18", "Not Found"]], "PARCEL",
                     "PARCEL", "SUBDIV")
    check(decide(sent18).verdict == REFUSE, "and one sentinel in 19 values refuses")

    # ---- missing columns
    raises(lambda: analyse(LEFT_HEADER, [], RIGHT_HEADER, [], "NOPE", "PARCEL", "SUBDIV"),
           "a missing left key column raises")
    raises(lambda: analyse(LEFT_HEADER, [], RIGHT_HEADER, [], "PARCEL", "PARCEL", "NOPE"),
           "a missing lookup value column raises")
    raises(lambda: analyse(LEFT_HEADER, [], RIGHT_HEADER, [], "PARCEL", "PARCEL", "SUBDIV",
                           "NOPE", ["Y"]), "a missing class column raises")

    # ---- describe
    lines = describe(rep, dec)
    check(lines[-1] == "VERDICT: REFUSE", "a refused report ends with its verdict")
    check(any(l.startswith("  REFUSED:") for l in lines), "a refusal reason is labelled REFUSED")
    check(any(l.startswith("class FLAG") for l in lines), "the class line names the column")
    check(any(l.startswith("repeated lookup:") for l in describe(rep10, dec10)),
          "the report has a repeated lookup line")
    lines6 = describe(rep6, dec6)
    check(any(l.startswith("  NOTHING JOINED:") for l in lines6),
          "a nothing-joined reason is labelled NOTHING JOINED")
    lines_pass = describe(rep2b, decide(rep2b))
    check(lines_pass[-1] == "VERDICT: PASS" and not any("class" in l for l in lines_pass),
          "a pass without a class prints no class line")

    # ---- the harness itself can fail
    quiet = Harness(io.StringIO())
    quiet.check(False, "x")
    quiet.raises(lambda: None, "no error")
    quiet.raises(lambda: 1 / 0, "wrong error")
    quiet.raises(lambda: int("x"), "right error")
    check(quiet.passed == 1 and len(quiet.failed) == 3,
          "the harness reports a false check, a missing error and a wrong error")

    # ---- importing the module runs nothing. Another script may import it for the core.
    spec = importlib.util.spec_from_file_location("joinproof_imported", os.path.abspath(__file__))
    imported = importlib.util.module_from_spec(spec)
    buf = io.StringIO()
    keep = sys.dont_write_bytecode
    sys.dont_write_bytecode = True  # the self-test leaves no __pycache__ behind
    old = sys.stdout
    sys.stdout = buf
    try:
        spec.loader.exec_module(imported)
    finally:
        sys.stdout = old
        sys.dont_write_bytecode = keep
    check(buf.getvalue() == "" and imported.decide({"matched": 0}).exit_code == 4,
          "importing the module prints nothing and exposes the core")

    # ---- real files, real command line
    scratch = tempfile.mkdtemp(prefix="joinproof-")
    try:
        _files(h, scratch)
    finally:
        shutil.rmtree(scratch)

    print("-" * 68)
    total = h.passed + len(h.failed)
    print("%d assertions, %d failed" % (total, len(h.failed)))
    return 1 if h.failed else 0


def _files(h, scratch):
    check, raises = h.check, h.raises
    def p(name):
        return os.path.join(scratch, name)

    left, right = _fixture()
    lcsv = "PARCEL,FLAG,OWNER\r\n" + "".join("%s\r\n" % ",".join(r) for r in left)
    rcsv = "PARCEL,SUBDIV\r\n" + "".join("%s\r\n" % ",".join(r) for r in right)
    _put(p("left.csv"), lcsv)
    _put(p("right.csv"), rcsv)
    base = ["--left", p("left.csv"), "--right", p("right.csv"), "--key", "PARCEL",
            "--value", "SUBDIV"]

    # ---- read_table
    header, rows = read_table(p("left.csv"))
    check(header == LEFT_HEADER and len(rows) == 10, "read_table reads a header and ten rows")
    _put(p("bom.csv"), "\ufeffPARCEL,SUBDIV\n\nK,Oak\n\n")
    header, rows = read_table(p("bom.csv"))
    check(header == ["PARCEL", "SUBDIV"] and rows == [["K", "Oak"]],
          "a BOM does not rename the first column and blank lines are skipped  <-- pinned defect")
    _put(p("spaced.csv"), " PARCEL , SUBDIV \nK,Oak\n")
    check(read_table(p("spaced.csv"))[0] == ["PARCEL", "SUBDIV"],
          "header names are trimmed  <-- pinned defect")
    _put(p("empty.csv"), "")
    raises(lambda: read_table(p("empty.csv")), "an empty file raises")
    _put(p("dup.csv"), "A,A\n1,2\n")
    raises(lambda: read_table(p("dup.csv")), "duplicate header names raise")
    _put(p("ragged.csv"), "A,B\n1,2,3\n")
    raises(lambda: read_table(p("ragged.csv")),
           "a row with too many cells raises instead of shifting columns  <-- pinned defect")
    _put(p("quote.csv"), 'A,B\n"open,2\n')
    raises(lambda: read_table(p("quote.csv")), "an unterminated quote raises")
    with io.open(p("latin.csv"), "wb") as fh:
        fh.write(b"A,B\n\xff,2\n")
    raises(lambda: read_table(p("latin.csv")), "bytes that are not UTF-8 raise")
    try:
        read_table(p("latin.csv"))
    except ValueError as exc:
        check("latin.csv" in str(exc), "the not-UTF-8 error names the file  <-- pinned defect")
    raises(lambda: read_table(p("missing.csv")), "a missing file raises OSError", OSError)
    raises(lambda: read_table(scratch), "a directory raises OSError", OSError)

    # ---- the command line, for real
    code, out, err = _run(base + ["--class-col", "FLAG", "--class-values", "Y,S"])
    check(code == 1 and "VERDICT: REFUSE" in out, "the fixture exits 1 through main")
    check("2 of 2 class row(s) came back absent" in out, "the class loss is printed")
    check(sorted(os.listdir(scratch)) == sorted(["left.csv", "right.csv", "bom.csv", "empty.csv",
                                                 "dup.csv", "ragged.csv", "quote.csv",
                                                 "latin.csv", "spaced.csv"]),
          "a check run wrote no file  <-- pinned defect")
    code, out, err = _run(base + ["--allow-repeats", "1", "--max-sentinel", "1.0"])
    check(code == 0 and "VERDICT: PASS" in out,
          "without a class, with the limits raised, main exits 0")
    code, out, err = _run(base + ["--class-col", "FLAG", "--class-values", "Y",
                                  "--max-class-absent", "1.0", "--allow-repeats", "1",
                                  "--max-sentinel", "1.0", "--right-key", "PARCEL"])
    check(code == 0, "--right-key and every limit at its ceiling exit 0")
    _put(p("right2.csv"), "ID,SUBDIV\nP01,Oak\n")
    code, out, err = _run(["--left", p("left.csv"), "--right", p("right2.csv"), "--key",
                           "PARCEL", "--right-key", "ID", "--value", "SUBDIV",
                           "--allow-repeats", "9", "--max-sentinel", "1.0", "--max-absent", "1.0"])
    check(code == 0 and "1 matched" in out, "--right-key joins a lookup with another key name")
    code, out, err = _run(base + ["--sentinel", "Elm", "--allow-repeats", "1"])
    check(code == 1 and "sentinels are" in out, "--sentinel adds a placeholder value")
    _put(p("right3.csv"), "PARCEL,SUBDIV\nH-001,Oak\n")
    code, out, err = _run(["--left", p("left.csv"), "--right", p("right3.csv"), "--key",
                           "PARCEL", "--value", "SUBDIV"])
    check(code == 4 and "NOTHING JOINED" in out, "main exits 4 when nothing joined")

    # ---- usage errors
    code, out, err = _run(["--left", p("left.csv")])
    check(code == 64 and "--right is required" in err, "a missing --right exits 64")
    check(_run(base + ["--max-sentinel", "1.5"])[0] == 64, "--max-sentinel above 1 exits 64")
    check(_run(base + ["--max-class-absent", "-0.1"])[0] == 64,
          "--max-class-absent below 0 exits 64")
    check(_run(base + ["--max-absent", "1.5"])[0] == 64, "--max-absent above 1 exits 64")
    check(_run(base + ["--allow-repeats", "-1"])[0] == 64, "a negative --allow-repeats exits 64")
    check(_run(base + ["--class-col", "FLAG", "--class-values", "  "])[0] == 64,
          "a blank --class-values with --class-col exits 64")
    check(_run(base + ["--class-col", "FLAG"])[0] == 64,
          "--class-col without --class-values exits 64")
    check(_run(base + ["--class-values", "Y"])[0] == 64,
          "--class-values without --class-col exits 64")
    code, out, err = _run(base + ["--apply"])
    check(code == 64 and "--apply needs --out" in err, "--apply without --out exits 64")
    code, out, err = _run(["--left", p("missing.csv"), "--right", p("right.csv"), "--key", "PARCEL",
                           "--value", "SUBDIV"])
    check(code == 64 and "error:" in err, "an unreadable input exits 64")
    code, out, err = _run(base[:-1] + ["NOPE"])
    check(code == 64 and "no column 'NOPE'" in err, "a missing column exits 64 and names it")

    # ---- spaces and empty items in --class-values; the overall and fan-out gates through main
    code, out, err = _run(base + ["--class-col", "FLAG", "--class-values", " Y , S ",
                                  "--allow-repeats", "1", "--max-sentinel", "1.0",
                                  "--max-class-absent", "1.0"])
    check(code == 0 and "class FLAG           2 row(s)" in out,
          "--class-values is split on commas and each item trimmed")
    code, out, err = _run(base + ["--class-col", "FLAG", "--class-values", " , "])
    check(code == 1 and "compared nothing" in out,
          "a --class-values of only commas and spaces puts no row in the class")
    _put(p("blankclass.csv"), "PARCEL,FLAG\nP01,\n")
    code, out, err = _run(["--left", p("blankclass.csv"), "--right", p("right.csv"), "--key",
                           "PARCEL", "--value", "SUBDIV", "--class-col", "FLAG",
                           "--class-values", " , "])
    check(code == 1 and "compared nothing" in out,
          "a blank class cell is not in a class made of empty items  <-- pinned defect")
    _put(p("lost_l.csv"), "PARCEL,FLAG\n" + "".join("P%d,N\n" % i for i in range(1, 101)))
    _put(p("lost_r.csv"), "PARCEL,SUBDIV\nP1,Oak\n")
    lost = ["--left", p("lost_l.csv"), "--right", p("lost_r.csv"), "--key", "PARCEL",
            "--value", "SUBDIV"]
    code, out, err = _run(lost)
    check(code == 1 and "99 no match" in out and "--max-absent" in out,
          "99 of 100 rows lost exits 1 through main  <-- pinned defect")
    check(_run(lost + ["--max-absent", "1.0"])[0] == 0,
          "--max-absent 1.0 exits 0 on the same files")
    _put(p("dup_r.csv"), "PARCEL,SUBDIV\nP01,Oak\nP01,Oak\n")
    _put(p("dup_l.csv"), "PARCEL\nP01\n")
    code, out, err = _run(["--left", p("dup_l.csv"), "--right", p("dup_r.csv"), "--key", "PARCEL",
                           "--value", "SUBDIV"])
    check(code == 1 and "1 left row(s) hit a repeated lookup key, 1 extra row(s)" in out,
          "a lookup key listed twice exits 1 through main  <-- pinned defect")

    # ---- the parser: nothing abbreviates
    a = _parse(base)
    check(a.apply is False, "--apply defaults to OFF")
    check(a.out is None and a.sentinel is None and a.right_key is None,
          "--out, --sentinel and --right-key default to nothing")
    check(_parse(["--self-test"]).self_test, "--self-test parses")
    for prefix in ("--ap", "--app", "--appl"):
        check(_refusal_code(base + [prefix]) == 2,
              "a prefix %s of --apply is REFUSED by the parser  <-- pinned defect" % prefix)
    check(_refusal_code(base + ["--out-"]) == 2, "a near miss of --out is refused too")
    check(_refusal_code(base + ["--max-abs", "1"]) == 2,
          "a prefix of --max-absent is refused too")
    check(_refusal_code(base + ["--apply"]) is None, "the full --apply is accepted")

    # ---- --out: nothing is written without --apply
    out_path = p("flagged.csv")
    code, out, err = _run(base + ["--class-col", "FLAG", "--class-values", "Y,S",
                                  "--out", out_path])
    check(not os.path.exists(out_path), "--out without --apply writes nothing  <-- pinned defect")
    check("Check only. 6 flagged row(s) would go to" in out, "the check-only line counts the rows")
    code, out, err = _run(base + ["--class-col", "FLAG", "--class-values", "Y,S",
                                  "--out", out_path, "--apply"])
    check(code == 1 and os.path.exists(out_path), "--apply writes the file and keeps the verdict")
    check("Wrote 6 flagged row(s) to" in out, "the success line counts the rows written")
    header, rows = read_table(out_path)
    check(header == LEFT_HEADER + [REASON_COLUMN] and len(rows) == 6,
          "the file holds the header, a reason column and six flagged rows")
    reasons = dict((r[0] + r[2], r[3]) for r in rows)
    check(reasons["P03c"] == "no-match;class" and reasons["P09i"] == "repeated-key",
          "a lost class row says no-match;class and a repeat says repeated-key")
    check(reasons["P06f"] == "sentinel" and reasons["P07g"] == "blank-value",
          "a sentinel and a blank value are named")
    with io.open(out_path, "r", encoding="utf-8", newline="") as fh:
        before = fh.read()
    code, out, err = _run(base + ["--out", out_path, "--apply"])
    with io.open(out_path, "r", encoding="utf-8", newline="") as fh:
        after = fh.read()
    check("error:" in err, "a refused overwrite prints the error")
    check(code == 3 and before == after,
          "--apply never overwrites an existing --out file  <-- pinned defect")
    code, out, err = _run(base + ["--out", p("left.csv"), "--apply"])
    with io.open(p("left.csv"), "r", encoding="utf-8", newline="") as fh:
        left_after = fh.read()
    check(code == 3 and left_after == lcsv, "--out pointed at an input leaves the input intact")
    raises(lambda: write_flagged(p("x.csv"), ["A", REASON_COLUMN], [], []),
           "a left table that already has the reason column raises")
    check(not os.path.exists(p("x.csv")), "and that raise wrote no file")
    _put(p("clash.csv"), "PARCEL,FLAG,%s\nP01,N,x\n" % REASON_COLUMN)
    code, out, err = _run(["--left", p("clash.csv"), "--right", p("right.csv"), "--key", "PARCEL",
                           "--value", "SUBDIV", "--out", p("clash-out.csv"), "--apply"])
    check(code == 3 and not os.path.exists(p("clash-out.csv")),
          "a reason-column clash exits 3 and writes nothing")


if __name__ == "__main__":
    sys.exit(main())
