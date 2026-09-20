import os
import sys
import json
import hmac
import hashlib
import requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

BASE_URL = "http://localhost:8000"
WEBHOOK_URL = f"{BASE_URL}/webhook/lemonsqueezy"
SECRET = os.environ.get("LEMONSQUEEZY_WEBHOOK_SECRET")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

if not SECRET:
    print("[ERROR] LEMONSQUEEZY_WEBHOOK_SECRET not set in .env")
    sys.exit(1)

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# 挑选一个测试用户
res = supabase.table("user_profiles").select("user_id, is_subscribed, subscription_updated_at").limit(1).execute()
if not res.data:
    print("[ERROR] No user found in user_profiles to test with.")
    sys.exit(1)

TEST_USER_ID = res.data[0]["user_id"]
print(f"[*] Using Test User ID: {TEST_USER_ID}")

# 重置测试用户状态
print("[*] Resetting test user profile to clean initial state...")
try:
    supabase.table("user_profiles").update({
        "is_subscribed": False,
        "subscription_updated_at": None,
        "subscription_id": None,
        "customer_id": None
    }).eq("user_id", TEST_USER_ID).execute()
    print("    [OK] User profile reset to: is_subscribed=False, subscription_updated_at=None")
except Exception as e:
    print(f"    [FAIL] Could not update user_profiles: {e}")
    sys.exit(1)

