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
  
  const [mode, setMode] = useState<'text' | 'epub'>('text');
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [showEpubModal, setShowEpubModal] = useState(false);
  const [epubTaskId, setEpubTaskId] = useState<string | null>(null);
  const [epubTaskStatus, setEpubTaskStatus] = useState<string | null>(null);
  const [epubTaskProgress, setEpubTaskProgress] = useState(0);
  const [epubTaskDesc, setEpubTaskDesc] = useState('');
  const [epubTaskError, setEpubTaskError] = useState<string | null>(null);
  const [epubDownloadUrl, setEpubDownloadUrl] = useState<string | null>(null);
  
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

  useEffect(() => {
    if (!epubTaskId || epubTaskStatus === 'completed' || epubTaskStatus === 'failed') return;
    const interval = setInterval(async () => {
      const { data } = await supabase.from('epub_tasks').select('*').eq('id', epubTaskId).single();
      if (data) {
        setEpubTaskStatus(data.status);
        setEpubTaskProgress(data.progress_percent);
        setEpubTaskDesc(data.current_step_desc || '');
        if (data.status === 'failed') setEpubTaskError(data.error_message || '未知错误');
        if (data.status === 'completed') {
          if (data.download_url) {
            setEpubDownloadUrl(data.download_url);
          } else if (data.storage_output_path) {
            const { data: signedUrlData } = await supabase.storage.from('epubs').createSignedUrl(data.storage_output_path, 3600);
            if (signedUrlData) setEpubDownloadUrl(signedUrlData.signedUrl);
          }
        }
      }
    }, 2000);
    return () => clearInterval(interval);
  }, [epubTaskId, epubTaskStatus]);

  const handleUploadEpub = async () => {
    if (!selectedFile) return;
    setShowEpubModal(false);
    setError(null);
    setEpubTaskId(null);
    setEpubTaskStatus(null);
    setEpubTaskError(null);
    setEpubDownloadUrl(null);
    
    const formData = new FormData();
    formData.append('file', selectedFile);
    formData.append('difficulty_level', displayLevel.toString());
    formData.append('target_lang', 'zh-Hans');
    
    try {
      const response = await fetch('http://localhost:8000/epub/upload', {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${session?.access_token}`
        },
        body: formData
      });
      if (response.ok) {
        const data = await response.json();
        setEpubTaskId(data.task_id);
        setEpubTaskStatus('pending');
        setEpubTaskDesc('任务已提交，排队中...');
      } else if (response.status === 403) {
        const errData = await response.json();
        setError(errData.detail);
      } else {
        const errData = await response.json();
        setError(errData.detail || '上传失败');
      }
    } catch (err) {
      setError("连接后端失败");
    }
  };

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
          <div style={{ display: 'flex', gap: '10px', marginBottom: '20px' }}>
            <button onClick={() => setMode('text')} style={{ flex: 1, padding: '8px', background: mode === 'text' ? '#3b82f6' : '#27272a', color: 'white', border: '1px solid #3f3f46', borderRadius: '6px', cursor: 'pointer' }}>Text Mode</button>
            <button onClick={() => setMode('epub')} style={{ flex: 1, padding: '8px', background: mode === 'epub' ? '#3b82f6' : '#27272a', color: 'white', border: '1px solid #3f3f46', borderRadius: '6px', cursor: 'pointer' }}>EPUB Mode</button>
          </div>

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

          {mode === 'text' ? (
            <div className="text-input-wrapper">
              <label>Paste English Text:</label>
              <textarea
                value={text}
                onChange={(e) => setText(e.target.value)}
                className="text-input"
                placeholder="Paste any English paragraph here..."
              />
            </div>
          ) : (
            <div className="text-input-wrapper" style={{ marginTop: '20px' }}>
               <label>Upload EPUB:</label>
               <input type="file" accept=".epub" onChange={e => setSelectedFile(e.target.files?.[0] || null)} style={{ marginBottom: '16px', display: 'block', color: '#e4e4e7', width: '100%' }} />
               <button disabled={!selectedFile} onClick={() => setShowEpubModal(true)} style={{ padding: '10px 16px', background: selectedFile ? '#3b82f6' : '#27272a', color: 'white', border: 'none', borderRadius: '6px', cursor: selectedFile ? 'pointer' : 'not-allowed', width: '100%', fontWeight: 600 }}>Upload & Translate</button>
            </div>
          )}
        </div>

        {/* Right: Reading View */}
        <div className="reading-panel">
          <label>Reading View:</label>
          {error && (
            <div style={{ padding: '12px', marginBottom: '16px', backgroundColor: '#fef2f2', color: '#991b1b', borderRadius: '6px', border: '1px solid #fecaca' }}>
              ⚠️ {error}
            </div>
          )}
          
          {mode === 'text' && (
            <>
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
            </>
          )}

          {mode === 'epub' && epubTaskId && (
             <div style={{ background: '#18181b', padding: '24px', borderRadius: '12px', border: '1px solid #3f3f46', marginTop: '10px' }}>
               <h3 style={{ marginTop: 0, marginBottom: '20px', color: '#e4e4e7' }}>EPUB Translation Status</h3>
               {epubTaskError ? (
                 <div style={{ color: '#ef4444', padding: '12px', background: '#450a0a', borderRadius: '6px' }}>❌ {epubTaskError}</div>
               ) : epubTaskStatus === 'completed' && epubDownloadUrl ? (
                 <div style={{ textAlign: 'center', padding: '20px' }}>
                   <div style={{ color: '#22c55e', marginBottom: '24px', fontSize: '1.2rem', fontWeight: 600 }}>✅ Translation Complete!</div>
                   <a href={epubDownloadUrl} target="_blank" rel="noopener noreferrer" style={{ display: 'inline-block', padding: '12px 24px', background: '#3b82f6', color: 'white', textDecoration: 'none', borderRadius: '6px', fontWeight: 600 }}>
                     Download Translated EPUB
                   </a>
                 </div>
               ) : (
                 <div>
                   <div style={{ marginBottom: '12px', color: '#a1a1aa', display: 'flex', justifyContent: 'space-between' }}>
                     <span>Status: {epubTaskStatus || 'Starting...'}</span>
                     <span>{epubTaskProgress}%</span>
                   </div>
                   <div style={{ background: '#27272a', height: '12px', borderRadius: '6px', overflow: 'hidden', marginBottom: '16px' }}>
                     <div style={{ width: `${epubTaskProgress}%`, background: '#3b82f6', height: '100%', transition: 'width 0.5s ease' }}></div>
                   </div>
                   <div style={{ color: '#e4e4e7', fontSize: '0.95rem' }}>{epubTaskDesc}</div>
                 </div>
               )}
             </div>
          )}
        </div>

      </section>

      {/* EPUB Config Modal */}
      {showEpubModal && (
        <div style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.8)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000, backdropFilter: 'blur(4px)' }}>
          <div style={{ background: '#18181b', padding: '32px', borderRadius: '16px', width: '400px', border: '1px solid #3f3f46', boxShadow: '0 10px 30px rgba(0,0,0,0.5)' }}>
            <h2 style={{ marginTop: 0, color: '#e4e4e7', fontSize: '1.2rem' }}>Configure EPUB Translation</h2>
            <div className="level-control" style={{ marginBottom: '32px', marginTop: '24px' }}>
              <div className="level-header">
                <label>Your Reading Level:</label>
                <span className="level-value">{displayLevel}</span>
              </div>
              <input type="range" min="500" max="1500" step="50" value={displayLevel} onChange={(e) => setDisplayLevel(Number(e.target.value))} className="level-slider" />
              <div className="level-labels"><span>Beginner</span><span>Intermediate</span><span>Advanced</span></div>
            </div>
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '12px' }}>
              <button onClick={() => setShowEpubModal(false)} style={{ padding: '10px 20px', background: 'transparent', border: '1px solid #3f3f46', color: '#e4e4e7', borderRadius: '6px', cursor: 'pointer', fontWeight: 500 }}>Cancel</button>
              <button onClick={handleUploadEpub} style={{ padding: '10px 20px', background: '#3b82f6', border: 'none', color: 'white', borderRadius: '6px', cursor: 'pointer', fontWeight: 600 }}>Confirm & Translate</button>
            </div>
          </div>
        </div>
      )}

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

