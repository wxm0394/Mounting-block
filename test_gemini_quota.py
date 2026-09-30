import sys
from google.genai.errors import ClientError
from backend import batch_translate, GeminiQuotaExhaustedError
import backend
import os

print("Starting Gemini Quota Error Handling Test")
print("-" * 50)

# 正确的 mock 构造方式
def make_client_error(quota_id: str) -> ClientError:
    body = {"error": {
        "code": 429, "status": "RESOURCE_EXHAUSTED",
        "message": "You exceeded your current quota, please check your plan and billing details.",
        "details": [
            {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
             "violations": [{"quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
                              "quotaId": quota_id}]},
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "12s"}
        ]
    }}
    return ClientError(429, body)

temp_429  = make_client_error("GenerateRequestsPerMinutePerProjectPerModel-FreeTier")
fatal_429 = make_client_error("GenerateRequestsPerDayPerProjectPerModel-FreeTier")

annotations_mock = {
    "test_random_12345": {
        "surface": "test",
        "lemma": "test",
        "kind": "word",
        "difficulty": {"base_level": 500, "adjusted_level": 500}
    }
}

class MockResponse:
    def __init__(self, text):
        self.text = text

# 测试用例 1: 验证临时限流 (PerMinute)
def test_per_minute_quota_retries():
    call_count = 0
    def mock_generate_content(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        print(f"  -> mock_generate_content called (attempt {call_count})")
        if call_count <= 2:
            raise temp_429
        return MockResponse('{"items":[{"span":"test_random_12345", "translation":"测试"}]}')

    print("Running test_per_minute_quota_retries (PerMinute temporary limit):")
    
    class MockClient:
        class MockModels:
            def generate_content(self, *args, **kwargs):
                return mock_generate_content(*args, **kwargs)
        models = MockModels()

    import google.genai
    original_client = google.genai.Client
    google.genai.Client = lambda *args, **kwargs: MockClient()

    try:
        # 执行 batch_translate
        result = batch_translate(annotations_mock, target_lang="zh-Hans")
        
        # 断言
        if call_count != 3:
            print(f"❌ FAILED: call_count is {call_count}, expected 3\n")
        else:
            print("✅ test_per_minute_quota_retries PASSED (Retried successfully)\n")
    except Exception as e:
        print(f"❌ ERROR: {e}\n")
    finally:
        google.genai.Client = original_client


# 测试用例 2: 验证真配额耗尽 (PerDay)
def test_per_day_quota_fast_fails():
    call_count = 0
    def mock_generate_content(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        print(f"  -> mock_generate_content called (attempt {call_count})")
        raise fatal_429

    print("Running test_per_day_quota_fast_fails (PerDay hard limit):")
    
    class MockClient:
        class MockModels:
            def generate_content(self, *args, **kwargs):
                return mock_generate_content(*args, **kwargs)
        models = MockModels()

    import google.genai
    original_client = google.genai.Client
    google.genai.Client = lambda *args, **kwargs: MockClient()

    try:
        try:
            batch_translate(annotations_mock, target_lang="zh-Hans")
            print("❌ FAILED: Should have raised GeminiQuotaExhaustedError\n")
        except GeminiQuotaExhaustedError:
            # 断言
            if call_count != 1:
                print(f"❌ FAILED: call_count is {call_count}, expected 1\n")
            else:
                print("✅ test_per_day_quota_fast_fails PASSED (Fast-failed immediately)\n")
    except Exception as e:
        print(f"❌ ERROR: {e}\n")
    finally:
        google.genai.Client = original_client

if __name__ == "__main__":
    test_per_minute_quota_retries()
    test_per_day_quota_fast_fails()
