"""
ReadLevel Engine — Context-aware difficulty-adaptive bilingual annotation backend.

Architecture:
  - Difficulty Score (0~100 internal) ← f(word_freq, phrase_freq, POS, dep_complexity)
  - Display Level (500~1500 UI) ← linear mapping from Difficulty Score
  - Annotation IR: structured intermediate representation for each annotated span
  - Multi-pass pipeline: PASS 0 (NER/PROPN) → 1 (hyphenated) → 2 (lexicalized phrases) → 3 (adjacent merge) → 4 (single words)
  - API returns FULL dictionary regardless of threshold; frontend handles filtering.
"""
import uvicorn
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

import re
import spacy
import json
import hashlib
import sqlite3
import os
import sys
from wordfreq import zipf_frequency
import concurrent.futures

# Redirect stdout to a file for debugging
log_file = open('/tmp/backend_debug.log', 'a', buffering=1)
sys.stdout = log_file
sys.stderr = log_file

nlp = spacy.load("en_core_web_sm")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class Settings(BaseSettings):
    supabase_url: str
    supabase_service_role_key: str
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash-lite"
    
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

settings = Settings()
if settings.gemini_api_key and not os.environ.get("GEMINI_API_KEY"):
    os.environ["GEMINI_API_KEY"] = settings.gemini_api_key

from supabase import create_client, Client
supabase: Client = create_client(settings.supabase_url, settings.supabase_service_role_key)

security = HTTPBearer()

def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Verifies JWT token with Supabase and returns the user object."""
    token = credentials.credentials
    try:
        user_res = supabase.auth.get_user(token)
        if not user_res or not user_res.user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
        return user_res.user
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Authentication failed: {str(e)}"
        )

def get_user_profile(user_id: str):
    res = supabase.table("user_profiles").select("*").eq("user_id", user_id).execute()
    if res.data:
        return res.data[0]
    return {"is_subscribed": False, "daily_char_limit": 5000}

import logging

logger = logging.getLogger("uvicorn.error")

from datetime import datetime, timezone

def try_consume_quota(user_id: str, char_count: int, limit: int):
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    
    try:
        # Atomic RPC call (Requires consume_quota function in DB)
        res = supabase.rpc(
            "consume_quota",
            {"p_user_id": user_id, "p_today": today_str, "p_chars_to_add": char_count, "p_limit": limit}
        ).execute()
        return res.data # Returns integer (new usage) or None (exceeded)
    except Exception as e:
        # Fallback (non-atomic) if RPC doesn't exist
        logger.warning(f"RPC fallback used for quota: {e}")
        res = supabase.table("daily_usage").select("chars_used").eq("user_id", user_id).eq("usage_date", today_str).execute()
        current = res.data[0]["chars_used"] if res.data else 0
        if current + char_count > limit:
            return None
        supabase.table("daily_usage").upsert({
            "user_id": user_id,
            "usage_date": today_str,
            "chars_used": current + char_count
        }).execute()
        return current + char_count

class AnnotationRequest(BaseModel):
    text: str
    target_lang: str = "zh-Hans"   # target native language for translation


import sqlite3
import hashlib
import os
from contextlib import contextmanager

CACHE_DB_PATH = os.path.join(os.path.dirname(__file__), "translation_cache.db")

def init_db():
    with sqlite3.connect(CACHE_DB_PATH, check_same_thread=False) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS translations (
                key TEXT,
                target_lang TEXT,
                doc_hash TEXT,
                translation TEXT,
                PRIMARY KEY (key, target_lang, doc_hash)
            )
        """)

init_db()

@contextmanager
def get_db_connection():
    conn = sqlite3.connect(CACHE_DB_PATH, check_same_thread=False, timeout=10.0)
    try:
        yield conn
    finally:
        conn.close()

def get_cached_translation(key: str, target_lang: str, doc_hash: str) -> str:
    with get_db_connection() as conn:
        cursor = conn.execute("SELECT translation FROM translations WHERE key=? AND target_lang=? AND doc_hash=?", (key, target_lang, doc_hash))
        row = cursor.fetchone()
        if row:
            return row[0]
        cursor = conn.execute("SELECT translation FROM translations WHERE key=? AND target_lang=? LIMIT 1", (key, target_lang))
        row = cursor.fetchone()
        return row[0] if row else None

def set_cached_translation(key: str, target_lang: str, doc_hash: str, translation: str):
    with get_db_connection() as conn:
        conn.execute("INSERT OR REPLACE INTO translations (key, target_lang, doc_hash, translation) VALUES (?, ?, ?, ?)",
                     (key, target_lang, doc_hash, translation))
        conn.commit()

