# coding=utf-8
"""
lib/localmode.py - "Go local": running against a LAN PMS with no plex.tv.

The dialog flow is the interesting part: a failed probe has to re-offer the
entry dialogs with the values prefilled, and "Add anyway" has to store the
server despite the failure. Those paths are hard to exercise by hand.
"""

from __future__ import absolute_import

import collections
import json

from kodienv import ENV

from lib import localmode
from lib import util

from .base import KodiTestCase


class FakeResponse(object):
    def __init__(self, status_code=200, content=b""):
        self.status_code = status_code
        self.content = content


class FakeRequests(object):
    """Records every GET and answers from a scripted map of path -> response."""

    def __init__(self, responses=None, raise_on=()):
        self.responses = responses or {}
        self.raise_on = raise_on
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, headers or {}, timeout))
        for needle in self.raise_on:
            if needle in url:
                raise IOError("boom")
        for needle, response in self.responses.items():
            if url.endswith(needle):
                return response
        return FakeResponse(404)


class StoredServersTest(KodiTestCase):
    def test_nothing_stored(self):
        self.assertEqual([], localmode.getStoredServers())

    def test_round_trip(self):
        servers = [{"connection": "10.0.0.5", "port": 32400, "token": None, "name": "Tower"}]
        localmode.saveStoredServers(servers)
        self.assertEqual(servers, localmode.getStoredServers())

    def test_malformed_json_is_ignored(self):
        ENV.settings["local_servers_json"] = "{not json"
        self.assertEqual([], localmode.getStoredServers())

    def test_entries_without_a_connection_are_dropped(self):
        ENV.settings["local_servers_json"] = json.dumps([
            {"connection": "10.0.0.5"},
            {"name": "no connection"},
            "not even a dict",
            {},
        ])
        self.assertEqual([{"connection": "10.0.0.5"}], localmode.getStoredServers())

    def test_an_empty_setting_is_treated_as_an_empty_list(self):
        ENV.settings["local_servers_json"] = ""
        self.assertEqual([], localmode.getStoredServers())


class ProbeTest(KodiTestCase):
    def setUp(self):
        KodiTestCase.setUp(self)
        self._orig_requests = localmode.requests

    def tearDown(self):
        localmode.requests = self._orig_requests
        KodiTestCase.tearDown(self)

    def test_a_reachable_server_reports_its_friendly_name(self):
        localmode.requests = FakeRequests({
            "/identity": FakeResponse(200),
            ":32400/": FakeResponse(200, b'<MediaContainer friendlyName="Tower"/>'),
        })
        self.assertEqual((True, "Tower", False), localmode.probe("10.0.0.5", 32400))

    def test_an_unreachable_server(self):
        localmode.requests = FakeRequests(raise_on=("/identity",))
        self.assertEqual((False, None, False), localmode.probe("10.0.0.5", 32400))

    def test_a_non_200_identity_means_not_a_pms(self):
        localmode.requests = FakeRequests({"/identity": FakeResponse(500)})
        self.assertEqual((False, None, False), localmode.probe("10.0.0.5", 32400))

    def test_a_401_on_the_root_flags_that_auth_is_needed(self):
        localmode.requests = FakeRequests({
            "/identity": FakeResponse(200),
            ":32400/": FakeResponse(401),
        })
        self.assertEqual((True, None, True), localmode.probe("10.0.0.5", 32400))

    def test_a_403_on_the_root_also_flags_auth(self):
        localmode.requests = FakeRequests({
            "/identity": FakeResponse(200),
            ":32400/": FakeResponse(403),
        })
        self.assertEqual((True, None, True), localmode.probe("10.0.0.5", 32400))

    def test_the_token_is_sent_as_a_plex_header(self):
        fake = FakeRequests({
            "/identity": FakeResponse(200),
            ":32400/": FakeResponse(200, b'<MediaContainer friendlyName="Tower"/>'),
        })
        localmode.requests = fake
        localmode.probe("10.0.0.5", 32400, token="secret")
        root_call = [call for call in fake.calls if call[0].endswith(":32400/")][0]
        self.assertEqual("secret", root_call[1].get("X-Plex-Token"))

    def test_no_token_means_no_token_header(self):
        fake = FakeRequests({
            "/identity": FakeResponse(200),
            ":32400/": FakeResponse(200, b'<MediaContainer friendlyName="Tower"/>'),
        })
        localmode.requests = fake
        localmode.probe("10.0.0.5", 32400)
        root_call = [call for call in fake.calls if call[0].endswith(":32400/")][0]
        self.assertNotIn("X-Plex-Token", root_call[1])

    def test_identity_ok_but_unparseable_root_still_counts_as_reachable(self):
        localmode.requests = FakeRequests({
            "/identity": FakeResponse(200),
            ":32400/": FakeResponse(200, b"not xml at all"),
        })
        self.assertEqual((True, None, False), localmode.probe("10.0.0.5", 32400))

    def test_the_probe_timeout_is_applied(self):
        fake = FakeRequests({"/identity": FakeResponse(200)})
        localmode.requests = fake
        localmode.probe("10.0.0.5", 32400)
        self.assertEqual(localmode.PROBE_TIMEOUT, fake.calls[0][2])


