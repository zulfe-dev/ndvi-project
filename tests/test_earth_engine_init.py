import json
import unittest
from unittest import mock

from app.services import ndvi_service


class InitializeEarthEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sample_creds = {
            "type": "service_account",
            "project_id": "demo-project",
            "private_key_id": "demo-key-id",
            "private_key": "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n",
            "client_email": "svc@example.iam.gserviceaccount.com",
            "client_id": "1234567890",
            "token_uri": "https://oauth2.googleapis.com/token",
        }

    def test_string_credentials_are_parsed_once_and_passed_as_string(self) -> None:
        creds_str = json.dumps(self.sample_creds)
        original_loads = ndvi_service.json.loads

        with (
            mock.patch.object(ndvi_service.os.environ, "get", return_value=creds_str),
            mock.patch.object(ndvi_service.json, "loads", wraps=original_loads) as loads_mock,
            mock.patch.object(ndvi_service.ee, "ServiceAccountCredentials") as credentials_ctor,
            mock.patch.object(ndvi_service.ee, "Initialize") as initialize_mock,
        ):
            ndvi_service.initialize_earth_engine()

        loads_arg = loads_mock.call_args.args[0]
        self.assertIsInstance(loads_arg, str)
        self.assertEqual(loads_arg, creds_str)
        self.assertEqual(credentials_ctor.call_args.args[0], self.sample_creds["client_email"])
        self.assertIsInstance(credentials_ctor.call_args.kwargs["key_data"], str)
        self.assertEqual(credentials_ctor.call_args.kwargs["key_data"], creds_str)
        initialize_mock.assert_called_once_with(credentials_ctor.return_value)

    def test_dict_credentials_are_stringified_before_json_loads(self) -> None:
        original_dumps = ndvi_service.json.dumps
        original_loads = ndvi_service.json.loads
        serialized_creds = original_dumps(self.sample_creds)

        with (
            mock.patch.object(ndvi_service.os.environ, "get", return_value=self.sample_creds),
            mock.patch.object(ndvi_service.json, "dumps", wraps=original_dumps) as dumps_mock,
            mock.patch.object(ndvi_service.json, "loads", wraps=original_loads) as loads_mock,
            mock.patch.object(ndvi_service.ee, "ServiceAccountCredentials") as credentials_ctor,
            mock.patch.object(ndvi_service.ee, "Initialize") as initialize_mock,
        ):
            ndvi_service.initialize_earth_engine()

        dumps_mock.assert_called_once_with(self.sample_creds)
        loads_arg = loads_mock.call_args.args[0]
        self.assertIsInstance(loads_arg, str)
        self.assertEqual(loads_arg, serialized_creds)
        self.assertEqual(credentials_ctor.call_args.args[0], self.sample_creds["client_email"])
        self.assertIsInstance(credentials_ctor.call_args.kwargs["key_data"], str)
        self.assertEqual(credentials_ctor.call_args.kwargs["key_data"], serialized_creds)
        initialize_mock.assert_called_once_with(credentials_ctor.return_value)


if __name__ == "__main__":
    unittest.main()