# ── A1-level ultra-common lemmas that NEVER need annotation ───────────
STOP_LEMMAS = {
    "be", "have", "do", "say", "go", "get", "make", "know", "think", "take",
    "see", "come", "want", "look", "use", "give", "find", "tell", "ask",
    "work", "seem", "feel", "try", "leave", "call", "need", "become",
    "keep", "let", "begin", "show", "hear", "play", "run", "move", "live",
    "the", "a", "an", "this", "that", "these", "those", "my", "your",
    "his", "her", "its", "our", "their", "i", "you", "he", "she", "it",
    "we", "they", "me", "him", "us", "them", "who", "what", "which",
    "when", "where", "how", "why", "all", "each", "every", "both",
    "few", "more", "most", "other", "some", "such", "no", "not", "only",
    "own", "same", "so", "than", "too", "very", "can", "will", "just",
    "should", "now", "also", "people", "time", "way", "day", "man",
    "woman", "child", "world", "life", "hand", "part", "place", "case",
    "week", "head", "side", "thing", "still", "well", "back", "here",
    "there", "then", "much", "many", "even", "good", "new", "first",
    "last", "long", "great", "little", "right", "old", "big", "high",
    "small", "large", "next", "early", "young", "important", "could",
    "would", "may", "might", "must", "shall", "put", "set", "turn",
    "start", "help", "line", "point", "city", "home", "read", "write",
    "learn", "grow", "draw", "open", "close", "stop", "hold", "bring",
    "stand", "sit", "walk", "talk", "eat", "drink", "sleep", "pick",
    "wait", "send", "fall", "cut", "reach", "stay", "kill", "pass",
    "sell", "buy", "pay", "meet", "carry", "offer", "lose", "spend",
    "win", "die", "rise", "speak", "name", "word", "real", "sure",
    "water", "food", "money", "book", "door", "room", "body", "face",
    "year", "girl", "boy", "number",
}

# ── Lexicalized phrases that should be treated as learning units ──────
# These are phrasal verbs, collocations, and idioms that cannot be
# naturally understood by translating each word individually.
LEXICALIZED_PHRASES = {
    # Phrasal verbs (verb + particle)
    "take care of", "look forward to", "get rid of", "come up with",
    "make up for", "put up with", "run out of", "give up", "take off",
    "break down", "carry out", "figure out", "find out", "give in",
    "go on", "hand in", "hold on", "keep up", "look after", "look into",
    "make out", "pick up", "point out", "set up", "show up", "shut down",
    "sort out", "stand out", "take over", "throw away", "turn down",
    "turn out", "turn up", "work out", "bring about", "bring up",
    "call off", "check out", "come across", "cut down", "deal with",
    "depend on", "drop off", "end up", "fall apart", "get along",
    "get away", "get over", "go through", "grow up", "hang out",
    "keep on", "lay off", "let down", "live up to", "look up to",
    "pass away", "pay off", "pull off", "rule out", "run into",
    "settle down", "take after", "take apart", "take in", "take on",
    "think over", "try on", "watch out", "wear out",
    # Common collocations / fixed expressions
    "as well as", "in order to", "in terms of", "on behalf of",
    "in spite of", "by means of", "for the sake of", "with regard to",
    "in addition to", "according to", "due to", "instead of",
    "regardless of", "prior to", "subsequent to", "in light of",
    "take place", "make sense", "pay attention", "bear in mind",
    "take advantage of", "make use of", "take part in", "come to terms with",
}

# ── Complex prepositions for PASS 2.5 ─────────────────────────────────
# Note: Although some items here (e.g., "on behalf of") might look like they
# overlap with LEXICALIZED_PHRASES, it is safe because PASS 2 matching 
# strictly requires a verb as the root, so it won't conflict with pure prepositions.
COMPLEX_PREPOSITIONS = {
    "under cover of", "in the name of", "on behalf of", "in front of",
    "in spite of", "by means of", "for the sake of", "with regard to",
    "in addition to", "in light of", "due to", "instead of",
    "regardless of", "prior to", "subsequent to"
}

