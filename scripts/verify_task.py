import os
import time
import zipfile
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()
supabase: Client = create_client(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_ROLE_KEY"))

# Poll for completion
while True:
    res = supabase.table("epub_tasks").select("*").order("created_at", desc=True).limit(1).execute()
    if res.data:
        task = res.data[0]
        if task["status"] == "completed":
            print("Task completed!")
            break
        elif task["status"] == "failed":
            print(f"Task failed: {task.get('error_message')}")
            exit(1)
    time.sleep(2)

output_path = task["storage_output_path"]
print(f"Downloading from {output_path}...")
res = supabase.storage.from_("epubs").download(output_path)
with open("test_output.epub", "wb") as f:
    f.write(res)

print("Extracting...")
os.makedirs("test_extract", exist_ok=True)
with zipfile.ZipFile("test_output.epub", 'r') as zip_ref:
    zip_ref.extractall("test_extract")
