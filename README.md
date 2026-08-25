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

`eis-batch.yml` takes a publication window and a named list **together**: naming
`date_from`/`date_to` asks for the window, `targets` asks for particular tenders, and asking
for both fetches both as one deduplicated list. `days` is the fallback for a caller that
named neither. Two runs would be two draws against a portal that refuses a third of runner
addresses, and two `day.json` files for one date where the second describes less than the
first.

Every window ends **today** unless `date_to` says otherwise, which is worth knowing before
asking for one: `days: 1` is today alone, `date_from` on its own runs from that date up to
today, and one particular day means giving `date_from` and `date_to` the same value. The
separate `date` input is not part of that question at all — it names the folder the delivery
is filed under, and a caller fetching yesterday's publications usually wants all three the
same. Both sides count in UTC.

Requirements: Python 3.12, `pip install -r requirements.txt` (pinned exactly), plus
`p7zip-full` and LibreOffice for 7z archives and Word 97 attachments. `curl` is used for
transport because it is what a runner has and its behaviour on a 400 MB download is the
boring, well-understood one.

## What one tender looks like

This is a pack as it sits on the runner, and as it sits inside the delivery below.

```
pack/
  procurement.json    the tender's own facts, read off its page — except whether the
                      register carries it, which the page usually does not say and the
                      caller usually knows (`register_check`)
  manifest.json       what was downloaded, with sha256 per file
  journal.jsonl       append-only progress — the resume point
  normalized/         one Markdown document per readable file, plus the audit list
  llm/                what the decoder could not read, read anyway — local OCR by default,
                      a hosted model only if one is configured. Marked `ocr-fallback` or
                      `llm-fallback` per entry, never merged into `normalized/`, and not
                      delivered: it stays in the pack and the run's artifact.
  summary.json        counts, bytes, digests
```

The directory is named `llm/` for a lane that has not been model-first since Tesseract
became its default. Renaming it would move paths the manifest already hands out, so the
name stays and this note carries the correction.

## Which country, and where it lands

One run is one country. Latvia is read from EIS and Lithuania from EPPS, and the two
portals have almost nothing in common underneath — EIS is a bespoke ASP.NET application
serving embedded JavaScript arrays, EPPS a European Dynamics Java application serving a
definition list. What they do have in common is everything after the read: the pack, the
digests, the index, the change comparison and the delivery are the same code for both.

    python3 eis_tool.py day 2026-08-20 --country LT --out work

The country is named once and both halves follow from it. It picks the reader — `eis_page`
or `lt_page`, refused rather than guessed for a code with neither — and it picks the folder
published to, which is the country's own under the runtime root:

```
work/LV/  <date>/{day.json,changes.json}   tenders/<pid>/…
work/LT/  <date>/{day.json,changes.json}   tenders/<pid>/…
          plans/{index.json,lines.jsonl}   doors/{index.json,doors.jsonl}
```

Lithuania publishes three populations where Latvia publishes one, and only the first is a
day. `day` takes the window — tenders and market consultations together, told apart by
procedure. `plans` reads the annual procurement plans buyers file months ahead. `doors`
lists the dynamic purchasing and qualification systems, which are applications rather than
bids. The last two are a stock read on demand, not a stream: a system announced once is no
more interesting on the day it appeared than on any day after.

    python3 eis_tool.py plans  --country LT --policy rules.json
    python3 eis_tool.py doors  --country LT --policy rules.json

**`GRAPH_DEST_ROOT` names the folder that CONTAINS the country folders, not one of them.**
The code is appended by the tool. Configuring the full path instead would put the country
in two places that can disagree, and the way that disagreement surfaces is a day of one
country's tenders sitting in the other's folder — uploaded cleanly, indexed validly, with
nothing anywhere saying so. A root already ending in a country code is refused, because
`work/LV/LV` is the same mistake wearing a different hat. There is no default country for
the same reason: a default would send the first Lithuanian run at Latvia in silence.

