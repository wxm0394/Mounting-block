# ReadLevel: Adaptive Ruby Annotator

ReadLevel is a reading assistance tool that automatically detects difficult English words and phrases and provides inline translations using `<ruby>` tags. The translations are intelligently filtered based on a dynamic reading level slider.

## Prerequisites & Setup

Follow these steps to set up the project locally:

### 1. Install Backend Dependencies
Make sure you have Python installed, then install the required packages:
```bash
pip install -r requirements.txt
```

### 2. Download spaCy Model
The backend uses spaCy for linguistic analysis. You need to download the English model:
```bash
python -m spacy download en_core_web_sm
```

### 3. Set Gemini API Key
The application relies on the Gemini API to fetch contextual translations. Export your API key in your terminal before running the backend:
```bash
export GEMINI_API_KEY="your-api-key-here"
```

### 4. Optional: Set Gemini Model
By default, the application uses `gemini-2.5-flash-lite`. You can switch to a different model (e.g., `gemini-2.5-flash`) by setting the `GEMINI_MODEL` environment variable. Note that in the future, if the 2.5 series is deprecated, you only need to change this environment variable without modifying any code.
```bash
export GEMINI_MODEL="gemini-2.5-flash-lite"
```

## Running the Application

You will need two separate terminal windows, one for the backend and one for the frontend.

### Start the Backend
In the root directory of this project (where `backend.py` is located), start the FastAPI server:
```bash
uvicorn backend:app --reload
```
*(The backend runs on `http://localhost:8000`)*

### Start the Frontend
In a new terminal window, start the Vite development server:
```bash
npm run dev
```
*(The frontend runs on `http://localhost:5173`)*

Open your browser and navigate to the frontend URL to use ReadLevel.
