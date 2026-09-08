#!/usr/bin/env python3
# PYTHON_ARGCOMPLETE_OK
"""
docs_tool.py -- consistency checks and EN->RU sync for Antora documentation
trees laid out as en/modules/<module>/... and ru/modules/<module>/...
(single-module sites with just a ROOT module work the same way).

Run from the repo root (use "python docs_tool.py ..." on Windows). Every
check scans all discovered modules automatically.

Commands:
    ./docs_tool.py check <family> [<family> ...] [--<rule> ...] [--target NAME] [--verbose] [--page NAME ...]
                        check terms adds  [--glossary PATH ...];  check refs, [--external-root NAME=PATH ...]
                        check links adds  [--offline] [--timeout N] [--allow-domain HOST ...]
                                          [--show-unverified] [--insecure] [--link-cache PATH]
    ./docs_tool.py show  <rule|rule-id>|all     -- one rule's rationale, or every rule with examples
    ./docs_tool.py list  [rules|targets]        -- the family/rule map, or a flat list
    ./docs_tool.py sync  <path/to/en/file.adoc> [--dry-run]

Rules are grouped into seven families, ordered by where a rule's authority
comes from: chars (Unicode/encoding), markup (AsciiDoc), refs (Antora
resolution), style (house style), terms (glossary), l10n (EN<->RU parity),
links (external URL health). Name one or more to run them; there is no
"run everything" keyword -- `links` reaches the network and is only ever
run on an explicit `check links`.

%RULES%
"docs_tool.py show <id>" explains one rule and gives runnable examples for it;
"docs_tool.py show all" prints those examples for every rule at once.

Examples:
    ./docs_tool.py check chars
    ./docs_tool.py check style --no-yo
    ./docs_tool.py check l10n --structure --verbose --page resource_groups.adoc
    ./docs_tool.py check chars markup --page UNCOMMITTED
    ./docs_tool.py check links --show-unverified
    ./docs_tool.py sync en/modules/ROOT/pages/reference/utils/analyzedb.adoc --dry-run

The legacy flag interface -- --check-<name>, --all-checks, --sync,
--list-checks, --list-modules -- still works; see "docs_tool.py --list-checks".

Heuristic checks (marked "beta" by "list") and sync can misfire on
legitimate content -- treat their output as a review list, not a hard gate.
"""
import argparse
import difflib
import ipaddress
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    import argcomplete
except ImportError:
    argcomplete = None

EN_MODULES_ROOT = Path("en/modules")
RU_MODULES_ROOT = Path("ru/modules")

CYRILLIC_RE = re.compile(r'[Ѐ-ӿ]')
EN_EM_DASH_RE = re.compile(r'[–—]')

# Zero-width/invisible-by-definition Unicode ranges: ZWSP/ZWNJ/ZWJ/bidi marks,
# soft hyphen, the Mongolian vowel separator, bidi embedding/override/isolate
# controls, word joiner and the invisible math operators, the BOM, and the
# Unicode tag characters (U+E0000-U+E007F) -- a range with no visible glyph
# at all, known to be abused to smuggle hidden ASCII text past a casual read
# of the source (an "ASCII smuggling" trick), not just a typography quirk.
_INVISIBLE_RANGES = (
    (0x00AD, 0x00AD),
    (0x180E, 0x180E),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x2064),
    (0x2066, 0x2069),
    (0xFEFF, 0xFEFF),
    (0xE0000, 0xE007F),
)
_INVISIBLE_RE = re.compile('[' + ''.join(
    re.escape(chr(lo)) if lo == hi else f"{re.escape(chr(lo))}-{re.escape(chr(hi))}"
    for lo, hi in _INVISIBLE_RANGES
) + ']')

# Populated from --external-root NAME=PATH (see main()). Lets
# --check-pages-broken-refs resolve xref:/include:: targets that point at a
# separate Antora component (e.g. `xref:ADCM:ROOT:page.adoc[]`) when the
# user has that component's repo checked out locally -- otherwise such
# targets are silently treated as pointing outside anything this tool can
# see, and left unchecked. {component_name: {"en": {module: root}, "ru": {module: root}}}
EXTERNAL_COMPONENTS = {}

# Component/module names a reference pointed at that couldn't be resolved
# against this repo or any --external-root, filled in by _resolve_module_ref
# and reported by _report_skipped_components. Those references are left
# unchecked, so without this a run that never looked at a whole component
# is indistinguishable from one that verified it.
_SKIPPED_COMPONENTS = set()


# --------------------------------------------------------------------------
# Module discovery
# --------------------------------------------------------------------------

def discover_module_names():
    """Every module directory found under en/modules/ and/or ru/modules/,
    sorted for stable output. A single-module site (just en/modules/ROOT)
    yields ["ROOT"]; a multi-module Antora site yields every module
    (ROOT, concept, how-to, ...) whether or not it has a RU counterpart yet
    (a missing RU module still produces useful MISSING findings)."""
    names = set()
    for base in (EN_MODULES_ROOT, RU_MODULES_ROOT):
        if base.is_dir():
            names.update(p.name for p in base.iterdir() if p.is_dir())
    return sorted(names)


def module_roots():
    """Yield (module_name, en_root, ru_root) for every discovered module."""
    for name in discover_module_names():
        yield name, EN_MODULES_ROOT / name, RU_MODULES_ROOT / name


def _load_external_components(specs):
    """Parse --external-root NAME=PATH values into
    {component_name: {"en": {module: root}, "ru": {module: root}}}, by
    running the same en/modules + ru/modules discovery this tool uses on
    its own repo against each external repo root. A typo'd or otherwise
    wrong PATH doesn't fail the run -- every reference into that component
    just falls back to "unregistered external, skip", identical to not
    passing --external-root at all -- so a warning is printed instead,
    since that silent fallback would otherwise look like a clean pass."""
    components = {}
    for spec in specs or []:
        if "=" not in spec:
            sys.exit(f"error: --external-root must be NAME=PATH, got: {spec!r}")
        name, _, path_str = spec.partition("=")
        repo_root = Path(path_str)
        en_root = repo_root / "en" / "modules"
        ru_root = repo_root / "ru" / "modules"
        module_names = set()
        for base in (en_root, ru_root):
            if base.is_dir():
                module_names.update(p.name for p in base.iterdir() if p.is_dir())
        if not module_names:
            if not repo_root.exists():
                reason = "path does not exist"
            elif not repo_root.is_dir():
                reason = "path is not a directory"
            else:
                reason = "no en/modules or ru/modules found under it"
            print(f"warning: --external-root {name}={path_str}: {reason} -- "
                  f"every reference into {name}: will be silently left unchecked, "
                  f"same as if --external-root had never been passed for it",
                  file=sys.stderr)
        components[name] = {
            "en": {m: en_root / m for m in module_names},
            "ru": {m: ru_root / m for m in module_names},
        }
    return components


# Populated from --glossary PATH (see main()). Backs --check-pages-terminology:
# {lookup_key: {"ru_display": {ru_translation, ...}, "patterns": [pattern, ...]}}
# where each `pattern` is a tuple of compiled regexes (see _compile_glossary_pattern)
# -- an entry is satisfied if the RU line matches every regex in any one
# pattern. Multiple glossary rows sharing the same EN term (e.g. the two "session"
# rows, or an abbreviation given its own row alongside the spelled-out term
# like "WAL" next to "write-ahead logging") contribute additional
# alternative patterns/translations under one merged key.
GLOSSARY = {}

_GLOSSARY_STEM_TOKEN_RE = re.compile(r'^(.+)<>$')


def _compile_glossary_pattern(ru_pattern: str):
    """Compiles one ru_pattern field (format documented in a
    *-glossary.psv file's header) into a tuple of regexes, one per
    whitespace-separated token: a `word<>` token becomes a word-boundary
    stem-prefix match (tolerating any Russian declension/conjugation
    suffix, or none); a bare `word` token becomes a word-boundary exact
    match. All regexes in the tuple must find a hit (in any order,
    anywhere in the line) for the pattern to be satisfied."""
    regexes = []
    for token in ru_pattern.split():
        m = _GLOSSARY_STEM_TOKEN_RE.match(token)
        stem = m.group(1) if m else token
        regexes.append(re.compile(r'\b' + re.escape(stem), re.IGNORECASE))
    return tuple(regexes)


def _discover_default_glossaries():
    """Default for --glossary when it isn't passed: every *-glossary.psv
    file directly under the current directory (the repo root docs_tool.py
    is run from -- same convention EN_MODULES_ROOT/RU_MODULES_ROOT rely on).
    Lets a docs repo that carries its own glossary file run
    --check-pages-terminology without spelling out the path every time.
    Sorted for stable, reproducible output; not recursive, so a glossary
    tucked away in a subdirectory still needs to be passed explicitly."""
    return sorted(str(p) for p in Path(".").glob("*-glossary.psv"))


def _load_glossary(paths):
    """Parses --glossary PATH pipe-delimited file(s) (columns
    en|ru|ru_pattern|note; format documented in greengagedb-glossary.psv's
    own header) into {lookup_key: {"ru_display": {...}, "patterns": [...]}}.
    "|" -- not "," -- is the column separator specifically so that ordinary
    EN/RU prose (which routinely contains commas, but essentially never a
    literal "|") never needs quoting/escaping the way the format's retired
    CSV predecessor did. `en` is used verbatim (lowercased) as the lookup
    key -- there's no disambiguation note embedded in it to strip; that
    context lives in the (match-irrelevant) `note` column instead. Multiple
    files, or multiple rows within one file, sharing the same `en` are
    merged: the key accumulates every contributing row's translation/pattern
    as an alternative, since a sense that can't be told apart automatically
    (e.g. the two "session" rows) must accept either as correct rather than
    guessing which one applies."""
    glossary = {}
    for path_str in paths or []:
        path = Path(path_str)
        text = _read_text(path)
        if text is None:
            sys.exit(f"error: --glossary {path_str}: file not found or unreadable")
        data_lines = [l for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]
        if not data_lines:
            continue
        for lineno, line in enumerate(data_lines[1:], 2):  # [0] is the en|ru|ru_pattern|note header
            fields = line.split("|")
            if len(fields) != 4:
                print(f"warning: --glossary {path_str}:{lineno}: expected 4 '|'-separated fields, "
                      f"got {len(fields)}, skipping: {line!r}", file=sys.stderr)
                continue
            en_part, ru, ru_pattern, _note = (f.strip() for f in fields)
            if not en_part or not ru_pattern:
                print(f"warning: --glossary {path_str}:{lineno}: skipping row with missing en/ru_pattern: {line!r}",
                      file=sys.stderr)
                continue
            key = en_part.lower()
            entry = glossary.setdefault(key, {"ru_display": set(), "patterns": []})
            entry["ru_display"].add(ru)
            entry["patterns"].append(_compile_glossary_pattern(ru_pattern))
    return glossary


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------

def _read_lines(path: Path):
    """Read a text file as a list of lines (no trailing newlines), tolerating
    encoding issues the way the shell tools (grep/perl -CSD) silently did."""
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, UnicodeError):
        return None


def _read_text(path: Path):
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _iter_files(root: Path, suffix: str = None):
    """Yield all files under root (recursively), optionally filtered by
    suffix (e.g. '.adoc'). Sorted for stable, reproducible output. Skips
    dotfiles/dotdirs (e.g. macOS' `.DS_Store`, a stray `.git`) -- unlike a
    shell glob, pathlib's `rglob("*")` matches a leading dot too, and
    that's never real Antora content, just editor/OS noise that would
    otherwise show up as e.g. a bogus ORPHANED image."""
    if not root.is_dir():
        return
    for p in sorted(root.rglob("*")):
        if any(part.startswith(".") for part in p.relative_to(root).parts):
            continue
        if p.is_file() and (suffix is None or p.suffix == suffix):
            yield p


# Optional --page filter, or None when not requested:
# {"names": {file-path-parts tuple, ...}, "dirs": {dir-parts tuple, ...}}
# "names" matches a file whose content-relative path (with its own .adoc
# stripped, see _content_relparts_stem) ENDS WITH the given parts tuple --
# from a NAME ending in .adoc, split on "/" (a bare "resource_groups.adoc"
# is just the 1-element case, matching by stem alone regardless of
# directory, same as before qualified names were supported). "dirs"
# matches every file under that content-relative directory, recursively
# (from a NAME not ending in .adoc -- see main()). Only applied at the
# per-file "report" loops of checks that compare an en/ru file pair
# directly -- corpus-building loops (broken-refs' partial-includer map,
# orphaned pages/examples/images) scan the whole site regardless, since
# narrowing those would just make them wrong rather than faster.
_PAGE_FILTER = None


def _content_relpath(path: Path):
    """`path` relative to the nearest ancestor `pages/` or `partials/`
    directory, e.g. Path("reference/sql_commands/create_role.adoc") for
    .../en/modules/ROOT/pages/reference/sql_commands/create_role.adoc --
    the content-relative path a directory --page filter is matched
    against, so the same subtree is scoped the same way regardless of
    which module or language it's found under. None if `path` isn't under
    either (not expected in practice, since this is only ever called on
    files this tool itself discovered under pages/ or partials/)."""
    parts = path.parts
    for i in range(len(parts) - 1, -1, -1):
        if parts[i] in ("pages", "partials"):
            return Path(*parts[i + 1:])
    return None


def _content_relparts_stem(path: Path):
    """_content_relpath(path), as a tuple of parts with the final segment's
    .adoc extension stripped, e.g. ("reference", "sql_commands",
    "create_role") -- what a --page/--sync file NAME (also .adoc-stripped
    and split on "/") is suffix-matched against, so a qualifying directory
    prefix disambiguates a same-named file in two different directories
    instead of being silently dropped. None if `path` isn't under a
    pages/partials directory (see _content_relpath)."""
    relpath = _content_relpath(path)
    if relpath is None:
        return None
    return relpath.parts[:-1] + (relpath.stem,)


def _ends_with_parts(parts, suffix) -> bool:
    return len(suffix) <= len(parts) and parts[len(parts) - len(suffix):] == suffix


def _page_allowed(path: Path) -> bool:
    if _PAGE_FILTER is None:
        return True
    relparts_stem = _content_relparts_stem(path)
    if relparts_stem is not None and any(_ends_with_parts(relparts_stem, n) for n in _PAGE_FILTER["names"]):
        return True
    if not _PAGE_FILTER["dirs"]:
        return False
    relpath = _content_relpath(path)
    if relpath is None:
        return False
    dir_parts = relpath.parts[:-1]
    return any(dir_parts[:len(d)] == d for d in _PAGE_FILTER["dirs"])


def _git_uncommitted_adoc_stems():
    """Filename stems of every .adoc file with uncommitted changes --
    staged, unstaged, or untracked -- per `git status --porcelain`. Backs
    `--page UNCOMMITTED`, so a pre-commit hook can scope checks to just
    what's about to be committed instead of the whole site."""
    result = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit("error: --page UNCOMMITTED requires running inside a git repository")
    stems = set()
    for line in result.stdout.splitlines():
        path = line[3:]
        if " -> " in path:  # renames: "old -> new"
            path = path.split(" -> ", 1)[1]
        path = path.strip('"')
        p = Path(path)
        if p.suffix == ".adoc":
            stems.add(p.stem)
    return stems


def _format_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


def _labeled_unified_diff(en_lines, ru_lines, en_label, ru_label, n=1):
    """Render a unified diff with each changed line prefixed by its source
    file label, matching the `sed -e "s|^-|- $en_file:  |" ...` treatment
    used throughout the original shell scripts so findings are easy to jump
    to directly from the terminal."""
    diff = difflib.unified_diff(en_lines, ru_lines, lineterm="", n=n)
    out = []
    for line in diff:
        if line.startswith(("---", "+++")):
            continue
        if line.startswith("-"):
            out.append(f"    - {en_label}:  {line[1:]}")
        elif line.startswith("+"):
            out.append(f"    + {ru_label}:  {line[1:]}")
        else:
            out.append(f"          {line[1:] if line.startswith(' ') else line}")
    return out


# --------------------------------------------------------------------------
# EXAMPLES checks
# --------------------------------------------------------------------------

def check_examples_no_cyrillic(verbose=False) -> bool:
    """Port of check_examples_no_cyrillic.sh: no Cyrillic in en/ examples
    (checked across every module)."""
    ok = True
    total_hits = 0
    for _, en_root, _ in module_roots():
        for f in _iter_files(en_root / "examples"):
            lines = _read_lines(f)
            if lines is None:
                continue
            hits = list(_first_match_hits(lines, CYRILLIC_RE))
            if hits:
                ok = False
                total_hits += len(hits)
                print(f"FILE     {f}")
                for i, col, l in hits:
                    print(f"  {f}:{i}:{col}: {l}")
    if ok:
        print("OK: no Cyrillic characters found in en/ examples.")
    else:
        print(f"\nTotal: {total_hits} line(s) with Cyrillic characters.")
    return ok


def check_examples_orphaned(verbose=False) -> bool:
    """Port of check_examples_orphaned.sh: every examples/ file must be
    pulled in by an include::example$<path>[] somewhere in that module's
    pages/partials."""
    ok = True
    orphaned_count = 0
    for _, en_root, ru_root in module_roots():
        for root in (en_root, ru_root):
            examples_root = root / "examples"
            if not examples_root.is_dir():
                continue
            corpus_parts = []
            for d in (root / "pages", root / "partials"):
                for f in _iter_files(d):
                    text = _read_text(f)
                    if text is not None:
                        corpus_parts.append(text)
            corpus = "\n".join(corpus_parts)
            for f in _iter_files(examples_root):
                rel = f.relative_to(examples_root).as_posix()
                needle = f"example$${rel}".replace("$$", "$")
                if needle not in corpus:
                    ok = False
                    orphaned_count += 1
                    print(f"ORPHANED  {f}  (not included in any pages/partials)")
    if ok:
        print("OK: all examples are included somewhere.")
    else:
        print(f"\nTotal: {orphaned_count} orphaned example file(s).")
    return ok


_COMMENT_STRIP_RE = re.compile(r'[ \t]+--([ \t].*)?$')


def _blank_sql_comments(text: str):
    """Port of the awk `blank_comments` helper: blanks out comment-only
    lines (-- line comments and /* ... */ blocks) so translated SQL
    comments don't count as content drift, while still comparing actual
    code lines (including indentation) verbatim."""
    out = []
    in_comment = False
    for line in text.splitlines():
        trimmed = line.strip()
        if in_comment:
            out.append("")
            if "*/" in trimmed:
                in_comment = False
            continue
        if trimmed == "" or trimmed.startswith("--"):
            out.append("")
            continue
        if trimmed.startswith("/*"):
            out.append("")
            if "*/" not in trimmed:
                in_comment = True
            continue
        out.append(_COMMENT_STRIP_RE.sub("", line))
    return out


def check_examples_parity(verbose=False) -> bool:
    """Port of check_examples_parity.sh: en/ru examples must have the same
    files (per module); non-.sql files must match byte-for-byte, .sql files
    only need to match once comment-only lines are blanked out."""
    ok = True
    mismatch_count = 0

    def skip(f: Path, base: Path):
        return "_demo_cluster" in f.relative_to(base).parts

    for _, en_root, ru_root in module_roots():
        en_examples = en_root / "examples"
        ru_examples = ru_root / "examples"

        for en_file in _iter_files(en_examples):
            if skip(en_file, en_examples):
                continue
            rel = en_file.relative_to(en_examples)
            ru_file = ru_examples / rel
            if not ru_file.is_file():
                print(f"MISSING  {en_file}  (no ru counterpart)")
                ok = False
                mismatch_count += 1
                continue

            if en_file.suffix == ".sql":
                en_text = _read_text(en_file) or ""
                ru_text = _read_text(ru_file) or ""
                en_blanked = _blank_sql_comments(en_text)
                ru_blanked = _blank_sql_comments(ru_text)
                if en_blanked != ru_blanked:
                    print(f"DIFF     {en_file}")
                    print(f"         {ru_file}")
                    ok = False
                    mismatch_count += 1
                    if verbose:
                        print("\n".join(_labeled_unified_diff(en_blanked, ru_blanked, en_file, ru_file)))
                        print()
                continue

            en_bytes = en_file.read_bytes()
            ru_bytes = ru_file.read_bytes()
            if en_bytes != ru_bytes:
                print(f"DIFF     {en_file}")
                print(f"         {ru_file}")
                ok = False
                mismatch_count += 1
                if verbose:
                    en_lines = (_read_text(en_file) or "").splitlines()
                    ru_lines = (_read_text(ru_file) or "").splitlines()
                    print("\n".join(_labeled_unified_diff(en_lines, ru_lines, en_file, ru_file)))
                    print()

        for ru_file in _iter_files(ru_examples):
            if skip(ru_file, ru_examples):
                continue
            rel = ru_file.relative_to(ru_examples)
            if not (en_examples / rel).is_file():
                print(f"MISSING  {ru_file}  (no en counterpart)")
                ok = False
                mismatch_count += 1

    if ok:
        print("OK: en/ru examples match.")
    else:
        print(f"\nTotal: {mismatch_count} mismatch(es).")
    return ok


# --------------------------------------------------------------------------
# IMAGES checks
# --------------------------------------------------------------------------

_IMAGE_REF_RE = re.compile(r'(image:{1,2}|injectSvg:{1,2}|inlineSVG:{1,2})([^\]\[\s]+)\[')


def _collect_used_images(lang_module_roots, lang, partial_includers):
    """Scans every pages/partials file (in this language) for
    image:/image::/injectSvg:/injectSvg::/inlineSVG:/inlineSVG:: macros and
    returns the set of concrete images/ file Paths they resolve to -- the
    same resolution _check_refs_in_file's image branch uses: a
    component/module-qualified prefix goes through _resolve_module_ref
    (skipped if it names an unregistered external component, same as
    broken-refs); an unqualified one resolves against whichever module(s)
    actually include the file (partial_includers), or the file's own
    module for a page. This is deliberately resolution-based rather than a
    basename-in-corpus-text check: two different modules can each have
    their own same-named images/foo.png, and only resolving each reference
    to a specific file -- instead of asking whether "foo.png" appears
    anywhere in the site's text -- tells a genuinely-unused duplicate
    apart from the one that's actually used (a substring match also has
    the opposite failure mode: images/services.png went unflagged for
    years because images/adb_add_services.png, an unrelated file in
    another module, happens to end in "services.png"). `own_name` lets a
    self-qualified reference to this repo's own component (e.g.
    `image::ADCM:ROOT:clusters/downloads_en.png[]`, written inside
    docs-adcm's own partials -- a real, existing pattern) still resolve,
    the same way an unqualified one would."""
    used = set()
    own_name = _own_component_name(lang)
    for root in lang_module_roots.values():
        for f in list(_iter_files(root / "pages", ".adoc")) + list(_iter_files(root / "partials", ".adoc")):
            lines = _read_lines(f)
            if lines is None:
                continue
            excluded = _excluded_ref_lines(f)
            fallback_roots = partial_includers.get(f) or {root}
            for lineno, line in enumerate(lines, 1):
                if lineno in excluded:
                    continue
                for macro, t in _IMAGE_REF_RE.findall(line):
                    if t.startswith(("http://", "https://")):
                        continue
                    if macro.startswith("injectSvg") or macro.startswith("inlineSVG"):
                        used.add(root / "images" / _strip_root_slash(t))
                        continue
                    if _VERSION_PIN_RE.match(t):
                        continue  # version@component:... pin -- can't resolve, left unchecked
                    candidate_roots = list(fallback_roots)
                    m_component = _COMPONENT_PREFIX_RE.match(t)
                    if m_component:
                        component = m_component.group(0)[:-1]
                        resolved = _resolve_module_ref(component, t[len(m_component.group(0)):], lang_module_roots, lang, own_name)
                        if resolved is None:
                            continue  # external component image, not registered via --external-root
                        candidate_roots = [resolved[0]]
                        t = resolved[1]
                    t = _strip_root_slash(t)
                    for cand in candidate_roots:
                        used.add(cand / "images" / t)
    return used


def check_images_orphaned(verbose=False) -> bool:
    """Port of check_images_orphaned.sh, made resolution-aware (see
    _collect_used_images) instead of a basename-in-corpus-text match: every
    images/ file must be the actual target of some image:/image::/
    injectSvg:/injectSvg::/inlineSVG:/inlineSVG:: macro somewhere in that
    language across the whole site, not just its own module, since a page
    in one module can reference another module's image via a qualified
    image::<module>:path[] macro, or, for a partial's image, via whichever
    module(s) actually include that partial."""
    ok = True
    orphaned_bytes = 0
    orphaned_count = 0
    modules = list(module_roots())
    en_module_roots = {name: en_root for name, en_root, _ in modules}
    ru_module_roots = {name: ru_root for name, _, ru_root in modules}
    en_module_list = [(name, en_root) for name, en_root, _ in modules]
    ru_module_list = [(name, ru_root) for name, _, ru_root in modules]
    en_includers = _build_partial_includers(en_module_list, en_module_roots, "en")
    ru_includers = _build_partial_includers(ru_module_list, ru_module_roots, "ru")

    for lang, lang_module_roots, includers in (
            ("en", en_module_roots, en_includers),
            ("ru", ru_module_roots, ru_includers),
    ):
        used = _collect_used_images(lang_module_roots, lang, includers)
        for root in lang_module_roots.values():
            images_root = root / "images"
            if not images_root.is_dir():
                continue
            for f in _iter_files(images_root):
                if f not in used:
                    ok = False
                    orphaned_count += 1
                    orphaned_bytes += f.stat().st_size
                    print(f"ORPHANED  {f}  (not referenced in any pages/partials)")
    if ok:
        print("OK: all images are referenced somewhere.")
    else:
        print(f"\nTotal: {orphaned_count} orphaned image(s), {_format_size(orphaned_bytes)}")
    return ok


# --------------------------------------------------------------------------
# NAV checks
# --------------------------------------------------------------------------

_NAV_SVG_RE = re.compile(r'^(\*+)\s\+\+\+<svg><use xlink:href="[^"]*#([^"]+)"')
_NAV_XREF_RE = re.compile(r'^(\*+)\s+xref:([^\[]+)\[')
_NAV_LISTITEM_RE = re.compile(r'^(\*+)\s')
_NAV_INCLUDE_RE = re.compile(r'^include::(.*)$')
_INCLUDE_PARTIAL_RE = re.compile(r'include::partial\$([^\[]+\.adoc)')


def _nav_skeleton(path: Path):
    """Structural skeleton of a nav file: list depth + xref/include target,
    or an <svg:...>/<text> placeholder. Numbered lines (1-based) for
    --verbose lookup; caller strips the number prefix for the plain equality check."""
    lines = _read_lines(path)
    if lines is None:
        return []
    out = []
    for lineno, line in enumerate(lines, 1):
        m = _NAV_SVG_RE.match(line)
        if m:
            out.append((lineno, f"{m.group(1)} <svg:{m.group(2)}>"))
            continue
        m = _NAV_XREF_RE.match(line)
        if m:
            out.append((lineno, f"{m.group(1)} xref:{m.group(2)}"))
            continue
        m = _NAV_LISTITEM_RE.match(line)
        if m:
            out.append((lineno, f"{m.group(1)} <text>"))
            continue
        m = _NAV_INCLUDE_RE.match(line)
        if m:
            out.append((lineno, f"include::{m.group(1)}"))
    return out


# How many skeleton-diff lines to show before truncating; --verbose shows all.
_SKELETON_DIFF_PREVIEW = 20


def _skeleton_diff_lines(en_skel, ru_skel, en_label, ru_label):
    """Like _labeled_unified_diff, but diffs the skeleton *content* only
    (ignoring line numbers) so that lines shifting by a line or two --
    normal given EN/RU text length differences -- don't make every
    subsequent equal entry look like a spurious diff. Line numbers are
    still shown, just not used to decide what counts as a difference."""
    en_plain = [s for _, s in en_skel]
    ru_plain = [s for _, s in ru_skel]
    sm = difflib.SequenceMatcher(a=en_plain, b=ru_plain, autojunk=False)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        for i in range(i1, i2):
            out.append(f"    - {en_label}:  {en_skel[i][0]}:{en_skel[i][1]}")
        for j in range(j1, j2):
            out.append(f"    + {ru_label}:  {ru_skel[j][0]}:{ru_skel[j][1]}")
    return out


def _compare_skeleton_pair(en_file: Path, ru_file: Path, skeleton_fn, verbose) -> bool:
    en_skel = skeleton_fn(en_file)
    ru_skel = skeleton_fn(ru_file)
    en_plain = [s for _, s in en_skel]
    ru_plain = [s for _, s in ru_skel]
    if en_plain == ru_plain:
        return True
    print(f"DIFF     {en_file}")
    print(f"         {ru_file}")
    diff = _skeleton_diff_lines(en_skel, ru_skel, en_file, ru_file)
    if verbose or len(diff) <= _SKELETON_DIFF_PREVIEW:
        print("\n".join(diff))
    else:
        print("\n".join(diff[:_SKELETON_DIFF_PREVIEW]))
        print(f"         ... {len(diff) - _SKELETON_DIFF_PREVIEW} more diff line(s); "
              f"rerun with --verbose for the full diff")
    print()
    return False


def check_nav_structure_parity(verbose=False) -> bool:
    """Port of check_nav_structure_parity.sh. A module only has a nav.adoc
    of its own on some multi-module Antora sites (e.g. a top-level ROOT nav
    plus a second one for a "how-to" module); modules without one are
    silently skipped."""
    ok = True
    any_nav = False
    mismatch_count = 0
    for _, en_root, ru_root in module_roots():
        en_nav = en_root / "nav.adoc"
        ru_nav = ru_root / "nav.adoc"
        if not (en_nav.is_file() and ru_nav.is_file()):
            continue
        any_nav = True
        if not _compare_skeleton_pair(en_nav, ru_nav, _nav_skeleton, verbose):
            ok = False
            mismatch_count += 1

        en_text = _read_text(en_nav) or ""
        for partial_name in _INCLUDE_PARTIAL_RE.findall(en_text):
            en_partial = en_root / "partials" / partial_name
            ru_partial = ru_root / "partials" / partial_name
            if en_partial.is_file() and ru_partial.is_file():
                if not _compare_skeleton_pair(en_partial, ru_partial, _nav_skeleton, verbose):
                    ok = False
                    mismatch_count += 1

    if not any_nav:
        print("OK: no nav.adoc found to compare.")
    elif ok:
        print("OK: nav structure matches for en/ru.")
    else:
        print(f"\nTotal: {mismatch_count} mismatched file(s).")
    return ok


# --------------------------------------------------------------------------
# PAGES: broken references
# --------------------------------------------------------------------------

_REF_SCAN_RE = re.compile(r'(?:xref:|include::|injectSvg:{1,2}|inlineSVG:{1,2}|image:{1,2}|link:{1,2})[^\]\[\s]+\[')
_ANCHOR_ID_TPL = r'^\[#{0}\]$|\[\[{0}(,|\]\])'

# Antora injects these as page-scoped attributes pointing at each family's
# directory for the current module (https://docs.antora.org -- "family
# attributes"), since a generic Asciidoctor macro like link: isn't
# Antora-resource-ID-aware the way xref:/include::/image:: are. Only
# attachmentsdir is seen in practice in this org's repos (link:{attachmentsdir}/
# file[]), but the others are legitimate Antora attributes too.
_ANTORA_FAMILY_ATTR_RE = re.compile(r'^\{(attachmentsdir|examplesdir|imagesdir|partialsdir)\}/?(.*)$')
_ANTORA_FAMILY_DIRS = {
    "attachmentsdir": "attachments",
    "examplesdir": "examples",
    "imagesdir": "images",
    "partialsdir": "partials",
}
_INCLUDE_CONTENT_RE = re.compile(
    r'include::(?:([A-Za-z][A-Za-z0-9_-]*):)?(?:([A-Za-z][A-Za-z0-9_-]*):)?(partial|page)\$([^\[]+\.adoc)'
)
_COMPONENT_PREFIX_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_-]*:')
# Antora's version@component:module:page pin (e.g. "6.29.1.1@ADB:tutorials:
# adbc/external-db.adoc[]", a real pattern in docs-adbes linking to a specific
# past ADB release) -- the lookahead requires what follows "@" to actually
# look like a component prefix, so this can't misfire on an unrelated string
# that happens to contain "@". Deliberately never resolved even when
# --external-root registers that component: the root only ever holds
# whatever's checked out *now*, not the pinned historical version, so
# resolving against it would silently validate (or invalidate) the wrong
# content instead of just leaving it as the "external, unchecked" case this
# tool already has for any other component it can't verify.
_VERSION_PIN_RE = re.compile(r'^\d[\w.\-]*@(?=[A-Za-z][A-Za-z0-9_-]*:)')


