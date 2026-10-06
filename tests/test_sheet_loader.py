"""GoogleSheetLoader: the service account key from a file or from GOOGLE_SERVICE_ACCOUNT_JSON (gspread is faked)."""

import json

import gspread
import pytest

from app.sheet import SHEETS_READONLY_SCOPE, GoogleSheetLoader, SheetFormatError

KEY = {"type": "service_account", "client_email": "bot@x.iam.gserviceaccount.com",
       "private_key": "-----BEGIN PRIVATE KEY-----\nSECRETVALUE\n-----END PRIVATE KEY-----\n"}


class FakeClient:
    def __init__(self):
        self.timeout = None

    def set_timeout(self, seconds):
        self.timeout = seconds

    def open_by_key(self, sheet_id):
        self.sheet_id = sheet_id
        return self

    def worksheet(self, tab):
        self.tab = tab
        return self

    def get_all_values(self, value_render_option=None):
        return [["property_id"], ["P-1"]]


@pytest.fixture
def fake_gspread(monkeypatch):
    calls = {}
    client = FakeClient()

    def from_dict(info, scopes):
        calls["from_dict"] = (info, list(scopes))
        return client

    def from_file(filename, scopes):
        calls["from_file"] = (filename, list(scopes))
        return client

    monkeypatch.setattr(gspread, "service_account_from_dict", from_dict)
    monkeypatch.setattr(gspread, "service_account", from_file)
    return calls, client


def test_json_setting_is_used_with_read_only_scope(fake_gspread):
    calls, client = fake_gspread
    loader = GoogleSheetLoader("secrets/unused.json", "SHEET", "Properties", service_account_json=json.dumps(KEY))
    assert loader() == [["property_id"], ["P-1"]]
    assert calls["from_dict"] == (KEY, [SHEETS_READONLY_SCOPE])
    assert "from_file" not in calls
    assert (client.sheet_id, client.tab, client.timeout) == ("SHEET", "Properties", 30)


def test_quotes_kept_by_the_host_are_removed(fake_gspread):
    calls, _ = fake_gspread
    GoogleSheetLoader("x", "SHEET", "Properties", service_account_json="'" + json.dumps(KEY) + "'")()
    assert calls["from_dict"][0] == KEY


def test_file_is_used_when_no_json_setting(fake_gspread):
    calls, _ = fake_gspread
    GoogleSheetLoader("secrets/service-account.json", "SHEET", "Properties")()
    assert calls["from_file"] == ("secrets/service-account.json", [SHEETS_READONLY_SCOPE])
    assert "from_dict" not in calls


def test_invalid_json_error_never_contains_the_key(fake_gspread):
    broken = json.dumps(KEY)[:-5]  # cut off: not valid JSON
    with pytest.raises(SheetFormatError) as info:
        GoogleSheetLoader("x", "SHEET", "Properties", service_account_json=broken)()
    assert "not valid JSON" in str(info.value)
    assert "SECRETVALUE" not in str(info.value)
    assert info.value.__cause__ is None  # the JSON parser's message (with a snippet) is dropped


def test_store_passes_the_json_setting(monkeypatch):
    from app.config import get_settings
    from app.properties import build_property_store

    settings = get_settings().model_copy(update={
        "google_sheet_id": "SHEET",
        "google_service_account_json": __import__("pydantic").SecretStr(json.dumps(KEY)),
    })
    store = build_property_store(settings)
    assert store._loader.service_account_json == json.dumps(KEY)
    assert store._loader.sheet_id == "SHEET"
