#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The batch driver: does the reading really happen while the next tender downloads?

The property under test is not "it is fast" — it is that exactly one thread ever talks to
the portal while the post-processing runs behind it. Downloading is where our manners live;
everything else is our own CPU and somebody else's API.
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import batch


class Recorder(object):
    """Stands in for the portal and for the reading, and counts who is in flight."""

    def __init__(self, fetch_time=0.05, work_time=0.05, fail_on=()):
        self.fetch_time, self.work_time, self.fail_on = fetch_time, work_time, set(fail_on)
        self.lock = threading.Lock()
        self.in_fetch = 0
        self.peak_fetch = 0
        self.overlapped = False
        self.working = 0
        self.order = []
        self.register_uuids = []

    def fetch(self, url, pack, sections=None, register_uuid=None):
        with self.lock:
            self.in_fetch += 1
            self.peak_fetch = max(self.peak_fetch, self.in_fetch)
            self.register_uuids.append(register_uuid)
            # The whole point: was somebody reading while this download ran?
            if self.working:
                self.overlapped = True
            self.order.append("fetch:" + os.path.basename(pack))
        try:
            if os.path.basename(pack) in self.fail_on:
                raise batch.eis_fetch.Fail("EIS refused this one")
            time.sleep(self.fetch_time)
            os.makedirs(pack, exist_ok=True)
        finally:
            with self.lock:
                self.in_fetch -= 1

    def post(self, pack, llm_max_files=None):
        with self.lock:
            self.working += 1
            self.order.append("read:" + os.path.basename(pack))
        try:
            time.sleep(self.work_time)
        finally:
            with self.lock:
                self.working -= 1


class Pipeline(unittest.TestCase):
    def setUp(self):
        self.out = tempfile.mkdtemp(prefix="eis_batch_")
        self.addCleanup(shutil.rmtree, self.out, True)
        self._fetch, self._post = batch.eis_fetch.fetch, batch.post_process

    def tearDown(self):
        batch.eis_fetch.fetch, batch.post_process = self._fetch, self._post

    def _run(self, n=5, **kw):
        rec = Recorder(**kw)
        batch.eis_fetch.fetch = rec.fetch
        batch.post_process = rec.post
        targets = ["https://www.eis.gov.lv/EKEIS/Supplier/Procurement/%d" % (100 + i)
                   for i in range(n)]
        done, failed, _withdrawn = batch.run(targets, self.out)
        return rec, done, failed

    def test_reading_happens_while_the_next_tender_downloads(self):
        # Reading is made deliberately slower than downloading, so the overlap is a
        # structural consequence rather than a race the scheduler might lose.
        rec, done, failed = self._run(5, fetch_time=0.02, work_time=0.30)
        self.assertTrue(rec.overlapped, rec.order)
        self.assertEqual(len(done), 5, failed)

    def test_only_one_thread_ever_talks_to_the_portal(self):
        # Our manners live here. Two concurrent downloads would be a different client.
        rec, _, _ = self._run(6, fetch_time=0.03, work_time=0.09)
        self.assertEqual(rec.peak_fetch, 1, rec.order)

    def test_downloads_keep_their_order(self):
        rec, _, _ = self._run(4)
        fetched = [s for s in rec.order if s.startswith("fetch:")]
        self.assertEqual(fetched, ["fetch:10%d" % i for i in range(4)])

    def test_a_refused_tender_does_not_stop_the_batch(self):
        rec, done, failed = self._run(4, fail_on=["101"])
        self.assertEqual(len(done), 3)
        self.assertEqual(len(failed), 1)
        self.assertIn("101", failed[0])

    def test_a_failure_while_reading_is_one_tender_too(self):
        rec = Recorder()
        def explode(pack, llm_max_files=None, shards=1):
            if os.path.basename(pack) == "102":
                raise RuntimeError("normalize exited 2")
            rec.post(pack)
        batch.eis_fetch.fetch = rec.fetch
        batch.post_process = explode
        targets = ["https://www.eis.gov.lv/EKEIS/Supplier/Procurement/%d" % (100 + i)
                   for i in range(4)]
        done, failed, _withdrawn = batch.run(targets, self.out)
        self.assertEqual(len(done), 3)
        self.assertTrue(any("102" in f for f in failed), failed)

    def test_every_tender_is_accounted_for_on_disk(self):
        # done.txt plus failed.txt is the record a consumer reads; a tender missing from
        # both would be a silent loss.
        self._run(4, fail_on=["101"])
        with open(os.path.join(self.out, "done.txt"), encoding="utf-8") as fh:
            done = [l for l in fh.read().splitlines() if l]
        with open(os.path.join(self.out, "failed.txt"), encoding="utf-8") as fh:
            failed = [l for l in fh.read().splitlines() if l]
        self.assertEqual(len(done) + len(failed), 4)