def _strip_root_slash(t: str) -> str:
    """Antora resource-id-style targets (xref page paths, family$ include
    paths, injectSvg targets) are always relative to their module/family
    root -- a leading '/' some authors add for emphasis isn't filesystem
    syntax there. Left unstripped, `root / "pages" / t` would silently
    treat it as an absolute path and check against the OS filesystem root
    instead of the intended file (see the toast-storage.adoc glossary
    xref that surfaced this)."""
    return t.lstrip("/")

_HEADING_ID_RE = re.compile(r'^=+\s+(.*\S)\s*$')
_ID_STRIP_MARKUP_RE = re.compile(r'[`*]')
_ID_INVALID_CHARS_RE = re.compile(r'[^\w]+', re.UNICODE)
_ID_PREFIX_SEP_COMBOS = (("", "-"), ("_", "_"), ("", "_"), ("_", "-"))


def _heading_autoids(title: str):
    """Anchors are usually left implicit: a heading like `== 6.23.3` gets an
    ID Asciidoctor derives from its text, not one written in the source, so
    `_anchor_exists` can't just grep for `[#id]`/`[[id]]` -- it has to
    reproduce that derivation. The exact result depends on the site's
    `idprefix`/`idseparator` attributes (not visible to this tool, since
    they live in the Antora playbook, not this repo), so this tries a few
    common conventions and accepts a match against any of them."""
    plain = _ID_STRIP_MARKUP_RE.sub("", title).lower()
    return {
        prefix + _ID_INVALID_CHARS_RE.sub(sep, plain).strip(sep)
        for prefix, sep in _ID_PREFIX_SEP_COMBOS
    }


_OWN_COMPONENT_NAME_CACHE = {}


def _own_component_name(lang):
    """This repo's own antora.yml `name:` for `lang` (e.g. "ADB"), cached.
    A page is free to reference its own component's content fully
    qualified -- `image::ADCM:ROOT:clusters/downloads_en.png[]` written
    inside docs-adcm's own partials is a real, existing pattern, not just
    a hypothetical cross-repo one -- so every _resolve_module_ref call
    site needs this, not just the ones added for cross-repo tag usage."""
    if lang not in _OWN_COMPONENT_NAME_CACHE:
        antora_yml = (EN_MODULES_ROOT if lang == "en" else RU_MODULES_ROOT).parent / "antora.yml"
        _OWN_COMPONENT_NAME_CACHE[lang] = _parse_component_name(antora_yml)
    return _OWN_COMPONENT_NAME_CACHE[lang]


def _resolve_module_ref(name, rest, lang_module_roots, lang, own_name=None):
    """Resolve a single `name:` prefix already peeled off a target/include
    path. `name` may be a module in this repo's current language, or (if
    registered via --external-root) a sibling Antora component -- in which
    case an optional following `module:` segment at the start of `rest`
    selects the module within it (defaulting to ROOT, same as Antora).
    `own_name` (this repo's own antora.yml `name:`, e.g. "ADB") lets a
    self-qualified reference resolve back into `lang_module_roots` too --
    needed when `name`/`rest` come from a *different* repo's content (see
    _collect_tag_usage scanning a registered external component's pages/
    partials), which may reference this repo the same fully-qualified way
    it would reference any other sibling component, e.g.
    `include::ADB:how-to:metrics.adoc[tag=...]` written in docs-adbes.
    Returns (target_root, remaining_rest), or None if `name` names
    something this tool can't resolve (unregistered external component --
    left unchecked, not reported broken)."""
    if name in lang_module_roots:
        return lang_module_roots[name], rest
    modules = EXTERNAL_COMPONENTS.get(name, {}).get(lang)
    if modules is None and name == own_name:
        modules = lang_module_roots
    if modules is None:
        # Not a module here, not a registered --external-root, not our own
        # component name: nothing to resolve against, so the reference is
        # left unchecked rather than reported broken. Record the name --
        # a run that silently skips a whole component otherwise looks
        # exactly like one that verified it (see _report_skipped_components).
        _SKIPPED_COMPONENTS.add(name)
        return None
    if rest.startswith(":"):
        # Antora's explicit-empty-module form (`component::page`) means the
        # same thing as omitting the module segment entirely
        # (`component:page`, the `not m` branch below) -- both default to
        # ROOT -- so strip the leading ':' the same way a named module's
        # trailing ':' is stripped just below. Without this, a self- or
        # externally-qualified `component::page.adoc` xref/include/image
        # resolves with a stray leading ':' still in the path, which never
        # matches a real file and gets reported broken even though the
        # reference is fine (seen in docs-greengagedb's own
        # `xref:docs-gg::connect_with_psql.adoc[]`, self-qualified with its
        # own antora.yml component name).
        rest = rest[1:]
        return (modules["ROOT"], rest) if "ROOT" in modules else None
    m = _COMPONENT_PREFIX_RE.match(rest)
    if m:
        module = m.group(0)[:-1]
        if module in modules:
            return modules[module], rest[len(m.group(0)):]
        return None
    if "ROOT" in modules:
        return modules["ROOT"], rest
    return None


def _collect_include_partials(file: Path, root: Path, lang_module_roots=None, lang="en", depth=0, seen=None):
    """A page's anchors may live in content it pulls in via
    include::partial$...[] or include::page$...[] (recursively, and
    possibly module- or component-qualified, e.g.
    include::how-to:partial$...[] or include::ADCM:ROOT:partial$...[]), not
    its own source. Depth-capped to guard against an accidental include
    cycle."""
    if seen is None:
        seen = set()
    if depth > 5 or file in seen:
        return []
    seen.add(file)
    result = [file]
    text = _read_text(file)
    if text is None:
        return result
    lang_module_roots = lang_module_roots or {}
    own_name = _own_component_name(lang)
    for prefix1, prefix2, family, name in _INCLUDE_CONTENT_RE.findall(text):
        target_root = root
        if prefix1:
            resolved = _resolve_module_ref(prefix1, f"{prefix2}:" if prefix2 else "", lang_module_roots, lang, own_name)
            if resolved is None:
                continue  # external component's content, not registered via --external-root
            target_root, _ = resolved
        subdir = "partials" if family == "partial" else "pages"
        target_file = target_root / subdir / _strip_root_slash(name)
        if target_file.is_file():
            result.extend(_collect_include_partials(target_file, target_root, lang_module_roots, lang, depth + 1, seen))
    return result


def _anchor_exists(target_file: Path, anchor_id: str, root: Path, lang_module_roots=None, lang="en") -> bool:
    pattern = re.compile(_ANCHOR_ID_TPL.format(re.escape(anchor_id)))
    for f in _collect_include_partials(target_file, root, lang_module_roots, lang):
        text = _read_text(f)
        if text is None:
            continue
        for line in text.splitlines():
            if pattern.search(line):
                return True
            m = _HEADING_ID_RE.match(line)
            if m and anchor_id in _heading_autoids(m.group(1)):
                return True
    return False


_CODE_DELIM_LINE_RE = re.compile(r'^(----|\.\.\.\.)\s*$')


def _excluded_ref_lines(path: Path) -> set:
    """Line numbers to skip when scanning for references: AsciiDoc line
    (`//`) and block (`////`) comments, and anything inside a ---- / ....
    literal/listing block. Tracks *which* delimiter opened the current
    block rather than a single shared on/off toggle, so a line that looks
    like the *other* delimiter but is really just literal content inside an
    already-open block -- e.g. a `----` table-separator row from `psql`
    output sitting inside a `....` literal block -- doesn't prematurely
    close tracking (only a line matching the delimiter that opened the
    block can close it, matching how Asciidoctor itself pairs delimiters by
    type)."""
    lines = _read_lines(path)
    if lines is None:
        return set()
    excluded = set()
    open_delim = None
    in_comment = False
    for lineno, line in enumerate(lines, 1):
        stripped = line.strip()
        if re.match(r'^/{4,}\s*$', line):
            in_comment = not in_comment
            excluded.add(lineno)
            continue
        if in_comment:
            excluded.add(lineno)
            continue
        if stripped.startswith("//"):
            excluded.add(lineno)
            continue
        m = _CODE_DELIM_LINE_RE.match(line)
        if m:
            if open_delim is None:
                open_delim = m.group(1)
            elif open_delim == m.group(1):
                open_delim = None
            excluded.add(lineno)
            continue
        if open_delim is not None:
            excluded.add(lineno)
    return excluded


_DOC_ATTR_DEF_RE = re.compile(r'^:([A-Za-z0-9_-]+):\s*(.*)$')
_ATTR_REF_RE = re.compile(r'\{([A-Za-z0-9_-]+)\}')


def _collect_doc_attrs(lines):
    """Document attributes (`:name: value`) defined anywhere in the file, so
    a reference target using `{name}` (e.g. `xref:{install-link}[]`) can be
    resolved the way Asciidoctor would substitute it. Attributes are only
    collected from the file itself, not from included partials or the
    Antora playbook, so a target relying on those is left unresolved (and
    reported, same as an unknown attribute)."""
    attrs = {}
    for line in lines:
        m = _DOC_ATTR_DEF_RE.match(line)
        if m:
            attrs[m.group(1)] = m.group(2).strip()
    return attrs


def _substitute_attrs(text, attrs):
    return _ATTR_REF_RE.sub(lambda m: attrs.get(m.group(1), m.group(0)), text)


def _check_refs_in_file(file: Path, root: Path, report, lang_module_roots=None, lang="en", partial_includers=None):
    """`lang_module_roots` (name -> module root, for the same language as
    `root`) lets a component-prefixed xref (`xref:other-module:page.adoc[]`)
    resolve against a sibling module on multi-module Antora sites, instead
    of always being treated as pointing outside this repo. `lang` selects
    which side of any registered --external-root component to resolve
    against. `partial_includers` (see _build_partial_includers) lets an
    *unqualified* xref/include written inside a partial be checked against
    the module(s) that actually include it -- that's the context Antora
    resolves it in, not the partial file's own directory."""
    lines = _read_lines(file)
    if lines is None:
        return
    excluded = _excluded_ref_lines(file)
    directory = file.parent
    lang_module_roots = lang_module_roots or {}
    own_name = _own_component_name(lang)
    doc_attrs = _collect_doc_attrs(lines)
    fallback_roots = (partial_includers or {}).get(file) or {root}

    for lineno, line in enumerate(lines, 1):
        if lineno in excluded:
            continue
        for m in _REF_SCAN_RE.finditer(line):
            target = m.group(0)[:-1]  # strip trailing '['
            if "{" in target:
                target = _substitute_attrs(target, doc_attrs)

            if target.startswith("xref:"):
                t = target[len("xref:"):]
                if _VERSION_PIN_RE.match(t):
                    continue  # version@component:... pin -- can't resolve, left unchecked
                candidate_roots = list(fallback_roots)
                m_component = _COMPONENT_PREFIX_RE.match(t)
                if m_component:
                    component = m_component.group(0)[:-1]  # strip trailing ':'
                    resolved = _resolve_module_ref(component, t[len(m_component.group(0)):], lang_module_roots, lang, own_name)
                    if resolved is None:
                        continue  # external component xref (blog::x, ...)
                    candidate_roots = [resolved[0]]
                    t = resolved[1]

                fragment = ""
                if "#" in t:
                    t, fragment = t.split("#", 1)

                if t.endswith(".adoc"):
                    page_t = _strip_root_slash(t)
                    found_root = next((cand for cand in candidate_roots if (cand / "pages" / page_t).is_file()), None)
                    if found_root is None:
                        report(file, lineno, f"xref:{t}")
                    elif fragment and not _anchor_exists(found_root / "pages" / page_t, fragment, found_root, lang_module_roots, lang):
                        report(file, lineno, f"xref:{t}#{fragment} (anchor not found)")
                elif t:
                    if not any(_anchor_exists(file, t, cand, lang_module_roots, lang) for cand in candidate_roots):
                        report(file, lineno, f"xref:{t} (anchor not found)")

            elif target.startswith("include::"):
                t = target[len("include::"):]
                if _VERSION_PIN_RE.match(t):
                    continue  # version@component:... pin -- can't resolve, left unchecked
                candidate_roots = list(fallback_roots)
                qualified = False
                m_component = _COMPONENT_PREFIX_RE.match(t)
                if m_component:
                    component = m_component.group(0)[:-1]  # strip trailing ':'
                    resolved = _resolve_module_ref(component, t[len(m_component.group(0)):], lang_module_roots, lang, own_name)
                    if resolved is None:
                        continue  # external component/module include, not registered via --external-root
                    candidate_roots = [resolved[0]]
                    t = resolved[1]
                    qualified = True

                target_file = None
                if t.startswith("partial$"):
                    name = _strip_root_slash(t[len("partial$"):])
                    target_file = next((cand / "partials" / name for cand in candidate_roots
                                         if (cand / "partials" / name).is_file()), None)
                    if target_file is None:
                        report(file, lineno, target)
                elif t.startswith("example$"):
                    name = _strip_root_slash(t[len("example$"):])
                    target_file = next((cand / "examples" / name for cand in candidate_roots
                                         if (cand / "examples" / name).is_file()), None)
                    if target_file is None:
                        report(file, lineno, target)
                elif t.startswith("page$"):
                    name = _strip_root_slash(t[len("page$"):])
                    target_file = next((cand / "pages" / name for cand in candidate_roots
                                         if (cand / "pages" / name).is_file()), None)
                    if target_file is None:
                        report(file, lineno, target)
                elif qualified:
                    # A component/module-qualified include with no family$
                    # marker defaults to the page family, same as xref:
                    # already does above for a bare module:page.adoc.
                    name = _strip_root_slash(t)
                    target_file = next((cand / "pages" / name for cand in candidate_roots
                                         if (cand / "pages" / name).is_file()), None)
                    if target_file is None:
                        report(file, lineno, target)
                elif (directory / t).is_file():
                    target_file = directory / t
                else:
                    report(file, lineno, target)

                if target_file is not None:
                    # tag=/tags= names a region that must actually exist in
                    # the included file -- Antora silently renders nothing
                    # for a tag that isn't there (e.g. connections.adoc
                    # defining tag::allow-remote-connections1[] while a page
                    # includes tag=allow-remote-connections), so a plain
                    # file-exists check misses it.
                    attrs_end = line.find("]", m.end())
                    attrs_str = line[m.end():attrs_end] if attrs_end != -1 else ""
                    wanted_tags, _negated, _whole_file = _parse_include_attrs(attrs_str)
                    if wanted_tags:
                        target_lines = _read_lines(target_file)
                        if target_lines is not None:
                            defined_tags = {name for name, _, _ in _parse_tag_regions(target_lines)}
                            for missing in sorted(wanted_tags - defined_tags):
                                report(file, lineno, f"{target}  (tag '{missing}' not found in {target_file})")

            elif target.startswith("image::") or target.startswith("image:"):
                prefix = "image::" if target.startswith("image::") else "image:"
                t = target[len(prefix):]
                if t.startswith(("http://", "https://")):
                    continue  # remote image URL, not a local file to check
                if _VERSION_PIN_RE.match(t):
                    continue  # version@component:... pin -- can't resolve, left unchecked
                candidate_roots = list(fallback_roots)
                m_component = _COMPONENT_PREFIX_RE.match(t)
                if m_component:
                    component = m_component.group(0)[:-1]  # strip trailing ':'
                    resolved = _resolve_module_ref(component, t[len(m_component.group(0)):], lang_module_roots, lang, own_name)
                    if resolved is None:
                        continue  # external component image, not registered via --external-root
                    candidate_roots = [resolved[0]]
                    t = resolved[1]
                t = _strip_root_slash(t)
                if not any((cand / "images" / t).is_file() for cand in candidate_roots):
                    report(file, lineno, target)

            elif target.startswith("injectSvg::"):
                t = _strip_root_slash(target[len("injectSvg::"):])
                if not (root / "images" / t).is_file():
                    report(file, lineno, f"injectSvg::{t}")

            elif target.startswith("injectSvg:"):
                t = _strip_root_slash(target[len("injectSvg:"):])
                if not (root / "images" / t).is_file():
                    report(file, lineno, f"injectSvg:{t}")

            elif target.startswith("inlineSVG::"):
                t = _strip_root_slash(target[len("inlineSVG::"):])
                if not (root / "images" / t).is_file():
                    report(file, lineno, f"inlineSVG::{t}")

            elif target.startswith("inlineSVG:"):
                t = _strip_root_slash(target[len("inlineSVG:"):])
                if not (root / "images" / t).is_file():
                    report(file, lineno, f"inlineSVG:{t}")

            elif target.startswith("link::") or target.startswith("link:"):
                prefix = "link::" if target.startswith("link::") else "link:"
                t = target[len(prefix):]
                if "://" in t or t.startswith(("mailto:", "#")):
                    continue  # remote URL, mailto, or same-page anchor -- not a local file
                m_attr = _ANTORA_FAMILY_ATTR_RE.match(t)
                if m_attr:
                    family_dir = _ANTORA_FAMILY_DIRS[m_attr.group(1)]
                    name = _strip_root_slash(m_attr.group(2))
                    if not any((cand / family_dir / name).is_file() for cand in fallback_roots):
                        report(file, lineno, target)
                elif "{" in t:
                    continue  # some other attribute (site/playbook-level, not visible to this tool) -- unchecked
                elif not (directory / t).is_file():
                    report(file, lineno, target)


def _build_partial_includers(module_list, lang_module_roots, lang):
    """{partial_file: {module_root, ...}} for every partials/*.adoc file
    (in this language) actually pulled in via include::...partial$...[]
    somewhere in the site. Antora resolves an unqualified xref/include
    found *inside* a partial using the context of whichever page includes
    it -- the partial's content becomes part of that page's document during
    conversion -- not the partial file's own directory. So a bare
    `xref:foo.adoc[]` written in a partial that's only ever included from
    module A must be checked against module A, even though the partial
    physically lives under module B's partials/."""
    includers = {}
    own_name = _own_component_name(lang)
    for _, module_root in module_list:
        for f in list(_iter_files(module_root / "pages", ".adoc")) + list(_iter_files(module_root / "partials", ".adoc")):
            text = _read_text(f)
            if text is None:
                continue
            for prefix1, prefix2, family, name in _INCLUDE_CONTENT_RE.findall(text):
                if family != "partial":
                    continue
                target_root = module_root
                if prefix1:
                    resolved = _resolve_module_ref(prefix1, f"{prefix2}:" if prefix2 else "", lang_module_roots, lang, own_name)
                    if resolved is None:
                        continue
                    target_root, _ = resolved
                includers.setdefault(target_root / "partials" / _strip_root_slash(name), set()).add(module_root)
    return includers


def _report_skipped_components():
    """Name the components whose references this run left unchecked. The
    counterpart to the --external-root warning in _load_external_components:
    that catches a *wrong* path, this catches an *omitted* one -- the more
    common case, since nobody passes a flag they've forgotten they need.
    Not a finding (nothing is known to be broken), so it doesn't fail the
    run -- but silence here means an unverified component reads exactly
    like a verified one."""
    skipped = sorted(_SKIPPED_COMPONENTS)
    if not skipped:
        return
    print(f"\nnote: {len(skipped)} referenced component(s) left unchecked -- "
          f"{', '.join(skipped)}", file=sys.stderr)
    print("      pass --external-root NAME=PATH for each one you have "
          "checked out locally", file=sys.stderr)


def check_pages_broken_refs(verbose=False) -> bool:
    """Port of check_pages_broken_refs.sh, extended to resolve
    component-prefixed xrefs against sibling modules of the same language
    when the component name matches a discovered module."""
    ok = True
    broken_count = 0
    _SKIPPED_COMPONENTS.clear()   # report only what this scan itself skipped

    def report(file, lineno, msg):
        nonlocal ok, broken_count
        ok = False
        broken_count += 1
        print(f"BROKEN   {file}:{lineno}  {msg}")

    modules = list(module_roots())
    en_module_roots = {name: en_root for name, en_root, _ in modules}
    ru_module_roots = {name: ru_root for name, _, ru_root in modules}
    en_module_list = [(name, en_root) for name, en_root, _ in modules]
    ru_module_list = [(name, ru_root) for name, _, ru_root in modules]
    en_includers = _build_partial_includers(en_module_list, en_module_roots, "en")
    ru_includers = _build_partial_includers(ru_module_list, ru_module_roots, "ru")

    for _, en_root, ru_root in modules:
        for lang, root, lang_module_roots, includers in (
                ("en", en_root, en_module_roots, en_includers),
                ("ru", ru_root, ru_module_roots, ru_includers),
        ):
            for f in list(_iter_files(root / "pages", ".adoc")) + list(_iter_files(root / "partials", ".adoc")):
                _check_refs_in_file(f, root, report, lang_module_roots, lang, includers)

    if ok:
        print("OK: no broken xref/include/image references found.")
    else:
        print(f"\nTotal: {broken_count} broken reference(s).")
    _report_skipped_components()
    return ok


# --------------------------------------------------------------------------
# TAGS: orphaned tagged regions
# --------------------------------------------------------------------------

# Deliberately not anchored to a comment prefix (//, --, #, ...) -- tag/end
# markers are always written inside whatever that file's comment syntax is
# (adoc pages/partials use //, .sql examples use --), and the marker itself
# is the same regardless of which one wraps it.
_TAG_START_RE = re.compile(r'\btag::([\w][\w.-]*)\[\]')
_TAG_END_RE = re.compile(r'\bend::([\w][\w.-]*)\[\]')


def _parse_tag_regions(lines):
    """Returns [(name, start_lineno, end_lineno), ...] for every balanced
    tag::NAME[]/end::NAME[] region in `lines`. Matched by name (not simple
    stack order) so a region opened while another is still open -- e.g. a
    tag nested inside a wider one, like connect.adoc's part-02 wrapping
    part-03 -- still pairs with the right end:: marker even if regions
    close out of nesting order."""
    open_stack = []
    regions = []
    for lineno, line in enumerate(lines, 1):
        m = _TAG_START_RE.search(line)
        if m:
            open_stack.append((m.group(1), lineno))
            continue
        m = _TAG_END_RE.search(line)
        if m:
            name = m.group(1)
            for i in range(len(open_stack) - 1, -1, -1):
                if open_stack[i][0] == name:
                    _, start = open_stack.pop(i)
                    regions.append((name, start, lineno))
                    break
    return regions


def _parse_include_attrs(attrs_str):
    """Returns (tags_used, negated_tags, whole_file) parsed from an
    include::...[...] attribute list. tag=/tags= may name one or more tags
    (tags= separates them with ';', Asciidoctor style); a leading '!'
    negates a tag (an explicit exclusion, tracked separately since it
    overrides mere nesting -- see check_tags_orphaned) and a bare '*'/'**'
    wildcard means every (non-negated) tag in the file is pulled in. An
    include with no tag/tags attribute at all (the common case, e.g. `[]`
    or `[leveloffset=+1]`) pulls in the whole file -- and therefore every
    tag it contains -- same as a wildcard would."""
    tags = set()
    negated = set()
    whole_file = True
    for part in attrs_str.split(","):
        part = part.strip()
        if part.startswith("tags="):
            whole_file = False
            for tok in part[len("tags="):].split(";"):
                tok = tok.strip()
                if not tok:
                    continue
                if tok.startswith("!"):
                    negated.add(tok[1:])
                    continue
                if tok in ("*", "**"):
                    whole_file = True
                else:
                    tags.add(tok)
        elif part.startswith("tag="):
            whole_file = False
            tok = part[len("tag="):].strip()
            if tok:
                tags.add(tok)
    return tags, negated, whole_file


def _excluded_comment_lines(path: Path) -> set:
    """Line numbers to skip as AsciiDoc comments: '//' line comments and
    '////' block comments. Unlike _excluded_ref_lines, this deliberately
    does NOT exclude ---- / .... listing/source blocks -- an
    include::...[tag=...] macro living inside one (the standard way to pull
    in an example snippet so it renders as code) is still processed by
    Asciidoctor and must count as real usage, not be skipped the way a
    broken-refs scan skips illustrative text inside a listing block."""
    lines = _read_lines(path)
    if lines is None:
        return set()
    excluded = set()
    in_comment = False
    for lineno, line in enumerate(lines, 1):
        stripped = line.strip()
        if re.match(r'^/{4,}\s*$', line):
            in_comment = not in_comment
            excluded.add(lineno)
            continue
        if in_comment:
            excluded.add(lineno)
            continue
        if stripped.startswith("//"):
            excluded.add(lineno)
    return excluded


def _resolve_include_target(t, directory, root, lang_module_roots, lang, own_name=None):
    """Resolve an include::TARGET[...] target string to a concrete file
    Path, the same way _check_refs_in_file's include branch does: an
    optional component/module prefix (peeled off via _resolve_module_ref,
    passing through `own_name` so a self-qualified reference to this repo
    written in someone else's content still resolves), then family$name
    (partial$/page$/example$) relative to that root's subdir. Lacking a
    family marker, a *qualified* target (one that had a component/module
    prefix, e.g. `include::ADB:how-to:metrics.adoc[tag=x]`) defaults to the
    page family, same as xref: already does for a bare `module:page.adoc`
    -- while a truly plain, unqualified target is a filesystem path
    relative to the *including* file's own directory (e.g.
    `include::freeipa_kerberos.adoc[tag=x]` from a sibling page$ file,
    which is a real, common form -- not every include is Antora
    resource-id-qualified). Returns None for an unregistered external
    component, same as broken-refs: can't resolve, not this tool's problem
    to check."""
    candidate_root = root
    qualified = False
    if _VERSION_PIN_RE.match(t):
        return None  # version@component:... pin -- can't resolve, left unchecked
    m_component = _COMPONENT_PREFIX_RE.match(t)
    if m_component:
        component = m_component.group(0)[:-1]
        resolved = _resolve_module_ref(component, t[len(m_component.group(0)):], lang_module_roots, lang, own_name)
        if resolved is None:
            return None
        candidate_root, t = resolved
        qualified = True

    if t.startswith("partial$"):
        return candidate_root / "partials" / _strip_root_slash(t[len("partial$"):])
    elif t.startswith("example$"):
        return candidate_root / "examples" / _strip_root_slash(t[len("example$"):])
    elif t.startswith("page$"):
        return candidate_root / "pages" / _strip_root_slash(t[len("page$"):])
    elif qualified:
        return candidate_root / "pages" / _strip_root_slash(t)
    else:
        return directory / t


_INCLUDE_MACRO_RE = re.compile(r'include::([^\[\s]+)\[([^\]]*)\]')


def _collect_tag_usage(lang_module_roots, lang):
    """Scans every pages/partials file (in this language) for
    include::...[...] macros and returns a dict mapping a resolved target
    file to a list of (tags, negated, whole_file) events, one per include
    call site that references it -- kept per call site, deliberately not
    merged into one blanket set of "tags ever requested"/"tags ever
    negated" per file, because a nested tag's usage has to be judged
    per call site: the same nested tag can be pulled in unnegated by one
    include (`tag=parent`, which also renders everything nested inside
    `parent`) and separately negated by another (`tags=parent;!nested`) --
    merging those into one set would make the negation win globally and
    call the nested tag orphaned even though the first include really
    does render it (see check_tags_orphaned, and the real docs-greengagedb
    case that motivated this: `compression_codecs_no_compression` nested
    in `compression_codecs`, negated only in the includes that also list
    `format_null`/`compression_codecs` explicitly alongside `!..._no_compression`,
    but rendered plainly wherever `tag=compression_codecs` is used alone).
    pages/partials/nav.adoc are scanned as *sources* of includes -- nav.adoc
    routinely pulls in a whole partial via `include::partial$...[]` (e.g. a
    long submenu factored out of the main nav tree, as in the real
    docs-greengagedb case that motivated adding it here:
    nav_reference_utils.adoc/nav_reference_admin_schemas.adoc, each included
    only from nav.adoc and nowhere else, wrongly reported orphaned by
    check_partials_orphaned before nav.adoc was scanned) -- while examples
    are always a leaf, never something that itself includes other content.

    Sources aren't limited to this repo's own modules: a tag defined here
    can just as well be pulled in from a registered --external-root
    component's content (e.g. docs-adbes writing
    `include::ADB:how-to:metrics.adoc[tag=view_metrics_prometheus]`), so
    every registered external component's pages/partials (same `lang`) are
    scanned too. `own_name` -- this repo's own antora.yml `name:` -- is
    passed down so that a self-qualified reference like that one resolves
    back into `lang_module_roots`, the same way it would for a genuinely
    external sibling component. Without any --external-root, no component
    is registered and this is exactly the previous, single-repo-only
    behavior."""
    events = {}
    own_name = _own_component_name(lang)
    source_roots = list(lang_module_roots.values())
    for comp_modules in EXTERNAL_COMPONENTS.values():
        source_roots.extend(comp_modules.get(lang, {}).values())
    for root in source_roots:
        nav = root / "nav.adoc"
        nav_files = [nav] if nav.is_file() else []
        for f in list(_iter_files(root / "pages", ".adoc")) + list(_iter_files(root / "partials", ".adoc")) + nav_files:
            lines = _read_lines(f)
            if lines is None:
                continue
            excluded = _excluded_comment_lines(f)
            directory = f.parent
            for lineno, line in enumerate(lines, 1):
                if lineno in excluded:
                    continue
                for t, attrs in _INCLUDE_MACRO_RE.findall(line):
                    target_file = _resolve_include_target(t, directory, root, lang_module_roots, lang, own_name)
                    if target_file is None:
                        continue  # external component's content, not registered via --external-root
                    tags, negated, whole_file = _parse_include_attrs(attrs)
                    events.setdefault(target_file, []).append((tags, negated, whole_file))
    return events


def check_tags_orphaned(verbose=False) -> bool:
    """New check (not a port of an existing shell script): every
    tag::NAME[]/end::NAME[] region defined in an examples/pages/partials
    file must actually be pulled in somewhere -- directly via
    include::...[tag=NAME]/[tags=NAME;...], nested inside another region
    that is itself used, or via a plain/wildcarded include of the whole
    file. A tag satisfying none of those is dead: nothing in the rendered
    site ever shows that content, however it looks in the source. "Pulled
    in somewhere" includes registered --external-root components' own
    content, not just this repo's (see _collect_tag_usage) -- without that
    flag, a tag only ever consumed by a sibling repo looks orphaned here
    even though it's genuinely rendered there.

    A file with no tag::/end:: regions at all is skipped here -- it has no
    tagged region to judge -- but a partials/ file in that shape (content
    meant to be pulled in only as a whole file via a bare include::...[])
    isn't left unchecked: see check_partials_orphaned, which covers that
    exact case.

    --page filters which files' own tag regions get reported on, not the
    usage scan: _collect_tag_usage still scans every page/partial in the
    site regardless of --page, since a tag defined in the filtered-in file
    can be include::...[tag=...]'d from any other page -- narrowing that
    scan would risk a false "orphaned" report for a tag that's actually
    used from a file --page filtered out."""
    ok = True
    orphaned_count = 0
    modules = list(module_roots())
    en_module_roots = {name: en_root for name, en_root, _ in modules}
    ru_module_roots = {name: ru_root for name, _, ru_root in modules}

    for lang, lang_module_roots in (("en", en_module_roots), ("ru", ru_module_roots)):
        tag_events = _collect_tag_usage(lang_module_roots, lang)

        for root in lang_module_roots.values():
            for d in ("examples", "pages", "partials"):
                for f in _iter_files(root / d):
                    if d != "examples" and f.suffix != ".adoc":
                        continue
                    if not _page_allowed(f):
                        continue
                    events = tag_events.get(f, [])
                    lines = _read_lines(f)
                    if lines is None:
                        continue
                    regions = _parse_tag_regions(lines)
                    if not regions:
                        continue
                    for name, start, end in regions:
                        # Judged per include call site (`events`), not by
                        # merging tags/negations across every call site
                        # into one blanket per-file verdict: the same
                        # nested tag can be rendered plainly by one include
                        # and separately excluded by another, and either
                        # one alone is enough to make it "used" (see
                        # _collect_tag_usage).
                        used = False
                        for tags, negated, whole_file in events:
                            if name in tags:
                                used = True
                                break
                            if name in negated:
                                continue  # excluded in this call site -- check the others
                            if whole_file:
                                used = True
                                break
                            # A parent region covers this one by nesting only if
                            # this call site's tags= actually requested that
                            # parent (a nested tag pulled in and then cut back
                            # out via !this in the *same* call site never
                            # renders, but that's already handled by the
                            # `name in negated` check above).
                            if any(oname != name and ostart <= start and end <= oend and oname in tags
                                   for oname, ostart, oend in regions):
                                used = True
                                break
                        if used:
                            continue
                        ok = False
                        orphaned_count += 1
                        print(f"ORPHANED  {f}:{start}  tag::{name}[]  (never pulled in via tag=/tags=)")

    if ok:
        print("OK: all tagged regions are included somewhere.")
    else:
        print(f"\nTotal: {orphaned_count} orphaned tag(s).")
    return ok