**Lithuania needs no shards.** A shard exists so four runners can draw four addresses at a
portal that refuses a third of them. EPPS refuses none and serves each tender as one
archive, so its day is written directly rather than reconciled from shard indexes — the
same two files, arrived at without the machinery that Latvia cannot do without.

### Each country has its own lane all the way to the drive

    eis-batch.yml → batch.py       → deliver_graph.py     Latvia, four shards
    lt-day.yml    → eis_tool.py day → deliver_lt.py       Lithuania, one pass

They are separate because the delivery is, and the delivery is separate for reasons that
were each found by asking what the Latvian one would do to a Lithuanian tender:

- `deliver_graph` **rebuilds** `index.json` from `procurement.json` and the normalized
  manifest. Lithuania's index is not derivable from those — it carries the amendment
  number that placed each document and the address a person clicks, both read off the EPPS
  catalogue and both gone by the time `procurement.json` is written. `deliver_lt` ships the
  index the fetch already wrote.
- `deliver_graph.download_url` is a literal `https://www.eis.gov.lv/EKEIS/Document/…`.
  Pointed at Lithuania it does not fail; it stamps a working Latvian URL shape onto a
  Lithuanian procurement, and the card links to a document that is not the one it names.
- A shard is in `deliver_graph`'s path, and Lithuania has no shards.

**The change comparison happens at delivery, against the drive.** `lt_day` compares each
procurement with `state.json` in its own home, which is right on a workstation that keeps
`work/` and worthless on a runner, whose disk is new every night: every procurement would
come back `new`, for ever, and `changes.json` would be a copy of the day. The drive is the
only durable thing in the arrangement, so `deliver_lt` reads the stored state back out of
it — exactly as `deliver_graph` already does — and rewrites the day's verdict before
uploading it. `compared_against: "drive"` in the delivered `changes.json` says so.

**The gate is required, not optional, in the scheduled lane.** `batch.load_policy` fails
open by design: an unreadable policy returns `None` rather than dropping everything. Inside
a library that is right; for an unattended night it would mean fetching every archive the
window holds from a state portal because a secret was misspelt. `lt-day.yml` therefore
checks that the policy parses before the portal is touched, and stops if it does not.

## What gets published

A tender has one home. A day is a list of what moved. This shape is the tool's own and is
what a reader may rely on:

```
tenders/<pid>/                the tender, complete, whenever each part of it arrived
  procurement.json              its facts, as above
  manifest.json                 what was downloaded, sha256 per file
  doc/<digest>.md               the Markdown, one file per document, named for its source
  normalized/manifest_normalized.json
  structure.json                Word numbering, when the tender had any
  index.json                    what is here and what is worth opening — written LAST
  state.json                    the fingerprint the next run compares against
  seen.json                     when it was first and last looked at
  runs/<date>.json              what that date's run found — one file per date
  <pid>.zip                     the whole tender, one request

<date>/
  changes.json                what moved — read this first
  day.json                    the list, and the proof the day is there to be read
  shards/
    eis-batch-shard-N/
      done.txt failed.txt withdrawn.txt resolved.tsv
      index.json              every tender in this shard, each with its change record
                              inline — written LAST
```

**The day folder holds no tender bytes, and no per-tender file.** A day is a statement about
what a run did; the tenders it did it to are addressed from here. `changes.json` and
`day.json` answer everything a consumer asks of a day, and both are small — so a reader
takes the two, then fetches only the tenders they point at.

**Each document in `index.json` carries `download`: the EIS address of the file its text was
extracted from.** A consumer that shows somebody one sentence out of one document is asked,
next, for the document — and the index is the only file it has open. The ids that address a
download were learned during the fetch and live nowhere a reader looks, so rebuilding them
means a page and a POST per record against a portal that refuses a third of the addresses
that ask. Two shapes, because the download had two: a file EIS offers on its own is linked
directly, and a file it only serves inside its record's archive is linked as that archive —
which is also what the person clicking receives. A file the extractor found inside a
published archive is the same case from the other end, so its link is the archive and
`source` goes on naming the member to open. A document the manifest cannot place carries no
`download` at all, because a guessed URL fetches another tender's document silently.

