"""Tests for the browser test-kit builder + scorer (testkit.py)."""

from qb_automation.services.testkit import parse_replies, score_kit, select_samples, write_kit


def test_select_samples_deterministic_and_stratified():
    a = select_samples(n=24, seed=7)
    b = select_samples(n=24, seed=7)
    assert [s.file_name for s in a] == [s.file_name for s in b]
    assert len(a) == 24
    reasons = {s.reason for s in a}
    assert any(r.startswith("edge-") for r in reasons), f"no edge cases: {reasons}"
    divisions = {s.division for s in a}
    assert len(divisions) >= 2, "must cover both divisions"


def test_kit_layout_and_self_exclusion(tmp_path):
    manifest = write_kit(tmp_path / "kit", n=8, per_batch=4, seed=7)
    kit = tmp_path / "kit"
    assert (kit / "00_setup_prompt.md").exists()
    assert (kit / "answer_key.json").exists()
    assert (kit / "manifest.json").exists()
    assert len(list(kit.glob("batch_*.md"))) == 2
    assert manifest["n"] == 8
    # Staged neutral copies: one per sample, none carrying the real filename
    staged = sorted((kit / "files").rglob("DOC-*.pdf"))
    assert len(staged) == 8
    assert not any(s["file_name"] in p.name for s in manifest["samples"] for p in staged)
    # Answer leakage: the real filename must appear NOWHERE in the batch md
    # (staged copies are neutral DOC-XX.pdf; examples exclude the test file)
    for batch in kit.glob("batch_*.md"):
        text = batch.read_text(encoding="utf-8")
        for s in manifest["samples"]:
            assert s["file_name"] not in text, f"leak: {s['file_name']}"


def test_scorer_exact_and_partial(tmp_path):
    manifest = write_kit(tmp_path / "kit", n=4, per_batch=4, seed=7)
    key_samples = manifest["samples"]
    truth0 = key_samples[0]["file_name"]
    replies = (
        f"DOC-{key_samples[0]['doc_id']}:\ncompany_name: X\ndate: 2026-01-01\n"
        f"vendor: X\namount: 1\ndoc_type: Payment\nunit_class: NONE\n"
        f"header_memo: x\nledger_memo: x\nsuggested_filename: {truth0.removesuffix('.pdf')}\n"
        f"DOC-{key_samples[1]['doc_id']}:\ncompany_name: Wrong\nsuggested_filename: Total Guess — Nope — 0.00 — XYZ\n"
    )
    rp = tmp_path / "replies.md"
    rp.write_text(replies, encoding="utf-8")
    summary = score_kit(tmp_path / "kit", rp)
    assert summary["n"] == 4
    assert summary["answered"] == 2
    assert summary["exact"] == 1
    assert parse_replies("nothing here") == {}
