from time import monotonic
from unittest import TestCase
from unittest.mock import Mock, patch

from instagram_pilot import endpoint_pages
from instagram_pilot.live_roster import LiveBrowserError


def page(users, *, more=False, cursor=None):
    return {"status": "ok", "users": users, "has_more": more, "next_max_id": cursor}


class EndpointPagesTests(TestCase):
    def collect(self, pages, *, expected=None, permit=None, initial=None):
        fetch = Mock(side_effect=pages)
        result = endpoint_pages.read_endpoint_pages(
            fetch,
            expected_count=expected,
            deadline=monotonic() + 60,
            before_load=permit or Mock(return_value=True),
            initial_page=initial,
        )
        return result, [call.args[0] for call in fetch.call_args_list]

    def test_initial_page_is_counted_without_a_second_request(self):
        gate = Mock(return_value=True)
        first = page([{"username": "already.seen"}], more=True, cursor="tail")
        result, cursors = self.collect(
            [page([{"username": "last"}])], expected=2, permit=gate, initial=first
        )
        self.assertTrue(result["complete"])
        self.assertEqual(result["page_count"], 2)
        self.assertEqual(cursors, ["tail"])
        gate.assert_called_once()
        gate.reset_mock()
        result, cursors = self.collect([], expected=0, permit=gate, initial=page([]))
        self.assertTrue(result["complete"])
        self.assertEqual(result["page_count"], 1)
        self.assertEqual(cursors, [])
        gate.assert_not_called()

    def test_provider_refusal_aborts_initial_or_subsequent_page(self):
        for data, reason in (
            ({"message": "login_required"}, "challenge"),
            ({"message": "checkpoint_required"}, "challenge"),
            ({"message": "challenge_required"}, "challenge"),
            ({"message": "feedback_required"}, "cooldown"),
            ({"message": "cooldown"}, "cooldown"),
            ({"feedback_required": True}, "cooldown"),
            ({"status": "fail"}, "unavailable"),
        ):
            first = page([{"username": "alpha"}], more=True, cursor="n")
            for seed in (True, False):
                with (
                    self.subTest(data=data, initial=seed),
                    self.assertRaises(LiveBrowserError) as error,
                ):
                    self.collect(
                        [] if seed else [data], initial=data if seed else first
                    )
                self.assertEqual(error.exception.code, reason)

    def test_walks_cursors_and_merges_duplicate_accounts(self):
        big_id = 2**60 + 1
        gate = Mock(return_value=True)
        result, cursors = self.collect(
            [
                page([{"username": "First"}], more=True, cursor="opaque+cursor="),
                page(
                    [{"username": "FIRST", "pk": big_id, "pk_id": str(big_id)}],
                    more=True,
                    cursor=4,
                ),
                page([{"username": "third", "id": "9"}]),
            ],
            expected=2,
            permit=gate,
        )
        self.assertEqual(cursors, [None, "opaque+cursor=", "4"])
        self.assertEqual(gate.call_count, 3)
        self.assertEqual(
            result,
            {
                "members": [
                    {"handle": "first", "id": str(big_id)},
                    {"handle": "third", "id": "9"},
                ],
                "complete": True,
                "reason": "",
                "expected_count": 2,
                "page_count": 3,
            },
        )

    def test_empty_or_round_count_requires_an_explicit_end(self):
        for expected in (None, 0):
            with self.subTest(expected=expected):
                result, _ = self.collect([page([])], expected=expected)
                self.assertTrue(result["complete"])
        result, _ = self.collect([page([])], expected=1)
        self.assertEqual(result["reason"], "count_mismatch")
        for bad in (
            {"status": "ok", "users": []},
            page([], more=False, cursor="further"),
            page([], more=True),
            page([], more=1),
            page([], more=True, cursor=["bad"]),
            page([], more=True, cursor="\nextra"),
            page([], more=True, cursor="x" * 2049),
        ):
            with self.subTest(bad=bad):
                result, _ = self.collect([bad], expected=0)
                self.assertFalse(result["complete"])
                self.assertEqual(result["reason"], "ambiguous_end")

    def test_cursor_cycle_stops_with_partial_list(self):
        first = page([{"username": "a"}], more=True, cursor="already-used")
        result, cursors = self.collect([first, first], expected=1)
        self.assertEqual(cursors, [None, "already-used"])
        self.assertEqual(result["members"], [{"handle": "a", "id": ""}])
        self.assertEqual(result["reason"], "stalled")

    def test_transient_failure_keeps_previous_pages(self):
        first = page([{"username": "reported", "pk": 123}], more=True, cursor="next")
        result, cursors = self.collect(
            [first, endpoint_pages.EndpointPageUnavailable()], expected=2
        )
        self.assertEqual(cursors, [None, "next"])
        self.assertEqual(result["members"], [{"handle": "reported", "id": "123"}])
        self.assertFalse(result["complete"])
        self.assertEqual(result["reason"], "unavailable")
        self.assertEqual(result["page_count"], 1)

    def test_ambiguous_identity_or_malformed_data_is_partial(self):
        first = page([{"username": "known", "pk": "123"}], more=True, cursor="n")
        for last in (
            page([{"username": "known", "pk": "124"}]),
            page([{"username": "other", "pk": "123"}]),
            page([{"username": "other", "id": "10", "pk_id": "11"}]),
            page([{"username": "other", "id": False}]),
            page([{"pk": "999"}]),
            page([{"username": "../invalid"}]),
            {"status": "ok", "users": None},
            [],
        ):
            with self.subTest(last=last):
                result, _ = self.collect([first, last])
                self.assertEqual(result["reason"], "unavailable")
                self.assertEqual(result["members"], [{"handle": "known", "id": "123"}])

    def test_limits_and_challenges_stop_requests(self):
        first = page([{"username": "a"}], more=True, cursor="next")
        permit = Mock(side_effect=[True, False])
        stopped, cursors = self.collect([first], permit=permit)
        self.assertEqual((stopped["reason"], cursors), ("time_limit", [None]))
        with patch.object(endpoint_pages, "monotonic", side_effect=[0, 0, 11]):
            late = endpoint_pages.read_endpoint_pages(
                lambda cursor: page([]),
                expected_count=0,
                deadline=10,
                before_load=lambda deadline: True,
            )
        self.assertEqual((late["reason"], late["page_count"]), ("time_limit", 1))

        with patch.object(endpoint_pages, "MAX_PAGES", 1):
            stopped, cursors = self.collect([first])
        self.assertEqual((stopped["reason"], cursors), ("row_limit", [None]))
        with patch.object(endpoint_pages, "MAX_MEMBERS", 1):
            stopped, _ = self.collect([first, page([{"username": "b"}])])
        self.assertEqual(stopped["reason"], "row_limit")
        self.assertEqual(len(stopped["members"]), 1)
        for reason in ("challenge", "cooldown"):
            with (
                self.subTest(reason=reason),
                self.assertRaises(LiveBrowserError) as error,
            ):
                self.collect([first, LiveBrowserError(reason)])
            self.assertEqual(error.exception.code, reason)