# ── Difficulty Score engine ───────────────────────────────────────────
def compute_difficulty_score(word: str) -> float:
    """
    Compute a difficulty score (0.0 ~ 100.0) from Zipf frequency.
    Higher score = harder word.
    Zipf 7.0 (ultra-common) → score ~0
    Zipf 0.0 (unknown/rare) → score 100
    """
    zipf = zipf_frequency(word.lower(), 'en')
    if zipf == 0.0:
        return 100.0
    # Linear mapping: zipf 7.0 → 0, zipf 1.0 → 100
    score = (7.0 - zipf) * (100.0 / 6.0)
    return max(0.0, min(100.0, score))

def difficulty_to_display_level(score: float) -> int:
    """Map internal difficulty score (0~100) to UI display level (500~1500)."""
    return int(500 + score * 10.0)

def display_level_to_min_score(display_level: int) -> float:
    """Convert a UI display level threshold to minimum difficulty score."""
    return (display_level - 500) / 10.0

# Mock for LLM structured output.
# In production, this data is returned by the offline LLM batch task.
FR_LEXICAL_METADATA = {
    "library": {"cognate_status": "false_friend", "cognate_confidence": 0.99},
    "actually": {"cognate_status": "false_friend", "cognate_confidence": 0.95},
    "communication": {"cognate_status": "true_cognate", "cognate_confidence": 0.99},
    "sensible": {"cognate_status": "context_dependent", "cognate_confidence": 0.90},
    "apple": {"cognate_status": "unrelated", "cognate_confidence": 0.99},
}

ES_LEXICAL_METADATA = {
    "library": {"cognate_status": "false_friend", "cognate_confidence": 0.99},
    "actually": {"cognate_status": "false_friend", "cognate_confidence": 0.95},
    "communication": {"cognate_status": "true_cognate", "cognate_confidence": 0.99},
    "sensible": {"cognate_status": "context_dependent", "cognate_confidence": 0.90},
    "apple": {"cognate_status": "unrelated", "cognate_confidence": 0.99},
}

def is_difficult_token(token, min_score: float) -> bool:
    """Check if a spaCy token exceeds the difficulty threshold."""
    if len(token.text) < 4:
        return False
    if token.lemma_.lower() in STOP_LEMMAS:
        return False
    return compute_difficulty_score(token.lemma_) >= min_score

# ── POS labels ────────────────────────────────────────────────────────
POS_MAP = {
    "NOUN": "n.", "PROPN": "n.", "VERB": "v.", "ADJ": "adj.",
    "ADV": "adv.", "ADP": "prep.", "CCONJ": "conj.", "SCONJ": "conj.",
    "DET": "det.", "PRON": "pron.", "NUM": "num.", "INTJ": "interj.",
}

def get_pos_label(token) -> str:
    if token.tag_ == "VBN" and token.dep_ in ("amod", "acomp", "attr"):
        return "adj."
    if token.tag_ == "VBG" and token.dep_ in ("amod", "acomp"):
        return "adj."
    return POS_MAP.get(token.pos_, "")

# ── Annotation IR builder ─────────────────────────────────────────────
def make_annotation(surface: str, lemma: str, kind: str, base_level: int,
                    pos: str = "", head: str = "", pattern: str = "",
                    char_start: int = -1, char_end: int = -1,
                    sub_words: list = None) -> dict:
    """Build a structured language-agnostic Annotation IR entry."""
    return {
        "surface": surface,
        "lemma": lemma,
        "kind": kind,           # "word" | "phrase" | "entity"
        "difficulty": {
            "base_level": base_level,
            # These will be updated in the fan-out adjustment layer
            "adjusted_level": base_level,
        },
        "pos": pos,
        "syntax": {"head": head, "pattern": pattern} if pattern else None,
        "semantic": None,       # filled by translation layer
        "sub_words": sub_words or []
    }

# ── Lexicalized phrase detection ──────────────────────────────────────
# TODO (Known Bug): This function contains a greedy consumption bug. 
# If a verb has any `prt` child, it indiscriminately returns True. Because the 
# PASS 2 builder forcibly appends all `prep`+`pobj` branches of the verb, a 
# phrasal verb with a particle (e.g., "give up") will greedily swallow any 
# following adverbial prepositional phrases (e.g., "under cover of darkness"), 
# causing them to be falsely merged into the verb phrase.
#
# TODO (Limitation): PASS 2.5 A (Complex Prepositions) currently only recognizes 
# `pobj` (noun objects). It does not recognize `pcomp` (e.g., gerunds like 
# "instead of complaining"), so those structures won't be successfully merged yet.
def is_lexicalized_phrase(phrase_text: str, verb_token) -> bool:
    """
    Check if a verb phrase is a lexicalized unit (idiom/collocation)
    rather than a regular syntactic combination like 'opened the door'.
    """
    lower = phrase_text.lower()
    # Direct dictionary match
    if lower in LEXICALIZED_PHRASES:
        return True
    # Check verb + particle (always lexicalized)
    if any(c.dep_ == "prt" for c in verb_token.children):
        return True
    return False

