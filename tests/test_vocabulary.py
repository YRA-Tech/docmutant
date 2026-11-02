"""The vocabulary: the common words that a run takes unless told otherwise, named by hash."""

from __future__ import annotations

import json
import re
from importlib.resources import files
from pathlib import Path

from test_anonymize import _bytes, _layered, _run, _source
from test_coverage import _Boxes

from docmutant.cli import main
from docmutant.manifest import manifest_path
from docmutant.vocabulary import default_vocabulary, fingerprint


def test_the_bundled_vocabulary_is_common_lowercase_words():
    words = default_vocabulary()
    assert words == sorted(set(words))
    assert 30_000 < len(words) < 50_000
    assert all(re.fullmatch(r"[a-z]{2,15}", word) for word in words)
    assert {"the", "and", "paper", "window", "quietly"} <= set(words)
    # Letters in the plural, and a slur that the blocklist leaves out.
    assert not {"cs", "ms", "ho"} & set(words)


def test_its_notice_says_where_it_comes_from_and_that_it_was_changed():
    notice = (files("docmutant") / "data" / "vocabulary-copyright.txt").read_text(encoding="ascii")
    assert "modified version of SCOWL" in notice
    assert "Copyright 2000-2018 by Kevin Atkinson" in notice
    assert "Permission to use, copy, modify, distribute and sell" in notice


def test_a_fingerprint_names_a_list_by_its_words_and_not_their_order():
    assert fingerprint(["bee", "ant", "ant"]) == fingerprint(["ant", "bee"])
    assert fingerprint(["ant"])["words"] == 1
    assert fingerprint(["ant"])["sha256"] != fingerprint(["bee"])["sha256"]


def test_a_run_names_its_vocabulary_without_showing_it():
    result = _run(_source(), vocabulary=["bead", "dace"])
    params = next(m["params"] for m in result.mutations if m["operator"] == "fit-words")
    assert params["vocabulary"] == fingerprint(["dace", "bead"])
    assert "bead" not in json.dumps(params)


def _command(tmp_path: Path, monkeypatch, *extra: str) -> dict:
    source = tmp_path / "source.pdf"
    source.write_bytes(_bytes(_layered(_source())))
    out = tmp_path / "out.pdf"
    monkeypatch.setattr("docmutant.substitute.FontSource.__init__", lambda self, *_: None)
    monkeypatch.setattr("docmutant.substitute.FontSource.pick", _Boxes.pick)
    arguments = ["anonymize", str(source), "--out", str(out), "--profile", "privacy"]
    assert main([*arguments, "--tolerance", "5", *extra]) == 0
    manifest = json.loads(manifest_path(out).read_text(encoding="utf-8"))
    return next(m["params"] for m in manifest["mutations"] if m["operator"] == "fit-words")


def test_the_command_takes_the_bundled_words_unless_given_others(tmp_path, monkeypatch):
    assert _command(tmp_path, monkeypatch)["vocabulary"] == fingerprint(default_vocabulary())
    empty = tmp_path / "none.txt"
    empty.write_text("", encoding="utf-8")
    params = _command(tmp_path, monkeypatch, "--vocabulary", str(empty))
    assert params["vocabulary"]["words"] == 0
    assert "real word" not in params["tiers"]
