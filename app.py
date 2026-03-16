import os
import re
import json
import time
import shutil
import uuid
import threading
from queue import Queue, Empty
from pathlib import Path

from flask import Flask, request, jsonify, render_template, Response, send_file

app = Flask(__name__)

CLOUD_MODE = os.environ.get("CLOUD_MODE", "0") == "1"
DOWNLOAD_DIR = str(Path.home() / "Downloads")
TEMP_DIR = str(Path(__file__).parent / "temp_downloads")
os.makedirs(TEMP_DIR, exist_ok=True)

# Store active download progress queues
progress_queues = {}

# Track temp files for cleanup: {file_id: {"path": ..., "created": timestamp}}
temp_files = {}
TEMP_FILE_TTL = 30 * 60  # 30 minutes

# Common yt-dlp options to avoid YouTube bot detection
YDL_BASE_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "extractor_args": {"youtube": {"player_client": ["mediaconnect"]}},
    "http_headers": {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    },
}

# Use cookies.txt if it exists
COOKIES_FILE = os.path.join(os.path.dirname(__file__), "cookies.txt")
if os.path.exists(COOKIES_FILE):
    YDL_BASE_OPTS["cookiefile"] = COOKIES_FILE


def cleanup_temp_files():
    """Remove temp files older than TTL, runs every 5 minutes."""
    while True:
        time.sleep(300)
        now = time.time()
        expired = [fid for fid, info in temp_files.items()
                   if now - info["created"] > TEMP_FILE_TTL]
        for fid in expired:
            info = temp_files.pop(fid, None)
            if info and os.path.exists(info["path"]):
                try:
                    os.remove(info["path"])
                except OSError:
                    pass


# Start cleanup thread
cleanup_thread = threading.Thread(target=cleanup_temp_files, daemon=True)
cleanup_thread.start()


@app.route("/")
def index():
    return render_template("index.html", cloud_mode=CLOUD_MODE)


@app.route("/api/check")
def check_ffmpeg():
    has_ffmpeg = shutil.which("ffmpeg") is not None
    return jsonify({"ffmpeg": has_ffmpeg})