# ── Core multi-pass analysis (returns ALL annotations, no threshold filtering) ──
def analyze_text(text: str):
    doc = nlp(text)
    annotations: dict[str, dict] = {}
    consumed: set[int] = set()
    min_score = 0.0  # Extract everything; frontend filters by threshold

    # ── PASS 0: Named Entities & Proper Noun Chunks ──────────────────
    for chunk in doc.noun_chunks:
        if any(t.pos_ == "PROPN" for t in chunk):
            chunk_text = chunk.text.strip().lower()
            lemma = " ".join(t.lemma_.lower() for t in chunk if t.pos_ != "DET")
            if len(chunk) == 1:
                score = compute_difficulty_score(chunk_text)
                annotations[chunk_text] = make_annotation(
                    chunk_text, lemma, "entity", difficulty_to_display_level(score), pos="n."
                )
            else:
                score = compute_difficulty_score(chunk_text)
                annotations[chunk_text] = make_annotation(
                    chunk_text, lemma, "entity", difficulty_to_display_level(score)
                )
            for i in range(chunk.start, chunk.end):
                consumed.add(i)

    for token in doc:
        if token.pos_ == "PROPN" and token.i not in consumed:
            score = compute_difficulty_score(token.text)
            annotations[token.text.lower()] = make_annotation(
                token.text.lower(), token.lemma_.lower(), "entity", difficulty_to_display_level(score), pos="n."
            )
            consumed.add(token.i)

    # ── PASS 1: Hyphenated compounds ─────────────────────────────────
    for m in re.finditer(r'\b([a-zA-Z]+-(?:[a-zA-Z]+-)*[a-zA-Z]+)\b', text):
        compound = m.group(1).lower()
        
        # Find overlapping tokens
        span_tokens = [t for t in doc if t.idx >= m.start() and (t.idx + len(t.text)) <= m.end()]
        span_indices = set(t.i for t in span_tokens)
        
        if span_indices and span_indices.isdisjoint(consumed):
            score = compute_difficulty_score(compound)
            if compound not in STOP_LEMMAS:
                sub_words = []
                for t in span_tokens:
                    if t.pos_ not in ("PUNCT", "SPACE", "SYM", "NUM") and len(t.text) >= 4 and t.lemma_.lower() not in STOP_LEMMAS:
                        sw_score = compute_difficulty_score(t.lemma_)
                        sub_words.append({
                            "surface": t.text.lower(),
                            "lemma": t.lemma_.lower(),
                            "level": difficulty_to_display_level(sw_score),
                            "pos": get_pos_label(t)
                        })
                annotations[compound] = make_annotation(
                    compound, compound, "phrase", difficulty_to_display_level(score), sub_words=sub_words
                )
                consumed.update(span_indices)

    # ── PASS 2: Lexicalized verb phrases only ────────────────────────
    for token in doc:
        if token.pos_ != "VERB":
            continue
        if token.i in consumed:
            continue

        parts = [token]
        for child in token.children:
            if child.i in consumed:
                continue
            if child.dep_ == "prt":
                parts.append(child)
            elif child.dep_ in ("dobj", "obj"):
                parts.append(child)
            elif child.dep_ == "prep":
                parts.append(child)
                for grandchild in child.children:
                    if grandchild.dep_ == "pobj" and grandchild.i not in consumed:
                        parts.append(grandchild)

        parts.sort(key=lambda t: t.i)

        if len(parts) >= 2 and parts[-1].i - parts[0].i <= 5:
            phrase_text = doc[parts[0].i:parts[-1].i + 1].text.strip().lower()
            if len(phrase_text.split()) >= 2:
                # ★ Only accept lexicalized phrases, not regular V+O combinations
                if is_lexicalized_phrase(phrase_text, token):
                    span_indices = set(range(parts[0].i, parts[-1].i + 1))
                    if span_indices.isdisjoint(consumed):
                        scores = [compute_difficulty_score(t.lemma_) for t in parts]
                        avg_score = sum(scores) / len(scores)
                        lemma = " ".join(t.lemma_.lower() for t in parts)
                        
                        sub_words = []
                        for t in doc[parts[0].i:parts[-1].i + 1]:
                            if t.pos_ not in ("PUNCT", "SPACE", "SYM", "NUM") and len(t.text) >= 4 and t.lemma_.lower() not in STOP_LEMMAS:
                                sw_score = compute_difficulty_score(t.lemma_)
                                sub_words.append({
                                    "surface": t.text.lower(),
                                    "lemma": t.lemma_.lower(),
                                    "level": difficulty_to_display_level(sw_score),
                                    "pos": get_pos_label(t)
                                })
                                
                        annotations[phrase_text] = make_annotation(
                            phrase_text, lemma, "phrase", difficulty_to_display_level(avg_score),
                            head=token.lemma_, pattern="VERB+PRT" if any(c.dep_ == "prt" for c in token.children) else "VERB+PREP",
                            sub_words=sub_words
                        )
                        consumed.update(span_indices)

    # ── PASS 2.5: Syntactic Modifier Merge ───────────────────────────
    # A. Complex Prepositions
    text_lower = text.lower()
    for cp in COMPLEX_PREPOSITIONS:
        for m in re.finditer(r'\b' + re.escape(cp) + r'\b', text_lower):
            cp_tokens = [t for t in doc if t.idx >= m.start() and (t.idx + len(t.text)) <= m.end()]
            if not cp_tokens: continue
            
            cp_indices = set(t.i for t in cp_tokens)
            if not cp_indices.isdisjoint(consumed):
                continue
                
            last_cp_token = cp_tokens[-1]
            pobj_tokens = []
            
            for child in last_cp_token.children:
                if child.dep_ == "pobj":
                    pobj_tokens = list(child.subtree)
                    break
                    
            if pobj_tokens:
                pobj_indices = set(t.i for t in pobj_tokens)
                if pobj_indices.isdisjoint(consumed):
                    start_i = min(cp_tokens[0].i, pobj_tokens[0].i)
                    end_i = max(cp_tokens[-1].i, pobj_tokens[-1].i)
                    span_tokens = doc[start_i:end_i+1]
                    span_indices = set(range(start_i, end_i+1))
                    
                    if span_indices.isdisjoint(consumed):
                        phrase_text = span_tokens.text.strip().lower()
                        
                        content_words = [t for t in span_tokens if t.lemma_.lower() not in STOP_LEMMAS and len(t.text) >= 4]
                        if content_words:
                            avg_score = sum(compute_difficulty_score(t.lemma_) for t in content_words) / len(content_words)
                        else:
                            avg_score = 0.0
                            
                        lemma = " ".join(t.lemma_.lower() for t in span_tokens if t.pos_ not in ("PUNCT", "SPACE"))
                        sub_words = []
                        for t in span_tokens:
                            if t.pos_ not in ("PUNCT", "SPACE", "SYM", "NUM") and len(t.text) >= 4 and t.lemma_.lower() not in STOP_LEMMAS:
                                sw_score = compute_difficulty_score(t.lemma_)
                                sub_words.append({
                                    "surface": t.text.lower(),
                                    "lemma": t.lemma_.lower(),
                                    "level": difficulty_to_display_level(sw_score),
                                    "pos": get_pos_label(t)
                                })
                                
                        annotations[phrase_text] = make_annotation(
                            phrase_text, lemma, "phrase", difficulty_to_display_level(avg_score),
                            pattern="COMPLEX_PREP", sub_words=sub_words
                        )
                        consumed.update(span_indices)

    # B. Syntactic Modifiers (amod, advmod, compound)
    for head in doc:
        if head.i in consumed:
            continue
            
        # Only start from the top of a modifier chain
        if head.dep_ in ("amod", "advmod", "compound"):
            continue
            
        cluster = []
        queue = [head]
        while queue:
            curr = queue.pop(0)
            if curr not in cluster:
                cluster.append(curr)
            for child in curr.children:
                if child.dep_ in ("amod", "advmod", "compound"):
                    queue.append(child)
                    
        if len(cluster) >= 2 and len(cluster) <= 5:
            cluster.sort(key=lambda t: t.i)
            if any(t.i in consumed for t in cluster):
                continue
                
            start_i = cluster[0].i
            end_i = cluster[-1].i
            
            is_contiguous = True
            for i in range(start_i, end_i + 1):
                if doc[i] not in cluster and doc[i].pos_ not in ("PUNCT", "SPACE"):
                    is_contiguous = False
                    break
                    
            if is_contiguous:
                if any(is_difficult_token(t, 20.0) for t in cluster):
                    span_indices = set(range(start_i, end_i + 1))
                    span_tokens = doc[start_i:end_i+1]
                    
                    phrase_text = span_tokens.text.strip().lower()
                    
                    content_words = [t for t in cluster if t.lemma_.lower() not in STOP_LEMMAS and len(t.text) >= 4]
                    if content_words:
                        avg_score = sum(compute_difficulty_score(t.lemma_) for t in content_words) / len(content_words)
                    else:
                        avg_score = 0.0
                        
                    lemma = " ".join(t.lemma_.lower() for t in span_tokens if t.pos_ not in ("PUNCT", "SPACE"))
                    sub_words = []
                    for t in span_tokens:
                        if t.pos_ not in ("PUNCT", "SPACE", "SYM", "NUM") and len(t.text) >= 4 and t.lemma_.lower() not in STOP_LEMMAS:
                            sw_score = compute_difficulty_score(t.lemma_)
                            sub_words.append({
                                "surface": t.text.lower(),
                                "lemma": t.lemma_.lower(),
                                "level": difficulty_to_display_level(sw_score),
                                "pos": get_pos_label(t)
                            })
                            
                    annotations[phrase_text] = make_annotation(
                        phrase_text, lemma, "phrase", difficulty_to_display_level(avg_score),
                        pattern="MODIFIER", sub_words=sub_words
                    )
                    consumed.update(span_indices)

    # ── PASS 3: Adjacent difficult words merge (max 3 words) ─────────
    tokens = list(doc)
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if (t.i in consumed or t.pos_ in ("PUNCT", "SPACE", "SYM")
            or not is_difficult_token(t, 20.0)):  # Use low bar for extraction
            i += 1
            continue

        run = [t]
        j = i + 1
        while j < len(tokens) and len(run) < 4:
            tj = tokens[j]
            if tj.i in consumed or tj.pos_ in ("PUNCT", "SPACE"):
                break
            if tj.lemma_.lower() in STOP_LEMMAS and len(tj.text) <= 3:
                if j + 1 < len(tokens) and (tokens[j+1].i not in consumed) and is_difficult_token(tokens[j+1], 20.0):
                    run.append(tj)
                    j += 1
                    continue
                break
            if is_difficult_token(tj, 20.0):
                run.append(tj)
                j += 1
            else:
                break

        content_in_run = [t for t in run if t.lemma_.lower() not in STOP_LEMMAS]
        if len(content_in_run) >= 2:
            span_indices = set(range(run[0].i, run[-1].i + 1))
            if span_indices.isdisjoint(consumed):
                phrase_text = doc[run[0].i:run[-1].i + 1].text.strip().lower()
                scores = [compute_difficulty_score(t.lemma_) for t in content_in_run]
                avg_score = sum(scores) / len(scores)
                lemma = " ".join(t.lemma_.lower() for t in content_in_run)
                
                sub_words = []
                for t in doc[run[0].i:run[-1].i + 1]:
                    if t.pos_ not in ("PUNCT", "SPACE", "SYM", "NUM") and len(t.text) >= 4 and t.lemma_.lower() not in STOP_LEMMAS:
                        sw_score = compute_difficulty_score(t.lemma_)
                        sub_words.append({
                            "surface": t.text.lower(),
                            "lemma": t.lemma_.lower(),
                            "level": difficulty_to_display_level(sw_score),
                            "pos": get_pos_label(t)
                        })
                        
                annotations[phrase_text] = make_annotation(
                    phrase_text, lemma, "phrase", difficulty_to_display_level(avg_score), sub_words=sub_words
                )
                consumed.update(span_indices)
            i = j
        else:
            i += 1

    # ── PASS 4: Remaining single difficult words ─────────────────────
    for token in doc:
        if token.i in consumed:
            continue
        if token.pos_ in ("PUNCT", "SPACE", "SYM", "NUM"):
            continue
        if len(token.text) < 4:
            continue
        if token.lemma_.lower() in STOP_LEMMAS:
            continue

        score = compute_difficulty_score(token.lemma_)
        pos = get_pos_label(token)
        annotations[token.text.lower()] = make_annotation(
            token.text.lower(), token.lemma_.lower(), "word", difficulty_to_display_level(score), pos=pos
        )

    return annotations

