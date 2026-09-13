import { useState, useEffect, useMemo } from 'react';
import { Session } from '@supabase/supabase-js';
import { supabase } from './lib/auth';
import { annotateText, MOCK_DICTIONARY } from './lib/annotator';

import './index.css';

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
  const [text, setText] = useState('');
  const [displayLevel, setDisplayLevel] = useState<number>(700);
  const [dictionary, setDictionary] = useState<Record<string, any>>(MOCK_DICTIONARY);
  const [isAnalyzing, setIsAnalyzing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [currentPageIndex, setCurrentPageIndex] = useState(0);
  
  const [session, setSession] = useState<Session | null>(null);
  const [authLoading, setAuthLoading] = useState(true);
  const [usage, setUsage] = useState<{chars_used: number, chars_limit: number | string} | null>(null);

  useEffect(() => {
    supabase.auth.getSession().then(({ data: { session } }) => {
      setSession(session);
      setAuthLoading(false);
    });

    const {
      data: { subscription },
    } = supabase.auth.onAuthStateChange((_event, session) => {
      setSession(session);
    });

    return () => subscription.unsubscribe();
  }, []);


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
      setError(null);
      try {
        const response = await fetch('http://localhost:8000/annotate', {
          method: 'POST',
          headers: { 
            'Content-Type': 'application/json',
            'Authorization': `Bearer ${session?.access_token}`
          },
          body: JSON.stringify({ text, target_lang: 'zh-Hans' }),
          signal: abortController.signal,
        });
        if (response.ok) {
          const data = await response.json();
          if (data.dictionary) {
            setDictionary(data.dictionary);
          }
          if (data.usage) {
            setUsage(data.usage);
          }
        } else if (response.status === 429) {
          const errData = await response.json();
          if (errData.detail && errData.detail.error === 'daily_quota_exceeded') {
            setError(`🚫 今日免费额度已用完（已用 ${errData.detail.chars_used} / ${errData.detail.chars_limit} 字符）。额度将于 ${errData.detail.resets_at} 重置。订阅可解锁更高额度。`);
          } else {
            setError('请求超限，请稍后再试');
          }
        } else if (response.status === 401) {
          setError('认证失败或 Session 已过期，请重新登录。');
          supabase.auth.signOut();
        } else {
          setError(`后端返回错误（status: ${response.status}），请检查后端终端日志`);
        }
      } catch (err: any) {
        if (err.name !== 'AbortError') {
          console.error("Backend unreachable:", err);
          setError("无法连接后端服务，请检查后端是否已启动");
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
  const annotatedPage = useMemo(() => {
    const counts: Record<string, number> = {};
    const paragraphs = currentPageText.split(/\n+/).filter(p => p.trim().length > 0);
    return paragraphs.map((p, idx) => (
      <p key={idx} className="reader-paragraph">
        {annotateText(p, dictionary, counts)}
      </p>
    ));
  }, [currentPageText, dictionary]);

  if (authLoading) return <div style={{display: 'flex', justifyContent: 'center', alignItems: 'center', height: '100vh', background: '#09090b', color: '#e4e4e7'}}>Loading...</div>;

  if (!session) {
    return <AuthScreen />;
  }

  return (
    <div className="app-container">

      <header className="app-header">
        <div>
          <h1>ReadLevel</h1>
          <p className="subtitle">Adaptive Ruby Annotator — words & phrases above your level get native translation</p>
        </div>
        
        <div style={{display: 'flex', alignItems: 'center', gap: '20px'}}>
          {usage && (
            <div style={{fontSize: '13px', color: '#a1a1aa'}}>
              Usage: <span style={{color: '#e4e4e7', fontWeight: 600}}>{usage.chars_used}</span> / {usage.chars_limit} chars
            </div>
          )}
          {isAnalyzing && (
            <div className="status-pill">
              <span className="status-dot"></span>
              AI 正在解析...
            </div>
          )}
          <button 
            onClick={() => supabase.auth.signOut()}
            style={{padding: '6px 12px', background: '#27272a', color: '#a1a1aa', border: '1px solid #3f3f46', borderRadius: '6px', cursor: 'pointer', fontSize: '13px'}}
          >
            Sign out
          </button>
        </div>
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
          {error && (
            <div style={{ padding: '12px', marginBottom: '16px', backgroundColor: '#fef2f2', color: '#991b1b', borderRadius: '6px', border: '1px solid #fecaca' }}>
              ⚠️ {error}
            </div>
          )}
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

function AuthScreen() {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState('');

  const handleLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setMessage('');
    const { error } = await supabase.auth.signInWithPassword({ email, password });
    if (error) setMessage(error.message);
    setLoading(false);
  };

  const handleSignUp = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setMessage('');
    const { error } = await supabase.auth.signUp({ email, password });
    if (error) setMessage(error.message);
    else setMessage('Sign up successful! You can now log in.');
    setLoading(false);
  };

  return (
    <div className="auth-container" style={{display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', height: '100vh', background: '#09090b', color: '#e4e4e7'}}>
      <div style={{background: '#18181b', padding: '40px', borderRadius: '12px', width: '340px', boxShadow: '0 4px 20px rgba(0,0,0,0.5)'}}>
        <h1 style={{marginTop: 0, marginBottom: '24px', fontSize: '24px', textAlign: 'center'}}>Sign in to ReadLevel</h1>
        {message && <div style={{marginBottom: '16px', color: '#fca5a5', fontSize: '14px', background: '#450a0a', padding: '8px', borderRadius: '6px'}}>{message}</div>}
        <form style={{display: 'flex', flexDirection: 'column', gap: '16px'}}>
          <input type="email" placeholder="Email" value={email} onChange={e => setEmail(e.target.value)} style={{padding: '10px', borderRadius: '6px', border: '1px solid #3f3f46', background: '#27272a', color: 'white'}} />
          <input type="password" placeholder="Password" value={password} onChange={e => setPassword(e.target.value)} style={{padding: '10px', borderRadius: '6px', border: '1px solid #3f3f46', background: '#27272a', color: 'white'}} />
          <div style={{display: 'flex', gap: '12px', marginTop: '8px'}}>
            <button type="button" onClick={handleLogin} disabled={loading} style={{flex: 1, padding: '10px', background: '#3b82f6', color: 'white', border: 'none', borderRadius: '6px', cursor: 'pointer', fontWeight: 600}}>Sign In</button>
            <button type="button" onClick={handleSignUp} disabled={loading} style={{flex: 1, padding: '10px', background: '#27272a', color: 'white', border: '1px solid #3f3f46', borderRadius: '6px', cursor: 'pointer', fontWeight: 600}}>Sign Up</button>
          </div>
        </form>
      </div>
    </div>
  );
}

export default App;

