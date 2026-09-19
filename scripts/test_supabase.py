import os
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

url = os.environ.get("SUPABASE_URL")
key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
supabase: Client = create_client(url, key)

print("1. Checking epub_tasks table...")
try:
    res = supabase.table("epub_tasks").select("id").limit(1).execute()
    print("   [OK] Table exists and is accessible. Rows returned:", len(res.data))
except Exception as e:
    print("   [ERROR] accessing epub_tasks:", e)

print("\n2. Checking epubs bucket...")
try:
    res = supabase.storage.get_bucket("epubs")
    print(f"   [OK] Bucket '{res.id}' exists.")
    print(f"   [INFO] Is Public: {res.public}")
except Exception as e:
    print("   [ERROR] accessing bucket:", e)
