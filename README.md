# voice_paste 🎙

Hold **Ctrl+Space** to record your voice. Release to transcribe and paste the text wherever your cursor is — any app, any surface.

Uses OpenAI's `gpt-4o-transcribe` model. Menu bar icon shows 🎙 at rest and 🔴 while recording.

---

## Step 0: Get an OpenAI API key

voice_paste uses OpenAI's Whisper API to transcribe your audio. You'll need an API key before setup.

1. Go to [platform.openai.com](https://platform.openai.com) and sign in (or create a free account)
2. Click your profile icon (top-right) → **Your profile** → **[API keys](https://platform.openai.com/api-keys)** in the left sidebar
3. Click **+ Create new secret key**, give it a name (e.g. `voice_paste`), and click **Create**
4. **Copy the key immediately** — OpenAI only shows it once
5. You'll paste it when `setup.sh` asks for your API key

> **Billing note:** The API is pay-as-you-go. Add a payment method at **Settings → Billing** and set a monthly spending limit. Typical usage costs a few cents per day.

---

## Requirements

- macOS 12+
- Python 3.9+
- [Homebrew](https://brew.sh)
- An [OpenAI API key](https://platform.openai.com/api-keys)

---

## Setup

```bash
cd voice_paste
bash setup.sh
```

`setup.sh` will:
1. Install PortAudio (via Homebrew) and all Python packages
2. Save your OpenAI API key to a local `.env` file
3. Walk you through two required macOS permission steps (see below)

---

## macOS permissions (required — setup.sh will guide you)

**1. Free up Ctrl+Space**

macOS reserves Ctrl+Space for input source switching. Disable it at:
`System Settings → Keyboard → Keyboard Shortcuts → Input Sources → uncheck "Select the previous input source"`

**2. Grant Accessibility access**

`pynput` needs Accessibility permission to detect global keypresses. You need to add **two entries**:

- Your terminal app (Terminal, iTerm2, Warp, etc.)
- The Python binary itself — find it with `which python3`, then add that path

Both at: `System Settings → Privacy & Security → Accessibility → + `

---

## Running

```bash
# Start in background (survives closing Terminal)
bash start.sh

# Or run in foreground to see live logs
python3 voice_paste.py
```

To stop:
```bash
kill $(cat voice_paste.pid)
```

---

## Usage

1. Click into any text field (Slack, Notion, browser, email — anywhere)
2. Hold **Ctrl+Space** — menu bar icon turns 🔴
3. Speak
4. Release **Ctrl+Space** — text is transcribed and pasted at your cursor

---

## Configuration

Open `voice_paste.py` and edit the config block near the top:

```python
MODEL    = "gpt-4o-transcribe"  # or "whisper-1" for the older, cheaper model
LANGUAGE = "en"                 # set to None for automatic language detection
```

---

## Troubleshooting

**Ctrl+Space does nothing**
- Check Accessibility permission is granted to both your terminal app and the Python binary (`which python3`)
- Make sure the macOS Ctrl+Space shortcut is disabled (see above)
- Run `python3 voice_paste.py` in the foreground and check for errors

**Wrong API key / 401 error**
- Your key lives in `voice_paste/.env` — edit it directly: `nano .env`
- Make sure `OPENAI_API_KEY` is not also set in `~/.zshrc` (that will override `.env`)
- Check for conflicts: `grep OPENAI_API_KEY ~/.zshrc ~/.bash_profile ~/.bashrc`

**Transcription pastes in the wrong place**
- Make sure focus stays in your target field before pressing Ctrl+Space
- Don't click away while recording

**App crashes on longer recordings**
- Make sure you're on the latest `voice_paste.py` — earlier versions updated the menu bar icon from a background thread, which macOS doesn't allow

---

## API costs

| Model | Price |
|---|---|
| `gpt-4o-transcribe` | ~$0.006 / min |
| `whisper-1` | ~$0.006 / min |

Typical voice note (15 sec) costs roughly $0.0015. Negligible for daily use.

---

## Files

```
voice_paste/
├── voice_paste.py   # main script
├── setup.sh         # one-time setup
├── start.sh         # background launcher
├── requirements.txt # Python dependencies
├── .env             # your API key (never commit this)
└── README.md
```

---

## Auto-start on login (optional)

Add to your `~/.zshrc`:
```bash
bash ~/path/to/voice_paste/start.sh
```
