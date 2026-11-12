"""What the redactor must and must not remove.

Two things can go wrong here and they pull in opposite directions. Under-redact
and a credential reaches a browser. Over-redact and the page destroys the text
someone opened it to read — a diff with its tail eaten is worse than useless,
because it looks complete.

``ReferenceCorpus`` pins the decision on each of a fixed set of texts, so a
change to the patterns shows as a failed expectation.

``Boundaries`` and ``RepeatedProcessing`` go past the reference. The first pins
every boundary the anchoring relies on, with the text either side of the key
kept verbatim; the second pins that the redactor is a fixed point — a marker it
wrote, or a writer wrote before it, survives another pass — and that the fake
keys in the live fixtures, which the live board redacts on read, come out.
"""

import re
import unittest

from nullius.redact import redact

from . import FIXTURES

KEY = "sk-abcd1234efgh5678"
MARK = "[REDACTED]"


class Anchoring(unittest.TestCase):
    """``sk-`` must start at a token boundary.

    Embedded ``sk-`` text in base64 must not truncate patches shown in the UI.
    """

    def test_sk_inside_base64_is_left_alone(self):
        blob = "+aGVsbG8sk-worldXXXXXXXXXXXXXXXXXXXX=="
        self.assertEqual(redact(blob), blob)

    def test_sk_at_a_boundary_is_removed(self):
        out = redact("auth failed for sk-abcd1234efgh5678")
        self.assertNotIn("abcd1234", out)
        self.assertIn("auth failed for", out)


class AssignmentForms(unittest.TestCase):
    """Credentials arrive as shell assignments and as JSON.

    ``trajectory.py`` renders tool arguments with ``json.dumps``, so JSON
    credentials need the same protection as shell assignments.
    """

    FORMS = (
        "API_KEY=abcd1234efgh",
        "api_key: abcd1234efgh",
        '{"api_key": "abcd1234efgh"}',
        '{\\"api_key\\": \\"abcd1234efgh\\"}',
        'Authorization="abcd1234efgh"',
        "access_token = abcd1234efgh",
        "password: abcd1234efgh",
        "token=abcd1234efgh",
    )

    def test_value_is_removed_and_key_survives(self):
        for text in self.FORMS:
            with self.subTest(text):
                out = redact(text)
                self.assertNotIn("abcd1234efgh", out)
                self.assertRegex(out.lower(), r"key|auth|token|password|secret")

    def test_bearer_header(self):
        out = redact("Bearer abcd1234efgh")
        self.assertNotIn("abcd1234efgh", out)
        self.assertIn("Bearer", out)


class LeavesOrdinaryTextAlone(unittest.TestCase):
    """Over-redaction is the failure mode that hides itself."""

    CLEAN = (
        "TimeoutError: session/prompt timed out after 3600.0s "
        "while editing sklearn/mixture/base.py",
        "diff --git a/sklearn/mixture/base.py b/sklearn/mixture/base.py",
        "debug=1",                      # too short to be a secret
        "task=swe-bench/scikit-learn",  # 'task' is not a credential word
    )

    def test_unchanged(self):
        for text in self.CLEAN:
            with self.subTest(text):
                self.assertEqual(redact(text), text)

    def test_empty_and_none(self):
        self.assertEqual(redact(""), "")
        self.assertEqual(redact(None), "")


class PrefixedNamesAndBareTokens(unittest.TestCase):
    """An agent that runs `env` or clones with a token in the URL: the names
    carry a prefix, and GitHub, Hugging Face and AWS tokens need no name."""

    DECISIONS = (
        ("GH_TOKEN=abcd1234efgh5678", "GH_TOKEN=[REDACTED]"),
        ("CURSOR_API_KEY=abcd1234efgh", "CURSOR_API_KEY=[REDACTED]"),
        ('{"MODEL_API_KEY": "abcd1234efgh"}', '{"MODEL_API_KEY": "[REDACTED]"}'),
        ("MODEL_GPU_BOX_API_KEY=abcd1234efgh", "MODEL_GPU_BOX_API_KEY=[REDACTED]"),       # a named endpoint's key
        ('{"MODEL_GPU_API_KEY": "abcd1234efgh"}', '{"MODEL_GPU_API_KEY": "[REDACTED]"}'),
        ("AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG", "AWS_SECRET_ACCESS_KEY=[REDACTED]"),
        ("pushed with ghp_abcdefghijklmnopqrstuvwxyz0123", "pushed with [REDACTED]"),
        ("hf_abcdefghijklmnopqrstuvwxyz and AKIAIOSFODNN7EXAMPLE", "[REDACTED] and [REDACTED]"),
        ("git clone https://x:hunter2pass@github.com/a/b", "git clone https://x:[REDACTED]@github.com/a/b"),
        ("Authorization: token abcd1234efgh", "Authorization: token [REDACTED]"),
        ('{"Authorization": "Basic dXNlcjpwYXNz"}', '{"Authorization": "Basic [REDACTED]"}'),
        # left alone: no credential word ends the name, or no URL userinfo
        ("completion_tokens: 123456789", "completion_tokens: 123456789"),
        ("basic functionality is unchanged", "basic functionality is unchanged"),
        ("http://[::1]:5057/runs", "http://[::1]:5057/runs"),
    )

    def test_each_is_decided(self):
        for text, want in self.DECISIONS:
            with self.subTest(text=text):
                self.assertEqual(redact(text), want)
                self.assertEqual(redact(want), want)


