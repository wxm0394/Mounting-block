import { useState, useEffect, useMemo } from 'react';
import { annotateText, MOCK_DICTIONARY } from './lib/annotator';
import './index.css';

const DEFAULT_TEXT = `Remember how you learned to make money as a kid?
There was baby-sitting and delivering newspapers. Shoveling snow off the neighbors' sidewalk and driveway. Mowing lawns and taking care of other people's pets and plants when they went on vacation.
There is one business which, chances are, almost every kid tries at least once in his or her life. A tried and true operation as American as baseball and Mom's apple pie.
The Lemonade Stand.
It's this world of childhood, of lemonade stands and sunny days that the author describes in this inspiring book.`;

function paginateText(text: string, charsPerPage = 1200): string[] {
  const paragraphs = text.split(/\n+/).filter(p => p.trim().length > 0);
  const pages: string[] = [];
  let currentPage = "";
  
  for (const p of paragraphs) {
    if (p.length > charsPerPage) {
      // Force split by sentence boundaries if the paragraph is huge
      const sentences = p.match(/[^.!?]+[.!?]+/g) || [p];
      for (const s of sentences) {
        if (currentPage.length + s.length > charsPerPage && currentPage.length > 0) {
          pages.push(currentPage);
          currentPage = s + " ";
        } else {
          currentPage += s + " ";
        }
      }
      currentPage += "\n\n";
    } else {
      if (currentPage.length + p.length > charsPerPage && currentPage.length > 0) {
        pages.push(currentPage);
        currentPage = p + "\n\n";
      } else {
        currentPage += p + "\n\n";
      }
    }
  }
  if (currentPage.trim().length > 0) {
    pages.push(currentPage);
  }
  return pages.length > 0 ? pages : [""];
}

function App() {
  const [text, setText] = useState(DEFAULT_TEXT);
  const [displayLevel, setDisplayLevel] = useState<number>(700);
  const [dictionary, setDictionary] = useState<Record<string, any>>(MOCK_DICTIONARY);
  const [isAnalyzing, setIsAnalyzing] = useState(false);
  const [currentPageIndex, setCurrentPageIndex] = useState(0);

  const pages = useMemo(() => paginateText(text), [text]);

  // Reset to first page when text changes
  useEffect(() => {
    setCurrentPageIndex(0);
  }, [text]);

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
          if (data.dictionary) {
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

  const currentPageText = pages[currentPageIndex] || "";
  const annotatedPage = useMemo(() => annotateText(currentPageText, dictionary), [currentPageText, dictionary]);

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
          <div className="reader-container" data-threshold={displayLevel}>
            {annotatedPage}
          </div>
          
          {/* Pagination Controls */}
          {pages.length > 1 && (
            <div className="pagination-controls" style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', gap: '15px', marginTop: '15px' }}>
              <button 
                disabled={currentPageIndex === 0} 
                onClick={() => setCurrentPageIndex(prev => prev - 1)}
                style={{ padding: '8px 16px', background: '#27272a', border: '1px solid #3f3f46', borderRadius: '6px', color: '#e4e4e7', cursor: currentPageIndex === 0 ? 'not-allowed' : 'pointer' }}
              >
                Prev Page
              </button>
              <span style={{ fontSize: '0.9rem', color: '#a1a1aa' }}>
                Page {currentPageIndex + 1} of {pages.length}
              </span>
              <button 
                disabled={currentPageIndex === pages.length - 1} 
                onClick={() => setCurrentPageIndex(prev => prev + 1)}
                style={{ padding: '8px 16px', background: '#27272a', border: '1px solid #3f3f46', borderRadius: '6px', color: '#e4e4e7', cursor: currentPageIndex === pages.length - 1 ? 'not-allowed' : 'pointer' }}
              >
                Next Page
              </button>
            </div>
          )}
        </div>

      </section>

    </div>
  );
}

export default App;
