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

The data above is synthetic. It is ten invented rows (`P01` to `P09`, with `P09` twice) and a
lookup with eight rows, one of which has a blank key.

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
- **ArcGIS Pro Add Join** keeps every input row by default and gives unmatched rows NULL in the
  joined fields. With a join table that has no Object ID field, only a one-to-first join is possible. See the references.

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
126 assertions, 0 failed
```

The self-test prints 126 assertions on Windows (Python 3.13.2), on Python 3.9.25 and on Ubuntu
(Python 3.12.3). The Windows and Ubuntu outputs match line for line, apart from the line endings
(CRLF on Windows). `coverage run --branch joinproof.py --self-test` reports 100 percent of lines
and branches (532 statements, 102 branches). The code passes `pyflakes` and compiles under
`python -W error`.

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
| `blank-key` | The left key is empty. A NULL key never matches, not even another NULL. |
| `blank-value` | The key matched, and the value is empty. |
| `sentinel` | The key matched, and the value is a placeholder. |
| `ambiguous` | The lookup holds more than one distinct value for the key. |

The default placeholders are `Not Found`, `N/A`, `NA`, `None`, `Null`, `<Null>`, `NaN`, `Unknown`, `TBD`,
`-`, `-9999` and `-999`. The tool trims the value and ignores case. `0` is not on the list because
it is a real value in many numeric columns. Add it with `--sentinel 0` when it is not.

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
   row, even when every row holds the same value. A SQL, pandas or Add Join left join then
   returns extra rows. The report counts the left rows and the extra rows.
6. **Too many absent rows.** More than `--max-absent` of all left rows have no usable joined
   value. With a default of one half, a join where 99 of 100 keys found no partner is refused
   with or without a class. Lower the limit for a tighter net.

The run exits 4 and says so when no left row matched any lookup key. Note that a half-broken key
format, such as `12` against `12.0` on most rows, is caught by check 6 and not by exit 4. That is a check that
compared nothing. A key written two ways (`H-001` and `H-1`, or `12` and `12.0`) causes it.
A scheduled job must not read that clean exit as "safe to publish".

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
- Values are compared as text, so the `-9999` default does not match `-9999.0`.
- Keys are case sensitive, but class values and sentinels are not. Unicode normalisation (NFC
  against NFD) and zero-width characters are not handled, and a class value that contains a
  comma cannot be given.
- The join is single column. A composite key must be concatenated first.
- The tool checks one `--value` column per run.
- The sentinel list is a list of common placeholders. It cannot know yours. Add them with
  `--sentinel`. It does not include Excel's `#N/A`, nor numeric spellings such as `-9999.0` or
  `-999.00` (what pandas writes for float columns). A column made only of those values passes
  as `ok` (verified). Sentinels are checked only in the `--value` column, not in the key or the
  class column.
- A `--sentinel` value that begins with `-` is read by argparse as a flag and exits 2. Write it
  as `--sentinel=-x`.
- Blank lines are skipped. A one-column left file therefore loses its blank-key rows, and they
  are not counted as blank-key. A blank key never causes a refusal anyway.
- A lookup key that has two values is counted as ambiguous, and one that is listed twice with the
  same value is counted as a repeat. Both are counted only for lookup keys that some left row uses.
- Everything is held in memory. The run time and memory on a very large table were not measured.
- With stdout redirected on Windows (cp1252), a non-ASCII column name that gets printed (for
  example `--class-col` with CJK text) raises `UnicodeEncodeError` after the first report lines.
  The traceback ends in exit 1, the same code as a refusal. Set `PYTHONUTF8=1` or use ASCII
  headers (verified).
- `--out` copies the flagged left rows unchanged. A cell that starts with `=`, `+`, `-` or `@`
  can run as a formula when a spreadsheet opens the file. The file also holds your left data
  (for example owner names) in a new, unprotected CSV. Handle it as sensitive.
- The write is exclusive. Case variants, absolute or relative spellings, a trailing dot and a
  `::$DATA` suffix of an existing file all exit 3 (verified). Windows device names such as `NUL`
  and hard links to a new name were not tested.
- If a disk error happens part way through an `--apply` write, a partial `--out` file is left
  behind and the exit is 3. Exit 3 also hides the check verdict from the exit code, although
  the verdict is still printed.
- The default `--max-absent` of 0.5 is a coarse net. A join that lost a third of its rows
  passes it. Use `--class-col` or a lower limit when a smaller loss matters.
- It reads the exports you give it. Whether those exports match the live tables is up to you.

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
| Python 3.9, 3.12 and 3.13 | Self-test run on each, 126 assertions |

The pandas comparison was a throwaway script. It is not part of this repository.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## References

- Microsoft, [Joins (SQL Server)](https://learn.microsoft.com/en-us/sql/relational-databases/performance/joins),
  section "Null values and joins". NULLs do not match each other. A NULL from a failed join is hard to
  tell from a NULL in the data.
- Esri, [Add Join](https://doc.esri.com/en/arcgis-pro/latest/tool-reference/data-management/add-join.html).
  `KEEP_ALL` is the default. A record with no match "is given null values". The page says that
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
