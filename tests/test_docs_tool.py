"""Unit and fixture-based integration tests for docs_tool.py.

Run with:
    python3 -m unittest discover -s tests
or a single case:
    python3 -m unittest tests.test_docs_tool.ImagesOrphanedTests

Integration tests build a throwaway Antora tree under a tempdir and point
docs_tool's module-level EN_MODULES_ROOT/RU_MODULES_ROOT at it (the same
technique used ad hoc against scratch fixtures throughout development),
instead of running against this repo's real content -- that keeps them
independent of whatever docs currently exist here.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import docs_tool as dt


class FixtureTestCase(unittest.TestCase):
    """Base class: a fresh empty Antora tree per test, with docs_tool's
    roots pointed at it. Restores real state in tearDown so tests can run
    in any order without leaking into each other or into a real repo."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="docs_tool_test_")
        self.root = Path(self._tmpdir)
        self._orig_en = dt.EN_MODULES_ROOT
        self._orig_ru = dt.RU_MODULES_ROOT
        self._orig_external = dt.EXTERNAL_COMPONENTS
        self._orig_page_filter = dt._PAGE_FILTER
        self._orig_glossary = dt.GLOSSARY
        self._orig_links = (dt.LINK_TIMEOUT, dt.LINK_OFFLINE, dt.LINK_ALLOW_DOMAINS,
                            dt.LINK_CACHE_PATH, dt._probe_url,
                            dt.LINK_INSECURE, dt.LINK_SHOW_UNVERIFIED)
        dt.EN_MODULES_ROOT = self.root / "en" / "modules"
        dt.RU_MODULES_ROOT = self.root / "ru" / "modules"
        dt.EXTERNAL_COMPONENTS = {}
        dt._PAGE_FILTER = None
        dt.GLOSSARY = {}
        dt.LINK_OFFLINE = False
        dt.LINK_ALLOW_DOMAINS = set()
        dt.LINK_INSECURE = False
        dt.LINK_SHOW_UNVERIFIED = False
        dt.LINK_CACHE_PATH = None
        dt._OWN_COMPONENT_NAME_CACHE.clear()

    def tearDown(self):
        dt.EN_MODULES_ROOT = self._orig_en
        dt.RU_MODULES_ROOT = self._orig_ru
        dt.EXTERNAL_COMPONENTS = self._orig_external
        dt._PAGE_FILTER = self._orig_page_filter
        dt.GLOSSARY = self._orig_glossary
        (dt.LINK_TIMEOUT, dt.LINK_OFFLINE, dt.LINK_ALLOW_DOMAINS,
         dt.LINK_CACHE_PATH, dt._probe_url,
         dt.LINK_INSECURE, dt.LINK_SHOW_UNVERIFIED) = self._orig_links
        dt._OWN_COMPONENT_NAME_CACHE.clear()
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def write(self, rel_path: str, content: str = "") -> Path:
        """Write a file relative to the fixture root (e.g.
        "en/modules/ROOT/pages/index.adoc"), creating parent dirs."""
        p = self.root / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p

    def antora_yml(self, lang: str, name: str, version: str = "1.0"):
        self.write(f"{lang}/antora.yml", f"name: {name}\nversion: '{version}'\n")

    @staticmethod
    def run_check(check_fn, *args, **kwargs):
        """Runs a check_*() function with stdout and stderr captured,
        returning (ok, output_text) instead of letting findings print to
        the real terminal. Both streams are merged: some advisory output
        (the unchecked-component note, the glossary fallback) goes to
        stderr, and a test asserting on it shouldn't have to care which."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            ok = check_fn(*args, **kwargs)
        return ok, buf.getvalue()


class ResolveModuleRefTests(unittest.TestCase):
    """Unit tests for _resolve_module_ref -- no filesystem needed."""

    def setUp(self):
        self.roots = {"ROOT": Path("en/modules/ROOT"), "how-to": Path("en/modules/how-to")}

    def test_sibling_module_resolves(self):
        result = dt._resolve_module_ref("how-to", "page.adoc", self.roots, "en")
        self.assertEqual(result, (Path("en/modules/how-to"), "page.adoc"))

    def test_unregistered_external_component_is_none(self):
        result = dt._resolve_module_ref("ADCM", "ROOT:page.adoc", self.roots, "en")
        self.assertIsNone(result)

    def test_registered_external_component_resolves_its_module(self):
        external = {"en": {"ROOT": Path("/tmp/adcm/en/modules/ROOT")}}
        try:
            dt.EXTERNAL_COMPONENTS = {"ADCM": external}
            result = dt._resolve_module_ref("ADCM", "ROOT:page.adoc", self.roots, "en")
            self.assertEqual(result, (Path("/tmp/adcm/en/modules/ROOT"), "page.adoc"))
        finally:
            dt.EXTERNAL_COMPONENTS = {}

    def test_registered_external_component_defaults_to_root_module(self):
        external = {"en": {"ROOT": Path("/tmp/adcm/en/modules/ROOT")}}
        try:
            dt.EXTERNAL_COMPONENTS = {"ADCM": external}
            result = dt._resolve_module_ref("ADCM", "page.adoc", self.roots, "en")
            self.assertEqual(result, (Path("/tmp/adcm/en/modules/ROOT"), "page.adoc"))
        finally:
            dt.EXTERNAL_COMPONENTS = {}

    def test_self_qualified_own_name_resolves_like_unqualified(self):
        """image::ADB:how-to:page.adoc[] written inside this repo's own
        content (docs-adcm's real-world image::ADCM:ROOT:...[] pattern)
        must resolve the same as the unqualified how-to:page.adoc form."""
        result = dt._resolve_module_ref("ADB", "how-to:page.adoc", self.roots, "en", own_name="ADB")
        self.assertEqual(result, (Path("en/modules/how-to"), "page.adoc"))

    def test_own_name_mismatch_is_still_unresolved(self):
        result = dt._resolve_module_ref("ADCM", "ROOT:page.adoc", self.roots, "en", own_name="ADB")
        self.assertIsNone(result)


class ParseIncludeAttrsTests(unittest.TestCase):
    def test_no_attrs_means_whole_file(self):
        tags, negated, whole_file = dt._parse_include_attrs("")
        self.assertEqual((tags, negated, whole_file), (set(), set(), True))

    def test_leveloffset_only_is_still_whole_file(self):
        tags, negated, whole_file = dt._parse_include_attrs("leveloffset=+1")
        self.assertTrue(whole_file)
        self.assertEqual(tags, set())

    def test_single_tag(self):
        tags, negated, whole_file = dt._parse_include_attrs("tag=intro")
        self.assertEqual(tags, {"intro"})
        self.assertFalse(whole_file)

    def test_tags_list_with_negation(self):
        tags, negated, whole_file = dt._parse_include_attrs("tags=parent;!child")
        self.assertEqual(tags, {"parent"})
        self.assertEqual(negated, {"child"})
        self.assertFalse(whole_file)

    def test_wildcard_tags_means_whole_file(self):
        tags, negated, whole_file = dt._parse_include_attrs("tags=**")
        self.assertTrue(whole_file)


class ParseTagRegionsTests(unittest.TestCase):
    def test_simple_region(self):
        lines = ["intro text", "tag::part-01[]", "body", "end::part-01[]", "outro"]
        regions = dt._parse_tag_regions(lines)
        self.assertEqual(regions, [("part-01", 2, 4)])

    def test_nested_regions_pair_by_name_not_stack_order(self):
        """connect.adoc's real part-02-wraps-part-03 pattern: an inner tag
        closes before its outer one, so pairing must match by name, not
        assume strict LIFO order."""
        lines = [
            "tag::outer[]",
            "tag::inner[]",
            "body",
            "end::inner[]",
            "more",
            "end::outer[]",
        ]
        regions = dt._parse_tag_regions(lines)
        self.assertEqual(set(regions), {("inner", 2, 4), ("outer", 1, 6)})


class ScanDelimiterStackTests(unittest.TestCase):
    """Unit tests for _scan_delimiter_stack -- no filesystem needed. Feeds
    plain (file, lineno, text) triples through the stream directly rather
    than via _flatten_delimiter_lines, so these exercise the balancing
    algorithm itself in isolation."""

    @staticmethod
    def stream(lines, file="f.adoc"):
        return [(file, i, l) for i, l in enumerate(lines, 1)]

    def test_balanced_nested_blocks_return_empty(self):
        lines = ["====", "content", "===="]
        self.assertEqual(dt._scan_delimiter_stack(self.stream(lines)), [])

    def test_missing_table_close_is_pinpointed_not_cascaded(self):
        """Regression test for the foreign-tables.adoc bug: a table opened
        with `|===` inside an example block, with no matching `|===`
        before the example block's own `====` closes around it. A naive
        LIFO would treat the mismatched `====` as opening a new nesting
        level and misattribute the imbalance to unrelated, far-away lines;
        this must instead pin the blame on the actual unclosed `|===`."""
        lines = [
            "====",         # 1: open example block
            "[cols=\"1\"]",  # 2
            "|===",         # 3: open table -- never closed
            "cell content",  # 4
            "====",         # 5: closes the example block, table still open
            "====",         # 6: open+close a second, unrelated example block
            "content",      # 7
            "====",         # 8
        ]
        result = dt._scan_delimiter_stack(self.stream(lines))
        self.assertEqual(result, [("|===", "f.adoc", 3)])

    def test_genuine_different_length_nesting_still_balances(self):
        """A 5-equals example block nested inside a 4-equals one (real,
        supported Asciidoctor nesting) must still balance cleanly -- the
        deeper-stack recovery must not fire when the mismatch really is a
        new nesting level, not a bug."""
        lines = ["====", "=====", "inner content", "=====", "outer content", "===="]
        self.assertEqual(dt._scan_delimiter_stack(self.stream(lines)), [])

    def test_opaque_listing_block_content_is_not_mistaken_for_delimiters(self):
        """A `----` separator row inside a psql-style ASCII table shown
        verbatim inside a listing block must not be treated as closing or
        nesting anything; only the exact `----` that opened it can close."""
        lines = ["----", "id | name", "----+------", "1  | a", "----"]
        self.assertEqual(dt._scan_delimiter_stack(self.stream(lines)), [])

    def test_unclosed_delimiter_at_eof_is_still_reported(self):
        lines = ["====", "content"]
        result = dt._scan_delimiter_stack(self.stream(lines))
        self.assertEqual(result, [("====", "f.adoc", 1)])


class ComponentPrefixRegexTests(unittest.TestCase):
    def test_plain_module_prefix(self):
        m = dt._COMPONENT_PREFIX_RE.match("how-to:page.adoc")
        self.assertEqual(m.group(0), "how-to:")

    def test_no_match_for_bare_relative_path(self):
        self.assertIsNone(dt._COMPONENT_PREFIX_RE.match("some-file.adoc"))


class AntoraFamilyAttrRegexTests(unittest.TestCase):
    def test_attachmentsdir_with_subpath(self):
        m = dt._ANTORA_FAMILY_ATTR_RE.match("{attachmentsdir}/sample.csv")
        self.assertEqual(m.group(1), "attachmentsdir")
        self.assertEqual(m.group(2), "sample.csv")

    def test_unrelated_attribute_does_not_match(self):
        self.assertIsNone(dt._ANTORA_FAMILY_ATTR_RE.match("{install-link}"))


class IterFilesTests(unittest.TestCase):
    """Regression test for the .DS_Store bug: pathlib's rglob("*") matches
    dotfiles even though a shell glob wouldn't."""

    def test_dotfiles_and_dotdirs_are_skipped(self):
        tmpdir = tempfile.mkdtemp(prefix="docs_tool_iterfiles_")
        try:
            root = Path(tmpdir)
            (root / "sub").mkdir()
            (root / ".DS_Store").write_text("")
            (root / "real.png").write_text("")
            (root / "sub" / ".DS_Store").write_text("")
            (root / "sub" / "real2.png").write_text("")
            found = {p.name for p in dt._iter_files(root)}
            self.assertEqual(found, {"real.png", "real2.png"})
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class LoadExternalComponentsTests(unittest.TestCase):
    def test_valid_path_produces_no_warning(self):
        tmpdir = tempfile.mkdtemp(prefix="docs_tool_ext_")
        try:
            root = Path(tmpdir)
            (root / "en" / "modules" / "ROOT").mkdir(parents=True)
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                components = dt._load_external_components([f"ADCM={root}"])
            self.assertEqual(buf.getvalue(), "")
            self.assertIn("ROOT", components["ADCM"]["en"])
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_nonexistent_path_warns(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            components = dt._load_external_components(["ADCM=/no/such/path/at/all"])
        self.assertIn("does not exist", buf.getvalue())
        self.assertEqual(components["ADCM"]["en"], {})

    def test_path_with_no_modules_warns(self):
        tmpdir = tempfile.mkdtemp(prefix="docs_tool_ext_empty_")
        try:
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                dt._load_external_components([f"ADCM={tmpdir}"])
            self.assertIn("no en/modules or ru/modules", buf.getvalue())
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_path_that_is_a_file_warns(self):
        tmpdir = tempfile.mkdtemp(prefix="docs_tool_ext_file_")
        try:
            file_path = Path(tmpdir) / "not-a-dir"
            file_path.write_text("")
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                dt._load_external_components([f"ADCM={file_path}"])
            self.assertIn("not a directory", buf.getvalue())
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class LoadGlossaryTests(unittest.TestCase):
    def _load(self, rows_text: str):
        """rows_text is the pipe-delimited body (without header row/comments)."""
        tmpdir = tempfile.mkdtemp(prefix="docs_tool_glossary_")
        try:
            path = Path(tmpdir) / "glossary.psv"
            path.write_text("# a comment\nen|ru|ru_pattern|note\n" + rows_text, encoding="utf-8")
            return dt._load_glossary([str(path)])
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_comment_and_header_lines_skipped(self):
        glossary = self._load("host|хост|хост<>|\n")
        self.assertEqual(set(glossary), {"host"})

    def test_rows_sharing_en_term_merge_with_note_ignored_for_matching(self):
        glossary = self._load(
            "session|сессия|сесси<>|generic database sense\n"
            "session|сеанс|сеанс<>|bounded maintenance-operation sense\n"
        )
        self.assertEqual(set(glossary), {"session"})
        self.assertEqual(glossary["session"]["ru_display"], {"сессия", "сеанс"})
        self.assertEqual(len(glossary["session"]["patterns"]), 2)

    def test_missing_file_exits(self):
        with self.assertRaises(SystemExit):
            dt._load_glossary(["/no/such/glossary/file.psv"])

    def test_row_missing_pattern_is_skipped_with_warning(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            glossary = self._load("host|хост||\n")
        self.assertEqual(glossary, {})
        self.assertIn("missing en/ru_pattern", buf.getvalue())

    def test_row_with_wrong_field_count_is_skipped_with_warning(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            glossary = self._load("host|хост|хост<>\n")  # only 3 fields, missing note
        self.assertEqual(glossary, {})
        self.assertIn("expected 4 '|'-separated fields", buf.getvalue())

    def test_comma_in_field_needs_no_escaping(self):
        glossary = self._load("session|сессия, сеанс|сесси<>|a note, with a comma\n")
        self.assertEqual(glossary["session"]["ru_display"], {"сессия, сеанс"})


class DiscoverDefaultGlossariesTests(unittest.TestCase):
    """--glossary's fallback: every *-glossary.psv directly under the
    current directory, so a docs repo carrying its own glossary doesn't
    need --glossary spelled out on every run."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="docs_tool_discover_glossary_")
        self._orig_cwd = os.getcwd()
        os.chdir(self._tmpdir)

    def tearDown(self):
        os.chdir(self._orig_cwd)
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_no_match_returns_empty(self):
        self.assertEqual(dt._discover_default_glossaries(), [])

    def test_single_match_found(self):
        Path("greengagedb-glossary.psv").write_text("en|ru|ru_pattern|note\n", encoding="utf-8")
        self.assertEqual(dt._discover_default_glossaries(), ["greengagedb-glossary.psv"])

    def test_multiple_matches_found_sorted(self):
        Path("zzz-glossary.psv").write_text("en|ru|ru_pattern|note\n", encoding="utf-8")
        Path("aaa-glossary.psv").write_text("en|ru|ru_pattern|note\n", encoding="utf-8")
        self.assertEqual(dt._discover_default_glossaries(), ["aaa-glossary.psv", "zzz-glossary.psv"])

    def test_non_matching_files_ignored(self):
        Path("glossary.psv").write_text("en|ru|ru_pattern|note\n", encoding="utf-8")  # no "-glossary" prefix
        Path("notes-glossary.txt").write_text("irrelevant\n", encoding="utf-8")  # wrong extension
        self.assertEqual(dt._discover_default_glossaries(), [])

    def test_subdirectory_not_searched(self):
        Path("sub").mkdir()
        (Path("sub") / "nested-glossary.psv").write_text("en|ru|ru_pattern|note\n", encoding="utf-8")
        self.assertEqual(dt._discover_default_glossaries(), [])


class CompileGlossaryPatternTests(unittest.TestCase):
    def _matches(self, ru_pattern: str, ru_line: str) -> bool:
        pattern = dt._compile_glossary_pattern(ru_pattern)
        return all(regex.search(ru_line) for regex in pattern)

    def test_stem_token_matches_declined_form(self):
        self.assertTrue(self._matches("таблиц<>", "в случае читающих таблиц распаковка выполняется"))
        self.assertTrue(self._matches("таблиц<>", "это таблица"))

    def test_stem_token_requires_word_boundary(self):
        # "стол<>" must not match inside an unrelated longer word like "престол".
        self.assertFalse(self._matches("стол<>", "царский престол"))
        self.assertTrue(self._matches("стол<>", "деревянный стол"))

    def test_bare_token_requires_exact_word(self):
        self.assertTrue(self._matches("NOT NULL", "ограничение NOT NULL здесь"))
        self.assertFalse(self._matches("NOT NULL", "ограничение NOTNULL здесь"))

    def test_missing_word_does_not_match(self):
        self.assertFalse(self._matches("хеш<>", "здесь этого слова нет"))

    def test_multiword_pattern_requires_all_tokens_any_order(self):
        self.assertTrue(self._matches(
            "стоимостн<> оптимизатор<> запрос<>",
            "запросов используется новый оптимизатор, стоимостной по своей природе",
        ))
        self.assertFalse(self._matches(
            "стоимостн<> оптимизатор<> запрос<>",
            "используется новый оптимизатор запросов",  # missing "стоимостн..."
        ))

    def test_entry_satisfied_by_any_alternative_pattern(self):
        entry = {
            "ru_display": {"сессия", "сеанс"},
            "patterns": [dt._compile_glossary_pattern("сесси<>"), dt._compile_glossary_pattern("сеанс<>")],
        }
        self.assertTrue(dt._glossary_entry_satisfied(entry, "начните новый сеанс"))
        self.assertTrue(dt._glossary_entry_satisfied(entry, "текущая сессия истекла"))
        self.assertFalse(dt._glossary_entry_satisfied(entry, "текущее подключение истекло"))

    def test_do_not_translate_entry_requires_verbatim_en(self):
        entry = {"ru_display": {"не переводить"}, "patterns": [dt._compile_glossary_pattern("Greengage DB")]}
        self.assertTrue(dt._glossary_entry_satisfied(entry, "работает в Greengage DB кластере"))
        self.assertFalse(dt._glossary_entry_satisfied(entry, "работает в кластере gpdb"))


class ImagesOrphanedTests(FixtureTestCase):
    def test_duplicate_basename_only_unused_copy_is_flagged(self):
        """Regression test for the promql-prometheus-query-language_dark.png
        bug: two modules each have their own images/shared.png, only one is
        referenced -- a basename-substring check would clear both."""
        self.antora_yml("en", "TEST")
        self.write("en/modules/mod-a/pages/page-a.adoc", "image::shared.png[]\n")
        self.write("en/modules/mod-a/images/shared.png", "used")
        self.write("en/modules/mod-b/images/shared.png", "unused duplicate")

        ok, output = self.run_check(dt.check_images_orphaned)
        self.assertFalse(ok)
        self.assertIn("mod-b", output)
        self.assertIn("shared.png", output)
        self.assertNotIn(str(dt.EN_MODULES_ROOT / "mod-a" / "images" / "shared.png"), output)

    def test_substring_collision_does_not_hide_a_real_orphan(self):
        """Regression test for images/services.png being hidden by
        images/adb_add_services.png (an unrelated file whose name happens
        to end with "services.png") under the old basename-in-text check."""
        self.antora_yml("en", "TEST")
        self.write("en/modules/ROOT/pages/page.adoc", "image::adb_add_services.png[]\n")
        self.write("en/modules/ROOT/images/adb_add_services.png", "used")
        self.write("en/modules/ROOT/images/services.png", "genuinely unused")

        ok, output = self.run_check(dt.check_images_orphaned)
        self.assertFalse(ok)
        self.assertIn("services.png", output)

    def test_inlinesvg_macro_counts_as_usage(self):
        """Regression test: inlineSVG:icon.svg[] (a real macro used for
        get-started card icons) must count as usage, not just image:/
        image::/injectSvg:/injectSvg::."""
        self.antora_yml("en", "TEST")
        self.write("en/modules/ROOT/pages/page.adoc", "inlineSVG:icons/plus.svg[]\n")
        self.write("en/modules/ROOT/images/icons/plus.svg", "svg")

        ok, output = self.run_check(dt.check_images_orphaned)
        self.assertTrue(ok, output)

    def test_commented_out_reference_does_not_count_as_usage(self):
        self.antora_yml("en", "TEST")
        self.write("en/modules/ROOT/pages/page.adoc", "// image::dead.png[]\n")
        self.write("en/modules/ROOT/images/dead.png", "unused")

        ok, output = self.run_check(dt.check_images_orphaned)
        self.assertFalse(ok)
        self.assertIn("dead.png", output)


class TagsOrphanedTests(FixtureTestCase):
    def test_unused_tag_is_flagged(self):
        self.antora_yml("en", "TEST")
        self.write("en/modules/ROOT/pages/page.adoc", "intro\n")
        self.write(
            "en/modules/ROOT/partials/snippet.adoc",
            "tag::used[]\nkept\nend::used[]\ntag::orphan[]\ndead\nend::orphan[]\n",
        )
        self.write("en/modules/ROOT/pages/other.adoc", "include::partial$snippet.adoc[tag=used]\n")

        ok, output = self.run_check(dt.check_tags_orphaned)
        self.assertFalse(ok)
        self.assertIn("tag::orphan[]", output)
        self.assertNotIn("tag::used[]", output)

    def test_negated_tag_in_nested_include_is_still_orphaned(self):
        self.antora_yml("en", "TEST")
        self.write(
            "en/modules/ROOT/partials/snippet.adoc",
            "tag::parent[]\ntag::child[]\ninner\nend::child[]\nend::parent[]\n",
        )
        self.write("en/modules/ROOT/pages/page.adoc", "include::partial$snippet.adoc[tags=parent;!child]\n")

        ok, output = self.run_check(dt.check_tags_orphaned)
        self.assertFalse(ok)
        self.assertIn("tag::child[]", output)
        self.assertNotIn("tag::parent[]", output)

    def test_nested_tag_negated_by_one_include_but_used_by_another_is_not_orphaned(self):
        """Regression test for the docs-greengagedb scenario
        (compression_codecs/compression_codecs_no_compression,
        format_null/null_description/gpload_note): a nested tag negated by
        one include call site (tags=parent;!child) but pulled in plainly
        by a *different* include of the same parent (tag=parent, no
        exclusion) really does render on that second page, so it must not
        be reported orphaned just because some other call site excludes
        it -- negation has to be judged per call site, not merged into one
        blanket "ever negated in this file" verdict."""
        self.antora_yml("en", "TEST")
        self.write(
            "en/modules/ROOT/partials/snippet.adoc",
            "tag::parent[]\ntag::child[]\ninner\nend::child[]\nend::parent[]\n",
        )
        self.write("en/modules/ROOT/pages/excludes-child.adoc",
                    "include::partial$snippet.adoc[tags=parent;!child]\n")
        self.write("en/modules/ROOT/pages/includes-child.adoc",
                    "include::partial$snippet.adoc[tag=parent]\n")

        ok, output = self.run_check(dt.check_tags_orphaned)
        self.assertTrue(ok, output)

    def test_page_filter_narrows_report_but_not_usage_scan(self):
        """--page must narrow which files' own tag regions get reported
        on, but the usage scan itself still has to cover the whole site --
        a tag defined in the --page-filtered-in file, used only from a
        page --page filters out, must still be recognized as used, not
        reported as a false orphan just because its user was excluded from
        the report."""
        self.antora_yml("en", "TEST")
        self.write("en/modules/ROOT/partials/kept.adoc", "tag::kept_tag[]\nbody\nend::kept_tag[]\n")
        self.write("en/modules/ROOT/partials/other.adoc", "tag::other_tag[]\nbody\nend::other_tag[]\n")
        self.write("en/modules/ROOT/pages/user.adoc", "include::partial$kept.adoc[tag=kept_tag]\n")

        dt._PAGE_FILTER = {"names": {("kept",)}, "dirs": set()}
        ok, output = self.run_check(dt.check_tags_orphaned)
        self.assertTrue(ok, output)  # kept_tag used (from user.adoc, filtered out of the report but still scanned)
        self.assertNotIn("other_tag", output)  # other.adoc's own orphaned tag is filtered out of the report

    def test_tag_used_only_from_nav_adoc_is_not_orphaned(self):
        """_collect_tag_usage (shared with check_partials_orphaned) must
        scan nav.adoc for include::...[] macros too, not just pages/ and
        partials/ -- nav.adoc can pull in a tagged region the same as any
        page can."""
        self.antora_yml("en", "TEST")
        self.write(
            "en/modules/ROOT/partials/snippet.adoc",
            "tag::used[]\nkept\nend::used[]\n",
        )
        self.write("en/modules/ROOT/nav.adoc", "include::partial$snippet.adoc[tag=used]\n")

        ok, output = self.run_check(dt.check_tags_orphaned)
        self.assertTrue(ok, output)

    def test_cross_repo_usage_via_external_root_is_not_orphaned(self):
        """Regression test for the docs-adbes scenario: a tag defined here
        but only pulled in by a registered --external-root component's own
        content (via a self-qualified include::ADB:how-to:...[]) must not
        be reported orphaned."""
        self.antora_yml("en", "ADB")
        self.write("en/modules/how-to/pages/metrics.adoc", "tag::prometheus[]\nbody\nend::prometheus[]\n")

        external_root = Path(tempfile.mkdtemp(prefix="docs_tool_ext_repo_"))
        (external_root / "en" / "modules" / "ROOT" / "pages").mkdir(parents=True)
        (external_root / "en" / "modules" / "ROOT" / "pages" / "index.adoc").write_text(
            "include::ADB:how-to:metrics.adoc[tag=prometheus]\n", encoding="utf-8"
        )
        try:
            dt.EXTERNAL_COMPONENTS = dt._load_external_components([f"ADBES={external_root}"])
            ok, output = self.run_check(dt.check_tags_orphaned)
            self.assertTrue(ok, output)
        finally:
            shutil.rmtree(external_root, ignore_errors=True)


class PagesOrphanedTests(FixtureTestCase):
    """check_pages_orphaned (RF02). Reachability here means *a nav.adoc
    entry*, deliberately -- NOT "something, somewhere links to it".

    That distinction is the whole rule, so it is pinned here. Antora
    publishes a page whether or not the nav lists it, so it is tempting to
    read a nav-absent-but-xref'd page as a false positive and "fix" the
    rule to count prose xrefs. Don't: a repo part-way through adding a
    section relies on this to list what still needs wiring into the nav,
    and counting xrefs would hide exactly that. A page linked only from
    body text has no sidebar entry, which is the finding."""

    def test_page_missing_from_nav_is_orphaned(self):
        self.antora_yml("en", "TEST")
        self.write("en/modules/ROOT/nav.adoc", "* xref:listed.adoc[Listed]\n")
        self.write("en/modules/ROOT/pages/listed.adoc", "= L\n")
        self.write("en/modules/ROOT/pages/stray.adoc", "= S\n")

        ok, out = self.run_check(dt.check_pages_orphaned)
        self.assertFalse(ok)
        self.assertIn("stray.adoc", out)
        self.assertNotIn("listed.adoc", out)

    def test_xref_from_another_page_does_not_count_as_reachable(self):
        """The case most likely to be mistaken for a bug -- see the class
        docstring. A page linked from prose but absent from the nav is
        still reported."""
        self.antora_yml("en", "TEST")
        self.write("en/modules/ROOT/nav.adoc", "* xref:index.adoc[Index]\n")
        self.write("en/modules/ROOT/pages/index.adoc",
                   "= I\n\nSee xref:detail.adoc[the detail page].\n")
        self.write("en/modules/ROOT/pages/detail.adoc", "= D\n")

        ok, out = self.run_check(dt.check_pages_orphaned)
        self.assertFalse(ok)
        self.assertIn("detail.adoc", out)

    def test_nested_page_matched_by_its_path_relative_to_pages(self):
        self.antora_yml("en", "TEST")
        self.write("en/modules/ROOT/nav.adoc",
                   "* xref:reference/sql/select.adoc[SELECT]\n")
        self.write("en/modules/ROOT/pages/reference/sql/select.adoc", "= S\n")

        ok, out = self.run_check(dt.check_pages_orphaned)
        self.assertTrue(ok, out)

    def test_module_qualified_nav_entry_counts(self):
        self.antora_yml("en", "TEST")
        self.write("en/modules/ROOT/nav.adoc", "* xref:howto:guide.adoc[Guide]\n")
        self.write("en/modules/howto/pages/guide.adoc", "= G\n")

        ok, out = self.run_check(dt.check_pages_orphaned)
        self.assertTrue(ok, out)

    def test_component_qualified_nav_entry_counts(self):
        """nav.adoc may spell out its own component name instead of using
        the shorter module-qualified form."""
        self.antora_yml("en", "TEST")
        self.write("en/modules/ROOT/nav.adoc", "* xref:TEST:howto:guide.adoc[Guide]\n")
        self.write("en/modules/howto/pages/guide.adoc", "= G\n")

        ok, out = self.run_check(dt.check_pages_orphaned)
        self.assertTrue(ok, out)

    def test_a_sibling_modules_nav_can_do_the_linking(self):
        """Navs are unioned: modules cross-reference each other, so a page
        need not be listed by its *own* module's nav."""
        self.antora_yml("en", "TEST")
        self.write("en/modules/ROOT/nav.adoc", "* xref:howto:guide.adoc[Guide]\n")
        self.write("en/modules/howto/nav.adoc", "* xref:ROOT:index.adoc[Index]\n")
        self.write("en/modules/howto/pages/guide.adoc", "= G\n")
        self.write("en/modules/ROOT/pages/index.adoc", "= I\n")

        ok, out = self.run_check(dt.check_pages_orphaned)
        self.assertTrue(ok, out)

    def test_nav_entry_inside_an_included_partial_counts(self):
        """_combined_nav_text flattens include::partial$...[] first."""
        self.antora_yml("en", "TEST")
        self.write("en/modules/ROOT/nav.adoc", "include::partial$nav-extra.adoc[]\n")
        self.write("en/modules/ROOT/partials/nav-extra.adoc", "* xref:deep.adoc[Deep]\n")
        self.write("en/modules/ROOT/pages/deep.adoc", "= D\n")

        ok, out = self.run_check(dt.check_pages_orphaned)
        self.assertTrue(ok, out)

    def test_start_page_is_exempt(self):
        """antora.yml's start_page is the component's entry point, so it
        needs no nav entry of its own."""
        self.write("en/antora.yml",
                   "name: TEST\nversion: '1.0'\nstart_page: ROOT:home.adoc\n")
        self.write("en/modules/ROOT/nav.adoc", "* xref:other.adoc[Other]\n")
        self.write("en/modules/ROOT/pages/home.adoc", "= H\n")
        self.write("en/modules/ROOT/pages/other.adoc", "= O\n")

        ok, out = self.run_check(dt.check_pages_orphaned)
        self.assertTrue(ok, out)

    def test_pdf_glossary_layout_page_is_exempt(self):
        """:page-layout: pdf-glossary marks a page built for the PDF only,
        which is intentionally absent from the site nav."""
        self.antora_yml("en", "TEST")
        self.write("en/modules/ROOT/nav.adoc", "* xref:other.adoc[Other]\n")
        self.write("en/modules/ROOT/pages/other.adoc", "= O\n")
        self.write("en/modules/ROOT/pages/glossary.adoc",
                   "= G\n:page-layout: pdf-glossary\n")

        ok, out = self.run_check(dt.check_pages_orphaned)
        self.assertTrue(ok, out)

    def test_a_language_with_no_nav_at_all_is_skipped(self):
        """No nav to compare against means no evidence either way -- every
        page would otherwise be reported."""
        self.antora_yml("en", "TEST")
        self.write("en/modules/ROOT/pages/page.adoc", "= P\n")

        ok, out = self.run_check(dt.check_pages_orphaned)
        self.assertTrue(ok, out)

    def test_en_and_ru_are_both_scanned(self):
        self.antora_yml("en", "TEST")
        self.antora_yml("ru", "TEST")
        for lang in ("en", "ru"):
            self.write(f"{lang}/modules/ROOT/nav.adoc", "* xref:listed.adoc[L]\n")
            self.write(f"{lang}/modules/ROOT/pages/listed.adoc", "= L\n")
        self.write("ru/modules/ROOT/pages/zabytaya.adoc", "= Z\n")

        ok, out = self.run_check(dt.check_pages_orphaned)
        self.assertFalse(ok)
        self.assertIn("zabytaya.adoc", out)


class PartialsOrphanedTests(FixtureTestCase):
    def test_whole_file_partial_with_no_tags_and_no_include_is_orphaned(self):
        """Regression test: a partials/ file with no tag::/end:: markers at
        all -- content meant to be pulled in only as a whole file via a
        bare include::...[] -- must be reported orphaned once nothing
        includes it anymore. Previously check_tags_orphaned's
        `if not regions: continue` skipped such files entirely, so a
        whole-file partial left behind after its last
        include::partial$...[] was deleted was invisible to every
        orphaned-content check (docs-adb's hosts-online.adoc)."""
        self.antora_yml("en", "TEST")
        self.write(
            "en/modules/ROOT/partials/hosts-online.adoc",
            "IMPORTANT: some plain content, no tag markers\n",
        )
        self.write("en/modules/ROOT/pages/page.adoc", "intro\n")

        ok, output = self.run_check(dt.check_partials_orphaned)
        self.assertFalse(ok)
        self.assertIn("hosts-online.adoc", output)

    def test_whole_file_partial_included_plainly_is_not_orphaned(self):
        self.antora_yml("en", "TEST")
        self.write(
            "en/modules/ROOT/partials/hosts-online.adoc",
            "IMPORTANT: some plain content, no tag markers\n",
        )
        self.write("en/modules/ROOT/pages/page.adoc", "include::partial$hosts-online.adoc[]\n")

        ok, output = self.run_check(dt.check_partials_orphaned)
        self.assertTrue(ok, output)

    def test_tagged_partial_is_left_to_tags_orphaned_check(self):
        """A partial that has its own tag::/end:: regions is judged
        tag-by-tag by check_tags_orphaned instead -- check_partials_orphaned
        must not also flag it as a whole-file orphan just because it isn't
        pulled in via a plain/wildcarded include."""
        self.antora_yml("en", "TEST")
        self.write(
            "en/modules/ROOT/partials/snippet.adoc",
            "tag::used[]\nkept\nend::used[]\n",
        )
        self.write("en/modules/ROOT/pages/page.adoc", "include::partial$snippet.adoc[tag=used]\n")

        ok, output = self.run_check(dt.check_partials_orphaned)
        self.assertTrue(ok, output)

    def test_whole_file_partial_included_only_from_nav_adoc_is_not_orphaned(self):
        """Regression test for the docs-greengagedb false positive:
        nav_reference_utils.adoc/nav_reference_admin_schemas.adoc are each
        pulled in only via include::partial$...[] from nav.adoc itself (a
        long submenu factored out of the main nav tree), never from any
        pages/partials file. _collect_tag_usage only scanned pages/ and
        partials/ for include macros, so nav.adoc's own includes were
        invisible to the usage scan and this whole-file partial was
        wrongly reported orphaned."""
        self.antora_yml("en", "TEST")
        self.write(
            "en/modules/ROOT/partials/nav_reference_utils.adoc",
            "* xref:reference/utils/foo.adoc[]\n",
        )
        self.write("en/modules/ROOT/nav.adoc", "* xref:index.adoc[]\ninclude::partial$nav_reference_utils.adoc[]\n")

        ok, output = self.run_check(dt.check_partials_orphaned)
        self.assertTrue(ok, output)

    def test_cross_repo_usage_via_external_root_is_not_orphaned(self):
        self.antora_yml("en", "ADB")
        self.write("en/modules/ROOT/partials/hosts-online.adoc", "plain content, no tag markers\n")

        external_root = Path(tempfile.mkdtemp(prefix="docs_tool_ext_repo_"))
        (external_root / "en" / "modules" / "ROOT" / "pages").mkdir(parents=True)
        (external_root / "en" / "modules" / "ROOT" / "pages" / "index.adoc").write_text(
            "include::ADB:ROOT:partial$hosts-online.adoc[]\n", encoding="utf-8"
        )
        try:
            dt.EXTERNAL_COMPONENTS = dt._load_external_components([f"ADBES={external_root}"])
            ok, output = self.run_check(dt.check_partials_orphaned)
            self.assertTrue(ok, output)
        finally:
            shutil.rmtree(external_root, ignore_errors=True)


class PagesBrokenRefsTests(FixtureTestCase):
    def test_self_qualified_own_component_image_resolves(self):
        """Regression test for the docs-adcm scenario:
        image::ADCM:ROOT:pic.png[] written inside docs-adcm's own content
        must resolve against this repo's own modules, not be silently
        skipped as an unregistered external component."""
        self.antora_yml("en", "ADCM")
        self.write("en/modules/ROOT/partials/snippet.adoc", "image::ADCM:ROOT:pic.png[]\n")
        self.write("en/modules/ROOT/pages/page.adoc", "include::partial$snippet.adoc[]\n")
        self.write("en/modules/ROOT/images/pic.png", "real")

        ok, output = self.run_check(dt.check_pages_broken_refs)
        self.assertTrue(ok, output)

    def test_self_qualified_own_component_missing_image_is_broken(self):
        self.antora_yml("en", "ADCM")
        self.write("en/modules/ROOT/partials/snippet.adoc", "image::ADCM:ROOT:missing.png[]\n")
        self.write("en/modules/ROOT/pages/page.adoc", "include::partial$snippet.adoc[]\n")

        ok, output = self.run_check(dt.check_pages_broken_refs)
        self.assertFalse(ok)
        self.assertIn("missing.png", output)

    def test_self_qualified_explicit_empty_module_xref_resolves(self):
        """Regression test for the docs-greengagedb scenario:
        xref:docs-gg::connect_with_psql.adoc[] -- Antora's explicit-empty-
        module form ("component::page", double colon) meaning the same
        thing as "component:page" (module omitted) -- must resolve to
        ROOT, not be reported broken with a stray leading ':' left in the
        path (the bug produced exactly "xref::connect_with_psql.adoc" in
        the report, one colon short of the real target)."""
        self.antora_yml("en", "docs-gg")
        self.write("en/modules/ROOT/pages/connect_with_psql.adoc", "content\n")
        self.write("en/modules/ROOT/partials/snippet.adoc",
                    "xref:docs-gg::connect_with_psql.adoc[]\n")
        self.write("en/modules/ROOT/pages/page.adoc", "include::partial$snippet.adoc[]\n")

        ok, output = self.run_check(dt.check_pages_broken_refs)
        self.assertTrue(ok, output)

    def test_self_qualified_explicit_empty_module_missing_xref_is_broken(self):
        self.antora_yml("en", "docs-gg")
        self.write("en/modules/ROOT/partials/snippet.adoc",
                    "xref:docs-gg::missing.adoc[]\n")
        self.write("en/modules/ROOT/pages/page.adoc", "include::partial$snippet.adoc[]\n")

        ok, output = self.run_check(dt.check_pages_broken_refs)
        self.assertFalse(ok)
        self.assertIn("missing.adoc", output)

    def test_different_unregistered_component_is_left_unchecked(self):
        """A reference into a genuinely different, unregistered component
        must stay silently skipped, not get validated as if it were this
        repo's own content."""
        self.antora_yml("en", "ADCM")
        self.write("en/modules/ROOT/pages/page.adoc", "image::ADPG:ROOT:whatever.png[]\n")

        ok, output = self.run_check(dt.check_pages_broken_refs)
        self.assertTrue(ok, output)

    def test_qualified_include_without_family_marker_defaults_to_page(self):
        """Regression test: include::ADB:how-to:metrics.adoc[tag=x] (no
        page$/partial$/example$ marker) must default to the page family,
        the same way a bare xref:module:page.adoc[] already does -- not
        fall back to "relative to the including file's own directory"."""
        self.antora_yml("en", "ADB")
        self.write("en/modules/how-to/pages/metrics.adoc", "content\n")
        self.write("en/modules/ROOT/pages/page.adoc", "include::ADB:how-to:metrics.adoc[]\n")

        ok, output = self.run_check(dt.check_pages_broken_refs)
        self.assertTrue(ok, output)

    def test_link_to_attachmentsdir_resolves(self):
        self.antora_yml("en", "TEST")
        self.write("en/modules/ROOT/attachments/sample.csv", "a,b\n")
        self.write("en/modules/ROOT/pages/page.adoc", "link:{attachmentsdir}/sample.csv[Download]\n")

        ok, output = self.run_check(dt.check_pages_broken_refs)
        self.assertTrue(ok, output)

    def test_link_to_missing_attachmentsdir_file_is_broken(self):
        self.antora_yml("en", "TEST")
        self.write("en/modules/ROOT/pages/page.adoc", "link:{attachmentsdir}/missing.csv[Download]\n")

        ok, output = self.run_check(dt.check_pages_broken_refs)
        self.assertFalse(ok)
        self.assertIn("missing.csv", output)

    def test_link_to_external_url_is_skipped(self):
        self.antora_yml("en", "TEST")
        self.write("en/modules/ROOT/pages/page.adoc", "link:https://example.com/foo[External]\n")

        ok, output = self.run_check(dt.check_pages_broken_refs)
        self.assertTrue(ok, output)

    def test_link_to_missing_relative_file_is_broken(self):
        self.antora_yml("en", "TEST")
        self.write("en/modules/ROOT/pages/page.adoc", "link:missing-sibling.txt[Download]\n")

        ok, output = self.run_check(dt.check_pages_broken_refs)
        self.assertFalse(ok)
        self.assertIn("missing-sibling.txt", output)

    def test_include_tag_not_present_in_target_is_broken(self):
        """Regression test for the connections.adoc bug: the target file
        exists, but the requested tag= doesn't match any tag::NAME[] region
        in it (e.g. a rename left the include pointing at the old name).
        Asciidoctor silently renders nothing for this, so a plain
        file-exists check missed it -- it must be reported as broken."""
        self.antora_yml("en", "TEST")
        self.write(
            "en/modules/ROOT/partials/snippet.adoc",
            "tag::allow-remote-connections1[]\nbody\nend::allow-remote-connections1[]\n",
        )
        self.write("en/modules/ROOT/pages/page.adoc", "include::partial$snippet.adoc[tag=allow-remote-connections]\n")

        ok, output = self.run_check(dt.check_pages_broken_refs)
        self.assertFalse(ok)
        self.assertIn("allow-remote-connections", output)
        self.assertIn("not found", output)

    def test_include_tag_present_in_target_is_not_broken(self):
        self.antora_yml("en", "TEST")
        self.write(
            "en/modules/ROOT/partials/snippet.adoc",
            "tag::intro[]\nbody\nend::intro[]\n",
        )
        self.write("en/modules/ROOT/pages/page.adoc", "include::partial$snippet.adoc[tag=intro]\n")

        ok, output = self.run_check(dt.check_pages_broken_refs)
        self.assertTrue(ok, output)

    def test_include_without_tag_attribute_does_not_check_tags(self):
        """A plain include (no tag=/tags=) pulls in the whole file, so a
        target with no tag regions at all -- or different ones -- must not
        be flagged; only an explicit tag= request is checked."""
        self.antora_yml("en", "TEST")
        self.write("en/modules/ROOT/partials/snippet.adoc", "plain content, no tags\n")
        self.write("en/modules/ROOT/pages/page.adoc", "include::partial$snippet.adoc[]\n")

        ok, output = self.run_check(dt.check_pages_broken_refs)
        self.assertTrue(ok, output)

    def test_version_pinned_xref_is_left_unchecked(self):
        """Regression test for the docs-adbes scenario: a version-pinned
        Antora xref (e.g. "6.29.1.1@ADB:tutorials:external-db.adoc[]",
        linking to a specific past ADB release) must not be reported
        broken just because "6.29.1.1@ADB" doesn't start with a letter and
        so isn't recognized as a component prefix -- it's left unchecked
        the same way any other unregistered external component is, even
        with --external-root ADB=... registered, since that root only
        holds the current checkout, not the pinned historical version."""
        self.antora_yml("en", "TEST")
        self.write("en/modules/ROOT/pages/page.adoc",
                    "xref:6.29.1.1@ADB:tutorials:external-db.adoc[]\n")

        ok, output = self.run_check(dt.check_pages_broken_refs)
        self.assertTrue(ok, output)

        external = Path(tempfile.mkdtemp(prefix="docs_tool_ext_pin_"))
        (external / "en" / "modules" / "ROOT").mkdir(parents=True)
        try:
            dt.EXTERNAL_COMPONENTS = dt._load_external_components([f"ADB={external}"])
            ok, output = self.run_check(dt.check_pages_broken_refs)
            self.assertTrue(ok, output)
        finally:
            shutil.rmtree(external, ignore_errors=True)

    def test_version_pinned_include_does_not_count_as_tag_usage(self):
        """A tag only ever pulled in via a version-pinned include must
        still be reported orphaned -- the tool can't verify usage against
        a pinned historical version it doesn't have checked out."""
        self.antora_yml("en", "TEST")
        self.write(
            "en/modules/ROOT/partials/snippet.adoc",
            "tag::example[]\nbody\nend::example[]\n",
        )
        self.write(
            "en/modules/ROOT/pages/page.adoc",
            "include::6.29.1.1@ADB:how-to:metrics.adoc[tag=example]\n",
        )

        ok, output = self.run_check(dt.check_tags_orphaned)
        self.assertFalse(ok)
        self.assertIn("tag::example[]", output)


class PagesNoYoTests(FixtureTestCase):
    def test_yo_is_flagged(self):
        self.write("ru/modules/ROOT/pages/page.adoc", "Настройте параметр для Ёлка и ёж.\n")
        ok, output = self.run_check(dt.check_pages_no_yo)
        self.assertFalse(ok)
        self.assertIn("page.adoc:1:", output)

    def test_no_yo_passes(self):
        self.write("ru/modules/ROOT/pages/page.adoc", "Настройте параметр елка.\n")
        ok, _ = self.run_check(dt.check_pages_no_yo)
        self.assertTrue(ok)

    def test_page_author_attribute_is_exempt(self):
        self.write("ru/modules/ROOT/pages/page.adoc", ":page-author: Фёдоров\n\nОбычный текст.\n")
        ok, _ = self.run_check(dt.check_pages_no_yo)
        self.assertTrue(ok)

    def test_page_author_exemption_does_not_leak_to_other_lines(self):
        self.write("ru/modules/ROOT/pages/page.adoc", ":page-author: Фёдоров\n\nОн живёт здесь.\n")
        ok, output = self.run_check(dt.check_pages_no_yo)
        self.assertFalse(ok)
        self.assertIn("живёт", output)

    def test_en_pages_are_not_scanned(self):
        self.write("en/modules/ROOT/pages/page.adoc", "This mentions ёж as a loanword.\n")
        ok, _ = self.run_check(dt.check_pages_no_yo)
        self.assertTrue(ok)


class PagesRuLatinHomoglyphsTests(FixtureTestCase):
    def test_mixed_script_word_is_flagged(self):
        self.write("ru/modules/ROOT/pages/page.adoc",
                    "Настройте PAMавторизацию для example входа.\n")
        ok, output = self.run_check(dt.check_pages_ru_latin_homoglyphs)
        self.assertFalse(ok)
        self.assertIn("PAMавторизацию", output)

    def test_standalone_homoglyph_letter_is_flagged(self):
        self.write("ru/modules/ROOT/pages/page.adoc",
                    "Взаимодействие c другими example службами.\n")
        ok, output = self.run_check(dt.check_pages_ru_latin_homoglyphs)
        self.assertFalse(ok)
        self.assertIn("'c'", output)

    def test_hyphenated_identifier_is_not_flagged(self):
        """gcc-c++/xerces-c-devel-style identifiers leave a bare single
        letter between hyphens -- must not be treated as a standalone
        homoglyph typo."""
        self.write("ru/modules/ROOT/pages/page.adoc",
                    "Пакет называется xerces-c-devel для example сборки.\n")
        ok, output = self.run_check(dt.check_pages_ru_latin_homoglyphs)
        self.assertTrue(ok, output)

    def test_parenthetical_enum_code_is_not_flagged(self):
        """Postgres catalog docs' own "SHARED_DEPENDENCY_OWNER (o)" enum-code
        convention must not be flagged as a typo'd Cyrillic letter."""
        self.write("ru/modules/ROOT/pages/page.adoc",
                    "Тип SHARED_DEPENDENCY_OWNER (o) example используется здесь.\n")
        ok, output = self.run_check(dt.check_pages_ru_latin_homoglyphs)
        self.assertTrue(ok, output)

    def test_pipe_delimited_short_code_is_not_flagged(self):
        """pg_dump.adoc-style "c | custom" description-list short codes must
        not be flagged."""
        self.write("ru/modules/ROOT/pages/page.adoc",
                    "c | custom example формат вывода.\n")
        ok, output = self.run_check(dt.check_pages_ru_latin_homoglyphs)
        self.assertTrue(ok, output)

    def test_bold_italic_ui_string_is_not_flagged(self):
        """DBeaver-style "*_Connect to a database_*" verbatim UI quoting is
        kept in English by convention; the standalone "a" inside it must not
        be flagged."""
        self.write("ru/modules/ROOT/pages/page.adoc",
                    "Нажмите *_Connect to a database_* example чтобы продолжить.\n")
        ok, output = self.run_check(dt.check_pages_ru_latin_homoglyphs)
        self.assertTrue(ok, output)

    def test_uppercase_c_is_not_flagged(self):
        """Uppercase Latin C collides with real usage like "the C language";
        the standalone-letter check is deliberately lowercase-only."""
        self.write("ru/modules/ROOT/pages/page.adoc",
                    "В языках, отличных от SQL и C, example используется другой синтаксис.\n")
        ok, output = self.run_check(dt.check_pages_ru_latin_homoglyphs)
        self.assertTrue(ok, output)

    def test_description_attribute_value_is_flagged(self):
        """:description:/:page-htmltitle: are ":"-prefixed structural
        attribute lines that _iter_prose_lines skips wholesale, but their
        values render as real Russian prose (<meta description>/<title>)
        and must still be scanned for homoglyph typos."""
        self.write("ru/modules/ROOT/pages/page.adoc",
                    ":description: Работа c example базой данных.\n"
                    "\n"
                    "Текст страницы.\n")
        ok, output = self.run_check(dt.check_pages_ru_latin_homoglyphs)
        self.assertFalse(ok)
        self.assertIn("'c'", output)

    def test_other_attribute_lines_still_not_scanned(self):
        """Non-prose attribute lines (e.g. :page-author:) must stay out of
        scope -- only :description:/:page-htmltitle: values are scanned."""
        self.write("ru/modules/ROOT/pages/page.adoc",
                    ":page-author: c example\n"
                    "\n"
                    "Текст страницы.\n")
        ok, output = self.run_check(dt.check_pages_ru_latin_homoglyphs)
        self.assertTrue(ok, output)


class PagesFilePathItalicsTests(FixtureTestCase):
    def test_extension_whitelist_match_is_flagged(self):
        self.antora_yml("en", "TEST")
        self.write("en/modules/ROOT/pages/page.adoc",
                    "Edit the postgresql.conf file to change settings.\n")
        ok, output = self.run_check(dt.check_pages_file_path_italics)
        self.assertFalse(ok)
        self.assertIn("postgresql.conf", output)

    def test_absolute_dir_path_is_flagged(self):
        self.antora_yml("en", "TEST")
        self.write("en/modules/ROOT/pages/page.adoc",
                    "The configuration lives under /etc/postgresql/data for this cluster.\n")
        ok, output = self.run_check(dt.check_pages_file_path_italics)
        self.assertFalse(ok)
        self.assertIn("/etc/postgresql/data", output)

    def test_bare_basename_with_phrase_is_flagged(self):
        """"src" is only flagged with the "a/the X folder" grammar gate,
        unlike the unambiguous basenames below."""
        self.antora_yml("en", "TEST")
        self.write("en/modules/ROOT/pages/page.adoc",
                    "Copy the compiled binaries into the src folder before packaging.\n")
        ok, output = self.run_check(dt.check_pages_file_path_italics)
        self.assertFalse(ok)
        self.assertIn("src", output)

    def test_unambiguous_basename_is_flagged_without_phrase(self):
        """"bin" is flagged on bold/code-span alone, with no "a/the X
        folder" phrase required."""
        self.antora_yml("en", "TEST")
        self.write("en/modules/ROOT/pages/page.adoc",
                    "The install places scripts under `bin` for convenience.\n")
        ok, output = self.run_check(dt.check_pages_file_path_italics)
        self.assertFalse(ok)
        self.assertIn("bin", output)

    def test_compound_underscore_or_slash_word_is_flagged(self):
        self.antora_yml("en", "TEST")
        self.write("en/modules/ROOT/pages/page.adoc",
                    "Check the greengage_path folder and the backup/adb folder for the installed binaries.\n")
        ok, output = self.run_check(dt.check_pages_file_path_italics)
        self.assertFalse(ok)
        self.assertIn("greengage_path", output)
        self.assertIn("backup/adb", output)

    def test_code_span_word_before_file_is_flagged(self):
        self.antora_yml("en", "TEST")
        self.write("en/modules/ROOT/pages/page.adoc",
                    "Delete the `backup` folder once the migration finishes.\n")
        ok, output = self.run_check(dt.check_pages_file_path_italics)
        self.assertFalse(ok)
        self.assertIn("backup", output)

    def test_camelcase_parameter_name_is_not_flagged(self):
        """A camelCase code-span word before "directory" reads as a config
        parameter name (e.g. zookeeper's `dataLogDir`), not a literal Unix
        directory name."""
        self.antora_yml("en", "TEST")
        self.write("en/modules/ROOT/pages/page.adoc",
                    "Configure the `dataLogDir` directory for dedicated disk usage.\n")
        ok, output = self.run_check(dt.check_pages_file_path_italics)
        self.assertTrue(ok, output)

    def test_generic_noun_format_descriptor_is_not_flagged(self):
        """pg_dump's own "the `directory` archive format" names a format,
        not a literal directory."""
        self.antora_yml("en", "TEST")
        self.write("en/modules/ROOT/pages/page.adoc",
                    "Parallel dumps are only supported for the `directory` archive format.\n")
        ok, output = self.run_check(dt.check_pages_file_path_italics)
        self.assertTrue(ok, output)

    def test_dotfile_mention_is_flagged(self):
        self.antora_yml("en", "TEST")
        self.write("en/modules/ROOT/pages/page.adoc",
                    "For example, edit .bashrc to update your shell startup.\n")
        ok, output = self.run_check(dt.check_pages_file_path_italics)
        self.assertFalse(ok)
        self.assertIn(".bashrc", output)

    def test_already_italicized_is_not_flagged(self):
        self.antora_yml("en", "TEST")
        self.write("en/modules/ROOT/pages/page.adoc",
                    "Edit the _postgresql.conf_ file to change settings.\n")
        ok, output = self.run_check(dt.check_pages_file_path_italics)
        self.assertTrue(ok, output)


class PagesTableCellPeriodsTests(FixtureTestCase):
    def test_last_line_of_cell_with_period_is_flagged(self):
        self.antora_yml("en", "TEST")
        self.write(
            "en/modules/ROOT/pages/table.adoc",
            "|===\n|Description\n\nSome description sentence.\n|===\n",
        )
        ok, output = self.run_check(dt.check_pages_table_cell_periods)
        self.assertFalse(ok)
        self.assertIn("Some description sentence.", output)

    def test_list_in_cell_is_exempt(self):
        self.antora_yml("en", "TEST")
        self.write(
            "en/modules/ROOT/pages/table.adoc",
            "|===\n|Options\n\n* First item.\n|===\n",
        )
        ok, output = self.run_check(dt.check_pages_table_cell_periods)
        self.assertTrue(ok, output)

    def test_admonition_in_cell_is_exempt(self):
        self.antora_yml("en", "TEST")
        self.write(
            "en/modules/ROOT/pages/table.adoc",
            "|===\n|Warning\n\nNOTE: This requires elevated privileges.\n|===\n",
        )
        ok, output = self.run_check(dt.check_pages_table_cell_periods)
        self.assertTrue(ok, output)

    def test_abbreviation_exceptions(self):
        self.antora_yml("ru", "TEST")
        self.write(
            "ru/modules/ROOT/pages/table_a.adoc",
            "|===\n|Ссылка\n\nПодробнее см. документацию и т.д.\n|===\n",
        )
        self.write(
            "ru/modules/ROOT/pages/table_b.adoc",
            "|===\n|Мин.\n|===\n",
        )
        ok, output = self.run_check(dt.check_pages_table_cell_periods)
        self.assertTrue(ok, output)

    def test_multi_cell_single_line_is_split(self):
        """A compact header-style row packs several plain "|"-cells on one
        physical line; each must be checked individually, not just the last
        one on the line."""
        self.antora_yml("en", "TEST")
        self.write(
            "en/modules/ROOT/pages/table.adoc",
            "|===\n|Algorithm |Compression ratio. |Min |Max\n|===\n",
        )
        ok, output = self.run_check(dt.check_pages_table_cell_periods)
        self.assertFalse(ok)
        self.assertIn("Compression ratio.", output)


class PagesTranslationTests(FixtureTestCase):
    def test_identical_line_is_flagged_as_untranslated(self):
        self.write("en/modules/ROOT/pages/page.adoc",
                    "This is a real sentence with enough words.\n")
        self.write("ru/modules/ROOT/pages/page.adoc",
                    "This is a real sentence with enough words.\n")
        ok, output = self.run_check(dt.check_pages_translation)
        self.assertFalse(ok)
        self.assertIn("UNTRANSLATED", output)

    def test_code_block_is_skipped(self):
        """A code block that's identical between EN and RU (as code
        legitimately is) must not be flagged as untranslated prose."""
        self.write(
            "en/modules/ROOT/pages/page.adoc",
            "----\necho hello world\n----\nThis sentence has been properly translated for real.\n",
        )
        self.write(
            "ru/modules/ROOT/pages/page.adoc",
            "----\necho hello world\n----\nЭто предложение действительно переведено на русский.\n",
        )
        ok, output = self.run_check(dt.check_pages_translation)
        self.assertTrue(ok, output)

    def test_stopword_flagging_is_always_on(self):
        """A leftover English stopword inside otherwise-Russian text is
        flagged on a plain run; --verbose only appends the matched word."""
        self.write(
            "en/modules/ROOT/pages/page.adoc",
            "This paragraph explains the new caching behavior in detail.\n",
        )
        self.write(
            "ru/modules/ROOT/pages/page.adoc",
            "Этот абзац объясняет and новое поведение кэширования.\n",
        )
        ok, output = self.run_check(dt.check_pages_translation, verbose=False)
        self.assertFalse(ok)
        self.assertIn("SUSPECT", output)
        self.assertNotIn("[and]", output)

        ok, output = self.run_check(dt.check_pages_translation, verbose=True)
        self.assertFalse(ok)
        self.assertIn("SUSPECT", output)
        self.assertIn("[and]", output)

    def test_description_attribute_untranslated_is_flagged(self):
        """:description:/:page-htmltitle: are ":"-prefixed structural
        attribute lines that _is_skip_line treats as non-prose, but their
        values render as real page <meta description>/<title> text and
        must still be checked for copy-pasted English."""
        self.write("en/modules/ROOT/pages/page.adoc",
                    ":description: This page explains the new caching behavior.\n")
        self.write("ru/modules/ROOT/pages/page.adoc",
                    ":description: This page explains the new caching behavior.\n")
        ok, output = self.run_check(dt.check_pages_translation)
        self.assertFalse(ok)
        self.assertIn("UNTRANSLATED", output)

    def test_description_attribute_translated_passes(self):
        self.write("en/modules/ROOT/pages/page.adoc",
                    ":description: This page explains the new caching behavior.\n")
        self.write("ru/modules/ROOT/pages/page.adoc",
                    ":description: На этой странице описано новое поведение кэширования.\n")
        ok, output = self.run_check(dt.check_pages_translation)
        self.assertTrue(ok, output)

    def test_other_attribute_lines_still_not_scanned(self):
        """Non-prose attribute lines outside the description/title
        carve-out must stay excluded, even when copied verbatim and long
        enough to pass the word-count threshold."""
        self.write("en/modules/ROOT/pages/page.adoc",
                    ":page-partial: This attribute is not real page prose.\n")
        self.write("ru/modules/ROOT/pages/page.adoc",
                    ":page-partial: This attribute is not real page prose.\n")
        ok, output = self.run_check(dt.check_pages_translation)
        self.assertTrue(ok, output)


class ContentRelpathTests(unittest.TestCase):
    def test_finds_subpath_after_pages(self):
        p = Path("en/modules/ROOT/pages/reference/sql_commands/create_role.adoc")
        self.assertEqual(dt._content_relpath(p), Path("reference/sql_commands/create_role.adoc"))

    def test_finds_subpath_after_partials(self):
        p = Path("en/modules/ROOT/partials/reference/shared.adoc")
        self.assertEqual(dt._content_relpath(p), Path("reference/shared.adoc"))

    def test_top_level_page_has_no_leading_directory(self):
        p = Path("en/modules/ROOT/pages/index.adoc")
        self.assertEqual(dt._content_relpath(p), Path("index.adoc"))

    def test_none_when_neither_pages_nor_partials_in_path(self):
        self.assertIsNone(dt._content_relpath(Path("en/modules/ROOT/examples/foo.sql")))


class ContentRelpartsStemTests(unittest.TestCase):
    def test_strips_adoc_extension_from_final_segment_only(self):
        p = Path("en/modules/ROOT/pages/reference/gp_toolkit/gp_ao_diskquota_no_perm_map.adoc")
        self.assertEqual(dt._content_relparts_stem(p), ("reference", "gp_toolkit", "gp_ao_diskquota_no_perm_map"))

    def test_bare_top_level_file(self):
        p = Path("en/modules/ROOT/pages/index.adoc")
        self.assertEqual(dt._content_relparts_stem(p), ("index",))

    def test_none_when_not_under_pages_or_partials(self):
        self.assertIsNone(dt._content_relparts_stem(Path("en/modules/ROOT/examples/foo.sql")))


class EndsWithPartsTests(unittest.TestCase):
    def test_bare_single_segment_matches_last_segment_only(self):
        self.assertTrue(dt._ends_with_parts(("reference", "gp_toolkit", "gp_ao"), ("gp_ao",)))

    def test_multi_segment_suffix_must_match_in_order(self):
        self.assertTrue(dt._ends_with_parts(("reference", "gp_toolkit", "gp_ao"), ("gp_toolkit", "gp_ao")))
        self.assertFalse(dt._ends_with_parts(("reference", "gp_toolkit", "gp_ao"), ("gp_ao", "gp_toolkit")))

    def test_suffix_longer_than_parts_never_matches(self):
        self.assertFalse(dt._ends_with_parts(("gp_ao",), ("reference", "gp_toolkit", "gp_ao")))

    def test_non_matching_middle_segment_fails(self):
        self.assertFalse(dt._ends_with_parts(("reference", "gp_toolkit", "gp_ao"), ("reference", "utils", "gp_ao")))


class PageAllowedDirectoryFilterTests(unittest.TestCase):
    """_page_allowed's directory-matching half of --page: recursive,
    content-relative, module-agnostic."""

    def setUp(self):
        self._orig_page_filter = dt._PAGE_FILTER

    def tearDown(self):
        dt._PAGE_FILTER = self._orig_page_filter

    def test_direct_child_matches(self):
        dt._PAGE_FILTER = {"names": set(), "dirs": {("reference", "sql_commands")}}
        p = Path("en/modules/ROOT/pages/reference/sql_commands/create_role.adoc")
        self.assertTrue(dt._page_allowed(p))

    def test_nested_grandchild_also_matches_recursively(self):
        dt._PAGE_FILTER = {"names": set(), "dirs": {("reference",)}}
        p = Path("en/modules/ROOT/pages/reference/sql_commands/create_role.adoc")
        self.assertTrue(dt._page_allowed(p))

    def test_sibling_directory_does_not_match(self):
        dt._PAGE_FILTER = {"names": set(), "dirs": {("reference", "sql_commands")}}
        p = Path("en/modules/ROOT/pages/reference/utils/gpstate.adoc")
        self.assertFalse(dt._page_allowed(p))

    def test_segment_prefix_does_not_falsely_match(self):
        """"reference/sql" must not match "reference/sql_commands/..." --
        matching is by whole path segment, not a raw string prefix."""
        dt._PAGE_FILTER = {"names": set(), "dirs": {("reference", "sql")}}
        p = Path("en/modules/ROOT/pages/reference/sql_commands/create_role.adoc")
        self.assertFalse(dt._page_allowed(p))

    def test_matches_regardless_of_module_or_language(self):
        dt._PAGE_FILTER = {"names": set(), "dirs": {("reference", "sql_commands")}}
        for root in (
            "en/modules/ROOT/pages/reference/sql_commands/create_role.adoc",
            "ru/modules/ROOT/pages/reference/sql_commands/create_role.adoc",
            "en/modules/how-to/pages/reference/sql_commands/create_role.adoc",
        ):
            self.assertTrue(dt._page_allowed(Path(root)), root)

    def test_file_filter_and_directory_filter_both_apply(self):
        dt._PAGE_FILTER = {"names": {("index",)}, "dirs": {("reference",)}}
        self.assertTrue(dt._page_allowed(Path("en/modules/ROOT/pages/index.adoc")))
        self.assertTrue(dt._page_allowed(Path("en/modules/ROOT/pages/reference/gpstate.adoc")))
        self.assertFalse(dt._page_allowed(Path("en/modules/ROOT/pages/unrelated.adoc")))

    def test_bare_filename_matches_regardless_of_directory(self):
        """Unqualified single-segment NAME (the pre-existing form, e.g.
        --page foo.adoc) must keep matching a file with that stem in ANY
        directory -- no behavior change for the common case."""
        dt._PAGE_FILTER = {"names": {("gp_ao",)}, "dirs": set()}
        self.assertTrue(dt._page_allowed(Path("en/modules/ROOT/pages/reference/gp_toolkit/gp_ao.adoc")))
        self.assertTrue(dt._page_allowed(Path("en/modules/ROOT/pages/gp_ao.adoc")))

    def test_qualified_name_disambiguates_same_stem_in_different_directories(self):
        """The actual bug this was built to fix: a directory-qualified NAME
        (--page reference/gp_toolkit/gp_ao.adoc) must select only the file
        under that directory, not silently match a same-named file
        elsewhere too."""
        dt._PAGE_FILTER = {"names": {("gp_toolkit", "gp_ao")}, "dirs": set()}
        self.assertTrue(dt._page_allowed(Path("en/modules/ROOT/pages/reference/gp_toolkit/gp_ao.adoc")))
        self.assertFalse(dt._page_allowed(Path("en/modules/ROOT/pages/reference/utils/gp_ao.adoc")))
        self.assertFalse(dt._page_allowed(Path("en/modules/ROOT/pages/gp_ao.adoc")))

    def test_fully_qualified_name_is_also_module_agnostic(self):
        """The module name is never part of the matched path (see
        _content_relpath), so even a fully directory-qualified name still
        matches the same subpath in any module -- consistent with the
        directory filter's own module-agnostic behavior."""
        dt._PAGE_FILTER = {"names": {("reference", "gp_toolkit", "gp_ao")}, "dirs": set()}
        self.assertTrue(dt._page_allowed(Path("en/modules/ROOT/pages/reference/gp_toolkit/gp_ao.adoc")))
        self.assertTrue(dt._page_allowed(Path("en/modules/how-to/pages/reference/gp_toolkit/gp_ao.adoc")))
        self.assertFalse(dt._page_allowed(Path("en/modules/ROOT/pages/reference/utils/gp_ao.adoc")))


class ResolvePageStemQualifiedNameTests(unittest.TestCase):
    """--sync's fallback (_resolve_page_stem) gets the same suffix-matching
    treatment as --page, for the same reason: a bare filename can be
    ambiguous across directories, and a qualifying prefix should actually
    disambiguate instead of being silently dropped."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="docs_tool_resolve_stem_")
        self._orig_cwd = os.getcwd()
        os.chdir(self._tmpdir)

    def tearDown(self):
        os.chdir(self._orig_cwd)
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def write(self, rel_path: str) -> Path:
        p = Path(self._tmpdir) / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("", encoding="utf-8")
        return p

    def test_bare_name_matches_across_directories_ambiguously(self):
        self.write("en/modules/ROOT/pages/reference/gp_toolkit/gp_ao.adoc")
        self.write("en/modules/ROOT/pages/reference/utils/gp_ao.adoc")
        matches = dt._resolve_page_stem(("gp_ao",))
        self.assertEqual(len(matches), 2)

    def test_qualified_name_disambiguates(self):
        self.write("en/modules/ROOT/pages/reference/gp_toolkit/gp_ao.adoc")
        self.write("en/modules/ROOT/pages/reference/utils/gp_ao.adoc")
        matches = dt._resolve_page_stem(("gp_toolkit", "gp_ao"))
        self.assertEqual(matches, [Path("en/modules/ROOT/pages/reference/gp_toolkit/gp_ao.adoc")])

    def test_run_sync_accepts_qualified_name_end_to_end(self):
        self.write("en/modules/ROOT/pages/reference/gp_toolkit/gp_ao.adoc")
        self.write("en/modules/ROOT/pages/reference/utils/gp_ao.adoc")
        self.write("ru/modules/ROOT/pages/reference/gp_toolkit/gp_ao.adoc")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            dt.run_sync("reference/gp_toolkit/gp_ao.adoc", dry_run=True)
        self.assertIn("reference/gp_toolkit/gp_ao.adoc", buf.getvalue())
        self.assertIn("already matches", buf.getvalue())

    def test_run_sync_bare_name_still_ambiguous(self):
        self.write("en/modules/ROOT/pages/reference/gp_toolkit/gp_ao.adoc")
        self.write("en/modules/ROOT/pages/reference/utils/gp_ao.adoc")
        with self.assertRaises(SystemExit) as ctx:
            dt.run_sync("gp_ao.adoc", dry_run=True)
        self.assertIn("matches multiple files", str(ctx.exception))


class PagesTerminologyTests(FixtureTestCase):
    def _set_glossary(self, *rows: str):
        """Each row is an "en|ru|ru_pattern" pipe-delimited line (no note, no trailing newline)."""
        path = self.write("glossary.psv", "en|ru|ru_pattern|note\n" + "\n".join(r + "|" for r in rows) + "\n")
        dt.GLOSSARY = dt._load_glossary([str(path)])

    def test_missing_glossary_exits(self):
        dt.GLOSSARY = {}
        with self.assertRaises(SystemExit):
            dt.check_pages_terminology()

    def test_missing_glossary_in_multi_check_run_skips_not_aborts(self):
        """A multi-family `check` or `--all-checks` sweeps terminology in; with
        no glossary it must be dropped with a note, not abort the run."""
        self.write("ru/modules/ROOT/pages/page.adoc", "Обычный текст.\n")
        cwd = os.getcwd()
        os.chdir(self._tmpdir)  # away from this repo's own greengagedb-glossary.psv
        try:
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                ok = dt._run_selected(["pages-no-yo", "pages-terminology"], False, None)
        finally:
            os.chdir(cwd)
        self.assertTrue(ok)
        self.assertIn("skipping terminology check", err.getvalue())

    def test_correct_translation_passes(self):
        self._set_glossary("host|хост|хост<>")
        self.write("en/modules/ROOT/pages/page.adoc", "Connect to the host over SSH.\n")
        self.write("ru/modules/ROOT/pages/page.adoc", "Подключитесь к хосту по SSH.\n")
        ok, output = self.run_check(dt.check_pages_terminology)
        self.assertTrue(ok, output)

    def test_missing_translation_is_flagged(self):
        self._set_glossary("host|хост|хост<>")
        self.write("en/modules/ROOT/pages/page.adoc", "Connect to the host over SSH.\n")
        self.write("ru/modules/ROOT/pages/page.adoc", "Подключитесь к серверу по SSH.\n")
        ok, output = self.run_check(dt.check_pages_terminology)
        self.assertFalse(ok)
        self.assertIn("MISMATCH", output)
        self.assertIn("'host'", output)

    def test_duplicate_key_accepts_either_translation(self):
        self._set_glossary(
            "session|сессия|сесси<>",
            "session|сеанс|сеанс<>",
        )
        self.write("en/modules/ROOT/pages/page.adoc", "Start a new session before continuing.\n")
        self.write("ru/modules/ROOT/pages/page.adoc", "Перед продолжением начните новый сеанс.\n")
        ok, output = self.run_check(dt.check_pages_terminology)
        self.assertTrue(ok, output)

    def test_do_not_translate_term_must_stay_literal(self):
        self._set_glossary("Greengage DB|не переводить|Greengage DB")
        self.write("en/modules/ROOT/pages/page.adoc", "This is a Greengage DB cluster.\n")
        self.write("ru/modules/ROOT/pages/page.adoc", "Это кластер GPDB.\n")
        ok, output = self.run_check(dt.check_pages_terminology)
        self.assertFalse(ok)
        self.assertIn("MISMATCH", output)

        self.write("ru/modules/ROOT/pages/page.adoc", "Это кластер Greengage DB.\n")
        ok, output = self.run_check(dt.check_pages_terminology)
        self.assertTrue(ok, output)

    def test_term_inside_code_span_is_skipped(self):
        self._set_glossary("host|хост|хост<>")
        self.write("en/modules/ROOT/pages/page.adoc", "Set the `host` config option.\n")
        self.write("ru/modules/ROOT/pages/page.adoc", "Настройте параметр `host`.\n")
        ok, output = self.run_check(dt.check_pages_terminology)
        self.assertTrue(ok, output)

    def test_page_filter_scopes_check(self):
        self._set_glossary("host|хост|хост<>")
        self.write("en/modules/ROOT/pages/keep.adoc", "Connect to the host.\n")
        self.write("ru/modules/ROOT/pages/keep.adoc", "Подключитесь к серверу.\n")
        self.write("en/modules/ROOT/pages/skip.adoc", "Connect to the host.\n")
        self.write("ru/modules/ROOT/pages/skip.adoc", "Подключитесь к серверу.\n")
        dt._PAGE_FILTER = {"names": {("keep",)}, "dirs": set()}
        ok, output = self.run_check(dt.check_pages_terminology)
        self.assertFalse(ok)
        self.assertIn("keep.adoc", output)
        self.assertNotIn("skip.adoc", output)

    def test_page_filter_scopes_check_by_directory(self):
        self._set_glossary("host|хост|хост<>")
        self.write("en/modules/ROOT/pages/reference/sql_commands/keep.adoc", "Connect to the host.\n")
        self.write("ru/modules/ROOT/pages/reference/sql_commands/keep.adoc", "Подключитесь к серверу.\n")
        self.write("en/modules/ROOT/pages/reference/utils/skip.adoc", "Connect to the host.\n")
        self.write("ru/modules/ROOT/pages/reference/utils/skip.adoc", "Подключитесь к серверу.\n")
        dt._PAGE_FILTER = {"names": set(), "dirs": {("reference", "sql_commands")}}
        ok, output = self.run_check(dt.check_pages_terminology)
        self.assertFalse(ok)
        self.assertIn("keep.adoc", output)
        self.assertNotIn("skip.adoc", output)

    def test_description_attribute_mismatch_is_flagged(self):
        """:description:/:page-htmltitle: values render as real page
        <meta description>/<title> prose despite being ":"-prefixed
        structural attribute lines that _is_skip_line otherwise treats as
        non-prose -- a glossary violation inside them must still be caught."""
        self._set_glossary("host|хост|хост<>")
        self.write("en/modules/ROOT/pages/page.adoc",
                    ":description: Learn how to connect to the host.\n")
        self.write("ru/modules/ROOT/pages/page.adoc",
                    ":description: Узнайте, как подключиться к серверу.\n")
        ok, output = self.run_check(dt.check_pages_terminology)
        self.assertFalse(ok)
        self.assertIn("MISMATCH", output)
        self.assertIn("'host'", output)

    def test_description_attribute_correct_translation_passes(self):
        self._set_glossary("host|хост|хост<>")
        self.write("en/modules/ROOT/pages/page.adoc",
                    ":description: Learn how to connect to the host.\n")
        self.write("ru/modules/ROOT/pages/page.adoc",
                    ":description: Узнайте, как подключиться к хосту.\n")
        ok, output = self.run_check(dt.check_pages_terminology)
        self.assertTrue(ok, output)

    def test_term_inside_allcaps_command_name_is_skipped(self):
        """An SQL command name like "ALTER RESOURCE QUEUE" is kept
        untranslated by house style and appears unmarked-up (no code span)
        in :page-htmltitle:/:description: values -- a glossary term whose
        words happen to compose part of that name (e.g. "resource queue")
        must not be flagged just because the RU side rightly left the
        command name in English too."""
        self._set_glossary("resource queue|ресурсная очередь|ресурсн<> очеред<>")
        self.write("en/modules/ROOT/pages/page.adoc",
                    ":page-htmltitle: Overview of the ALTER RESOURCE QUEUE SQL command\n")
        self.write("ru/modules/ROOT/pages/page.adoc",
                    ":page-htmltitle: Обзор SQL-команды ALTER RESOURCE QUEUE\n")
        ok, output = self.run_check(dt.check_pages_terminology)
        self.assertTrue(ok, output)

    def test_other_attribute_lines_still_not_scanned(self):
        self._set_glossary("host|хост|хост<>")
        self.write("en/modules/ROOT/pages/page.adoc",
                    ":page-partial: Connect to the host over SSH.\n")
        self.write("ru/modules/ROOT/pages/page.adoc",
                    ":page-partial: Connect to the server over SSH.\n")
        ok, output = self.run_check(dt.check_pages_terminology)
        self.assertTrue(ok, output)

    def test_repeated_term_translated_every_time_passes(self):
        self._set_glossary("host|хост|хост<>")
        self.write("en/modules/ROOT/pages/page.adoc",
                    "The primary host talks to the standby host.\n")
        self.write("ru/modules/ROOT/pages/page.adoc",
                    "Основной хост общается с резервным хостом.\n")
        ok, output = self.run_check(dt.check_pages_terminology)
        self.assertTrue(ok, output)

    def test_repeated_term_translated_once_is_flagged(self):
        self._set_glossary("host|хост|хост<>")
        self.write("en/modules/ROOT/pages/page.adoc",
                    "The primary host talks to the standby host.\n")
        self.write("ru/modules/ROOT/pages/page.adoc",
                    "Основной хост общается с резервным сервером.\n")
        ok, output = self.run_check(dt.check_pages_terminology)
        self.assertFalse(ok)
        self.assertIn("MISMATCH", output)
        self.assertIn("'host'", output)
        self.assertIn("2x", output)
        self.assertIn("1x", output)

    def test_several_different_terms_each_checked_on_one_line(self):
        self._set_glossary(
            "commit|фиксация|фиксац<>",
            "rollback|откат|откат<>",
        )
        self.write("en/modules/ROOT/pages/page.adoc",
                    "You can commit or rollback the change.\n")
        self.write("ru/modules/ROOT/pages/page.adoc",
                    "Вы можете выполнить фиксацию или отменить изменение.\n")
        ok, output = self.run_check(dt.check_pages_terminology)
        self.assertFalse(ok)
        self.assertNotIn("'commit'", output)  # "фиксацию" satisfies the commit entry
        self.assertIn("'rollback'", output)   # "отменить" is not the house-style "откат"

    def test_multiword_term_repeated_counts_by_scarcest_token(self):
        self._set_glossary("resource queue|ресурсная очередь|ресурсн<> очеред<>")
        self.write("en/modules/ROOT/pages/page.adoc",
                    "A resource queue limits load; each resource queue has a name.\n")
        self.write("ru/modules/ROOT/pages/page.adoc",
                    "Ресурсная очередь ограничивает нагрузку; ресурсная очередь имеет имя.\n")
        ok, output = self.run_check(dt.check_pages_terminology)
        self.assertTrue(ok, output)

    def test_ru_count_helper_counts_scarcest_token(self):
        entry = {
            "ru_display": {"ресурсная очередь"},
            "patterns": [dt._compile_glossary_pattern("ресурсн<> очеред<>")],
        }
        self.assertEqual(
            dt._glossary_entry_ru_count(entry, "ресурсная очередь и ресурсная очередь"), 2)
        self.assertEqual(
            dt._glossary_entry_ru_count(entry, "ресурсная и ресурсная очередь"), 1)
        self.assertEqual(
            dt._glossary_entry_ru_count(entry, "здесь ничего нет"), 0)


class PagesStructureParityTests(FixtureTestCase):
    def test_matching_structure_passes(self):
        self.write(
            "en/modules/ROOT/pages/page.adoc",
            "== Title\n\nSome intro text.\n\n=== Sub heading\n\nMore text.\n",
        )
        self.write(
            "ru/modules/ROOT/pages/page.adoc",
            "== Заголовок\n\nНекоторый текст.\n\n=== Подзаголовок\n\nЕщё текст.\n",
        )
        ok, output = self.run_check(dt.check_pages_structure_parity)
        self.assertTrue(ok, output)

    def test_heading_level_mismatch_is_flagged(self):
        self.write(
            "en/modules/ROOT/pages/page.adoc",
            "== Title\n\nText.\n\n=== Details\n\nMore text.\n",
        )
        self.write(
            "ru/modules/ROOT/pages/page.adoc",
            "== Заголовок\n\nТекст.\n\n==== Детали\n\nЕщё текст.\n",
        )
        ok, output = self.run_check(dt.check_pages_structure_parity)
        self.assertFalse(ok)
        self.assertIn("DIFF", output)

    def test_delimited_block_mismatch_is_flagged(self):
        """A source/code block's delimiters dropped in the RU translation
        (e.g. a translator accidentally deleting "----" lines) must be
        caught even though line counts might coincidentally still match."""
        self.write(
            "en/modules/ROOT/pages/page.adoc",
            "== Title\n\n[source,sql]\n----\nSELECT 1;\n----\n\nText after.\n",
        )
        self.write(
            "ru/modules/ROOT/pages/page.adoc",
            "== Заголовок\n\nSELECT 1;\n\nТекст после.\n",
        )
        ok, output = self.run_check(dt.check_pages_structure_parity)
        self.assertFalse(ok)
        self.assertIn("DIFF", output)

    def test_include_directive_target_mismatch_is_flagged(self):
        self.write(
            "en/modules/ROOT/pages/page.adoc",
            "== Title\n\ninclude::partial$foo.adoc[]\n",
        )
        self.write(
            "ru/modules/ROOT/pages/page.adoc",
            "== Заголовок\n\ninclude::partial$bar.adoc[]\n",
        )
        ok, output = self.run_check(dt.check_pages_structure_parity)
        self.assertFalse(ok)
        self.assertIn("DIFF", output)


class PagesLinkParityTests(FixtureTestCase):
    def test_matching_links_pass(self):
        self.write("en/modules/ROOT/pages/page.adoc",
                   "See xref:udf.adoc[functions] and https://example.com/x[docs].\n")
        self.write("ru/modules/ROOT/pages/page.adoc",
                   "См. xref:udf.adoc[функции] и https://example.com/x[документацию].\n")
        ok, out = self.run_check(dt.check_pages_link_parity)
        self.assertTrue(ok, out)

    def test_dropped_xref_is_flagged(self):
        self.write("en/modules/ROOT/pages/page.adoc",
                   "See xref:udf.adoc[functions] and xref:vacuum.adoc[VACUUM].\n")
        self.write("ru/modules/ROOT/pages/page.adoc",
                   "См. xref:udf.adoc[функции].\n")
        ok, out = self.run_check(dt.check_pages_link_parity)
        self.assertFalse(ok)
        self.assertIn("xref:vacuum.adoc", out)

    def test_translated_xref_fragment_is_not_a_finding(self):
        """Antora derives a section anchor from the (translated) heading,
        so an xref #fragment legitimately differs EN vs RU."""
        self.write("en/modules/ROOT/pages/page.adoc",
                   "See xref:install.adoc#check-installation[the check].\n")
        self.write("ru/modules/ROOT/pages/page.adoc",
                   "См. xref:install.adoc#проверка-установки[проверку].\n")
        ok, out = self.run_check(dt.check_pages_link_parity)
        self.assertTrue(ok, out)

    def test_wikipedia_language_swap_is_not_a_finding(self):
        self.write("en/modules/ROOT/pages/page.adoc",
                   "A https://en.wikipedia.org/wiki/Side-channel_attack[side channel].\n")
        self.write("ru/modules/ROOT/pages/page.adoc",
                   "https://ru.wikipedia.org/wiki/%D0%90%D1%82%D0%B0%D0%BA%D0%B0[атака].\n")
        ok, out = self.run_check(dt.check_pages_link_parity)
        self.assertTrue(ok, out)

    def test_language_path_segment_in_url_is_not_a_finding(self):
        self.write("en/modules/ROOT/pages/page.adoc",
                   "https://docs.example.com/en/docs/7/x.html[docs].\n")
        self.write("ru/modules/ROOT/pages/page.adoc",
                   "https://docs.example.com/ru/docs/7/x.html[док].\n")
        ok, out = self.run_check(dt.check_pages_link_parity)
        self.assertTrue(ok, out)

    def test_localized_image_filename_is_not_a_finding(self):
        self.write("en/modules/ROOT/pages/page.adoc", "image:clusters/downloads_en.png[]\n")
        self.write("ru/modules/ROOT/pages/page.adoc", "image:clusters/downloads_ru.png[]\n")
        ok, out = self.run_check(dt.check_pages_link_parity)
        self.assertTrue(ok, out)

    def test_link_in_a_code_block_is_ignored(self):
        self.write("en/modules/ROOT/pages/page.adoc",
                   "----\nxref:only-in-en.adoc[x]\n----\n")
        self.write("ru/modules/ROOT/pages/page.adoc", "----\n----\n")
        ok, out = self.run_check(dt.check_pages_link_parity)
        self.assertTrue(ok, out)


class PagesLiteralParityTests(FixtureTestCase):
    def test_matching_literals_pass(self):
        self.write("en/modules/ROOT/pages/page.adoc",
                   "Run `ANALYZE` on the `orders` table.\n")
        self.write("ru/modules/ROOT/pages/page.adoc",
                   "Выполните `ANALYZE` для таблицy `orders`.\n")
        ok, out = self.run_check(dt.check_pages_literal_parity)
        self.assertTrue(ok, out)

    def test_dropped_literal_is_flagged(self):
        self.write("en/modules/ROOT/pages/page.adoc",
                   "The `get_part_name()` function and the `SELECT` keyword.\n")
        self.write("ru/modules/ROOT/pages/page.adoc",
                   "Функция `get_part_name()`.\n")
        ok, out = self.run_check(dt.check_pages_literal_parity)
        self.assertFalse(ok)
        self.assertIn("`SELECT`", out)

    def test_trailing_parens_are_normalized(self):
        self.write("en/modules/ROOT/pages/page.adoc", "Call `get_part_name()`.\n")
        self.write("ru/modules/ROOT/pages/page.adoc", "Вызовите `get_part_name`.\n")
        ok, out = self.run_check(dt.check_pages_literal_parity)
        self.assertTrue(ok, out)

    def test_near_identical_pair_reported_as_changed(self):
        self.write("en/modules/ROOT/pages/page.adoc", "The `VACUUM` command.\n")
        self.write("ru/modules/ROOT/pages/page.adoc", "Команда `VACCUM`.\n")
        ok, out = self.run_check(dt.check_pages_literal_parity)
        self.assertFalse(ok)
        self.assertIn("CHANGED", out)

    def test_ignore_list_literal_is_not_flagged(self):
        self.write("en/modules/ROOT/pages/page.adoc", "It returns NULL here.\n")
        self.write("ru/modules/ROOT/pages/page.adoc", "Возвращает `NULL`.\n")
        ok, out = self.run_check(dt.check_pages_literal_parity)
        self.assertTrue(ok, out)

    def test_pure_punctuation_span_is_ignored(self):
        self.write("en/modules/ROOT/pages/page.adoc", "The `<=` operator.\n")
        self.write("ru/modules/ROOT/pages/page.adoc", "Оператор.\n")
        ok, out = self.run_check(dt.check_pages_literal_parity)
        self.assertTrue(ok, out)

    def test_repeated_literal_with_different_count_is_not_flagged(self):
        self.write("en/modules/ROOT/pages/page.adoc", "`foo` then it runs.\n")
        self.write("ru/modules/ROOT/pages/page.adoc", "`foo`, затем `foo` запускается.\n")
        ok, out = self.run_check(dt.check_pages_literal_parity)
        self.assertTrue(ok, out)


class SyncMergeTests(unittest.TestCase):
    """Unit tests for sync_merge -- the pure structural-alignment function
    behind --sync, no filesystem/git involved."""

    def test_new_en_section_is_inserted_untranslated(self):
        en_lines = ["== Intro", "Some text.", "== New Section", "More text."]
        ru_lines = ["== Введение", "Какой-то текст."]
        merged, inserted, replaced, pairs, force_synced, orphaned = dt.sync_merge(en_lines, ru_lines)
        self.assertEqual(merged, ["== Введение", "Какой-то текст.", "== New Section", "More text."])
        self.assertEqual(inserted, [["== New Section", "More text."]])
        self.assertEqual(replaced, [])

    def test_force_sync_type_corrects_drifted_technical_token(self):
        """A FORCE_SYNC_TYPES line (e.g. an include path) that's drifted
        between EN and RU is corrected to EN's version, the same way a
        stale `plpythonu` gets corrected to `plpython3u`."""
        en_lines = ["include::partial$new_name.adoc[]"]
        ru_lines = ["include::partial$old_name.adoc[]"]
        merged, inserted, replaced, pairs, force_synced, orphaned = dt.sync_merge(en_lines, ru_lines)
        self.assertEqual(merged, ["include::partial$new_name.adoc[]"])
        self.assertEqual(replaced, [("include::partial$old_name.adoc[]", "include::partial$new_name.adoc[]")])

    def test_prose_mismatch_is_not_overwritten(self):
        """A reworded EN paragraph must never silently overwrite existing
        RU prose via the plain structural merge -- that's handled
        separately (STALE VERSION marking) by run_sync's git-diff pass."""
        en_lines = ["Some rewritten English paragraph text here."]
        ru_lines = ["Другой русский текст здесь совершенно другой."]
        merged, inserted, replaced, pairs, force_synced, orphaned = dt.sync_merge(en_lines, ru_lines)
        self.assertEqual(merged, ru_lines)
        self.assertEqual(replaced, [])


class RunSyncTests(unittest.TestCase):
    """Integration tests for run_sync -- the --sync entry point, writing
    real files under a tempdir."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="docs_tool_sync_")
        self.root = Path(self._tmpdir)

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def write(self, rel_path: str, content: str) -> Path:
        p = self.root / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p

    def test_dry_run_does_not_write_file(self):
        en_path = self.write(
            "en/modules/ROOT/pages/foo.adoc",
            "== Title\n\nText.\n\n== New Section\n\nNew content added later.\n",
        )
        ru_original = "== Заголовок\n\nТекст.\n"
        ru_path = self.write("ru/modules/ROOT/pages/foo.adoc", ru_original)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            dt.run_sync(str(en_path), dry_run=True)

        self.assertEqual(ru_path.read_text(encoding="utf-8"), ru_original)
        self.assertNotIn("Updated", buf.getvalue())
        self.assertIn("New Section", buf.getvalue())

    def test_new_content_is_written_and_reported(self):
        en_path = self.write(
            "en/modules/ROOT/pages/foo.adoc",
            "== Title\n\nText.\n\n== New Section\n\nNew content added later.\n",
        )
        ru_path = self.write("ru/modules/ROOT/pages/foo.adoc", "== Заголовок\n\nТекст.\n")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            dt.run_sync(str(en_path), dry_run=False)

        result = ru_path.read_text(encoding="utf-8")
        self.assertIn("== Заголовок", result)  # existing RU prose preserved
        self.assertIn("== New Section", result)  # new EN section copied in
        self.assertIn("New content added later.", result)
        self.assertIn("Updated", buf.getvalue())
        self.assertIn("Inserted", buf.getvalue())


class RunSyncStemResolutionTests(unittest.TestCase):
    """--sync's fallback: when its argument isn't an existing path, resolve
    it as a bare filename (must end in .adoc, same as --page NAME) against
    discovered EN pages/partials, same lookup --page uses. Needs a real cwd
    change (unlike RunSyncTests) since module_roots() resolves
    EN_MODULES_ROOT/RU_MODULES_ROOT relative to the current directory."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="docs_tool_sync_stem_")
        self._orig_cwd = os.getcwd()
        os.chdir(self._tmpdir)

    def tearDown(self):
        os.chdir(self._orig_cwd)
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def write(self, rel_path: str, content: str) -> Path:
        p = Path(self._tmpdir) / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p

    def test_bare_filename_with_adoc_suffix_resolves(self):
        self.write("en/modules/ROOT/pages/foo.adoc", "== Title\n\nText.\n")
        self.write("ru/modules/ROOT/pages/foo.adoc", "== Заголовок\n\nТекст.\n")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            dt.run_sync("foo.adoc", dry_run=True)
        self.assertIn("already matches", buf.getvalue())

    def test_name_without_adoc_suffix_is_rejected(self):
        """AsciiDoc/Antora has no separate topic-id -- the filename is the
        identifier, so a bare stem (no .adoc) must be rejected outright,
        not silently treated as a stem to search for."""
        self.write("en/modules/ROOT/pages/foo.adoc", "== Title\n\nText.\n")

        with self.assertRaises(SystemExit) as ctx:
            dt.run_sync("foo", dry_run=True)
        self.assertIn("must end with .adoc", str(ctx.exception))

    def test_ambiguous_filename_across_modules_exits_with_candidate_list(self):
        self.write("en/modules/ROOT/pages/foo.adoc", "== Title\n\nText.\n")
        self.write("en/modules/how-to/pages/foo.adoc", "== Title\n\nText.\n")

        with self.assertRaises(SystemExit) as ctx:
            dt.run_sync("foo.adoc", dry_run=True)
        message = str(ctx.exception)
        self.assertIn("matches multiple files", message)
        self.assertIn("ROOT", message)
        self.assertIn("how-to", message)

    def test_unresolvable_filename_exits_with_clear_error(self):
        self.write("en/modules/ROOT/pages/foo.adoc", "== Title\n\nText.\n")

        with self.assertRaises(SystemExit) as ctx:
            dt.run_sync("no-such-page.adoc", dry_run=True)
        self.assertIn("no page/partial named", str(ctx.exception))

    def test_full_path_still_takes_precedence_over_filename_search(self):
        """An existing full path is used as-is, without going through
        filename resolution at all -- so it works even where a bare
        filename would be ambiguous."""
        en_path = self.write("en/modules/ROOT/pages/foo.adoc", "== Title\n\nText.\n")
        self.write("en/modules/how-to/pages/foo.adoc", "== Title\n\nText.\n")
        self.write("ru/modules/ROOT/pages/foo.adoc", "== Заголовок\n\nТекст.\n")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            dt.run_sync(str(en_path), dry_run=True)
        self.assertIn("already matches", buf.getvalue())


class MainPageValueFormTests(unittest.TestCase):
    """CLI-level routing in main(): a --page NAME ending in .adoc is a file
    filter, one that doesn't is a directory filter (AsciiDoc/Antora has no
    separate topic-id, so there's no ambiguity to worry about), and
    UNCOMMITTED is still the special sentinel. Only the --page parsing is
    under test, but a bare en/modules/ROOT still has to exist: a check run
    from a directory with no docs tree is refused outright (see
    _require_a_docs_tree)."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="docs_tool_main_page_")
        self._orig_cwd = os.getcwd()
        os.chdir(self._tmpdir)
        (Path(self._tmpdir) / "en" / "modules" / "ROOT" / "pages").mkdir(parents=True)
        self._orig_argv = sys.argv
        self._orig_page_filter = dt._PAGE_FILTER

    def tearDown(self):
        sys.argv = self._orig_argv
        dt._PAGE_FILTER = self._orig_page_filter
        os.chdir(self._orig_cwd)
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_name_with_adoc_suffix_becomes_a_file_filter(self):
        sys.argv = ["docs_tool.py", "--check-pages-no-cyrillic", "--page", "resource_groups.adoc"]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                dt.main()
        self.assertEqual(dt._PAGE_FILTER, {"names": {("resource_groups",)}, "dirs": set()})

    def test_bare_name_becomes_a_directory_filter(self):
        sys.argv = ["docs_tool.py", "--check-pages-no-cyrillic", "--page", "reference/sql_commands"]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                dt.main()
        self.assertEqual(dt._PAGE_FILTER, {"names": set(), "dirs": {("reference", "sql_commands")}})

    def test_trailing_slash_on_directory_is_ignored(self):
        sys.argv = ["docs_tool.py", "--check-pages-no-cyrillic", "--page", "reference/sql_commands/"]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                dt.main()
        self.assertEqual(dt._PAGE_FILTER, {"names": set(), "dirs": {("reference", "sql_commands")}})

    def test_uncommitted_sentinel_still_accepted(self):
        subprocess.run(["git", "init", "-q"], check=True)
        sys.argv = ["docs_tool.py", "--check-pages-no-cyrillic", "--page", "UNCOMMITTED"]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit) as ctx:
                dt.main()
        self.assertIn("no uncommitted", str(buf.getvalue()) + str(ctx.exception))


class CompletePageNameTests(FixtureTestCase):
    """--page/--sync's argcomplete tab-completion source: every EN/RU
    pages/partials .adoc filename across all discovered modules."""

    def test_collects_en_and_ru_filenames_deduplicated(self):
        self.antora_yml("en", "TEST")
        self.write("en/modules/ROOT/pages/foo.adoc", "")
        self.write("en/modules/ROOT/partials/bar.adoc", "")
        self.write("ru/modules/ROOT/pages/foo.adoc", "")  # same name as EN -- deduplicated
        self.write("ru/modules/ROOT/pages/baz.adoc", "")  # RU-only

        names = dt._complete_page_name()
        self.assertEqual(names, sorted(names))  # sorted for stable completion order
        self.assertEqual(set(names), {"foo.adoc", "bar.adoc", "baz.adoc"})

    def test_page_action_and_sync_action_wired_to_the_right_completer(self):
        """--page also accepts a directory (see _content_relpath), --sync
        doesn't -- each gets the completer matching what it actually accepts."""
        parser = dt.build_parser()
        actions_by_flag = {opt: a for a in parser._actions for opt in a.option_strings}
        self.assertIs(actions_by_flag["--page"].completer, dt._complete_page_or_dir_name)
        self.assertIs(actions_by_flag["--sync"].completer, dt._complete_page_name)

    def test_dir_completer_includes_every_nesting_level_with_trailing_slash(self):
        """Trailing "/" on every directory candidate (not "reference",
        "reference/") -- otherwise completion goes dead the instant the
        user types the "/" themselves, since nothing in the candidate list
        would start with what's already typed."""
        self.antora_yml("en", "TEST")
        self.write("en/modules/ROOT/pages/reference/sql_commands/create_role.adoc", "")

        names, dirs = dt._discover_page_completions()
        self.assertIn("create_role.adoc", names)
        self.assertEqual(dirs, {"reference/", "reference/sql_commands/"})

    def test_file_candidates_include_every_qualified_suffix_form(self):
        """A nested file gets one candidate per suffix length -- bare
        filename, then each directory-qualified form up to the full
        content-relative path -- so completion can keep narrowing down
        after a directory prefix instead of stopping at the bare name."""
        self.antora_yml("en", "TEST")
        self.write("en/modules/ROOT/pages/reference/gp_toolkit/gp_ao_diskquota_no_perm_map.adoc", "")

        names, _ = dt._discover_page_completions()
        self.assertEqual(names, {
            "gp_ao_diskquota_no_perm_map.adoc",
            "gp_toolkit/gp_ao_diskquota_no_perm_map.adoc",
            "reference/gp_toolkit/gp_ao_diskquota_no_perm_map.adoc",
        })


class SkippedComponentReportTests(FixtureTestCase):
    """A reference into a component with no --external-root is left
    unchecked, not reported broken. Name those components: a run that never
    looked at one otherwise reads exactly like a run that verified it --
    the same silent-clean-pass the --external-root warning already guards
    against for a *wrong* path, here for an *omitted* one."""

    def setUp(self):
        super().setUp()
        self.antora_yml("en", "TEST")
        self.write("en/modules/ROOT/pages/page.adoc",
                   "xref:docs-backup::intro.adoc[Backup]\n"
                   "xref:blog::post.adoc[Post]\n")

    def test_unregistered_components_are_named(self):
        ok, out = self.run_check(dt.check_pages_broken_refs)
        self.assertTrue(ok, out)          # left unchecked, not broken
        self.assertIn("docs-backup", out)
        self.assertIn("blog", out)
        self.assertIn("--external-root", out)

    def test_nothing_reported_when_every_reference_resolves_locally(self):
        self.write("en/modules/ROOT/pages/page.adoc", "xref:other.adoc[O]\n")
        self.write("en/modules/ROOT/pages/other.adoc", "= O\n")
        ok, out = self.run_check(dt.check_pages_broken_refs)
        self.assertTrue(ok, out)
        self.assertNotIn("left unchecked", out)

    def test_a_registered_component_is_not_reported(self):
        ext = Path(self._tmpdir) / "sibling"
        (ext / "en" / "modules" / "ROOT" / "pages").mkdir(parents=True)
        (ext / "en/modules/ROOT/pages/intro.adoc").write_text("= I\n")
        dt.EXTERNAL_COMPONENTS = dt._load_external_components(
            [f"docs-backup={ext}"])
        ok, out = self.run_check(dt.check_pages_broken_refs)
        self.assertNotIn("docs-backup", out)
        self.assertIn("blog", out)        # the still-unregistered one remains

    def test_the_set_does_not_leak_between_runs(self):
        """Cleared per scan -- otherwise a multi-rule run would attribute one
        rule's skips to a later rule that never saw them."""
        self.run_check(dt.check_pages_broken_refs)
        self.write("en/modules/ROOT/pages/page.adoc", "= no refs here\n")
        _, out = self.run_check(dt.check_pages_broken_refs)
        self.assertNotIn("docs-backup", out)


class HelpRulesTableTests(unittest.TestCase):
    """--help carries the rule table itself. Anything that makes you run a
    second command to find out what the tool checks is one step too many --
    this is the first thing people look at."""

    def test_help_lists_every_rule_and_its_command(self):
        help_text = dt._build_v2_parser().format_help()
        for fam, rules in dt.FAMILIES.items():
            for rule, targets in rules.items():
                primary = targets.get("pages") or next(iter(targets.values()))
                self.assertIn(dt.RULE_IDS[primary], help_text, primary)
                self.assertIn(dt._check_command(primary), help_text, primary)

    def test_table_is_generated_from_the_dispatch_tables(self):
        """Not a hand-kept copy -- adding a rule must show up with no edit
        to the docstring."""
        table = dt._rules_table()
        rules = [r for fam in dt.FAMILIES.values() for r in fam]
        self.assertEqual(len(table.strip().splitlines()) - 1, len(rules))

    def test_multi_target_rules_collapse_to_one_row(self):
        """CH01/CH02 and RF02..RF06 are one rule each; five rows for refs
        --orphaned would crowd out every other family."""
        table = dt._rules_table()
        self.assertIn("CH01-CH02", table)
        self.assertIn("RF02-RF06", table)
        self.assertNotIn("RF03 ", table)
        self.assertIn("[--target ...]", table)

    def test_every_rule_has_a_short_flags_label(self):
        for fam, rules in dt.FAMILIES.items():
            for rule, targets in rules.items():
                primary = targets.get("pages") or next(iter(targets.values()))
                self.assertIn(primary, dt.RULE_FLAGS, primary)

    def test_table_fits_a_terminal(self):
        widest = max(len(l) for l in dt._rules_table().splitlines())
        self.assertLessEqual(widest, 90, "rule table is too wide for --help")


class RuleExampleTests(unittest.TestCase):
    """`show <rule>` ends with runnable examples, one per flag that changes
    what that particular rule does."""

    def _examples(self, rid):
        return dt._rule_examples(dt._ID_TO_KEY[rid])

    def test_every_rule_has_at_least_the_bare_invocation(self):
        for key in dt.CHECKS:
            ex = dt._rule_examples(key)
            self.assertTrue(ex, key)
            self.assertEqual(ex[0][0], f"./docs_tool.py {dt._check_command(key)}")

    def test_examples_are_real_commands(self):
        """Generated from _check_command, so they can't drift from the parser.
        Re-parse each one to prove it."""
        parser = dt._build_v2_parser()
        for key in dt.CHECKS:
            for cmd, _ in dt._rule_examples(key):
                argv = cmd.split()[1:]
                with contextlib.redirect_stderr(io.StringIO()):
                    parser.parse_args(argv)     # SystemExit here = a bad example

    def test_page_example_omitted_for_whole_site_rules(self):
        self.assertFalse(any("--page" in c for c, _ in self._examples("RF01")))
        self.assertTrue(any("--page" in c for c, _ in self._examples("ST01")))

    def test_verbose_example_only_where_verbose_does_something(self):
        self.assertTrue(any("--verbose" in c for c, _ in self._examples("LN02")))
        self.assertFalse(any("--verbose" in c for c, _ in self._examples("CH04")))

    def test_glossary_example_only_on_terms(self):
        self.assertTrue(any("--glossary" in c for c, _ in self._examples("TM01")))
        for rid in ("CH01", "LN02", "RF01"):
            self.assertFalse(any("--glossary" in c for c, _ in self._examples(rid)), rid)

    def test_sibling_targets_are_summarised_not_enumerated(self):
        """refs --orphaned has four sibling targets. One representative line
        stands in for all of them -- four near-identical rows would bury the
        flags that are actually specific to this rule. Asserted by counting
        examples, not by matching the wording."""
        ex = self._examples("RF06")
        self.assertEqual(len(dt._sibling_targets(dt._ID_TO_KEY["RF06"])), 4)
        others = [c for c, _ in ex
                  if "--orphaned" in c and "--target tags" not in c]
        self.assertEqual(len(others), 1, others)

    def test_single_sibling_target_is_named_outright(self):
        """With only one alternative there is nothing to summarise, so CH01
        says which target it is."""
        notes = [n for _, n in self._examples("CH01")]
        self.assertIn("over examples/", notes)

    def test_show_all_covers_every_rule_with_examples(self):
        """The one command that answers "how do I run each of these" -- the
        per-rule `show` needs 22 invocations to do the same job."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            dt._v2_show("all")
        text = out.getvalue()
        for key, rid in dt.RULE_IDS.items():
            self.assertIn(rid, text, rid)
            self.assertIn(f"./docs_tool.py {dt._check_command(key)}", text, rid)

    def test_show_all_omits_the_long_rationales(self):
        """22 full docstrings would be a document, not a reference."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            dt._v2_show("all")
        text = out.getvalue()
        doc = (dt.CHECKS["pages-no-yo"].__doc__ or "")
        self.assertNotIn("Фёдоров", text)          # from ST01's rationale
        self.assertIn("ST01", text)
        # ...and the single-rule view still has it
        one = io.StringIO()
        with contextlib.redirect_stdout(one):
            dt._v2_show("ST01")
        self.assertIn("Фёдоров", one.getvalue())

    def test_show_all_is_discoverable(self):
        """It only helps if --help and list say it exists."""
        self.assertIn("show all", dt.__doc__)
        listing = io.StringIO()
        with contextlib.redirect_stdout(listing):
            dt._v2_list(None)
        self.assertIn("show all", listing.getvalue())

    def test_show_prints_the_examples(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            dt._v2_show("ST01")
        text = out.getvalue()
        self.assertIn("Examples:", text)
        self.assertIn("./docs_tool.py check style --no-yo", text)


class RuleFlagMetadataTests(unittest.TestCase):
    """The three hand-maintained sets behind the examples are re-derived from
    the check bodies here. Without this they rot silently: a check gaining
    --page support would keep advertising the wrong examples forever."""

    @staticmethod
    def _analyse():
        import ast
        src = (Path(__file__).resolve().parent.parent / "docs_tool.py").read_text()
        fns = {n.name: n for n in ast.walk(ast.parse(src))
               if isinstance(n, ast.FunctionDef)}

        def calls(fn, names, seen=None):
            seen = seen or set()
            if fn in seen or fn not in fns:
                return False
            seen.add(fn)
            for n in ast.walk(fns[fn]):
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
                    if n.func.id in names or calls(n.func.id, names, seen):
                        return True
            return False

        def uses_verbose(fn, seen=None):
            seen = seen or set()
            if fn in seen or fn not in fns:
                return False
            seen.add(fn)
            for n in ast.walk(fns[fn]):
                if isinstance(n, ast.Name) and n.id == "verbose" \
                        and not isinstance(getattr(n, "ctx", None), ast.Store):
                    return True
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
                    for v in list(n.args) + [k.value for k in (n.keywords or [])]:
                        if isinstance(v, ast.Name) and v.id == "verbose":
                            return True
            return False

        ignores_page, has_verbose, uses_ext = set(), set(), set()
        for key, fn in dt.CHECKS.items():
            name = fn.__name__
            if not calls(name, {"_page_allowed"}):
                ignores_page.add(key)
            if uses_verbose(name):
                has_verbose.add(key)
            if calls(name, {"_resolve_module_ref"}):
                uses_ext.add(key)
        return ignores_page, has_verbose, uses_ext

    def test_page_verbose_and_external_root_sets_match_the_code(self):
        ignores_page, has_verbose, uses_ext = self._analyse()
        self.assertEqual(dt._RULES_IGNORING_PAGE, ignores_page)
        self.assertEqual(dt._RULES_WITH_VERBOSE, has_verbose)
        self.assertEqual(dt._RULES_WITH_EXTERNAL_ROOT, uses_ext)

    def test_sets_only_name_real_checks(self):
        for s in (dt._RULES_IGNORING_PAGE, dt._RULES_WITH_VERBOSE,
                  dt._RULES_WITH_EXTERNAL_ROOT):
            self.assertLessEqual(s, set(dt.CHECKS))


class NoDocsTreeTests(unittest.TestCase):
    """A check run from a directory with no en/modules or ru/modules must
    refuse to run. Scanning nothing and reporting "OK:" for every rule is a
    clean pass over content that was never read -- the failure mode that
    silently green-lights a CI job pointed at the wrong directory."""

    def setUp(self):
        self._argv, self._cwd = sys.argv, os.getcwd()
        self._tmp = tempfile.mkdtemp(prefix="docs_tool_notree_")
        os.chdir(self._tmp)

    def tearDown(self):
        sys.argv, _ = self._argv, os.chdir(self._cwd)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _run(self, *args):
        sys.argv = ["docs_tool.py", *args]
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                dt.main()
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        return code, out.getvalue() + err.getvalue() + (str(code) if code else "")

    def test_check_refuses_without_a_docs_tree(self):
        code, out = self._run("check", "chars", "markup", "refs", "style", "terms", "l10n")
        self.assertNotEqual(code, 0)
        self.assertNotIn("OK:", out)

    def test_legacy_check_refuses_too(self):
        code, out = self._run("--all-checks")
        self.assertNotEqual(code, 0)
        self.assertNotIn("OK:", out)

    def test_list_and_show_still_work_anywhere(self):
        """They describe the rule set, not the content -- no tree needed."""
        self.assertEqual(self._run("list")[0], 0)
        self.assertEqual(self._run("show", "ST01")[0], 0)

    def test_usage_errors_still_win_over_the_tree_check(self):
        """A bad family is a usage error wherever it's typed -- the user
        should get that, not a complaint about the working directory."""
        code, out = self._run("check", "nosuchfamily")
        self.assertEqual(code, 2)
        self.assertIn("nosuchfamily", out)


class UnmatchedPageRejectionTests(FixtureTestCase):
    """--page NAME that matches nothing aborts the run (exit 2). Letting it
    through would report "OK:" and exit 0 for a page that was never read,
    so a typo in a CI invocation would pass as a green run."""

    def setUp(self):
        super().setUp()
        self.antora_yml("en", "TEST")
        self.write("en/modules/ROOT/pages/reference/vacuum.adoc", "= T\n")

    def _filter(self, *pages):
        """Returns (exit_code_or_None, stderr) for _apply_page_filter."""
        err = io.StringIO()
        code = None
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            try:
                dt._apply_page_filter(list(pages))
            except SystemExit as e:
                code = e.code
        return code, err.getvalue()

    def test_unknown_filename_aborts(self):
        code, err = self._filter("nosuch.adoc")
        self.assertEqual(code, 2)
        self.assertIn("nosuch.adoc", err)

    def test_unknown_directory_aborts(self):
        code, err = self._filter("nosuchdir")
        self.assertEqual(code, 2)
        self.assertIn("nosuchdir", err)

    def test_real_filename_passes_through(self):
        self.assertEqual(self._filter("vacuum.adoc"), (None, ""))

    def test_real_directory_passes_through(self):
        self.assertEqual(self._filter("reference"), (None, ""))

    def test_one_bad_value_aborts_even_alongside_a_good_one(self):
        code, err = self._filter("vacuum.adoc", "nosuch.adoc")
        self.assertEqual(code, 2)
        self.assertIn("nosuch.adoc", err)
        self.assertNotIn("--page vacuum.adoc", err)

    def test_every_unmatched_value_is_reported_at_once(self):
        """One re-run per typo would be a poor trade for a one-line loop."""
        code, err = self._filter("nosuch.adoc", "alsonot.adoc")
        self.assertEqual(code, 2)
        self.assertIn("nosuch.adoc", err)
        self.assertIn("alsonot.adoc", err)


@unittest.skipUnless(dt.argcomplete, "argcomplete not installed")
class CheckRuleCompletionTests(unittest.TestCase):
    """`check <family> <TAB>` should offer that one family's --<rule> flags
    (which are help=SUPPRESS, so otherwise hidden), and nothing from other
    families."""

    DT = str(Path(__file__).resolve().parent.parent / "docs_tool.py")

    def _complete(self, line):
        tmp = tempfile.mktemp()
        env = {**os.environ, "_ARGCOMPLETE": "1", "_ARGCOMPLETE_SHELL": "zsh",
               "_ARGCOMPLETE_SUPPRESS_SPACE": "1", "ARGCOMPLETE_USE_TEMPFILES": "1",
               "_ARGCOMPLETE_STDOUT_FILENAME": tmp,
               "COMP_LINE": line, "COMP_POINT": str(len(line))}
        subprocess.run([sys.executable, self.DT], env=env, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            raw = Path(tmp).read_text()
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
        return {c.split(":", 1)[0] for c in raw.split("\013") if c}

    def test_family_scopes_rule_flags(self):
        comps = self._complete("docs_tool.py check l10n ")
        self.assertLessEqual({"--lines", "--structure", "--untranslated",
                              "--examples", "--nav"}, comps)
        self.assertNotIn("--no-yo", comps)      # a style rule
        self.assertNotIn("--backticks", comps)  # a markup rule

    def test_no_rule_flags_before_a_family_is_chosen(self):
        comps = self._complete("docs_tool.py check ")
        self.assertNotIn("--structure", comps)
        self.assertIn("l10n", comps)            # families still offered

    def test_no_rule_flags_with_two_families(self):
        comps = self._complete("docs_tool.py check chars markup ")
        self.assertNotIn("--no-cyrillic", comps)
        self.assertNotIn("--backticks", comps)


class RunSyncGitRewordTests(unittest.TestCase):
    """Integration test for the git-backed reworded-paragraph detection in
    run_sync: an EN paragraph reworded (not just extended) since RU was last
    touched must be appended as new text with the old RU translation kept
    alongside as a `// STALE VERSION:` comment, not silently discarded."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="docs_tool_sync_git_")
        self._orig_cwd = os.getcwd()
        os.chdir(self._tmpdir)
        subprocess.run(["git", "init", "-q"], check=True)

    def tearDown(self):
        os.chdir(self._orig_cwd)
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _git(self, *args):
        subprocess.run(
            ["git", "-c", "user.email=test@example.com", "-c", "user.name=Test", *args],
            check=True, capture_output=True,
        )

    def write(self, rel_path: str, content: str) -> Path:
        p = Path(rel_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p

    def test_reworded_paragraph_marked_stale_not_overwritten_silently(self):
        en_rel = "en/modules/ROOT/pages/foo.adoc"
        ru_rel = "ru/modules/ROOT/pages/foo.adoc"
        self.write(en_rel, "== Title\n\nThis is the original explanation of caching behavior.\n")
        self.write(ru_rel, "== Заголовок\n\nЭто оригинальное объяснение поведения кэширования.\n")
        self._git("add", "-A")
        self._git("commit", "-m", "initial")

        self.write(en_rel, "== Title\n\nThis paragraph now explains caching in a completely different way.\n")
        self._git("add", "-A")
        self._git("commit", "-m", "reword EN paragraph")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            dt.run_sync(en_rel, dry_run=False)

        result = Path(ru_rel).read_text(encoding="utf-8")
        self.assertIn("This paragraph now explains caching in a completely different way.", result)
        self.assertIn("// STALE VERSION:", result)
        self.assertIn("Это оригинальное объяснение поведения кэширования.", result)


class FamilySelectionTests(unittest.TestCase):
    """_resolve_family_selection: the routing layer that maps
    `check <family> [--rule] [--target]` to legacy CHECKS keys."""

    def test_every_family_and_rule_maps_to_a_real_check(self):
        for fam, rules in dt.FAMILIES.items():
            for rule, targets in rules.items():
                for key in targets.values():
                    self.assertIn(key, dt.CHECKS, f"{fam} --{rule} -> {key}")

    def test_every_check_is_reachable_through_some_family(self):
        reachable = {k for subs in dt.FAMILIES.values()
                     for t in subs.values() for k in t.values()}
        self.assertEqual(reachable, set(dt.CHECKS))

    def test_rule_names_are_unique_across_families(self):
        seen = [rule for rules in dt.FAMILIES.values() for rule in rules]
        self.assertEqual(len(seen), len(set(seen)))

    def test_whole_family_runs_every_target(self):
        self.assertEqual(
            set(dt._resolve_family_selection("style", None, None)),
            {"pages-no-yo", "pages-file-path-italics", "pages-table-cell-periods"},
        )
        self.assertEqual(
            set(dt._resolve_family_selection("refs", None, None)),
            {"pages-broken-refs", "pages-orphaned", "partials-orphaned",
             "examples-orphaned", "images-orphaned", "tags-orphaned"},
        )

    def test_rule_without_target_defaults_to_pages(self):
        self.assertEqual(
            dt._resolve_family_selection("chars", {"no-cyrillic"}, None),
            ["pages-no-cyrillic"],
        )

    def test_rule_with_target_picks_it(self):
        self.assertEqual(
            dt._resolve_family_selection("chars", {"no-cyrillic"}, "examples"),
            ["examples-no-cyrillic"],
        )

    def test_target_all_expands_every_target(self):
        self.assertEqual(
            set(dt._resolve_family_selection("chars", {"no-cyrillic"}, "all")),
            {"pages-no-cyrillic", "examples-no-cyrillic"},
        )

    def test_target_filters_whole_family(self):
        self.assertEqual(
            dt._resolve_family_selection("refs", None, "images"),
            ["images-orphaned"],
        )

    def test_none_family_spans_every_non_network_family(self):
        self.assertEqual(
            set(dt._resolve_family_selection(None, None, None)),
            set(dt.CHECKS) - dt._NETWORK_ONLY_CHECKS,
        )

    def test_network_family_only_runs_when_named(self):
        self.assertNotIn("links-external", dt._resolve_family_selection(None, None, None))
        self.assertEqual(dt._resolve_family_selection("links", None, None), ["links-external"])
        self.assertEqual(
            dt._resolve_family_selection("links", {"external"}, None), ["links-external"])

    def test_no_matching_target_yields_empty(self):
        self.assertEqual(dt._resolve_family_selection("style", None, "images"), [])

    def test_selection_is_deduplicated_and_ordered(self):
        got = dt._resolve_family_selection(None, None, None)
        self.assertEqual(len(got), len(set(got)))

    def test_family_of(self):
        self.assertEqual(dt._family_of("no-yo"), "style")
        self.assertEqual(dt._family_of("structure"), "l10n")
        self.assertIsNone(dt._family_of("nonsense"))


class CliV2RoutingTests(unittest.TestCase):
    """main() dispatch: `check|sync|list` route to the new surface,
    everything else stays on the legacy --check-* parser."""

    def setUp(self):
        self._argv = sys.argv
        self._pf = dt._PAGE_FILTER
        self._tmp = tempfile.mkdtemp(prefix="docs_tool_cli_")
        self._cwd = os.getcwd()
        os.chdir(self._tmp)
        # Enough of a tree for _require_a_docs_tree to let a check run --
        # these tests are about routing, not findings, so it stays empty.
        (Path(self._tmp) / "en" / "modules" / "ROOT" / "pages").mkdir(parents=True)

    def tearDown(self):
        sys.argv = self._argv
        dt._PAGE_FILTER = self._pf
        os.chdir(self._cwd)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _run(self, *args):
        sys.argv = ["docs_tool.py", *args]
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                dt.main()
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        return code, out.getvalue(), err.getvalue()

    def test_list_prints_the_family_map(self):
        code, out, _ = self._run("list")
        self.assertEqual(code, 0)
        self.assertIn("chars", out)
        self.assertIn("--no-yo", out)
        self.assertIn("CH01", out)
        self.assertIn("no Cyrillic", out)          # a SUMMARIES blurb
        self.assertNotIn("pages-no-cyrillic", out) # no legacy-key line in the tree

    def test_show_rule_prints_its_rationale(self):
        code, out, _ = self._run("show", "no-yo")
        self.assertEqual(code, 0)
        self.assertIn("ST01", out)
        self.assertIn("check style --no-yo", out)
        self.assertIn("ё", out)                    # from the docstring

    def test_show_rule_id_case_insensitive(self):
        code, out, _ = self._run("show", "ln02")
        self.assertEqual(code, 0)
        self.assertIn("check l10n --structure", out)

    def test_show_unknown_name_errors(self):
        code, _, err = self._run("show", "not-a-rule")
        self.assertEqual(code, 2)
        self.assertIn("unknown rule", err)

    def test_list_with_a_rule_name_hints_at_show(self):
        code, _, err = self._run("list", "no-yo")
        self.assertEqual(code, 2)
        self.assertIn("show no-yo", err)

    def test_list_modules_points_at_legacy_flag(self):
        code, _, err = self._run("list", "modules")
        self.assertEqual(code, 2)
        self.assertIn("--list-modules", err)

    def test_list_targets(self):
        code, out, _ = self._run("list", "targets")
        self.assertEqual(code, 0)
        self.assertIn("pages", out)
        self.assertIn("nav.adoc", out)
        for t in dt._SCAN_TARGETS + ("all",):
            self.assertIn(t, out)

    def test_check_requires_a_family(self):
        code, _, err = self._run("check")
        self.assertEqual(code, 2)
        self.assertIn("FAMILY", err)  # argparse: "the following arguments are required: FAMILY"

    def test_check_accepts_multiple_families(self):
        code, out, err = self._run("check", "chars", "markup")
        self.assertEqual(code, 0, err)   # no fixture tree -> nothing found

    def test_rule_with_multiple_families_is_rejected(self):
        code, _, err = self._run("check", "chars", "markup", "--backticks")
        self.assertEqual(code, 2)
        self.assertIn("exactly one family", err)

    def test_wrong_rule_for_family_is_rejected(self):
        code, _, err = self._run("check", "chars", "--no-yo")
        self.assertEqual(code, 2)
        self.assertIn("--no-yo", err)

    def test_check_sets_page_filter_then_runs(self):
        # The page has to exist -- an unmatched --page is a usage error now
        # (see UnmatchedPageRejectionTests). It stays empty: this is about
        # routing and --page parsing, not findings.
        for lang in ("en", "ru"):
            d = Path(self._tmp) / lang / "modules/ROOT/pages"
            d.mkdir(parents=True, exist_ok=True)
            (d / "foo.adoc").write_text("= T\n")
        code, out, _ = self._run("check", "l10n", "--structure", "--page", "foo.adoc")
        self.assertEqual(dt._PAGE_FILTER, {"names": {("foo",)}, "dirs": set()})
        self.assertEqual(code, 0)

    def test_target_flag_is_accepted(self):
        code, _, err = self._run("check", "refs", "--orphaned", "--target", "images")
        self.assertEqual(code, 0, err)

    def test_bad_target_value_is_rejected(self):
        code, _, err = self._run("check", "chars", "--target", "bogus")
        self.assertEqual(code, 2)
        self.assertIn("bogus", err)

    def test_legacy_flags_still_route_to_legacy(self):
        code, out, _ = self._run("--check-pages-no-yo")
        self.assertEqual(code, 0)
        self.assertIn("OK:", out)

    def test_multi_rule_run_headers_name_the_rule_not_the_registry_key(self):
        """Section headers are what the user would type to re-run just that
        rule -- internal CHECKS keys (pages-no-cyrillic) are not a surface."""
        code, out, _ = self._run("check", "chars")
        self.assertIn("=== CH01  check chars --no-cyrillic ===", out)
        self.assertIn("=== CH02  check chars --no-cyrillic --target examples ===", out)
        self.assertNotIn("=== pages-no-cyrillic ===", out)

    def test_legacy_run_headers_still_name_the_legacy_flag(self):
        code, out, _ = self._run("--check-pages-no-yo", "--check-pages-no-cyrillic")
        self.assertIn("=== --check-pages-no-yo ===", out)

    def test_list_rules_prints_commands_sorted_by_id(self):
        code, out, _ = self._run("list", "rules")
        self.assertEqual(code, 0)
        self.assertIn("CH01  check chars --no-cyrillic", out)
        self.assertIn("RF04  check refs --orphaned --target examples", out)
        self.assertIn("TM01  check terms", out)
        self.assertNotIn("pages-no-cyrillic", out)   # no internal registry keys
        lines = [l for l in out.splitlines() if l[:2] in ("CH", "MK", "RF", "ST", "TM", "LN")]
        self.assertEqual(lines, sorted(lines))

    def test_bare_invocation_prints_the_new_surface(self):
        code, out, _ = self._run()
        self.assertEqual(code, 0)
        self.assertIn("check <family>", out)
        self.assertIn("{check,show,list,sync}", out)

    def test_top_level_help_routes_to_new_surface(self):
        code, out, _ = self._run("--help")
        self.assertEqual(code, 0)
        self.assertIn("check <family>", out)

    def test_legacy_flag_still_gets_legacy_help(self):
        code, out, _ = self._run("--all-checks", "--help")
        self.assertEqual(code, 0)
        self.assertIn("Legacy flag interface", out)


class RuleIdRegistryTests(unittest.TestCase):
    def test_every_check_has_a_unique_id(self):
        self.assertEqual(set(dt.RULE_IDS), set(dt.CHECKS))
        self.assertEqual(len(set(dt.RULE_IDS.values())), len(dt.CHECKS))

    def test_ids_are_family_prefixed(self):
        prefix = {"chars": "CH", "markup": "MK", "refs": "RF",
                  "style": "ST", "terms": "TM", "l10n": "LN", "links": "LK"}
        for fam, subs in dt.FAMILIES.items():
            for targets in subs.values():
                for key in targets.values():
                    self.assertTrue(dt.RULE_IDS[key].startswith(prefix[fam]),
                                    f"{key} -> {dt.RULE_IDS[key]} (family {fam})")

    def test_show_accepts_an_id(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            dt._v2_show("ST01")
        self.assertIn("check style --no-yo", out.getvalue())

    def test_resolve_check_name(self):
        self.assertEqual(dt._resolve_check_name("no-yo"), "pages-no-yo")
        self.assertEqual(dt._resolve_check_name("LN02"), "pages-structure-parity")
        self.assertEqual(dt._resolve_check_name("examples-orphaned"), "examples-orphaned")
        self.assertIsNone(dt._resolve_check_name("nonsense"))


# ==========================================================================
# check links --external  (LK01)
# ==========================================================================

@contextlib.contextmanager
def mock_sleep():
    real = dt.time.sleep
    dt.time.sleep = lambda *a, **k: None
    try:
        yield
    finally:
        dt.time.sleep = real


def _probe_table(table, calls=None):
    """A stand-in for dt._probe_url. `table` maps url -> one of: an int
    status, a dict of _Probe kwargs, or ("err", kind, text). "*" is the
    fallback. If `calls` is a list, every probed url is appended to it."""
    def fake(url, timeout=None):
        if calls is not None:
            calls.append(url)
        spec = table.get(url, table.get("*", 200))
        if isinstance(spec, dict):
            return dt._Probe(url, **spec)
        if isinstance(spec, tuple):
            return dt._Probe(url, error=(spec[1], spec[2]))
        return dt._Probe(url, status=spec, final_url=url)
    return fake


class ExternalLinkExtractionTests(unittest.TestCase):
    def test_link_and_image_macros(self):
        self.assertEqual(
            dt._extract_urls_from_line("See link:https://a.example/x[here] and image:https://b.example/i.png[]"),
            ["https://a.example/x", "https://b.example/i.png"])

    def test_bare_url(self):
        self.assertEqual(dt._extract_urls_from_line("visit https://ex.example/page for more"),
                         ["https://ex.example/page"])

    def test_macro_url_not_double_counted(self):
        self.assertEqual(dt._extract_urls_from_line("link:https://x.example/p[label]"),
                         ["https://x.example/p"])

    def test_trailing_punctuation_stripped(self):
        self.assertEqual(dt._trim_url("https://x.example/p."), "https://x.example/p")
        self.assertEqual(dt._trim_url("https://x.example/p),"), "https://x.example/p")

    def test_balanced_parens_kept(self):
        self.assertEqual(dt._trim_url("https://en.wikipedia.org/wiki/Foo_(bar)"),
                         "https://en.wikipedia.org/wiki/Foo_(bar)")

    def test_wikipedia_parenthesised_title_survives_extraction(self):
        for line in (
            "the https://en.wikipedia.org/wiki/Kerberos_(protocol)[Kerberos^] page",
            "bare https://en.wikipedia.org/wiki/Kerberos_(protocol) here",
            "trailing https://en.wikipedia.org/wiki/Kerberos_(protocol).",
        ):
            self.assertEqual(dt._extract_urls_from_line(line),
                             ["https://en.wikipedia.org/wiki/Kerberos_(protocol)"], line)

    def test_prose_parens_around_a_url_are_stripped(self):
        self.assertEqual(
            dt._extract_urls_from_line("see (https://real-site.io/page) for detail"),
            ["https://real-site.io/page"])
        self.assertEqual(
            dt._extract_urls_from_line("(https://en.wikipedia.org/wiki/Foo_(bar)) note"),
            ["https://en.wikipedia.org/wiki/Foo_(bar)"])

    def test_two_urls_in_one_paren_group(self):
        self.assertEqual(
            dt._extract_urls_from_line("(https://a-site.io and https://b-site.io) both"),
            ["https://a-site.io", "https://b-site.io"])

    def test_new_window_caret_stripped(self):
        self.assertEqual(dt._extract_urls_from_line("see https://docker.com^[Docker] now"),
                         ["https://docker.com"])
        self.assertEqual(dt._extract_urls_from_line("link:https://docker.com^[Docker]"),
                         ["https://docker.com"])

    def test_passthrough_plus_wrapper_stripped(self):
        # `link:++URL++[text]` -- the ++ stops attribute substitution, not part of the URL
        self.assertEqual(
            dt._extract_urls_from_line("link:++https://tez.apache.org/releases/0.10.3/rel.txt++[0.10.3^]"),
            ["https://tez.apache.org/releases/0.10.3/rel.txt"])
        self.assertEqual(
            dt._extract_urls_from_line("bare https://trino.io/docs/release-481.html++[x]"),
            ["https://trino.io/docs/release-481.html"])

    def test_internal_plus_in_url_is_kept(self):
        self.assertEqual(
            dt._extract_urls_from_line("https://api.arenadata.io/search?q=a+b next"),
            ["https://api.arenadata.io/search?q=a+b"])

    def test_formatting_wrapped_url_is_not_a_link(self):
        # AsciiDoc renders _http://x_ as italic text, not an autolink
        self.assertEqual(
            dt._extract_urls_from_line("looks like: _http://FQDN:8081_. To log in"), [])
        self.assertEqual(
            dt._extract_urls_from_line("`https://mono.example/x` shown as code"), [])
        # ...but a URL merely near formatting is still a link
        self.assertEqual(dt._extract_urls_from_line("keep _this_ https://real.io/a_b_c here"),
                         ["https://real.io/a_b_c"])
        self.assertEqual(dt._extract_urls_from_line("path https://real.io/a_b_c_ end"),
                         ["https://real.io/a_b_c"])   # lone trailing _ still trimmed

    def test_backslash_escaped_url_is_not_a_link(self):
        self.assertEqual(
            dt._extract_urls_from_line(r"for example \http://FQDN:9999 in the bar"), [])
        self.assertEqual(
            dt._extract_urls_from_line(r"example: _\http://10.20.30.40:11200_."), [])

    def test_attribute_value_url_is_caught(self):
        self.assertEqual(dt._extract_urls_from_line(":page-source: https://repo.example/tree/main"),
                         ["https://repo.example/tree/main"])

    def test_should_probe_filters(self):
        self.assertFalse(dt._should_probe("https://example.com/x"))
        self.assertFalse(dt._should_probe("http://localhost:8080/"))
        self.assertFalse(dt._should_probe("https://{host}/page"))
        self.assertFalse(dt._should_probe("https://host.example/TODO"))
        self.assertFalse(dt._should_probe("https://github.com/org/repo.git"))
        self.assertFalse(dt._should_probe("https://docs.example.org/guide"))   # RFC 2606, suffix
        self.assertTrue(dt._should_probe("https://docs.arenadata.io/guide"))

    def test_private_and_placeholder_hosts_are_skipped(self):
        for u in ("http://10.20.30.40:9000/", "http://192.168.1.1", "http://172.16.0.9",
                  "http://169.254.1.1", "http://10.20.30.444",   # malformed IP
                  "http://FQDN:8081", "http://HOST:PORT", "http://hostname:25000/x",
                  "http://adh-host1.ru-central1.internal:25000/",
                  "https://wiki.corp.lan/page"):
            self.assertFalse(dt._should_probe(u), u)

    def test_public_ip_and_real_host_still_probed(self):
        self.assertTrue(dt._should_probe("https://8.8.8.8/status"))
        self.assertTrue(dt._should_probe("https://docs.arenadata.io/x"))

    def test_allow_domain_suffix_match(self):
        dt.LINK_ALLOW_DOMAINS = {"intranet.acme"}
        try:
            self.assertFalse(dt._should_probe("https://wiki.intranet.acme/x"))
            self.assertTrue(dt._should_probe("https://other.acme/x"))
        finally:
            dt.LINK_ALLOW_DOMAINS = set()

    def test_geo_sensitive_suffix(self):
        self.assertTrue(dt._is_geo_sensitive("publication.pravo.gov.ru"))
        self.assertFalse(dt._is_geo_sensitive("github.com"))


class ExternalLinkClassifyTests(unittest.TestCase):
    def _c(self, **kw):
        return dt._classify_probe(dt._Probe("https://h.example/p", **kw))

    def test_2xx_is_ok(self):
        self.assertEqual(self._c(status=200)[0], "OK")

    def test_404_is_a_finding(self):
        label, _, finding = self._c(status=404)
        self.assertEqual(label, "BROKEN")
        self.assertTrue(finding)

    def test_permanent_redirect_in_chain(self):
        label, detail, finding = dt._classify_probe(
            dt._Probe("https://h.example/p", status=200,
                      redirects=((301, "https://h.example/new"),),
                      final_url="https://h.example/new"))
        self.assertEqual(label, "REDIRECT")
        self.assertFalse(finding)
        self.assertIn("https://h.example/new", detail)

    def test_trailing_slash_redirect_is_not_flagged(self):
        label, _, _ = dt._classify_probe(dt._Probe(
            "https://h.example/docs", status=200,
            redirects=((301, "https://h.example/docs/"),),
            final_url="https://h.example/docs/"))
        self.assertEqual(label, "OK")

    def test_https_upgrade_redirect_is_not_flagged(self):
        label, _, _ = dt._classify_probe(dt._Probe(
            "http://h.example/p", status=301, final_url="https://h.example/p"))
        self.assertEqual(label, "OK")

    def test_cosmetic_redirects_are_not_flagged(self):
        trivial = [
            ("https://nvd.nist.gov/vuln/detail/CVE-2026-0994",
             "https://nvd.nist.gov/vuln/detail/cve-2026-0994"),          # case only
            ("http://hbase.apache.org/book.html#completebulkload",
             "https://hbase.apache.org/book.html"),                     # https + dropped #frag
            ("https://dask.org/", "https://www.dask.org/"),             # +www
        ]
        for src, dst in trivial:
            self.assertEqual(
                dt._classify_probe(dt._Probe(src, status=301, final_url=dst))[0],
                "OK", f"{src} -> {dst}")

    def test_real_redirect_is_still_flagged(self):
        label, detail, _ = dt._classify_probe(dt._Probe(
            "https://phoenix.apache.org/language/index.html", status=301,
            final_url="https://phoenix.apache.org/docs/grammar/"))
        self.assertEqual(label, "REDIRECT")
        self.assertIn("docs/grammar", detail)

    def test_temporary_redirect_is_ok(self):
        self.assertEqual(dt._classify_probe(
            dt._Probe("https://h.example/p", status=200,
                      redirects=((302, "https://h.example/x"),)))[0], "OK")

    def test_403_is_blocked_not_a_finding(self):
        label, _, finding = self._c(status=403)
        self.assertEqual(label, "BLOCKED")
        self.assertFalse(finding)

    def test_5xx_is_server_err(self):
        self.assertEqual(self._c(status=503)[0], "SERVER-ERR")

    def test_timeout_is_unreachable_not_a_finding(self):
        label, _, finding = self._c(error=("timeout", "no response in 10s"))
        self.assertEqual(label, "UNREACHABLE")
        self.assertFalse(finding)

    def test_nxdomain_is_a_finding(self):
        label, _, finding = self._c(error=("dns-nxdomain", "host does not resolve"))
        self.assertEqual(label, "BROKEN")
        self.assertTrue(finding)

    def test_geo_sensitive_failure_is_vpn_not_a_finding(self):
        label, _, finding = dt._classify_probe(
            dt._Probe("https://x.gov.ru/doc", error=("timeout", "no response")))
        self.assertEqual(label, "VPN?")
        self.assertFalse(finding)

    def test_geo_sensitive_nxdomain_still_downgraded(self):
        label, _, finding = dt._classify_probe(
            dt._Probe("https://x.gov.ru/doc", error=("dns-nxdomain", "no")))
        self.assertEqual(label, "VPN?")
        self.assertFalse(finding)


class ProbeOrchestrationTests(unittest.TestCase):
    """_probe_url's retry/back-off logic, with the single HTTP call
    (_http_attempt) faked so nothing touches the network."""

    def setUp(self):
        self._orig = (dt._http_attempt, dt._LINK_PER_HOST_CONCURRENCY)
        dt._LINK_HOST_SEMAPHORES.clear()
        self.calls = []

    def tearDown(self):
        dt._http_attempt, dt._LINK_PER_HOST_CONCURRENCY = self._orig
        dt._LINK_HOST_SEMAPHORES.clear()

    def _sequence(self, *probes):
        it = iter(probes)

        def fake(url, method, timeout, context=None):
            self.calls.append((method, context is not None))
            try:
                return next(it)
            except StopIteration:
                return probes[-1]
        dt._http_attempt = fake

    def test_429_is_retried_once(self):
        self._sequence(
            dt._Probe("https://h/x", status=429),   # HEAD
            dt._Probe("https://h/x", status=200),   # back-off GET retry
        )
        with mock_sleep():
            p = dt._probe_url("https://h/x")
        self.assertEqual(p.status, 200)

    def test_429_that_stays_429_is_reported(self):
        self._sequence(dt._Probe("https://h/x", status=429))
        with mock_sleep():
            p = dt._probe_url("https://h/x")
        self.assertEqual(p.status, 429)

    def test_head_403_falls_back_to_get(self):
        self._sequence(
            dt._Probe("https://h/x", status=403),
            dt._Probe("https://h/x", status=200),
        )
        with mock_sleep():
            p = dt._probe_url("https://h/x")
        self.assertEqual(p.status, 200)
        self.assertEqual(self.calls[0][0], "HEAD")
        self.assertEqual(self.calls[1][0], "GET")

    def test_transient_error_retried_once(self):
        self._sequence(
            dt._Probe("https://h/x", error=("timeout", "t")),
            dt._Probe("https://h/x", error=("timeout", "t")),
            dt._Probe("https://h/x", status=200),
        )
        with mock_sleep():
            p = dt._probe_url("https://h/x")
        self.assertEqual(p.status, 200)

    def test_insecure_mode_uses_unverified_context_upfront(self):
        self._sequence(dt._Probe("https://h/x", status=200))
        dt.LINK_INSECURE = True
        try:
            with mock_sleep():
                dt._probe_url("https://h/x")
        finally:
            dt.LINK_INSECURE = False
        self.assertTrue(self.calls[0][1])   # context passed on the first call

    def test_per_host_concurrency_is_capped(self):
        dt._LINK_PER_HOST_CONCURRENCY = 2
        dt._LINK_HOST_SEMAPHORES.clear()
        active = []
        peak = [0]
        lock = __import__("threading").Lock()

        def fake(url, method, timeout, context=None):
            with lock:
                active.append(1)
                peak[0] = max(peak[0], len(active))
            time.sleep(0.02)
            with lock:
                active.pop()
            return dt._Probe(url, status=200)
        dt._http_attempt = fake
        same = [f"https://one.example/{i}" for i in range(6)]
        list(dt._probe_many(same))
        self.assertLessEqual(peak[0], 2)

    def test_different_hosts_run_in_parallel(self):
        dt._LINK_PER_HOST_CONCURRENCY = 1
        dt._LINK_HOST_SEMAPHORES.clear()
        active = []
        peak = [0]
        lock = __import__("threading").Lock()

        def fake(url, method, timeout, context=None):
            with lock:
                active.append(1)
                peak[0] = max(peak[0], len(active))
            time.sleep(0.02)
            with lock:
                active.pop()
            return dt._Probe(url, status=200)
        dt._http_attempt = fake
        list(dt._probe_many([f"https://h{i}.example/x" for i in range(5)]))
        self.assertGreater(peak[0], 1)   # one lock per host, so no global bottleneck

    def test_on_done_callback_fires_once_per_url(self):
        dt._http_attempt = lambda *a, **k: dt._Probe(a[0], status=200)
        seen = []
        dt._probe_many([f"https://h{i}.example/x" for i in range(4)],
                       on_done=lambda n, total, probe: seen.append((n, total)))
        self.assertEqual(sorted(seen), [(1, 4), (2, 4), (3, 4), (4, 4)])


class ExternalLinkCheckTests(FixtureTestCase):
    def _page(self, body):
        self.antora_yml("en", "TEST")
        self.write("en/modules/ROOT/pages/p.adoc", body)

    def test_all_links_ok(self):
        self._page("a link:https://ok.example/1[x] and https://ok.example/2\n")
        dt._probe_url = _probe_table({"*": 200})
        ok, out = self.run_check(dt.check_links_external)
        self.assertTrue(ok, out)
        self.assertIn("all 2 external link(s) resolve", out)

    def test_404_fails_the_run(self):
        self._page("dead: https://dead.example/gone\n")
        dt._probe_url = _probe_table({"https://dead.example/gone": 404})
        ok, out = self.run_check(dt.check_links_external)
        self.assertFalse(ok)
        self.assertIn("BROKEN", out)
        self.assertIn("https://dead.example/gone", out)

    def test_redirect_reported_but_passes(self):
        self._page("moved: https://old.example/p\n")
        dt._probe_url = _probe_table({"https://old.example/p": dict(
            status=200, redirects=((301, "https://new.example/p"),),
            final_url="https://new.example/p")})
        ok, out = self.run_check(dt.check_links_external)
        self.assertTrue(ok, out)
        self.assertIn("REDIRECT", out)
        self.assertIn("https://new.example/p", out)

    def test_hits_are_grouped_broken_then_redirect_then_unreachable(self):
        self._page("a https://z-red.example/p\n"          # redirect, late alphabetically
                    "b https://a-slow.example/q\n"         # unreachable, early alphabetically
                    "c https://m-dead.example/r\n")        # broken, middle
        dt._probe_url = _probe_table({
            "https://z-red.example/p": dict(status=301, final_url="https://z-red.example/moved"),
            "https://a-slow.example/q": ("err", "timeout", "no response"),
            "https://m-dead.example/r": 404})
        ok, out = self.run_check(dt.check_links_external)
        self.assertFalse(ok)
        order = [ln.split()[0] for ln in out.splitlines()
                 if ln[:1].isalpha() and ln.split()[0] in ("BROKEN", "REDIRECT", "UNREACHABLE")]
        self.assertEqual(order, ["BROKEN", "REDIRECT", "UNREACHABLE"])

    def test_unreachable_is_shown_but_blocked_is_collapsed(self):
        self._page("slow: https://slow.example/p\nblock: https://wall.example/q\n")
        dt._probe_url = _probe_table({
            "https://slow.example/p": ("err", "timeout", "no response in 10s"),
            "https://wall.example/q": 403})
        ok, out = self.run_check(dt.check_links_external)
        self.assertTrue(ok, out)                          # neither fails the run
        self.assertIn("UNREACHABLE https://slow.example/p", out)   # shown line-by-line
        self.assertIn("1 unreachable", out)
        self.assertNotIn("https://wall.example/q", out)   # 403 collapsed
        self.assertIn("1 more link(s) got an unhelpful response (1 blocked)", out)
        self.assertIn("--show-unverified", out)
        self.assertIn("VPN", out)                         # the re-run-behind-a-VPN hint

    def test_show_unverified_lists_the_collapsed_ones(self):
        self._page("block: https://wall.example/q\n")
        dt._probe_url = _probe_table({"https://wall.example/q": 403})
        dt.LINK_SHOW_UNVERIFIED = True
        ok, out = self.run_check(dt.check_links_external)
        self.assertTrue(ok, out)
        self.assertIn("BLOCKED", out)
        self.assertIn("https://wall.example/q", out)

    def test_unreachable_shown_by_default(self):
        self._page("slow: https://slow.example/p\n")
        dt._probe_url = _probe_table({"https://slow.example/p": ("err", "timeout", "gone")})
        ok, out = self.run_check(dt.check_links_external)
        self.assertTrue(ok, out)
        self.assertIn("UNREACHABLE https://slow.example/p", out)
        self.assertIn("p.adoc:1", out)

    def test_clean_run_is_one_line(self):
        self._page("https://ok.example/1\n")
        dt._probe_url = _probe_table({"*": 200})
        ok, out = self.run_check(dt.check_links_external)
        self.assertTrue(ok)
        self.assertEqual(out.strip(), "OK: all 1 external link(s) resolve.")

    def test_progress_line_appears_past_the_threshold(self):
        body = "".join(f"https://h{i}.example/p\n" for i in range(dt._LINK_PROGRESS_THRESHOLD + 2))
        self._page(body)
        dt._probe_url = _probe_table({"*": 200})
        ok, out = self.run_check(dt.check_links_external)
        self.assertTrue(ok, out)
        self.assertIn(f"checking {dt._LINK_PROGRESS_THRESHOLD + 2} external link(s)", out)
        self.assertRegex(out, r"checking links: \d+/")

    def test_no_progress_line_for_a_few_links(self):
        self._page("https://a.example/1\nhttps://b.example/2\nhttps://c.example/3\n")
        dt._probe_url = _probe_table({"*": 200})
        ok, out = self.run_check(dt.check_links_external)
        self.assertNotIn("checking", out)

    def test_urls_in_code_blocks_are_skipped(self):
        self._page("----\nsee https://ignored.example/x\n----\n")
        calls = []
        dt._probe_url = _probe_table({"*": 200}, calls)
        ok, out = self.run_check(dt.check_links_external)
        self.assertTrue(ok, out)
        self.assertIn("no external links found", out)
        self.assertEqual(calls, [])

    def test_same_url_probed_once_reported_per_site(self):
        self._page("https://dup.example/p\n")
        self.write("en/modules/ROOT/pages/q.adoc", "also https://dup.example/p\n")
        calls = []
        dt._probe_url = _probe_table({"https://dup.example/p": 404}, calls)
        ok, out = self.run_check(dt.check_links_external)
        self.assertEqual(calls, ["https://dup.example/p"])
        self.assertIn("p.adoc", out)
        self.assertIn("q.adoc", out)

    def test_page_filter_is_honoured(self):
        self._page("https://a.example/1\n")
        self.write("en/modules/ROOT/pages/other.adoc", "https://b.example/2\n")
        dt._PAGE_FILTER = {"names": {("other",)}, "dirs": set()}
        calls = []
        dt._probe_url = _probe_table({"*": 200}, calls)
        self.run_check(dt.check_links_external)
        self.assertEqual(calls, ["https://b.example/2"])

    def test_offline_lists_without_probing(self):
        self._page("https://x.example/1\nlink:https://y.example/2[k]\n")
        dt.LINK_OFFLINE = True
        calls = []
        dt._probe_url = _probe_table({"*": 200}, calls)
        ok, out = self.run_check(dt.check_links_external)
        self.assertTrue(ok)
        self.assertEqual(calls, [])
        self.assertIn("https://x.example/1", out)
        self.assertIn("https://y.example/2", out)

    def test_total_network_failure_aborts(self):
        body = "".join(f"https://h{i}.example/p\n" for i in range(6))
        self._page(body)
        dt._probe_url = _probe_table({"*": ("err", "refused", "connection refused")})
        with self.assertRaises(SystemExit):
            self.run_check(dt.check_links_external)


class ExternalLinkCacheTests(FixtureTestCase):
    def setUp(self):
        super().setUp()
        dt.LINK_CACHE_PATH = str(self.root / ".link-cache.json")

    def test_no_cache_by_default_every_run_is_fresh(self):
        dt.LINK_CACHE_PATH = None
        self.antora_yml("en", "TEST")
        self.write("en/modules/ROOT/pages/p.adoc", "https://c.example/p\n")
        dt._probe_url = _probe_table({"https://c.example/p": 200})
        self.run_check(dt.check_links_external)
        self.assertFalse((self.root / ".link-cache.json").exists())

        calls = []
        dt._probe_url = _probe_table({"https://c.example/p": 200}, calls)
        self.run_check(dt.check_links_external)
        self.assertEqual(calls, ["https://c.example/p"])   # re-probed

    def test_cache_when_enabled_skips_re_probing(self):
        self.antora_yml("en", "TEST")
        self.write("en/modules/ROOT/pages/p.adoc", "https://c.example/p\n")

        dt._probe_url = _probe_table({"https://c.example/p": 200})
        ok, _ = self.run_check(dt.check_links_external)
        self.assertTrue(ok)
        self.assertTrue(Path(dt.LINK_CACHE_PATH).is_file())

        calls = []
        dt._probe_url = _probe_table({"*": 500}, calls)
        ok, out = self.run_check(dt.check_links_external)
        self.assertTrue(ok, out)
        self.assertEqual(calls, [])

    def test_expired_entry_is_re_probed(self):
        self.antora_yml("en", "TEST")
        self.write("en/modules/ROOT/pages/p.adoc", "https://c.example/p\n")
        stale = {"https://c.example/p": {"label": "OK", "detail": "200",
                                        "finding": False,
                                        "checked_at": 0}}
        Path(dt.LINK_CACHE_PATH).write_text(json.dumps(stale), encoding="utf-8")
        calls = []
        dt._probe_url = _probe_table({"https://c.example/p": 200}, calls)
        self.run_check(dt.check_links_external)
        self.assertEqual(calls, ["https://c.example/p"])


class ExternalLinkWiringTests(unittest.TestCase):
    def test_links_family_is_registered(self):
        self.assertIn("links", dt.FAMILIES)
        self.assertEqual(dt.RULE_IDS["links-external"], "LK01")
        self.assertIn("links-external", dt.BETA_CHECKS)
        self.assertEqual(dt._check_command("links-external"), "check links")

    def test_links_excluded_from_legacy_all_checks(self):
        parser = dt.build_parser()
        args = parser.parse_args(["--all-checks"])
        selected = [k for k in dt.CHECKS if k not in dt._NETWORK_ONLY_CHECKS]
        self.assertNotIn("links-external", selected)

    def test_link_args_parse_and_configure(self):
        p = dt._build_v2_parser()
        args = p.parse_args(["check", "links", "--offline", "--timeout", "5",
                             "--allow-domain", "x.example", "--insecure",
                             "--show-unverified", "--link-cache", "/tmp/lc.json"])
        _orig = (dt.LINK_TIMEOUT, dt.LINK_OFFLINE, dt.LINK_ALLOW_DOMAINS,
                 dt.LINK_CACHE_PATH, dt.LINK_INSECURE, dt.LINK_SHOW_UNVERIFIED)
        try:
            dt._configure_links_from_args(args)
            self.assertEqual(dt.LINK_TIMEOUT, 5.0)
            self.assertTrue(dt.LINK_OFFLINE)
            self.assertEqual(dt.LINK_ALLOW_DOMAINS, {"x.example"})
            self.assertEqual(dt.LINK_CACHE_PATH, "/tmp/lc.json")
            self.assertTrue(dt.LINK_INSECURE)
            self.assertTrue(dt.LINK_SHOW_UNVERIFIED)
        finally:
            (dt.LINK_TIMEOUT, dt.LINK_OFFLINE, dt.LINK_ALLOW_DOMAINS,
             dt.LINK_CACHE_PATH, dt.LINK_INSECURE, dt.LINK_SHOW_UNVERIFIED) = _orig

    def test_no_cache_flag_means_no_caching(self):
        args = dt._build_v2_parser().parse_args(["check", "links"])
        _orig = dt.LINK_CACHE_PATH
        try:
            dt._configure_links_from_args(args)
            self.assertIsNone(dt.LINK_CACHE_PATH)
        finally:
            dt.LINK_CACHE_PATH = _orig

    def test_help_lists_lk01(self):
        help_text = dt._build_v2_parser().format_help()
        self.assertIn("LK01", help_text)
        self.assertIn("check links", help_text)


if __name__ == "__main__":
    unittest.main()
