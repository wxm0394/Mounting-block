import os
import sys
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()
url = os.environ.get("SUPABASE_URL")
key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
supabase = create_client(url, key)

res = (
    supabase.table("epub_tasks")
    .select("id, status, progress_percent, difficulty_level, original_filename, created_at, error_message")
    .order("created_at", desc=True)
    .limit(20)
    .execute()
)

print(f"=== Total {len(res.data)} Most Recent Tasks ===")
for r in res.data:
    task_id = r.get("id")
    status = r.get("status")
    progress = r.get("progress_percent")
    filename = r.get("original_filename")
    created = r.get("created_at")
    err = r.get("error_message") or ""
    print(f"[{status.upper():<10}] {progress:>3}% | {task_id} | {filename} | {created}")
    if err:
        print(f"             └─ Error: {err}")

if len(sys.argv) > 1 and sys.argv[1] == "--cleanup":
    print("\n[Action] Cleaning up hanging tasks (pending/scanning)...")
    cleanup_res = (
        supabase.table("epub_tasks")
        .update({"status": "failed", "error_message": "Manual cleanup via script"})
        .in_("status", ["pending", "scanning", "translating", "generating"])
        .execute()
    )
    print("Done cleaning hanging tasks.")
