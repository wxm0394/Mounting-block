import { useState, useEffect } from 'react';
import { annotateText, MOCK_DICTIONARY } from './lib/annotator';
import './index.css';

const DEFAULT_TEXT = `Remember how you learned to make money as a kid?
There was baby-sitting and delivering newspapers. Shoveling snow off the neighbors' sidewalk and driveway. Mowing lawns and taking care of other people's pets and plants when they went on vacation.
There is one business which, chances are, almost every kid tries at least once in his or her life. A tried and true operation as American as baseball and Mom's apple pie.
The Lemonade Stand.
It's this world of childhood, of lemonade stands and sunny days that the author describes in this inspiring book.`;

function App() {
  const [text, setText] = useState(DEFAULT_TEXT);
  const [displayLevel, setDisplayLevel] = useState<number>(700);
  const [dictionary, setDictionary] = useState<Record<string, any>>(MOCK_DICTIONARY);
  const [isAnalyzing, setIsAnalyzing] = useState(false);

  // Fetch full dictionary when text changes (NOT when slider moves)
  useEffect(() => {
    if (!text.trim()) return;

    const abortController = new AbortController();
    const timer = setTimeout(async () => {
      setIsAnalyzing(true);
      try {
        const response = await fetch('http://localhost:8000/annotate', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text, target_lang: 'zh-Hans' }),
          signal: abortController.signal,
        });
        if (response.ok) {
          const data = await response.json();
          if (data.dictionary && Object.keys(data.dictionary).length > 0) {
            setDictionary(data.dictionary);
          }
        }
      } catch (err: any) {
        if (err.name !== 'AbortError') {
          console.error("Backend unreachable:", err);
        }
      } finally {
        setIsAnalyzing(false);
      }
    }, 600);

    return () => {
      clearTimeout(timer);
      abortController.abort();
    };
  }, [text]);

  return (
    <div className="app-container">

      <header className="app-header">
        <div>
          <h1>ReadLevel</h1>
          <p className="subtitle">Adaptive Ruby Annotator — words & phrases above your level get native translation</p>
        </div>
        {isAnalyzing && (
          <div className="status-pill">
            <span className="status-dot"></span>
            AI 正在解析...
          </div>
        )}
      </header>

      <section className="main-layout">

        {/* Left: Controls */}
        <div className="controls-panel">
          <div className="level-control">
            <div className="level-header">
              <label>Your Reading Level:</label>
              <span className="level-value">{displayLevel}</span>
            </div>
            <input
              type="range"
              min="500"
              max="1500"
              step="50"
              value={displayLevel}
              onChange={(e) => setDisplayLevel(Number(e.target.value))}
              className="level-slider"
            />
            <div className="level-labels">
              <span>Beginner 500</span>
              <span>Intermediate 1000</span>
              <span>Advanced 1500</span>
            </div>
          </div>

          <div className="text-input-wrapper">
            <label>Paste English Text:</label>
            <textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              className="text-input"
              placeholder="Paste any English paragraph here..."
            />
          </div>
        </div>

        {/* Right: Reading View */}
        <div className="reading-panel">
          <label>Reading View:</label>
          <div className="reader-container">
            {annotateText(text, displayLevel, dictionary)}
          </div>
        </div>

      </section>

    </div>
  );
}

export default App;
