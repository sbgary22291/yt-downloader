import os
import re
import json
import time
import shutil
import uuid
import threading
import requests as http_requests
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

# Track temp files for cleanup
temp_files = {}
TEMP_FILE_TTL = 30 * 60  # 30 minutes

# Invidious API - dynamically fetch working instances
_invidious_cache = {"instances": [], "updated": 0}


def get_invidious_instances():
    """Fetch list of working Invidious API instances, cached for 1 hour."""
    now = time.time()
    if _invidious_cache["instances"] and now - _invidious_cache["updated"] < 3600:
        return _invidious_cache["instances"]

    try:
        r = http_requests.get("https://api.invidious.io/instances.json", timeout=10)
        r.raise_for_status()
        instances = []
        for item in r.json():
            info = item[1] if isinstance(item, list) else item
            if (info.get("type") == "https" and
                    info.get("api") is not False and
                    info.get("uri")):
                instances.append(info["uri"])
        if instances:
            _invidious_cache["instances"] = instances
            _invidious_cache["updated"] = now
        return instances
    except Exception:
        pass

    # Hardcoded fallbacks
    return [
        "https://inv.nadeko.net",
        "https://invidious.fdn.fr",
        "https://invidious.nerdvpn.de",
        "https://vid.puffyan.us",
    ]

# yt-dlp options (for local mode only)
YDL_BASE_OPTS = {
    "quiet": True,
    "no_warnings": True,
}
COOKIES_FILE = os.path.join(os.path.dirname(__file__), "cookies.txt")
if os.path.exists(COOKIES_FILE):
    YDL_BASE_OPTS["cookiefile"] = COOKIES_FILE


def cleanup_temp_files():
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


cleanup_thread = threading.Thread(target=cleanup_temp_files, daemon=True)
cleanup_thread.start()


@app.route("/")
def index():
    return render_template("index.html", cloud_mode=CLOUD_MODE)


@app.route("/api/debug")
def debug_info():
    instances = get_invidious_instances()
    # Test first 3 instances
    results = []
    test_id = "dQw4w9WgXcQ"
    for api in instances[:3]:
        try:
            r = http_requests.get(f"{api}/api/v1/videos/{test_id}",
                                  timeout=10,
                                  params={"fields": "title"})
            results.append({
                "instance": api,
                "status": r.status_code,
                "title": r.json().get("title", "") if r.ok else r.text[:200],
            })
        except Exception as e:
            results.append({"instance": api, "error": str(e)[:200]})
    return jsonify({
        "total_instances": len(instances),
        "first_5": instances[:5],
        "test_results": results,
    })


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

    if CLOUD_MODE:
        return _get_info_cloud(url)
    else:
        return _get_info_local(url)


