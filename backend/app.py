"""
Reel Curator Backend
Downloads Instagram reels, extracts key frames based on scene changes,
and serves them for the swipe UI.
"""

import json
import re
import shutil
import subprocess
import threading
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

import cv2
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify, send_file, send_from_directory
from flask_cors import CORS

app = Flask(__name__, static_folder='../static')
CORS(app)

# Configuration
BASE_DIR = Path(__file__).parent.parent
DOWNLOADS_DIR = BASE_DIR / "downloads"
FRAMES_DIR = BASE_DIR / "frames"
LIKED_DIR = BASE_DIR / "liked"

# Ensure directories exist
for d in [DOWNLOADS_DIR, FRAMES_DIR, LIKED_DIR]:
    d.mkdir(exist_ok=True)

# Session storage (in production, use Redis or similar)
sessions = {}
sessions_lock = threading.Lock()

# Settings file for persistent storage
SETTINGS_FILE = BASE_DIR / "settings.json"

def load_settings():
    """Load settings from file."""
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}

def save_settings(settings):
    """Save settings to file."""
    with open(SETTINGS_FILE, 'w') as f:
        json.dump(settings, f)

# Browser for cookie extraction (None = no cookies, or 'chrome', 'firefox', 'edge', 'brave', 'opera', 'safari')
_settings = load_settings()
cookie_browser = _settings.get("browser")
cookie_browser_lock = threading.Lock()

def generate_session_id():
    """Generate a human-readable session ID based on timestamp."""
    return datetime.now().strftime("%b-%d_%H-%M-%S")


def sanitize_folder_name(name: str) -> str:
    """Sanitize a string to be safe for use as a folder name."""
    # Only remove characters that are invalid in Windows folder names: < > : " / \ | ? *
    sanitized = re.sub(r'[<>:"/\\|?*]', '', name)
    # Remove leading/trailing spaces and dots (Windows doesn't allow trailing dots/spaces)
    sanitized = sanitized.strip(' .')
    # Limit length
    sanitized = sanitized[:50]
    return sanitized or "Collection"


def get_unique_folder_name(base_name: str, exclude_path: Path = None) -> str:
    """Get a unique folder name in LIKED_DIR, appending number if needed."""
    sanitized = sanitize_folder_name(base_name)
    candidate = sanitized
    counter = 2

    while True:
        candidate_path = LIKED_DIR / candidate
        # If it doesn't exist, or it's the same as the path we're renaming from, it's valid
        if not candidate_path.exists() or (exclude_path and candidate_path == exclude_path):
            return candidate
        candidate = f"{sanitized}_{counter}"
        counter += 1


def detect_scene_changes(video_path: Path, threshold: float = 30.0) -> list[int]:
    """
    Detect frames where significant compositional changes occur.
    Uses histogram difference to detect scene cuts.
    """
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return []
    
    scene_frames = []
    prev_hist = None
    frame_idx = 0
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    
    # Always include first frame
    scene_frames.append(0)
    
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        
        # Convert to HSV and calculate histogram
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist([hsv], [0, 1], None, [50, 60], [0, 180, 0, 256])
        cv2.normalize(hist, hist, 0, 1, cv2.NORM_MINMAX)
        
        if prev_hist is not None:
            # Compare histograms
            diff = cv2.compareHist(prev_hist, hist, cv2.HISTCMP_CHISQR)
            
            # If difference exceeds threshold, it's a scene change
            if diff > threshold:
                # Avoid detecting changes too close together (min 0.5 sec apart)
                if not scene_frames or (frame_idx - scene_frames[-1]) > fps * 0.5:
                    scene_frames.append(frame_idx)
        
        prev_hist = hist
        frame_idx += 1
    
    cap.release()
    return scene_frames