**A tender delivered again uploads only the documents that were not there before.** Its
name is the digest of the file it came from, so an unchanged document has the same address
every day and there is nothing to re-send; `index.json` in the home goes on naming it, and a
reader that wants the tender whole never has to know which day any part of it arrived on.
A superseded document is not deleted — a digest cannot name two different files, so the
previous version stays readable and `runs/` says which day it stopped being current.

**And a tender that did not move writes only `seen.json` and `runs/<date>.json`.** Nothing
else needs rewriting: the manifests, the index, the archive and the fingerprint all still
describe the tender correctly, because nothing about the tender is different. That is why
`state.json` carries no date and no run id — a fingerprint that moved on its own would have
to be rewritten every day to say so, and it is the second largest file in the home. When a
tender was last looked at is a fact about the run, and lives in `seen.json`, which is small.

A shard index names the run that wrote it, and `day.json` counts a shard present only when
that run is its own. A day folder outlives the run that filled it, so fetching one date twice
leaves the earlier indexes in place — without the check, a shard that died mid-delivery is
counted on the strength of last time's index and the day calls itself complete while missing
a quarter of its tenders. A stale one is reported apart from a merely absent one.

**An index that exists was written after everything it names**, and `state.json` is written
after the index. The first is the reader's proof that a home is whole; the second is the next
run's proof of what it may skip, and a fingerprint that landed before the documents it
vouches for would let tomorrow carry over text that is not there. `day.json` goes last of
all, and a reader that lists folders instead of reading it will read the wrong day.

The shape above is this repository's contract. Which tender matters is not: no judgement is
made here and none can be.

## What changed, and how it is known

`changes.json` names every tender the day touched and what moved about it — `new`,
`changed` or `unchanged` — with the values on both sides of each move. A day on which two
deadlines shifted is a few kilobytes; the tenders themselves are not in it.

Everything is compared over sha256 of **original** bytes, never over the Markdown. That is
what keeps the answer honest: `normalize.py` is deterministic for a given version, but two
versions of it may render one unchanged PDF differently, and a diff taken over the text
would report that as the buyer replacing a document. The extractor's own version rides in
`state.json`, so *the text was extracted again* is a different sentence from *the tender
changed*, and a re-extraction refreshes the text without being reported as news.

That version is a digest of the extraction path — `normalize.py` and the pinned
requirements — and deliberately not the run's commit. A tender whose version moved has every
document re-uploaded, so tracking anything that cannot change the text would turn a corrected
comment into a re-delivery of the whole corpus. It does not cover the toolchain image, which
is pinned by tag rather than by digest: a rebuilt image can change how Word 97 attachments
and scans read without moving the value.

**The facts have their own version, for the same reason and a different file.** They are read
by `eis_page`, not by the extractor: one more spelling in a label map, or a field that used to
come back null, changes facts across the whole corpus in a day. Compared blind that is an
amendment reported against every buyer in the register, from this side of the wire. So a page
read by a different parser has none of its facts compared, the run is spent refreshing the
fingerprint so the next one compares clean, and no document travels for it.

What is compared, and what is deliberately not: the published fields (deadline, status,
value, CPV…), the page's document records by id, the extracted documents by digest, and the
files no decoder could read. **Not** `register_check` or `eis_only` — those record how this
tool came to know about the procurement, and a tender found through the register on Monday
and fetched by id on Tuesday has not changed; only the route to it has.

**And not a page served in the other language.** EIS answers in Latvian or in English and
does not let the caller decide — the downloader has always asked for Latvian first and was
answered in English twice in four consecutive fetches of one tender, an hour apart. Every
field whose value is a display string moves together when that happens: `Izsludināts` becomes
`Announced`, `Nē` becomes `No`, `(plānots)` becomes `(scheduled)`. Those fields are therefore
compared only between two pages in the same language; the flip is reported as itself, and
`facts_not_compared` names what was skipped. Fields that survive translation — the reference,
the title, the buyer, the value, the CPV codes, the profile code — are compared across one, so
a real amendment cannot hide behind a translation.