@app.route("/api/info", methods=["POST"])
def get_info():
    data = request.get_json()
    url = data.get("url", "").strip()

    if not url:
        return jsonify({"error": "請提供 YouTube 網址"}), 400

    try:
        import yt_dlp

        ydl_opts = {**YDL_BASE_OPTS}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        formats = info.get("formats", [])

        # Group video formats by resolution, pick best per resolution
        video_map = {}
        for f in formats:
            h = f.get("height")
            vcodec = f.get("vcodec", "none")
            if not h or vcodec == "none":
                continue
            ext = f.get("ext", "")
            # Prefer mp4/webm
            if ext not in ("mp4", "webm"):
                continue
            key = h
            filesize = f.get("filesize") or f.get("filesize_approx") or 0
            if key not in video_map or filesize > (video_map[key].get("filesize") or 0):
                video_map[key] = {
                    "format_id": f["format_id"],
                    "height": h,
                    "ext": ext,
                    "filesize": filesize,
                    "fps": f.get("fps", 30),
                    "vcodec": vcodec,
                }

        # Find best audio
        best_audio = None
        for f in formats:
            acodec = f.get("acodec", "none")
            vcodec = f.get("vcodec", "none")
            if acodec == "none" or vcodec != "none":
                continue
            abr = f.get("abr") or 0
            if best_audio is None or abr > (best_audio.get("abr") or 0):
                best_audio = {
                    "format_id": f["format_id"],
                    "abr": abr,
                    "ext": f.get("ext", ""),
                }

        # Build quality options using generic format selectors (works on all servers)
        target_heights = [2160, 1440, 1080, 720, 480, 360]
        quality_options = []
        for h in target_heights:
            if h in video_map:
                v = video_map[h]
                # Use generic format selector instead of specific format_id
                fmt_str = f"bestvideo[height<={h}]+bestaudio/best[height<={h}]/best"
                size_mb = round(v["filesize"] / (1024 * 1024), 1) if v["filesize"] else None
                label = f"{h}p"
                if v.get("fps") and v["fps"] > 30:
                    label = f"{h}p{v['fps']}"
                quality_options.append({
                    "format_id": fmt_str,
                    "label": label,
                    "size_mb": size_mb,
                })

        # Also add a "best audio only" option
        if best_audio:
            quality_options.append({
                "format_id": "bestaudio/best",
                "label": "僅音訊 (MP3)",
                "size_mb": None,
                "audio_only": True,
            })

        if not quality_options:
            quality_options.append({
                "format_id": "best",
                "label": "最佳畫質",
                "size_mb": None,
            })

        duration = info.get("duration", 0)
        minutes = int(duration // 60)
        seconds = int(duration % 60)

        result = {
            "title": info.get("title", "未知"),
            "thumbnail": info.get("thumbnail", ""),
            "duration": f"{minutes}:{seconds:02d}",
            "duration_seconds": duration,
            "channel": info.get("channel", info.get("uploader", "")),
            "qualities": quality_options,
        }
        return jsonify(result)

    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/download")
def download_video():
    url = request.args.get("url", "").strip()
    format_id = request.args.get("format_id", "best")
    audio_only = request.args.get("audio_only", "false") == "true"

    if not url:
        return jsonify({"error": "缺少 URL"}), 400

    download_id = str(uuid.uuid4())
    q = Queue()
    progress_queues[download_id] = q

    # In cloud mode, download to temp dir; otherwise to Downloads
    dest_dir = TEMP_DIR if CLOUD_MODE else DOWNLOAD_DIR

    def do_download():
        try:
            import yt_dlp

            output_template = os.path.join(dest_dir, "%(title)s.%(ext)s")

            def progress_hook(d):
                status = d.get("status", "")
                if status == "downloading":
                    total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                    downloaded = d.get("downloaded_bytes", 0)
                    speed = d.get("speed") or 0
                    eta = d.get("eta") or 0
                    percent = (downloaded / total * 100) if total else 0
                    speed_str = f"{speed / (1024*1024):.1f} MiB/s" if speed else "計算中..."
                    eta_str = f"{eta} 秒" if eta else "計算中..."
                    q.put({
                        "type": "progress",
                        "percent": round(percent, 1),
                        "speed": speed_str,
                        "eta": eta_str,
                    })
                elif status == "finished":
                    q.put({
                        "type": "merging",
                        "message": "正在合併影片和音訊...",
                    })

            ydl_opts = {
                **YDL_BASE_OPTS,
                "format": format_id,
                "outtmpl": output_template,
                "progress_hooks": [progress_hook],
                "merge_output_format": "mp4",
            }

            if audio_only:
                ydl_opts["postprocessors"] = [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }]

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)
                if audio_only:
                    filename = os.path.splitext(filename)[0] + ".mp3"
                basename = os.path.basename(filename)

            result = {
                "type": "done",
                "filename": basename,
            }

            # In cloud mode, register temp file and provide download link
            if CLOUD_MODE:
                file_id = str(uuid.uuid4())
                filepath = os.path.join(dest_dir, basename)
                temp_files[file_id] = {"path": filepath, "created": time.time()}
                result["file_id"] = file_id

            q.put(result)

        except Exception as e:
            q.put({"type": "error", "message": str(e)})
        finally:
            # Signal end of stream
            q.put(None)

    thread = threading.Thread(target=do_download, daemon=True)
    thread.start()

    def generate():
        yield f"data: {json.dumps({'type': 'started', 'download_id': download_id})}\n\n"
        while True:
            try:
                msg = q.get(timeout=30)
                if msg is None:
                    break
                yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
            except Empty:
                # Send keepalive
                yield f"data: {json.dumps({'type': 'keepalive'})}\n\n"

        # Cleanup
        progress_queues.pop(download_id, None)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/file/<file_id>")
def serve_file(file_id):
    """Serve a temp file for browser download (cloud mode)."""
    info = temp_files.get(file_id)
    if info and os.path.exists(info["path"]):
        return send_file(info["path"], as_attachment=True)
    return jsonify({"error": "檔案不存在或已過期"}), 404


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))

    print("\n" + "=" * 50)
    print("  YT Downloader 已啟動！")
    if CLOUD_MODE:
        print("  模式：雲端")
    else:
        print("  模式：本地")
    print("=" * 50)
    print(f"\n  本機：http://localhost:{port}")

    if not CLOUD_MODE:
        # Try to show LAN IP
        try:
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            print(f"  iPad：http://{ip}:{port}")
        except Exception:
            print("  iPad：用電腦的區網 IP + :" + str(port))
        print(f"\n  下載位置：{DOWNLOAD_DIR}")

    print("=" * 50 + "\n")

    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
