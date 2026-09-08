# docs_tool.py

One self-contained Python script that checks an Antora docs repo's `en/` and `ru/`
trees for consistency, and aligns a RU page's structure after an EN edit. Run it
from the repo root; every module under `en/modules/` and `ru/modules/` is
discovered and scanned automatically. Run from anywhere else and `check`/`sync`
refuse to start rather than report a clean pass over files they never read.

## Get it

```bash
curl -O https://raw.githubusercontent.com/andreyaksenov/docs-tool/main/docs_tool.py
chmod +x docs_tool.py
```

Needs Python 3.7+ (no dependencies). `git` is only used by `sync`. On Windows, drop
the `chmod` and run `python docs_tool.py …`.

## Usage

```
./docs_tool.py check <family> [<family> ...] [--<rule> ...]
                     [--target NAME] [--page NAME ...]
                     [--glossary PATH ...] [--external-root NAME=PATH ...]
                     [--offline] [--timeout N] [--show-unverified]        # check links
                     [--allow-domain HOST ...] [--insecure] [--link-cache PATH]

./docs_tool.py show <rule|rule-id>            # one rule's rationale + examples
./docs_tool.py show all                       # every rule, with examples
./docs_tool.py list                           # the family tree, one line per rule
./docs_tool.py list rules | list targets      # flat rule list · --target values
./docs_tool.py sync <en-file> [--dry-run]     # align a RU page to EN (beta)
```

Run with no arguments to print the full command list. A run exits `0` if everything
passed, `1` if any rule found something, `2` on a usage or setup error (bad flag,
missing glossary, wrong directory — nothing was checked). `list` labels each
family `suggest: block` / `suggest: warn` — that's advice for your pre-commit hook,
not something the tool enforces; the exit code is the same for every family.

Rules are grouped into seven **families**:

| Family   | Covers                                                                                   |
|----------|------------------------------------------------------------------------------------------|
| `chars`  | invisible chars, unicode dashes, RU/Latin homoglyphs, Cyrillic in EN files               |
| `markup` | stray backticks, unbalanced block delimiters                                             |
| `refs`   | broken `xref:`/`include:`/`image:` targets, orphaned pages/partials/examples/images/tags |
| `style`  | `ё`/`Ё`, un-italicized file paths, table-cell periods                                    |
| `terms`  | EN term translated to a non-house-style RU word (glossary-driven)                        |
| `l10n`   | line-count / structure / nav parity, link & literal parity, untranslated lines, `examples/` parity |
| `links`  | external `http(s)` links — `404`, permanent redirect, dead host (network; opt-in)        |

```bash
./docs_tool.py check style                   # the whole style family
./docs_tool.py check style --no-yo           # narrow to one rule
./docs_tool.py check chars markup            # several families
./docs_tool.py check chars markup refs style terms l10n   # all the offline families
./docs_tool.py check links                   # external links, on their own (network)
./docs_tool.py check l10n --structure --page resource_groups.adoc
```

There is no `check all` — name the families you want. `links` reaches the network
and only runs when you ask for it by name.

`--target NAME` picks a scan target other than the default `pages` (`pages/` +
`partials/`) — see `list targets`.

## Output

A clean rule prints one line; a rule with findings lists them and totals up:

```
$ ./docs_tool.py check style --no-yo
OK: no ё/Ё characters found in ru/ pages.

$ ./docs_tool.py check chars --dashes
FILE     en/modules/ROOT/pages/table_partitioning.adoc
  en/modules/ROOT/pages/table_partitioning.adoc:821:97: … for dates March 1–15 and …

Total: 1 line(s) with en/em dash characters.
```

Every finding starts with an uppercase label and a path, so runs are easy to `grep`
by kind:

| Label | Means |
|-------|-------|
| `FILE` | header for the `path:line:col:` findings indented under it |
| `BROKEN` | a reference (or, in `check links`, an external URL) that doesn't resolve |
| `ORPHANED` | a file nothing points at — no line, the whole file is the finding |
| `MISSING` | an EN or RU counterpart that doesn't exist |
| `DIFF` | an EN/RU pair that diverged — a one-line banner, then each differing line as a clickable `path:line` (`-` EN, `+` RU) |
| `REDIRECT` `UNREACHABLE` `VPN?` | `check links` only — see the `links` family below |