def check_partials_orphaned(verbose=False) -> bool:
    """Every partials/ file with no tag::/end:: regions at all -- content
    meant to be pulled in only as a whole file via a bare include::...[]
    -- must actually be included somewhere. The same shape of check as
    check_examples_orphaned (a whole content file nothing pulls in is
    dead), but for partials/ instead of examples/, and reusing
    _collect_tag_usage's include-macro resolution (component/module-
    qualified targets, external roots, relative-path includes) instead of
    a plain substring scan, since a partial can be referenced by a bare
    include::sibling.adoc[] from another partial in the same directory,
    not only include::partial$name.adoc[...].

    A partial that DOES have tag::/end:: regions is judged tag-by-tag by
    check_tags_orphaned instead -- this check only covers the whole-file
    case, which check_tags_orphaned skips (nothing to match a tag against).
    Without this check, a tag-less partial that nothing includes anymore
    (e.g. after the last include::partial$foo.adoc[] referencing it was
    deleted) would be invisible to every orphaned-content check -- pages
    are covered by check_pages_orphaned (nav.adoc reachability) and
    examples by check_examples_orphaned, but this exact gap in partials/
    was the real-world docs-adb hosts-online.adoc case that motivated it."""
    ok = True
    orphaned_count = 0
    modules = list(module_roots())
    en_module_roots = {name: en_root for name, en_root, _ in modules}
    ru_module_roots = {name: ru_root for name, _, ru_root in modules}

    for lang, lang_module_roots in (("en", en_module_roots), ("ru", ru_module_roots)):
        tag_events = _collect_tag_usage(lang_module_roots, lang)

        for root in lang_module_roots.values():
            for f in _iter_files(root / "partials"):
                if f.suffix != ".adoc":
                    continue
                if not _page_allowed(f):
                    continue
                lines = _read_lines(f)
                if lines is None:
                    continue
                if _parse_tag_regions(lines):
                    continue  # has tags of its own -- check_tags_orphaned judges it
                events = tag_events.get(f, [])
                if any(whole_file for _, _, whole_file in events):
                    continue
                ok = False
                orphaned_count += 1
                print(f"ORPHANED  {f}  (whole-file partial, never pulled in via include::...[])")

    if ok:
        print("OK: all whole-file partials are included somewhere.")
    else:
        print(f"\nTotal: {orphaned_count} orphaned partial(s).")
    return ok


# --------------------------------------------------------------------------
# PAGES: line parity
# --------------------------------------------------------------------------

def check_pages_line_parity(verbose=False) -> bool:
    """Port of check_pages_line_parity.sh (matches `wc -l` semantics: counts
    newline characters, not logical/visual lines)."""
    ok = True
    mismatch_count = 0
    for _, en_root, ru_root in module_roots():
        for subdir in ("pages", "partials"):
            for en_file in _iter_files(en_root / subdir, ".adoc"):
                if not _page_allowed(en_file):
                    continue
                rel = en_file.relative_to(en_root)
                ru_file = ru_root / rel
                if not ru_file.is_file():
                    print(f"MISSING  {en_file}  (no ru counterpart)")
                    ok = False
                    mismatch_count += 1
                    continue
                en_n = (_read_text(en_file) or "").count("\n")
                ru_n = (_read_text(ru_file) or "").count("\n")
                if en_n != ru_n:
                    print(f"DIFF     {en_file}  ({en_n} lines)")
                    print(f"         {ru_file}  ({ru_n} lines)")
                    ok = False
                    mismatch_count += 1

            for ru_file in _iter_files(ru_root / subdir, ".adoc"):
                if not _page_allowed(ru_file):
                    continue
                rel = ru_file.relative_to(ru_root)
                if not (en_root / rel).is_file():
                    print(f"MISSING  {ru_file}  (no en counterpart)")
                    ok = False
                    mismatch_count += 1

    if ok:
        print("OK: all compared en/ru pages have matching line counts.")
    else:
        print(f"\nTotal: {mismatch_count} mismatch(es).")
    return ok


# --------------------------------------------------------------------------
# PAGES: no Cyrillic / no unicode dashes
# --------------------------------------------------------------------------

def _first_match_hits(lines, pattern):
    """Yield (lineno, col, line) for the first match of `pattern` on each
    line that has one; col is a 1-based character offset, so a hit can be
    reported as a conventional, editor/terminal-clickable `file:line:col`
    reference instead of leaving a human to scan the whole line by eye for
    which character actually matched (the original complaint this fixes:
    a long line with one Cyrillic letter buried in it gave no way to jump
    straight to it)."""
    for i, l in enumerate(lines, 1):
        m = pattern.search(l)
        if m:
            yield i, m.start() + 1, l


def check_pages_no_cyrillic(verbose=False) -> bool:
    """Port of check_pages_no_cyrillic.sh (en/ only, all modules)."""
    ok = True
    total_hits = 0
    for _, en_root, _ in module_roots():
        for f in list(_iter_files(en_root / "pages", ".adoc")) + list(_iter_files(en_root / "partials", ".adoc")):
            if not _page_allowed(f):
                continue
            lines = _read_lines(f)
            if lines is None:
                continue
            hits = list(_first_match_hits(lines, CYRILLIC_RE))
            if hits:
                ok = False
                total_hits += len(hits)
                print(f"FILE     {f}")
                for i, col, l in hits:
                    print(f"  {f}:{i}:{col}: {l}")
    if ok:
        print("OK: no Cyrillic characters found in en/ pages.")
    else:
        print(f"\nTotal: {total_hits} line(s) with Cyrillic characters.")
    return ok


def _invisible_char_label(ch: str) -> str:
    """Human-readable label for an invisible character: its Unicode name if
    it has one, alongside the codepoint -- some tag characters format as
    nothing printable of their own, so the codepoint is sometimes all
    there is to go on."""
    try:
        name = unicodedata.name(ch)
    except ValueError:
        name = "UNKNOWN"
    return f"U+{ord(ch):04X} {name}"


def _mark_invisible_chars(line: str) -> str:
    """Render a line with every invisible character swapped for a visible
    marker -- printed verbatim, a hit would be indistinguishable from a
    clean line, which would defeat the point of the check."""
    return _INVISIBLE_RE.sub(lambda m: f"⟦U+{ord(m.group(0)):04X}⟧", line)


def check_pages_no_invisible_chars(verbose=False) -> bool:
    """New check (not a port of an existing shell script): flags zero-width
    and other invisible/formatting Unicode characters -- ZWSP, ZWNJ, ZWJ,
    word joiner, BOM, bidi control marks, and Unicode tag characters -- in
    en/ru pages/partials (see _INVISIBLE_RANGES for the full list and why).
    These render as nothing, so unlike the Cyrillic/dash checks, hits are
    reported with the character swapped for a visible marker rather than
    printed as-is."""
    ok = True
    total_hits = 0
    for _, en_root, ru_root in module_roots():
        for root in (en_root, ru_root):
            for f in list(_iter_files(root / "pages", ".adoc")) + list(_iter_files(root / "partials", ".adoc")):
                if not _page_allowed(f):
                    continue
                lines = _read_lines(f)
                if lines is None:
                    continue
                hits = list(_first_match_hits(lines, _INVISIBLE_RE))
                if hits:
                    ok = False
                    total_hits += len(hits)
                    print(f"FILE     {f}")
                    for i, col, l in hits:
                        labels = ", ".join(sorted({_invisible_char_label(ch) for ch in _INVISIBLE_RE.findall(l)}))
                        print(f"  {f}:{i}:{col}: {labels}")
                        if verbose:
                            print(f"    {_mark_invisible_chars(l)}")
    if ok:
        print("OK: no invisible/zero-width characters found in pages.")
    else:
        print(f"\nTotal: {total_hits} line(s) with invisible characters.")
    return ok


def check_pages_no_unicode_dashes(verbose=False) -> bool:
    """Port of check_pages_no_unicode_dashes.sh (en/ and ru/, all modules)."""
    ok = True
    total_hits = 0
    for _, en_root, ru_root in module_roots():
        for root in (en_root, ru_root):
            for f in list(_iter_files(root / "pages", ".adoc")) + list(_iter_files(root / "partials", ".adoc")):
                if not _page_allowed(f):
                    continue
                lines = _read_lines(f)
                if lines is None:
                    continue
                hits = list(_first_match_hits(lines, EN_EM_DASH_RE))
                if hits:
                    ok = False
                    total_hits += len(hits)
                    print(f"FILE     {f}")
                    for i, col, l in hits:
                        print(f"  {f}:{i}:{col}: {l}")
    if ok:
        print("OK: no en dash (–) or em dash (—) characters found in pages.")
    else:
        print(f"\nTotal: {total_hits} line(s) with en/em dash characters.")
    return ok


_YO_RE = re.compile(r'[ёЁ]')


def check_pages_no_yo(verbose=False) -> bool:
    """New check (not a port of an existing shell script): flags the
    letter ё/Ё in ru/ pages/partials -- house style spells it out as е
    instead, standard practice for most Russian technical writing (and
    already the convention this doc set's own content follows). The
    `:page-author:` attribute is exempt: a real person's name can
    legitimately contain ё (e.g. "Фёдоров"), and that's not something to
    rewrite."""
    ok = True
    total_hits = 0
    for _, _, ru_root in module_roots():
        for f in list(_iter_files(ru_root / "pages", ".adoc")) + list(_iter_files(ru_root / "partials", ".adoc")):
            if not _page_allowed(f):
                continue
            lines = _read_lines(f)
            if lines is None:
                continue
            hits = [(i, col, l) for i, col, l in _first_match_hits(lines, _YO_RE)
                    if not l.startswith(":page-author:")]
            if hits:
                ok = False
                total_hits += len(hits)
                print(f"FILE     {f}")
                for i, col, l in hits:
                    print(f"  {f}:{i}:{col}: {l}")
    if ok:
        print("OK: no ё/Ё characters found in ru/ pages.")
    else:
        print(f"\nTotal: {total_hits} line(s) with ё/Ё characters.")
    return ok


_PASSTHROUGH_RE = re.compile(r'\+\+.*?\+\+')


def _count_delimiter_backticks(line: str) -> int:
    """Backtick count after stripping `++...++` passthrough spans. AsciiDoc
    has no double-backtick span the way Markdown does, so the only way to
    put a literal backtick inside a monospace span is a passthrough like
    `` `++`++` `` -- the backtick inside `++...++` is literal content, not
    a delimiter, and left in would make a correctly paired line look
    unbalanced."""
    return _PASSTHROUGH_RE.sub('', line).count('`')


def check_pages_stray_backticks(verbose=False) -> bool:
    """New check (not a port of an existing shell script): flags lines in
    en/ru pages/partials with an odd number of backticks -- almost always a
    missing or stray ` around an inline monospace span (e.g. a trailing `
    left dangling after an xref, or a closing ` dropped from `code`). Lines
    inside comments/listing blocks are skipped (see _excluded_ref_lines)."""
    ok = True
    total_hits = 0
    for _, en_root, ru_root in module_roots():
        for root in (en_root, ru_root):
            for f in list(_iter_files(root / "pages", ".adoc")) + list(_iter_files(root / "partials", ".adoc")):
                if not _page_allowed(f):
                    continue
                lines = _read_lines(f)
                if lines is None:
                    continue
                excluded = _excluded_ref_lines(f)
                hits = [(i, l) for i, l in enumerate(lines, 1)
                        if i not in excluded and _count_delimiter_backticks(l) % 2 == 1]
                if hits:
                    ok = False
                    total_hits += len(hits)
                    print(f"FILE     {f}")
                    for i, l in hits:
                        print(f"  {f}:{i}: {l.strip()}")
    if ok:
        print("OK: no stray/unbalanced backticks found in pages.")
    else:
        print(f"\nTotal: {total_hits} line(s) with an odd number of backticks.")
    return ok


_BLOCK_DELIM_LINE_RE = re.compile(
    r'^(?:[aehlmsd]\|)?(-{4,}|-{2}|\.{4,}|={4,}|\*{4,}|_{4,}|\+{4,}|\|={3,}|/{4,})\s*$'
)


def _delimiter_kind(text: str) -> str:
    """Human label for a delimiter line's block kind, for reporting only --
    not used for matching/pairing."""
    ch = text[0]
    if ch == '-':
        return "open block (--)" if len(text) == 2 else "listing block (----)"
    return {
        '.': "literal block (....)",
        '=': "example block (====)",
        '*': "sidebar block (****)",
        '_': "quote block (____)",
        '+': "passthrough block (++++)",
        '|': "table (|===)",
        '/': "comment block (////)",
    }.get(ch, "block")


_OPAQUE_DELIM_CHARS = frozenset('-.  /'.replace(' ', ''))  # listing, literal, comment


def _resolve_include_for_flatten(target, directory, root, lang_module_roots, lang, own_name):
    """Like _resolve_include_target, but also returns the module root that
    the resolved file's *own* unqualified include::partial$/page$/example$
    targets should resolve against -- its own module, not necessarily the
    including file's -- since a component/module-qualified include can
    cross into a different module (or, via --external-root, a different
    repo entirely) whose further nested includes must resolve there, not
    back where the chain started. A plain, unqualified target stays within
    the current module's `root`. Returns (None, None) if `target` can't be
    resolved (an unregistered external component, same left-unchecked
    policy as everywhere else) or doesn't exist on disk."""
    if _VERSION_PIN_RE.match(target):
        return None, None
    t = target
    candidate_root = root
    qualified = False
    m_component = _COMPONENT_PREFIX_RE.match(t)
    if m_component:
        component = m_component.group(0)[:-1]
        resolved = _resolve_module_ref(component, t[len(m_component.group(0)):], lang_module_roots, lang, own_name)
        if resolved is None:
            return None, None
        candidate_root, t = resolved
        qualified = True
    if t.startswith("partial$"):
        f = candidate_root / "partials" / _strip_root_slash(t[len("partial$"):])
    elif t.startswith("page$"):
        f = candidate_root / "pages" / _strip_root_slash(t[len("page$"):])
    elif t.startswith("example$"):
        f = candidate_root / "examples" / _strip_root_slash(t[len("example$"):])
    elif qualified:
        f = candidate_root / "pages" / _strip_root_slash(t)
    else:
        f = directory / t
        candidate_root = root
    if not f.is_file():
        return None, None
    return f, candidate_root


def _tag_filtered_line_numbers(lines, tags, negated, whole_file):
    """Which 1-based line numbers of `lines` an include::...[tag=/tags=...]
    call actually pulls in, mirroring Asciidoctor's own selection rules (see
    _parse_include_attrs): with no filter (or a `*`/`**` wildcard) every
    line is included except any explicitly negated region; with specific
    tags requested, only lines inside those regions are included, and a
    negated region removes itself even if nested inside a requested one.
    Returns None (not a set) for the plain, no-filter case as a cheap
    "everything, don't bother building a set" sentinel."""
    if whole_file and not negated:
        return None
    regions = _parse_tag_regions(lines)
    if whole_file:
        included = set(range(1, len(lines) + 1))
        for name, start, end in regions:
            if name in negated:
                included -= set(range(start, end + 1))
        return included
    included = set()
    for name, start, end in regions:
        if name in tags:
            included |= set(range(start, end + 1))
    for name, start, end in regions:
        if name in negated:
            included -= set(range(start, end + 1))
    return included


def _flatten_delimiter_lines(file, root, lang_module_roots, lang, own_name, active_path, visited, depth=0, only_lines=None):
    """Yields (source_file, source_lineno, line_text) for `file`, splicing
    include::partial$/page$/example$ targets in place (recursively,
    honoring tag=/tags= filtering and component/module qualification) the
    way Asciidoctor actually assembles the rendered document -- so a
    delimited block deliberately (or accidentally) split across an include
    boundary is tracked as one continuous stream instead of two separately
    "broken" files.

    `active_path` guards against a genuine include cycle (A includes B
    includes A) without preventing the same partial from being included
    more than once at separate, non-overlapping points -- a real, common
    pattern (see e.g. custom-ulimits.adoc, reused by three different
    pages) where each occurrence's content must still be spliced in
    independently. `visited` collects every file actually reached this
    way, across the whole run, so the caller can afterward find partials
    no page ever includes and still check those standalone.

    A commented-out include:: line (inside a `////` block, or `//`-prefixed
    on its own) is not specially detected here and would incorrectly be
    resolved as if live -- a known, narrow limitation shared with the
    rest of this tool's include handling, accepted because a real
    disabled-include-inside-a-comment pattern hasn't been observed in
    practice and detecting it fully would require duplicating full
    comment-state tracking at every recursion level."""
    if depth > 25 or file in active_path:
        return
    lines = _read_lines(file)
    if lines is None:
        return
    visited.add(file)
    active_path = active_path + [file]
    directory = file.parent
    for lineno, line in enumerate(lines, 1):
        if only_lines is not None and lineno not in only_lines:
            continue
        stripped = line.strip()
        m = _INCLUDE_MACRO_RE.match(stripped) if stripped.startswith("include::") else None
        if not m:
            yield file, lineno, line
            continue
        target, attrs_str = m.group(1), m.group(2)
        resolved, target_root = _resolve_include_for_flatten(target, directory, root, lang_module_roots, lang, own_name)
        if resolved is None:
            continue  # external/unresolvable -- left unchecked, same policy as broken-refs
        target_lines = _read_lines(resolved)
        if target_lines is None:
            continue
        tags, negated, whole_file = _parse_include_attrs(attrs_str)
        included = _tag_filtered_line_numbers(target_lines, tags, negated, whole_file)
        yield from _flatten_delimiter_lines(resolved, target_root, lang_module_roots, lang, own_name,
                                             active_path, visited, depth + 1, included)


def _scan_delimiter_stack(line_stream):
    """Runs the LIFO delimiter-balance algorithm over `line_stream` -- an
    iterable of (source_file, source_lineno, line_text), possibly splicing
    content from more than one actual file via _flatten_delimiter_lines --
    and returns every delimiter that never got a proper close: [(text,
    source_file, source_lineno), ...], in the order each was opened.

    Keyed by the delimiter's exact matched text (not just its family) to
    mirror how Asciidoctor itself supports nesting a *container* block
    (example/sidebar/quote/open/table) inside a same-type container: the
    inner one uses a *different* length (e.g. a 5-equals `=====` example
    block nested inside a 4-equals `====` one), so only a line matching the
    text that opened the current innermost container can close it -- a
    line of the same family but a different length instead opens a new,
    independent nesting level.

    A line whose text doesn't match the top of the stack isn't necessarily
    such a nested open, though -- it's just as likely a forgotten close
    somewhere below (e.g. a table's `|===` open with no matching `|===`
    before the enclosing `====` block closes around it). Blindly stacking
    a new level on every mismatch, as a naive LIFO would, means that one
    missing delimiter derails the pairing of everything after it, and the
    leftover-stack report ends up blaming unrelated, far-away lines instead
    of the actual culprit. So on a mismatch this also checks *deeper* in
    the stack: if some still-open entry below the top has the exact same
    text, that line is almost certainly meant to close it, meaning
    everything stacked on top of that entry was never really closed --
    the same recovery a browser does for mismatched HTML tags. Those
    in-between entries are reported as unclosed and popped along with the
    match; only when no entry anywhere in the stack matches does the line
    genuinely open a new, independent nesting level.

    Listing (`----`), literal (`....`), and comment (`////`) blocks are
    different: Asciidoctor treats them as verbatim/opaque leaves that
    cannot contain a nested block of *any* kind, so once one is open, every
    other delimiter-looking line is just its raw content, not a real
    delimiter -- e.g. a `psql` ASCII-art table's own `----` separator row,
    or an arbitrary run of dashes/dots/equals inside a terminal-output
    `....` block, shown verbatim, must not be mistaken for a nested block
    boundary. Only a line matching the *exact* text that opened it can
    close such a block -- no deeper-stack recovery for these, since
    nothing else can ever be pushed on top of one anyway while it's open."""
    stack = []
    unclosed = []
    for source_file, source_lineno, line in line_stream:
        m = _BLOCK_DELIM_LINE_RE.match(line)
        if not m:
            continue
        text = m.group(1)
        if stack and stack[-1][0][0] in _OPAQUE_DELIM_CHARS and text != stack[-1][0]:
            continue  # raw content inside an open verbatim/opaque block
        if stack and stack[-1][0] == text:
            stack.pop()
            continue
        match_index = None
        for i in range(len(stack) - 1, -1, -1):
            if stack[i][0] == text:
                match_index = i
                break
        if match_index is None:
            stack.append((text, source_file, source_lineno))
        else:
            unclosed.extend(stack[match_index + 1:])
            del stack[match_index:]
    unclosed.extend(stack)
    return unclosed


def check_pages_unbalanced_delimiters(verbose=False) -> bool:
    """New check (not a port of an existing shell script): flags AsciiDoc
    block delimiters -- open `--`, listing `----`, literal `....`, example
    `====`, sidebar `****`, quote `____`, passthrough `++++`, table `|===`,
    comment `////` -- left unclosed once a page's full include chain
    (partial$/page$/example$, recursively, honoring tag=/tags= filtering)
    is flattened into the single continuous document Asciidoctor actually
    renders (see _flatten_delimiter_lines). Checking each file in
    isolation would misfire both ways on a block deliberately split across
    an include boundary -- a shared partial that opens a table and relies
    on whichever page includes it to supply the closing `|===` -- reporting
    the partial as broken even though it's fine in context, or the other
    way around, reporting a page as broken because of decorative dashes
    living inside a partial's own already-open literal block. Almost
    always a forgotten closing delimiter, which silently swallows every
    following line into that block (or, for a table, corrupts everything
    after it) when actually rendered.

    Every page is checked this way, since the flattened document is what a
    page actually renders as. Partials never reached by any page's include
    chain (in this language) -- typically genuinely orphaned content, also
    caught by --check-pages-orphaned/--check-examples-orphaned for other
    reasons -- are still checked standalone afterward, so a partial that
    happens not to be wired up anywhere doesn't silently lose delimiter
    coverage entirely."""
    ok = True
    total_hits = 0
    modules = list(module_roots())
    en_module_roots = {name: en_root for name, en_root, _ in modules}
    ru_module_roots = {name: ru_root for name, _, ru_root in modules}

    for lang, lang_module_roots in (("en", en_module_roots), ("ru", ru_module_roots)):
        own_name = _own_component_name(lang)
        visited = set()
        for root in lang_module_roots.values():
            for page in _iter_files(root / "pages", ".adoc"):
                if not _page_allowed(page):
                    continue
                stream = _flatten_delimiter_lines(page, root, lang_module_roots, lang, own_name, [], visited)
                stack = _scan_delimiter_stack(stream)
                if stack:
                    ok = False
                    total_hits += len(stack)
                    print(f"FILE     {page}")
                    for text, sfile, slineno in stack:
                        if sfile == page:
                            print(f"  {sfile}:{slineno}: unclosed {_delimiter_kind(text)}: {text!r}")
                        else:
                            print(f"  {sfile}:{slineno}: unclosed {_delimiter_kind(text)}: {text!r}  (included from {page})")

        for root in lang_module_roots.values():
            for partial in _iter_files(root / "partials", ".adoc"):
                if partial in visited or not _page_allowed(partial):
                    continue
                lines = _read_lines(partial)
                if lines is None:
                    continue
                stack = _scan_delimiter_stack((partial, i, l) for i, l in enumerate(lines, 1))
                if stack:
                    ok = False
                    total_hits += len(stack)
                    print(f"FILE     {partial}  (not reached by any page's includes -- checked standalone)")
                    for text, sfile, slineno in stack:
                        print(f"  {sfile}:{slineno}: unclosed {_delimiter_kind(text)}: {text!r}")
    if ok:
        print("OK: no unbalanced block delimiters found in pages.")
    else:
        print(f"\nTotal: {total_hits} unclosed block delimiter(s).")
    return ok


# --------------------------------------------------------------------------
# PAGES: orphaned (not reachable from nav.adoc)
# --------------------------------------------------------------------------

_START_PAGE_RE = re.compile(r'^start_page:\s*(?:([\w-]+):)?(\S+)', re.MULTILINE)
_COMPONENT_NAME_RE = re.compile(r'^name:\s*(\S+)', re.MULTILINE)
_COMMENT_LINE_ONLY_RE = re.compile(r'^\s*//')
# A page can opt out of nav reachability entirely by declaring a standalone
# page-layout (e.g. a PDF-only entry point rendered by a build extension
# rather than linked from any nav.adoc) -- such pages are never orphaned by
# definition, whatever nav.adoc does or doesn't say about them.
_STANDALONE_PAGE_LAYOUT_RE = re.compile(r'^:page-layout:\s*pdf-glossary\s*$', re.MULTILINE)


def _strip_comment_lines(text: str) -> str:
    return "\n".join(l for l in text.splitlines() if not _COMMENT_LINE_ONLY_RE.match(l))


def _parse_start_page(antora_yml: Path):
    """Returns (module_name, page_rel) for antora.yml's start_page, or
    (None, None) if not found. Antora defaults an unqualified start_page
    (no "module:" prefix) to the ROOT module."""
    antora_text = _read_text(antora_yml)
    if not antora_text:
        return None, None
    m = _START_PAGE_RE.search(antora_text)
    if not m:
        return None, None
    module = m.group(1) or "ROOT"
    return module, m.group(2).strip()


def _parse_component_name(antora_yml: Path):
    """Returns antora.yml's own component `name:` (e.g. "ADB"), or None. A
    nav.adoc is free to xref its own component's pages fully qualified
    (`xref:ADB:release-notes:page.adoc[]`) instead of the shorter
    module-qualified form -- both must count as "reachable"."""
    antora_text = _read_text(antora_yml)
    if not antora_text:
        return None
    m = _COMPONENT_NAME_RE.search(antora_text)
    return m.group(1) if m else None


def _combined_nav_text(root: Path) -> str:
    """A module's own nav.adoc plus any include::partial$...[] partials it
    pulls in, comments stripped."""
    nav = root / "nav.adoc"
    nav_text = _read_text(nav)
    if nav_text is None:
        return ""
    parts = [_strip_comment_lines(nav_text)]
    for partial_name in _INCLUDE_PARTIAL_RE.findall(nav_text):
        partial_text = _read_text(root / "partials" / partial_name)
        if partial_text is not None:
            parts.append(_strip_comment_lines(partial_text))
    return "\n".join(parts)


def check_pages_orphaned(verbose=False) -> bool:
    """Port of check_pages_orphaned.sh, generalized for multi-module Antora
    sites: a page is considered reachable if *any* module's nav.adoc (they
    can cross-reference each other, e.g. `xref:other-module:page.adoc[]`)
    contains an xref to it, either bare (same-module/default-component form),
    module-qualified, or fully component-qualified (`xref:<component
    name>:<module>:page.adoc[]` -- nav.adoc is free to spell out its own
    component name explicitly instead of using the shorter module-qualified
    form)."""
    ok = True
    orphaned_count = 0
    modules = list(module_roots())

    for lang_attr in ("en_root", "ru_root"):
        idx = 0 if lang_attr == "en_root" else 1
        lang_roots = {name: (en_root, ru_root)[idx] for name, en_root, ru_root in modules}
        modules_root = EN_MODULES_ROOT if idx == 0 else RU_MODULES_ROOT
        antora_yml = modules_root.parent / "antora.yml"
        start_module, start_page = _parse_start_page(antora_yml)
        component_name = _parse_component_name(antora_yml)

        # Union of every module's nav (a page can be linked from a sibling
        # module's nav via a component-qualified xref, not just its own).
        combined_nav_text = "\n".join(_combined_nav_text(r) for r in lang_roots.values() if (r / "nav.adoc").is_file())
        if not combined_nav_text:
            continue

        for name, root in lang_roots.items():
            for f in _iter_files(root / "pages", ".adoc"):
                rel = f.relative_to(root / "pages").as_posix()
                if start_module == name and start_page == rel:
                    continue
                if (f"xref:{rel}" in combined_nav_text
                        or f"xref:{name}:{rel}" in combined_nav_text
                        or (component_name and f"xref:{component_name}:{name}:{rel}" in combined_nav_text)):
                    continue
                page_text = _read_text(f)
                if page_text and _STANDALONE_PAGE_LAYOUT_RE.search(page_text):
                    continue
                ok = False
                orphaned_count += 1
                print(f"ORPHANED  {f}  (not referenced in any nav.adoc)")

    if ok:
        print("OK: all pages are referenced in nav.adoc.")
    else:
        print(f"\nTotal: {orphaned_count} orphaned page(s).")
    return ok


# --------------------------------------------------------------------------
# PAGES: structure parity (EN vs RU)
# --------------------------------------------------------------------------

_STRUCT_LINE_RE = re.compile(
    r'^(=+ |\.[^. ]|----$|\.\.\.\.$|====$|\*\*\*\*$|\|===$|\[.*\]$|include::)'
)
_STRUCT_HEADING_RE = re.compile(r'^(=+) .*')
_STRUCT_BLOCKTITLE_RE = re.compile(r'^\.[^. ].*')
# A line that is *only* "+": the AsciiDoc list-continuation marker that glues
# a following block (an admonition, a nested list, another code block, ...)
# onto the preceding list item. Dropping it (turning it into a blank line)
# silently detaches that block from the list in the rendered output, so it
# must line up between en/ru even though it carries no translatable text.
# (Not to be confused with a trailing " +" line-break at the end of a prose
# line, which this deliberately does not match.)
_STRUCT_CONTINUATION_RE = re.compile(r'^\+\s*$')
# List-item markers: repeated char = nesting depth (e.g. "**" is a level-2
# bullet, ".." a level-2 ordered item); "-" is AsciiDoc's single-level bullet
# form and "<digits>." an explicit-number ordered item (both flattened to
# depth 1, since neither nests). A translator flattening/renumbering a list
# changes the rendered structure but not any tracked text, so this is
# normalized to a depth+kind token rather than compared verbatim -- only the
# nesting shape has to match, not incidental digits. AsciiDoc list markers
# only count at column 0, so this deliberately doesn't match indented lines;
# spot-checking this repo found indented "*"/"..." look-alikes only ever
# occur as literal text inside [source] blocks, which are consumed by the
# in_code branch above before this runs.
_STRUCT_LIST_MARKER_RE = re.compile(r'^(\*{1,5}|\.{1,5}|[0-9]+\.|-)\s+\S')
_STRUCT_SOURCE_ATTR_RE = re.compile(r'^\[source(,.*)?\]$')
# Code comments are routinely translated inside otherwise-untranslated code
# blocks (see e.g. the SQL comments in select_cte.adoc, table_distribution.adoc)
# -- a whole-line comment is normalized to a placeholder (its presence as a
# comment still has to line up, but its translated text doesn't matter), and a
# padded trailing "  # comment" / "  -- comment" is stripped before comparing
# an otherwise-real code line.
_STRUCT_FULL_COMMENT_RE = re.compile(r'^\s*(?:#|--|//|;{1,2})(?:\s|$)')
_STRUCT_TRAILING_COMMENT_RE = re.compile(r'\s{2,}(?:#|--|//|;{1,2}).*$')
# A bare """ or ''' on its own line opens/closes a Python docstring -- a
# common idiom for describing an example snippet from *inside* the code
# (see e.g. client-usage-examples.adoc) rather than with a #-comment above
# it. Its content is translated same as a comment would be; only the
# delimiter itself (and the fact that a docstring is there at all) matters.
_STRUCT_DOCSTRING_DELIM_RE = re.compile(r'^\s*("""|\'\'\')\s*$')
# Delimiter lines, tolerant of stray trailing whitespace (seen in the wild --
# e.g. a "==== " admonition closer -- which the exact-match "====$" etc. in
# _STRUCT_LINE_RE below would otherwise silently fail to recognize at all,
# making that line invisible to the skeleton instead of just insensitive to
# the whitespace).
_STRUCT_TOLERANT_DELIM_RE = re.compile(r'^(----|\.\.\.\.|====|\*\*\*\*)\s*$')