def sign_payload(payload_bytes: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()

def make_payload(event_name: str, user_id: str, updated_at: str, sub_id: str = "sub_test_001", cust_id: str = "cust_test_999", status: str = "active"):
    return {
        "meta": {
            "event_name": event_name,
            "custom_data": {
                "user_id": user_id
            }
        },
        "data": {
            "type": "subscriptions",
            "id": sub_id,
            "attributes": {
                "customer_id": cust_id,
                "status": status,
                "updated_at": updated_at,
                "created_at": updated_at
            }
        }
    }

print("\n" + "="*70)
print("TEST 1: 签名鉴权测试（伪造/缺失签名拦截）")
print("="*70)

# 1.1 无签名
p1 = json.dumps(make_payload("subscription_created", TEST_USER_ID, "2026-09-20T10:00:00Z")).encode("utf-8")
r_no_sig = requests.post(WEBHOOK_URL, data=p1, headers={"Content-Type": "application/json"})
print(f"1.1 无 X-Signature 请求 -> HTTP {r_no_sig.status_code}, Body: {r_no_sig.text}")
assert r_no_sig.status_code == 400, f"Expected 400, got {r_no_sig.status_code}"

# 1.2 伪造错误签名
r_bad_sig = requests.post(WEBHOOK_URL, data=p1, headers={
    "Content-Type": "application/json",
    "X-Signature": "bad_signature_deadbeef1234567890abcdef"
})
print(f"1.2 伪造错误签名请求 -> HTTP {r_bad_sig.status_code}, Body: {r_bad_sig.text}")
assert r_bad_sig.status_code == 400, f"Expected 400, got {r_bad_sig.status_code}"

# 校验数据库未发生任何篡改
prof = supabase.table("user_profiles").select("*").eq("user_id", TEST_USER_ID).execute().data[0]
assert prof["is_subscribed"] is False, "DB altered on forged request!"
assert prof.get("subscription_updated_at") is None
print("    [PASS] 伪造请求被 100% 拦截，数据库状态保持原样。")

print("\n" + "="*70)
print("TEST 2: 合法签名 subscription_created 测试 (首次入库 + NULL 时序推进)")
print("="*70)

t1_str = "2026-09-20T10:00:00Z"
p2 = json.dumps(make_payload("subscription_created", TEST_USER_ID, t1_str, sub_id="sub_ls_1001", cust_id="cust_ls_5001")).encode("utf-8")
sig2 = sign_payload(p2, SECRET)
r2 = requests.post(WEBHOOK_URL, data=p2, headers={"Content-Type": "application/json", "X-Signature": sig2})
print(f"2.1 发送合法 subscription_created -> HTTP {r2.status_code}, Body: {r2.text}")
assert r2.status_code == 200, f"Expected 200, got {r2.status_code}"

prof = supabase.table("user_profiles").select("*").eq("user_id", TEST_USER_ID).execute().data[0]
print(f"    当前数据库状态: is_subscribed={prof['is_subscribed']}, subscription_id={prof.get('subscription_id')}, customer_id={prof.get('customer_id')}, subscription_updated_at={prof.get('subscription_updated_at')}")
assert prof["is_subscribed"] is True, "is_subscribed should be True!"
assert prof.get("subscription_id") == "sub_ls_1001"
assert prof.get("customer_id") == "cust_ls_5001"
print("    [PASS] subscription_created 成功生效，用户翻转为已订阅，外部 ID 与时间戳准确记录。")

print("\n" + "="*70)
print("TEST 3: 宽限期测试 subscription_cancelled (保持 is_subscribed=True 不变)")
print("="*70)

t2_str = "2026-09-20T11:00:00Z"
p3 = json.dumps(make_payload("subscription_cancelled", TEST_USER_ID, t2_str, sub_id="sub_ls_1001", cust_id="cust_ls_5001")).encode("utf-8")
sig3 = sign_payload(p3, SECRET)
r3 = requests.post(WEBHOOK_URL, data=p3, headers={"Content-Type": "application/json", "X-Signature": sig3})
print(f"3.1 发送合法 subscription_cancelled -> HTTP {r3.status_code}, Body: {r3.text}")
assert r3.status_code == 200, f"Expected 200, got {r3.status_code}"

prof = supabase.table("user_profiles").select("*").eq("user_id", TEST_USER_ID).execute().data[0]
print(f"    当前数据库状态: is_subscribed={prof['is_subscribed']}, subscription_updated_at={prof.get('subscription_updated_at')}")
assert prof["is_subscribed"] is True, "is_subscribed MUST remain True under grace period!"
print("    [PASS] subscription_cancelled 成功推进时间戳，且严格遵守宽限期逻辑保持 is_subscribed=True！")

print("\n" + "="*70)
print("TEST 4: 宽限期终结 subscription_expired (翻转 is_subscribed=False)")
print("="*70)

t3_str = "2026-09-20T12:00:00Z"
p4 = json.dumps(make_payload("subscription_expired", TEST_USER_ID, t3_str, sub_id="sub_ls_1001", cust_id="cust_ls_5001")).encode("utf-8")
sig4 = sign_payload(p4, SECRET)
r4 = requests.post(WEBHOOK_URL, data=p4, headers={"Content-Type": "application/json", "X-Signature": sig4})
print(f"4.1 发送合法 subscription_expired -> HTTP {r4.status_code}, Body: {r4.text}")
assert r4.status_code == 200, f"Expected 200, got {r4.status_code}"

prof = supabase.table("user_profiles").select("*").eq("user_id", TEST_USER_ID).execute().data[0]
print(f"    当前数据库状态: is_subscribed={prof['is_subscribed']}, subscription_updated_at={prof.get('subscription_updated_at')}")
assert prof["is_subscribed"] is False, "is_subscribed should be flipped to False on expired!"
print("    [PASS] subscription_expired 到期彻底收回权限，is_subscribed 翻转为 False。")

print("\n" + "="*70)
print("TEST 5: 网络乱序与逆序到达防御测试 (防旧事件倒灌覆盖)")
print("="*70)

# 当前库中最新时间戳为 t3_str (12:00:00)。现在模拟一个网络延迟重发的旧 created 事件 (10:30:00，早于 12:00:00)
t_old_str = "2026-09-20T10:30:00Z"
p5 = json.dumps(make_payload("subscription_created", TEST_USER_ID, t_old_str, sub_id="sub_ls_1001", cust_id="cust_ls_5001")).encode("utf-8")
sig5 = sign_payload(p5, SECRET)
r5 = requests.post(WEBHOOK_URL, data=p5, headers={"Content-Type": "application/json", "X-Signature": sig5})
print(f"5.1 发送乱序旧事件 (更新时间早于当前记录) -> HTTP {r5.status_code}, Body: {r5.text}")
assert r5.status_code == 200, f"Expected 200 (ACK), got {r5.status_code}"
assert r5.json().get("status") == "ignored", "Expected status='ignored'"
assert r5.json().get("reason") == "stale_event", "Expected reason='stale_event'"

prof = supabase.table("user_profiles").select("*").eq("user_id", TEST_USER_ID).execute().data[0]
print(f"    校验数据库状态: is_subscribed={prof['is_subscribed']}, subscription_updated_at={prof.get('subscription_updated_at')}")
assert prof["is_subscribed"] is False, "CRITICAL BUG: Stale event reverted user status to subscribed!"
print("    [PASS] 旧事件被精准识别并忽略，用户 is_subscribed 坚决保持 False，防倒灌机制生效！")

print("\n" + "="*70)
print("TEST 6: 检查 webhook_logs 表中的真实审计流水")
print("="*70)
try:
    logs_res = supabase.table("webhook_logs").select("*").eq("user_id", TEST_USER_ID).order("processed_at", desc=False).execute()
    logs = logs_res.data
    print(f"[*] 共查询到 {len(logs)} 条流水记录:")
    for idx, row in enumerate(logs, 1):
        print(f"    [{idx}] event_name={row['event_name']:<25} status={row['status']:<15} event_updated_at={row['event_updated_at']} processed_at={row['processed_at']}")
except Exception as e:
    print(f"    [FAIL] 查询 webhook_logs 异常: {e}")
