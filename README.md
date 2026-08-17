# eis-tool

Downloads every public document of a Latvian EIS procurement and turns it into text.
Deterministically, with no model in the main path, and with every file it could not read
named rather than quietly missing.

**Proprietary. No licence is granted — see [LICENSE](LICENSE).** Issues and pull requests
are closed and unreviewed.

Nothing here decides which procurements matter. The tool fetches what it is pointed at,
extracts it, and says what it could not read; the interest, the destination and the
schedule all arrive from outside as configuration.

## What it does

```
IUB notice  ─resolve─→  EIS page  ─fetch─→  documents  ─extract─→  Markdown
                            │                                          │
                            └── procurement.json                       └── what could not be read,
                                title · buyer · deadline · value · CPV      named with size and digest
```

The register's API never returns the platform link, so the first hop is real work.

Discovery walks the register and resolves each notice to its EIS page. A procurement that
carries no register notice is therefore not discovered — it can still be fetched by id, and
that is the supported route for one. Widening discovery beyond the register is deliberately
out of scope; `eis_page.walk_ids` is the unused mechanism for it, kept and not wired in.

## Use

```bash
python3 eis_tool.py probe                        # can this address reach EIS at all
python3 eis_tool.py resolve <iub-notice-uuid>    # notice → EIS URL
python3 eis_tool.py run <eis-url|notice> --out pack
```

In CI: **eis-fetch.yml** for one tender, **eis-batch.yml** for a list. Both redraw runners
until one is served — see "The address lottery" below.

Requirements: Python 3.12, `pip install -r requirements.txt` (pinned exactly), plus
`p7zip-full` and LibreOffice for 7z archives and Word 97 attachments. `curl` is used for
transport because it is what a runner has and its behaviour on a 400 MB download is the
boring, well-understood one.

## What comes out

```
pack/
  procurement.json    the tender's own facts, read off its page — except whether the
                      register carries it, which the page usually does not say and the
                      caller usually knows (`register_check`)
  manifest.json       what was downloaded, with sha256 per file
  journal.jsonl       append-only progress — the resume point
  normalized/         one Markdown document per readable file, plus the audit list
  llm/                optional: scans transcribed by a model, marked as such
  summary.json        counts, bytes, digests
```

What a consumer may rely on is the shape above plus `index.json`; the contract itself lives
with the consumer, not here.

Which notices are worth fetching is not decided in this repository either. `EIS_POLICY`
carries a caller's recall terms as JSON — see `cpv_policy.example.json` for the shape.
Unset, nothing is filtered and every discovered notice is fetched.

## Four properties worth knowing before changing anything

**Partial success is failure.** If any expected record or file is missing the run exits
non-zero and writes nothing to the success path. A tender that looks downloaded and is not
is worse than one that plainly failed, because only the second gets fixed. The journal
makes an interrupted run cheap to resume; it does not lower that bar.

**Nothing is dropped on a guess about importance.** Usefulness is never judged — that
would need a model. Each file is classified only by whether a decoder recovered characters
from it. Everything readable is extracted in full; everything else is listed by name, size
and digest, so a gap is visible instead of silent.

**Same bytes in, same text out.** Dependencies are pinned exactly, walk orders are sorted.
No OCR and no table detection in the main path: table text is already in the text layer, so
detection cost a great deal of time, recovered nothing plain extraction had missed, and
emitted rotated text backwards.

**The address lottery is real, and a failed fetch is never evidence about a tender.** EIS
refuses part of the cloud address space at the TCP layer: runners dispatched in the same
second, downloading nothing, are partly refused and partly served in about a second, while
the register answers all of them. A runner's address is fixed for the life of its job, so
the only way to draw again is a new job — which is why the workflows run sequential draws,
and why a batch of tenders is one job rather than twenty.

## Scans, and the account this does not need

There is no model in extraction. `assist.py` is a quarantined fallback that reads **only**
the files the deterministic extractor already listed as unreadable — scans with no text
layer — and writes **only** into `llm/`, cached by content digest so a re-run costs nothing
and returns identical bytes. Drawings are never sent. Consumers treat that text as grounds
to look, never as a located quote.

**The default reader is Tesseract on the runner: no account, no API key, no billing
relationship, nothing to migrate.** That is deliberate. An automation whose credential
belongs to a private sign-up is a dependency nobody owns, and it fails at the worst
possible moment. The volume argues for it too: the deterministic extractor reads most
tenders whole, which leaves this lane an ordinarily empty queue.

A hosted model is available for the day a scan defeats OCR — `--provider gemini` with
`GEMINI_API_KEY` — and adding another is one entry in `PROVIDERS`. Each result records
which reader produced it: `ocr-fallback` or `llm-fallback`, never merged.

## Tests

```bash
python3 -m unittest discover -s tests -t tests
```

Everything is offline. A third of runners cannot reach EIS, so a test that needed the
portal would fail for reasons unrelated to the code. The parsers are pure functions over
saved HTML for exactly this reason.