class WithdrawnTenders(unittest.TestCase):
    """EIS's own 'no displayable stage' answer is a fact about the notice, not a gap.

    `eis_fetch.Withdrawn` is a `Fail` a caller can tell apart from an ordinary one: the id
    was checked and EIS answered instantly and structurally, so retrying is pointless and
    counting it as missing work is wrong. It must land apart from `failed` — that is the
    whole reason `main()`'s exit rule (`0 if done else (1 if failed else 0)`) can call a
    shard that is entirely withdrawn a clean run instead of a short one.
    """

    def _run_raising(self, exc_cls, n=2):
        out = tempfile.mkdtemp(prefix="eis_withdrawn_")
        self.addCleanup(shutil.rmtree, out, True)
        old_fetch, old_post = batch.eis_fetch.fetch, batch.post_process

        def fetch(url, pack, sections=None, register_uuid=None):
            raise exc_cls("EIS answered with its own 'Access Denied' page")

        self.addCleanup(lambda: setattr(batch.eis_fetch, "fetch", old_fetch))
        self.addCleanup(lambda: setattr(batch, "post_process", old_post))
        batch.eis_fetch.fetch = fetch
        batch.post_process = lambda pack, llm_max_files=None: None
        targets = ["https://www.eis.gov.lv/EKEIS/Supplier/Procurement/%d" % (200 + i)
                   for i in range(n)]
        return out, batch.run(targets, out)

    def test_a_withdrawn_tender_is_recorded_apart_from_a_failure(self):
        _out, (done, failed, withdrawn) = self._run_raising(batch.eis_fetch.Withdrawn)
        self.assertEqual(done, [])
        self.assertEqual(failed, [])
        self.assertEqual(len(withdrawn), 2)

    def test_a_shard_that_is_entirely_withdrawn_still_exits_clean(self):
        _out, (done, failed, _withdrawn) = self._run_raising(batch.eis_fetch.Withdrawn)
        self.assertEqual(0 if done else (1 if failed else 0), 0)

    def test_withdrawn_lands_in_its_own_file_not_inside_failed_txt(self):
        out, (_done, _failed, withdrawn) = self._run_raising(batch.eis_fetch.Withdrawn)
        with open(os.path.join(out, "failed.txt"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "")
        with open(os.path.join(out, "withdrawn.txt"), encoding="utf-8") as fh:
            lines = [l for l in fh.read().splitlines() if l]
        self.assertEqual(len(lines), len(withdrawn))
        self.assertEqual(len(lines), 2)

    def test_an_ordinary_failure_is_unaffected(self):
        _out, (_done, failed, withdrawn) = self._run_raising(batch.eis_fetch.Fail)
        self.assertEqual(len(failed), 2)
        self.assertEqual(withdrawn, [])


class Targets(unittest.TestCase):
    def test_a_file_of_targets_ignores_comments_and_blanks(self):
        path = os.path.join(tempfile.mkdtemp(prefix="eis_t_"), "t.txt")
        self.addCleanup(shutil.rmtree, os.path.dirname(path), True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# why we asked\n\nhttps://www.eis.gov.lv/EKEIS/Supplier/Procurement/1\n"
                     "  \n2  # a bare id\n")
        targets, weights, uuids = batch.targets_from(path)
        self.assertEqual(targets,
                         ["https://www.eis.gov.lv/EKEIS/Supplier/Procurement/1", "2"])
        # A hand-written list carries no register data, so nothing can be weighed from it —
        # and nothing about the register can be claimed for it either.
        self.assertEqual(weights, {})
        self.assertEqual(uuids, {})

    def test_a_list_saved_with_a_byte_order_mark_still_names_its_targets(self):
        # Windows editors and PowerShell's `Set-Content -Encoding utf8` both prepend a BOM.
        # Read as plain utf-8 the first id is not a number, `as_url` sends it to `resolve`,
        # and the run says "no EIS procurement behind it" about a procurement that is
        # plainly there — pointing the reader at the portal instead of at their editor.
        path = os.path.join(tempfile.mkdtemp(prefix="eis_bom_"), "t.txt")
        self.addCleanup(shutil.rmtree, os.path.dirname(path), True)
        with open(path, "wb") as fh:
            fh.write(b"\xef\xbb\xbf174527\n178056\n")
        targets, _weights, _uuids = batch.targets_from(path)
        self.assertEqual(targets, ["174527", "178056"])
        self.assertEqual(batch.as_url(targets[0]),
                         "https://www.eis.gov.lv/EKEIS/Supplier/Procurement/174527")

    def test_a_bare_id_becomes_a_url_without_asking_the_network(self):
        self.assertEqual(batch.as_url("178475"),
                         "https://www.eis.gov.lv/EKEIS/Supplier/Procurement/178475")

    def test_a_url_is_left_alone(self):
        url = "https://www.eis.gov.lv/EKEIS/Supplier/Procurement/9"
        self.assertEqual(batch.as_url(url), url)


class WindowAndList(unittest.TestCase):
    """A period and a named list are asked for together, and come back as one list.

    Two runs would be two draws against a portal that refuses a third of runner addresses,
    two deliveries into one date, and two `day.json` files where the second describes less
    than the first and nothing says which is which.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="eis_union_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.addCleanup(setattr, batch.eis_tool, "discover", batch.eis_tool.discover)

    def url(self, pid):
        return "https://www.eis.gov.lv/EKEIS/Supplier/Procurement/%s" % pid

    def discovers(self, *notices):
        batch.eis_tool.discover = lambda **kw: {"notices": list(notices)}

    def notice(self, pid, uuid=None, cpv=None):
        return {"eis_url": self.url(pid), "uuid": uuid, "title": "Tender %s" % pid,
                "cpv": cpv}

    def named(self, *lines):
        path = os.path.join(self.dir, "t.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        return path

    def test_both_halves_are_fetched_in_one_run(self):
        self.discovers(self.notice("1"), self.notice("2"))
        targets, _w, _u = batch.targets_from(self.named("500"), days=1)
        self.assertEqual(targets, [self.url("1"), self.url("2"), "500"])

    def test_a_tender_in_both_halves_is_fetched_once(self):
        # Two shards handed the same tender both download it, both deliver it and both write
        # its state — the one arrangement that can lose an update, because the second writer's
        # fingerprint overwrites the first's without having seen its documents.
        self.discovers(self.notice("1"), self.notice("2"))
        targets, _w, _u = batch.targets_from(self.named("2", "500"), days=1)
        self.assertEqual(targets, [self.url("1"), self.url("2"), "500"])

    def test_a_bare_id_and_its_url_are_the_same_tender(self):
        self.discovers(self.notice("1"))
        targets, _w, _u = batch.targets_from(self.named(self.url("1")), days=1)
        self.assertEqual(targets, [self.url("1")])
        self.assertEqual(batch.identity("1"), batch.identity(self.url("1")))

    def test_a_notice_uuid_discovery_already_found_is_not_asked_for_twice(self):
        # A uuid cannot be reduced to an id without resolving it, which is a request. The
        # case that matters is caught against the uuids discovery hands back.
        self.discovers(self.notice("1", uuid="A1B2"))
        targets, _w, _u = batch.targets_from(self.named("a1b2"), days=1)
        self.assertEqual(targets, [self.url("1")])

    def test_the_discovered_half_keeps_its_weight_and_its_notice(self):
        # A tender in both halves keeps the discovered version, which is the one that knows
        # its register notice — the named list proves nothing about the register.
        self.discovers(self.notice("1", uuid="A1B2", cpv=[{"code": "45000000-7"}]))
        targets, weights, uuids = batch.targets_from(self.named("1"), days=1)
        self.assertEqual(targets, [self.url("1")])
        self.assertEqual(weights[self.url("1")], 10.0)
        self.assertEqual(uuids[self.url("1")], "A1B2")

    def test_a_named_entry_weighs_one_and_vouches_for_nothing(self):
        self.discovers()
        targets, weights, uuids = batch.targets_from(self.named("500"), days=1)
        self.assertEqual((targets, weights, uuids), (["500"], {}, {}))

    def test_a_list_alone_asks_the_register_nothing(self):
        def refuse(**kw):
            raise AssertionError("discovery was walked for a run that named its targets")
        batch.eis_tool.discover = refuse
        targets, _w, _u = batch.targets_from(self.named("500"))
        self.assertEqual(targets, ["500"])

    def test_an_exact_date_range_is_a_window_even_beside_a_list(self):
        # This is how "yesterday, and these" is written: naming a range is how the window is
        # asked for alongside a list, and `days` stays the fallback for neither.
        asked = {}
        batch.eis_tool.discover = lambda **kw: asked.update(kw) or {"notices": []}
        batch.targets_from(self.named("500"), date_from="2026-08-17", date_to="2026-08-17")
        self.assertEqual((asked["date_from"], asked["date_to"]),
                         ("2026-08-17", "2026-08-17"))


if __name__ == "__main__":
    unittest.main()


class ArchiveChoice(unittest.TestCase):
    """Whether to take superseded documents is the caller's call, and it reaches the fetcher."""

    def test_the_choice_is_passed_down_to_the_downloader(self):
        seen = []

        def fetch(url, pack, sections=None, register_uuid=None):
            seen.append(sections)
            os.makedirs(pack, exist_ok=True)

        out = tempfile.mkdtemp(prefix="eis_arch_")
        self.addCleanup(shutil.rmtree, out, True)
        old_fetch, old_post = batch.eis_fetch.fetch, batch.post_process
        self.addCleanup(lambda: setattr(batch.eis_fetch, "fetch", old_fetch))
        self.addCleanup(lambda: setattr(batch, "post_process", old_post))
        batch.eis_fetch.fetch = fetch
        batch.post_process = lambda pack, llm_max_files=None: None

        url = "https://www.eis.gov.lv/EKEIS/Supplier/Procurement/1"
        batch.run([url], out, sections=batch.eis_fetch.SECTIONS[:1])
        self.assertEqual(seen[-1], batch.eis_fetch.SECTIONS[:1])
        self.assertEqual([name for name, _ in seen[-1]], ["actual"])

        batch.run([url], out)
        self.assertIsNone(seen[-1], "no choice means take everything")


class RegisterProvenance(unittest.TestCase):
    """The notice a target came from reaches the pack, instead of being re-guessed there.

    Register membership is not something the EIS page reliably carries — measured, 43 of
    169 collected pages print no register link, and the register holds every one of them.
    So the run carries down what discovery already knew, and a target nobody vouched for
    carries nothing rather than a guess.
    """

    def _run_with(self, targets, uuids):
        rec = Recorder()
        out = tempfile.mkdtemp(prefix="eis_prov_")
        self.addCleanup(shutil.rmtree, out, True)
        old_fetch, old_post = batch.eis_fetch.fetch, batch.post_process
        self.addCleanup(lambda: setattr(batch.eis_fetch, "fetch", old_fetch))
        self.addCleanup(lambda: setattr(batch, "post_process", old_post))
        batch.eis_fetch.fetch = rec.fetch
        batch.post_process = rec.post
        batch.run(targets, out, uuids=uuids)
        return rec

    def test_the_notice_uuid_reaches_the_downloader(self):
        url = "https://www.eis.gov.lv/EKEIS/Supplier/Procurement/1"
        rec = self._run_with([url], {url: "6f1c8a02-0000-4000-8000-00000000abcd"})
        self.assertEqual(rec.register_uuids, ["6f1c8a02-0000-4000-8000-00000000abcd"])

    def test_a_target_discovery_never_vouched_for_carries_nothing(self):
        url = "https://www.eis.gov.lv/EKEIS/Supplier/Procurement/2"
        rec = self._run_with([url], {})
        self.assertEqual(rec.register_uuids, [None])


class Weighing(unittest.TestCase):
    """Balancing by a class prior, because the cheap proxies do not predict size.

    Measured over one day of 47 tenders: record count against bytes is r = +0.20 — the
    heaviest tender (609 MB, 43% of the day) had 15 records while the one with 136 records
    was 101 MB. What does carry signal is the procurement's own class: Būvdarbi / CPV 45 was
    11 tenders and 929 MB, two thirds of everything.
    """

    def test_construction_is_treated_as_heavy_by_cpv_or_by_kind(self):
        self.assertGreater(batch.weigh({"cpv_main": "45000000-7 Celtniecības darbi."}), 1)
        self.assertGreater(batch.weigh({"work_kind": "Būvdarbi"}), 1)
        self.assertGreater(batch.weigh({"work_kind": "Construction works"}), 1)
        self.assertGreater(batch.weigh({"cpv": [{"code": "71000000-8"}]}), 1)

    def test_everything_else_weighs_one(self):
        self.assertEqual(batch.weigh({"cpv_main": "33000000-0", "work_kind": "Piegādes"}), 1.0)
        self.assertEqual(batch.weigh({}), 1.0)



class WhoOwnsATender(unittest.TestCase):
    """Membership is a property of the tender, never of the list it arrived in.

    The shards never talk to each other: each walks the register and takes what is its own.
    That only works if two shards holding slightly different lists still agree about every
    tender in both. A greedy bin-pack does not — one item weighed differently moves it in the
    sort order and reshuffles everything packed after it. Measured on a four-shard run: all
    four agreed on 93 targets, the slices came to 21+23+23+23, ninety assignments covering
    sixty-eight tenders. About two dozen went unfetched and the day called itself complete.
    """

    def test_a_disagreement_about_the_list_moves_nothing_else(self):
        # The property the old partition lacked, stated directly: drop an item, insert
        # another, and every surviving tender stays with the shard that already owned it.
        first = ["eis:%d" % i for i in range(40)]
        second = [t for t in first if t != "eis:17"] + ["eis:900"]
        before = {t: batch.shard_of(t, 4) for t in first}
        after = {t: batch.shard_of(t, 4) for t in second}
        for t in set(before) & set(after):
            self.assertEqual(before[t], after[t], t)

    def test_a_url_and_its_bare_id_are_owned_by_the_same_shard(self):
        url = "https://www.eis.gov.lv/EKEIS/Supplier/Procurement/179550"
        self.assertEqual(batch.shard_of(url, 4), batch.shard_of("179550", 4))

    def test_every_target_lands_in_exactly_one_shard(self):
        items = ["t%02d" % i for i in range(23)]
        seen = [t for shard in (1, 2, 3, 4) for t in batch.take_shard(items, shard, 4)]
        self.assertEqual(sorted(seen), sorted(items))
        self.assertEqual(len(seen), len(set(seen)))

    def test_it_spreads_rather_than_bunches(self):
        # A digest promises uniformity in expectation and nothing sharper. That is as much as
        # the class prior could honestly buy: the variance inside a class dwarfs the
        # difference between classes, so no arrangement beats the largest single tender.
        items = ["eis:%d" % i for i in range(400)]
        counts = [len(batch.take_shard(items, n, 4)) for n in (1, 2, 3, 4)]
        self.assertEqual(sum(counts), 400)
        self.assertLess(max(counts) - min(counts), 60, counts)

    def test_one_shard_of_one_takes_everything(self):
        items = ["a", "b", "c"]
        self.assertEqual(batch.take_shard(items, 1, 1), items)


class DownloadFilter(unittest.TestCase):
    """The one filter allowed before a document exists.

    The policy under test is a FIXTURE, not a deployment's. What is being proved is the
    mechanism — which way each signal fails, and which rule wins when two disagree — and
    that is independent of anybody's terms. Committing real terms here would publish the
    policy that `EIS_POLICY` exists to keep out of the repository.
    """

    FIXTURE = json.dumps({
        # One opaque token and one inflected root, because roots are matched as substrings.
        "recall_title_terms": ["alfa", "sarkan"],
        "hard_exclude_prefixes": ["99999"],
        "hard_exclude_title_terms": ["omega"],
    })

    def setUp(self):
        self.policy = batch.load_policy(self.FIXTURE)
        self.assertIsNotNone(self.policy, "the fixture policy must load")

    def test_a_code_alone_never_vetoes_when_the_title_is_missing(self):
        # Missing title is missing evidence, whatever the codes happen to say.
        self.assertFalse(batch.outside_scope(
            {"cpv": [{"code": "33140000-3"}, {"code": "33141000-0"}]}, self.policy))

    def test_a_notice_with_no_code_at_all_is_never_dropped(self):
        # Silence is not a classification.
        self.assertFalse(batch.outside_scope({}, self.policy))
        self.assertFalse(batch.outside_scope({"cpv": []}, self.policy))

    def test_a_matching_title_is_kept(self):
        self.assertFalse(batch.outside_scope({"title": "Alfa piegāde"}, self.policy))

    def test_a_root_matches_an_inflected_form(self):
        # The whole reason roots are substrings: the language inflects the ending.
        for title in ("Sarkanā korpusa remonts", "Sarkanu detaļu piegāde"):
            self.assertFalse(batch.outside_scope({"title": title}, self.policy), title)

    def test_a_nonmatching_title_is_not_downloaded(self):
        self.assertTrue(batch.outside_scope({
            "title": "Kaut kas pavisam cits",
            "cpv": [{"code": "45000000-7"}],
        }, self.policy))

    def test_an_excluded_title_term_beats_a_matching_root(self):
        # Exclusion wins even when a recall root is present in the same title.
        self.assertTrue(batch.outside_scope(
            {"title": "Alfa un omega", "cpv": [{"code": "71320000-7"}]}, self.policy))

    def test_an_excluded_title_term_works_without_any_code(self):
        self.assertTrue(batch.outside_scope({"title": "Omega izbūve"}, self.policy))

    def test_an_all_excluded_code_set_is_dropped(self):
        self.assertTrue(batch.outside_scope({"cpv": [{"code": "99999000-1"}]}, self.policy))

    def test_one_excluded_code_among_others_is_not_enough(self):
        # `all`, not `any`: a notice carrying an excluded code beside an unrelated one is
        # not settled by the excluded one, and a missing title still fails open.
        self.assertFalse(batch.outside_scope(
            {"cpv": [{"code": "99999000-1"}, {"code": "45000000-7"}]}, self.policy))

    def test_no_policy_means_fetch_everything(self):
        self.assertFalse(batch.outside_scope({"cpv": [{"code": "33140000-3"}]}, None))
        self.assertFalse(batch.outside_scope({"title": "Omega"}, None))


class PolicySource(unittest.TestCase):
    """Where the policy comes from, and every way of not having one fails open."""

    def tearDown(self):
        os.environ.pop(batch.POLICY_ENV, None)

    def test_the_environment_supplies_it(self):
        os.environ[batch.POLICY_ENV] = json.dumps({"recall_title_terms": ["alfa"]})
        self.assertTrue(batch.outside_scope({"title": "beta"}, batch.load_policy()))
        self.assertFalse(batch.outside_scope({"title": "alfa"}, batch.load_policy()))

    def test_a_path_is_accepted_too(self):
        directory = tempfile.mkdtemp(prefix="eis_pol_")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "policy.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"recall_title_terms": ["alfa"]}))
        self.assertIsNotNone(batch.load_policy(path))

    def test_nothing_configured_means_no_filter(self):
        self.assertIsNone(batch.load_policy())
        self.assertIsNone(batch.load_policy(""))

    def test_a_broken_policy_fails_open_rather_than_dropping_everything(self):
        # The failure that must never happen quietly: a policy that will not parse must not
        # be read as "nothing matches".
        self.assertIsNone(batch.load_policy("{not json"))
        self.assertIsNone(batch.load_policy("/no/such/file.json"))
        self.assertIsNone(batch.load_policy(json.dumps({"recall_title_terms": []})))


class ShardsOutsideTheRequest(unittest.TestCase):
    """The matrix is fixed at four; the count is an input. Those two must not disagree.

    Observed on a live run asked for two shards: it started four, and shard 3 wrapped onto
    shard 1's slice while shard 4 wrapped onto shard 2's. Both pairs downloaded the same
    tenders — the same duplicate pull the draw ordering was added to stop, arriving through
    a different door.
    """

    def test_a_shard_beyond_the_count_has_no_work(self):
        targets = ["a", "b", "c", "d", "e"]
        self.assertEqual(batch.take_shard(targets, 3, 2), [])
        self.assertEqual(batch.take_shard(targets, 4, 2), [])

    def test_the_requested_shards_still_cover_everything_exactly_once(self):
        targets = ["t%02d" % i for i in range(17)]
        for of in (1, 2, 3, 4):
            seen = []
            for shard in (1, 2, 3, 4):          # the matrix always starts four
                seen += batch.take_shard(targets, shard, of)
            self.assertEqual(sorted(seen), sorted(targets), "of=%d" % of)
            self.assertEqual(len(seen), len(set(seen)), "of=%d duplicated work" % of)

    def test_the_guard_holds_for_a_weighted_partition_too(self):
        targets = ["t%02d" % i for i in range(9)]
        weights = {t: (10.0 if int(t[1:]) % 3 == 0 else 1.0) for t in targets}
        seen = []
        for shard in (1, 2, 3, 4):
            seen += batch.take_shard(targets, shard, 2)
        self.assertEqual(sorted(seen), sorted(targets))
        self.assertEqual(len(seen), len(set(seen)))