def _structure_skeleton(path: Path):
    lines = _read_lines(path)
    if lines is None:
        return []
    out = []
    in_code = False
    code_delim = None
    source_pending = False
    in_docstring = False
    for lineno, line in enumerate(lines, 1):
        if in_code:
            delim = _code_delim_type(line)
            if delim == code_delim:
                out.append((lineno, line.rstrip()))
                in_code = False
                code_delim = None
                in_docstring = False
            elif in_docstring:
                if _STRUCT_DOCSTRING_DELIM_RE.match(line):
                    out.append((lineno, line.strip()))
                    in_docstring = False
                else:
                    out.append((lineno, "code> <docstring>"))
            elif _STRUCT_DOCSTRING_DELIM_RE.match(line):
                out.append((lineno, line.strip()))
                in_docstring = True
            elif _STRUCT_FULL_COMMENT_RE.match(line):
                out.append((lineno, "code> <comment>"))
            else:
                out.append((lineno, "code> " + _STRUCT_TRAILING_COMMENT_RE.sub("", line)))
            continue

        if _STRUCT_CONTINUATION_RE.match(line):
            out.append((lineno, "+"))
            source_pending = False
            continue

        m = _STRUCT_LIST_MARKER_RE.match(line)
        if m:
            marker = m.group(1)
            if marker == "-":
                out.append((lineno, "list> bullet1"))
            elif marker[0] == "*":
                out.append((lineno, f"list> bullet{len(marker)}"))
            elif marker[0].isdigit():
                out.append((lineno, "list> ordered1"))
            else:
                out.append((lineno, f"list> ordered{len(marker)}"))
            source_pending = False
            continue

        # Checked ahead of the stricter _STRUCT_LINE_RE gate (which requires
        # an exact "----"/"...."/"===="/"****" with nothing else on the line)
        # so a delimiter with e.g. a stray trailing space -- seen in the wild
        # in this doc set, for both "----" and "====" -- is still recognized.
        # For "----"/"...." specifically, missing this would leave
        # source_pending stuck true, and the next unrelated "----" anywhere
        # later in the file would be wrongly read as the start of a code
        # block, corrupting everything in between.
        m = _STRUCT_TOLERANT_DELIM_RE.match(line)
        if m:
            out.append((lineno, m.group(1)))
            delim = _code_delim_type(line)
            if delim and source_pending:
                in_code = True
                code_delim = delim
            source_pending = False
            continue

        if not _STRUCT_LINE_RE.match(line):
            continue

        m = _STRUCT_HEADING_RE.match(line)
        if m:
            out.append((lineno, f"{m.group(1)} <heading>"))
            source_pending = False
            continue
        if _STRUCT_BLOCKTITLE_RE.match(line):
            out.append((lineno, ".<block title>"))
            continue

        out.append((lineno, line))

        if _STRUCT_SOURCE_ATTR_RE.match(line):
            source_pending = True
        else:
            source_pending = False
    return out


def check_pages_structure_parity(verbose=False) -> bool:
    """Port of check_pages_structure_parity.sh."""
    ok = True
    mismatch_count = 0
    for _, en_root, ru_root in module_roots():
        for subdir in ("pages", "partials"):
            for en_file in _iter_files(en_root / subdir, ".adoc"):
                if not _page_allowed(en_file):
                    continue
                rel = en_file.relative_to(en_root)
                ru_file = ru_root / rel
                if not ru_file.is_file():
                    print(f"MISSING  {en_file}  (no ru counterpart)")
                    ok = False
                    mismatch_count += 1
                    continue
                if not _compare_skeleton_pair(en_file, ru_file, _structure_skeleton, verbose):
                    ok = False
                    mismatch_count += 1

            for ru_file in _iter_files(ru_root / subdir, ".adoc"):
                if not _page_allowed(ru_file):
                    continue
                rel = ru_file.relative_to(ru_root)
                if not (en_root / rel).is_file():
                    print(f"MISSING  {ru_file}  (no en counterpart)")
                    ok = False
                    mismatch_count += 1

    if ok:
        print("OK: en/ru structure matches for all compared files.")
    else:
        print(f"\nTotal: {mismatch_count} mismatch(es).")
    return ok


# --------------------------------------------------------------------------
# PAGES: link parity / literal parity (EN vs RU)
#
# Both walk the same en/ru page pairs as structure-parity and compare a
# bag of *language-invariant* tokens -- things a translator has to carry
# across unchanged. A divergence means a cross-reference, an external
# link, or a code identifier was dropped, added, retargeted, or (for a
# literal) accidentally translated.
# --------------------------------------------------------------------------


def _en_ru_page_pairs():
    """Yield (en_file, ru_file) for every pages/ + partials/ .adoc under
    every module that passes the --page filter and exists on the EN side
    (ru_file may not exist yet -- the caller reports MISSING), then
    (None, ru_file) for every RU file with no EN counterpart. The shared
    walk behind the per-file EN/RU comparison checks."""
    for _, en_root, ru_root in module_roots():
        for subdir in ("pages", "partials"):
            for en_file in _iter_files(en_root / subdir, ".adoc"):
                if _page_allowed(en_file):
                    yield en_file, ru_root / en_file.relative_to(en_root)
            for ru_file in _iter_files(ru_root / subdir, ".adoc"):
                if _page_allowed(ru_file) and not (
                        en_root / ru_file.relative_to(ru_root)).is_file():
                    yield None, ru_file


def _emit_parity_rows(rows, verbose):
    """Print the per-file token rows, truncated to a preview unless
    --verbose (same cutoff as the structure-parity skeleton diff)."""
    if verbose or len(rows) <= _SKELETON_DIFF_PREVIEW:
        print("\n".join(rows))
    else:
        print("\n".join(rows[:_SKELETON_DIFF_PREVIEW]))
        print(f"         ... {len(rows) - _SKELETON_DIFF_PREVIEW} more; "
              f"rerun with --verbose for the full list")


# xref: / inline image: targets. The bracket that ends the target ("[")
# is required so a bare "xref:" in prose about the macro itself isn't
# picked up.
_XREF_TARGET_RE = re.compile(r'\bxref:([^\[\]\s]+)\[')
_INLINE_IMAGE_TARGET_RE = re.compile(r'\bimage:{1,2}([^\[\]\s]+)\[')
# A leading /en/ or /ru/ path segment, and an _en / -ru language tag just
# before a file extension -- both legitimately differ EN vs RU (a docs
# site serving a translated copy under a language path; a screenshot with
# a localised UI, downloads_en.png / downloads_ru.png).
_URL_LANG_PATH_RE = re.compile(r'^/(?:en|ru)(?=/|$)')
_ASSET_LANG_TAG_RE = re.compile(r'[_-](?:en|ru)(\.[A-Za-z0-9]+)$')


def _normalize_parity_url(url):
    """Lower-case scheme+host, drop a trailing slash, and fold the shapes
    that legitimately differ EN vs RU: an `en.`/`ru.` host prefix, a
    leading `/en/` or `/ru/` path segment, and any Wikipedia link (its
    article title is translated too) -- all deliberate localisation, not a
    divergence."""
    m = re.match(r'^(https?://)([^/]+)(/[^?#]*)?(.*)$', url, re.I)
    if not m:
        return url
    host = m.group(2).lower()
    if host.endswith("wikipedia.org"):
        return "https://wikipedia.org/<article>"
    host = re.sub(r'^(?:en|ru)\.', '', host)
    path = _URL_LANG_PATH_RE.sub("", (m.group(3) or "").rstrip("/"))
    return f"https://{host}{path}{m.group(4)}"


def _module_of(path: Path):
    """The Antora module a pages/partials file lives in --
    `<lang>/modules/<module>/pages/...` -> `<module>`, or None."""
    parts = path.parts
    for i in range(len(parts) - 1, 1, -1):
        if parts[i] in ("pages", "partials") and parts[i - 2] == "modules":
            return parts[i - 1]
    return None


def _normalize_xref_target(target, own_module):
    """Drop the `#fragment` (Antora derives it from the translated
    heading) and a leading `<own-module>:` self-qualifier -- from a page
    in module ROOT, `xref:ROOT:x.adoc` and `xref:x.adoc` resolve to the
    same page, and the two forms are used interchangeably."""
    target = target.split("#", 1)[0]
    if own_module and target.startswith(f"{own_module}:"):
        rest = target[len(own_module) + 1:]
        # only when what's left is a plain (module-less) target, so a
        # component ref that happens to start with the module name is safe
        if ":" not in rest:
            target = rest
    return target


def _link_parity_tokens(path: Path):
    """{token: [line numbers]} for every language-invariant link on a
    comment/code-filtered line: xref target files (fragment and
    own-module self-qualifier dropped -- see _normalize_xref_target),
    inline image targets, and external URLs. Image and URL targets are
    normalised for the `/en/`|`/ru/` and `_en`|`_ru` localisation
    patterns."""
    lines = _read_lines(path)
    if lines is None:
        return {}
    own_module = _module_of(path)
    excluded = _excluded_ref_lines(path)
    hits = {}
    for lineno, line in enumerate(lines, 1):
        if lineno in excluded:
            continue
        toks = [f"xref:{_normalize_xref_target(t, own_module)}"
                for t in _XREF_TARGET_RE.findall(line)]
        toks += [f"image:{_ASSET_LANG_TAG_RE.sub(r'_XX\1', t)}"
                 for t in _INLINE_IMAGE_TARGET_RE.findall(line)]
        toks += [f"url:{_normalize_parity_url(u)}"
                 for u in _extract_urls_from_line(line)]
        for t in toks:
            hits.setdefault(t, []).append(lineno)
    return hits


def _parity_refs(en_file, en_lines, ru_file, ru_lines, verbose):
    """Clickable `path:line` references for one differing token, so a
    finding opens straight from an IDE / editor terminal. Returns
    (row_suffix, extra_lines):

    * always a compact suffix -- the first hit on each side -- appended
      to the token's own row;
    * under --verbose, one indented sub-line per further occurrence."""
    sides = [(en_file, sorted(set(en_lines))), (ru_file, sorted(set(ru_lines)))]
    suffix = "   " + "  ".join(f"{f}:{ns[0]}" for f, ns in sides if ns)
    extra = []
    if verbose:
        extra = [f"        {f}:{n}" for f, ns in sides for n in ns[1:]]
    return (suffix if suffix.strip() else ""), extra


def check_pages_link_parity(verbose=False) -> bool:
    """EN and RU pages must reference the same things.

    Compares, per EN/RU page pair, the multiset of language-invariant link
    targets -- xref target *files*, inline `image:` targets, and external
    URLs. Link *text* is translated and ignored; only the destination is
    compared. A mismatch means a cross-reference or link was dropped,
    added, or retargeted in translation -- e.g. RU prose that silently
    loses an xref the EN reader gets. Honours --page. Every finding
    carries a clickable `path:line` (the first hit on each side);
    --verbose adds a sub-line for each further occurrence.

    Folded, not reported: a `#fragment` on an xref (Antora derives it from
    the translated heading), a redundant `<own-module>:` xref self-qualifier
    (`xref:ROOT:x.adoc` == `xref:x.adoc` from a ROOT page), a `/en/`|`/ru/`
    URL path segment or `en.`/`ru.` host, a Wikipedia article, and an
    `_en`|`_ru` tag on an image filename.

    Beta: a repo that deliberately links the EN docs site from RU pages
    (or vice versa), or writes a cross-module xref sometimes qualified and
    sometimes not, still shows up here -- treat the output as a review
    list. Not compared: targets inside ---- / .... blocks or // comments."""
    ok = True
    mismatch_count = 0
    for en_file, ru_file in _en_ru_page_pairs():
        if en_file is None:
            print(f"MISSING  {ru_file}  (no en counterpart)")
            ok = False
            mismatch_count += 1
            continue
        if not ru_file.is_file():
            print(f"MISSING  {en_file}  (no ru counterpart)")
            ok = False
            mismatch_count += 1
            continue
        en_hits, ru_hits = _link_parity_tokens(en_file), _link_parity_tokens(ru_file)
        en_c = Counter({k: len(v) for k, v in en_hits.items()})
        ru_c = Counter({k: len(v) for k, v in ru_hits.items()})
        if en_c == ru_c:
            continue
        print(f"DIFF     {en_file}")
        print(f"         {ru_file}")
        rows = []
        for t in sorted(set(en_c) | set(ru_c)):
            if en_c[t] == ru_c[t]:
                continue
            suffix, extra = _parity_refs(en_file, en_hits.get(t, []),
                                         ru_file, ru_hits.get(t, []), verbose)
            rows.append(f"    EN {en_c[t]} / RU {ru_c[t]}   {t}{suffix}")
            rows += extra
        _emit_parity_rows(rows, verbose)
        print()
        ok = False
        mismatch_count += 1

    if ok:
        print("OK: en/ru reference the same xrefs, images and URLs.")
    else:
        print(f"\nTotal: {mismatch_count} file(s) with link mismatches.")
    return ok


# Inline monospace: the ``double`` form first so a `single` span inside it
# isn't split out on its own.
_MONO_SPAN_RE = re.compile(r'``(.+?)``|`([^`]+)`')
# A balanced +...+ / ++...++ passthrough wrapper (`++<=++` -> <=). Anchored
# and count-matched so a trailing operator like `C++` is left intact.
_PASSTHROUGH_WRAP_RE = re.compile(r'^(\+{1,3})(.+?)\1$')

# Literals routinely back-ticked in one language and left bare in the
# other: true divergence in spirit, but almost never a translation bug and
# not worth a reviewer's time. Dropped before comparing. Extend per repo.
_LITERAL_PARITY_IGNORE = frozenset({"NULL", "true", "false"})


def _normalize_literal(tok):
    """Fold the incidental differences: surrounding whitespace, a `+...+`
    passthrough wrapper, and a trailing `()` on a function name (so
    `get_part_name()` and `get_part_name` are one token)."""
    tok = tok.strip()
    m = _PASSTHROUGH_WRAP_RE.match(tok)
    if m:
        tok = m.group(2).strip()
    if tok.endswith("()"):
        tok = tok[:-2]
    return tok


def _literal_parity_tokens(path: Path):
    """{literal: [line numbers]} for every inline monospace span on a
    comment/code-filtered line, after _normalize_literal. Dropped: empty
    tokens, spans with no alphanumeric character (`|`, `<=`, punctuation
    table artifacts), very long spans (a whole translated sentence in
    backticks), and _LITERAL_PARITY_IGNORE entries."""
    lines = _read_lines(path)
    if lines is None:
        return {}
    excluded = _excluded_ref_lines(path)
    hits = {}
    for lineno, line in enumerate(lines, 1):
        if lineno in excluded:
            continue
        for m in _MONO_SPAN_RE.finditer(line):
            tok = _normalize_literal(m.group(1) or m.group(2))
            if (not tok or len(tok) > 80 or tok in _LITERAL_PARITY_IGNORE
                    or not any(c.isalnum() for c in tok)):
                continue
            hits.setdefault(tok, []).append(lineno)
    return hits


def _pair_changed_literals(en_only, ru_only):
    """Match near-identical EN-only / RU-only literals -- a typo, a case
    slip, a renumbered identifier -- so they print as one CHANGED line
    instead of a scattered pair. Greedy, best-ratio-first; mutates the two
    lists to remove what it paired and returns the (en, ru) pairs."""
    pairs = []
    for a in list(en_only):
        best, best_r = None, 0.80
        for b in ru_only:
            r = difflib.SequenceMatcher(None, a, b).ratio()
            if r > best_r:
                best, best_r = b, r
        if best is not None:
            pairs.append((a, best))
            en_only.remove(a)
            ru_only.remove(best)
    return pairs


def check_pages_literal_parity(verbose=False) -> bool:
    """EN and RU pages must contain the same back-ticked literals.

    Compares, per EN/RU page pair, the *set* of inline monospace spans
    (`` `...` ``) -- identifiers, SQL keywords, parameter and function
    names, verbatim error strings: text a translator has to carry across
    unchanged. `foo()` and `foo` count as one token, a `++...++`
    passthrough wrapper is unwrapped, and pure-punctuation spans plus
    _LITERAL_PARITY_IGNORE entries are dropped.

    Reported per file: CHANGED (a near-identical pair -- a likely
    typo/case slip), then EN-only, then RU-only literals. Presence is
    what's compared: a literal legitimately repeated a different number of
    times on each side is normal and not a finding. Every finding carries
    a clickable `path:line` (the first hit on each side); --verbose adds a
    sub-line for each further occurrence.

    Beta: RU technical prose that back-ticks a term EN left bare (or the
    reverse) surfaces here and usually isn't a translation bug -- treat
    the output as a review list. Not compared: spans inside ---- / ....
    blocks or // comments."""
    ok = True
    mismatch_count = 0
    for en_file, ru_file in _en_ru_page_pairs():
        if en_file is None:
            print(f"MISSING  {ru_file}  (no en counterpart)")
            ok = False
            mismatch_count += 1
            continue
        if not ru_file.is_file():
            print(f"MISSING  {en_file}  (no ru counterpart)")
            ok = False
            mismatch_count += 1
            continue
        en_hits = _literal_parity_tokens(en_file)
        ru_hits = _literal_parity_tokens(ru_file)
        en_only = sorted(set(en_hits) - set(ru_hits))
        ru_only = sorted(set(ru_hits) - set(en_hits))
        if not en_only and not ru_only:
            continue
        changed = _pair_changed_literals(en_only, ru_only)
        print(f"DIFF     {en_file}")
        print(f"         {ru_file}")
        rows = []
        for a, b in changed:
            suffix, extra = _parity_refs(en_file, en_hits[a], ru_file, ru_hits[b], verbose)
            rows.append(f"    CHANGED   `{a}`  ->  `{b}`{suffix}")
            rows += extra
        for a in en_only:
            suffix, extra = _parity_refs(en_file, en_hits[a], ru_file, [], verbose)
            rows.append(f"    EN only   `{a}`{suffix}")
            rows += extra
        for b in ru_only:
            suffix, extra = _parity_refs(en_file, [], ru_file, ru_hits[b], verbose)
            rows.append(f"    RU only   `{b}`{suffix}")
            rows += extra
        _emit_parity_rows(rows, verbose)
        print()
        ok = False
        mismatch_count += 1

    if ok:
        print("OK: en/ru pages carry the same back-ticked literals.")
    else:
        print(f"\nTotal: {mismatch_count} file(s) with literal mismatches.")
    return ok


# --------------------------------------------------------------------------
# PAGES: untranslated-line heuristic
# --------------------------------------------------------------------------

_STOPWORDS = (
    "the|is|are|and|or|with|this|that|these|those|you|your|for|from|into|"
    "when|where|which|while|because|however|therefore|then|than|been|have|"
    "has|had|will|would|should|could|can|not|but|also|each|such|only|about|"
    "between|through|before|after|during|without|within|both|either|neither|"
    "more|most|some|any|all|other|same|its|their|our"
)
_STOPWORDS_RE = re.compile(rf'\b(?:{_STOPWORDS})\b')
_PRODUCT_NAMES_RE = re.compile(r'\b(?:CentOS|Ubuntu|Red Hat|RHEL)\b')

_SKIP_ATTR_RE = re.compile(r'^\[.*\]$')
_SKIP_CODESPAN_ITEM_RE = re.compile(r'^[*.\s]+`[^`]+`\s*$')
_SKIP_BOLDITALIC_ITEM_RE = re.compile(r'^[*.\s]+\*_.+_\*:?(\s*\S+)?\s*$')
_SKIP_TABLE_CELL_RE = re.compile(r'^(\.\d+\+)?[a-z]?\|')
_SKIP_ALLCAPS_TITLE_RE = re.compile(r'^\.[^a-z]+$')
_SKIP_FUNC_HEADING_RE = re.compile(r'^=+\s[A-Za-z_][A-Za-z0-9_]*\(.*\)')
_LOWERCASE_RE = re.compile(r'[a-z]')
_HEADING_RE = re.compile(r'^=+\s')

_STRIP_CODE_SPAN_RE = re.compile(r'`[^`]*`')
_STRIP_PLACEHOLDER_RE = re.compile(r'<[^>]*>')
_STRIP_DOUBLE_ANGLE_RE = re.compile(r'<<[^>]*>>')
_STRIP_BRACKET_RE = re.compile(r'\[[^\]]*\]')
_STRIP_PAREN_RE = re.compile(r'\([^)]*\)')
_STRIP_BOLD_RE = re.compile(r'\*\*[^*]*\*\*')
_STRIP_BOLDITALIC_RE = re.compile(r'\*_[^*_]*_\*')
_STRIP_ITALIC_RE = re.compile(r'_[^_]*_')
_STRIP_XREF_RE = re.compile(r'xref:\S*')
_STRIP_URL_RE = re.compile(r'https?://\S*')
_HYPHEN_JOIN_RE = re.compile(r'([a-z])-([a-z])')


def _code_delim_type(line: str):
    if re.match(r'^----\s*$', line):
        return "dash"
    if re.match(r'^\.\.\.\.\s*$', line):
        return "dot"
    if re.match(r'^\+\+\+\+\s*$', line):
        return "plus"  # passthrough block (e.g. [stem] math formulas), not prose
    return None


def _is_skip_line(line: str) -> bool:
    if line.strip() == "":
        return True
    if line.startswith(":"):
        return True
    if _SKIP_ATTR_RE.match(line):
        return True
    if line.startswith("include::"):
        return True
    if line.startswith("//"):
        return True
    if _SKIP_CODESPAN_ITEM_RE.match(line):
        return True
    if _SKIP_BOLDITALIC_ITEM_RE.match(line):
        return True
    if line[:1].isspace():
        return True
    if _SKIP_TABLE_CELL_RE.match(line):
        return True
    if _SKIP_ALLCAPS_TITLE_RE.match(line):
        return True
    if _SKIP_FUNC_HEADING_RE.match(line):
        return True
    if line.endswith("::"):
        return True
    if not _LOWERCASE_RE.search(line):
        return True

    stripped = _STRIP_CODE_SPAN_RE.sub("", line)
    stripped = _STRIP_PLACEHOLDER_RE.sub("", stripped)
    stripped = _PRODUCT_NAMES_RE.sub("", stripped)
    if not _LOWERCASE_RE.search(stripped):
        return True

    return False


# :description:/:page-htmltitle: are the two ":"-prefixed attribute lines
# whose values render as real visible prose (the page's <meta description>
# and <title>) rather than structural directives -- every _is_skip_line
# caller that walks prose (translation, terminology, homoglyphs, file-path
# italics) needs to carve these two out and check their values instead of
# skipping the line outright.
_PROSE_ATTR_RE = re.compile(r'^:(description|page-htmltitle):\s*(.*)$')


def _strip_noise(line: str) -> str:
    s = _STRIP_CODE_SPAN_RE.sub("", line)
    s = _STRIP_DOUBLE_ANGLE_RE.sub("", s)
    s = _STRIP_BRACKET_RE.sub("", s)
    s = _STRIP_PAREN_RE.sub("", s)
    s = _STRIP_BOLD_RE.sub("", s)
    s = _STRIP_BOLDITALIC_RE.sub("", s)
    s = _STRIP_ITALIC_RE.sub("", s)
    s = _STRIP_XREF_RE.sub("", s)
    s = _STRIP_URL_RE.sub("", s)
    return s


def _check_translation_pair(en_file: Path, ru_file: Path, verbose: bool, report_header):
    """Returns the number of UNTRANSLATED/SUSPECT lines flagged."""
    en_lines = _read_lines(en_file)
    ru_lines = _read_lines(ru_file)
    if en_lines is None or ru_lines is None:
        return 0
    n = min(len(en_lines), len(ru_lines))

    in_code = None
    in_comment_block = False
    in_cell = False
    header_printed = False
    finding_count = 0

    def ensure_header():
        nonlocal header_printed
        if not header_printed:
            report_header(ru_file)
            header_printed = True

    for i in range(n):
        en_line = en_lines[i]
        ru_line = ru_lines[i]
        lineno = i + 1

        if re.match(r'^////\s*$', en_line):
            in_comment_block = not in_comment_block
            continue
        if in_comment_block:
            continue

        delim = _code_delim_type(en_line)
        if delim:
            if in_code == delim:
                in_code = None
            elif in_code is None:
                in_code = delim
            continue
        if in_code:
            continue

        if en_line.startswith("|==="):
            in_cell = False
        elif re.match(r'^(\.\d+\+)?a\|', en_line):
            in_cell = True
        elif _SKIP_TABLE_CELL_RE.match(en_line):
            in_cell = False
        elif in_cell:
            continue

        attr_m = _PROSE_ATTR_RE.match(en_line)
        if attr_m:
            en_text = attr_m.group(2)
            ru_attr_m = _PROSE_ATTR_RE.match(ru_line)
            ru_text = ru_attr_m.group(2) if ru_attr_m else ru_line
        elif _is_skip_line(en_line):
            continue
        else:
            en_text = en_line
            ru_text = ru_line

        if len(en_text.split()) < 3:
            continue

        if en_text == ru_text:
            ensure_header()
            finding_count += 1
            print(f"  UNTRANSLATED  {ru_file}:{lineno}: {en_text}")
        elif not _HEADING_RE.match(en_text):
            candidate = _strip_noise(ru_text).lower()
            candidate = _HYPHEN_JOIN_RE.sub(r'\1\2', candidate)
            hits = _STOPWORDS_RE.findall(candidate)
            if hits:
                ensure_header()
                finding_count += 1
                marker = f" [{', '.join(sorted(set(hits)))}]" if verbose else ""
                print(f"  SUSPECT       {ru_file}:{lineno}: {ru_text}{marker}")

    return finding_count


def check_pages_translation(verbose=False) -> bool:
    """Port of check_pages_translation.sh. Flags RU lines byte-identical to EN
    (UNTRANSLATED) and RU lines carrying English stopwords (SUSPECT); --verbose
    appends the matched stopword(s) to each SUSPECT line."""
    ok = True
    total_hits = 0

    def report_header(ru_file):
        nonlocal ok
        ok = False
        print(f"FILE     {ru_file}")

    for _, en_root, ru_root in module_roots():
        for subdir in ("pages", "partials"):
            for en_file in _iter_files(en_root / subdir, ".adoc"):
                if not _page_allowed(en_file):
                    continue
                rel = en_file.relative_to(en_root)
                ru_file = ru_root / rel
                if not ru_file.is_file():
                    continue
                total_hits += _check_translation_pair(en_file, ru_file, verbose, report_header)

    if ok:
        print("OK: no untranslated lines detected.")
    else:
        print(f"\nTotal: {total_hits} untranslated/suspect line(s).")
    return ok


# --------------------------------------------------------------------------
# PAGES: un-italicized file/directory names
# --------------------------------------------------------------------------

# Deliberately narrow: config/unit-file-style extensions that are almost
# never anything other than a literal filename in admin-facing prose --
# unlike short generic ones (.io, .sh, .py), which collide too easily with
# abbreviations, URLs, and version numbers to be a reliable signal.
# jar/war/rpm/deb/tar/gz/tgz/whl/zip added later: this doc set is heavily
# about package-based installation, and these package/archive formats
# turned up real (some already-italicized, some not) matches across all
# four repos tested (e.g. "plcontainer-*.tar.gz", "postgresql-42.7.10.jar",
# "wiki.deb") with the same low collision risk as the original list --
# "tar.gz" doesn't need special-casing: "tar" alone is in the list, so
# "archive.tar.gz" already matches on "...gz6.tar" (stopping at the "tar"
# segment, not continuing through ".gz") -- a good enough anchor for
# --verbose to show the full line, without needing a two-extension pattern.
# xml added later: real Hadoop config files (hive-site.xml, hdfs-site.xml,
# core-site.xml, ...) turned up ~13 times in docs-adh, almost all already
# italicized correctly, with one confirmed miss in backticks
# (release-notes.adoc: "manage granular parameters (`hdfs-site.xml`,
# `core-site.xml`, `hive-site.xml`)") -- same low collision risk as
# json/yaml already in the list.
# h/keytab added later: postgres.h/fmgr.h/elog.h/palloc.h (C headers, a
# PostgreSQL extension-dev guide in docs-adpg) and krb5.keytab (Kerberos,
# confirmed consistently italicized in both docs-adpg and docs-adb) are
# both always "word.ext" shaped, so the same word-boundary requirement
# that keeps the rest of this list safe applies here too.
_ITALIC_FILE_EXT_RE = re.compile(
    r'\b[\w][\w-]*\.(?:conf|ya?ml|cfg|ini|toml|json|xml|service|socket|log|env|pem|crt|key|properties'
    r'|jar|war|rpm|deb|tar|gz|tgz|whl|zip|h|keytab)\b'
)
# Absolute paths under directories that are essentially always literal
# filesystem paths in this kind of doc, never prose or URLs.
_ITALIC_DIR_PATH_RE = re.compile(
    r'(?:/etc|/var|/opt|/usr/local|/usr/share|/home)/[\w./-]*[\w/-]'
)

_MASK_BOLDITALIC_RE = re.compile(r'\*_[^*_]*_\*')
# Same word-adjacency reasoning as italics below: a filename mentioned
# inside bold text doesn't also need italics per this doc set's style
# guide (bold already marks it as "not plain prose"), so bold spans are
# exempt the same way code spans and italics are.
_MASK_BOLD_RE = re.compile(r'(?<!\w)\*(?!\s)(?:.+?)(?<!\s)\*(?!\w)')
_MASK_CODE_SPAN_RE = re.compile(r'`[^`]*`')
# Asciidoctor only recognizes _..._ as (constrained) italic when neither
# underscore is adjacent to a word character -- otherwise it's just a
# literal underscore inside an identifier (pg_hba, connection_type,
# COORDINATOR_DATA_DIRECTORY, all over this doc set). The content itself
# can still contain a literal underscore (e.g. "_pg_hba.conf_" is a single
# genuine italic span), so only the delimiters' word-adjacency is
# restricted, not the content charset -- a naive `_[^_]*_` would instead
# pair up literal underscores across completely unrelated words and
# corrupt the rest of the line's masking.
_MASK_ITALIC_RE = re.compile(r'(?<!\w)_(?!\s)(?:.+?)(?<!\s)_(?!\w)')
# Any AsciiDoc macro of the general "name:target[attrs]" (inline) or
# "name::target[attrs]" (block) shape -- xref:, link:, image:/image::,
# kbd:, btn:, pass:, footnote:, and anything else sharing this syntax --
# rather than enumerating macro names one at a time: a file/path mention
# inside a macro's target or attribute list (e.g. an image's alt text) is
# not plain prose. The bracket suffix is mandatory in AsciiDoc macro syntax,
# so greedy \S* backtracks correctly to find it (unlike a bare URL, where
# the trailing brackets are optional -- see _MASK_URL_RE).
_MASK_MACRO_RE = re.compile(r'\b[a-zA-Z][a-zA-Z0-9]*:{1,2}\S*\[[^\]]*\]')
_MASK_DOUBLE_ANGLE_RE = re.compile(r'<<[^>]*>>')
_MASK_URL_RE = re.compile(r'https?://[^\s\[]*(?:\[[^\]]*\])?')
# A run of 2+ ALL-CAPS words (e.g. "ALTER RESOURCE QUEUE") is this doc
# set's plain-text convention for a literal SQL command/object name kept
# untranslated by house style -- same "content is meant literally"
# signal ALL_CAPS_TERM_RE reads elsewhere in this file. It shows up
# unmarked-up (no code span) in :page-htmltitle:/:description: values,
# e.g. "Overview of the ALTER RESOURCE QUEUE SQL command", where a
# glossary term whose words happen to compose part of the command name
# (e.g. "resource queue" inside "ALTER RESOURCE QUEUE") would otherwise
# be flagged for not having its RU translation, even though the command
# name itself is correctly left in English on the RU side too.
_MASK_ALLCAPS_RUN_RE = re.compile(r'\b[A-Z][A-Z0-9]*(?:\s+[A-Z][A-Z0-9]*)+\b')

# Trailing noun for the "a/the X <noun>" family of checks below. Extended
# beyond file/folder/directory to script/archive: real usage across all
# four repos backs both ("_bin/yarn_ script", "_.har_ archive", "_tar.gz_
# archives" are the norm, with the `spark3-submit` script in docs-adh
# looking like a genuine, real miss rather than a different convention).
# "package" was tried too and dropped: unlike files/scripts/archives, an
# OS package *name* (`oidentd`, `tzdata`, `libpam-ldapd`) is consistently
# kept in backticks throughout this doc set, never italicized -- the same
# "different category, stays in code spans" pattern as systemd unit names
# and config parameter names excluded elsewhere in this check. A package
# *file*, when one is actually meant (e.g. "the _.deb_ package"), is still
# caught by the dotfile/extension checks, which are unaffected by this.
_FILE_NOUN_GROUP = r'(?:files?|folders?|directories|directory|scripts?|archives?)'

def _marked_alt(word_pattern: str) -> str:
    """The "*word*|`word`|_word_" alternation (bold, code span, italic, in
    that group order) shared by every marked-word regex below -- factored
    out since it's identical in all of them; callers differ only in what
    they wrap it with (a grammar-gated bare group, an ungated one, or none
    at all), which is why this only covers the marked forms."""
    return (
            r'\*(' + word_pattern + r')\*'
                                    r'|`(' + word_pattern + r')`'
                                                            r'|_(' + word_pattern + r')_'
    )


