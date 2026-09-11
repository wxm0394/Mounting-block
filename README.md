# ReadLevel: Adaptive Ruby Annotator

ReadLevel is a reading assistance tool that automatically detects difficult English words and phrases and provides inline translations using `<ruby>` tags. The translations are intelligently filtered based on a dynamic reading level slider.

## Prerequisites & Setup

Follow these steps to set up the project locally:

### 1. Python Environment Setup (Virtual Environment)
In Debian/Kali and modern Linux distributions, global package installation is blocked (`externally-managed-environment` / PEP 668). **Always use the project's virtual environment**:

If `venv` is already initialized in the repository, you can skip to Step 3. Otherwise, create and set it up:
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python -m spacy download en_core_web_sm
```

### 2. Set Environment Variables
The application relies on the Gemini API to fetch contextual translations:
```bash
export GEMINI_API_KEY="your-api-key-here"
# Optional (default is gemini-2.5-flash-lite)
export GEMINI_MODEL="gemini-2.5-flash-lite"
```

## Running the Application

You will need two separate terminal windows: one for the backend and one for the frontend.

### Start the Backend

**Option A (Fastest, no activation required):**
```bash
export GEMINI_API_KEY="your-api-key-here"
./venv/bin/uvicorn backend:app --reload --port 8000
```

**Option B (Standard via virtualenv activation):**
```bash
source venv/bin/activate
export GEMINI_API_KEY="your-api-key-here"
uvicorn backend:app --reload --port 8000
```
*(The backend runs on `http://localhost:8000`)*

> **Tip: If you see `[Errno 98] Address already in use`**:
> The 8000 port is still occupied by a previous process. Kill it with:
> ```bash
> fuser -k 8000/tcp
> # or: lsof -ti :8000 | xargs kill -9
> ```

### Start the Frontend

In a new terminal window, start the Vite development server (using polling mode to avoid file limit errors):
```bash
CHOKIDAR_USEPOLLING=true npm run dev
```
*(The frontend runs on `http://localhost:5173`)*

Open your browser and navigate to the frontend URL to use ReadLevel.
