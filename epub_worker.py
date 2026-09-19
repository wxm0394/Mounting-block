import os
import time
import uuid
import traceback
import tempfile
from datetime import datetime, timezone
from pydantic_settings import BaseSettings, SettingsConfigDict
from supabase import create_client, Client
import epub_pipeline

class Settings(BaseSettings):
    supabase_url: str
    supabase_service_role_key: str
    
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

settings = Settings()
supabase: Client = create_client(settings.supabase_url, settings.supabase_service_role_key)

WORKER_ID = f"worker-{uuid.uuid4().hex[:8]}"

def download_file(storage_path: str, local_path: str):
    """Downloads a file from Supabase Storage private bucket."""
    with open(local_path, "wb") as f:
        bucket = "epubs"
        res = supabase.storage.from_(bucket).download(storage_path)
        f.write(res)

def upload_file(local_path: str, storage_path: str):
    """Uploads a file to Supabase Storage private bucket."""
    with open(local_path, "rb") as f:
        bucket = "epubs"
        supabase.storage.from_(bucket).upload(storage_path, f, file_options={"content-type": "application/epub+zip"})

def update_task_progress(task_id: str, percent: int, desc: str):
    """Callback passed to the pipeline to update progress in DB."""
    try:
        supabase.table("epub_tasks").update({
            "progress_percent": percent,
            "current_step_desc": desc,
            "updated_at": datetime.now(timezone.utc).isoformat()
        }).eq("id", task_id).execute()
        print(f"[Task {task_id}] {percent}% - {desc}")
    except Exception as e:
        print(f"Warning: Failed to update progress for task {task_id}: {e}")

def process_task(task: dict):
    task_id = task["id"]
    input_storage_path = task["storage_input_path"]
    user_id = task["user_id"]
    
    # Generate an output path if not provided
    output_storage_path = task.get("storage_output_path")
    if not output_storage_path:
        output_storage_path = f"{user_id}/{task_id}/annotated_{task['original_filename']}"
    
    print(f"--- Starting Task {task_id} ---")
    
    with tempfile.TemporaryDirectory() as tmpdir:
        local_in = os.path.join(tmpdir, "input.epub")
        local_out = os.path.join(tmpdir, "output.epub")
        
        try:
            # 1. Download
            update_task_progress(task_id, 2, "正在下载 EPUB 文件...")
            download_file(input_storage_path, local_in)
            
            # 2. Run Pipeline
            def progress_cb(percent: int, desc: str):
                update_task_progress(task_id, percent, desc)
                
            stats = epub_pipeline.process_epub_file(
                input_epub_path=local_in,
                output_epub_path=local_out,
                target_lang=task.get("target_lang", "zh-Hans"),
                difficulty_level=task.get("difficulty_level", 900),
                per_chapter_limit=2,
                progress_callback=progress_cb
            )
            
            # 3. Upload Result
            update_task_progress(task_id, 95, "正在上传标注后的 EPUB 文件...")
            upload_file(local_out, output_storage_path)
            
            # Generate a signed download URL (valid for 7 days)
            bucket = "epubs"
            signed_url_res = supabase.storage.from_(bucket).create_signed_url(output_storage_path, 7 * 24 * 3600)
            print(f"[DEBUG] create_signed_url response: {signed_url_res}")
            download_url = signed_url_res.get("signedURL") or signed_url_res.get("signedUrl", "")
            
            # 4. Mark as Completed
            supabase.table("epub_tasks").update({
                "status": "completed",
                "progress_percent": 100,
                "current_step_desc": "任务完成！",
                "storage_output_path": output_storage_path,
                "download_url": download_url,
                "total_chapters": stats["total_chapters"],
                "total_unique_words": stats["total_unique_words"],
                "translated_words": stats["annotated_count"], # using annotated count for now
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat()
            }).eq("id", task_id).execute()
            
            print(f"--- Task {task_id} Completed Successfully ---")
            
        except Exception as e:
            traceback_str = traceback.format_exc()
            print(f"--- Task {task_id} Failed ---")
            print(traceback_str)
            
            # 5. Mark as Failed
            supabase.table("epub_tasks").update({
                "status": "failed",
                "current_step_desc": "任务失败",
                "error_message": f"处理出错: {str(e)}",
                "error_detail": traceback_str,
                "updated_at": datetime.now(timezone.utc).isoformat()
            }).eq("id", task_id).execute()


def main():
    print(f"🚀 Starting EPUB Worker [{WORKER_ID}]...")
    print("Polling public.epub_tasks every 3 seconds for new tasks.")
    
    while True:
        try:
            # Atomic claim via RPC
            res = supabase.rpc("claim_epub_task", {"p_worker_id": WORKER_ID}).execute()
            
            # The RPC returns SETOF epub_tasks, so res.data is a list
            if res.data and len(res.data) > 0:
                task = res.data[0]
                process_task(task)
            else:
                # No pending tasks, sleep and poll again
                time.sleep(3)
                
        except Exception as e:
            print(f"Worker Loop Error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
