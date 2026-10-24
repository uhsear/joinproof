# joinproof

Prove a left join before you trust it. Checks the things that a join never reports: a class of
rows that came back NULL, left keys that repeat, lookup keys that fan a join out, placeholder
values such as "Not Found" that pass as real data, and a join that lost most of its rows.

A parcel layer is joined to a subdivision lookup. The lookup was built from a helper table, and
the helper left out one flagged class of parcels. Those parcels found no partner. The left join
kept them, as a left join does, and gave each one a NULL subdivision.

Nothing failed. The job exited 0 and the layer published. Weeks later someone exported "every
parcel in this subdivision" for a mailing. The flagged parcels were missing from every export,
because the filter was `SubdivisionName IS NOT NULL`.

A second fault sat beside it. The lookup stored the text `Not Found` for parcels that matched
nothing. That is not NULL, so the same filter kept those rows and counted them as present. A
third fault came with the layer itself: a few dozen parcel numbers appeared on more than one
row, so the exports listed them twice. Each fault is one `GROUP BY`. No one had run any of them.

`joinproof` runs them together, on two CSV exports, and refuses when one fails. Run on its
own, a check can pass while the join is still wrong. One lost row in 41 is a 2.4 percent NULL
rate, which looks small, even when that row is the only flagged parcel in the file. So the tool
reports the class on its own line, and it counts a placeholder in the class as a NULL.

```
$ python joinproof.py --left left.csv --right lookup.csv --key PARCEL --value SUBDIV \
      --class-col FLAG --class-values Y,S --out flagged.csv
left rows:           10 (8 matched a lookup key)
joined value:        6 ok, 1 sentinel, 1 blank, 0 ambiguous, 2 no match, 0 blank key
repeated left keys:  1 key(s), 1 extra row(s)
repeated lookup:     0 left row(s) hit a repeated lookup key, 0 extra row(s)
class FLAG           2 row(s): 2 absent (2 no match, 0 blank, 0 sentinel, 0 ambiguous, 0 blank key)
outside the class:  8 row(s): 2 absent
  REFUSED: left keys repeat: 1 key(s), 1 extra row(s), more than the 0 allowed by --allow-repeats
  REFUSED: 2 of 2 class row(s) came back absent (100.0%), more than the 0.0% allowed
  REFUSED: sentinels are 14.3% of the values that look present, more than the 5.0% allowed
VERDICT: REFUSE
Check only. 6 flagged row(s) would go to flagged.csv; re-run with --apply.
```

The data above is synthetic. These are the exact rows, and the self-test builds the same files.

```
left.csv                      lookup.csv
PARCEL,FLAG,OWNER             PARCEL,SUBDIV
P01,N,a   P06,N,f             P01,Oak
P02,N,b   P07,N,g             P02,Oak
P03,Y,c   P08,N,h             P05,Elm
P04,S,d   P09,N,i             P06,Not Found
P05,N,e   P09,N,j             P07,"  "
                              P08,Elm
                              P09,Elm
                              ,Orphan
```

## What already exists

- **pandas `merge`** does the cardinality part well. `validate="m:1"` raises `MergeError` when keys
  repeat in the wrong table, and `indicator=True` marks the rows that found no partner. Use it
  if your pipeline is already in pandas. It has no idea what a sentinel is, and it does not
  look at one class of rows. On the example above, `validate="1:m"` raises `MergeError` for the
  repeated `P09`. With `validate="m:1"` the merge succeeds, and the NULL and `Not Found` rows
  pass without a word.
- **Great Expectations** is a general data-validation framework. It suits a team that keeps a
  suite of expectations over many tables, which this one-file tool does not try to be. It was
  not run for this README, so nothing here says whether it can express these checks.
- **ArcGIS Pro Add Join** keeps every input row by default (`KEEP_ALL`) and gives unmatched
  rows NULL in the joined fields. When the join table has no Object ID field, Esri says only a
  one-to-first join is possible, so repeated lookup keys are hidden there instead of fanning
  out. It reports neither case. See the references.

A search of GitHub for the name `joinproof` on 2026-10-23 found this repository and one other,
`dvygolov/TG.JoinProof`. That one concerns Telegram and is unrelated. A second search, for join
checks with duplicate keys and sentinels, returned nothing. That shows only that the searches
found nothing on the day they ran.

## Requirements

Python 3.9 or newer and nothing else. No `arcpy`, no third-party package, no network, no database.
Export both tables to CSV first, with any tool you like.

```
git clone https://github.com/uhsear/joinproof.git
```

## Quick start

```
python joinproof.py --self-test
```

The self-test is synthetic and runs offline. This is a real run on Windows, Python 3.13.2:

```
joinproof self-test: temporary files only, no network, no credentials
--------------------------------------------------------------------
PASS  a matched real value is ok
PASS  a matched blank is blank-value
PASS  Not Found is a sentinel, not a value
...
PASS  a class row holding N/A counts as absent  <-- pinned defect
...
PASS  a key written two ways joins nothing and exits 4, not 0  <-- pinned defect
...
PASS  a prefix --ap of --apply is REFUSED by the parser  <-- pinned defect
...
--------------------------------------------------------------------
150 assertions, 0 failed
```

The self-test prints 150 assertions on Windows (Python 3.13.2 and 3.12), on Ubuntu (Python
3.12.3) and on Linux Python 3.9.25. The Windows 3.13 and Ubuntu 3.12 outputs match line for line
(compared with `diff`, after the CRLF line endings are removed). `coverage run --branch
joinproof.py --self-test` reports 100 percent of lines and branches (576 statements, 110
branches). The code passes `pyflakes` and compiles under `python -W error`.

## Usage

Check first. Nothing is written without `--apply`.

```
python joinproof.py --left parcels.csv --right lookup.csv --key PARCEL --value SUBDIV
python joinproof.py --left parcels.csv --right lookup.csv --key PARCEL --value SUBDIV \
    --class-col FLAG --class-values Y,S
python joinproof.py --left parcels.csv --right lookup.csv --key PARCEL --value SUBDIV \
    --class-col FLAG --class-values Y,S --out flagged.csv --apply
```

| Flag | Default | What it does |
|---|---|---|
| `--left`, `--right` | none | CSV of the left table, and CSV of the lookup. Required. |
| `--key` | none | Join column in the left table. Required. |
| `--right-key` | same as `--key` | Join column in the lookup, if it has another name. |
| `--value` | none | Lookup column that the join brings across. Required. |
| `--class-col`, `--class-values` | off | Left column and comma separated values that mark the class that must not be lost. Give both or neither. |
| `--sentinel` | none | Extra placeholder to treat as absent. Repeat the flag for more. |
| `--max-sentinel` | `0.05` | Largest sentinel share of the values that look present. |
| `--max-class-absent` | `0.0` | Largest share of class rows that may come back absent. |
| `--max-absent` | `0.5` | Largest share of all left rows that may have no usable joined value (no match, blank key, blank, sentinel or ambiguous). |
| `--allow-repeats` | `0` | Extra left rows tolerated for repeated keys. |
| `--out` | none | New CSV for the flagged left rows. Written only with `--apply`. |
| `--apply` | off | Write the `--out` file. Needs `--out`. |
| `--self-test` | off | Run the offline assertions and exit. |

Exit codes: 0 pass, 1 refused, 2 usage error from the parser (including a flag prefix), 3 the
`--out` write failed, 4 nothing joined, 64 bad arguments or unreadable input.

## What it checks

The tool joins the two tables in memory. Each left row gets one status.

| Status | Meaning |
|---|---|
| `ok` | The key matched, and the value is real. |
| `no-match` | The key is not in the lookup. A left join returns NULL here. |
| `blank-key` | The left key is empty. A NULL key never matches, not even another NULL, so it never counts as a repeated lookup key either. |
| `blank-value` | The key matched, and the value is empty. |
| `sentinel` | The key matched, and the value is a placeholder. |
| `ambiguous` | The lookup holds more than one distinct value for the key. |

The default placeholders are `Not Found`, `N/A`, `#N/A`, `NA`, `None`, `Null`, `<Null>`, `NaN`,
`Unknown`, `TBD`, `-`, `-9999` and `-999`. The tool trims the value and ignores case. A whole
number written with a decimal point (`-9999.0`, `-999.00`) counts as the same placeholder as
the number without it, because pandas and Excel write float columns that way. `0` is not on the
list because it is a real value in many numeric columns. Add it with `--sentinel 0` when it is
not. The list cannot be switched off: see Limits.

The run is refused when any of these is true. The tool also reports each lookup-side repeat.

1. **Repeated left keys.** The left table has more extra rows than `--allow-repeats`. A blank
   key is not a repeat. It is a `blank-key` row.
2. **Ambiguous lookup keys.** One lookup key carries two values. A join then fans out, or keeps the
   first value without saying which.
3. **A lost class.** More than `--max-class-absent` of the class rows came back as anything but `ok`.
   A class row holding a sentinel counts as lost. A `--class-values` that matches no row is also
   refused, because 0 of 0 would otherwise pass.
4. **Too many sentinels.** The sentinels exceed `--max-sentinel` of the values that look present.
5. **Repeated lookup keys.** A left row hits a lookup key that is listed on more than one lookup
   row, even when every row holds the same value. A SQL or pandas left join then returns extra
   rows, and so does an Add Join when the lookup has an Object ID field. The report counts the
   left rows and the extra rows. A blank key is never counted here.