Where a specific line is meaningful it's `path:line` or `path:line:col`, which most
editors and terminals turn into a clickable link. Running more than one rule puts a
header before each, naming the command that re-runs just that rule on its own:

```
$ ./docs_tool.py check chars markup
=== CH01  check chars --no-cyrillic ===
OK: no Cyrillic characters found in en/ pages.

=== CH04  check chars --dashes ===
...
```

Advisory lines (`note:`, `warning:`, `info:`) go to stderr, so `> findings.txt`
keeps them out of the findings themselves.

## Rules

Every rule has a stable **rule ID**. `list` prints this section as a tree;
`show <rule|id>` (e.g. `show no-yo`, `show ST03`) prints one rule's full
rationale, exceptions, and the false positives it was tuned against, ending with
runnable examples. `show all` prints every rule with its examples but without the
rationales — the terminal equivalent of this section. `beta` rules are heuristics
— treat their output as a review list, not a hard gate.

| ID | Command | Flags |
|----|---------|-------|
| `CH01` | `check chars --no-cyrillic` | Cyrillic in EN files |
| `CH03` | `check chars --no-invisible` | zero-width characters |
| `CH04` | `check chars --dashes` | literal en/em dashes |
| `CH05` | `check chars --homoglyphs` | Latin letters in RU prose |
| `MK01` | `check markup --backticks` | odd backtick count |
| `MK02` | `check markup --delimiters` | unclosed block delimiter |
| `RF01` | `check refs --broken` | dead xref / include / image |
| `RF02`–`RF06` | `check refs --orphaned [--target …]` | defined but never referenced |
| `ST01` | `check style --no-yo` | `ё` in RU files |
| `ST02` | `check style --file-path-italics` | file path not in italics |
| `ST03` | `check style --table-cell-periods` | table cell: unwanted trailing period, or one missing before a NOTE |
| `TM01` | `check terms` | off-glossary RU translation |
| `LN01` | `check l10n --lines` | EN/RU line counts differ |
| `LN02` | `check l10n --structure` | EN/RU skeletons differ |
| `LN03` | `check l10n --untranslated` | RU line still English |
| `LN04` | `check l10n --examples` | EN/RU examples differ |
| `LN05` | `check l10n --nav` | EN/RU nav differs |
| `LN06` | `check l10n --links` | EN/RU reference different xref / image / URL targets |
| `LN07` | `check l10n --literals` | EN/RU carry different back-ticked literals |
| `LK01` | `check links` | dead / redirected / unreachable external links (404 fails; the rest are flagged) |

Name the families you want to run — there's no "run everything" keyword. `links`
reaches the network and only runs when named.

### `chars` — Unicode / encoding

- **`CH01` · `check chars --no-cyrillic`** — no Cyrillic in `en/` files (RU text
  left in an EN file). `--target examples` also scans `examples/` → `CH02`.
  ```bash
  ./docs_tool.py check chars --no-cyrillic
  ./docs_tool.py check chars --no-cyrillic --page resource_groups.adoc
  ./docs_tool.py check chars --no-cyrillic --target examples
  ```

- **`CH03` · `check chars --no-invisible`** — no zero-width / invisible /
  bidi-control Unicode characters. Each hit line is printed with the character marked.
  ```bash
  ./docs_tool.py check chars --no-invisible
  ./docs_tool.py check chars --no-invisible --page auth.adoc
  ```

- **`CH04` · `check chars --dashes`** — no literal en dash (`–`) or em dash (`—`);
  house style uses `--`.
  ```bash
  ./docs_tool.py check chars --dashes
  ./docs_tool.py check chars --dashes --page resource_groups.adoc
  ```

- **`CH05` · `check chars --homoglyphs`** · beta — Latin letters in `ru/` prose
  that should be Cyrillic: a mixed-script word, or a lone `а`/`о`/`с`/`у` look-alike.
  ```bash
  ./docs_tool.py check chars --homoglyphs
  ./docs_tool.py check chars --homoglyphs --page resource_groups.adoc
  ```

