# Reel Curator

Extract and curate key frames from Instagram reels with a Tinder-style swipe interface.

## Features

- **Batch Processing**: Paste multiple Instagram reel URLs, one per line
- **Smart Frame Extraction**: Automatically detects scene changes/compositional shifts (not just timed intervals)
- **Swipe UI**: Swipe right (or press →) to keep, swipe left (or press ←) to skip
- **Export**: Download all liked frames as a ZIP file

## Quick Start

### Prerequisites

```bash
# Python 3.10+
pip install flask flask-cors yt-dlp opencv-python-headless numpy
```

### Run

```bash
./run.sh
# or
python backend/app.py
```

Open **http://localhost:5000** in your browser.

## How It Works

1. **Download**: Uses `yt-dlp` to download Instagram reels
2. **Scene Detection**: Analyzes video with HSV histogram comparison to detect when the visual composition changes significantly (works great for photo slideshows in reels)
3. **Frame Extraction**: Pulls out the first frame after each detected scene change
4. **Review**: Present frames one-by-one in a swipe interface
5. **Export**: Package liked frames into a downloadable ZIP

## Project Structure

```
reel-curator/
├── backend/
│   └── app.py          # Flask API + scene detection logic
├── static/
│   └── index.html      # React frontend (single file)
├── downloads/          # Temporary video storage (auto-cleaned)
├── frames/             # Extracted frames per session
├── liked/              # Saved frames per session
├── requirements.txt
├── run.sh
└── README.md
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/process` | POST | Start processing URLs `{ "urls": [...] }` |
| `/api/status/<session_id>` | GET | Get processing status |
| `/api/frames/<session_id>/<filename>` | GET | Serve frame image |
| `/api/like` | POST | Mark frame as liked `{ "session_id", "filename" }` |
| `/api/export/<session_id>` | GET | Download liked frames as ZIP |
| `/api/cleanup/<session_id>` | POST | Clean up session data |

## Tuning Scene Detection

In `backend/app.py`, you can adjust the `threshold` parameter in `detect_scene_changes()`:

- **Lower threshold** (e.g., 15-20): More sensitive, detects subtle changes
- **Higher threshold** (e.g., 40-50): Less sensitive, only detects major scene cuts

Default is `30.0` which works well for typical photo slideshow reels.

## Notes

- Instagram may rate-limit or block downloads - use responsibly
- Videos are deleted after frame extraction to save space
- Sessions are stored in memory (restart clears them)

## License

MIT