**The fetch still takes everything; only the delivery is a delta.** A record's `publish_date`
would be a cheaper signal, and it is the one a buyer can leave alone while replacing a file
inside that record. Such a record is reported as `silent` in the change record — which is a
finding this arrangement can make precisely because it compares digests it went and got.

Where the previous state comes from is the destination itself. A run remembers nothing: the
runner is new and the previous run's artifact is exactly what a consumer cannot reach, so
each tender's `state.json` is read back off the drive with the credential the delivery
already holds. No cache, no committed file, no second service.

**What that buys, and what it costs.** A delta delivery trusts `state.json` about what is
already on the drive, so a document deleted by hand is not noticed and not replaced: the
index would name a file that is not there. The remedy is one deletion — remove that tender's
`state.json` and the next run delivers it whole, because a tender with no state is a tender
nobody has seen. `runs/<date>.json` is never read by the delivery and there is one per date,
so `state.json` can be rebuilt from the runs if it is ever lost. Fetching a date twice
rewrites that date's record and leaves every other one alone.

`tenders/` grows one folder per procurement ever fetched, and SharePoint's list view stops
enumerating a folder past five thousand children — which is a browsing limit, not a fetching
one: nothing here ever lists that folder, and every address a reader needs is carried
explicitly as `home` in `day.json`, `changes.json` and each shard index. That is also what
makes the layout changeable later without breaking a reader.

Which notices are worth fetching is not decided here either. `EIS_POLICY`
carries a caller's recall terms as JSON — see `cpv_policy.example.json` for the shape.
Unset, nothing is filtered and every discovered notice is fetched.

## Four properties worth knowing before changing anything

**Partial success is failure.** If any expected record or file is missing the run exits
non-zero and writes nothing to the success path. The delivery honours the same rule: a pack
directory exists for every tender the downloader started, including one whose extraction
then failed, and only the tenders `done.txt` names are published. Publishing a partial one
would also record a fingerprint saying that is what the procurement is, and every later run
would agree with it. A tender that looks downloaded and is not
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

**The shards divide the day without talking, so membership is a property of the tender.**
Each walks the register for itself and takes what a digest of the tender's own identity
assigns it. That only works because two shards holding slightly different lists still agree
about every tender in both.

The partition used to be a weighted bin-pack over the list, and the premise that every shard
computed the same plan failed measurably: on one four-shard run all four agreed there were 93
targets, one weighed a single notice differently, and the slices came to 21+23+23+23 — ninety
assignments covering sixty-eight distinct tenders. Twenty-two fetched twice, about two dozen
fetched by nobody, and the day called itself complete because every shard had delivered
something. Greedy packing cascades; a digest cannot.

What that gives up is the weighted balance, which bought less than it looked like it did: the
same run spent 46.8 minutes on one shard against 7.4 on another, balanced, because the
variance inside a class dwarfs the difference between classes. A digest spreads heavy and
light alike in expectation, which is as much as a class prior can honestly promise.

Each shard also publishes the whole day's target list. `collect_day` subtracts what was
delivered and what the shards reported as failed or withdrawn, and `coverage.unaccounted`
names anything left — a tender whose owner never saw it. `complete` is false when that list
is not empty.

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

Nothing this lane does may fail a tender, and the guard around it catches every exception
rather than one class. Naming a class made the promise depend on what a dependency chose to
raise: PyMuPDF raises its own hierarchy, so one oversized page threw past the handler and
marked a tender carrying 1.4 million extracted characters as a failure.

## Tests

```bash
python3 -m unittest discover -s tests -t tests
```

Everything is offline. A third of runners cannot reach EIS, so a test that needed the
portal would fail for reasons unrelated to the code. The parsers are pure functions over
saved HTML for exactly this reason.