# Bare (no extension, no leading "/") directory/file basenames that are
# essentially never generic English words in this grammatical slot --
# unlike e.g. "log"/"data"/"config"/"cache"/"share"/"run", which are
# extremely common as generic descriptors ("a log file", "a data file")
# that don't refer to a literal directory of that name, these are deliberately
# excluded to keep the false-positive rate low. Found via a real "a *bin*
# folder" case in install_from_package.adoc-adjacent content, wrapped in
# bold instead of italics.
_BARE_DIR_BASENAMES = {"bin", "sbin", "etc", "lib", "tmp", "var", "opt", "src"}
_BARE_DIR_BASENAME_ALT = '|'.join(_BARE_DIR_BASENAMES)
# Unlike the extension/path checks, this one needs to see the CURRENT
# formatting around the word directly -- bold or a code span here is
# itself the wrong-formatting finding, not something to treat as already
# exempt the way it is for the rest of this check (a filename that's
# merely *also* bold elsewhere is fine; a directory basename that's
# *only* ever bold/code-spanned, never italicized, is not).
#
# Requires the "a/the X file/folder" construction: the grammar is what
# disambiguates a couple of these words from unrelated generic terms (a
# JS "var" declaration, an HTML `src` attribute, a `cp src dst` argument
# placeholder -- all real, confirmed via synthetic test cases like
# "Declare a `var` in JavaScript" and "Set the `src` attribute").
_BARE_NAME_MENTION_RE = re.compile(
    r'\b(?:a|an|the)\s+(?:' + _marked_alt(_BARE_DIR_BASENAME_ALT) + r'|(' + _BARE_DIR_BASENAME_ALT + r'))\s+'
    + _FILE_NOUN_GROUP + r'\b'
)
# Subset of the above that's safe to flag on bold/code-span alone, with no
# surrounding grammar required at all: these are essentially never generic
# English/programming terms in ANY context in this doc domain (unlike the
# excluded var/src/lib, see _BARE_NAME_MENTION_RE), so a deliberately
# emphasized mention anywhere is already a strong enough signal -- e.g.
# "(for example, `bin`)" referring back to an earlier "shell startup file"
# mention has no "a/the X folder" phrase in sight, but is the same intent
# as the italicized `.bashrc` case this whole check started from.
_BARE_DIR_BASENAMES_UNAMBIGUOUS = {"bin", "sbin", "etc", "tmp", "opt"}
_UNAMBIGUOUS_BASENAME_ALT = '|'.join(_BARE_DIR_BASENAMES_UNAMBIGUOUS)
_UNAMBIGUOUS_BASENAME_MARKED_RE = re.compile(_marked_alt(_UNAMBIGUOUS_BASENAME_ALT))

# Generalization of the whitelist above to any word, not just curated
# ones: a word with an internal underscore or slash (not leading/
# trailing) in the "a/the X file/folder" slot is very likely a real
# filename/relative path -- English essentially never uses a mid-word
# underscore or slash outside a technical identifier or path (unlike
# bare dictionary words), and a relative path with no leading "/" (e.g.
# "backup/adb") isn't covered by the absolute _ITALIC_DIR_PATH_RE check
# above. Hyphen was tried too and dropped: real hits on docs-adh turned
# out to be ordinary English compound adjectives ("a global-level file",
# "a zero-length file", "the first-level directory"), not filenames --
# underscore/slash alone had zero false positives across all four repos
# tested (the one common English "/" idiom, "and/or", doesn't realistically
# combine with "file/folder" as its object).
_COMPOUND_WORD = r'[A-Za-z0-9]+(?:[_/][A-Za-z0-9]+)+'
_COMPOUND_NAME_MENTION_RE = re.compile(
    r'\b(?:a|an|the)\s+(?:' + _marked_alt(_COMPOUND_WORD) + r'|(' + _COMPOUND_WORD + r'))\s+'
    + _FILE_NOUN_GROUP + r'\b'
)

# Word-content-agnostic: ANY word in a code span in the "a/the X
# file/folder" slot, whitelisted or not (e.g. "the `backup` folder") --
# the code-span formatting itself is the signal here, not the word.
# Deliberately restricted to code spans, not bold: a code span is
# essentially never used for plain prose emphasis in AsciiDoc (it's
# reserved for literal technical tokens), so one here is a strong signal
# the author meant a literal name, whereas bold commonly *is* used for
# ordinary emphasis on a descriptive adjective ("the *most important*
# file") -- bold stays limited to the curated/underscore checks above,
# which have their own evidence backing them, rather than every bold word
# in this slot.
#
# A camelCase match (an uppercase letter after the first character, e.g.
# `dataLogDir`/`dataDir`) is excluded in code: real Unix file/directory
# names in this doc set are essentially always lowercase, so camelCase is
# a strong signal of a config *parameter* name instead (confirmed via
# zookeeper/configure.adoc: "set up a dedicated disk for the `dataLogDir`
# directory" -- the same sentence separately calls it "the `dataLogDir`
# parameter", not a literal directory name -- same category as the
# `krb5.conf`-as-parameter case already excluded from the extension check).
_MARKED_WORD_BEFORE_FILE_RE = re.compile(
    r'\b(?:a|an|the)\s+`([\w./-]+)`\s+' + _FILE_NOUN_GROUP + r'\b'
)
# A captured word that is itself one of the trailing nouns (e.g. "the
# `directory` archive format") is a format/type descriptor, not a literal
# name -- pg_dump's own format options are named "custom"/"directory"/
# "tar"/"plain", and "the `directory` archive format" means "the archive
# format called directory", not a real directory (confirmed via
# docs-adpg's sql-dump.adoc: "Parallel dumps are only supported for the
# `directory` archive format", right after "the `custom` dump format" in
# the same paragraph -- same category as the package-name/parameter-name
# exclusions elsewhere in this check).
_GENERIC_NOUN_WORDS = {"file", "files", "folder", "folders", "directory", "directories", "script", "scripts", "archive", "archives"}

# Common shell/tool dotfiles: unlike the extension whitelist above (which
# matches "name.ext"), these are "." + name with nothing before the dot, so
# they need their own pattern. Also checked directly like the bare-basename
# check above rather than pre-exempting bold/code -- confirmed via a real
# "(for example, `.bashrc`)" case in install_from_package.adoc, inconsistent
# with every other .bashrc mention in this doc set, which is correctly
# italicized. Unlike extension matches such as `.service`/`.timer`, which
# turned up legitimate backtick use elsewhere as systemd unit names/config
# identifiers (not literal files to open), a dotfile mention is unambiguous.
#
# Deliberately a hardcoded list, not a generalized "a/the .<name>
# file/directory" pattern: tested that generalization empirically (zero
# hits, zero false positives, across all four repos), but a fully generic
# version would also match generic file-*type* mentions like "save it as
# a `.csv` file", which don't need italics (describing a format, same as
# "a JSON file" wouldn't) -- the same generic-vs-literal confusion that
# already produced false positives in the bare-basename and parameter-name
# checks above. A whitelist just needs an occasional one-line addition
# (as new real cases turn up) instead of carrying that silent risk.
_ITALIC_DOTFILES = ("bashrc", "bash_profile", "bash_login", "profile", "zshrc",
                    "vimrc", "gitconfig", "psqlrc", "pgpass", "npmrc", "editorconfig", "gitignore", "env",
                    "dockerignore", "eslintrc", "pylintrc", "htaccess", "htpasswd", "claude", "idea")
_DOTFILE_CORE = r'\.(?:' + '|'.join(_ITALIC_DOTFILES) + r')'
_DOTFILE_MENTION_RE = re.compile(_marked_alt(_DOTFILE_CORE) + r'|(?<!\w)(' + _DOTFILE_CORE + r')\b')


def _mask_non_prose_spans(line: str) -> str:
    """Like _mask_formatted_spans, but leaves bold/code/italic markers
    alone -- used ahead of _BARE_NAME_MENTION_RE, which needs to inspect
    those directly rather than have them pre-blanked out as "already
    fine"."""
    s = _MASK_MACRO_RE.sub(lambda m: ' ' * len(m.group(0)), line)
    s = _MASK_DOUBLE_ANGLE_RE.sub(lambda m: ' ' * len(m.group(0)), s)
    s = _MASK_URL_RE.sub(lambda m: ' ' * len(m.group(0)), s)
    return s


def _mask_formatted_spans(line: str) -> str:
    """Blank out already-formatted or non-prose spans (code spans, bold,
    italics, bold-italics, AsciiDoc macros -- xref/link/image/etc., plus the
    `<<anchor,text>>` xref shorthand -- and bare URLs) with same-length
    whitespace, so a path/filename search afterwards only sees text still
    in plain, unformatted prose."""
    s = _MASK_BOLDITALIC_RE.sub(lambda m: ' ' * len(m.group(0)), line)
    s = _MASK_BOLD_RE.sub(lambda m: ' ' * len(m.group(0)), s)
    s = _MASK_CODE_SPAN_RE.sub(lambda m: ' ' * len(m.group(0)), s)
    s = _MASK_ITALIC_RE.sub(lambda m: ' ' * len(m.group(0)), s)
    s = _MASK_MACRO_RE.sub(lambda m: ' ' * len(m.group(0)), s)
    s = _MASK_DOUBLE_ANGLE_RE.sub(lambda m: ' ' * len(m.group(0)), s)
    s = _MASK_URL_RE.sub(lambda m: ' ' * len(m.group(0)), s)
    return s


def _iter_prose_lines(lines):
    """Yield (lineno, line) for plain body-prose lines only -- skips
    delimited code/literal blocks, ////-comment blocks, whole tables (a
    plain "|"-cell's content can span several blank-line-separated
    paragraphs with no per-line "|" marker, so cell-by-cell tracking is
    unreliable -- the whole table is excluded instead), attribute lines,
    headings, and block titles/captions (none of these are italicized by
    convention, whatever they mention). Mirrors the skip logic in
    _check_translation_pair, kept separate to avoid touching that check's
    behavior."""
    in_code = None
    in_comment_block = False
    in_table = False
    for i, line in enumerate(lines, 1):
        if re.match(r'^////\s*$', line):
            in_comment_block = not in_comment_block
            continue
        if in_comment_block:
            continue
        if line.strip() == "|===":
            in_table = not in_table
            continue
        if in_table:
            continue
        delim = _code_delim_type(line)
        if delim:
            if in_code == delim:
                in_code = None
            elif in_code is None:
                in_code = delim
            continue
        if in_code:
            continue
        if _HEADING_RE.match(line):
            continue
        if _STRUCT_BLOCKTITLE_RE.match(line):
            continue
        if _is_skip_line(line):
            continue
        yield i, line


def _collect_marked_hits(pattern, text, matches):
    """Run one of the "bold|code|italic[|bare]" marked-word regexes built
    with _marked_alt (optionally plus a trailing bare-word group) against
    `text`, appending "word (kind, should be italic)" to `matches` for
    every non-italic hit. Shared by the four regex-based checks in
    check_pages_file_path_italics that all follow this same shape --
    _MARKED_WORD_BEFORE_FILE_RE is the odd one out (single group, always a
    code span) and stays inline."""
    for m in pattern.finditer(text):
        groups = m.groups()
        bold_w, code_w, italic_w = groups[0], groups[1], groups[2]
        bare_w = groups[3] if len(groups) > 3 else None
        if italic_w:
            continue  # already correctly italicized
        word = bold_w or code_w or bare_w
        if not word:
            continue
        kind = "bold" if bold_w else ("code span" if code_w else "unformatted")
        matches.append(f"{word} ({kind}, should be italic)")


def check_pages_file_path_italics(verbose=False) -> bool:
    """New check (not a port of an existing shell script): flags file names
    (by a curated config/unit-file extension whitelist), directory paths
    (by well-known absolute-path prefixes), bare directory/file basenames
    (see _BARE_DIR_BASENAMES), and common dotfiles (see _ITALIC_DOTFILES)
    mentioned in plain prose without the italics (`_..._`) this doc set's
    style guide requires for them. Deliberately narrow and heuristic -- see
    the regexes above for scope and reasoning."""
    ok = True
    total_hits = 0
    for _, en_root, ru_root in module_roots():
        for root in (en_root, ru_root):
            for f in list(_iter_files(root / "pages", ".adoc")) + list(_iter_files(root / "partials", ".adoc")):
                if not _page_allowed(f):
                    continue
                lines = _read_lines(f)
                if lines is None:
                    continue
                hits = []
                for i, line in _iter_prose_lines(lines):
                    masked = _mask_formatted_spans(line)
                    matches = sorted(set(_ITALIC_FILE_EXT_RE.findall(masked) + _ITALIC_DIR_PATH_RE.findall(masked)))

                    lightly_masked = _mask_non_prose_spans(line)
                    _collect_marked_hits(_BARE_NAME_MENTION_RE, lightly_masked, matches)
                    _collect_marked_hits(_UNAMBIGUOUS_BASENAME_MARKED_RE, lightly_masked, matches)
                    _collect_marked_hits(_COMPOUND_NAME_MENTION_RE, lightly_masked, matches)
                    for m in _MARKED_WORD_BEFORE_FILE_RE.finditer(lightly_masked):
                        word = m.group(1)
                        if word.lower() in _GENERIC_NOUN_WORDS:
                            continue  # format/type descriptor, not a literal name
                        if not any(c.isupper() for c in word[1:]):  # skip camelCase parameter names
                            matches.append(f"{word} (code span, should be italic)")
                    _collect_marked_hits(_DOTFILE_MENTION_RE, lightly_masked, matches)

                    matches = sorted(set(matches))
                    if matches:
                        hits.append((i, line, matches))
                if hits:
                    ok = False
                    total_hits += len(hits)
                    print(f"FILE     {f}")
                    for i, line, matches in hits:
                        print(f"  {f}:{i}: {', '.join(matches)}")
                        if verbose:
                            print(f"    {line}")
    if ok:
        print("OK: no un-italicized file/directory names found in pages.")
    else:
        print(f"\nTotal: {total_hits} line(s) with un-italicized file/directory names.")
    return ok


# --------------------------------------------------------------------------
# PAGES: no trailing period on a table cell's last sentence
# --------------------------------------------------------------------------

_ADMONITION_LABEL_RE = re.compile(r'^(?:NOTE|TIP|WARNING|IMPORTANT|CAUTION):\s')
_A_CELL_START_RE = re.compile(r'^(\.\d+\+)?a\|')
# Broader than the shared _SKIP_TABLE_CELL_RE: also recognizes cell-format/
# alignment prefixes (^, <, >, ~, rowspan/colspan like "2.3+", combined
# forms like "^.^") seen on header cells (e.g. "^|Pros" in adb-to-adb/
# overview.adoc) that the shared regex doesn't match, so cell boundaries
# here don't go undetected just because a cell has an alignment prefix.
_TABLE_CELL_START_RE = re.compile(r'^(?:[.\d+<>^~a-z]{0,6})\|')


def _is_abbreviation_like(content: str) -> bool:
    """A single space-free token ending in a period (`Мин.`, `e.g.`, `etc.`)
    reads as an abbreviation, not a full sentence -- the period there is
    part of the abbreviation, not the sentence-final punctuation the style
    rule is actually about (found via the `Мин.`/`Макс.` column headers in
    data_encryption.adoc)."""
    return content.endswith('.') and ' ' not in content.rstrip('.')


# A full, otherwise-compliant sentence ending in one of these keeps its
# period even at the end of a cell, since the period belongs to the
# trailing abbreviation, not the sentence (e.g. "...the Global Deadlock
# Detector process, etc." in db-schemas/db.adoc -- other rows in the same
# table correctly drop the period, only this one doesn't, because it ends
# in "etc."). Russian abbreviates the same way sometimes ("и т.д.") but not
# always (the RU counterpart of that same row spells it out as "и так
# далее" instead, with no trailing period at all).
_TRAILING_ABBREVS = ("etc.", "e.g.", "i.e.", "и т.д.", "т.д.", "и т.п.", "т.п.", "и др.")


def _ends_with_known_abbreviation(content: str) -> bool:
    lowered = content.rstrip().lower()
    return any(lowered.endswith(ab) for ab in _TRAILING_ABBREVS)


def _is_period_violation(content: str) -> bool:
    return (content.endswith('.')
            and not _is_abbreviation_like(content)
            and not _ends_with_known_abbreviation(content))


def check_pages_table_cell_periods(verbose=False) -> bool:
    """New check (not a port of an existing shell script): house style says
    the last sentence in a table cell should not end with a period.
    Exceptions found by inspecting real tables in this doc set:

    - a cell containing a list (its last line is normal list-item prose and
      keeps its period, e.g. table_compression.adoc's `compresslevel` cells);
    - a cell containing a NOTE/TIP/WARNING/IMPORTANT/CAUTION admonition
      (either the `LABEL: text` one-liner or a `[LABEL]`/`====` block),
      since that marks the content as not just a plain descriptive sentence;
    - a single space-free abbreviation like `Мин.`/`Макс.` (see
      _is_abbreviation_like).

    Deliberately heuristic: cells are tracked by lookahead, but a blank line
    is *never* by itself a cell boundary -- both `a|` cells (rebalance_status.
    adoc) and even plain `|` cells (fs-commands/setfacl.adoc) can hold several
    blank-line-separated paragraphs, so a line only counts as the last line
    of its cell if the next *non-blank* line is `|===` or itself starts a
    new cell. A single physical line can also pack multiple `|`-separated
    plain cells (a compact header row like `|Algorithm |Default |Min |Max`);
    only a bare `|` cell (not `a|`/`m|`/etc., which always occupy the rest
    of their line) is split this way."""
    ok = True
    total_hits = 0
    for _, en_root, ru_root in module_roots():
        for root in (en_root, ru_root):
            for f in list(_iter_files(root / "pages", ".adoc")) + list(_iter_files(root / "partials", ".adoc")):
                if not _page_allowed(f):
                    continue
                lines = _read_lines(f)
                if lines is None:
                    continue
                hits = []
                in_table = False
                in_admonition_block = False
                cell_exempt = False
                n = len(lines)
                for i, line in enumerate(lines):
                    stripped = line.strip()
                    if stripped == "|===":
                        in_table = not in_table
                        in_admonition_block = False
                        cell_exempt = False
                        continue
                    if not in_table:
                        continue

                    is_cell_start = _TABLE_CELL_START_RE.match(line) is not None
                    if is_cell_start:
                        in_admonition_block = False
                        cell_exempt = False

                    if stripped == "" or stripped.startswith("//"):
                        continue

                    if stripped == "====":
                        in_admonition_block = not in_admonition_block
                        if in_admonition_block:
                            cell_exempt = True
                        continue
                    if in_admonition_block:
                        continue

                    if _ADMONITION_LABEL_RE.match(stripped) or _STRUCT_LIST_MARKER_RE.match(stripped):
                        cell_exempt = True

                    j = i + 1
                    while j < n and (lines[j].strip() == "" or lines[j].strip().startswith("//")):
                        j += 1
                    next_line = lines[j] if j < n else "|==="
                    is_last_of_cell = (
                            next_line.strip() == "|==="
                            or _TABLE_CELL_START_RE.match(next_line) is not None
                    )
                    if not is_last_of_cell or cell_exempt:
                        continue

                    if is_cell_start and "|" in line[1:] and not _A_CELL_START_RE.match(line):
                        segments = line.split("|")[1:]  # leading "|" -> segments[0] is the first cell
                        for seg in segments[:-1]:
                            content = seg.strip()
                            if _is_period_violation(content):
                                hits.append((i + 1, content))
                        content = segments[-1].strip()
                    else:
                        content = stripped
                        if is_cell_start:
                            m = _TABLE_CELL_START_RE.match(line)
                            content = line[m.end():].strip()

                    if _is_period_violation(content):
                        hits.append((i + 1, line if not is_cell_start else content))

                if hits:
                    ok = False
                    total_hits += len(hits)
                    print(f"FILE     {f}")
                    for lineno, line in hits:
                        print(f"  {f}:{lineno}: {line.strip() if isinstance(line, str) else line}")

    if ok:
        print("OK: no table cell ends its last sentence with a period.")
    else:
        print(f"\nTotal: {total_hits} table cell(s) ending with a period.")
    return ok


# --------------------------------------------------------------------------
# PAGES: glossary terminology consistency
# --------------------------------------------------------------------------

def _glossary_entry_ru_count(entry, ru_line: str) -> int:
    """How many distinct translated mentions of the term `ru_line` can be
    credited with, using the entry's best-fitting accepted pattern. For one
    pattern that's the least frequent of its required stem regexes (see
    _compile_glossary_pattern) -- a two-token pattern like "ресурсн<>
    очеред<>" is credited once per (ресурсн, очеред) pair the line can
    form, i.e. min of the two counts; across the entry's alternative
    patterns the best (max) such count wins. 0 means no pattern matched at
    all. Each pattern comes straight from a glossary row's ru_pattern
    column, so there's no guessing here: a stem token's boundary was chosen
    by whoever authored the glossary, not inferred at match time."""
    best = 0
    for pattern in entry["patterns"]:
        counts = [len(regex.findall(ru_line)) for regex in pattern]
        if counts:
            best = max(best, min(counts))
    return best


def _glossary_entry_satisfied(entry, ru_line: str) -> bool:
    """True if `ru_line` matches at least one of the entry's accepted
    patterns (see _glossary_entry_ru_count)."""
    return _glossary_entry_ru_count(entry, ru_line) > 0


def _build_glossary_term_re(glossary):
    """One alternation of every glossary key, longest-first so a multi-word
    key (e.g. "master host") wins over a shorter key that's one of its words
    (e.g. "host") when both would otherwise match at the same position --
    same leftmost-longest ordering trick check_pages_file_path_italics's
    regexes use."""
    if not glossary:
        return None
    keys = sorted(glossary, key=len, reverse=True)
    return re.compile(r'\b(?:' + '|'.join(re.escape(k) for k in keys) + r')\b', re.IGNORECASE)


def _check_terminology_pair(en_file: Path, ru_file: Path, term_re, glossary, verbose, report_header):
    """Returns the number of glossary mismatches flagged. Walks EN/RU by
    line index -- same positional alignment _check_translation_pair uses --
    so it carries the same known limitation: if the two files have drifted
    out of line-parity, a matched EN line can end up checked against the
    wrong RU line.

    A term is flagged when its accepted RU translation turns up on the
    aligned line fewer times than the EN term itself does: 0 of any is the
    plain "wrong/missing translation" case, and N-of-fewer-than-N catches
    an EN line that repeats a term (or packs several different glossary
    terms in) where the RU side only translated some of the mentions.
    Because Russian routinely avoids repeating a noun (a pronoun, ellipsis,
    or "тот же" stands in), the repeat case can misfire -- it stays on the
    same "beta, review list" footing as the rest of this check."""
    en_lines = _read_lines(en_file)
    ru_lines = _read_lines(ru_file)
    if en_lines is None or ru_lines is None:
        return 0
    n = min(len(en_lines), len(ru_lines))

    in_code = None
    in_comment_block = False
    in_cell = False
    header_printed = False
    finding_count = 0

    def ensure_header():
        nonlocal header_printed
        if not header_printed:
            report_header(ru_file)
            header_printed = True

    for i in range(n):
        en_line = en_lines[i]
        ru_line = ru_lines[i]
        lineno = i + 1

        if re.match(r'^////\s*$', en_line):
            in_comment_block = not in_comment_block
            continue
        if in_comment_block:
            continue

        delim = _code_delim_type(en_line)
        if delim:
            if in_code == delim:
                in_code = None
            elif in_code is None:
                in_code = delim
            continue
        if in_code:
            continue

        if en_line.startswith("|==="):
            in_cell = False
        elif re.match(r'^(\.\d+\+)?a\|', en_line):
            in_cell = True
        elif _SKIP_TABLE_CELL_RE.match(en_line):
            in_cell = False
        elif in_cell:
            continue

        attr_m = _PROSE_ATTR_RE.match(en_line)
        if attr_m:
            en_text = attr_m.group(2)
            ru_attr_m = _PROSE_ATTR_RE.match(ru_line)
            ru_text = ru_attr_m.group(2) if ru_attr_m else ru_line
        elif _is_skip_line(en_line):
            continue
        else:
            en_text = en_line
            ru_text = ru_line

        masked_en = _mask_code_and_links(en_text)
        masked_en = _MASK_ALLCAPS_RUN_RE.sub(lambda m: ' ' * len(m.group(0)), masked_en)
        en_counts = Counter(m.group(0).lower() for m in term_re.finditer(masked_en))
        if not en_counts:
            continue

        for key in sorted(en_counts):
            entry = glossary[key]
            en_count = en_counts[key]
            ru_count = _glossary_entry_ru_count(entry, ru_text)
            if ru_count >= en_count:
                continue
            ensure_header()
            finding_count += 1
            forms = ", ".join(f"'{f}'" for f in sorted(entry["ru_display"]))
            if ru_count == 0:
                print(f"  MISMATCH  {ru_file}:{lineno}: term '{key}' -- expected one of [{forms}], not found")
            else:
                print(f"  MISMATCH  {ru_file}:{lineno}: term '{key}' -- appears {en_count}x on the EN line "
                      f"but a translation from [{forms}] is present only {ru_count}x")
            if verbose:
                print(f"    EN: {en_text}")
                print(f"    RU: {ru_text}")

    return finding_count


def check_pages_terminology(verbose=False) -> bool:
    """New check (not a port of an existing shell script): flags an EN
    glossary term (see --glossary) whose aligned RU line contains its
    accepted RU translation fewer times than the EN term appears, catching a
    translator drifting onto an inconsistent or outdated Russian term for
    something the glossary already has a house-style answer for -- including
    the case where an EN line uses the term (or several glossary terms) more
    than once and only some mentions were translated correctly.

    An EN term is located via a longest-first regex alternation over every
    glossary key (see _build_glossary_term_re). Whether the RU line "has the
    right translation" is then decided entirely by the glossary author's own
    ru_pattern column, not a guessed heuristic: each pattern is a set of
    word tokens (see _compile_glossary_pattern), a `word<>` one matching
    that stem plus any suffix (declension/conjugation-tolerant) and a bare
    `word` one requiring that exact word -- e.g. a do-not-translate entry's
    pattern is just the EN term's own words, all bare, so it's effectively
    required verbatim. All tokens in a pattern must be found somewhere in
    the RU line (any order) for that pattern to count as a match; an entry
    is credited once per full set of its pattern tokens the line can form
    (see _glossary_entry_ru_count), and that count is compared against how
    many times the EN term occurs on the aligned line. This still can't
    tell "right words in an unrelated sentence" from a real match, and the
    repeat comparison additionally trips on Russian's habit of not
    repeating a noun it already named, so it's deliberately biased toward
    fewer false positives at the cost of some missed drift -- same "beta,
    review list" tradeoff as this file's other heuristic checks.

    Two glossary rows sharing an EN key (e.g. the two "session" senses) are
    merged by _load_glossary into one set of alternative patterns, so either
    translation counts as correct -- a bare EN term match can't tell the
    senses apart, so this deliberately doesn't try.

    Requires --glossary PATH (repeatable) -- or, if omitted, at least one
    *-glossary.psv file discoverable in the current directory (see
    _discover_default_glossaries, wired up in main()); exits with an error
    if neither is available, since that's a misconfiguration, not "nothing
    to check"."""
    if not GLOSSARY:
        sys.exit("error: --check-pages-terminology requires --glossary PATH "
                  "(no *-glossary.psv found in the current directory to default to either)")

    term_re = _build_glossary_term_re(GLOSSARY)
    ok = True
    total_hits = 0

    def report_header(ru_file):
        nonlocal ok
        ok = False
        print(f"FILE     {ru_file}")

    for _, en_root, ru_root in module_roots():
        for subdir in ("pages", "partials"):
            for en_file in _iter_files(en_root / subdir, ".adoc"):
                if not _page_allowed(en_file):
                    continue
                rel = en_file.relative_to(en_root)
                ru_file = ru_root / rel
                if not ru_file.is_file():
                    continue
                total_hits += _check_terminology_pair(en_file, ru_file, term_re, GLOSSARY, verbose, report_header)

    if ok:
        print("OK: no glossary terminology mismatches found.")
    else:
        print(f"\nTotal: {total_hits} terminology mismatch(es).")
    return ok


# --------------------------------------------------------------------------
# PAGES: Latin/Cyrillic homoglyph mix-ups in ru/ prose
# --------------------------------------------------------------------------

_LATIN_LETTER_RE = re.compile(r'[A-Za-z]')
_HOMOGLYPH_WORD_TOKEN_RE = re.compile(r'[A-Za-zЀ-ӿ]+')
# The four Cyrillic/Latin lowercase pairs that are near-perfect visual
# homoglyphs AND happen to double as real one-letter Russian words
# (а "and/but", о "about", с "with", у "at/by") -- a standalone single
# Latin letter matching one of these in ru/ prose is almost certainly the
# wrong script for that letter, found via a real typo: "взаимодействия c
# каждой" with a Latin "c" instead of Cyrillic "с". A mixed-script *word*
# (see below) can't catch this case since it's a lone character, not part
# of a longer token.
#
# Requires real word boundaries on both sides -- not just "not a letter"
# but specifically not a letter *or* a hyphen -- since a hyphenated
# identifier can otherwise leave a bare single letter as its own
# "word" (e.g. "gcc-c++", "xerces-c-devel", real package names in
# software_requirements.adoc, both false positives without this).
#
# Deliberately lowercase-only: uppercase "C" collided for real with "the
# C language" (e.g. "языках, отличных от SQL и C" in udf.adoc, a
# UDF/C-function doc) -- а/о/у have no equivalent common uppercase
# standalone-letter meaning in this doc set, but dropping all four
# uppercase forms uniformly is simpler and safer than special-casing just
# "C" (also sidesteps "С" starting a sentence, e.g. a "С уважением"-style
# closing, which would otherwise need its own carve-out).
_HOMOGLYPH_STANDALONE_LETTER_RE = re.compile(r'(?<![\w-])([aocy])(?![\w-])')


def _mask_code_and_links(line: str) -> str:
    """Blank out code spans, bold-italic UI-element quotes, AsciiDoc
    macros, `<<anchor,text>>` xrefs, and URLs -- legitimately pure-Latin
    technical content -- while leaving plain bold/italic *text* in place,
    since ordinarily-emphasized prose still needs to be checked for
    homoglyphs (unlike the italics check's masking, which treats all
    bold/italic as already-handled and blanks it too).
    Bold-italic (`*_..._*`) specifically is excluded here because this
    doc family uses it for verbatim third-party UI strings kept in
    English by convention (e.g. DBeaver's own "*_Connect to a database_*"
    dialog title) -- real prose typos never occur inside that quoting
    convention, confirmed via false positives on exactly those two real
    cases."""
    s = _MASK_BOLDITALIC_RE.sub(lambda m: ' ' * len(m.group(0)), line)
    s = _MASK_CODE_SPAN_RE.sub(lambda m: ' ' * len(m.group(0)), s)
    s = _MASK_MACRO_RE.sub(lambda m: ' ' * len(m.group(0)), s)
    s = _MASK_DOUBLE_ANGLE_RE.sub(lambda m: ' ' * len(m.group(0)), s)
    s = _MASK_URL_RE.sub(lambda m: ' ' * len(m.group(0)), s)
    return s


def _find_homoglyph_hits(masked: str):
    """Shared scan step: run the mixed-script-word and standalone-letter
    homoglyph patterns against an already-masked line/value, returning a
    list of (col, tok) hits. Factored out so the same logic applies both
    to ordinary prose lines and to the :description:/:page-htmltitle:
    attribute values (see check_pages_ru_latin_homoglyphs), which carry
    real rendered Russian prose despite being skipped by
    _iter_prose_lines as structural attribute lines."""
    found = []
    for m in _HOMOGLYPH_WORD_TOKEN_RE.finditer(masked):
        tok = m.group(0)
        if len(tok) > 1 and CYRILLIC_RE.search(tok) and _LATIN_LETTER_RE.search(tok):
            found.append((m.start() + 1, tok))
    for m in _HOMOGLYPH_STANDALONE_LETTER_RE.finditer(masked):
        start, end = m.start(1), m.end(1)
        if start > 0 and end < len(masked) and masked[start - 1] == '(' and masked[end] == ')':
            continue
        if start == 0 and re.match(r'\s*\|', masked[end:]):
            continue
        found.append((start + 1, m.group(1)))
    return found