def _get_info_cloud(url):
    """Use Invidious API for video info (free, no auth)."""
    try:
        video_id = ""
        m = re.search(r'(?:v=|youtu\.be/|/shorts/)([a-zA-Z0-9_-]{11})', url)
        if m:
            video_id = m.group(1)
        if not video_id:
            return jsonify({"error": "無法辨識 YouTube 網址"}), 400

        instances = get_invidious_instances()
        data = None
        last_error = ""
        for api in instances[:10]:  # Try up to 10 instances
            try:
                r = http_requests.get(
                    f"{api}/api/v1/videos/{video_id}",
                    timeout=10,
                    params={"fields": "title,author,lengthSeconds,videoThumbnails,adaptiveFormats"},
                )
                r.raise_for_status()
                data = r.json()
                if data.get("title"):
                    break
                data = None
            except Exception as e:
                last_error = str(e)
                continue

        if not data:
            return jsonify({"error": f"無法取得影片資訊：{last_error}"}), 400

        # Collect video qualities
        seen_heights = set()
        quality_options = []
        for f in data.get("adaptiveFormats", []):
            ftype = f.get("type", "")
            h = f.get("resolution", "").replace("p", "")
            dl_url = f.get("url", "")
            if "video" in ftype and h.isdigit() and dl_url:
                h_int = int(h)
                if h_int not in seen_heights:
                    seen_heights.add(h_int)
                    quality_options.append({
                        "format_id": dl_url,
                        "label": f"{h_int}p",
                        "size_mb": None,
                        "height": h_int,
                    })

        quality_options.sort(key=lambda x: x.get("height", 0), reverse=True)
        for q in quality_options:
            q.pop("height", None)

        # Audio option
        for f in data.get("adaptiveFormats", []):
            ftype = f.get("type", "")
            dl_url = f.get("url", "")
            if "audio" in ftype and dl_url:
                quality_options.append({
                    "format_id": dl_url,
                    "label": "僅音訊",
                    "size_mb": None,
                    "audio_only": True,
                })
                break

        if not quality_options:
            quality_options.append({
                "format_id": "best",
                "label": "最佳畫質",
                "size_mb": None,
            })

        duration = data.get("lengthSeconds", 0)
        minutes = int(duration // 60)
        seconds = int(duration % 60)

        thumbnails = data.get("videoThumbnails", [])
        thumb_url = thumbnails[0]["url"] if thumbnails else ""

        result = {
            "title": data.get("title", "未知"),
            "thumbnail": thumb_url,
            "duration": f"{minutes}:{seconds:02d}",
            "duration_seconds": duration,
            "channel": data.get("author", ""),
            "qualities": quality_options,
        }
        return jsonify(result)

    except Exception as e:
        return jsonify({"error": f"無法取得影片資訊：{str(e)}"}), 400


def _get_info_local(url):
    """Use yt-dlp for local mode."""
    try:
        import yt_dlp

        ydl_opts = {**YDL_BASE_OPTS}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        formats = info.get("formats", [])
        available_heights = set()
        for f in formats:
            h = f.get("height")
            if h and f.get("vcodec", "none") != "none":
                available_heights.add(h)

        has_audio = any(f.get("acodec", "none") != "none" for f in formats)

        quality_options = []
        for h in sorted(available_heights, reverse=True):
            quality_options.append({
                "format_id": f"bv[height<={h}]+ba/b[height<={h}]/b",
                "label": f"{h}p",
                "size_mb": None,
            })

        if has_audio:
            quality_options.append({
                "format_id": "ba/b",
                "label": "僅音訊 (MP3)",
                "size_mb": None,
                "audio_only": True,
            })

        if not quality_options:
            quality_options.append({
                "format_id": "b",
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

    if CLOUD_MODE:
        return _download_cloud(url, format_id, audio_only)
    else:
        return _download_local(url, format_id, audio_only)


def _download_cloud(url, format_id, audio_only):
    """Piped API already gives us direct stream URLs in format_id."""
    try:
        # format_id is already the direct stream URL from Piped
        if not format_id or not format_id.startswith("http"):
            return jsonify({"error": "無效的下載連結"}), 400

        filename = "video.mp4" if not audio_only else "audio.mp3"

        return jsonify({
            "status": "redirect",
            "download_url": format_id,
            "filename": filename,
        })

    except Exception as e:
        return jsonify({"error": f"下載失敗：{str(e)}"}), 400


def _download_local(url, format_id, audio_only):
    """Use yt-dlp for local downloads (original logic)."""
    download_id = str(uuid.uuid4())
    q = Queue()
    progress_queues[download_id] = q

    dest_dir = DOWNLOAD_DIR

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

            q.put({
                "type": "done",
                "filename": basename,
            })

        except Exception as e:
            q.put({"type": "error", "message": str(e)})
        finally:
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
                yield f"data: {json.dumps({'type': 'keepalive'})}\n\n"

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
    info = temp_files.get(file_id)
    if info and os.path.exists(info["path"]):
        return send_file(info["path"], as_attachment=True)
    return jsonify({"error": "檔案不存在或已過期"}), 404


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))

    print("\n" + "=" * 50)
    print("  YT Downloader 已啟動！")
    if CLOUD_MODE:
        print("  模式：雲端（Cobalt API）")
    else:
        print("  模式：本地（yt-dlp）")
    print("=" * 50)
    print(f"\n  本機：http://localhost:{port}")

    if not CLOUD_MODE:
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