class EndpointUrls(unittest.TestCase):
    """Plain model endpoints and documentation URLs remain readable. Credentials
    embedded in URL paths or queries are redacted at token boundaries."""

    KEPT = (
        "api_base=https://openrouter.ai/api/v1",
        "see https://docs.python.org/3/library/re.html for details",
        "'api_base': 'https://gw.example.com/v1', 'api_key': '******'",
    )
    REDACTED = (
        "https://gw.example.com/v1/sk-abcdefghijklmnopqrstuv/chat",
        "https://gw.example.com/v1?api_key=sk-abcdefghijklmnopqrstuv",
    )

    def test_a_plain_endpoint_survives(self):
        for text in self.KEPT:
            with self.subTest(text=text):
                self.assertEqual(redact(text), text)

    def test_a_key_inside_a_url_is_removed(self):
        for text in self.REDACTED:
            with self.subTest(text=text):
                out = redact(text)
                self.assertNotIn("sk-abcdefghijklmnopqrstuv", out)
                # and the URL around it is still recognisable as one
                self.assertIn("https://gw.example.com/v1", out)


class ReferenceCorpus(unittest.TestCase):
    """Nine texts and what the redactor makes of each: anchoring, the
    assignment and JSON forms, the Bearer header, the endpoint URLs, and the
    ordinary text that must come through untouched."""

    DECISIONS = (
        ("auth failed for sk-abcd1234efgh5678", "auth failed for [REDACTED]"),
        ("+aGVsbG8sk-worldXXXXXXXXXXXXXXXXXXXX==", "+aGVsbG8sk-worldXXXXXXXXXXXXXXXXXXXX=="),
        ("env API_KEY=sk-abcd1234efgh5678 python run.py", "env API_KEY=[REDACTED] python run.py"),
        ('{"api_key": "abcd1234efgh"}', '{"api_key": "[REDACTED]"}'),
        ("api_key: abcd1234efgh", "api_key: [REDACTED]"),
        ('Authorization="abcd1234efgh"', 'Authorization="[REDACTED]"'),
        ("access_token = abcd1234efgh", "access_token = [REDACTED]"),
        ("Bearer abcd1234efgh", "Bearer [REDACTED]"),
        ("TimeoutError: timed out after 3600.0s while editing base.py",
         "TimeoutError: timed out after 3600.0s while editing base.py"),
    )

    def test_each_text_of_the_corpus_is_decided_as_the_reference_decides_it(self):
        for text, want in self.DECISIONS:
            with self.subTest(text=text):
                self.assertEqual(redact(text), want)


