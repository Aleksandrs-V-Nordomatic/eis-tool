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
carries no register notice is therefore not discovered by that route — a fifth of what the
platform publishes, measured: unregulated procurements, market consultations, and closed
competitions inside a dynamic purchasing system.

**`idwalk.py` is the second source, and it asks the platform instead.** It walks procurement
ids: a budgeted handful a night, above the newest known id and back into the space below it,
and the ones that turn out to be published join the same run's target list. It is not a
sweep and will not become one — the id space is thousands wide and the portal is a public
service.

Which ids get asked about is `idspace.py`, and the answers are remembered at
`<country>/idspace.json` beside the delivery, because a runner is new every night. Every
shard reads that file and asks about its own slice; `collect_day.py` merges the slices and
is the only writer. A frontier alone would not do: an id is assigned when the record is
created and publishes whenever the buyer is ready, so the space below the newest id keeps
filling in. Measured over 72 published procurements, the gap between a notice's own id and the highest
id published by that date was 77 at the median, 1 760 at the ninth decile, and 9 972 once.

## Use

```bash
python3 eis_tool.py probe                        # can this address reach the register and EIS
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

## One country

This tool reads one country: Latvia, from EIS. `country.py` names it and nothing else, and
`--country` has no default — a run launched without it stops rather than publishing under a
folder the tool guessed, which is a failure that otherwise succeeds quietly.

Everything after the read — the pack, the digests, the index, the change comparison, the
delivery — is deliberately generic, so the shape a reader sees does not depend on which
portal it came from.

### Why country.py is not deleted in a one-country tool

    python3 batch.py --days 1 --out packs          # the Latvian day
    python3 deliver_graph.py --packs packs --shard 1 --date 2026-08-20 --country LV

`--country` has no default and never gains one. It picks the reader — refused rather than
guessed for a code this repository has no source for — and it picks the folder published to,
which is the country's own under the runtime root:

```
work/LV/  idspace.json  <date>/{day.json,changes.json}   tenders/<pid>/…
```

**`GRAPH_DEST_ROOT` names the folder that CONTAINS the country folders, not one of them.**
The code is appended by the tool. Configuring the full path instead would put the country in
two places that can disagree, and the way that disagreement surfaces is a day of one
country's tenders sitting in the other's folder — uploaded cleanly, indexed validly, with
nothing anywhere saying so. A root already ending in a country code is refused, because
`work/LV/LV` is the same mistake wearing a different hat, and so is a root that ends in any
other country's code.

Any code this tool has no source for is stopped by the same line that stops `EE`. That is
the guarantee: this tool cannot be pointed at another country's portal, and it cannot be
pointed at another country's folder.

### Where the day is

`eis_tool.py` is the single tender and the pieces a day is made of — `probe`, `resolve`,
`discover`, `fetch`, `run`, `extract`. The day itself is not one of them, because a Latvian
day is four runners drawing four addresses at a portal that refuses about a third of them:

    eis-batch.yml → eis-shard-chain.yml → eis-shard.yml → batch.py → deliver_graph.py
                  → collect_day.py

`batch.py` fetches one shard's slice, `deliver_graph.py` delivers it, and `collect_day.py`
reconciles the four shard indexes into the day's two files afterwards.

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

Which notices are worth fetching is not decided here either. `policy.py` holds the rule and
nothing else: recall terms and CPV prefixes arrive as JSON — see `cpv_policy.example.json`
for the shape, which is a deliberately unrelated illustration — so this repository names no
industry, no trade and no target. Unset, nothing is filtered and every discovered notice is
fetched.

It arrives by one of two roads. `EIS_POLICY` carries the JSON, or a path to it, in the
environment. Or `POLICY_SOURCE` and `POLICY_TOKEN` name a URL and a credential, and the
workflow fetches the file to the runner before the day starts — which is the road for a
caller who would rather author the classification where the scope it serves is authored, and
review a change to it, than keep it in a secret nobody can read back. **Configured and
unreachable stops the run**: an absent policy meaning "fetch everything" is the right
direction when no filter was asked for and the wrong one when a token has expired. The gate is in its own file because it is
the one piece of judgement that is not about a country: every country tool runs this exact
rule, and it is written where it belongs rather than reached for out of a shard driver.

A policy may be **exclusions only**. Recall terms are a whitelist, and a whitelist suits a
caller who buys a nameable thing: the word is in the title or the notice is not theirs. It is
exactly wrong for a caller whose scope hides inside somebody else's purchase, where the title
names the building and the scope appears three documents down — there the honest gate is the
buyer's own classification, and a whitelist drops most of a day. So `recall_title_terms` is
one optional half of a policy rather than the price of having one; a policy carrying no rule
at all still means no filter.

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

**One retry policy, and it lives in `net.py`.** Every request this tool makes goes through
it: the honest exception set — `OSError` and `http.client.HTTPException`, because a reset
arrives as `RemoteDisconnected` and that is neither a `URLError` nor a `TimeoutError` — a
budget that outlasts a portal hiccup, `Retry-After`, and the parse inside the retry because a
portal under load answers 200 with an error page. Call sites do not get a vote on it. A shard
once died 0.8 seconds into a run holding a reset its own four-try loop had watched go past,
because the loop named the wrong exception, and the same defect was sitting in four other
places including the delivery that runs after a whole day of downloads. A rule that has to be
re-derived at each call site is a rule that gets written wrong again.

**Resolution is proven the way coverage is.** Coverage is checked against the register's own
`x-total-count` before each notice is resolved to its EIS page, so a connection reset during
resolution used to shrink the day with nothing downstream able to tell: `resolve` answered
`None` both for "this purchase is conducted somewhere else" and for "we never reached the
register", and discovery skips a notice with no link by design. The two are kept apart now.
If nothing resolved at all the runner stands down for a fresh draw; if some did, the ones
that did not are carried down by uuid and named in `failed.txt` if the fetch stage cannot
reach them either.

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
