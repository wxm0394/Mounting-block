import { useState, useEffect, useMemo, useRef, useCallback } from 'react';
import { Session } from '@supabase/supabase-js';
import { supabase } from './lib/auth';
import { annotateText, MOCK_DICTIONARY } from './lib/annotator';

import './index.css';

const API_URL = import.meta.env.VITE_API_URL;
if (!API_URL) {
  throw new Error("Missing VITE_API_URL environment variable");
}

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
interface EpubTaskItem {
  id: string;
  original_filename: string;
  difficulty_level: number;
  target_lang: string;
  status: 'pending' | 'scanning' | 'translating' | 'generating' | 'completed' | 'failed' | string;
  progress_percent: number;
  current_step_desc: string;
  error_message?: string;
  storage_output_path?: string;
  created_at: string;
  updated_at: string;
  download_url?: string;
}

function formatTime(isoStr: string): string {
  try {
    const d = new Date(isoStr);
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  } catch {
    return isoStr;
  }
}

function isTaskTimedOut(task: EpubTaskItem): boolean {
  if (!['pending', 'scanning', 'translating', 'generating'].includes(task.status)) {
    return false;
  }
  const updatedTime = new Date(task.updated_at || task.created_at).getTime();
  return Date.now() - updatedTime > 15 * 60 * 1000;
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
  const [epubTasks, setEpubTasks] = useState<EpubTaskItem[]>([]);
  const [hiddenTaskIds, setHiddenTaskIds] = useState<string[]>(() => {
    try {
      const saved = localStorage.getItem('rl_hidden_task_ids');
      return saved ? JSON.parse(saved) : [];
    } catch {
      return [];
    }
  });
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [isUploadingEpub, setIsUploadingEpub] = useState(false);
  const [uploadSuccessNotice, setUploadSuccessNotice] = useState<string | null>(null);
  const [uploadModalError, setUploadModalError] = useState<string | null>(null);
  
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
        const response = await fetch(`${API_URL}/annotate`, {
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
        } else if (response.status === 422) {
          setError('文本超过 8 万字上限');
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

  const fetchEpubTasks = useCallback(async () => {
    if (!session?.user?.id) return;
    const threeHoursAgo = new Date(Date.now() - 3 * 3600 * 1000).toISOString();
    try {
      const { data, error: fetchErr } = await supabase
        .from('epub_tasks')
        .select('*')
        .gte('created_at', threeHoursAgo)
        .order('created_at', { ascending: false });

      if (fetchErr) {
        console.error('Error fetching epub tasks:', fetchErr);
        return;
      }

      if (data) {
        const enriched: EpubTaskItem[] = await Promise.all(
          data.map(async (task: any) => {
            let download_url = task.download_url;
            if (task.status === 'completed' && task.storage_output_path && !download_url) {
              try {
                const { data: signedData } = await supabase.storage
                  .from('epubs')
                  .createSignedUrl(task.storage_output_path, 3600);
                if (signedData?.signedUrl) {
                  download_url = signedData.signedUrl;
                }
              } catch {
                // Ignore signed url generation errors
              }
            }
            return {
              ...task,
              download_url,
            };
          })
        );
        setEpubTasks(enriched);
      }
    } catch (err) {
      console.error('Failed to load epub tasks:', err);
    }
  }, [session?.user?.id]);

  // Initial fetch on session or mode change
  useEffect(() => {
    if (session?.user?.id && mode === 'epub') {
      fetchEpubTasks();
    }
  }, [session?.user?.id, mode, fetchEpubTasks]);

  // Polling for active tasks (with tab visibility awareness)
  useEffect(() => {
    if (!session?.user?.id || mode !== 'epub') return;

    const visible = epubTasks.filter(t => !hiddenTaskIds.includes(t.id));
    const hasActiveTask = visible.some(t =>
      ['pending', 'scanning', 'translating', 'generating'].includes(t.status)
    );

    if (!hasActiveTask) return;

    const interval = setInterval(() => {
      if (!document.hidden) {
        fetchEpubTasks();
      }
    }, 3000);

    const handleVisibilityChange = () => {
      if (!document.hidden) {
        fetchEpubTasks();
      }
    };
    document.addEventListener('visibilitychange', handleVisibilityChange);

    return () => {
      clearInterval(interval);
      document.removeEventListener('visibilitychange', handleVisibilityChange);
    };
  }, [session?.user?.id, mode, epubTasks, hiddenTaskIds, fetchEpubTasks]);

  const handleHideTask = (taskId: string) => {
    setHiddenTaskIds(prev => {
      const updated = prev.includes(taskId) ? prev : [...prev, taskId];
      try {
        localStorage.setItem('rl_hidden_task_ids', JSON.stringify(updated));
      } catch (e) {
        console.error(e);
      }
      return updated;
    });
  };

  const visibleTasks = useMemo(() => {
    return epubTasks.filter(t => !hiddenTaskIds.includes(t.id));
  }, [epubTasks, hiddenTaskIds]);

  const handleUploadEpub = async () => {
    if (!selectedFile || isUploadingEpub) return;
    setIsUploadingEpub(true);
    setUploadModalError(null);
    setError(null);
    setUploadSuccessNotice(null);
    
    const formData = new FormData();
    formData.append('file', selectedFile);
    formData.append('difficulty_level', displayLevel.toString());
    formData.append('target_lang', 'zh-Hans');
    
    try {
      const response = await fetch(`${API_URL}/epub/upload`, {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${session?.access_token}`
        },
        body: formData
      });
      if (response.ok) {
        // 成功创建任务：关闭弹窗，清空文件输入框，重置选择
        setShowEpubModal(false);
        setIsUploadingEpub(false);
        if (fileInputRef.current) {
          fileInputRef.current.value = '';
        }
        setSelectedFile(null);
        setUploadSuccessNotice('🎉 文件已上传成功！任务已创建，开始排队处理。');
        setTimeout(() => {
          setUploadSuccessNotice(null);
        }, 6000);
        fetchEpubTasks();
      } else {
        setIsUploadingEpub(false);
        let detail = '上传失败';
        try {
          const errData = await response.json();
          if (Array.isArray(errData?.detail)) {
            detail = errData.detail
              .map((d: any) => d.msg || (typeof d === 'string' ? d : JSON.stringify(d)))
              .join('; ');
          } else if (typeof errData?.detail === 'string') {
            detail = errData.detail;
          } else if (errData?.detail && typeof errData.detail === 'object') {
            detail = JSON.stringify(errData.detail);
          } else if (response.status === 422) {
            detail = '上传参数格式校验失败';
          }
        } catch {
          detail = `上传请求失败 (HTTP ${response.status})`;
        }
        setUploadModalError(detail);
        setError(`EPUB 上传失败: ${detail}`);
        if (fileInputRef.current) {
          fileInputRef.current.value = '';
        }
        setSelectedFile(null);
      }
    } catch (err: any) {
      setIsUploadingEpub(false);
      const msg = err?.message || '网络连接失败，请检查网络连接或服务器状态';
      setUploadModalError(msg);
      setError(`EPUB 上传异常: ${msg}`);
      if (fileInputRef.current) {
        fileInputRef.current.value = '';
      }
      setSelectedFile(null);
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
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '0.4rem' }}>
                <label style={{ margin: 0 }}>Paste English Text:</label>
                <span style={{ fontSize: '0.8rem', color: text.length >= 80000 ? '#ef4444' : '#a1a1aa' }}>
                  {text.length.toLocaleString()} / 80,000
                </span>
              </div>
              <textarea
                value={text}
                onChange={(e) => setText(e.target.value)}
                maxLength={80000}
                className="text-input"
                placeholder="Paste any English paragraph here..."
              />
            </div>
          ) : (
            <div className="text-input-wrapper" style={{ marginTop: '20px' }}>
               <label>Upload EPUB:</label>
               <input
                 ref={fileInputRef}
                 type="file"
                 accept=".epub"
                 disabled={isUploadingEpub}
                 onChange={e => setSelectedFile(e.target.files?.[0] || null)}
                 style={{ marginBottom: '16px', display: 'block', color: '#e4e4e7', width: '100%', opacity: isUploadingEpub ? 0.6 : 1 }}
               />
               <button
                 disabled={!selectedFile || isUploadingEpub}
                 onClick={() => setShowEpubModal(true)}
                 style={{
                   padding: '10px 16px',
                   background: (!selectedFile || isUploadingEpub) ? '#27272a' : '#3b82f6',
                   color: 'white',
                   border: 'none',
                   borderRadius: '6px',
                   cursor: (!selectedFile || isUploadingEpub) ? 'not-allowed' : 'pointer',
                   width: '100%',
                   fontWeight: 600
                 }}
               >
                 {isUploadingEpub ? 'Uploading...' : 'Upload & Translate'}
               </button>
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

          {mode === 'epub' && uploadSuccessNotice && (
            <div style={{ background: '#052e16', border: '1px solid #14532d', color: '#86efac', padding: '14px 18px', borderRadius: '10px', marginTop: '12px', marginBottom: '12px', display: 'flex', alignItems: 'center', gap: '12px', boxShadow: '0 2px 10px rgba(0,0,0,0.3)' }}>
              <span style={{ fontSize: '20px' }}>🚀</span>
              <div>
                <strong style={{ display: 'block', fontSize: '14px', color: '#4ade80' }}>上传成功，任务已创建！</strong>
                <span style={{ fontSize: '13px', color: '#bbf7d0' }}>文件已送达服务器并加入处理队列，下方进度卡片将实时更新。</span>
              </div>
            </div>
          )}

          {mode === 'epub' && (
            <div style={{ marginTop: '16px' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '14px' }}>
                <h3 style={{ margin: 0, color: '#e4e4e7', fontSize: '1.05rem', fontWeight: 600 }}>
                  EPUB 任务列表 (最近 3 小时)
                </h3>
                <span style={{ fontSize: '12px', color: '#71717a' }}>
                  共 {visibleTasks.length} 个任务
                </span>
              </div>

              {visibleTasks.length === 0 ? (
                <div style={{ background: '#18181b', padding: '36px 20px', borderRadius: '12px', border: '1px dashed #3f3f46', textAlign: 'center', color: '#71717a' }}>
                  <p style={{ margin: 0, fontSize: '14px' }}>暂无最近 3 小时内的任务记录。</p>
                  <p style={{ margin: '8px 0 0', fontSize: '12px', color: '#52525b' }}>可在左侧选择并上传 EPUB 文件发起翻译任务。</p>
                </div>
              ) : (
                visibleTasks.map((task) => {
                  const isTimedOut = isTaskTimedOut(task);
                  return (
                    <div
                      key={task.id}
                      style={{
                        background: '#18181b',
                        padding: '18px 20px',
                        borderRadius: '12px',
                        border: '1px solid #3f3f46',
                        marginBottom: '14px',
                        boxShadow: '0 2px 8px rgba(0,0,0,0.2)'
                      }}
                    >
                      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: '10px' }}>
                        <div style={{ flex: 1, minWidth: 0, paddingRight: '12px' }}>
                          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
                            <strong style={{ color: '#e4e4e7', fontSize: '14px', wordBreak: 'break-all' }}>
                              📄 {task.original_filename}
                            </strong>
                            <span style={{ fontSize: '11px', color: '#a1a1aa', background: '#27272a', padding: '2px 6px', borderRadius: '4px' }}>
                              难度: {task.difficulty_level}
                            </span>
                            {isTimedOut && (
                              <span style={{ fontSize: '11px', color: '#f59e0b', background: '#451a03', border: '1px solid #78350f', padding: '2px 6px', borderRadius: '4px' }}>
                                ⏱️ 处理超时（可能已中断）
                              </span>
                            )}
                          </div>
                          <div style={{ fontSize: '11px', color: '#71717a', marginTop: '4px' }}>
                            提交时间: {formatTime(task.created_at)}
                          </div>
                        </div>

                        <button
                          type="button"
                          title="从列表中隐藏此任务"
                          onClick={() => handleHideTask(task.id)}
                          style={{
                            background: 'transparent',
                            border: 'none',
                            color: '#71717a',
                            cursor: 'pointer',
                            fontSize: '16px',
                            lineHeight: 1,
                            padding: '4px 6px',
                            borderRadius: '4px'
                          }}
                        >
                          ✕
                        </button>
                      </div>

                      {task.status === 'failed' ? (
                        <div style={{ color: '#ef4444', padding: '10px 14px', background: '#450a0a', borderRadius: '6px', fontSize: '13px', border: '1px solid #7f1d1d' }}>
                          ❌ 处理失败: {task.error_message || '未知错误'}
                        </div>
                      ) : task.status === 'completed' ? (
                        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', background: '#052e16', padding: '12px 16px', borderRadius: '8px', border: '1px solid #14532d' }}>
                          <span style={{ color: '#4ade80', fontSize: '13px', fontWeight: 600 }}>
                            ✅ 翻译生成完成！
                          </span>
                          {task.download_url ? (
                            <a
                              href={task.download_url}
                              target="_blank"
                              rel="noopener noreferrer"
                              style={{
                                display: 'inline-block',
                                padding: '6px 14px',
                                background: '#22c55e',
                                color: '#052e16',
                                textDecoration: 'none',
                                borderRadius: '6px',
                                fontWeight: 700,
                                fontSize: '12px'
                              }}
                            >
                              下载标注版 EPUB
                            </a>
                          ) : (
                            <span style={{ color: '#86efac', fontSize: '12px' }}>准备就绪</span>
                          )}
                        </div>
                      ) : (
                        <div>
                          <div style={{ marginBottom: '8px', color: '#a1a1aa', display: 'flex', justifyContent: 'space-between', fontSize: '12px' }}>
                            <span>{task.current_step_desc || `状态: ${task.status}`}</span>
                            <span style={{ fontWeight: 600, color: '#38bdf8' }}>{task.progress_percent}%</span>
                          </div>
                          <div style={{ background: '#27272a', height: '8px', borderRadius: '4px', overflow: 'hidden' }}>
                            <div
                              style={{
                                width: `${task.progress_percent}%`,
                                background: '#38bdf8',
                                height: '100%',
                                transition: 'width 0.4s ease'
                              }}
                            />
                          </div>
                        </div>
                      )}
                    </div>
                  );
                })
              )}
            </div>
          )}
        </div>

      </section>

      {/* EPUB Config Modal */}
      {showEpubModal && (
        <div style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.8)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000, backdropFilter: 'blur(4px)' }}>
          <div style={{ background: '#18181b', padding: '32px', borderRadius: '16px', width: '420px', border: '1px solid #3f3f46', boxShadow: '0 10px 30px rgba(0,0,0,0.5)' }}>
            <h2 style={{ marginTop: 0, color: '#e4e4e7', fontSize: '1.2rem' }}>Configure EPUB Translation</h2>
            
            {uploadModalError && (
              <div style={{ marginBottom: '16px', color: '#fca5a5', fontSize: '13px', background: '#450a0a', padding: '10px', borderRadius: '6px', border: '1px solid #7f1d1d' }}>
                ⚠️ {uploadModalError}
              </div>
            )}

            <div className="level-control" style={{ marginBottom: '32px', marginTop: '20px' }}>
              <div className="level-header" style={{ justifyContent: 'center' }}>
                <span className="level-value" style={{ fontSize: '14px', color: '#a1a1aa' }}>
                  翻译难度：{displayLevel}（在左侧面板调整）
                </span>
              </div>
            </div>

            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '12px' }}>
              <button 
                disabled={isUploadingEpub}
                onClick={() => {
                  if (!isUploadingEpub) {
                    setShowEpubModal(false);
                    setUploadModalError(null);
                  }
                }} 
                style={{ 
                  padding: '10px 20px', 
                  background: 'transparent', 
                  border: '1px solid #3f3f46', 
                  color: isUploadingEpub ? '#52525b' : '#e4e4e7', 
                  borderRadius: '6px', 
                  cursor: isUploadingEpub ? 'not-allowed' : 'pointer', 
                  fontWeight: 500 
                }}
              >
                Cancel
              </button>
              <button 
                onClick={handleUploadEpub} 
                disabled={isUploadingEpub} 
                style={{ 
                  padding: '10px 20px', 
                  background: isUploadingEpub ? '#1d4ed8' : '#3b82f6', 
                  border: 'none', 
                  color: isUploadingEpub ? '#93c5fd' : 'white', 
                  borderRadius: '6px', 
                  cursor: isUploadingEpub ? 'not-allowed' : 'pointer', 
                  fontWeight: 600,
                  display: 'flex',
                  alignItems: 'center',
                  gap: '8px'
                }}
              >
                {isUploadingEpub && (
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{width: '16px', height: '16px', animation: 'spin 1s linear infinite'}}>
                    <line x1="12" y1="2" x2="12" y2="6"></line><line x1="12" y1="18" x2="12" y2="22"></line><line x1="4.93" y1="4.93" x2="7.76" y2="7.76"></line><line x1="16.24" y1="16.24" x2="19.07" y2="19.07"></line><line x1="2" y1="12" x2="6" y2="12"></line><line x1="18" y1="12" x2="22" y2="12"></line><line x1="4.93" y1="19.07" x2="7.76" y2="16.24"></line><line x1="16.24" y1="7.76" x2="19.07" y2="4.93"></line>
                  </svg>
                )}
                {isUploadingEpub ? '正在上传…' : 'Confirm & Translate'}
              </button>
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
  const [loading, setLoading] = useState<'login' | 'signup' | false>(false);
  const [message, setMessage] = useState('');
  const [successMsg, setSuccessMsg] = useState('');

  const isEmailValid = email === '' || /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email);
  const isPasswordValid = password === '' || password.length >= 6;
  const isFormValid = email !== '' && password !== '' && isEmailValid && isPasswordValid;

  const handleLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!isFormValid) return;
    setLoading('login');
    setMessage('');
    setSuccessMsg('');
    const { error } = await supabase.auth.signInWithPassword({ email, password });
    if (error) setMessage(error.message);
    setLoading(false);
  };

  const handleSignUp = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!isFormValid) return;
    setLoading('signup');
    setMessage('');
    setSuccessMsg('');
    const { error } = await supabase.auth.signUp({ email, password });
    if (error) {
      setMessage(error.message);
    } else {
      setSuccessMsg('注册成功！请查收邮箱完成验证后再登录');
    }
    setLoading(false);
  };

  return (
    <div className="auth-container" style={{display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', height: '100vh', background: '#09090b', color: '#e4e4e7'}}>
      <div style={{background: '#18181b', padding: '40px', borderRadius: '12px', width: '340px', boxShadow: '0 4px 20px rgba(0,0,0,0.5)'}}>
        <h1 style={{marginTop: 0, marginBottom: '24px', fontSize: '24px', textAlign: 'center'}}>Sign in to ReadLevel</h1>
        {message && <div style={{marginBottom: '16px', color: '#fca5a5', fontSize: '14px', background: '#450a0a', padding: '10px', borderRadius: '6px', border: '1px solid #7f1d1d'}}>⚠️ {message}</div>}
        {successMsg && <div style={{marginBottom: '16px', color: '#86efac', fontSize: '14px', background: '#052e16', padding: '10px', borderRadius: '6px', border: '1px solid #14532d'}}>✅ {successMsg}</div>}
        
        <form style={{display: 'flex', flexDirection: 'column', gap: '16px'}}>
          <div>
            <input 
              type="email" 
              placeholder="Email" 
              value={email} 
              onChange={e => setEmail(e.target.value)} 
              style={{
                padding: '10px', borderRadius: '6px', border: `1px solid ${isEmailValid ? '#3f3f46' : '#ef4444'}`, 
                background: '#27272a', color: 'white', width: '100%', boxSizing: 'border-box', outline: 'none'
              }} 
            />
            {!isEmailValid && <div style={{color: '#f87171', fontSize: '12px', marginTop: '4px'}}>请输入有效的邮箱地址</div>}
          </div>
          
          <div>
            <input 
              type="password" 
              placeholder="Password" 
              value={password} 
              onChange={e => setPassword(e.target.value)} 
              style={{
                padding: '10px', borderRadius: '6px', border: `1px solid ${isPasswordValid ? '#3f3f46' : '#ef4444'}`, 
                background: '#27272a', color: 'white', width: '100%', boxSizing: 'border-box', outline: 'none'
              }} 
            />
            {!isPasswordValid && <div style={{color: '#f87171', fontSize: '12px', marginTop: '4px'}}>密码至少需要 6 个字符</div>}
          </div>

          <div style={{display: 'flex', gap: '12px', marginTop: '8px'}}>
            <button 
              type="button" 
              onClick={handleLogin} 
              disabled={!!loading || !isFormValid} 
              style={{
                flex: 1, padding: '10px', background: (!isFormValid || loading) ? '#1d4ed8' : '#3b82f6', 
                color: (!isFormValid || loading) ? '#93c5fd' : 'white', border: 'none', borderRadius: '6px', 
                cursor: (!isFormValid || loading) ? 'not-allowed' : 'pointer', fontWeight: 600, display: 'flex', justifyContent: 'center', alignItems: 'center', gap: '8px'
              }}
            >
              {loading === 'login' && (
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{width: '16px', height: '16px', animation: 'spin 1s linear infinite'}}>
                  <line x1="12" y1="2" x2="12" y2="6"></line><line x1="12" y1="18" x2="12" y2="22"></line><line x1="4.93" y1="4.93" x2="7.76" y2="7.76"></line><line x1="16.24" y1="16.24" x2="19.07" y2="19.07"></line><line x1="2" y1="12" x2="6" y2="12"></line><line x1="18" y1="12" x2="22" y2="12"></line><line x1="4.93" y1="19.07" x2="7.76" y2="16.24"></line><line x1="16.24" y1="7.76" x2="19.07" y2="4.93"></line>
                </svg>
              )}
              Sign In
            </button>
            <button 
              type="button" 
              onClick={handleSignUp} 
              disabled={!!loading || !isFormValid} 
              style={{
                flex: 1, padding: '10px', background: '#27272a', 
                color: (!isFormValid || loading) ? '#52525b' : 'white', border: '1px solid #3f3f46', borderRadius: '6px', 
                cursor: (!isFormValid || loading) ? 'not-allowed' : 'pointer', fontWeight: 600, display: 'flex', justifyContent: 'center', alignItems: 'center', gap: '8px'
              }}
            >
              {loading === 'signup' && (
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{width: '16px', height: '16px', animation: 'spin 1s linear infinite'}}>
                  <line x1="12" y1="2" x2="12" y2="6"></line><line x1="12" y1="18" x2="12" y2="22"></line><line x1="4.93" y1="4.93" x2="7.76" y2="7.76"></line><line x1="16.24" y1="16.24" x2="19.07" y2="19.07"></line><line x1="2" y1="12" x2="6" y2="12"></line><line x1="18" y1="12" x2="22" y2="12"></line><line x1="4.93" y1="19.07" x2="7.76" y2="16.24"></line><line x1="16.24" y1="7.76" x2="19.07" y2="4.93"></line>
                </svg>
              )}
              Sign Up
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

export default App;