def check_pages_ru_latin_homoglyphs(verbose=False) -> bool:
    """New check (not a port of an existing shell script): flags Latin
    letters that look like they were meant to be Cyrillic in ru/ prose --
    the mirror image of check_pages_no_cyrillic (which only looks for
    Cyrillic contaminating en/; the reverse direction there, a stray
    Cyrillic letter inside an otherwise-Latin word like "A Сlient ID",
    is already caught by that check's broad Cyrillic-anywhere-in-en/
    scan, so it doesn't need repeating here).

    Two patterns, both requiring an actual mix of scripts rather than
    just "any Latin in ru/" (which would flag every legitimate product
    name/command and be useless):

    - a word containing BOTH Cyrillic and Latin letters (token boundary
      is any non-letter, so "PAM-аутентификация" is two clean single-
      script tokens, not one mixed one -- this is a very common pattern
      in this doc set and must not misfire on it);
    - a standalone single Latin letter matching one of the four homoglyph
      one-letter Russian words (see _HOMOGLYPH_STANDALONE_LETTER_RE).

    Deliberately heuristic and ru/-only: code/literal blocks, comments,
    tables, headings, and attribute lines are skipped via
    _iter_prose_lines, and inline code spans/macros/xrefs/URLs are
    additionally blanked per-line, since legitimate Latin content
    (commands, product names, xref targets) lives in exactly those
    places."""
    ok = True
    total_hits = 0
    for _, _, ru_root in module_roots():
        for f in list(_iter_files(ru_root / "pages", ".adoc")) + list(_iter_files(ru_root / "partials", ".adoc")):
            if not _page_allowed(f):
                continue
            lines = _read_lines(f)
            if lines is None:
                continue
            hits = []
            for i, line in _iter_prose_lines(lines):
                masked = _mask_code_and_links(line)
                for col, tok in _find_homoglyph_hits(masked):
                    hits.append((i, col, tok, line))
            # _iter_prose_lines skips all ":"-prefixed attribute lines as
            # structural, but :description:/:page-htmltitle: carry real
            # Russian prose that renders into the page's <meta
            # description>/<title> -- scan just their values here (offset
            # by the prefix length) so a homoglyph typo there isn't missed.
            for i, line in enumerate(lines, 1):
                attr_m = _PROSE_ATTR_RE.match(line)
                if not attr_m:
                    continue
                value = attr_m.group(2)
                offset = attr_m.start(2)
                masked = _mask_code_and_links(value)
                for col, tok in _find_homoglyph_hits(masked):
                    hits.append((i, offset + col, tok, line))
            if hits:
                ok = False
                total_hits += len(hits)
                print(f"FILE     {f}")
                for i, col, tok, line in hits:
                    print(f"  {f}:{i}:{col}: {tok!r}")
                    if verbose:
                        print(f"    {line}")
    if ok:
        print("OK: no Latin/Cyrillic homoglyph mix-ups found in ru/ pages.")
    else:
        print(f"\nTotal: {total_hits} homoglyph hit(s).")
    return ok


# --------------------------------------------------------------------------
# LINKS: external URL health
# --------------------------------------------------------------------------
#
# The one check in this file that touches the network: every http(s) link in
# pages/ and partials/ is fetched and its status classified (404, redirect,
# unreachable host, ...). Because it is slow, non-deterministic, and needs
# connectivity, it is deliberately kept OUT of `--all-checks` (see
# _NETWORK_ONLY_CHECKS) -- it only ever runs on an explicit `check links`. Findings are advisory (marked [beta]): a link that 403s a
# datacenter IP or times out from a CI box is far more often the network in
# between than a genuinely dead page, so only a hard 404/410 (or an NXDOMAIN
# host) is counted as a failure; everything else is reported for a human to
# eyeball, and a footer spells out why a "could not verify" list is expected.

# Knobs, set from argv by _configure_links_from_args (same module-global
# pattern as EXTERNAL_COMPONENTS / GLOSSARY). Defaults chosen so a bare
# `check links` just works.
LINK_TIMEOUT = 10.0
LINK_OFFLINE = False
LINK_ALLOW_DOMAINS = set()
LINK_CACHE_PATH = None          # off by default; --link-cache PATH opts in
LINK_INSECURE = False           # skip TLS verification without the per-run note
LINK_SHOW_UNVERIFIED = False    # also list the server-said-no links (403/429/5xx)
LINK_MAX_WORKERS = 8
_LINK_CACHE_TTL = 7 * 24 * 3600          # a cached result older than this is re-probed
_LINK_PROGRESS_THRESHOLD = 8            # show a progress line past this many fresh fetches
# At most a few requests in flight per host: fanning all 8 workers at one
# host (github.com carries most links in these docs) earns a 429 that is
# our fault. A semaphore rather than a hard lock -- strict serialisation
# turned a 13s run into 90s once one host owned a third of the links.
_LINK_HOST_SEMAPHORES = {}
_LINK_HOST_SEM_GUARD = threading.Lock()
_LINK_PER_HOST_CONCURRENCY = 3

# A real browser UA: a bare "Python-urllib/3.x" is 403'd or tar-pitted by a
# large fraction of sites (Cloudflare, AWS WAF, ...), which would otherwise
# show up as a wall of false BLOCKED findings.
_LINK_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# Host suffixes known to refuse traffic from outside a region or from
# datacenter IP ranges. A failure to reach one of these is relabelled
# "VPN?" and never counted as a finding -- from most machines it says more
# about the route than the page. Matched as a dotted-suffix on the
# hostname ("pravo.gov.ru" matches "publication.pravo.gov.ru"). Keep this
# list short and evidence-based: add a domain only after it has actually
# produced a false BROKEN/UNREACHABLE that a VPN then fixed.
_GEO_SENSITIVE_SUFFIXES = (
    "gov.ru",
    "gosuslugi.ru",
    "nalog.ru",
    "cbr.ru",
)

# AsciiDoc decorations that cling to a URL but aren't part of it: a trailing
# `^` ("open in a new window": `https://x^[label]`) and a `++...++` /
# `+...+` passthrough wrapper (`link:++https://x++[label]`, used to stop
# attribute substitution). Both are stripped from the bare-URL sweep and
# from a macro target.
_LINK_MACRO_RE = re.compile(r'\b(?:link|image):{1,2}\+*(https?://[^\[\]\s^+]+)[+^]*\[')
# Parens ARE allowed in the body -- Wikipedia et al. put them in real paths
# (`/wiki/Kerberos_(protocol)`). An unbalanced trailing one picked up from
# prose (`(see https://x)`) is stripped afterwards by _trim_url.
_BARE_URL_RE = re.compile(r'https?://[^\s\[\]<>"`|^]+[^\s\[\]<>"`|^.,;:!?+_]')
# A trailing `_` is almost always an AsciiDoc italic marker that ran into
# the URL (`_http://host_`); a real URL essentially never ends in one.
_URL_TRAILING_JUNK = '.,;:!?"\'»<>^+_'
_URL_SKIP_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}
# RFC 2606 reserved domains -- matched as a suffix, so www.example.com and
# docs.example.org go too.
_URL_EXAMPLE_DOMAINS = ("example.com", "example.org", "example.net", "example.edu")
# Stand-in hostnames docs use in "http://<this>:PORT" style instructions.
_URL_PLACEHOLDER_HOSTS = {
    "fqdn", "host", "hostname", "hosts", "host1", "host2", "port", "ip",
    "ipaddress", "ip-address", "address", "server", "servername", "server-name",
    "namenode", "master", "your-host", "your-server", "host-or-ip",
}
# Non-routable / doc-only TLDs -- a link into one is never a live external page.
_URL_PRIVATE_TLDS = (".local", ".internal", ".lan", ".intranet", ".corp",
                     ".home", ".invalid", ".test", ".localdomain")
_URL_PLACEHOLDER_RE = re.compile(r'(?i)(?:\bTODO\b|\bFIXME\b|\bXXX\b|CHANGEME|'
                                 r'your[-_]|<[^>]+>|\.\.\.)')
_DOTTED_QUAD_RE = re.compile(r'^\d{1,3}(?:\.\d{1,3}){3}$')


def _trim_url(url):
    """Strip trailing sentence punctuation a bare URL picked up from prose,
    and an unbalanced closing paren (the `(https://x)` case) -- while
    leaving a balanced one alone, since Wikipedia et al. put parens in
    real paths (`/wiki/Foo_(disambiguation)`)."""
    url = url.rstrip(_URL_TRAILING_JUNK)
    while url.endswith(")") and url.count("(") < url.count(")"):
        url = url[:-1].rstrip(_URL_TRAILING_JUNK)
    return url


# Constrained-formatting markers: a bare URL wrapped in a matching pair
# (`_http://x_`, `` `http://x` ``) is rendered as text, not autolinked.
_FMT_WRAP_MARKERS = "_*`+#"


def _extract_urls_from_line(line):
    """Every distinct http(s) URL that AsciiDoc would render as a live link
    on one already comment/code-filtered line. Macro targets
    (`link:URL[..]`, `image:URL[..]`) are read first and masked out so the
    bare-URL sweep doesn't double-count them. A bare URL is skipped when
    AsciiDoc wouldn't autolink it: escaped with a backslash (`\\http://x`)
    or wrapped in a formatting pair (`_http://x_`)."""
    urls = []
    masked = line
    for m in _LINK_MACRO_RE.finditer(line):
        urls.append(_trim_url(m.group(1)))
        masked = masked[:m.start(1)] + " " * (m.end(1) - m.start(1)) + masked[m.end(1):]
    for m in _BARE_URL_RE.finditer(masked):
        before = masked[m.start() - 1] if m.start() else ""
        after = masked[m.end()] if m.end() < len(masked) else ""
        if before == "\\":
            continue
        if before and before == after and before in _FMT_WRAP_MARKERS:
            continue
        urls.append(_trim_url(m.group(0)))
    # de-dup within the line, preserve order
    seen = set()
    return [u for u in urls if u and not (u in seen or seen.add(u))]


def _host_of(url):
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _host_suffix_match(host, suffix):
    return host == suffix or host.endswith("." + suffix)


def _is_geo_sensitive(host):
    return any(_host_suffix_match(host, s) for s in _GEO_SENSITIVE_SUFFIXES)


def _is_unroutable_host(host):
    """True for a host that is never a live external page: a private /
    loopback / link-local / malformed IP, a doc-only TLD (.internal,
    .local, ...), or a stand-in like FQDN / HOST / HOSTNAME. These show up
    all over "point your browser at http://<host>:<port>" instructions."""
    if host in _URL_PLACEHOLDER_HOSTS:
        return True
    if host.endswith(_URL_PRIVATE_TLDS):
        return True
    if any(_host_suffix_match(host, d) for d in _URL_EXAMPLE_DOMAINS):
        return True
    try:
        return not ipaddress.ip_address(host).is_global
    except ValueError:
        # a dotted-quad shape that isn't a valid address (10.20.30.444)
        return bool(_DOTTED_QUAD_RE.match(host))


def _should_probe(url):
    """Skip URLs that can't or shouldn't be fetched: RFC-2606 example
    hosts, localhost, private/placeholder hosts (see _is_unroutable_host),
    an unsubstituted `{attribute}`, an obvious placeholder, or a host the
    caller allow-listed with --allow-domain."""
    if "{" in url or "}" in url:
        return False
    if url.endswith(".git"):
        return False  # a VCS clone remote (git clone URL.git), not a web page
    if _URL_PLACEHOLDER_RE.search(url):
        return False
    host = _host_of(url)
    if not host or host in _URL_SKIP_HOSTS or _is_unroutable_host(host):
        return False
    if any(_host_suffix_match(host, d) for d in LINK_ALLOW_DOMAINS):
        return False
    return True


class _Probe:
    """Outcome of one URL fetch. `error` is None on any HTTP response
    (even a 500), else an (kind, text) tuple where kind is one of
    dns-nxdomain / dns / timeout / refused / reset / ssl / other."""
    __slots__ = ("url", "status", "final_url", "redirects", "error")

    def __init__(self, url, status=None, final_url=None, redirects=(), error=None):
        self.url = url
        self.status = status
        self.final_url = final_url or url
        self.redirects = tuple(redirects)
        self.error = error


class _ChainRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self):
        self.chain = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.chain.append((code, newurl))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _describe_neterror(reason):
    if isinstance(reason, socket.timeout) or "timed out" in str(reason).lower():
        return ("timeout", f"no response in {LINK_TIMEOUT:.0f}s")
    if isinstance(reason, socket.gaierror):
        txt = str(reason).lower()
        if reason.args and reason.args[0] in (socket.EAI_NONAME, getattr(socket, "EAI_NODATA", None)) \
                or "name or service not known" in txt or "nodename nor servname" in txt \
                or "no address associated" in txt:
            return ("dns-nxdomain", "host does not resolve")
        return ("dns", "DNS lookup failed")
    txt = str(reason).lower()
    if "refused" in txt:
        return ("refused", "connection refused")
    if "reset" in txt:
        return ("reset", "connection reset")
    if "ssl" in txt or "certificate" in txt:
        return ("ssl", str(reason))
    return ("other", str(reason).strip() or reason.__class__.__name__)


_UNVERIFIED_TLS_CTX = ssl._create_unverified_context()
# Hosts where cert verification failed and we fell back to an unverified
# connection -- almost always a missing local CA bundle (a fresh macOS
# python that never ran "Install Certificates.command") or a MITM proxy,
# not a real problem with the site. Reported once as a footer note.
_LINK_TLS_FELL_BACK = set()


def _is_cert_error(reason):
    return isinstance(reason, ssl.SSLCertVerificationError) or \
        "CERTIFICATE_VERIFY_FAILED" in str(reason)


def _host_gate(host):
    with _LINK_HOST_SEM_GUARD:
        return _LINK_HOST_SEMAPHORES.setdefault(
            host, threading.Semaphore(_LINK_PER_HOST_CONCURRENCY))


def _http_attempt(url, method, timeout, context=None):
    """One HTTP request. Returns a _Probe. On a local cert-verification
    failure (missing CA bundle / MITM proxy), retries once unverified and
    records the host in _LINK_TLS_FELL_BACK. The single seam most probe
    tests patch."""
    handler = _ChainRedirectHandler()
    handlers = [handler]
    if context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(
        url, method=method,
        headers={"User-Agent": _LINK_BROWSER_UA, "Accept": "*/*"})
    if method == "GET":
        req.add_header("Range", "bytes=0-0")
    try:
        resp = opener.open(req, timeout=timeout)
        code = getattr(resp, "status", None) or resp.getcode()
        final = resp.geturl()
        resp.close()
        return _Probe(url, status=code, final_url=final, redirects=handler.chain)
    except urllib.error.HTTPError as e:
        return _Probe(url, status=e.code, final_url=getattr(e, "url", url),
                      redirects=handler.chain)
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        reason = getattr(e, "reason", e)
        if context is None and not LINK_INSECURE and _is_cert_error(reason):
            retry = _http_attempt(url, method, timeout, context=_UNVERIFIED_TLS_CTX)
            if retry.error is None:
                _LINK_TLS_FELL_BACK.add(_host_of(url))
                return retry
        return _Probe(url, error=_describe_neterror(reason))


def _probe_url(url, timeout=None):
    """Fetch one URL and classify the transport outcome. This is the
    network seam -- tests monkeypatch it wholesale. Tries HEAD first,
    falls back to a ranged GET when a server rejects or mishandles HEAD,
    and retries a transient failure or a 429/503 once with a short
    back-off. No more than _LINK_PER_HOST_CONCURRENCY requests to one host
    run at once, so our own fan-out doesn't earn the rate limiting."""
    timeout = LINK_TIMEOUT if timeout is None else timeout
    ctx = _UNVERIFIED_TLS_CTX if LINK_INSECURE else None

    def run():
        probe = _http_attempt(url, "HEAD", timeout, ctx)
        if probe.error is not None or probe.status in (403, 405, 501, 400):
            alt = _http_attempt(url, "GET", timeout, ctx)
            if alt.error is None or probe.error is not None:
                probe = alt
        if probe.error is not None and probe.error[0] in ("timeout", "reset", "other"):
            time.sleep(0.4)
            retry = _http_attempt(url, "GET", timeout, ctx)
            if retry.error is None:
                probe = retry
        if probe.status in (429, 503):
            time.sleep(1.0)
            retry = _http_attempt(url, "GET", timeout, ctx)
            if retry.error is None and retry.status not in (429, 503):
                probe = retry
        return probe

    try:
        with _host_gate(_host_of(url)):
            return run()
    except Exception as e:   # a malformed URL etc. must not kill the whole run
        return _Probe(url, error=("other", f"{e.__class__.__name__}: {e}"))


def _probe_many(urls, on_done=None):
    """Probe every URL concurrently. `on_done(n_completed, total, last_probe)`
    is called (under a lock, from worker threads) after each result, for
    progress reporting."""
    if not urls:
        return []
    workers = max(1, min(LINK_MAX_WORKERS, len(urls)))
    total = len(urls)
    counter = {"n": 0}
    lock = threading.Lock()

    def one(u):
        probe = _probe_url(u)
        if on_done is not None:
            with lock:
                counter["n"] += 1
                on_done(counter["n"], total, probe)
        return probe

    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(zip(urls, ex.map(one, urls)))


class _LinkProgress:
    """Live 'checked k/N' line on a TTY; a periodic one-liner otherwise so a
    CI log still shows the run is moving. Everything goes to stderr, clear of
    the findings on stdout."""

    def __init__(self, total):
        self.total = total
        self.tty = sys.stderr.isatty()
        self.start = time.monotonic()
        self._last_line = 0

    def __call__(self, n, total, probe):
        host = _host_of(probe.url)[:38]
        if self.tty:
            print(f"\r  checking links: {n}/{total}  {host:<38}",
                  end="", file=sys.stderr, flush=True)
        elif n == total or n - self._last_line >= 20:
            self._last_line = n
            print(f"  checking links: {n}/{total} "
                  f"({time.monotonic() - self.start:.0f}s)", file=sys.stderr, flush=True)

    def done(self):
        if self.tty:
            print(f"\r{' ' * 64}\r", end="", file=sys.stderr, flush=True)


def _trivial_redirect(a, b):
    """True when a 301/308 only cosmetically rewrites the URL and points at
    the same page -- an http->https upgrade, a +/- `www.`, a +/- trailing
    slash, a dropped #fragment (servers strip it; the browser reapplies
    it), or a change of letter case (e.g. NVD's CVE-1234 -> cve-1234).
    None of those is worth editing in the source."""
    def norm(u):
        u = u.split("#", 1)[0].rstrip("/").lower()
        u = re.sub(r'^https?://(www\.)?', '', u)
        return u
    return norm(a) == norm(b)


def _classify_probe(probe):
    """(label, detail, is_finding). Only a hard 404/410 or a non-resolving
    host is a finding; every other non-OK outcome is reported but does not
    fail the run."""
    host = _host_of(probe.url)
    if probe.error is not None:
        kind, text = probe.error
        if _is_geo_sensitive(host):
            return ("VPN?", f"{text} -- host is often region-locked", False)
        if kind == "dns-nxdomain":
            return ("BROKEN", "host does not resolve", True)
        return ("UNREACHABLE", text, False)

    s = probe.status
    if s is None:
        return ("UNREACHABLE", "no response", False)
    if 200 <= s < 300:
        perm = [c for c, _ in probe.redirects if c in (301, 308)]
        if perm and not _trivial_redirect(probe.url, probe.final_url):
            return ("REDIRECT", f"{perm[0]} -> {probe.final_url}", False)
        return ("OK", str(s), False)
    if s in (301, 308):
        if _trivial_redirect(probe.url, probe.final_url):
            return ("OK", str(s), False)
        return ("REDIRECT", f"{s} -> {probe.final_url}", False)
    if s in (302, 303, 307):
        return ("OK", f"{s} -> {probe.final_url}", False)
    if s in (404, 410):
        return ("BROKEN", str(s), True)
    if s in (401, 403):
        if _is_geo_sensitive(host):
            return ("VPN?", f"{s} -- host is often region-locked", False)
        return ("BLOCKED", f"{s} (auth or anti-bot wall)", False)
    if s == 429:
        return ("RATELIMIT", "429 (too many requests)", False)
    if 500 <= s < 600:
        return ("SERVER-ERR", f"{s} (server-side, likely transient)", False)
    return ("BLOCKED", str(s), False)


