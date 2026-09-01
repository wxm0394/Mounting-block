"""
ReadLevel Engine — Context-aware difficulty-adaptive bilingual annotation backend.

Architecture:
  - Difficulty Score (0~100 internal) ← f(word_freq, phrase_freq, POS, dep_complexity)
  - Display Level (500~1500 UI) ← linear mapping from Difficulty Score
  - Annotation IR: structured intermediate representation for each annotated span
  - Multi-pass pipeline: PASS 0 (NER/PROPN) → 1 (hyphenated) → 2 (lexicalized phrases) → 3 (adjacent merge) → 4 (single words)
  - API returns FULL dictionary regardless of threshold; frontend handles filtering.
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import re
import spacy
from wordfreq import zipf_frequency
import concurrent.futures

nlp = spacy.load("en_core_web_sm")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class AnnotationRequest(BaseModel):
    text: str
    target_lang: str = "zh-Hans"   # target native language for translation

import sqlite3
import os

CACHE_DB_PATH = os.path.join(os.path.dirname(__file__), "translation_cache.db")

def init_db():
    with sqlite3.connect(CACHE_DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS translations (
                key TEXT,
                target_lang TEXT,
                translation TEXT,
                PRIMARY KEY (key, target_lang)
            )
        """)

init_db()

def get_cached_translation(key: str, target_lang: str) -> str:
    with sqlite3.connect(CACHE_DB_PATH) as conn:
        cursor = conn.execute("SELECT translation FROM translations WHERE key=? AND target_lang=?", (key, target_lang))
        row = cursor.fetchone()
        return row[0] if row else None

def set_cached_translation(key: str, target_lang: str, translation: str):
    with sqlite3.connect(CACHE_DB_PATH) as conn:
        conn.execute("INSERT OR REPLACE INTO translations (key, target_lang, translation) VALUES (?, ?, ?)",
                     (key, target_lang, translation))

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
                annotations[chunk_text] = make_annotation(
                    chunk_text, lemma, "entity", 1500, pos="n."
                )
            else:
                annotations[chunk_text] = make_annotation(
                    chunk_text, lemma, "entity", 1500
                )
            for i in range(chunk.start, chunk.end):
                consumed.add(i)

    for token in doc:
        if token.pos_ == "PROPN" and token.i not in consumed:
            annotations[token.text.lower()] = make_annotation(
                token.text.lower(), token.lemma_.lower(), "entity", 1500, pos="n."
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
    to_translate = []
    # Collect all top-level keys
    for key in annotations:
        if get_cached_translation(key, target_lang) is None:
            to_translate.append(key)
        # Also collect all sub_words
        for sw in annotations[key].get("sub_words", []):
            sw_key = sw["surface"].lower()
            if get_cached_translation(sw_key, target_lang) is None and sw_key not in to_translate:
                to_translate.append(sw_key)

    if to_translate:
        client = genai.Client() # Assumes GEMINI_API_KEY is in env
        chunk_size = 50
        for i in range(0, len(to_translate), chunk_size):
            chunk = to_translate[i:i + chunk_size]
            try:
                prompt = f"""
Translate the following English words/phrases into {target_lang} based on their context in the source text.
Return ONLY valid JSON matching the schema.

Source text:
{source_text}

Words/Phrases to translate:
{json.dumps(chunk)}
"""
                response = client.models.generate_content(
                    model='gemini-2.5-flash',
                    contents=prompt,
                    config=genai.types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=TranslationBatch,
                        temperature=0.1
                    )
                )
                
                if response.parsed:
                    for item in response.parsed.items:
                        key = item.span.lower()
                        # 锚定校验 1: 确保返回的短语是我们需要翻译的短语 (防漂移)
                        if key in chunk:
                            # 锚定校验 2: 确保该短语确实存在于源文中 (防模型编造幻觉)
                            if key in source_text.lower():
                                set_cached_translation(key, target_lang, item.translation)
            except Exception as e:
                print(f"Gemini translation batch {i//chunk_size} failed: {e}")

    result = {}
    lang_prefix = target_lang.lower().split('-')[0]
    for key, meta in annotations.items():
        trans = get_cached_translation(key, target_lang) or ""
        entry = dict(meta)
        
        # Hydrate sub_words translation
        if "sub_words" in entry:
            hydrated_sub_words = []
            for sw in entry["sub_words"]:
                sw_key = sw["surface"].lower()
                sw_trans = get_cached_translation(sw_key, target_lang) or ""
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
def annotate_endpoint(req: AnnotationRequest):
    """
    Returns the FULL annotation dictionary for the given text.
    No threshold filtering — the frontend handles display level filtering via CSS/JS.
    """
    annotations = analyze_text(req.text)
    adjusted = apply_target_lang_adjustments(annotations, req.target_lang)
    enriched = batch_translate(adjusted, req.target_lang, req.text)
    return {"dictionary": enriched}