class AddServerDialogTest(KodiTestCase):
    def setUp(self):
        KodiTestCase.setUp(self)
        self._orig_requests = localmode.requests

    def tearDown(self):
        localmode.requests = self._orig_requests
        KodiTestCase.tearDown(self)

    def reachable(self, name="Tower"):
        localmode.requests = FakeRequests({
            "/identity": FakeResponse(200),
            ":32400/": FakeResponse(
                200, '<MediaContainer friendlyName="{0}"/>'.format(name).encode("utf-8")),
        })

    def answer(self, *values):
        ENV.dialog_answers = collections.deque(values)

    def test_a_successful_entry_stores_the_server(self):
        self.reachable()
        self.answer("10.0.0.5", "32400", "")
        self.assertTrue(localmode.addServerDialog())
        self.assertEqual([{"connection": "10.0.0.5", "port": 32400, "token": None,
                           "name": "Tower"}], localmode.getStoredServers())

    def test_an_empty_ip_cancels(self):
        self.answer("")
        self.assertFalse(localmode.addServerDialog())
        self.assertEqual([], localmode.getStoredServers())

    def test_an_empty_port_cancels(self):
        self.answer("10.0.0.5", "")
        self.assertFalse(localmode.addServerDialog())
        self.assertEqual([], localmode.getStoredServers())

    def test_an_entered_token_is_stored(self):
        self.reachable()
        self.answer("10.0.0.5", "32400", "sekrit")
        self.assertTrue(localmode.addServerDialog())
        self.assertEqual("sekrit", localmode.getStoredServers()[0]["token"])

    def test_a_failed_probe_offers_a_retry_that_can_succeed(self):
        localmode.requests = FakeRequests(raise_on=("/identity",))
        # first attempt fails -> "Try again" (1) -> second attempt succeeds
        self.answer("10.0.0.5", "32400", "", 1)

        original_probe = localmode.probe
        attempts = {"n": 0}

        def probe(ip, port, token=None):
            attempts["n"] += 1
            if attempts["n"] == 1:
                return False, None, False
            return True, "Tower", False

        localmode.probe = probe
        try:
            ENV.dialog_answers = collections.deque(["10.0.0.5", "32400", "", 1,
                                                    "10.0.0.5", "32400", ""])
            self.assertTrue(localmode.addServerDialog())
        finally:
            localmode.probe = original_probe

        self.assertEqual(2, attempts["n"])
        self.assertEqual("Tower", localmode.getStoredServers()[0]["name"])

    def test_add_anyway_stores_an_unreachable_server(self):
        localmode.requests = FakeRequests(raise_on=("/identity",))
        # ip, port, token, then the custom button (2) == "Add anyway"
        self.answer("10.0.0.5", "32400", "", 2)
        self.assertTrue(localmode.addServerDialog())
        stored = localmode.getStoredServers()
        self.assertEqual(1, len(stored))
        self.assertIsNone(stored[0]["name"], "an unreachable server has no friendly name")

    def test_cancelling_the_failure_dialog_stores_nothing(self):
        localmode.requests = FakeRequests(raise_on=("/identity",))
        self.answer("10.0.0.5", "32400", "", 0)
        self.assertFalse(localmode.addServerDialog())
        self.assertEqual([], localmode.getStoredServers())

    def test_re_adding_the_same_host_replaces_rather_than_duplicates(self):
        self.reachable("First")
        self.answer("10.0.0.5", "32400", "")
        localmode.addServerDialog()

        self.reachable("Second")
        self.answer("10.0.0.5", "32400", "")
        localmode.addServerDialog()

        stored = localmode.getStoredServers()
        self.assertEqual(1, len(stored))
        self.assertEqual("Second", stored[0]["name"])

    def test_a_different_host_is_appended(self):
        self.reachable("First")
        self.answer("10.0.0.5", "32400", "")
        localmode.addServerDialog()

        self.reachable("Second")
        self.answer("10.0.0.6", "32400", "")
        localmode.addServerDialog()

        self.assertEqual(["10.0.0.5", "10.0.0.6"],
                         [s["connection"] for s in localmode.getStoredServers()])

    def test_an_auth_required_server_gets_an_explanation_dialog(self):
        localmode.requests = FakeRequests({
            "/identity": FakeResponse(200),
            ":32400/": FakeResponse(401),
        })
        self.answer("10.0.0.5", "32400", "")
        self.assertTrue(localmode.addServerDialog())
        self.assertIn("ok", [call[0] for call in ENV.dialog_calls])

    def test_the_port_is_stored_as_an_int(self):
        self.reachable()
        self.answer("10.0.0.5", "32400", "")
        localmode.addServerDialog()
        self.assertIsInstance(localmode.getStoredServers()[0]["port"], int)


class OfferAndBootstrapTest(KodiTestCase):
    def setUp(self):
        KodiTestCase.setUp(self)
        self._orig_add = localmode.addServerDialog

    def tearDown(self):
        localmode.addServerDialog = self._orig_add
        KodiTestCase.tearDown(self)

    def test_declining_the_offer_does_not_open_the_entry_dialog(self):
        called = []
        localmode.addServerDialog = lambda: called.append(True) or True
        ENV.dialog_answers = collections.deque([False])
        self.assertFalse(localmode.offerServerIfNoneFound())
        self.assertEqual([], called)

    def test_accepting_the_offer_opens_the_entry_dialog(self):
        localmode.addServerDialog = lambda: True
        ENV.dialog_answers = collections.deque([True])
        self.assertTrue(localmode.offerServerIfNoneFound())

    def test_bootstrap_sets_the_local_mode_setting_on_success(self):
        localmode.addServerDialog = lambda: True
        self.assertTrue(localmode.bootstrap())
        self.assertIs(True, util.getSetting("local_mode", False))

    def test_bootstrap_leaves_local_mode_off_when_entry_is_cancelled(self):
        localmode.addServerDialog = lambda: False
        self.assertFalse(localmode.bootstrap())
        self.assertIs(False, util.getSetting("local_mode", False))
