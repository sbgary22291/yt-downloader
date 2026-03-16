import os
import json
import shutil
import uuid
import threading
from queue import Queue, Empty
from pathlib import Path

from flask import Flask, request, jsonify, render_template, Response, send_file

app = Flask(__name__)

# Ensure ffmpeg is in PATH (Windows)
_ffmpeg_dir = os.path.expanduser(
    "~/AppData/Local/Microsoft/WinGet/Packages/"
    "Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe/"
    "ffmpeg-8.0.1-full_build/bin"
)
if os.path.isdir(_ffmpeg_dir) and _ffmpeg_dir not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")

DOWNLOAD_DIR = str(Path.home() / "Downloads")

# Store active download progress queues
progress_queues = {}

# yt-dlp options
YDL_BASE_OPTS = {
    "quiet": True,
    "no_warnings": True,
}


@app.route("/")
def index():
    return render_template("index.html")


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

        # Group video formats by resolution
        video_map = {}
        for f in formats:
            h = f.get("height")
            vcodec = f.get("vcodec", "none")
            if not h or vcodec == "none":
                continue
            ext = f.get("ext", "")
            if ext not in ("mp4", "webm"):
                continue
            filesize = f.get("filesize") or f.get("filesize_approx") or 0
            if h not in video_map or filesize > (video_map[h].get("filesize") or 0):
                video_map[h] = {
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

        # Build quality options
        quality_options = []
        for h in sorted(video_map.keys(), reverse=True):
            v = video_map[h]
            fmt_id = v["format_id"]
            if best_audio:
                fmt_id = f"{v['format_id']}+{best_audio['format_id']}"
            size_mb = round(v["filesize"] / (1024 * 1024), 1) if v["filesize"] else None
            label = f"{h}p"
            if v.get("fps") and v["fps"] > 30:
                label = f"{h}p{v['fps']}"
            quality_options.append({
                "format_id": fmt_id,
                "label": label,
                "size_mb": size_mb,
            })

        if best_audio:
            quality_options.append({
                "format_id": best_audio["format_id"],
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

    def do_download():
        try:
            import yt_dlp

            output_template = os.path.join(DOWNLOAD_DIR, "%(title)s.%(ext)s")

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

            q.put({"type": "done", "filename": basename})

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
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    port = 5000

    print("\n" + "=" * 50)
    print("  YT Downloader Started!")
    print("=" * 50)
    print(f"\n  http://localhost:{port}")

    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        print(f"  iPad: http://{ip}:{port}")
    except Exception:
        pass

    print(f"\n  Downloads: {DOWNLOAD_DIR}")
    print("=" * 50 + "\n")

    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
