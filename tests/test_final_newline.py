"""Every tracked text file ends with a newline.

A file without one is invisible until it matters: `wc -l` reports one line fewer than
`git diff` shows, an append rewrites the last line instead of adding one, and two copies
of a file that differ by that single byte get argued over as if they differed in content.

`.editorconfig` asks an editor to add the newline. This test is what fails the repository
when one does not: it asks git which paths are tracked, not the filesystem, so an untracked
note in a working copy is not scanned and a dot-directory is. `EditorConfigTest` reads the
file that asks, so the editor half of the rule cannot rot while the sweep stays green.

The allowlist holds a path whose last byte is the point of the file. It is empty today.
An entry is a path mapped to the reason it cannot end with a newline, and two tests below
keep it honest: an entry that is not a tracked path fails, and an entry with no reason
fails. Neither the walker's allowlist branch nor the exemption may be trusted untested,
so OffenderWalkerTests proves both on a fixture instead of waiting for the first entry.
"""

import os
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EDITORCONFIG = os.path.join(ROOT, ".editorconfig")

#: Tracked paths that must end without a newline, each mapped to why. Empty by design.
ALLOWED = {}

#: A NUL in the first or the last few KiB is how this project tells a binary file from a text
#: one; git's own heuristic reads no more than 8000 bytes for the same question. Both ends are
#: read because a file can carry its first NUL past that window - an asset with a long ASCII
#: header - and a NUL at the tail is a binary too.
BINARY_SNIFF_BYTES = 8000


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def complies(path):
    """Whether the file is empty, binary, or ends with a newline.

    An empty file has no last byte and a binary file is not text: neither is a violation.
    """
    with open(path, "rb") as handle:
        head = handle.read(BINARY_SNIFF_BYTES)
        if not head:
            return True
        if b"\0" in head:
            return True
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        if size > BINARY_SNIFF_BYTES:
            handle.seek(size - BINARY_SNIFF_BYTES)
            if b"\0" in handle.read(BINARY_SNIFF_BYTES):
                return True
        handle.seek(-1, os.SEEK_END)
        return handle.read(1) == b"\n"


def offenders(root, paths, allowed=ALLOWED):
    """The repo-relative paths under root that are text and do not end with a newline.

    A pure function of its arguments, so the allowlist branch can be proved on a fixture.
    """
    return sorted(rel for rel in paths if rel not in allowed and not complies(os.path.join(root, rel)))


def tracked_paths(root=ROOT):
    """Every tracked path, as git reports it."""
    try:
        listing = subprocess.run(["git", "-C", root, "ls-files", "-z"], check=True, capture_output=True).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        raise unittest.SkipTest(f"not a git checkout, cannot list tracked files: {error}") from error
    return [raw.decode("utf-8", "surrogateescape") for raw in listing.split(b"\0") if raw]


def editorconfig_sections(text):
    """`{section: {key: value}}`, keys and values lowercased, comments and blank lines dropped.

    A section header is `[...]`; a key is `key = value` inside the section above it. Keys before
    any header (`root = true`) belong to no section and are not returned.
    """
    sections = {}
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1].strip(), {})
            continue
        key, sep, value = line.partition("=")
        if sep and current is not None:
            current[key.strip().lower()] = value.strip().lower()
    return sections


class FinalNewlineTests(unittest.TestCase):
    def test_every_tracked_text_file_ends_with_a_newline(self):
        found = offenders(ROOT, tracked_paths())
        self.assertEqual(found, [], "missing a final newline: " + ", ".join(found))

    def test_every_allowlist_entry_is_a_tracked_path(self):
        stale = sorted(set(ALLOWED) - set(tracked_paths()))
        self.assertEqual(stale, [], "an allowlist entry that is not a tracked path is a stale exemption")

    def test_every_allowlist_entry_carries_a_reason(self):
        unexplained = [path for path, reason in ALLOWED.items() if not reason.strip()]
        self.assertEqual(unexplained, [], "an allowlist entry needs a one-line reason")


class EditorConfigTest(unittest.TestCase):
    """The file that asks editors for the newline, checked where it is written.

    Nothing read `.editorconfig`, so deleting `insert_final_newline = true` left the suite green:
    the sweep still guarded the repository while the file told the next contributor something it
    no longer said.
    """

    def test_the_every_file_section_asks_for_a_final_newline_and_utf8(self):
        settings = editorconfig_sections(read(EDITORCONFIG)).get("*")
        self.assertIsNotNone(settings, "no [*] section: the request no longer covers every file")
        self.assertEqual(settings.get("insert_final_newline"), "true")
        self.assertEqual(settings.get("charset"), "utf-8")

    def test_a_section_is_read_where_it_is_written(self):
        text = "[*]\ninsert_final_newline = true\n\n[*.md]\ninsert_final_newline = false\n"
        sections = editorconfig_sections(text)
        self.assertEqual(sections["*"]["insert_final_newline"], "true")
        self.assertEqual(sections["*.md"]["insert_final_newline"], "false")

    def test_a_key_before_any_section_is_not_attributed_to_one(self):
        self.assertEqual(editorconfig_sections("root = true\n[*]\ncharset = utf-8\n"), {"*": {"charset": "utf-8"}})


class OffenderWalkerTests(unittest.TestCase):
    """The allowlist branch runs only when ALLOWED is non-empty, so prove it here."""

    #: 8710 bytes, past the head window, with its first NUL at 8509 and its last byte an `!`.
    LATE_NUL = b"a" * 8509 + b"\0" + b"a" * 199 + b"!"

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = self._dir.name
        self.write("terminated.txt", b"text\n")
        self.write("unterminated.txt", b"text")
        self.write("exempt.txt", b"text")
        self.write("empty.txt", b"")
        self.write("binary.bin", b"\x00\x01\x02")
        self.write("late-nul.bin", self.LATE_NUL)
        self.write("large.txt", b"a" * 9000)

    def write(self, name, content):
        with open(os.path.join(self.root, name), "wb") as handle:
            handle.write(content)

    def test_only_the_unterminated_file_is_returned(self):
        paths = ["terminated.txt", "unterminated.txt", "exempt.txt", "empty.txt", "binary.bin"]
        self.assertEqual(
            offenders(self.root, paths, {"exempt.txt": "a fixture whose last byte is asserted"}),
            ["unterminated.txt"],
        )

    def test_an_allowlisted_path_stops_being_an_offender(self):
        self.assertEqual(offenders(self.root, ["unterminated.txt"]), ["unterminated.txt"])
        self.assertEqual(offenders(self.root, ["unterminated.txt"], {"unterminated.txt": "reason"}), [])

    def test_an_empty_file_and_a_binary_file_are_not_offenders(self):
        self.assertEqual(offenders(self.root, ["empty.txt", "binary.bin"]), [])

    def test_a_binary_whose_first_nul_is_past_the_head_window_is_not_an_offender(self):
        # The head-only sniff called this text and reported its last byte as a missing newline.
        self.assertEqual(len(self.LATE_NUL), 8710)
        self.assertTrue(complies(os.path.join(self.root, "late-nul.bin")))
        self.assertEqual(offenders(self.root, ["late-nul.bin"]), [])

    def test_a_large_text_file_without_a_newline_is_still_an_offender(self):
        # Reading the tail must not turn the guard off for files bigger than the window.
        self.assertEqual(offenders(self.root, ["large.txt"]), ["large.txt"])


if __name__ == "__main__":
    unittest.main()
