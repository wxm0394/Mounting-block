import io
import uuid
import zipfile
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient
import backend
from backend import app, get_current_user, TargetLang, AnnotationRequest


def create_dummy_epub_bytes():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("META-INF/container.xml", "<container/>")
    return buf.getvalue()


class MockUser:
    id = "00000000-0000-0000-0000-000000000001"
    email = "test@example.com"


# Override get_current_user dependency
app.dependency_overrides[get_current_user] = lambda: MockUser()
client = TestClient(app)


def test_annotate_target_lang():
    # 1. Valid target_lang in /annotate
    for lang in ["zh-Hans", "zh-Hant", "fr", "es"]:
        req = AnnotationRequest(text="hello", target_lang=lang)
        assert req.target_lang == lang

    # 2. Invalid target_lang in /annotate -> 422
    for invalid in ["de", "ja", "zh", "invalid"]:
        res = client.post("/annotate", json={"text": "hello", "target_lang": invalid})
        assert res.status_code == 422, f"Expected 422 for target_lang={invalid}, got {res.status_code}"
    print("[PASS] test_annotate_target_lang: valid langs accepted, invalid langs return 422")


def test_upload_epub_valid_combinations():
    dummy_epub = create_dummy_epub_bytes()
    
    with patch("backend.get_user_profile", return_value={"is_subscribed": True}), \
         patch.object(backend.supabase.storage, "from_") as mock_storage, \
         patch.object(backend.supabase, "table") as mock_table:
        
        mock_storage.return_value.upload.return_value = {"Key": "epubs/test"}
        mock_table.return_value.insert.return_value.execute.return_value = MagicMock(data=[{"id": "task-1"}])

        # Test valid target_lang and boundary difficulty_levels (500, 900, 1500)
        valid_cases = [
            ("zh-Hans", 900),
            ("zh-Hant", 500),
            ("fr", 1500),
            ("es", 1000),
        ]
        for lang, diff in valid_cases:
            files = {"file": ("book.epub", dummy_epub, "application/epub+zip")}
            data = {"difficulty_level": diff, "target_lang": lang}
            res = client.post("/epub/upload", files=files, data=data)
            assert res.status_code == 200, f"Expected 200 for lang={lang}, diff={diff}, got {res.status_code}: {res.text}"
            assert "task_id" in res.json()
    print("[PASS] test_upload_epub_valid_combinations: valid target_lang & difficulty (500, 900, 1500) return 200")


def test_upload_epub_invalid_target_lang():
    dummy_epub = create_dummy_epub_bytes()
    
    invalid_langs = ["de", "ja", "en", "zh", "fr-FR", "invalid_123"]
    for lang in invalid_langs:
        files = {"file": ("book.epub", dummy_epub, "application/epub+zip")}
        data = {"difficulty_level": 900, "target_lang": lang}
        res = client.post("/epub/upload", files=files, data=data)
        assert res.status_code == 422, f"Expected 422 for invalid target_lang={lang}, got {res.status_code}: {res.text}"
    print("[PASS] test_upload_epub_invalid_target_lang: non-whitelisted target_lang return 422")


def test_upload_epub_out_of_bounds_difficulty():
    dummy_epub = create_dummy_epub_bytes()
    
    out_of_bounds_diffs = [
        499,    # strictly below lower bound 500
        0,      # far below
        -100,   # negative
        1501,   # strictly above upper bound 1500
        2000,   # far above
        99999,  # extreme
    ]
    for diff in out_of_bounds_diffs:
        files = {"file": ("book.epub", dummy_epub, "application/epub+zip")}
        data = {"difficulty_level": diff, "target_lang": "zh-Hans"}
        res = client.post("/epub/upload", files=files, data=data)
        assert res.status_code == 422, f"Expected 422 for difficulty_level={diff}, got {res.status_code}: {res.text}"
    print("[PASS] test_upload_epub_out_of_bounds_difficulty: difficulty_level < 500 or > 1500 return 422")


if __name__ == "__main__":
    test_annotate_target_lang()
    test_upload_epub_valid_combinations()
    test_upload_epub_invalid_target_lang()
    test_upload_epub_out_of_bounds_difficulty()
    print("\nALL TARGET_LANG AND DIFFICULTY TESTS PASSED!")