### `markup` — AsciiDoc syntax

- **`MK01` · `check markup --backticks`** — no line with an odd number of
  backticks (usually a stray or missing `` ` `` around inline monospace).
  ```bash
  ./docs_tool.py check markup --backticks
  ./docs_tool.py check markup --backticks --page resource_groups.adoc
  ```

- **`MK02` · `check markup --delimiters`** — every AsciiDoc block delimiter
  (`----`, `====`, `|===`, `////`, …) closed, checked on the flattened include chain.
  ```bash
  ./docs_tool.py check markup --delimiters
  ./docs_tool.py check markup --delimiters --page resource_groups.adoc
  ```

### `refs` — Antora reference resolution

Always scans the whole site. `--page` only narrows *which files are reported* for
`--orphaned --target tags|partials`; everything else in `refs` ignores it. Bare
`check refs` runs `--broken` plus every orphan target.

- **`RF01` · `check refs --broken`** — every `xref:` / `include::` / `image:` /
  `link:` reference resolves to a real file or anchor. `--external-root NAME=PATH`
  (repeatable) resolves references into a sibling Antora repo checked out locally.
  ```bash
  ./docs_tool.py check refs --broken
  ./docs_tool.py check refs --broken --external-root ADCM=../docs-adcm
  ```
  A reference into a component with no `--external-root` can't be resolved either
  way, so it's left unchecked rather than called broken. The run ends by naming
  those components on stderr — an unverified component otherwise looks exactly
  like a verified one:
  ```
  note: 2 referenced component(s) left unchecked -- docs-backup, docs-pxf
        pass --external-root NAME=PATH for each one you have checked out locally
  ```

- **`RF02`–`RF06` · `check refs --orphaned [--target …]`** — flags content that is
  defined but never referenced. `check refs --orphaned` runs all five; `--target`
  picks one:

  | `--target` | ID | Flags a … |
  |------------|----|-----------|
  | `pages`    | `RF02` | `pages/*.adoc` not reachable from any `nav.adoc` (`start_page` exempt) |
  | `partials` | `RF03` | tag-less `partials/` file never `include::`d whole |
  | `examples` | `RF04` | `examples/` file never pulled in via `include::example$…[]` |
  | `images`   | `RF05` | `images/` file that is no `image:` / `injectSvg:` macro's target |
  | `tags`     | `RF06` | `tag::NAME[]` region never pulled in via `include::…[tag=NAME]` |

  ```bash
  ./docs_tool.py check refs --orphaned
  ./docs_tool.py check refs --orphaned --target tags
  ./docs_tool.py check refs --orphaned --target partials \
    --external-root ADB=../docs-adb --external-root ADH=../docs-adh
  ```

### `style` — Arenadata style guide

Heuristic family — treat findings as a review list, not a hard gate.

- **`ST01` · `check style --no-yo`** — no `ё`/`Ё` in `ru/` files; house style
  spells it `е`. The `:page-author:` attribute is exempt.
  ```bash
  ./docs_tool.py check style --no-yo
  ./docs_tool.py check style --no-yo --page resource_groups.adoc
  ```

- **`ST02` · `check style --file-path-italics`** · beta — file / directory names
  in plain prose that should be in `_italics_` per house style.
  ```bash
  ./docs_tool.py check style --file-path-italics
  ./docs_tool.py check style --file-path-italics --page resource_groups.adoc
  ```

- **`ST03` · `check style --table-cell-periods`** · beta — a table cell's last
  sentence shouldn't end with a period (lists, admonitions, abbreviations exempt).
  The flip side: when a cell ends with any admonition (`NOTE`/`TIP`/`WARNING`/
  `IMPORTANT`/`CAUTION`, one-liner or `[…]`/`====` block), the prose *before* it
  is mid-cell text and **should** end with `.`/`!`/`?`/`:` — a cell missing that
  is reported as `NO PERIOD before a trailing <TYPE>`.
  ```bash
  ./docs_tool.py check style --table-cell-periods
  ./docs_tool.py check style --table-cell-periods --page resource_groups.adoc
  ```

### `terms` — controlled vocabulary

Needs a glossary: `--glossary PATH` (pipe-delimited `en|ru|ru_pattern|note`), or any
`*-glossary.psv` in the current directory (auto-discovered).

- **`TM01` · `check terms`** · beta — flags an EN glossary term whose aligned RU
  line uses a non-house-style translation (or leaves some repeats untranslated).
  The EN/RU line pair is printed under each finding.
  ```bash
  ./docs_tool.py check terms
  ./docs_tool.py check terms --glossary greengagedb-glossary.psv
  ./docs_tool.py check terms --page resource_groups.adoc
  ```

### `l10n` — EN↔RU parity

- **`LN01` · `check l10n --lines`** — every EN `.adoc` has a RU counterpart with
  the same line count, and vice versa.
  ```bash
  ./docs_tool.py check l10n --lines
  ./docs_tool.py check l10n --lines --page resource_groups.adoc
  ```

- **`LN02` · `check l10n --structure`** · beta — EN/RU structural skeletons
  (headings, blocks, `include::`) must match, catching drift when line counts don't.
  Prints the diff, capped at 30 lines per file (`--page <file>` for the rest).
  ```bash
  ./docs_tool.py check l10n --structure
  ./docs_tool.py check l10n --structure --page resource_groups.adoc
  ```

- **`LN03` · `check l10n --untranslated`** · beta — RU lines byte-identical to
  their EN counterpart (`UNTRANSLATED`), plus RU lines carrying English stopwords
  like `the`/`and`/`with` (`SUSPECT`). Each `SUSPECT` line ends with the matched stopword.
  ```bash
  ./docs_tool.py check l10n --untranslated
  ./docs_tool.py check l10n --untranslated --page resource_groups.adoc
  ```

- **`LN04` · `check l10n --examples`** — EN and RU `examples/` must hold the same
  files (byte-for-byte; `.sql` comments may differ). Whole-site — ignores `--page`.
  ```bash
  ./docs_tool.py check l10n --examples
  ```

- **`LN05` · `check l10n --nav`** — EN and RU `nav.adoc` structure (list depth,
  `xref:`/`include::` targets) must match; translated labels ignored. Ignores `--page`.
  ```bash
  ./docs_tool.py check l10n --nav
  ```

- **`LN06` · `check l10n --links`** · beta — EN and RU must reference the same
  targets: xref target *files*, inline `image:` targets, and external URLs. Link
  text is translated and ignored — only the destination is compared. Catches a
  cross-reference or link silently dropped in translation. Folded (deliberate
  localisation, not reported): an xref `#fragment` (Antora derives it from the
  translated heading), a `/en/`|`/ru/` URL path segment or `en.`/`ru.` host, a
  Wikipedia article, an `_en`|`_ru` image-filename tag. Beta: a repo that
  deliberately links its EN docs site from RU pages, or writes an xref sometimes
  module-qualified and sometimes not, shows up here. Every finding carries a
  clickable `path:line` (first hit on each side on the row, further
  occurrences one sub-line each).
  ```bash
  ./docs_tool.py check l10n --links
  ./docs_tool.py check l10n --links --page resource_groups.adoc
  ```

- **`LN07` · `check l10n --literals`** · beta — EN and RU must carry the same set
  of inline monospace spans (`` `...` ``): identifiers, SQL keywords, parameter and
  function names, verbatim error strings. `foo()` and `foo` count as one; a
  `++...++` wrapper is unwrapped; pure-punctuation spans and `NULL`/`true`/`false`
  are ignored. Reports `CHANGED` (a near-identical pair — likely a typo), then
  EN-only, then RU-only. Presence is compared, not count. Beta: RU prose that
  back-ticks a term EN left bare shows up here and usually isn't a bug — treat the
  output as a review list. Every finding carries a clickable `path:line` (first hit
  on each side on the row, further occurrences one sub-line each).
  ```bash
  ./docs_tool.py check l10n --literals
  ./docs_tool.py check l10n --literals --page resource_groups.adoc
  ```

### `links` — external URL health

- **`LK01` · `check links`** · beta — fetches every `http(s)` link in `pages/` and
  `partials/` (both languages). De-duplicates site-wide (one request per distinct
  URL, a few concurrent per host). Honours `--page`.

  This is the only check that touches the network — slow, non-deterministic, and
  connectivity-dependent — so it only runs when you name it (`check links`), on its
  own schedule (a nightly job, not a pre-commit hook). The legacy `--all-checks`
  sweep skips it too. A big doc set can carry many hundreds of external links and a
  full run then takes minutes; a `checking k/N` line on stderr tracks progress.
  **Every run is fresh** — pass `--link-cache PATH` to cache results there (7-day
  TTL) if you want reruns to be quick.

  **Printed line by line:**

  | Label | Means | Fails the run? |
  |-------|-------|:--:|
  | `BROKEN` | `404` / `410`, or a host that doesn't resolve | **yes** |
  | `REDIRECT` | permanent `301`/`308` to a genuinely different URL | no |
  | `UNREACHABLE` | timeout / connection refused / DNS failure | no |
  | `VPN?` | unreachable on a host known to region-lock | no |

  `UNREACHABLE`/`VPN?` don't fail the run — from any one machine a blocked route
  looks the same as a dead link, so re-run behind a VPN to tell them apart.
  Findings are grouped `BROKEN` → `REDIRECT` → `UNREACHABLE`, worst first.

  **Collapsed to a one-line count** (the server answered, just not usefully): `401`/
  `403` anti-bot walls, `429`s, `5xx`. `--show-unverified` lists
  those too. A `301`/`308` that only rewrites the URL cosmetically — `http`→`https`,
  ± `www.`, ± trailing slash, a dropped `#fragment`, a letter-case change — is
  treated as clean, not a `REDIRECT`.

  A separate one-line note flags any host whose TLS certificate the local trust
  store couldn't validate (a missing CA bundle, not a bad site) — `--insecure`
  makes unverified TLS the default and drops the note.

  Only what AsciiDoc renders as a live link is checked. Skipped: comment lines and
  `----`/`....` blocks; a bare URL that's backslash-escaped (`\http://x`) or wrapped
  in a formatting pair (`_http://x_`, `` `http://x` ``); RFC 2606 example hosts;
  `localhost`; private / link-local IPs; doc placeholders (`http://FQDN:PORT`,
  `*.internal`, `10.x`, a bare `HOST`); `*.git` clone URLs; unresolved
  `{attributes}`; and any host passed to `--allow-domain`.

  ```bash
  ./docs_tool.py check links
  ./docs_tool.py check links --offline                       # just list the links, don't fetch
  ./docs_tool.py check links --show-unverified                # list the 403/429/5xx links too
  ./docs_tool.py check links --page install.adoc
  ./docs_tool.py check links --timeout 5                     # per-request, default 10s
  ./docs_tool.py check links --allow-domain intranet.example # skip a host you know is fine
  ./docs_tool.py check links --insecure                      # skip TLS verification + its note
  ./docs_tool.py check links --link-cache .link-cache.json   # cache results (off by default)
  ```

## Scoping with `--page`

By default, every per-file rule scans the whole site. `--page NAME` (repeatable)
limits `chars`, `markup`, `style`, `terms`, `l10n`, and `links` to matching files:

```bash
./docs_tool.py check l10n --untranslated --page resource_groups.adoc   # one file (must end .adoc)
./docs_tool.py check l10n --untranslated --page reference/sql_commands # a directory, recursively
./docs_tool.py check chars markup --page UNCOMMITTED                   # whatever git says is uncommitted
```

If a bare filename matches two files, qualify it (`--page gp_toolkit/gp_ao.adoc`) or
pass the full path. `--page UNCOMMITTED` with nothing uncommitted exits `0`
immediately — which is what the pre-commit hook relies on.

A `--page` value that matches no file aborts the run with exit `2` — an empty run
otherwise looks identical to a clean one, so a typo in a CI invocation would pass
green. `--page UNCOMMITTED` resolving to nothing is exempt: that's the normal
"no `.adoc` changes" case, and still exits `0`.

## Sync

`sync` aligns a RU page's structure to its EN counterpart. Heuristic aligner, not a
semantic merge — review the diff.

```bash
./docs_tool.py sync analyzedb.adoc            # full path or bare filename, like --page
./docs_tool.py sync analyzedb.adoc --dry-run  # print the diff, don't write
```

Only ever writes the RU file. It aligns structure (headings, anchors, blocks, code
lines), copies in new/changed EN lines untranslated (run `check l10n --untranslated`
after to find them), and fixes drifted technical tokens (flag names, ids, paths).
Existing RU prose is never rewritten. When an EN paragraph is **reworded** (not just
extended), the new text is appended after the old translation with a
`// STALE VERSION:` marker — reconcile those by hand.

## Pre-commit hook

Two `check` calls scoped to the commit — the deterministic families block, the rest
just report. Put this in `.git/hooks/pre-commit` (and `chmod +x` it):

```bash
#!/usr/bin/env bash
cd "$(git rev-parse --show-toplevel)"

python3 docs_tool.py check chars markup --page UNCOMMITTED \
  || { echo "pre-commit: blocking check(s) failed" >&2; exit 1; }

python3 docs_tool.py check style terms l10n --page UNCOMMITTED || true
```

Move a family from the second line to the first once it runs clean in practice.

**Don't put `refs` in the hook.** It ignores `--page` and always scans the whole
site (see above), so every commit touching one `.adoc` would print every orphan and
broken reference in the repo. Run `check refs` in CI, or by hand before a release.

## Legacy `--check-*` flags

The pre-subcommand interface still works: `--check-<name>`, `--all-checks`,
`--sync`, `--list-checks`, `--list-modules`. Every check is reachable both ways —
upgrading a vendored copy doesn't break an existing hook or CI job. See the
[migration map](docs/proposals/cli-redesign.md#4-full-migration-map) for the
`check <family>` equivalent of each `--check-*` flag, or run `./docs_tool.py --list-checks`.
`--all-checks` runs everything **except `--check-links-external`** (network) — that
one only runs when named.

Dropped along the way: `sync --since REF`, the `-v` / `-n` short aliases, and
`--verbose` — every check now prints one complete output (a long per-file diff is
capped at 30 lines; use `--page <file>` to see the rest). Two silent no-ops also
became errors — running outside a docs tree, and a `--page` that matches no file —
since both previously reported a clean pass over nothing.

If you need the pre-redesign script itself, it's frozen on the
[`legacy-flags`](https://github.com/andreyaksenov/docs-tool/tree/legacy-flags) branch:

```bash
curl -O https://raw.githubusercontent.com/andreyaksenov/docs-tool/legacy-flags/docs_tool.py
```

That branch is a snapshot for rollback, not a maintained release line — fixes land
on `main`.

## Tab completion (optional)

<details>
<summary>argcomplete setup</summary>

```bash
pip install --user argcomplete        # or: sudo apt install python3-argcomplete
```

Add to `~/.zshrc` / `~/.bashrc` and open a new shell:

```bash
eval "$(python3 -m argcomplete.scripts.register_python_argcomplete docs_tool.py)"
```

The module form is used deliberately: `pip install --user` puts the
`register-python-argcomplete` wrapper in a bin directory that often isn't on
`PATH`, and the failure is silent — `eval` of an empty string leaves you with no
completion and no explanation.

Then `./docs_tool.py <TAB>` completes subcommands and families, and completion is
family-aware: `check l10n <TAB>` offers `--lines`, `--structure`, … but not
`--no-yo` (another family's rule), `--glossary` (`terms` only) or the `check links`
flags — and drops a family already on the line. `show <TAB>` completes rule names
and IDs; `--page` and `sync`'s file argument complete real filenames from the
current site.
</details>

## Tests

```bash
python3 -m unittest discover -s tests
```

Stdlib `unittest`, no dependencies; fixture-based, never touches this repo's real content.
