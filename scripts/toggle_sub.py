import os
import sys
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

url = os.environ.get("SUPABASE_URL")
key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
supabase: Client = create_client(url, key)

users_response = supabase.auth.admin.list_users()
users = users_response if isinstance(users_response, list) else getattr(users_response, 'users', users_response)
user_id = users[0].id

if len(sys.argv) > 1:
    is_subscribed = sys.argv[1].lower() == 'true'
    supabase.table("user_profiles").update({"is_subscribed": is_subscribed}).eq("user_id", user_id).execute()
    print(f"Updated user {user_id} is_subscribed to {is_subscribed}")
else:
    res = supabase.table("user_profiles").select("is_subscribed").eq("user_id", user_id).execute()
    print(res.data)
