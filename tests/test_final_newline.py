"""Every tracked text file ends with a newline.

A file without one is invisible until it matters: `wc -l` reports one line fewer than
`git diff` shows, an append rewrites the last line instead of adding one, and two copies
of a file that differ by that single byte get argued over as if they differed in content.

`.editorconfig` asks an editor to add the newline. This test is what fails the repository
when one does not: it asks git which paths are tracked, not the filesystem, so an
untracked note in a working copy is not scanned and a dot-directory is.

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

#: Tracked paths that must end without a newline, each mapped to why. Empty by design.
ALLOWED = {}

#: A NUL in the first few KiB is how this project tells a binary file from a text one;
#: git's own heuristic reads no more than 8000 bytes for the same question.
BINARY_SNIFF_BYTES = 8000


def complies(path):
    """Whether the file is empty, binary, or ends with a newline.

    An empty file has no last byte and a binary file is not text: neither is a violation.
    """
    with open(path, "rb") as handle:
        head = handle.read(BINARY_SNIFF_BYTES)
        if not head or b"\0" in head:
            return True
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
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


class OffenderWalkerTests(unittest.TestCase):
    """The allowlist branch runs only when ALLOWED is non-empty, so prove it here."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = self._dir.name
        self.write("terminated.txt", b"text\n")
        self.write("unterminated.txt", b"text")
        self.write("exempt.txt", b"text")
        self.write("empty.txt", b"")
        self.write("binary.bin", b"\x00\x01\x02")

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
