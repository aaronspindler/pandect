import io
import json
from datetime import UTC, datetime
from unittest.mock import patch
from uuid import uuid4

from django.test import SimpleTestCase, override_settings

from socialmedia import instagram_connector as connector


@override_settings(
    PANDECT_INSTAGRAM_PILOT_URL="http://instagram-pilot:8080",
    PANDECT_INSTAGRAM_PILOT_KEY="synthetic-unit-test-pilot-key-material",
)
class ConnectorTests(SimpleTestCase):
    def test_signed_in_website_configuration_does_not_enable_scheduling(self):
        self.assertTrue(connector.scheduling_available())
        with self.settings(PANDECT_INSTAGRAM_LIVE={"url": "http://127.0.0.1:8765"}):
            self.assertFalse(connector.scheduling_available())
        with self.settings(PANDECT_INSTAGRAM_PILOT_URL=""):
            self.assertFalse(connector.scheduling_available())

    def test_synthetic_connection_expiry_verifies_signed_binding(self):
        owner = uuid4()
        issued = 1_800_000_000
        expires = issued + connector.SYNTHETIC_SESSION_SECONDS
        with patch("django.core.signing.time.time", return_value=issued):
            pair = connector.start_pairing(owner)
            session = connector.finish_pairing(owner, pair["id"])["session"]
            self.assertEqual(
                connector.connection_expires_at(session),
                datetime.fromtimestamp(expires, tz=UTC),
            )
            self.assertIsNone(connector.connection_expires_at("invalid"))
        with patch("django.core.signing.time.time", return_value=expires + 1):
            self.assertIsNone(connector.connection_expires_at(session))

    def test_pair_is_bound_to_authenticated_investigator(self):
        owner = uuid4()
        pair = connector.start_pairing(owner)
        self.assertEqual(pair["url"], "")
        for user, token in [(uuid4(), pair["id"]), (owner, "invalid")]:
            with self.assertRaisesMessage(connector.ConnectorError, "pairing_expired"):
                connector.finish_pairing(user, token)
        result = connector.finish_pairing(owner, pair["id"])
        with self.assertRaisesMessage(connector.ConnectorError, "access_lost"):
            connector.collect(result["session"], "third.party")
        with self.assertRaisesMessage(connector.ConnectorError, "challenge"):
            connector.collect("invalid", connector.PILOT_TARGET)

    def test_only_fixture_data_crosses_service_boundary(self):
        owner = uuid4()
        pair = connector.finish_pairing(owner, connector.start_pairing(owner)["id"])
        response = {
            "collector_version": connector.VERSION,
            "target": connector.PILOT_TARGET,
            "followers": {},
            "following": {},
        }
        with patch.object(connector, "build_opener") as opener:
            call = opener.return_value.open
            call.return_value = io.BytesIO(json.dumps(response).encode())
            data = connector.collect(pair["session"], connector.PILOT_TARGET)
            self.assertEqual(data["observer"], pair["observer"])
            self.assertEqual(data["target_id"], connector.PILOT_TARGET_ID)
            self.assertEqual(call.call_args.args[0].data, b"{}")
            self.assertEqual(
                call.call_args.args[0].full_url, "http://instagram-pilot:8080/collect"
            )
            for invalid in (
                b"[]",
                b"garbage",
                json.dumps(response | {"collector_version": "unqualified"}).encode(),
            ):
                call.return_value = io.BytesIO(invalid)
                with self.assertRaisesMessage(
                    connector.ConnectorError, "invalid_response"
                ):
                    connector.collect(pair["session"], connector.PILOT_TARGET)
        with self.settings(PANDECT_INSTAGRAM_PILOT_KEY=""):
            self.assertFalse(connector.available())
            with self.assertRaisesMessage(connector.ConnectorError, "not_configured"):
                connector.collect(pair["session"], connector.PILOT_TARGET)