# ── Target Language Adjustments ───────────────────────────────────────
def apply_target_lang_adjustments(annotations: dict[str, dict], target_lang: str) -> dict[str, dict]:
    """Applies target-language specific difficulty adjustments."""
    lang_prefix = target_lang.lower().split('-')[0]
    
    adjusted = {}
    for key, meta in annotations.items():
        entry = dict(meta)
        adj_level = entry["difficulty"]["base_level"]
        
        # Select target lexicon
        lexicon = {}
        if lang_prefix == "fr":
            lexicon = FR_LEXICAL_METADATA
        elif lang_prefix == "es":
            lexicon = ES_LEXICAL_METADATA
            
        if entry["lemma"] in lexicon:
            metadata = lexicon[entry["lemma"]]
            status = metadata.get("cognate_status", "unrelated")
            confidence = metadata.get("cognate_confidence", 1.0)
            
            # Confidence Thresholding: If low confidence, force to context_dependent
            if confidence < 0.8:
                status = "context_dependent"
                
            if status == "false_friend":
                adj_level = 1500  # Max level: MUST show to warn the reader
            elif status == "true_cognate":
                adj_level = 500   # Min level: Hide unless reader is absolute beginner
            elif status == "context_dependent":
                adj_level = adj_level # Pass-through to dynamic LLM
            elif status == "unrelated":
                adj_level = adj_level # Pass-through
                
        entry["difficulty"]["adjusted_level"] = adj_level
        adjusted[key] = entry
        
    return adjusted

