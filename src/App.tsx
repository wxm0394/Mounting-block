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
  current_step_desc: string | null;
  error_message: string | null;
  storage_output_path: string | null;
  download_url: string | null;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
}

// 只取界面用到的列；不取 error_detail（内部堆栈）、failed_words 等大字段
const EPUB_TASK_COLUMNS =
  'id,original_filename,difficulty_level,target_lang,status,progress_percent,current_step_desc,' +
  'error_message,storage_output_path,download_url,created_at,updated_at,completed_at';
const ACTIVE_STATUSES = ['pending', 'scanning', 'translating', 'generating'];
const TASK_WINDOW_MS = 3 * 3600 * 1000;
const TASK_TIMEOUT_MS = 90 * 60 * 1000;
const LEGACY_HIDDEN_KEY = 'rl_hidden_task_ids';
const hiddenKeyFor = (userId: string) => `rl_hidden_task_ids:${userId}`;

function loadHiddenIds(userId: string): string[] {
  try {
    const raw = localStorage.getItem(hiddenKeyFor(userId));
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed.filter((x: unknown) => typeof x === 'string') : [];
  } catch {
    return [];
  }
}

function saveHiddenIds(userId: string, ids: string[]) {
  try {
    if (ids.length === 0) localStorage.removeItem(hiddenKeyFor(userId));
    else localStorage.setItem(hiddenKeyFor(userId), JSON.stringify(ids));
  } catch (e) {
    console.error(e);
  }
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
  if (!ACTIVE_STATUSES.includes(task.status)) {
    return false;
  }
  const updatedTime = new Date(task.updated_at || task.created_at).getTime();
  return Date.now() - updatedTime > TASK_TIMEOUT_MS;
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
  const [hiddenTaskIds, setHiddenTaskIds] = useState<string[]>([]);
  const [downloadingTaskId, setDownloadingTaskId] = useState<string | null>(null);
  const [downloadErrors, setDownloadErrors] = useState<Record<string, string>>({});
  const currentUidRef = useRef<string | undefined>(undefined);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [isUploadingEpub, setIsUploadingEpub] = useState(false);
  const [uploadSuccessNotice, setUploadSuccessNotice] = useState<string | null>(null);
  const [uploadModalError, setUploadModalError] = useState<string | null>(null);
  
  const [session, setSession] = useState<Session | null>(null);
  const [authLoading, setAuthLoading] = useState(true);
  const [usage, setUsage] = useState<{chars_used: number, chars_limit: number | string} | null>(null);
  const [billingConfig, setBillingConfig] = useState<{environment: string, client_token: string | null, plans: {monthly: boolean, yearly: boolean}} | null>(null);
  const [subscribeBusy, setSubscribeBusy] = useState<string | null>(null);
  const [subscribeError, setSubscribeError] = useState<string | null>(null);
  const [subscribeNotice, setSubscribeNotice] = useState<string | null>(null);

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

  // Paddle 订阅：登录后拉取可售方案；从 Paddle 结账返回时提示等待 webhook 确认。
  // 权限只由服务端 webhook 授予，浏览器回到本站不代表已开通。
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.get('checkout') === 'success') {
      setSubscribeNotice('付款完成！订阅状态将在 Paddle 确认后更新，通常几秒内生效，刷新后额度会变为订阅档。');
      window.history.replaceState({}, '', window.location.pathname);
    }
    if (!session?.access_token) {
      setBillingConfig(null);
      return;
    }
    fetch(`${API_URL}/billing/config`, {
      headers: { 'Authorization': `Bearer ${session.access_token}` },
    }).then(r => r.ok ? r.json() : null).then(setBillingConfig).catch(() => setBillingConfig(null));
  }, [session?.access_token]);

  const loadPaddleJs = () => new Promise<any>((resolve, reject) => {
    const w = window as any;
    if (w.Paddle) return resolve(w.Paddle);
    const s = document.createElement('script');
    s.src = 'https://cdn.paddle.com/v2/paddle.js';
    s.onload = () => resolve((window as any).Paddle);
    s.onerror = () => reject(new Error('Paddle.js 加载失败'));
    document.head.appendChild(s);
  });

  const handleSubscribe = async (plan: 'monthly' | 'yearly') => {
    if (!session?.access_token) return;
    setSubscribeBusy(plan);
    setSubscribeError(null);
    try {
      const res = await fetch(`${API_URL}/billing/checkout`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${session.access_token}` },
        body: JSON.stringify({ plan }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.detail || `结账创建失败（${res.status}）`);
      }
      if (data.checkout_url) {
        window.location.href = data.checkout_url;
        return;
      }
      // 托管结账链接为空时退回 Paddle.js 悬浮结账（需 client_token）。
      if (data.transaction_id && billingConfig?.client_token) {
        const Paddle = await loadPaddleJs();
        Paddle.Initialize({ token: billingConfig.client_token, environment: billingConfig.environment === 'live' ? 'production' : 'sandbox' });
        Paddle.Checkout.open({ transactionId: data.transaction_id, settings: { successUrl: `${window.location.origin}/?checkout=success` } });
        return;
      }
      throw new Error('结账链接不可用，请稍后再试');
    } catch (err: any) {
      setSubscribeError(err.message || '订阅失败，请稍后再试');
    } finally {
      setSubscribeBusy(null);
    }
  };


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
        } else if (response.status === 503) {
          const errData = await response.json().catch(() => null);
          setError(errData?.detail || '用户配置服务暂时不可用，请稍后重试');
        } else {
          const errData = await response.json().catch(() => null);
          setError(typeof errData?.detail === 'string' ? errData.detail : `请求失败（status: ${response.status}）`);
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

  // 用户切换时载入该用户自己的隐藏列表；旧版不带用户 id 的键无法判断归属，直接删除
  useEffect(() => {
    const uid = session?.user?.id;
    currentUidRef.current = uid;
    try {
      localStorage.removeItem(LEGACY_HIDDEN_KEY);
    } catch {
      // ignore
    }
    setHiddenTaskIds(uid ? loadHiddenIds(uid) : []);
    setEpubTasks([]);
  }, [session?.user?.id]);

  const fetchEpubTasks = useCallback(async () => {
    const uid = session?.user?.id;
    if (!uid) return;
    const windowStart = new Date(Date.now() - TASK_WINDOW_MS).toISOString();
    try {
      // 窗口：进行中（不限时间） 或 completed_at 在 3 小时内
      const { data, error: fetchErr } = await supabase
        .from('epub_tasks')
        .select(EPUB_TASK_COLUMNS)
        .eq('user_id', uid)
        .or(`status.in.(${ACTIVE_STATUSES.join(',')}),completed_at.gte."${windowStart}"`)
        .order('created_at', { ascending: false });

      if (currentUidRef.current !== uid) return; // 请求期间切换了账号，丢弃旧结果
      if (fetchErr) {
        console.error('Error fetching epub tasks:', fetchErr);
        return;
      }

      const rows = (data ?? []) as unknown as EpubTaskItem[];
      setEpubTasks(rows);

      // 清理过期隐藏项：只保留仍在窗口内的任务 id（查询成功才清理，失败时不动）
      const alive = new Set(rows.map(r => r.id));
      setHiddenTaskIds(prev => {
        const kept = prev.filter(id => alive.has(id));
        if (kept.length === prev.length) return prev;
        saveHiddenIds(uid, kept);
        return kept;
      });
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
    const hasActiveTask = visible.some(t => ACTIVE_STATUSES.includes(t.status));

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
    const uid = session?.user?.id;
    if (!uid) return;
    setHiddenTaskIds(prev => {
      if (prev.includes(taskId)) return prev;
      const updated = [...prev, taskId];
      saveHiddenIds(uid, updated);
      return updated;
    });
  };

  // 点击时才取链接：优先用 worker 写入的 download_url（7 天签名），否则临时签 5 分钟
  const handleDownload = async (task: EpubTaskItem) => {
    if (downloadingTaskId) return;
    setDownloadingTaskId(task.id);
    setDownloadErrors(prev => {
      const { [task.id]: _drop, ...rest } = prev;
      return rest;
    });
    try {
      let url = task.download_url || null;
      if (!url && task.storage_output_path) {
        const { data: signed, error: signErr } = await supabase.storage
          .from('epubs')
          .createSignedUrl(task.storage_output_path, 300, { download: `annotated_${task.original_filename}` });
        if (signErr) throw signErr;
        url = signed?.signedUrl ?? null;
      }
      if (!url) throw new Error('没有可用的下载链接');
      window.location.assign(url);
    } catch (err: any) {
      setDownloadErrors(prev => ({ ...prev, [task.id]: err?.message || '获取下载链接失败' }));
    } finally {
      setDownloadingTaskId(null);
    }
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
        // 失败时保留已选文件，方便直接重试
        setUploadModalError(detail);
        setError(`EPUB 上传失败: ${detail}`);
      }
    } catch (err: any) {
      setIsUploadingEpub(false);
      const msg = err?.message || '网络连接失败，请检查网络连接或服务器状态';
      setUploadModalError(msg);
      setError(`EPUB 上传异常: ${msg}`);
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
          {session && billingConfig && billingConfig.plans.monthly && (
            <button
              onClick={() => handleSubscribe('monthly')}
              disabled={subscribeBusy !== null}
              style={{padding: '6px 12px', background: '#3b82f6', color: 'white', border: 'none', borderRadius: '6px', cursor: 'pointer', fontSize: '13px', fontWeight: 600}}
            >
              {subscribeBusy === 'monthly' ? '跳转中...' : '月付订阅'}
            </button>
          )}
          {session && billingConfig && billingConfig.plans.yearly && (
            <button
              onClick={() => handleSubscribe('yearly')}
              disabled={subscribeBusy !== null}
              style={{padding: '6px 12px', background: '#3b82f6', color: 'white', border: 'none', borderRadius: '6px', cursor: 'pointer', fontSize: '13px', fontWeight: 600}}
            >
              {subscribeBusy === 'yearly' ? '跳转中...' : '年付订阅'}
            </button>
          )}
          <button 
            onClick={() => supabase.auth.signOut()}
            style={{padding: '6px 12px', background: '#27272a', color: '#a1a1aa', border: '1px solid #3f3f46', borderRadius: '6px', cursor: 'pointer', fontSize: '13px'}}
          >
            Sign out
          </button>
        </div>
      </header>

      {subscribeNotice && (
        <div style={{ padding: '12px 20px', backgroundColor: '#f0fdf4', color: '#166534', borderBottom: '1px solid #bbf7d0', fontSize: '14px' }}>
          ✅ {subscribeNotice}
        </div>
      )}
      {subscribeError && (
        <div style={{ padding: '12px 20px', backgroundColor: '#fef2f2', color: '#991b1b', borderBottom: '1px solid #fecaca', fontSize: '14px' }}>
          ⚠️ {subscribeError}
        </div>
      )}

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
                  EPUB 任务列表 (进行中 / 3 小时内完成)
                </h3>
                <span style={{ fontSize: '12px', color: '#71717a' }}>
                  共 {visibleTasks.length} 个任务
                </span>
              </div>

              {visibleTasks.length === 0 ? (
                <div style={{ background: '#18181b', padding: '36px 20px', borderRadius: '12px', border: '1px dashed #3f3f46', textAlign: 'center', color: '#71717a' }}>
                  <p style={{ margin: 0, fontSize: '14px' }}>暂无进行中或 3 小时内完成的任务。</p>
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
                            {task.completed_at && <> · 完成时间: {formatTime(task.completed_at)}</>}
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
                        <div style={{ background: '#052e16', padding: '12px 16px', borderRadius: '8px', border: '1px solid #14532d' }}>
                          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                            <span style={{ color: '#4ade80', fontSize: '13px', fontWeight: 600 }}>
                              ✅ {task.current_step_desc || '翻译生成完成！'}
                            </span>
                            {(task.download_url || task.storage_output_path) ? (
                              <button
                                type="button"
                                id={`download-task-${task.id}`}
                                disabled={downloadingTaskId !== null}
                                onClick={() => handleDownload(task)}
                                style={{
                                  padding: '6px 14px',
                                  background: downloadingTaskId === task.id ? '#15803d' : '#22c55e',
                                  color: '#052e16',
                                  border: 'none',
                                  borderRadius: '6px',
                                  fontWeight: 700,
                                  fontSize: '12px',
                                  cursor: downloadingTaskId !== null ? 'wait' : 'pointer'
                                }}
                              >
                                {downloadingTaskId === task.id ? '获取链接中...' : '下载标注版 EPUB'}
                              </button>
                            ) : (
                              <span style={{ color: '#fca5a5', fontSize: '12px' }}>输出文件缺失</span>
                            )}
                          </div>
                          {downloadErrors[task.id] && (
                            <div style={{ marginTop: '8px', color: '#fca5a5', fontSize: '12px' }}>
                              下载失败：{downloadErrors[task.id]}
                            </div>
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

