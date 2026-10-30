import os
import time
import uuid
import functools
import traceback
import tempfile
from datetime import datetime, timezone
from pydantic_settings import BaseSettings, SettingsConfigDict
from supabase import create_client, Client
import epub_pipeline
from backend import GeminiQuotaExhaustedError

class Settings(BaseSettings):
    supabase_url: str
    supabase_service_role_key: str
    
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

settings = Settings()
supabase: Client = create_client(settings.supabase_url, settings.supabase_service_role_key)

WORKER_ID = f"worker-{uuid.uuid4().hex[:8]}"

# ── Supabase fault tolerance: retry + client rebuild ──────────────────────
# Supabase 的 HTTP 连接池会在空闲/远端断开后变 stale，后续调用报
# "Server disconnected" / ConnectionTerminated / 555。重试前重建客户端可恢复。

def rebuild_supabase_client():
    """Rebuild the Supabase client (fresh HTTP connection pool)."""
    global supabase
    supabase = create_client(settings.supabase_url, settings.supabase_service_role_key)


def _is_connection_interruption(exc: Exception) -> bool:
    """True if the exception looks like a stale/broken Supabase connection."""
    name = exc.__class__.__name__
    if "ConnectionTerminated" in name or "RemoteProtocolError" in name:
        return True
    msg = str(exc)
    if "Server disconnected" in msg or "ConnectionTerminated" in msg:
        return True
    code = getattr(exc, "code", None)
    if code is not None and str(code) == "555":
        return True
    return False


def supabase_retry(fn):
    """Retry a Supabase call up to 3 attempts with exponential backoff (1s, 2s).

    On connection-interruption signatures, rebuild the client after the
    backoff wait, before the next attempt. After 3 failures, log ERROR and
    re-raise. Functions using the global `supabase` pick up the rebuilt
    client automatically on retry (global lookup at call time).
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        last_exc = None
        for attempt in range(1, 4):
            try:
                result = fn(*args, **kwargs)
                if attempt > 1:
                    print(f"[SUCCESS] Operation completed on attempt {attempt}.")
                return result
            except Exception as e:
                last_exc = e
                if attempt == 3:
                    break
                if _is_connection_interruption(e):
                    print(f"[REBUILD] Triggered client rebuild due to: {e} (attempt {attempt}/3)")
                time.sleep(2 ** (attempt - 1))
                if _is_connection_interruption(e):
                    try:
                        rebuild_supabase_client()
                    except Exception as re:
                        print(f"[REBUILD] Client rebuild failed: {re}")
        detail = f"code {last_exc.code}" if getattr(last_exc, "code", None) else str(last_exc)
        print(f"[ERROR] Supabase operation failed after 3 retries: {detail}")
        raise last_exc
    return wrapper


@supabase_retry
def _update_task_row(task_id: str, fields: dict):
    """Single Supabase epub_tasks row update (retry-wrapped)."""
    return supabase.table("epub_tasks").update(fields).eq("id", task_id).execute()


@supabase_retry
def _create_signed_url(storage_path: str, expires_in: int):
    """Single Supabase Storage signed URL creation (retry-wrapped)."""
    return supabase.storage.from_("epubs").create_signed_url(storage_path, expires_in)


@supabase_retry
def _claim_task():
    """Single claim_epub_task RPC call (retry-wrapped)."""
    return supabase.rpc("claim_epub_task", {"p_worker_id": WORKER_ID}).execute()

@supabase_retry
def download_file(storage_path: str, local_path: str):
    """Downloads a file from Supabase Storage private bucket."""
    with open(local_path, "wb") as f:
        bucket = "epubs"
        res = supabase.storage.from_(bucket).download(storage_path)
        f.write(res)

@supabase_retry
def upload_file(local_path: str, storage_path: str):
    """Uploads a file to Supabase Storage private bucket."""
    with open(local_path, "rb") as f:
        bucket = "epubs"
        supabase.storage.from_(bucket).upload(storage_path, f, file_options={"content-type": "application/epub+zip"})

def update_task_progress(task_id: str, percent: int, desc: str):
    """Callback passed to the pipeline to update progress in DB."""
    try:
        _update_task_row(task_id, {
            "progress_percent": percent,
            "current_step_desc": desc,
            "updated_at": datetime.now(timezone.utc).isoformat()
        })
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
            signed_url_res = _create_signed_url(output_storage_path, 7 * 24 * 3600)
            print(f"[DEBUG] create_signed_url response: {signed_url_res}")
            download_url = signed_url_res.get("signedURL") or signed_url_res.get("signedUrl", "")
            
            # 4. Mark as Completed
            step_desc = "任务完成！"
            if stats["total_unique_words"] == 0:
                step_desc = "本书生词均低于您的难度门槛，任务完成！"
            elif stats["annotated_count"] == 0:
                step_desc = "任务完成，但部分内容可能未能正确标注，如有疑问请联系我们。"
                book_hash = stats.get("book_hash", "unknown")
                print(f"[WARN] candidate_meta non-empty but annotated_count=0, book_hash={book_hash}")
                
            _update_task_row(task_id, {
                "status": "completed",
                "progress_percent": 100,
                "current_step_desc": step_desc,
                "storage_output_path": output_storage_path,
                "download_url": download_url,
                "total_chapters": stats["total_chapters"],
                "total_unique_words": stats["total_unique_words"],
                "translated_words": stats["annotated_count"], # using annotated count for now
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat()
            })
            
            print(f"--- Task {task_id} Completed Successfully ---")
            
        except GeminiQuotaExhaustedError as e:
            print(f"--- Task {task_id} Failed: Gemini API Quota Exhausted ---")
            _update_task_row(task_id, {
                "status": "failed",
                "current_step_desc": "API 配额耗尽，任务终止",
                "error_message": "Gemini API 每日配额已用完，无法继续翻译。请稍后重试或确认 API Key 计费已开通。",
                "error_detail": str(e),
                "updated_at": datetime.now(timezone.utc).isoformat()
            })
            
        except Exception as e:
            traceback_str = traceback.format_exc()
            print(f"--- Task {task_id} Failed ---")
            print(traceback_str)
            
            # 5. Mark as Failed
            _update_task_row(task_id, {
                "status": "failed",
                "current_step_desc": "任务失败",
                "error_message": f"处理出错: {str(e)}",
                "error_detail": traceback_str,
                "updated_at": datetime.now(timezone.utc).isoformat()
            })


def main():
    print(f"🚀 Starting EPUB Worker [{WORKER_ID}]...")
    print("Polling public.epub_tasks every 3 seconds for new tasks.")
    
    while True:
        try:
            # Atomic claim via RPC
            res = _claim_task()
            
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
