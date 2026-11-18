import io
import json
from unittest.mock import patch
from uuid import uuid4

from django.test import SimpleTestCase, override_settings

from socialmedia import instagram_connector as connector


@override_settings(
    PANDECT_INSTAGRAM_PILOT_URL="http://instagram-pilot:8080",
    PANDECT_INSTAGRAM_PILOT_KEY="synthetic-unit-test-pilot-key-material",
)
class ConnectorTests(SimpleTestCase):
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