# ── Translation layer ─────────────────────────────────────────────────
from google import genai
import json

class TranslationItem(BaseModel):
    span: str = Field(description="The exact English word or phrase from the source text")
    translation: str = Field(description="The translation of the span in the context of the source text")

class TranslationBatch(BaseModel):
    items: list[TranslationItem]

def batch_translate(annotations: dict[str, dict], target_lang: str, source_text: str) -> dict[str, dict]:
    doc_hash = hashlib.md5(source_text.encode()).hexdigest()
    to_translate = []
    # Collect all top-level keys
    for key in annotations:
        if get_cached_translation(key, target_lang, doc_hash) is None:
            to_translate.append(key)
        # Also collect all sub_words
        for sw in annotations[key].get("sub_words", []):
            sw_key = sw["surface"].lower()
            if get_cached_translation(sw_key, target_lang, doc_hash) is None and sw_key not in to_translate:
                to_translate.append(sw_key)

    if to_translate:
        try:
            client = genai.Client(api_key=settings.gemini_api_key or os.environ.get("GEMINI_API_KEY"))
            chunk_size = 50
            for i in range(0, len(to_translate), chunk_size):
                chunk = to_translate[i:i + chunk_size]
                try:
                    prompt = f"""
Translate the following English words/phrases into {target_lang} based on their context in the source text.
Return ONLY valid JSON matching exactly this schema:
{{
  "items": [
    {{"span": "word or phrase", "translation": "translated text"}},
    ...
  ]
}}

Source text:
{source_text}

Words/Phrases to translate:
{json.dumps(chunk)}
"""
                    response = None
                    import time
                    for attempt in range(3):
                        try:
                            response = client.models.generate_content(
                                model=settings.gemini_model,
                                contents=prompt,
                                config=genai.types.GenerateContentConfig(
                                    response_mime_type="application/json",
                                    temperature=0.1
                                )
                            )
                            break # Success!
                        except Exception as e:
                            print(f"Gemini translation batch {i//chunk_size} attempt {attempt+1} failed: {e}")
                            if attempt < 2:
                                time.sleep(2 ** attempt) # Exponential backoff: 1s, 2s
                            else:
                                raise e # Re-raise if all attempts fail
                    
                    if response and response.text:
                        try:
                            clean_text = response.text.strip()
                            if clean_text.startswith("```json"):
                                clean_text = clean_text[7:]
                            elif clean_text.startswith("```"):
                                clean_text = clean_text[3:]
                            if clean_text.endswith("```"):
                                clean_text = clean_text[:-3]
                            parsed_json = json.loads(clean_text)
                            items = parsed_json.get("items", [])
                        except Exception as parse_err:
                            print(f"JSON Parse Error in batch {i//chunk_size}: {parse_err}. Attempting regex fallback...")
                            pattern = r'\{\s*"span"\s*:\s*"((?:[^"\\]|\\.)*)"\s*,\s*"translation"\s*:\s*"((?:[^"\\]|\\.)*)"\s*\}'
                            matches = re.findall(pattern, clean_text)
                            if not matches:
                                pattern_rev = r'\{\s*"translation"\s*:\s*"((?:[^"\\]|\\.)*)"\s*,\s*"span"\s*:\s*"((?:[^"\\]|\\.)*)"\s*\}'
                                matches = [(s, t) for t, s in re.findall(pattern_rev, clean_text)]
                            items = []
                            for s, t in matches:
                                try:
                                    s = json.loads(f'"{s}"')
                                    t = json.loads(f'"{t}"')
                                except Exception:
                                    pass
                                items.append({"span": s, "translation": t})
                            if not items:
                                print(f"Regex fallback failed to extract items. Response was: {response.text}")
                            else:
                                print(f"Regex fallback recovered {len(items)} items from malformed response.")
                        for item in items:
                            key = item.get("span", "").lower()
                            # 锚定校验 1: 确保返回的短语是我们需要翻译的短语 (防漂移)
                            if key in chunk:
                                # 锚定校验 2: 确保该短语确实存在于源文中 (防模型编造幻觉)
                                if key in source_text.lower():
                                    set_cached_translation(key, target_lang, doc_hash, item.get("translation", ""))
                                else:
                                    print(f"Key '{key}' not in source text")
                            else:
                                print(f"Key '{key}' not in chunk")
                except Exception as e:
                    print(f"Gemini translation batch {i//chunk_size} failed: {e}")
                
                # Sleep to prevent rate limiting
                import time
                time.sleep(2)
        except Exception as e:
            print(f"Gemini translation failed to initialize or execute: {e}")

    result = {}
    lang_prefix = target_lang.lower().split('-')[0]
    for key, meta in annotations.items():
        trans = get_cached_translation(key, target_lang, doc_hash) or ""
        entry = dict(meta)
        
        # Hydrate sub_words translation
        if "sub_words" in entry:
            hydrated_sub_words = []
            for sw in entry["sub_words"]:
                sw_key = sw["surface"].lower()
                sw_trans = get_cached_translation(sw_key, target_lang, doc_hash) or ""
                hydrated_sw = dict(sw)
                hydrated_sw["translation"] = sw_trans
                hydrated_sw["translation_status"] = "success" if sw_trans else "failed"
                hydrated_sub_words.append(hydrated_sw)
            entry["sub_words"] = hydrated_sub_words
        
        # Fetch metadata from LLM mock
        metadata = {}
        if lang_prefix == "fr" and entry["lemma"] in FR_LEXICAL_METADATA:
            metadata = FR_LEXICAL_METADATA[entry["lemma"]]
        elif lang_prefix == "es" and entry["lemma"] in ES_LEXICAL_METADATA:
            metadata = ES_LEXICAL_METADATA[entry["lemma"]]
            
        status = metadata.get("cognate_status", "unrelated")
        confidence = metadata.get("cognate_confidence", 1.0)
        
        if confidence < 0.8:
            status = "context_dependent"
            
        entry["semantic"] = {
            "translation": trans, 
            "translation_confidence": 0.9 if trans else 0.0,
            "cognate_status": status,
            "cognate_confidence": confidence,
            "translation_status": "success" if trans else "failed"
        }
        result[key] = entry
    return result