def _load_link_cache():
    if not LINK_CACHE_PATH:
        return {}
    try:
        data = json.loads(Path(LINK_CACHE_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    now = time.time()
    return {u: v for u, v in data.items()
            if isinstance(v, dict) and now - v.get("checked_at", 0) < _LINK_CACHE_TTL}


def _save_link_cache(cache):
    if not LINK_CACHE_PATH:
        return
    try:
        Path(LINK_CACHE_PATH).write_text(
            json.dumps(cache, indent=1, sort_keys=True), encoding="utf-8")
    except OSError:
        pass


def check_links_external(verbose=False) -> bool:
    """Fetch every http(s) link in pages/ and partials/ (both languages).

    Printed line by line:

        BROKEN       404 / 410, or a host that does not resolve  -- fails the run
        REDIRECT     permanent (301/308) to a genuinely different URL
        UNREACHABLE  timeout / connection refused / DNS failure
        VPN?         unreachable on a host known to region-lock

    UNREACHABLE / VPN? don't fail the run -- re-run behind a VPN to tell a
    dead link from a blocked route. Collapsed to a one-line count (the
    server answered, just not usefully): 401/403 anti-bot walls, 429s, 5xx.
    --show-unverified (or --verbose) lists those too.

    Only BROKEN fails the run. Links are de-duplicated site-wide (one fetch
    per distinct URL, at most a few concurrent per host). Every run is fresh
    unless --link-cache PATH is given (then results are cached there with a
    7-day TTL). --offline skips fetching and just lists the links found.
    Honours --page. Past a handful of fetches a "checking k/N" progress line
    is written to stderr.

    Hosts whose TLS certificate the local trust store can't validate (a
    missing CA bundle, a MITM proxy) are retried once without verification
    and noted -- pass --insecure to make that the default and drop the note.

    Only what AsciiDoc renders as a live link is checked: comment lines and
    ---- / .... blocks are skipped, and so is a bare URL that is
    backslash-escaped (\\http://x) or wrapped in a formatting pair
    (_http://x_, `http://x`). Also skipped: RFC-2606 example hosts,
    localhost, private/link-local IPs, doc-only hosts (FQDN:PORT,
    *.internal, 10.x, ...), *.git remotes, and any host passed to
    --allow-domain.

    A 301/308 that only rewrites the URL cosmetically -- http->https, +/-
    www., +/- trailing slash, a dropped #fragment, a change of letter case
    -- is treated as clean, not a REDIRECT. Findings are grouped
    BROKEN, then REDIRECT, then UNREACHABLE/VPN?."""
    _LINK_TLS_FELL_BACK.clear()
    _LINK_HOST_SEMAPHORES.clear()
    sites = {}
    for _, en_root, ru_root in module_roots():
        for root in (en_root, ru_root):
            for sub in ("pages", "partials"):
                for f in _iter_files(root / sub, ".adoc"):
                    if not _page_allowed(f):
                        continue
                    lines = _read_lines(f)
                    if lines is None:
                        continue
                    excluded = _excluded_ref_lines(f)
                    for lineno, line in enumerate(lines, 1):
                        if lineno in excluded:
                            continue
                        for url in _extract_urls_from_line(line):
                            if _should_probe(url):
                                sites.setdefault(url, []).append((f, lineno))

    if not sites:
        print("OK: no external links found.")
        return True

    if LINK_OFFLINE:
        print(f"{len(sites)} distinct external link(s) found "
              f"(--offline: not fetched):\n")
        for url in sorted(sites):
            print(f"  {url}")
            if verbose:
                for f, ln in sites[url]:
                    print(f"        {f}:{ln}")
        return True

    cache = _load_link_cache()
    fresh_urls = [u for u in sorted(sites) if u not in cache]
    # Progress reporting kicks in only once there's enough work to wonder
    # whether it's stuck -- a handful of links resolve before you'd look.
    if len(fresh_urls) >= _LINK_PROGRESS_THRESHOLD:
        cached_n = len(sites) - len(fresh_urls)
        note = f" ({cached_n} already cached)" if cached_n else ""
        print(f"checking {len(fresh_urls)} external link(s){note}, "
              f"timeout {LINK_TIMEOUT:.0f}s each ...", file=sys.stderr, flush=True)
        progress = _LinkProgress(len(fresh_urls))
        fresh = _probe_many(fresh_urls, on_done=progress)
        progress.done()
    else:
        fresh = _probe_many(fresh_urls)

    if fresh and len(fresh) >= 5 and all(
            p.error is not None and p.error[0] != "dns-nxdomain" for _, p in fresh):
        sys.exit(
            f"error: all {len(fresh)} freshly-checked links failed to connect -- "
            f"this looks like no network / blocked egress, not {len(fresh)} dead "
            f"links. Re-run with network access, or 'check links --offline' to "
            f"just list them.")

    results = {}
    for url, probe in fresh:
        label, detail, finding = _classify_probe(probe)
        results[url] = (label, detail, finding)
        cache[url] = {"label": label, "detail": detail, "finding": finding,
                      "checked_at": time.time()}
    for url in sites:
        if url not in results:
            c = cache[url]
            results[url] = (c["label"], c["detail"], c["finding"])
    _save_link_cache(cache)

    total = len(sites)
    # Shown line-by-line: things you act on in the source (BROKEN, REDIRECT)
    # and things you'd re-test behind a VPN (UNREACHABLE, VPN?).
    # Collapsed to a count: the server answered, just not usefully -- 403
    # anti-bot walls, 429s, 5xx. --show-unverified / --verbose lists those.
    # Worst first: dead links you must fix, then redirects to update, then
    # the couldn't-reach list. URL-sorted within each group.
    _ORDER = {"BROKEN": 0, "REDIRECT": 1, "UNREACHABLE": 2, "VPN?": 2}
    shown, collapsed = [], []
    for url in sorted(sites):
        label, detail, finding = results[url]
        if label == "OK":
            continue
        (shown if label in _ORDER else collapsed).append((label, detail, url))
    shown.sort(key=lambda h: (_ORDER[h[0]], h[2]))

    def _print_hit(label, detail, url):
        print(f"{label:<11} {url}  ({detail})")
        refs = sites[url]
        for f, ln in (refs if verbose else refs[:3]):
            print(f"        {f}:{ln}")
        if not verbose and len(refs) > 3:
            print(f"        ... and {len(refs) - 3} more (--verbose for all)")

    prev = None
    for label, detail, url in shown:
        if prev is not None and _ORDER[label] != _ORDER[prev]:
            print()                       # blank line between groups
        _print_hit(label, detail, url)
        prev = label

    show_collapsed = verbose or LINK_SHOW_UNVERIFIED
    if collapsed and show_collapsed:
        if shown:
            print()
        for label, detail, url in collapsed:
            _print_hit(label, detail, url)

    findings = sum(1 for lbl, _, _ in shown if lbl == "BROKEN")
    redirects = sum(1 for lbl, _, _ in shown if lbl == "REDIRECT")
    unreachable = sum(1 for lbl, _, _ in shown if lbl in ("UNREACHABLE", "VPN?"))

    if not shown and not collapsed:
        print(f"OK: all {total} external link(s) resolve.")
    else:
        parts = [f"{findings} broken", f"{redirects} redirect(s)"]
        if unreachable:
            parts.append(f"{unreachable} unreachable")
        print(f"\nTotal: {', '.join(parts)}, of {total} external link(s).")

    if unreachable:
        print("Unreachable from here can mean the link is dead, or that a "
              "proxy / datacenter-IP block / regional restriction is in the\n"
              "way -- re-run behind a VPN to tell them apart.")

    if collapsed and not show_collapsed:
        by_label = Counter(lbl for lbl, _, _ in collapsed)
        pretty = {"BLOCKED": "blocked", "RATELIMIT": "rate-limited",
                  "SERVER-ERR": "server error"}
        breakdown = ", ".join(f"{n} {pretty.get(lbl, lbl.lower())}"
                              for lbl, n in by_label.most_common())
        print(f"{len(collapsed)} more link(s) got an unhelpful response "
              f"({breakdown}) -- --show-unverified to list them.")

    if _LINK_TLS_FELL_BACK:
        print(f"{len(_LINK_TLS_FELL_BACK)} host(s) were checked without TLS "
              f"verification (local trust store can't validate them --\n"
              f"usually a missing CA bundle: on macOS run \"Install "
              f"Certificates.command\", or pass --insecure to silence this).")
    return findings == 0


# --------------------------------------------------------------------------
# CHECK REGISTRY
# --------------------------------------------------------------------------

CHECKS = {
    "examples-no-cyrillic": check_examples_no_cyrillic,
    "examples-orphaned": check_examples_orphaned,
    "examples-parity": check_examples_parity,
    "images-orphaned": check_images_orphaned,
    "nav-structure-parity": check_nav_structure_parity,
    "pages-broken-refs": check_pages_broken_refs,
    "pages-file-path-italics": check_pages_file_path_italics,
    "pages-line-parity": check_pages_line_parity,
    "pages-link-parity": check_pages_link_parity,
    "pages-literal-parity": check_pages_literal_parity,
    "pages-no-cyrillic": check_pages_no_cyrillic,
    "pages-no-invisible-chars": check_pages_no_invisible_chars,
    "pages-no-unicode-dashes": check_pages_no_unicode_dashes,
    "pages-no-yo": check_pages_no_yo,
    "pages-orphaned": check_pages_orphaned,
    "pages-ru-latin-homoglyphs": check_pages_ru_latin_homoglyphs,
    "pages-stray-backticks": check_pages_stray_backticks,
    "pages-structure-parity": check_pages_structure_parity,
    "pages-table-cell-periods": check_pages_table_cell_periods,
    "pages-terminology": check_pages_terminology,
    "pages-translation": check_pages_translation,
    "pages-unbalanced-delimiters": check_pages_unbalanced_delimiters,
    "partials-orphaned": check_partials_orphaned,
    "tags-orphaned": check_tags_orphaned,
    "links-external": check_links_external,
}

# Checks whose logic is heuristic (no real AsciiDoc parser behind it) and can
# therefore misfire on legitimate content -- flagged so --list-checks and the
# README can warn people to treat their output as a review list, not a gate.
BETA_CHECKS = {
    "pages-file-path-italics",
    "pages-link-parity",
    "pages-literal-parity",
    "pages-ru-latin-homoglyphs",
    "pages-structure-parity",
    "pages-table-cell-periods",
    "pages-terminology",
    "pages-translation",
    "links-external",   # not heuristic parsing, but network flakiness makes it advisory
}

# Checks that reach the network. Excluded from the legacy `--all-checks`
# sweep (a pre-commit hook or CI job must not fan out hundreds of HTTP
# requests just because it asked for "everything"); on the `check` surface
# there is no "everything" keyword at all, so a network family only ever
# runs when named (`check links`).
_NETWORK_ONLY_CHECKS = {"links-external"}
_NETWORK_FAMILIES = {"links"}


# --------------------------------------------------------------------------
# CHECK FAMILIES  (the `docs_tool check <family>` surface)
# --------------------------------------------------------------------------
#
# The 22 flat CHECKS keys above stay the source of truth -- every check
# function, its behaviour, and the legacy `--check-<key>` flag are unchanged.
# FAMILIES is a routing layer on top: it groups the same checks by where a
# rule's authority comes from (the "writing-quality pyramid" -- see
# docs/proposals/cli-redesign.md), which also predicts how deterministic a
# check is and whether it should block a commit.
#
#   FAMILIES[family][rule] = {scan-target: CHECKS-key}
#
# Selection rules (see _resolve_family_selection):
#   check <family>                       -> every rule in the family, all targets
#   check <family> --<rule>              -> that rule, target "pages" (or its
#                                           sole target)
#   check <family> --<rule> --target X   -> that rule, target X
#   check <family> --target X            -> every rule in the family that has
#                                           a target X
#   --target all                            -> every target of whatever is selected
#
# TIERS is advisory and display-only: `list`/`show` render it as
# "suggest: block"/"suggest: warn", a hint for what a pre-commit hook should
# hard-fail on vs. just report. Nothing here changes how the tool exits --
# every run is 0 clean / 1 on findings regardless of family.
FAMILIES = {
    "chars": {                        # L0 -- Unicode / encoding
        "no-cyrillic":  {"pages": "pages-no-cyrillic", "examples": "examples-no-cyrillic"},
        "no-invisible": {"pages": "pages-no-invisible-chars"},
        "dashes":       {"pages": "pages-no-unicode-dashes"},
        "homoglyphs":   {"pages": "pages-ru-latin-homoglyphs"},
    },
    "markup": {                       # L1 -- AsciiDoc spec
        "backticks":  {"pages": "pages-stray-backticks"},
        "delimiters": {"pages": "pages-unbalanced-delimiters"},
    },
    "refs": {                         # L2 -- Antora reference resolution
        "broken":   {"pages": "pages-broken-refs"},
        "orphaned": {"pages": "pages-orphaned", "partials": "partials-orphaned",
                     "examples": "examples-orphaned", "images": "images-orphaned",
                     "tags": "tags-orphaned"},
    },
    "style": {                        # L3 -- Arenadata style guide
        "no-yo":              {"pages": "pages-no-yo"},
        "file-path-italics":  {"pages": "pages-file-path-italics"},
        "table-cell-periods": {"pages": "pages-table-cell-periods"},
    },
    "terms": {                        # L4 -- controlled vocabulary (glossary)
        "terminology": {"pages": "pages-terminology"},
    },
    "l10n": {                         # L5 -- "RU mirrors EN"
        "lines":        {"pages": "pages-line-parity"},
        "structure":    {"pages": "pages-structure-parity"},
        "links":        {"pages": "pages-link-parity"},
        "literals":     {"pages": "pages-literal-parity"},
        "untranslated": {"pages": "pages-translation"},
        "examples":     {"examples": "examples-parity"},
        "nav":          {"nav": "nav-structure-parity"},
    },
    "links": {                        # L6 -- the open web (network; runs only when named)
        "external": {"pages": "links-external"},
    },
}

TIERS = {
    "universal": ("chars", "markup", "refs"),   # deterministic  -> block by default
    "house":     ("style", "terms"),            # per-vendor      -> warn by default
    "relational": ("l10n",),                    # needs both trees -> warn by default
    "external":  ("links",),                    # network, non-deterministic -> warn
}

_SCAN_TARGETS = ("pages", "partials", "examples", "images", "tags", "nav")

# `--target` values, for the two rules that have more than one target
# (chars --no-cyrillic, refs --orphaned). Shown by `list targets`.
TARGET_DESC = {
    "pages":    "pages/ + partials/  (the default)",
    "partials": "partials/ only",
    "examples": "examples/",
    "images":   "images/",
    "tags":     "tag:: / end:: regions (in pages/, partials/, examples/)",
    "nav":      "nav.adoc",
    "all":      "every target above",
}

_ALL_RULES = tuple(sorted({rule for fam in FAMILIES.values() for rule in fam}))

# Stable per-check identifiers, family-prefixed. The user-facing handle for a
# check: accepted by `show <id>`, printed by `list`. (Decoupling the selector
# from the Python function name; inline-suppression / JSON keying will build
# on these -- see cli-redesign.md phase 1/4.)
RULE_IDS = {
    "pages-no-cyrillic":          "CH01",
    "examples-no-cyrillic":       "CH02",
    "pages-no-invisible-chars":   "CH03",
    "pages-no-unicode-dashes":    "CH04",
    "pages-ru-latin-homoglyphs":  "CH05",
    "pages-stray-backticks":      "MK01",
    "pages-unbalanced-delimiters": "MK02",
    "pages-broken-refs":          "RF01",
    "pages-orphaned":             "RF02",
    "partials-orphaned":          "RF03",
    "examples-orphaned":          "RF04",
    "images-orphaned":            "RF05",
    "tags-orphaned":              "RF06",
    "pages-no-yo":                "ST01",
    "pages-file-path-italics":    "ST02",
    "pages-table-cell-periods":   "ST03",
    "pages-terminology":          "TM01",
    "pages-line-parity":          "LN01",
    "pages-structure-parity":     "LN02",
    "pages-translation":          "LN03",
    "examples-parity":            "LN04",
    "nav-structure-parity":       "LN05",
    "pages-link-parity":          "LN06",
    "pages-literal-parity":       "LN07",
    "links-external":             "LK01",
}
_ID_TO_KEY = {v: k for k, v in RULE_IDS.items()}

# One-line "what it does" for `docs_tool list`. The full rationale/exceptions
# stay in each check_* function's docstring (docs_tool show <rule>).
SUMMARIES = {
    "pages-no-cyrillic":           "no Cyrillic characters in en/ files (RU text left in EN)",
    "examples-no-cyrillic":        "same check, over examples/ (all file types)",
    "pages-no-invisible-chars":    "no zero-width / invisible / bidi-control characters",
    "pages-no-unicode-dashes":     "no literal en/em dash -- house style uses --",
    "pages-ru-latin-homoglyphs":   "Latin letters in ru/ prose that should be Cyrillic",
    "pages-stray-backticks":       "no line with an odd number of backticks",
    "pages-unbalanced-delimiters": "every block delimiter closed once includes are flattened",
    "pages-broken-refs":           "every xref: / include:: / image: target resolves",
    "pages-orphaned":              "every pages/*.adoc reachable from some nav.adoc",
    "partials-orphaned":           "every tag-less partial pulled in by some include::",
    "examples-orphaned":           "every examples/ file pulled in by include::example$",
    "images-orphaned":             "every images/ file the target of an image: macro",
    "tags-orphaned":               "every tag::/end:: region pulled in by an include tag=",
    "pages-no-yo":                 "no ё/Ё in ru/ files (:page-author: exempt)",
    "pages-file-path-italics":     "file / directory names in prose need _italics_",
    "pages-table-cell-periods":    "a table cell's last sentence shouldn't end with a period",
    "pages-terminology":           "EN glossary term translated to a non-house-style RU word",
    "pages-line-parity":           "EN file and its RU counterpart have the same line count",
    "pages-structure-parity":      "EN and RU structural skeletons must match",
    "pages-link-parity":           "EN and RU must reference the same xref / image / URL targets",
    "pages-literal-parity":        "EN and RU must carry the same back-ticked literals",
    "pages-translation":           "RU line still identical to EN, or carrying English stopwords",
    "examples-parity":             "EN and RU examples/ must match (byte / comment-stripped)",
    "nav-structure-parity":        "EN and RU nav.adoc structure must match",
    "links-external":              "external http(s) links: 404 fails, redirects and unreachable flagged",
}

# Short label per family for `list`; tier -> commit disposition.
FAMILY_DESC = {
    "chars":  "Unicode / encoding",
    "markup": "AsciiDoc syntax",
    "refs":   "Antora reference resolution",
    "style":  "Arenadata house style",
    "terms":  "controlled vocabulary",
    "l10n":   "EN<->RU parity",
    "links":  "external URL health (network; run explicitly)",
}
_TIER_DISPOSITION = {"universal": "block", "house": "warn", "relational": "warn",
                     "external": "warn"}

# Which optional flags actually do something for a given rule, so `show` can
# offer examples that work instead of a generic list. Derived from the check
# bodies rather than guessed -- ExampleFlagMetadataTests re-derives them from
# the source and fails if a check gains or loses support without this being
# updated.
#
# The --page exceptions are listed rather than the rules that honour it: most
# do, and the whole-site checks are the memorable minority.
# Terse "what it flags" per rule, for the one-line-per-rule table in --help.
# Deliberately shorter than SUMMARIES: the table has to fit a terminal, and at
# a glance you want to pick the rule, not read its definition.
RULE_FLAGS = {
    "pages-no-cyrillic":           "Cyrillic in EN files",
    "pages-no-invisible-chars":    "zero-width characters",
    "pages-no-unicode-dashes":     "literal en/em dashes",
    "pages-ru-latin-homoglyphs":   "Latin letters in RU prose",
    "pages-stray-backticks":       "odd backtick count",
    "pages-unbalanced-delimiters": "unclosed block delimiter",
    "pages-broken-refs":           "dead xref / include / image",
    "pages-orphaned":              "defined but never referenced",
    "pages-no-yo":                 "ё in RU files",
    "pages-file-path-italics":     "file path not in italics",
    "pages-table-cell-periods":    "table cell ending in a period",
    "pages-terminology":           "off-glossary RU translation",
    "pages-line-parity":           "EN/RU line counts differ",
    "pages-structure-parity":      "EN/RU skeletons differ",
    "pages-link-parity":           "EN/RU link targets differ",
    "pages-literal-parity":        "EN/RU back-ticked literals differ",
    "pages-translation":           "RU line still English",
    "examples-parity":             "EN/RU examples differ",
    "nav-structure-parity":        "EN/RU nav differs",
    "links-external":              "dead / redirected / unreachable",
}


def _rules_table():
    """One line per rule: ID, the command that runs it, what it flags. Built
    from the same tables the tool dispatches on, so it cannot drift from the
    real rule set. Multi-target rules collapse to a single row with an ID
    range (RF02-RF06) -- five near-identical rows would crowd out everything
    else in --help."""
    rows = []
    for fam, rules in FAMILIES.items():
        for rule, targets in rules.items():
            primary = targets.get("pages") or next(iter(targets.values()))
            ids = sorted(RULE_IDS[k] for k in targets.values())
            rid = ids[0] if len(ids) == 1 else f"{ids[0]}-{ids[-1]}"
            cmd = _check_command(primary)
            if len(targets) > 1:
                cmd += " [--target ...]"
            rows.append((rid, cmd, RULE_FLAGS.get(primary, SUMMARIES.get(primary, ""))))
    w_id = max(len(r[0]) for r in rows)
    w_cmd = max(len(r[1]) for r in rows)
    out = ["Rules:"]
    out += [f"    {rid:<{w_id}}  {cmd:<{w_cmd}}  {what}" for rid, cmd, what in rows]
    return "\n".join(out) + "\n"


_RULES_IGNORING_PAGE = {
    "examples-no-cyrillic", "examples-parity", "nav-structure-parity",
    "pages-broken-refs", "pages-orphaned", "examples-orphaned", "images-orphaned",
}
_RULES_WITH_VERBOSE = {
    "pages-no-invisible-chars", "pages-ru-latin-homoglyphs", "pages-structure-parity",
    "pages-translation", "examples-parity", "nav-structure-parity",
    "pages-file-path-italics", "pages-terminology", "links-external",
    "pages-link-parity", "pages-literal-parity",
}
_RULES_WITH_EXTERNAL_ROOT = {
    "pages-unbalanced-delimiters", "pages-broken-refs", "partials-orphaned",
    "images-orphaned", "tags-orphaned",
}


def _sibling_targets(key):
    """The other --target values the same rule can be pointed at, e.g.
    ('examples',) for CH01. Empty for a single-target rule."""
    for rules in FAMILIES.values():
        for targets in rules.values():
            if key in targets.values() and len(targets) > 1:
                return tuple(t for t, k in targets.items() if k != key)
    return ()


def _rule_examples(key):
    """Runnable `docs_tool` lines for one rule: the bare form first, then one
    per optional flag that changes what this particular rule does. Generated
    from _check_command so an example can't drift from the real syntax."""
    cmd = _check_command(key)
    out = [(f"./docs_tool.py {cmd}", "the whole site")]
    if key not in _RULES_IGNORING_PAGE:
        out.append((f"./docs_tool.py {cmd} --page resource_groups.adoc",
                    "one page, a directory, or UNCOMMITTED"))
    siblings = _sibling_targets(key)
    if siblings:
        # One representative --target line. refs --orphaned has four siblings;
        # listing each would bury the flags that are actually specific to this
        # rule under near-identical repetition.
        tgt = siblings[0]
        other = _check_command(_resolve_family_selection(
            _family_of(_rule_of(key)), {_rule_of(key)}, tgt)[0])
        rest = "another --target" if len(siblings) > 1 else f"over {tgt}/"
        out.append((f"./docs_tool.py {other}", rest))
    if key in _RULES_WITH_VERBOSE:
        vlabel = ("every referencing page + the 403/429/5xx list"
                  if key == "links-external" else "full diff / per-hit detail")
        out.append((f"./docs_tool.py {cmd} --verbose", vlabel))
    if key == "pages-terminology":
        out.append((f"./docs_tool.py {cmd} --glossary my-glossary.psv",
                    "explicit glossary (else *-glossary.psv)"))
    if key in _RULES_WITH_EXTERNAL_ROOT:
        out.append((f"./docs_tool.py {cmd} --external-root ADCM=../docs-adcm",
                    "resolve cross-repo refs"))
    if key == "links-external":
        out.append((f"./docs_tool.py {cmd} --offline", "just list the links, don't fetch"))
        out.append((f"./docs_tool.py {cmd} --show-unverified", "list the 403/429/5xx links too"))
        out.append((f"./docs_tool.py {cmd} --timeout 5", "shorter per-request timeout (default 10s)"))
        out.append((f"./docs_tool.py {cmd} --allow-domain intranet.example.com",
                    "skip a host you know is fine"))
        out.append((f"./docs_tool.py {cmd} --insecure", "skip TLS verification (missing local CA bundle)"))
        out.append((f"./docs_tool.py {cmd} --link-cache .link-cache.json", "cache results (off by default)"))
    return out


def _family_of(rule):
    """The family a rule name belongs to (rule names are unique
    across families), or None."""
    for fam, rules in FAMILIES.items():
        if rule in rules:
            return fam
    return None


def _resolve_family_selection(family, picked_rules, target):
    """Map a `check` invocation to an ordered, de-duplicated list of CHECKS
    keys. `family` is one FAMILIES key, or None for every non-network family
    (network families are only ever run when named -- there is no "all"
    keyword on this surface); `picked_rules` is a set (empty = whole
    family); `target` is a scan target, "all", or None. See the selection
    rules above FAMILIES."""
    if family is None:
        fams = [f for f in FAMILIES if f not in _NETWORK_FAMILIES]
    else:
        fams = [family]
    picked = set(picked_rules or ())
    out = []
    for fam in fams:
        for rule, targets in FAMILIES[fam].items():
            if picked and rule not in picked:
                continue
            if target == "all":
                out.extend(targets.values())
            elif target:
                if target in targets:
                    out.append(targets[target])
            elif picked:
                out.append(targets.get("pages") or next(iter(targets.values())))
            else:
                out.extend(targets.values())
    seen = set()
    return [k for k in out if k and not (k in seen or seen.add(k))]


# ==========================================================================
# SYNC: align a RU page's structure/content with its EN counterpart after an
# EN edit. Ported from sync_pages_from_en.py -- see that tool's original
# docstring (preserved below) for the detailed design rationale.
# ==========================================================================
"""
Never touches the EN file. Aligns the RU file's structural "skeleton"
(headings, anchors, delimited blocks, option/flag terms, code lines) to EN's,
and copies in new or changed EN lines verbatim (left untranslated) wherever
RU has nothing corresponding yet. Existing RU prose is never rewritten or
removed -- only technical tokens that must be byte-identical across
languages (flag names, code/command lines, include paths, ids) are corrected
when they've drifted (e.g. a stale `plpythonu` left behind after EN moved to
`plpython3u`).
"""

EN_MARK = "en/modules/"
RU_MARK = "ru/modules/"

DELIM_RE = re.compile(r'^(?:-{2}|-{4,}|\.{4,}|={4,}|\*{4,}|\|={3,})$')
CODE_DELIM_RE = re.compile(r'^(?:-{4,}|\.{4,})$')

HEADING_RE = re.compile(r'^(=+)\s+\S')
ID_RE = re.compile(r'^\[#([\w-]+)]\s*$')
DOCATTR_RE = re.compile(r'^:([\w-]+):')
ATTR_RE = re.compile(r'^\[[^\[\]].*]\s*$')
INCLUDE_RE = re.compile(r'^include::')
BLOCKTITLE_RE = re.compile(r'^\.[^.\s]')
TERM_RE = re.compile(r'^(\[\[[\w-]+])?(.+)::\s*$')
ALL_CAPS_TERM_RE = re.compile(r'^[A-Z][A-Z0-9_]*(\s*,\s*[A-Z][A-Z0-9_]*)*$')
XREF_ITEM_RE = re.compile(r'^[*.]+\s*xref:([^\[]+)\[[^\]]*]\s*$')

CELL_KEY_RE = re.compile(r'^\|([A-Z][A-Z0-9_]+)\b(.*)$')
CELL_KEY_PLACEHOLDER_RE = re.compile(r'''^\s*['"]?<''')
CELL_KEY_CAPS_ONLY_RE = re.compile(r'^[A-Z0-9_,\s]+$')
CELL_LITERAL_RE = re.compile(r'^\|([a-z][a-z0-9_]*|-+)$')


def _is_cell_key(line):
    if CELL_LITERAL_RE.match(line):
        return True
    m = CELL_KEY_RE.match(line)
    if not m:
        return False
    rest = m.group(2)
    if not rest.strip():
        return True
    if CELL_KEY_PLACEHOLDER_RE.match(rest):
        return True
    if '\\|' in rest:
        return True
    if CELL_KEY_CAPS_ONLY_RE.match(rest):
        return True
    return False


COMMENT_IN_CODE_RE = re.compile(r'^\s*(#|--|//)\s')
STALE_MARK_RE = re.compile(r'^// STALE VERSION:')
ORPHAN_MARK_RE = re.compile(r'^// POSSIBLY ORPHANED:')
COMMENT_LINE_RE = re.compile(r'^//')
SYNC_CYRILLIC_RE = re.compile(r'[Ѐ-ӿ]')

FORCE_SYNC_TYPES = {"DELIM", "ID", "ATTR", "INCLUDE", "TERM", "CODE", "CONT", "CELLKEY"}


def sync_classify(line, stack):
    stripped = line.strip()

    if STALE_MARK_RE.match(stripped):
        return ("STALEMARK",)
    if ORPHAN_MARK_RE.match(stripped):
        return ("ORPHANMARK",)

    if DELIM_RE.match(stripped):
        if stack and stack[-1] == stripped:
            stack.pop()
        else:
            stack.append(stripped)
        return ("DELIM", stripped)

    if stack and CODE_DELIM_RE.match(stack[-1]):
        if stripped == "":
            return ("BLANK",)
        if COMMENT_IN_CODE_RE.match(line):
            return ("COMMENT",)
        return ("CODE", line)

    if COMMENT_LINE_RE.match(stripped):
        return ("COMMENT",)

    if stripped == "":
        return ("BLANK",)

    if stripped == "+":
        return ("CONT", "+")

    m = HEADING_RE.match(line)
    if m:
        return ("HEADING", len(m.group(1)))

    m = ID_RE.match(line)
    if m:
        return ("ID", m.group(1))

    m = DOCATTR_RE.match(line)
    if m:
        return ("DOCATTR", m.group(1))

    if ATTR_RE.match(line):
        return ("ATTR", line)

    if INCLUDE_RE.match(line):
        return ("INCLUDE", line)

    m = XREF_ITEM_RE.match(line)
    if m:
        return ("XREFITEM", m.group(1))

    if BLOCKTITLE_RE.match(line):
        return ("BLOCKTITLE",)

    if not SYNC_CYRILLIC_RE.search(line) and _is_cell_key(line):
        return ("CELLKEY", stripped)

    m = TERM_RE.match(line)
    if m:
        content = m.group(2).strip()
        if content.startswith(("-", "`")) or ALL_CAPS_TERM_RE.match(content):
            return ("TERM", line)
        return ("TERMX",)

    return ("PROSE",)


def sync_signatures(lines):
    stack = []
    return [sync_classify(line, stack) for line in lines]


GENERIC_TYPES = {"PROSE", "COMMENT", "HEADING", "BLOCKTITLE", "TERMX"}


def matching_signatures(sigs, side):
    out = []
    for idx, sig in enumerate(sigs):
        if sig[0] in GENERIC_TYPES:
            out.append((sig[0], side, idx))
        else:
            out.append(sig)
    return out


def _sync_pair(en_l, ru_l, sig_type, replaced, en_idx, force_synced):
    if sig_type in FORCE_SYNC_TYPES and en_l != ru_l and not SYNC_CYRILLIC_RE.search(ru_l):
        replaced.append((ru_l, en_l))
        force_synced.add(en_idx)
        return en_l
    return ru_l


def _front_pair_and_append(en_slice, ru_slice, en_sig_slice, ru_sig_slice, out, inserted, replaced, pairs, en_base, ru_base, force_synced, orphaned):
    ei = ri = 0
    while ei < len(en_slice) and ri < len(ru_slice):
        if ru_sig_slice[ri][0] in ("STALEMARK", "ORPHANMARK"):
            out.append(ru_slice[ri])
            ri += 1
            continue
        en_type, ru_type = en_sig_slice[ei][0], ru_sig_slice[ri][0]
        if en_type != ru_type:
            en_rest_types = [t[0] for t in en_sig_slice[ei:]]
            ru_rest_types = [t[0] for t in ru_sig_slice[ri:]]
            if len(en_rest_types) > len(ru_rest_types) and en_rest_types[-len(ru_rest_types):] == ru_rest_types:
                prefix_len = len(en_rest_types) - len(ru_rest_types)
                extra = en_slice[ei:ei + prefix_len]
                out.extend(extra)
                inserted.append(extra)
                ei += prefix_len
                continue
            if len(ru_rest_types) > len(en_rest_types) and ru_rest_types[-len(en_rest_types):] == en_rest_types:
                prefix_len = len(ru_rest_types) - len(en_rest_types)
                extra = ru_slice[ri:ri + prefix_len]
                start_pos = len(out)
                out.extend(extra)
                orphaned.append((start_pos, extra))
                ri += prefix_len
                continue
            start_pos = len(out)
            out.append(ru_slice[ri])
            orphaned.append((start_pos, [ru_slice[ri]]))
            out.append(en_slice[ei])
            inserted.append([en_slice[ei]])
            ei += 1
            ri += 1
            continue
        out.append(_sync_pair(en_slice[ei], ru_slice[ri], en_type, replaced, en_base + ei, force_synced))
        pairs.append((en_base + ei, ru_base + ri))
        ei += 1
        ri += 1
    if ei < len(en_slice):
        extra = en_slice[ei:]
        out.extend(extra)
        inserted.append(extra)
    if ri < len(ru_slice):
        extra = ru_slice[ri:]
        start_pos = len(out)
        out.extend(extra)
        orphaned.append((start_pos, extra))


def _align_replace_span(en_slice, ru_slice, en_sig_slice, ru_sig_slice, out, inserted, replaced, pairs, en_base, ru_base, force_synced, orphaned):
    en_types = matching_signatures(en_sig_slice, "EN")
    ru_types = matching_signatures(ru_sig_slice, "RU")
    sm2 = difflib.SequenceMatcher(a=en_types, b=ru_types, autojunk=False)

    for tag, i1, i2, j1, j2 in sm2.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                out.append(_sync_pair(
                    en_slice[i1 + k], ru_slice[j1 + k], en_sig_slice[i1 + k][0], replaced,
                    en_base + i1 + k, force_synced,
                    ))
                pairs.append((en_base + i1 + k, ru_base + j1 + k))
        elif tag == "delete":
            new_lines = en_slice[i1:i2]
            out.extend(new_lines)
            inserted.append(new_lines)
        elif tag == "insert":
            extra = ru_slice[j1:j2]
            start_pos = len(out)
            out.extend(extra)
            orphaned.append((start_pos, extra))
        elif tag == "replace":
            _front_pair_and_append(
                en_slice[i1:i2], ru_slice[j1:j2], en_sig_slice[i1:i2], ru_sig_slice[j1:j2],
                out, inserted, replaced, pairs, en_base + i1, ru_base + j1, force_synced, orphaned,
                                                )


def sync_merge(en_lines, ru_lines, pins=None):
    en_sigs = sync_signatures(en_lines)
    ru_sigs = sync_signatures(ru_lines)
    if pins:
        for en_idx, ru_idx in pins.items():
            if en_sigs[en_idx][0] in GENERIC_TYPES and ru_sigs[ru_idx][0] in GENERIC_TYPES:
                en_sigs[en_idx] = ("PINNED", ru_idx)
                ru_sigs[ru_idx] = ("PINNED", ru_idx)
    en_match = matching_signatures(en_sigs, "EN")
    ru_match = matching_signatures(ru_sigs, "RU")
    sm = difflib.SequenceMatcher(a=en_match, b=ru_match, autojunk=False)

    out = []
    inserted = []
    replaced = []
    pairs = []
    force_synced = set()
    orphaned = []

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            out.extend(ru_lines[j1:j2])
            for k in range(i2 - i1):
                pairs.append((i1 + k, j1 + k))
        elif tag == "delete":
            new_lines = en_lines[i1:i2]
            out.extend(new_lines)
            inserted.append(new_lines)
        elif tag == "insert":
            extra = ru_lines[j1:j2]
            start_pos = len(out)
            out.extend(extra)
            orphaned.append((start_pos, extra))
        elif tag == "replace":
            _align_replace_span(
                en_lines[i1:i2], ru_lines[j1:j2], en_sigs[i1:i2], ru_sigs[j1:j2],
                out, inserted, replaced, pairs, i1, j1, force_synced, orphaned,
            )

    return out, inserted, replaced, pairs, force_synced, orphaned


def _content_diff_pins(old_en_lines, new_en_lines, ru_lines):
    """Lines unchanged between the EN file's previous and current revision
    can shift position when new content is inserted elsewhere -- e.g. a new
    bullet added before an existing one in an anchor-free list. sync_merge
    then has nothing but raw position to align RU against, and can pair the
    shifted-but-unchanged EN line with the wrong RU line (see pg_depend's
    PARTITION_PRI bullet landing on the pre-existing PIN bullet's RU text).

    Since old-EN-vs-new-EN is a same-language exact-text diff, it can find
    that unchanged content with certainty. Combined with a baseline
    old-EN-to-RU alignment (RU should already mirror old EN structurally
    from the last successful sync), this recovers new-EN-index -> RU-index
    pins for content sync_merge would otherwise have to guess about."""
    if not old_en_lines or not ru_lines:
        return {}
    baseline_pairs = sync_merge(old_en_lines, ru_lines)[3]
    old_to_ru = dict(baseline_pairs)
    pins = {}
    used_ru = set()
    sm = difflib.SequenceMatcher(a=old_en_lines, b=new_en_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "equal":
            continue
        for k in range(i2 - i1):
            ru_idx = old_to_ru.get(i1 + k)
            if ru_idx is not None and ru_idx not in used_ru:
                pins[j1 + k] = ru_idx
                used_ru.add(ru_idx)
    return pins


def _sync_merge_safe(en_lines, ru_lines, old_en_lines):
    """Runs the plain structural merge first, and only reaches for the
    old-EN-diff pins (see _content_diff_pins) when there's actually
    something to fix. Skipping pins on an already-clean file matters
    because the pins' baseline old-EN<->RU alignment assumes RU still
    mirrors old EN -- if RU was already hand-updated ahead of the tool
    (e.g. a manual fix applied before re-running --sync), that assumption
    breaks and pins can misalign a file that was already fine."""
    plain = sync_merge(en_lines, ru_lines)
    if plain[0] == ru_lines or not old_en_lines:
        return plain
    pins = _content_diff_pins(old_en_lines, en_lines, ru_lines)
    if not pins:
        return plain
    return sync_merge(en_lines, ru_lines, pins=pins)


HUNK_RE = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@')


def _last_commit_touching(path: Path):
    result = subprocess.run(
        ["git", "log", "-1", "--format=%H", "--", str(path)],
        capture_output=True, text=True,
    )
    sha = result.stdout.strip()
    return sha or None


def _git_show(ref: str, path: Path):
    result = subprocess.run(
        ["git", "show", f"{ref}:{path.as_posix()}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout


def _git_diff_hunks(ref: str, path: Path):
    result = subprocess.run(
        ["git", "diff", "--unified=0", ref, "--", str(path)],
        capture_output=True, text=True,
    )
    hunks = []
    current = None
    for line in result.stdout.splitlines():
        m = HUNK_RE.match(line)
        if m:
            if current:
                hunks.append(current)
            old_start, old_count, new_start, new_count = m.groups()
            current = {
                "old_count": int(old_count) if old_count is not None else 1,
                "new_start": int(new_start),
                "new_count": int(new_count) if new_count is not None else 1,
                "minus": [], "plus": [],
            }
        elif current is not None and line.startswith("-") and not line.startswith("---"):
            current["minus"].append(line[1:])
        elif current is not None and line.startswith("+") and not line.startswith("+++"):
            current["plus"].append(line[1:])
    if current:
        hunks.append(current)
    return hunks


def find_reworded_lines(en_path: Path, ru_path: Path, en_lines, ru_lines, pairs, force_synced, ref: str = None):
    ref = ref or _last_commit_touching(ru_path)
    if not ref:
        return None, []

    ru_touched = set()
    for h in _git_diff_hunks(ref, ru_path):
        if h["new_count"] == 0:
            continue
        ru_touched.update(range(h["new_start"], h["new_start"] + h["new_count"]))

    hunks = _git_diff_hunks(ref, en_path)
    en_to_ru = dict(pairs)
    findings = []
    for h in hunks:
        if h["old_count"] == 0 or h["new_count"] == 0:
            continue
        for k in range(h["new_count"]):
            new_lineno = h["new_start"] + k
            en_idx = new_lineno - 1
            if en_idx in force_synced:
                continue
            ru_idx = en_to_ru.get(en_idx)
            if ru_idx is None:
                continue
            if (ru_idx + 1) in ru_touched:
                continue
            old_en = h["minus"][k] if k < len(h["minus"]) else None
            findings.append({
                "lineno": new_lineno,
                "old_en": old_en,
                "new_en": en_lines[en_idx],
                "ru_lineno": ru_idx + 1,
                "ru": ru_lines[ru_idx],
            })
    return ref, findings


def apply_stale_markers(ru_lines, reworded):
    marked = list(ru_lines)
    count = 0
    for f in sorted(reworded, key=lambda f: f["ru_lineno"], reverse=True):
        ru_idx = f["ru_lineno"] - 1
        if marked[ru_idx] == f["new_en"]:
            continue
        old_ru = marked[ru_idx]
        marked[ru_idx] = f["new_en"]
        marked.insert(ru_idx + 1, f"// STALE VERSION: {old_ru}")
        count += 1
    return marked, count


ORPHAN_MARKER_TEXT = (
    "// POSSIBLY ORPHANED: no EN counterpart found nearby -- "
    "review whether this was intentionally removed upstream"
)


def _visible_orphan_offsets(block, already_reported):
    return [
        o for o, l in enumerate(block)
        if l.strip() and not STALE_MARK_RE.match(l.strip())
           and not ORPHAN_MARK_RE.match(l.strip())
           and not COMMENT_LINE_RE.match(l.strip()) and l not in already_reported
    ]


def apply_orphan_markers(ru_lines, orphaned, already_reported):
    marked = list(ru_lines)
    positions = set()
    for start_pos, block in orphaned:
        offsets = _visible_orphan_offsets(block, already_reported)
        if offsets:
            positions.add(start_pos + offsets[0])
    count = 0
    for pos in sorted(positions, reverse=True):
        if pos > 0 and ORPHAN_MARK_RE.match(marked[pos - 1].strip()):
            continue
        marked.insert(pos, ORPHAN_MARKER_TEXT)
        count += 1
    return marked, count


def ru_path_for(en_path: Path) -> Path:
    s = str(en_path)
    if EN_MARK not in s:
        sys.exit(f"error: path does not look like an EN page (missing '{EN_MARK}'): {en_path}")
    return Path(s.replace(EN_MARK, RU_MARK, 1))


def _resolve_page_stem(name_parts):
    """Every EN pages/partials .adoc file (across all discovered modules)
    whose content-relative path (.adoc stripped) ends with `name_parts` --
    the same suffix-matching convention --page's NAME already uses (see
    _page_allowed). Backs --sync's fallback when its argument (already
    validated to end in .adoc) isn't an existing path: lets --sync take a
    bare filename like "analyzedb.adoc", or a directory-qualified one like
    "reference/gp_toolkit/gp_ao_diskquota_no_perm_map.adoc" to disambiguate
    a same-named file in two different directories, instead of always
    requiring the full relative path."""
    matches = []
    for _, en_root, _ in module_roots():
        for subdir in ("pages", "partials"):
            for f in _iter_files(en_root / subdir, ".adoc"):
                relparts_stem = _content_relparts_stem(f)
                if relparts_stem is not None and _ends_with_parts(relparts_stem, name_parts):
                    matches.append(f)
    return matches


def run_sync(en_file: str, dry_run: bool):
    if not en_file.endswith(".adoc"):
        sys.exit(f"error: --sync {en_file!r} must end with .adoc -- "
                  f"AsciiDoc/Antora has no separate topic-id, the filename is the identifier.")
    en_path = Path(en_file)
    if not en_path.is_file():
        # Not an existing path -- try resolving it as a --page-style bare or
        # directory-qualified filename (e.g. "analyzedb.adoc" or
        # "reference/gp_toolkit/gp_ao_diskquota_no_perm_map.adoc") against
        # the discovered EN pages/partials instead of immediately failing.
        name_parts = tuple(p for p in en_file[:-len(".adoc")].split("/") if p)
        matches = _resolve_page_stem(name_parts)
        if len(matches) == 1:
            en_path = matches[0]
        elif len(matches) > 1:
            listing = "\n".join(f"  {m}" for m in sorted(str(m) for m in matches))
            sys.exit(f"error: --sync {en_file!r} matches multiple files -- pass a full path to disambiguate:\n{listing}")
        else:
            sys.exit(f"error: not a file, and no page/partial named {en_file!r}: {en_path}")

    ru_path = ru_path_for(en_path)
    en_lines = (_read_text(en_path) or "").splitlines()
    ru_existed = ru_path.is_file()

    if ru_existed:
        ru_lines = (_read_text(ru_path) or "").splitlines()
    else:
        print(f"NOTE: {ru_path} does not exist yet -- creating it as a full (untranslated) copy of EN.")
        ru_lines = []

    ref = None
    old_en_lines = None
    if ru_existed:
        ref = _last_commit_touching(ru_path)
        if ref:
            old_en_text = _git_show(ref, en_path)
            if old_en_text is not None:
                old_en_lines = old_en_text.splitlines()

    merged, inserted, replaced, pairs, force_synced, orphaned = _sync_merge_safe(en_lines, ru_lines, old_en_lines)

    reworded, marked = [], 0
    if ru_existed:
        ref, reworded = find_reworded_lines(en_path, ru_path, en_lines, ru_lines, pairs, force_synced, ref=ref)
        if reworded:
            ru_lines_marked, marked = apply_stale_markers(ru_lines, reworded)
            if marked:
                merged, inserted, replaced, pairs, force_synced, orphaned = _sync_merge_safe(en_lines, ru_lines_marked, old_en_lines)

    already_reported = set()
    for f in reworded:
        already_reported.add(f["new_en"])
        already_reported.add(f["ru"])
        if f["old_en"] is not None:
            already_reported.add(f["old_en"])
    for old, new in replaced:
        already_reported.add(old)
        already_reported.add(new)
    for block in inserted:
        already_reported.update(block)

    merged, orphan_marked = apply_orphan_markers(merged, orphaned, already_reported)

    structurally_synced = merged == ru_lines

    if structurally_synced:
        print(f"OK: {ru_path} already matches the EN structure/content; nothing to do structurally.")
    elif dry_run:
        diff = difflib.unified_diff(
            ru_lines, merged,
            fromfile=str(ru_path), tofile=str(ru_path) + " (proposed)",
            lineterm="",
        )
        print("\n".join(diff))
    else:
        ru_path.parent.mkdir(parents=True, exist_ok=True)
        ru_path.write_text("\n".join(merged) + "\n", encoding="utf-8")
        print(f"Updated {ru_path}")

    real_inserted = []
    for block in inserted:
        visible = [l for l in block if not COMMENT_LINE_RE.match(l.strip())]
        if any(l.strip() for l in visible):
            real_inserted.append(visible)

    if real_inserted:
        total = sum(len(b) for b in real_inserted)
        print(f"\nInserted {total} new line(s) from EN across {len(real_inserted)} block(s), left untranslated:")
        for block in real_inserted:
            for l in block:
                print(f"  + {l}")
            print()

    if replaced:
        print(f"Synced {len(replaced)} stale technical line(s) (flags/code/ids/paths) to match EN:")
        for old, new in replaced:
            print(f"  - {old}")
            print(f"  + {new}")

    if marked:
        print(f"\nMarked {marked} reworded line(s) (EN wording changed since {ref[:10]} on lines the aligner")
        print("otherwise left untouched): new EN sentence copied in, old RU preserved as a `// STALE VERSION:` comment:")
        for f in reworded:
            if f["ru"] == f["new_en"]:
                continue
            print(f"\n  EN:{f['lineno']} / RU:{f['ru_lineno']}")
            print(f"    {f['new_en']}")
            print(f"    // STALE VERSION: {f['ru']}")

    if ru_existed and ref is None:
        print(f"\nNOTE: no git history found for {ru_path}; skipped the reworded-line check.")

    real_orphaned = []
    for _, block in orphaned:
        visible = [block[o] for o in _visible_orphan_offsets(block, already_reported)]
        if visible:
            real_orphaned.append(visible)
    if real_orphaned:
        total = sum(len(b) for b in real_orphaned)
        print(f"\nPOSSIBLY ORPHANED: {total} RU line(s) across {len(real_orphaned)} block(s) have no EN counterpart")
        print("anywhere nearby (left in place, not deleted -- review whether EN removed this on purpose).")
        if orphan_marked:
            print(f"Marked {orphan_marked} of them with a `// POSSIBLY ORPHANED:` comment right before the block, "
                  "so it's visible directly in the file:")
        for block in real_orphaned:
            for l in block:
                print(f"  ? {l}")
            print()

    if real_inserted or replaced or marked:
        print("\nNext: run ./docs_tool.py --check-pages-translation to locate the newly untranslated lines for translation.")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _discover_page_completions():
    """(names, dirs) -- every EN/RU pages/partials .adoc filename, and
    every content-relative directory (see _content_relpath) at every
    nesting level, across all discovered modules. Shared source for both
    --page (accepts files and directories) and --sync (files only) tab
    completion. A no-op unless argcomplete is installed and active (see
    Tab completion in the README); harmless to always attach.

    Each directory candidate carries a trailing "/" (e.g. "reference/",
    not "reference") -- --page's own parsing tolerates either form (a
    trailing slash is stripped, see main()), but the completer needs it:
    without it, no candidate starts with what the shell has typed the
    moment the user types the "/" themselves (real path completion always
    appends it too, for the same reason), so completion would go dead
    right after completing a directory instead of continuing into it.

    Each file gets one candidate per suffix-length qualified form, from
    the bare filename up to its full content-relative path (e.g. for
    reference/gp_toolkit/gp_ao_diskquota_no_perm_map.adoc:
    "gp_ao_diskquota_no_perm_map.adoc", then
    "gp_toolkit/gp_ao_diskquota_no_perm_map.adoc", then the full path) --
    matching every qualified form --page/--sync's own suffix-matching (see
    _page_allowed/_resolve_page_stem) actually accepts, so completion can
    keep going after a directory prefix to narrow down to one file, e.g.
    when the bare filename alone would be ambiguous across directories."""
    names = set()
    dirs = set()
    for _, en_root, ru_root in module_roots():
        for root in (en_root, ru_root):
            for subdir in ("pages", "partials"):
                for f in _iter_files(root / subdir, ".adoc"):
                    rel = _content_relpath(f)
                    if rel is None:
                        continue
                    parts = rel.parts[:-1]
                    for i in range(len(parts), -1, -1):
                        names.add("/".join(parts[i:] + (f.name,)))
                    for i in range(1, len(parts) + 1):
                        dirs.add("/".join(parts[:i]) + "/")
    return names, dirs


def _complete_page_name(**kwargs):
    """argcomplete completer for --sync: every EN/RU pages/partials .adoc
    filename (bare, no path -- matching what EN_FILE accepts), since
    --sync's single-file target can't be a directory."""
    names, _ = _discover_page_completions()
    return sorted(names)


def _complete_page_or_dir_name(**kwargs):
    """argcomplete completer for --page: filenames plus every
    content-relative directory, since --page can also scope a whole
    subtree (unlike --sync's single-file-only target)."""
    names, dirs = _discover_page_completions()
    return sorted(names | dirs)


def _complete_family(prefix="", **kwargs):
    """argcomplete completer for the 'check' FAMILY positional. Returns an
    ordered {name: description} map so the shell lists the families in
    writing-quality-pyramid order (chars -> links), not sorted, and shows
    each one's one-liner instead of a shared blob."""
    return {k: v for k, v in FAMILY_DESC.items() if k.startswith(prefix)}


if argcomplete:
    class _CheckCompletionFinder(argcomplete.CompletionFinder):
        """The --<rule> flags are registered with help=SUPPRESS (so `check -h`
        stays short), which also hides them from completion. Re-offer them
        here, but only the ones that belong to the single family already on
        the line -- `check l10n <TAB>` should suggest --lines/--structure/...,
        not --no-yo."""

        def collect_completions(self, active_parsers, parsed_args, cword_prefix):
            comps = super().collect_completions(active_parsers, parsed_args, cword_prefix)
            fams = [f for f in (getattr(parsed_args, "families", None) or []) if f in FAMILIES]
            if len(fams) == 1:
                for rule, targets in FAMILIES[fams[0]].items():
                    flag = "--" + rule
                    if flag.startswith(cword_prefix) and flag not in comps:
                        comps.append(flag)
                        primary = targets.get("pages") or next(iter(targets.values()))
                        self._display_completions[flag] = SUMMARIES.get(primary, "")
            return comps
else:
    _CheckCompletionFinder = None


def build_parser():
    parser = argparse.ArgumentParser(
        prog="docs_tool.py",
        description="Legacy flag interface (--check-<name>, --all-checks, --sync, "
                    "--list-checks, --list-modules). Still supported. The current "
                    "surface is 'docs_tool.py check <family>' -- run 'docs_tool.py --help' "
                    "(no other args) or 'docs_tool.py list' for it.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    check_group = parser.add_argument_group("checks")
    for name in CHECKS:
        check_group.add_argument(f"--check-{name}", action="store_true", help=argparse.SUPPRESS)

    parser.add_argument("--all-checks", action="store_true", help="Run every check.")
    parser.add_argument("--list-checks", action="store_true", help="List available --check-* flags and exit.")
    parser.add_argument("--list-modules", action="store_true",
                        help="List every discovered module (under en/modules/ and ru/modules/) and exit.")
    parser.add_argument("--verbose", action="store_true",
                        help="Verbose mode: show full diffs on the parity checks (instead of "
                             "a truncated preview) and per-hit detail on the heuristic checks.")
    page_action = parser.add_argument("--page", action="append", metavar="NAME",
                        help="Limit the per-file en/ru checks (translation, line-parity, "
                             "structure-parity, link-parity, literal-parity, no-cyrillic, "
                             "no-unicode-dashes, no-yo, no-invisible-chars, ru-latin-homoglyphs, "
                             "table-cell-periods, file-path-italics, terminology) to page(s)/partial(s) whose filename "
                             "matches NAME, e.g. --page resource_groups.adoc -- NAME must end with .adoc "
                             "(AsciiDoc/Antora has no separate topic-id, the filename is the identifier). "
                             "A same-named file in two different directories can be disambiguated by "
                             "qualifying NAME with as much of the trailing directory path as needed, e.g. "
                             "--page reference/gp_toolkit/gp_ao.adoc or just --page gp_toolkit/gp_ao.adoc "
                             "(matched by the path relative to pages/partials ending with NAME; the module "
                             "itself is never part of the match, same as the directory form below). "
                             "Alternatively, a NAME not ending in .adoc scopes every page/partial under "
                             "that content-relative directory instead, recursively, in any module, e.g. "
                             "--page reference/sql_commands matches every file under any module's "
                             "pages/reference/sql_commands/ or partials/reference/sql_commands/. "
                             "Repeatable, and file/directory forms can be mixed. Pass the "
                             "special value UNCOMMITTED instead of a name to scope to whatever "
                             ".adoc files currently have uncommitted changes (staged, unstaged, "
                             "or untracked) per `git status` -- handy in a pre-commit hook. "
                             "Whole-site checks (broken-refs, orphaned, nav/structure parity of "
                             "nav.adoc) are unaffected and always scan everything. Optional -- "
                             "omit to check the whole site as before.")
    page_action.completer = _complete_page_or_dir_name
    parser.add_argument("--external-root", action="append", metavar="NAME=PATH",
                        help="With --check-pages-broken-refs: resolve xref:/include:: targets "
                             "against another Antora component's repo checked out locally, e.g. "
                             "--external-root ADCM=../docs-adcm. Repeatable. Without this, "
                             "references into a component that isn't part of this repo are left "
                             "unchecked rather than reported broken.")
    parser.add_argument("--glossary", action="append", metavar="PATH",
                        help="With --check-pages-terminology: an EN-term-to-RU-translation "
                             "glossary file (pipe-delimited, columns en|ru|ru_pattern|note -- "
                             "format documented in a *-glossary.psv file's own header) to check "
                             "pages/partials against. Repeatable -- entries from every file "
                             "passed are merged. If omitted, defaults to every *-glossary.psv "
                             "file found directly under the current directory.")
    _add_link_args(parser)

    sync_group = parser.add_argument_group("sync")
    sync_action = sync_group.add_argument("--sync", metavar="EN_FILE",
                            help="(beta) Align the RU counterpart of EN_FILE to match its current "
                                 "structure/content. EN_FILE must end with .adoc, and can be the full "
                                 "relative path (e.g. en/modules/ROOT/pages/foo.adoc) or, like --page NAME, "
                                 "just the bare filename (e.g. foo.adoc) -- resolved by searching all "
                                 "discovered modules' pages/partials, same as --page. A bare filename "
                                 "matching more than one file can be disambiguated the same way --page's "
                                 "can, by qualifying it with trailing directory path segments (e.g. "
                                 "reference/gp_toolkit/gp_ao.adoc), or by passing the full path instead. "
                                 "Heuristic aligner, not a semantic merge -- review its output before "
                                 "trusting it.")
    sync_action.completer = _complete_page_name
    sync_group.add_argument("--dry-run", action="store_true",
                            help="With --sync: print the diff instead of writing the RU file.")
    return parser


def _require_a_docs_tree():
    """Abort unless this looks like a docs repo root. Without it, running
    from the wrong directory scans nothing and every check reports "OK:" --
    a clean pass over content that was never read, which is worse than an
    error (same reasoning as the --external-root warning above)."""
    if discover_module_names():
        return
    sys.exit(f"error: no {EN_MODULES_ROOT}/ or {RU_MODULES_ROOT}/ found in "
             f"{Path.cwd()} -- run docs_tool from the docs repo root")


def _reject_unmatched_page_filters(page_args):
    """Abort on a --page NAME that matched no file. Running the remaining
    rules would report "OK:" for a page that was never read, and exit 0 --
    so a typo'd filename in a CI invocation would pass as a green run.
    A usage error (exit 2), like a bad --target: nothing was checked, so
    this is not a finding. UNCOMMITTED is exempt -- resolving to nothing
    there is the normal "no .adoc changes" case, handled by the caller."""
    explicit = [n for n in page_args if n != "UNCOMMITTED"]
    if not explicit:
        return
    matched_names, matched_dirs = set(), set()
    for _, en_root, ru_root in module_roots():
        for root in (en_root, ru_root):
            for subdir in ("pages", "partials"):
                for path in _iter_files(root / subdir, ".adoc"):
                    relparts = _content_relparts_stem(path)
                    if relparts is None:
                        continue
                    matched_names |= {n for n in _PAGE_FILTER["names"]
                                      if _ends_with_parts(relparts, n)}
                    matched_dirs |= {d for d in _PAGE_FILTER["dirs"]
                                     if relparts[:len(d)] == d}
    unmatched = []
    for name in explicit:
        if name.endswith(".adoc"):
            key = tuple(p for p in name[:-len(".adoc")].split("/") if p)
            hit = key in matched_names
        else:
            key = tuple(p for p in name.split("/") if p)
            hit = key in matched_dirs
        if not hit:
            unmatched.append(name)
    if unmatched:
        # Every unmatched value at once: a run with several --page typos
        # shouldn't take one re-run per typo to discover them all.
        for name in unmatched:
            kind = "file" if name.endswith(".adoc") else "file under that directory"
            print(f"error: --page {name} matched no {kind}", file=sys.stderr)
        print("aborting -- a --page value that matches nothing would report a "
              "clean run over a page that was never read", file=sys.stderr)
        sys.exit(2)


def _apply_page_filter(page_args):
    """Turn --page NAME values into the _PAGE_FILTER global (shared by both
    the legacy and the `check` surface). Exits 0 immediately if --page
    UNCOMMITTED resolved to nothing, same as before."""
    global _PAGE_FILTER
    if not page_args:
        return
    names, dirs = set(), set()
    for name in page_args:
        if name == "UNCOMMITTED":
            names |= {(s,) for s in _git_uncommitted_adoc_stems()}
        elif name.endswith(".adoc"):
            names.add(tuple(p for p in name[:-len(".adoc")].split("/") if p))
        else:
            dirs.add(tuple(p for p in name.split("/") if p))
    if not names and not dirs:
        print("OK: no uncommitted .adoc changes to check.")
        sys.exit(0)
    _PAGE_FILTER = {"names": names, "dirs": dirs}
    _reject_unmatched_page_filters(page_args)


def _run_selected(selected, verbose, glossary_paths, legacy_headers=False):
    """Run an ordered list of CHECKS keys, printing a header between them
    when more than one is selected. Loads the glossary lazily if a
    terminology check is in the set. Returns True if every check passed."""
    global GLOSSARY
    selected = list(selected)
    if "pages-terminology" in selected:
        paths = glossary_paths or _discover_default_glossaries()
        if not paths and len(selected) > 1:
            # Swept in as part of a multi-check run (a multi-family `check`,
            # `--all-checks`) with no glossary available -- skip it with a note
            # rather than aborting (a bare `check terms` still errors, in the check).
            print("note: skipping terminology check -- no --glossary and no "
                  "*-glossary.psv in the current directory", file=sys.stderr)
            selected = [k for k in selected if k != "pages-terminology"]
        else:
            if paths and not glossary_paths:
                print(f"info: --glossary not passed -- defaulting to discovered "
                      f"{', '.join(paths)}", file=sys.stderr)
            GLOSSARY = _load_glossary(paths)

    overall_ok = True
    for i, name in enumerate(selected):
        if len(selected) > 1:
            if i:
                print()
            # The `check` surface labels each section the way the user would
            # re-run it (rule ID + command); the legacy surface keeps naming
            # the --check-* flag that selected it.
            header = f"--check-{name}" if legacy_headers else \
                f"{RULE_IDS[name]}  {_check_command(name)}"
            print(f"=== {header} ===")
        if not CHECKS[name](verbose=verbose):
            overall_ok = False
    return overall_ok


def _main_legacy():
    """The pre-subcommand CLI: `docs_tool.py --check-<name> ...`,
    `--all-checks`, `--sync`, `--list-checks`, `--list-modules`. Still
    supported; `docs_tool.py check <family>` is the current surface (see
    docs/proposals/cli-redesign.md)."""
    global EXTERNAL_COMPONENTS
    parser = build_parser()
    if argcomplete and os.environ.get("_ARGCOMPLETE") == "1":
        # Blank out the SUPPRESS sentinel so it doesn't leak into the completion
        # listing as a fake description -- only touches argparse's in-memory
        # action objects during an actual completion request, so --help (which
        # relies on help=SUPPRESS to hide these from its output) is unaffected.
        for action in parser._actions:
            if action.help == argparse.SUPPRESS:
                action.help = None
        argcomplete.autocomplete(parser, print_suppressed=True)
    args = parser.parse_args()
    EXTERNAL_COMPONENTS = _load_external_components(args.external_root)
    _configure_links_from_args(args)

    if args.list_checks:
        for name in CHECKS:
            tag = " (beta)" if name in BETA_CHECKS else ""
            print(f"--check-{name}{tag}")
        if any(name in BETA_CHECKS for name in CHECKS):
            print("\n(beta): heuristic, not a real AsciiDoc parser -- treat findings as a "
                  "review list, not a hard failure.")
        return

    if args.list_modules:
        for name in discover_module_names():
            print(name)
        return

    if args.sync:
        _require_a_docs_tree()
        run_sync(args.sync, dry_run=args.dry_run)
        return

    selected = [k for k in CHECKS if k not in _NETWORK_ONLY_CHECKS] if args.all_checks else [
        name for name in CHECKS if getattr(args, f"check_{name.replace('-', '_')}")
    ]

    if not selected:
        parser.print_help()
        sys.exit(2)

    _require_a_docs_tree()
    _apply_page_filter(args.page)

    overall_ok = _run_selected(selected, args.verbose, args.glossary, legacy_headers=True)
    sys.exit(0 if overall_ok else 1)


# --------------------------------------------------------------------------
# `docs_tool check|show|list|sync` -- the family-based surface
# --------------------------------------------------------------------------

_V2_VERBS = ("check", "sync", "list", "show")


def _add_link_args(parser):
    """The `check links` knobs. Shared by both surfaces so
    `--check-links-external --timeout 5` works on the legacy one too."""
    parser.add_argument("--timeout", type=float, metavar="SECONDS",
                        help="Per-request timeout for 'check links' (default 10).")
    parser.add_argument("--offline", action="store_true",
                        help="'check links': list the external links found, don't fetch them.")
    parser.add_argument("--allow-domain", action="append", metavar="HOST",
                        help="'check links': skip this host (suffix match). Repeatable.")
    parser.add_argument("--link-cache", metavar="PATH",
                        help="'check links': cache results to PATH (7-day TTL) so reruns "
                             "skip re-fetching. Off by default -- every run is fresh.")
    parser.add_argument("--show-unverified", action="store_true",
                        help="'check links': also list the 403/429/5xx links, not just count them.")
    parser.add_argument("--insecure", action="store_true",
                        help="'check links': skip TLS certificate verification (and its note).")


def _configure_links_from_args(args):
    """Push the `check links` knobs into the module globals check_links_external
    reads (same pattern as EXTERNAL_COMPONENTS / GLOSSARY)."""
    global LINK_TIMEOUT, LINK_OFFLINE, LINK_ALLOW_DOMAINS
    global LINK_CACHE_PATH, LINK_INSECURE, LINK_SHOW_UNVERIFIED
    if getattr(args, "timeout", None) is not None:
        LINK_TIMEOUT = args.timeout
    LINK_OFFLINE = bool(getattr(args, "offline", False))
    LINK_ALLOW_DOMAINS = {d.lower() for d in (getattr(args, "allow_domain", None) or [])}
    LINK_CACHE_PATH = getattr(args, "link_cache", None) or None
    LINK_SHOW_UNVERIFIED = bool(getattr(args, "show_unverified", False))
    LINK_INSECURE = bool(getattr(args, "insecure", False))


def _build_v2_parser():
    p = argparse.ArgumentParser(
        prog="docs_tool.py",
        # The rule table is generated, not written out here: --help is where
        # people look for "what can this check and how do I run it", and a
        # hand-kept copy would drift from FAMILIES the first time a rule moved.
        description=__doc__.replace("%RULES%", _rules_table()),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="verb", required=True, metavar="{check,show,list,sync}")

    c = sub.add_parser("check", help="Run checks by family (e.g. 'check style --no-yo').",
                       epilog="Each family has --<rule> flags (e.g. --no-yo, --structure) "
                              "and, where relevant, --target NAME. Run 'docs_tool list' "
                              "for the full map, or 'docs_tool show <rule>' for one "
                              "rule's rationale and worked examples.")
    fam = c.add_argument("families", nargs="+", metavar="FAMILY",
                   choices=list(FAMILIES),
                   help="one or more of: chars, markup, refs, style, terms, l10n, links. "
                        "--<rule> flags need exactly one family.")
    fam.completer = _complete_family
    for rule in _ALL_RULES:
        c.add_argument(f"--{rule}", dest=rule.replace("-", "_"), action="store_true",
                       help=argparse.SUPPRESS)
    c.add_argument("--target", metavar="NAME",
                   choices=_SCAN_TARGETS + ("all",),
                   help="Restrict to one scan target: %s, or 'all' "
                        "(default: pages)." % ", ".join(_SCAN_TARGETS))
    c.add_argument("--verbose", action="store_true",
                   help="Full diffs / per-hit detail on the heuristic checks; for "
                        "'check links', every referencing page plus the 403/429/5xx list.")
    pa = c.add_argument("--page", action="append", metavar="NAME",
                        help="Limit per-file EN/RU checks to matching page(s)/"
                             "partial(s); 'UNCOMMITTED' for the current git diff. "
                             "Repeatable. refs always scans site-wide.")
    pa.completer = _complete_page_or_dir_name
    c.add_argument("--external-root", action="append", metavar="NAME=PATH",
                   help="Resolve cross-repo refs against a local checkout, e.g. "
                        "--external-root ADCM=../docs-adcm. Repeatable.")
    c.add_argument("--glossary", action="append", metavar="PATH",
                   help="Glossary file(s) for 'check terms'. Repeatable. "
                        "Defaults to *-glossary.psv in the current directory.")
    _add_link_args(c)

    sh = sub.add_parser("show", help="One rule's rationale + examples, or 'show all' for every rule.")
    sh.add_argument("name", metavar="RULE", help="e.g. 'no-yo', 'ST01', or 'all'.")

    ls = sub.add_parser("list", help="The family/rule map (also 'list rules' / 'list targets').")
    ls.add_argument("what", nargs="?", metavar="[rules|targets]",
                    help="Omit for the family tree; 'rules' / 'targets' for flat lists.")

    s = sub.add_parser("sync", help="Align a RU page to its EN counterpart (beta).")
    sy = s.add_argument("file", metavar="EN_FILE",
                        help="EN .adoc file: full path or bare filename (resolved like --page).")
    sy.completer = _complete_page_name
    s.add_argument("--dry-run", action="store_true",
                   help="Print the diff instead of writing the RU file.")

    if argcomplete and os.environ.get("_ARGCOMPLETE") == "1":
        _CheckCompletionFinder()(p)
    return p


def _rule_of(key):
    """The rule name (e.g. 'no-yo') that resolves to a CHECKS key, or
    the key itself if none maps to it directly."""
    for rules in FAMILIES.values():
        for rule, targets in rules.items():
            if key in targets.values():
                return rule
    return key


def _resolve_check_name(name):
    """A rule name, a legacy CHECKS key, or a rule ID -> CHECKS key
    (None if it matches nothing)."""
    fam = _family_of(name)
    if fam:
        targets = FAMILIES[fam][name]
        return targets.get("pages") or next(iter(targets.values()))
    if name in CHECKS:
        return name
    return _ID_TO_KEY.get(name.upper())


def _tier_of(fam):
    return next((t for t, fams in TIERS.items() if fam in fams), None)


def _target_of(key):
    """The non-default scan target `--target` needs to reach a specific
    CHECKS key (e.g. 'examples' for examples-orphaned), or None. Only
    rules with more than one target ever need it -- a single-target
    rule like l10n --examples is unambiguous on its own."""
    for rules in FAMILIES.values():
        for targets in rules.values():
            if key not in targets.values() or len(targets) == 1:
                continue
            for t, k in targets.items():
                if k == key and t != "pages":
                    return t
    return None


def _check_command(key):
    """The `check ...` command that runs one CHECKS key -- 'check terms' for
    a single-rule family, else 'check <fam> --<rule> [--target X]'."""
    rule = _rule_of(key)
    fam = _family_of(rule)
    tgt = _target_of(key)
    if fam and len(FAMILIES[fam]) == 1:
        base = f"check {fam}"
    else:
        base = f"check {fam} --{rule}"
    return base + (f" --target {tgt}" if tgt else "")


def _v2_list(what):
    if what == "rules":
        rows = sorted((RULE_IDS[k], _check_command(k),
                       "  [beta]" if k in BETA_CHECKS else "") for k in CHECKS)
        for rid, cmd, beta in rows:
            print(f"{rid}  {cmd}{beta}")
        return
    if what == "targets":
        print("--target values (for rules that scan more than one place):\n")
        for t, desc in TARGET_DESC.items():
            print(f"  {t:<10}{desc}")
        return
    if what == "modules":
        print("list: module discovery moved -- use 'docs_tool.py --list-modules'", file=sys.stderr)
        sys.exit(2)
    if what not in (None, "families"):
        hint = f" -- did you mean 'docs_tool show {what}'?" if _resolve_check_name(what) else ""
        print(f"list: unknown argument '{what}' (expected: rules, targets){hint}", file=sys.stderr)
        sys.exit(2)

    for i, (fam, rules) in enumerate(FAMILIES.items()):
        if i:
            print()
        disp = _TIER_DISPOSITION.get(_tier_of(fam), "?")
        print(f"{fam}  —  {FAMILY_DESC.get(fam, '')}  (suggest: {disp})")
        for rule, targets in rules.items():
            primary = targets.get("pages") or next(iter(targets.values()))
            beta = "  [beta]" if primary in BETA_CHECKS else ""
            print(f"  {RULE_IDS[primary]:<5}--{rule:<21}{SUMMARIES[primary]}{beta}")
            if len(targets) > 1:
                others = [t for t in targets if t != "pages"]
                oids = sorted({RULE_IDS[targets[t]] for t in others})
                note = f"  [{oids[0]}–{oids[-1]}]" if len(oids) > 1 else f"  [{oids[0]}]"
                print(f"{'':>7}{'':<21}  --target {'|'.join(others)}{note}")
    print()
    print("check <family> [<family> ...]  run those families")
    print("check <family> --<rule>        run just one rule")
    print("show <rule|rule-id>            one rule's rationale + examples")
    print("show all                       every rule, with examples")
    print("list rules | list targets      flat rule list · --target values")
    print()
    print("'suggest:' is advice for your pre-commit hook, not something the tool")
    print("enforces -- a run exits 0 clean / 1 on findings (for links, only a dead")
    print("link counts; redirects and unreachable hosts are reported, not failed).")


def _print_examples(key, indent="  "):
    examples = _rule_examples(key)
    # Label first, command second. Trailing comments would have to be padded
    # past the longest command -- and `refs --orphaned --target tags
    # --external-root ADCM=../docs-adcm` is 85 characters on its own, which
    # drags every sibling line off the edge of the terminal. A short leading
    # label column aligns no matter how long the commands get.
    width = max(len(note) for _, note in examples)
    for cmd, note in examples:
        print(f"{indent}{note:<{width}}   {cmd}")


def _v2_show_all():
    """Every rule with its examples, in rule-ID order. The per-rule docstrings
    are left out on purpose: 22 full rationales is a document, not a reference
    you scan -- `show <rule>` is there when you want one of them."""
    for i, (key, rid) in enumerate(sorted(RULE_IDS.items(), key=lambda kv: kv[1])):
        if i:
            print()
        beta = "  [beta]" if key in BETA_CHECKS else ""
        print(f"{rid}  {SUMMARIES.get(key, '')}{beta}")
        _print_examples(key, indent="      ")
    print("\nshow <rule|rule-id>  for one rule's full rationale, exceptions, and "
          "known false positives")


def _v2_show(name):
    if name == "all":
        return _v2_show_all()
    key = _resolve_check_name(name)
    if key is None:
        print(f"unknown rule: {name}  (run 'docs_tool list' for the map, or "
              f"'docs_tool show all' for every rule with examples)", file=sys.stderr)
        sys.exit(2)
    fam = _family_of(_rule_of(key)) or "?"
    disp = _TIER_DISPOSITION.get(_tier_of(fam), "?")
    beta = "  [beta -- treat findings as a review list, not a hard gate]" if key in BETA_CHECKS else ""
    print(f"{RULE_IDS[key]}  {_check_command(key)}   (suggest: {disp}){beta}\n")
    print(SUMMARIES.get(key, ""))
    doc = (CHECKS[key].__doc__ or "").strip()
    if doc:
        print("\n" + "\n".join(line.strip() for line in doc.splitlines()))
    print("\nExamples:")
    _print_examples(key)


def _main_v2():
    global EXTERNAL_COMPONENTS
    parser = _build_v2_parser()
    if not sys.argv[1:]:                 # bare `docs_tool.py` -> full help, exit 0
        parser.print_help()
        return
    args = parser.parse_args()

    if args.verb == "list":
        return _v2_list(args.what)
    if args.verb == "show":
        return _v2_show(args.name)
    if args.verb == "sync":
        _require_a_docs_tree()
        run_sync(args.file, dry_run=args.dry_run)
        return

    # verb == "check"
    EXTERNAL_COMPONENTS = _load_external_components(args.external_root)
    _configure_links_from_args(args)
    glossary = args.glossary

    families = list(dict.fromkeys(args.families))   # de-dup, keep order
    picked = {rule for rule in _ALL_RULES if getattr(args, rule.replace("-", "_"))}

    if picked and len(families) != 1:
        print("check: --<rule> flags need exactly one family "
              f"(got: {' '.join(families)})", file=sys.stderr)
        sys.exit(2)
    bad = {rule for rule in picked if _family_of(rule) != families[0]}
    if bad:
        print(f"check {families[0]}: unknown flag(s) for this family: "
              f"{', '.join('--' + b for b in sorted(bad))}", file=sys.stderr)
        sys.exit(2)

    selected = []
    for fam in families:
        selected += _resolve_family_selection(fam, picked, args.target)
    seen = set()
    selected = [k for k in selected if not (k in seen or seen.add(k))]
    if not selected:
        print(f"check: that selection matched no rules "
              f"({' '.join(families)}, --target={args.target}).", file=sys.stderr)
        sys.exit(2)

    _require_a_docs_tree()
    _apply_page_filter(args.page)

    ok = _run_selected(selected, args.verbose, glossary)
    sys.exit(0 if ok else 1)


def main():
    comp_line = os.environ.get("COMP_LINE")
    if comp_line is not None:
        # Tab-completion: route on the partial line. Prefer the subcommand
        # parser while the first token is still empty or could become a verb
        # (so `docs_tool <TAB>` and `docs_tool che<TAB>` complete the verbs);
        # fall to the legacy parser only once a `-`-flag is being typed.
        toks = comp_line.split()
        first = toks[1] if len(toks) > 1 else ""
        if not first or any(v.startswith(first) for v in _V2_VERBS) or first in _V2_VERBS:
            return _main_v2()
        return _main_legacy()
    argv = sys.argv[1:]
    # No args, top-level --help, or a subcommand -> the current surface.
    # A legacy flag (--check-*, --all-checks, --list-*, --sync, --verbose ...) -> legacy.
    if not argv or argv[0] in _V2_VERBS or argv[0] in ("-h", "--help"):
        return _main_v2()
    return _main_legacy()


if __name__ == "__main__":
    main()