class Boundaries(unittest.TestCase):
    """Where a key may start, and what must be left standing either side.

    The ``sk-`` pattern is anchored on the character before it: anything that
    is not a key character is a boundary, and every one of them is a place a
    credential has been seen — the start of a line, a quote, a bracket, a JSON
    string, a URL path or query, a shell assignment, whitespace. The
    complement is the base64 guard: a letter, digit, ``_`` or ``-`` before
    ``sk-`` means the key is inside something else and must be left alone.
    Each case checks the text around the key verbatim, because a redactor that
    eats a bracket or a trailing full stop has changed what the page says.
    """

    # (before, after): the key sits between the two and only the key goes.
    BOUNDED = (
        ("", ""),                              # the whole text
        ("", "\n"),                            # start of text, end of line
        ("\n", ""),                            # after a newline, end of text
        ("line one\n", "\nline three"),        # a line of its own
        ("tab\t", "\tend"),
        ("auth failed for ", " on retry"),
        ('"', '"'),
        ("'", "'"),
        ("(", ")"),
        ("[", "]"),
        ("{", "}"),
        ("<", ">"),
        ('{"key": "', '"}'),                   # a JSON string value
        ('{\\"key\\": \\"', '\\"}'),           # the same, escaped once more
        ("OPENAI_API_KEY=", ""),               # a shell assignment
        ("export KEY=", "; run"),
        ("https://gw.example.com/v1/", "/chat"),
        ("https://gw.example.com/v1?key=", "&x=1"),
        ("key ", "."),                         # trailing punctuation
        ("key ", ", then"),
        ("key ", ": next"),
        ("key ", "; next"),
        ("--api-key ", " --model m"),
    )

    # A key character before ``sk-`` is not a boundary: the key is inside a
    # larger token (base64, an identifier) and the pattern must not fire.
    UNBOUNDED = ("a", "9", "_", "-", "prefix", "aGVsbG8")

    def test_the_key_goes_and_both_neighbours_stay_verbatim(self):
        for before, after in self.BOUNDED:
            text = before + KEY + after
            with self.subTest(text=text):
                self.assertEqual(redact(text), before + MARK + after)

    def test_a_key_character_before_sk_is_not_a_boundary(self):
        for prefix in self.UNBOUNDED:
            text = prefix + KEY
            with self.subTest(text=text):
                self.assertEqual(redact(text), text)

    def test_several_keys_on_one_line_each_go_and_the_words_between_stay(self):
        text = f"first {KEY}, then sk-zyxw9876vuts5432 (and {KEY} again)"
        self.assertEqual(redact(text), f"first {MARK}, then {MARK} (and {MARK} again)")

    def test_an_assigned_value_keeps_its_own_boundaries(self):
        # The assignment pattern stops at the first character outside the
        # value alphabet, so a closing quote, bracket or separator after the
        # value stays, and so does whatever follows it.
        cases = (
            ('{"api_key": "abcd1234efgh", "model": "m"}', '{"api_key": "[REDACTED]", "model": "m"}'),
            ("token=abcd1234efgh;next=1", "token=[REDACTED];next=1"),
            ("(password: abcd1234efgh)", "(password: [REDACTED])"),
            ("Authorization: Bearer abcd1234efgh\nAccept: */*", "Authorization: Bearer [REDACTED]\nAccept: */*"),
        )
        for text, want in cases:
            with self.subTest(text=text):
                self.assertEqual(redact(text), want)


class RepeatedProcessing(unittest.TestCase):
    """The redactor is a fixed point, and its marker is not a key.

    The live board redacts every line on read whether or not the writer
    already did (``runs/live.py``), and the judge state is built from views
    that were redacted once already. So ``redact(redact(x)) == redact(x)``
    must hold, and a ``[REDACTED]`` a writer left must come through as it is
    — nothing may ever turn it into ``[[REDACTED]]`` or eat the key name in
    front of it.
    """

    def _every_case(self):
        yield from (b + KEY + a for b, a in Boundaries.BOUNDED)
        yield from (p + KEY for p in Boundaries.UNBOUNDED)
        yield from AssignmentForms.FORMS
        yield from LeavesOrdinaryTextAlone.CLEAN
        yield from EndpointUrls.KEPT + EndpointUrls.REDACTED
        yield from (text for text, _ in ReferenceCorpus.DECISIONS)

    def test_a_second_pass_changes_nothing(self):
        for text in self._every_case():
            once = redact(text)
            with self.subTest(text=text):
                self.assertEqual(redact(once), once)

    def test_the_live_fixtures_fake_keys_go_and_their_markers_stay(self):
        # Every recorded events.jsonl under fixtures/live, read the way the
        # live board reads it: line by line through redact(). The reference
        # fixture carries one fake key a gateway line printed; Harbor's P3
        # recording carries the marker its writer left in `activity`, and a
        # line the writer already redacted must come through untouched. Lines
        # are taken as text, not parsed: one fixture ends in a torn write on
        # purpose, and the board redacts before it parses anyway.
        found_keys = found_marks = 0
        for path in sorted((FIXTURES / "live").rglob("events.jsonl")):
            for n, line in enumerate(path.read_text().splitlines(), 1):
                keys, marks = re.findall(r"sk-[A-Za-z0-9_-]{8,}", line), line.count(MARK)
                found_keys, found_marks = found_keys + len(keys), found_marks + marks
                out = redact(line)
                with self.subTest(fixture=str(path.relative_to(FIXTURES)), line=n):
                    self.assertNotRegex(out, r"sk-[A-Za-z0-9_-]{8,}")
                    self.assertEqual(out.count(MARK), marks + len(keys))
                    self.assertNotIn("[[", out)
                    if not keys:
                        self.assertEqual(out, line)
        self.assertGreater(found_keys, 0)   # neither half of the check is vacuous
        self.assertGreater(found_marks, 0)



if __name__ == "__main__":
    unittest.main()