# ── API endpoint ──────────────────────────────────────────────────────
@app.post("/annotate")
def annotate_endpoint(req: AnnotationRequest, user = Depends(get_current_user)):
    """
    Returns the FULL annotation dictionary for the given text.
    Checks user quota before processing.
    """
    profile = get_user_profile(user.id)
    
    if not profile.get("is_subscribed"):
        limit = profile.get("daily_char_limit", 5000)
        char_count = len(req.text)
        result = try_consume_quota(user.id, char_count, limit)
        
        if result is None:
            today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            res = supabase.table("daily_usage").select("chars_used").eq("user_id", user.id).eq("usage_date", today_str).execute()
            current = res.data[0]["chars_used"] if res.data else 0
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "daily_quota_exceeded",
                    "message": "今日免费额度已用完",
                    "chars_used": current,
                    "chars_limit": limit,
                    "resets_at": "UTC 00:00 (北京时间 08:00)"
                }
            )
        chars_used = result
    else:
        chars_used = 0 # Subscribed users don't consume chars_used
        limit = profile.get("daily_char_limit", 50000) # Use actual limit instead of string

    annotations = analyze_text(req.text)
    adjusted = apply_target_lang_adjustments(annotations, req.target_lang)
    enriched = batch_translate(adjusted, req.target_lang, req.text)
    
    return {
        "dictionary": enriched,
        "usage": {
            "chars_used": chars_used,
            "chars_limit": limit
        }
    }