6. **Too many absent rows.** More than `--max-absent` of all left rows have no usable joined
   value. With a default of one half, a join where 99 of 100 keys found no partner is refused
   with or without a class. Lower the limit for a tighter net.

The run exits 4 and says so when no left row matched any lookup key. A key written two ways
(`H-001` and `H-1`, or `12` and `12.0`) causes it. A half-broken key format, such as `12`
against `12.0` on most rows, is not exit 4. Check 6 catches it when enough rows are lost. A
scheduled job must not read a clean exit as "safe to publish".

## What it writes

`--out` receives the flagged left rows, with a `joinproof_reason` column such as `no-match;class`
or `repeated-key`. `--apply` creates the file with exclusive mode, so it never overwrites. An
existing file, including your input, makes the run exit 3 and leaves the file as it was. Nothing
else writes.

## Limits

- It reads CSV. It does not read a feature class or a SQL view. Export the tables first.
- Input must be UTF-8 (a BOM is accepted). A cp1252 file (an Excel CSV with an accented name) or
  a UTF-16 file exits 64 with a message that names the file. Re-save the export as UTF-8 first.
- Keys are compared as trimmed text, case sensitive. `001` and `1` are different keys, and so
  are `12` and `12.0`. Such a mismatch stops the run with exit 4 only when no row at all matches.
  Otherwise the `--max-absent` check catches it when enough rows are lost.
- Values are compared as text. Only a whole number written with a decimal point is read as its
  plain spelling, so `-9999.5` is a real value and `007` is not `7`.
- Keys are case sensitive, but class values and sentinels are not. Unicode normalisation (NFC
  against NFD) and zero-width characters are not handled, and a class value that contains a
  comma cannot be given.
- The join is single column. A composite key must be concatenated first.
- The tool checks one `--value` column per run.
- The sentinel list is a list of common placeholders. It cannot know yours. Add them with
  `--sentinel`. Sentinels are checked only in the `--value` column, not in the key or the class
  column. A placeholder with an inner double space, such as `Not  Found`, is not recognised and
  passes as a real value (verified, exit 0).
- The default placeholders cannot be removed. `NA`, `None` and `Unknown` can be real codes, and
  a lookup that uses them as real data is over-counted: a column of `NA` values exits 1
  (verified).
- Placeholders at or below `--max-sentinel` (5 percent by default) are reported and pass with
  exit 0 when there is no `--class-col`.
- A `--sentinel` value that begins with `-` is read by argparse as a flag and exits 2. Write it
  as `--sentinel=-x`. A `--class-values` item that begins with a hyphen is accepted only when it
  looks like a negative number, such as `-1`. Another hyphen-led value, such as `-x`, exits 2
  and must be written `--class-values=-x`.
- Blank lines are skipped, except in a one-column file. There an empty line is a blank cell, so
  it counts as a `blank-key` row (a trailing empty line counts too). Blank keys count toward
  `--max-absent`, so a one-column left file that is mostly blank lines is refused (verified: 6 of
  8, exit 1).
- A whitespace-only line is not blank. It has one cell, so a file with a trailing line of spaces
  exits 64 (`data row N has 1 cell(s)`). A file that begins with an empty line exits 64 with
  `data row 1 has 2 cell(s), the header has 0`. A ragged row, or trailing commas from some
  exporters, stops the run with exit 64 for the whole file. Fix the export first.
- Only comma-delimited CSV is read. A semicolon or tab file is read as one column and exits 64
  with `no column` (verified). A field longer than 131072 characters exits 64 (the Python `csv`
  field limit). A quote followed by text inside a cell, such as `"Oak"Grove`, exits 64 (strict
  quoting).
- A column that is blank on up to half of the matched rows, with no `--class-col`, passes. Blank
  values are not sentinels and count only toward `--max-absent` (verified: 2 blank of 5 rows
  exits 0).
- A NUL byte in a CSV raises `csv.Error` (exit 64) on Python 3.9 and 3.10 and is read as data on
  3.11 and later. I saw it read as data on 3.13. Python 3.10 and 3.11 were not run.
- A lookup key that has two values is counted as ambiguous, and one that is listed twice with the
  same value is counted as a repeat. Both are counted only for lookup keys that some left row uses.
- Everything is held in memory. The run time and memory on a very large table were not measured.
- With stdout redirected on Windows (cp1252), a non-ASCII column name that gets printed (for
  example `--class-col` with CJK text) raises `UnicodeEncodeError` after the first report lines.
  The traceback ends in exit 1, the same code as a refusal, so a scheduler cannot tell a crash
  from a finding. Set `PYTHONUTF8=1` or use ASCII headers (verified). An `--out` path that cp1252
  cannot encode does the same in the `Wrote` or `Check only` line, and with `--apply` the file
  is already written when it happens (verified with a CJK file name).