def extract_frames(video_path: Path, frame_indices: list[int], output_dir: Path) -> list[str]:
    """Extract specific frames from video and save as images."""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return []
    
    extracted = []
    video_name = video_path.stem
    
    for idx in frame_indices:
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ret, frame = cap.read()
        if ret:
            filename = f"{video_name}_frame_{idx:06d}.jpg"
            filepath = output_dir / filename
            cv2.imwrite(str(filepath), frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
            extracted.append(filename)
    
    cap.release()
    return extracted


def download_with_gallery_dl(url: str, output_dir: Path, media_id: str, browser: str | None) -> tuple[list[Path], str | None]:
    """Try to download images using gallery-dl as fallback for carousel/image posts."""
    try:
        # Build gallery-dl command
        # Key: Use --directory "" to prevent subdirectory creation (flat structure)
        # Use {num} in filename to handle carousel items
        cmd = [
            "gallery-dl",
            "--dest", str(output_dir),
            "--directory", "",  # Flat directory - no subdirs
            "--filename", f"{media_id}_{{num}}.{{extension}}",
            "--no-mtime",  # Don't set file modification time
            "--no-part",   # Don't use .part files
        ]

        # Add cookies from browser if configured (REQUIRED for most Instagram content)
        if browser:
            cmd.extend(["--cookies-from-browser", browser])

        cmd.append(url)

        print(f"Running gallery-dl: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        print(f"gallery-dl stdout: {result.stdout}")
        print(f"gallery-dl stderr: {result.stderr}")
        print(f"gallery-dl return code: {result.returncode}")

        # Find downloaded files - check both direct and any subdirs gallery-dl might create
        downloaded_files = list(output_dir.glob(f"{media_id}_*"))

        # Also search recursively in case gallery-dl created subdirectories anyway
        if not downloaded_files:
            downloaded_files = list(output_dir.rglob(f"{media_id}_*"))

        if downloaded_files:
            # Move any files from subdirs to output_dir for consistency
            final_files = []
            for f in downloaded_files:
                if f.parent != output_dir:
                    new_path = output_dir / f.name
                    shutil.move(str(f), str(new_path))
                    final_files.append(new_path)
                else:
                    final_files.append(f)
            return final_files, None

        # No files found - check for errors
        error_msg = result.stderr or result.stdout or "gallery-dl failed"
        print(f"gallery-dl error: {error_msg}")

        # Check for common errors
        if "gallery-dl" in error_msg and "not found" in error_msg.lower():
            return [], "Image post - install gallery-dl: pip install gallery-dl"

        # Check for login required
        if "401" in error_msg or "login" in error_msg.lower() or "HttpError" in error_msg:
            if not browser:
                return [], "Login required - select your browser in Settings"
            else:
                return [], f"Login required - make sure you're logged into Instagram in {browser.title()}"

        # Check for rate limiting
        if "429" in error_msg or "rate" in error_msg.lower():
            return [], "Rate limited by Instagram - wait a few minutes and try again"

        return [], "Image/carousel download failed - check Settings for browser cookies"

    except FileNotFoundError:
        return [], "Image post - install gallery-dl: pip install gallery-dl"
    except subprocess.TimeoutExpired:
        return [], "Image download timed out"
    except Exception as e:
        print(f"gallery-dl error: {e}")
        return [], f"Image download error: {str(e)}"


def download_media(url: str, output_dir: Path) -> tuple[list[Path], str | None]:
    """Download Instagram media using yt-dlp. Returns (list of paths, error_message).

    Supports single videos, photos, and carousel posts with multiple items.
    For mixed carousels (videos + images), uses yt-dlp for videos and gallery-dl for images.
    """
    global cookie_browser

    try:
        # Generate unique prefix for this download
        media_id = str(uuid.uuid4())[:8]
        # Use %(autonumber)s to handle multiple files in carousel posts
        output_template = str(output_dir / f"{media_id}_%(autonumber)s.%(ext)s")

        # Add cookie extraction if browser is configured
        with cookie_browser_lock:
            browser = cookie_browser

        # First try: download video/media with yt-dlp
        # Use --ignore-no-formats-error to continue past image slides in carousels
        cmd = [
            "yt-dlp",
            "--no-warnings",
            "--ignore-no-formats-error",  # Don't fail on image-only slides
            "-o", output_template,
        ]

        if browser:
            cmd.extend(["--cookies-from-browser", browser])

        cmd.append(url)

        print(f"Running yt-dlp: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        print(f"yt-dlp stderr: {result.stderr}")

        # Check what we got from yt-dlp
        ytdlp_files = list(output_dir.glob(f"{media_id}_*"))
        has_image_errors = "No video formats found" in result.stderr

        # If yt-dlp completely failed with image error (no files), use gallery-dl
        if result.returncode != 0 and not ytdlp_files and has_image_errors:
            print(f"No video found, trying gallery-dl for images...")
            return download_with_gallery_dl(url, output_dir, media_id, browser)

        # If yt-dlp got some videos but also had image errors, supplement with gallery-dl
        # This handles mixed carousels (videos + images)
        if ytdlp_files and has_image_errors:
            print(f"Mixed carousel detected - got {len(ytdlp_files)} videos, trying gallery-dl for images...")
            gallery_files, _ = download_with_gallery_dl(url, output_dir, media_id + "img", browser)
            if gallery_files:
                # Filter out video files from gallery-dl (yt-dlp already got those)
                # Only keep images to avoid duplicate video processing
                image_extensions = {'.jpg', '.jpeg', '.png', '.webp', '.gif'}
                gallery_images = [f for f in gallery_files if f.suffix.lower() in image_extensions]
                # Delete the duplicate video files from gallery-dl
                for f in gallery_files:
                    if f.suffix.lower() not in image_extensions:
                        f.unlink(missing_ok=True)
                print(f"Filtered gallery-dl: kept {len(gallery_images)} images, removed {len(gallery_files) - len(gallery_images)} duplicate videos")
                ytdlp_files.extend(gallery_images)

        if result.returncode != 0 and not ytdlp_files:
            error_msg = result.stderr
            print(f"yt-dlp error: {error_msg}")

            # Check for cookie database lock error (Chrome/Edge while running)
            if "Could not copy" in error_msg and "cookie database" in error_msg:
                return [], f"Close {browser.title()} browser completely and try again"

            # Check for DPAPI decryption error (Windows + Chromium browsers)
            if "Failed to decrypt with DPAPI" in error_msg or "DPAPI" in error_msg:
                return [], f"Windows cannot decrypt {browser.title()} cookies - use Firefox instead"

            # Check if it's an authentication error
            if "Instagram sent an empty media response" in error_msg or "login" in error_msg.lower():
                if not browser:
                    return [], "Login required - select your browser in Settings"
                else:
                    return [], f"Login required - make sure you're logged into Instagram in {browser.title()}"

            # Check for invalid URL
            if "Unable to extract" in error_msg or "Unsupported URL" in error_msg:
                return [], "Invalid URL - make sure this is a direct link to a reel or post"

            # Check for post not found
            if "does not exist" in error_msg.lower() or "not found" in error_msg.lower():
                return [], "Post not found - it may have been deleted or is private"

            return [], "Download failed"

        # Return the files we found (ytdlp_files may include gallery-dl files too)
        if not ytdlp_files:
            return [], "Download completed but no files found"

        return ytdlp_files, None

    except subprocess.TimeoutExpired:
        return [], "Download timed out (try again later)"
    except Exception as e:
        print(f"Download error: {e}")
        return [], f"Error: {str(e)}"


def process_media_file(file_path: Path, session_dir: Path) -> list[str]:
    """Process a single media file - extract frames from videos, copy images directly."""
    extracted = []
    suffix = file_path.suffix.lower()

    # Image files - copy directly to session folder
    if suffix in ['.jpg', '.jpeg', '.png', '.webp']:
        # Generate unique filename
        new_name = f"{file_path.stem}_photo.jpg"
        dest_path = session_dir / new_name

        # Convert to JPG if needed (for consistency)
        img = cv2.imread(str(file_path))
        if img is not None:
            cv2.imwrite(str(dest_path), img, [cv2.IMWRITE_JPEG_QUALITY, 95])
            extracted.append(new_name)

    # Video files - extract keyframes
    elif suffix in ['.mp4', '.webm', '.mov', '.avi', '.mkv']:
        scene_frames = detect_scene_changes(file_path)
        video_frames = extract_frames(file_path, scene_frames, session_dir)
        extracted.extend(video_frames)

    return extracted


def process_urls(session_id: str, urls: list[str]):
    """Process multiple URLs in background."""
    # Clean up any orphaned frames from previous sessions before starting
    try:
        for old_session in FRAMES_DIR.iterdir():
            if old_session.is_dir():
                shutil.rmtree(old_session)
        print(f"Cleaned up old frames")
    except Exception as e:
        print(f"Error cleaning frames folder: {e}")

    session_dir = FRAMES_DIR / session_id
    session_dir.mkdir(exist_ok=True)

    all_frames = []
    processed = 0
    errors = []

    # Filter out empty URLs and track original indices
    url_list = [(i + 1, url.strip()) for i, url in enumerate(urls) if url.strip()]

    with sessions_lock:
        sessions[session_id] = {
            "status": "processing",
            "total": len(url_list),
            "processed": 0,
            "frames": [],
            "errors": [],
            "cancelled": False
        }

    for url_num, url in url_list:
        # Check if cancelled
        with sessions_lock:
            if sessions.get(session_id, {}).get("cancelled"):
                sessions[session_id]["status"] = "cancelled"
                return

        try:
            # Download all media from the URL (supports carousel posts)
            media_files, error_msg = download_media(url, DOWNLOADS_DIR)

            if media_files:
                # Process each downloaded file (videos and images)
                for media_file in media_files:
                    try:
                        extracted = process_media_file(media_file, session_dir)
                        all_frames.extend(extracted)
                    finally:
                        # Clean up downloaded file
                        media_file.unlink(missing_ok=True)
            else:
                # Format error with URL number for easy identification
                errors.append(f"Link #{url_num}: {error_msg}")

        except Exception as e:
            errors.append(f"Link #{url_num}: Unexpected error - {str(e)}")

        processed += 1
        with sessions_lock:
            sessions[session_id]["processed"] = processed
            sessions[session_id]["frames"] = all_frames
            sessions[session_id]["errors"] = errors

    # Clean up any remaining files in downloads folder
    try:
        for f in DOWNLOADS_DIR.iterdir():
            if f.is_file():
                f.unlink(missing_ok=True)
        print(f"Cleaned up downloads folder")
    except Exception as e:
        print(f"Error cleaning downloads folder: {e}")

    with sessions_lock:
        sessions[session_id]["status"] = "complete"


@app.route("/api/process", methods=["POST"])
def start_processing():
    """Start processing a list of reel URLs."""
    data = request.json
    urls = data.get("urls", [])

    if not urls:
        return jsonify({"error": "No URLs provided"}), 400

    # Create session with readable timestamp-based ID
    session_id = generate_session_id()

    # Start background processing
    executor = ThreadPoolExecutor(max_workers=1)
    executor.submit(process_urls, session_id, urls)

    return jsonify({"session_id": session_id})


@app.route("/api/status/<session_id>")
def get_status(session_id: str):
    """Get processing status for a session."""
    with sessions_lock:
        session = sessions.get(session_id)

    if not session:
        return jsonify({"error": "Session not found"}), 404

    return jsonify(session)


@app.route("/api/cancel/<session_id>", methods=["POST"])
def cancel_processing(session_id: str):
    """Cancel an in-progress processing session."""
    with sessions_lock:
        session = sessions.get(session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404

        if session["status"] != "processing":
            return jsonify({"error": "Session is not processing"}), 400

        session["cancelled"] = True

    return jsonify({"success": True})


@app.route("/api/frames/<session_id>/<filename>")
def get_frame(session_id: str, filename: str):
    """Serve a frame image."""
    session_dir = FRAMES_DIR / session_id
    return send_from_directory(session_dir, filename)


@app.route("/api/like", methods=["POST"])
def like_frame():
    """Mark a frame as liked (save to liked folder)."""
    data = request.json
    session_id = data.get("session_id")
    filename = data.get("filename")
    
    if not session_id or not filename:
        return jsonify({"error": "Missing session_id or filename"}), 400
    
    src = FRAMES_DIR / session_id / filename
    
    # Create session-specific liked folder
    liked_session_dir = LIKED_DIR / session_id
    liked_session_dir.mkdir(exist_ok=True)
    
    dst = liked_session_dir / filename
    
    if src.exists():
        shutil.copy2(src, dst)
        return jsonify({"success": True})
    
    return jsonify({"error": "Frame not found"}), 404


@app.route("/api/export/<path:session_id>")
def export_liked(session_id: str):
    """Export liked frames as a zip file."""
    liked_session_dir = LIKED_DIR / session_id

    if not liked_session_dir.exists():
        return jsonify({"error": "No liked frames"}), 404

    files = list(liked_session_dir.glob("*.jpg"))
    if not files:
        return jsonify({"error": "No liked frames"}), 404

    # Create zip file - use sanitized name for the zip
    zip_path = LIKED_DIR / f"{session_id}_export.zip"

    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, f.name)

    # Use the session name for download filename
    download_name = f"{session_id}.zip"

    return send_file(
        zip_path,
        mimetype='application/zip',
        as_attachment=True,
        download_name=download_name
    )


@app.route("/api/cleanup/<path:session_id>", methods=["POST"])
def cleanup_session(session_id: str):
    """Clean up session data."""
    session_dir = FRAMES_DIR / session_id
    liked_session_dir = LIKED_DIR / session_id

    if session_dir.exists():
        shutil.rmtree(session_dir)

    with sessions_lock:
        sessions.pop(session_id, None)

    return jsonify({"success": True})


@app.route("/api/sessions")
def list_sessions():
    """List all saved sessions with liked photos."""
    saved_sessions = []

    if LIKED_DIR.exists():
        for session_dir in LIKED_DIR.iterdir():
            if session_dir.is_dir():
                photos = list(session_dir.glob("*.jpg"))
                if photos:
                    # Get creation time from oldest photo
                    oldest_photo = min(photos, key=lambda p: p.stat().st_ctime)
                    created_at = oldest_photo.stat().st_ctime

                    # Folder name IS the display name
                    saved_sessions.append({
                        "session_id": session_dir.name,
                        "photo_count": len(photos),
                        "created_at": created_at,
                        "preview": photos[0].name if photos else None,
                        "name": session_dir.name  # Folder name is the name
                    })

    # Sort by creation time, newest first
    saved_sessions.sort(key=lambda s: s["created_at"], reverse=True)

    return jsonify({"sessions": saved_sessions})


@app.route("/api/sessions/<path:session_id>/rename", methods=["POST"])
def rename_session(session_id: str):
    """Rename a saved session by renaming its folder."""
    liked_session_dir = LIKED_DIR / session_id

    if not liked_session_dir.exists():
        return jsonify({"error": "Session not found"}), 404

    data = request.json
    new_name = data.get("name", "").strip()

    if not new_name:
        return jsonify({"error": "Name cannot be empty"}), 400

    # Get unique folder name (handles duplicates)
    new_folder_name = get_unique_folder_name(new_name, exclude_path=liked_session_dir)
    new_path = LIKED_DIR / new_folder_name

    # If it's actually a different name, rename the folder
    if new_path != liked_session_dir:
        try:
            liked_session_dir.rename(new_path)
            # Also rename export zip if it exists
            old_zip = LIKED_DIR / f"{session_id}_export.zip"
            if old_zip.exists():
                old_zip.rename(LIKED_DIR / f"{new_folder_name}_export.zip")
        except Exception as e:
            return jsonify({"error": f"Failed to rename: {str(e)}"}), 500

    return jsonify({
        "success": True,
        "name": new_folder_name,
        "new_session_id": new_folder_name
    })


@app.route("/api/sessions/<path:session_id>/photos")
def get_session_photos(session_id: str):
    """Get list of photos in a saved session."""
    liked_session_dir = LIKED_DIR / session_id

    if not liked_session_dir.exists():
        return jsonify({"error": "Session not found"}), 404

    photos = [f.name for f in liked_session_dir.glob("*.jpg")]
    return jsonify({"photos": photos})


@app.route("/api/sessions/<path:session_id>/photo/<filename>")
def get_session_photo(session_id: str, filename: str):
    """Serve a photo from a saved session."""
    liked_session_dir = LIKED_DIR / session_id
    return send_from_directory(liked_session_dir, filename)


@app.route("/api/sessions/<path:session_id>/upload", methods=["POST"])
def upload_edited_photo(session_id: str):
    """Upload an edited photo to a session."""
    liked_session_dir = LIKED_DIR / session_id

    if not liked_session_dir.exists():
        return jsonify({"error": "Session not found"}), 404

    if 'image' not in request.files:
        return jsonify({"error": "No image provided"}), 400

    image = request.files['image']

    # Generate unique filename
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"edited_{timestamp}.jpg"
    filepath = liked_session_dir / filename

    # Save the image
    image.save(filepath)

    return jsonify({"success": True, "filename": filename})


@app.route("/api/sessions/<path:session_id>", methods=["DELETE"])
def delete_session(session_id: str):
    """Delete a saved session."""
    liked_session_dir = LIKED_DIR / session_id
    zip_path = LIKED_DIR / f"{session_id}_export.zip"

    if liked_session_dir.exists():
        shutil.rmtree(liked_session_dir)
    if zip_path.exists():
        zip_path.unlink()

    return jsonify({"success": True})


@app.route("/api/sessions/<path:session_id>/photo/<filename>", methods=["DELETE"])
def delete_photo(session_id: str, filename: str):
    """Delete a single photo from a saved session."""
    liked_session_dir = LIKED_DIR / session_id
    photo_path = liked_session_dir / filename

    if not photo_path.exists():
        return jsonify({"error": "Photo not found"}), 404

    photo_path.unlink()

    # Check if session is now empty and clean up if so
    remaining = list(liked_session_dir.glob("*.jpg"))
    if not remaining:
        liked_session_dir.rmdir()
        # Also remove any export zip
        zip_path = LIKED_DIR / f"{session_id}_export.zip"
        if zip_path.exists():
            zip_path.unlink()

    return jsonify({"success": True, "remaining": len(remaining)})


@app.route("/api/settings/browser", methods=["GET"])
def get_browser_setting():
    """Get the current browser setting for cookie extraction."""
    with cookie_browser_lock:
        return jsonify({"browser": cookie_browser})


@app.route("/api/settings/browser", methods=["POST"])
def set_browser_setting():
    """Set the browser for cookie extraction."""
    global cookie_browser

    data = request.json
    browser = data.get("browser")

    # Validate browser choice
    valid_browsers = [None, "chrome", "firefox", "edge", "brave", "opera", "safari", "chromium"]
    if browser not in valid_browsers:
        return jsonify({"error": f"Invalid browser. Choose from: {valid_browsers}"}), 400

    with cookie_browser_lock:
        cookie_browser = browser
        # Persist to file
        settings = load_settings()
        settings["browser"] = browser
        save_settings(settings)

    return jsonify({"success": True, "browser": browser})


# Serve frontend
@app.route("/")
def serve_frontend():
    return send_from_directory("../static", "index.html")


@app.route("/<path:path>")
def serve_static(path):
    return send_from_directory("../static", path)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