- `--out` copies the flagged left rows unchanged. A cell that starts with `=`, `+`, `-` or `@`
  can run as a formula when a spreadsheet opens the file. The file also holds your left data
  (for example owner names) in a new, unprotected CSV. Handle it as sensitive.
- The write is exclusive. Case variants, absolute or relative spellings, a trailing dot and a
  `::$DATA` suffix of an existing file all exit 3 (verified). A Windows device name is not
  rejected: `--out NUL --apply` prints `Wrote 1 flagged row(s) to NUL.` and stores nothing
  (verified). Hard links to a new name were not tested.
- If a disk error happens part way through an `--apply` write, a partial `--out` file is left
  behind and the exit is 3. Exit 3 also hides the check verdict from the exit code, although
  the verdict is still printed.
- The default `--max-absent` of 0.5 is a coarse net. A join that lost a third of its rows
  passes it. Use `--class-col` or a lower limit when a smaller loss matters.
- It reads the exports you give it. Whether those exports match the live tables is up to you.
- "Nothing is written without `--apply`" covers the checked files. `--self-test` itself creates
  a temporary directory with synthetic files, then deletes it. The banner says so.

Every line of the module runs in the self-test, so no line is left uncovered. The tool uses no
network, so there is no network path to stub.

## What was exercised

| Claim | Exercised |
|---|---|
| Nothing is written without `--apply` | Self-test, and a real run that left no file |
| `--apply` never overwrites, including an input | Self-test, and a real run that exited 3 |
| A prefix of `--apply` is refused | Self-test for `--ap`, `--app`, `--appl`; real run of `--ap` exited 2 |
| Exit codes 0, 1, 3, 4 and 64 | Self-test through `main()`, on temporary files |
| Exit code 2 | Self-test (`_refusal_code`) and the real `--ap` run |
| A lost class, repeats, sentinels, ambiguous keys | Self-test, and the example above |
| 99 of 100 rows lost, with and without a class, is refused | Self-test, and a real run on 100 synthetic rows that exited 1 |
| A lookup key listed twice is refused | Self-test, and a real run on a two-line lookup that exited 1 |
| Cp1252, UTF-16, `#N/A`, `-9999.0`, `12.0`, `--sentinel -x`, CJK piped, formula cell, `--out` spellings | Real runs, scratch directory, Windows, Python 3.13 |
| The in-memory join matches pandas | Scratch run, pandas 3.0.5, `how="left"`: 10 rows, 2 left-only, 1 blank, 1 `Not Found` |
| A blank key never fans out, and a one-column file keeps its blank keys | Self-test, and real runs on small files (exit 0 and exit 1) |
| `#N/A`, `-9999.0` and `-999.00` are placeholders | Self-test, and a real run that exited 1 |
| Python 3.9, 3.12 and 3.13 | Self-test run on each, 150 assertions |

The pandas comparison was a throwaway script. It is not part of this repository.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## References

- Microsoft, [Joins (SQL Server)](https://learn.microsoft.com/en-us/sql/relational-databases/performance/joins),
  section "Null values and joins" (page read 2026-10-24). It says that "the null values don't
  match each other", and that after a left outer join "the results don't make it easy to
  distinguish a `NULL` in the data from a `NULL` that represents a failure to join".
- Esri, [Add Join](https://doc.esri.com/en/arcgis-pro/latest/tool-reference/data-management/add-join.html)
  (page read 2026-10-24). `KEEP_ALL` is the default. A record with no match "is given null
  values for all the fields being appended". The page says that
  "Records from the join table can be matched to more than one record if the join table has an
  Object ID field", and that with no Object ID field "only a one-to-first join is possible".
- pandas, [`DataFrame.merge`](https://pandas.pydata.org/docs/reference/api/pandas.DataFrame.merge.html).
  `validate` checks key uniqueness. `indicator` marks `left_only` rows.
- Python, [`argparse`](https://docs.python.org/3.12/library/argparse.html). `allow_abbrev=False`
  turns off prefix matching of long options.

## Related

Other single-file tools in this portfolio that pair with this one:

- [safe-republish](https://github.com/uhsear/safe-republish) - refuses to truncate a layer when the staged replacement fails a plausibility check
- [idreuse](https://github.com/uhsear/idreuse) - reconciles two layers on a shared ID and refuses matches that the geometry disproves
- [arcpy-nullscan](https://github.com/uhsear/arcpy-nullscan) - scans a geodatabase for fields that are quietly NULL
