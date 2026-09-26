"""
ReadLevel — EPUB Ingestion & Generation Pipeline (Track 3)

Two-pass streaming pipeline for processing EPUB books:
  - Pass 1: Streaming scan of each chapter XHTML -> NLP analysis -> global deduplicated candidate vocabulary
  - Phase 1.5: Whole-book deduplicated batch translation via Gemini (bound to stable book_hash)
  - Pass 2: Streaming DOM injection of <span class="annotation-view"> tags + inline <style> -> re-pack EPUB

Preserves original EPUB format, metadata, toc.ncx, and assets without loading entire book into memory.
"""
import zipfile
import os
import re
import gc
from collections import Counter
import hashlib
from xml.sax.saxutils import escape
import lxml.etree as ET

import backend

# ── Hard cap on candidate vocabulary to bound Gemini API usage ────────
MAX_CANDIDATES = 8000

# ── Inline CSS injected into each modified chapter's <head> ─────────────────
READLEVEL_INLINE_CSS = """
/* ── ReadLevel EPUB Annotations ── */
.annotation-view {
  position: relative;
  display: inline-block;
  white-space: nowrap;
}
.annotation-rt {
  position: absolute;
  left: 50%;
  transform: translateX(-50%);
  top: -0.9em;
  font-size: 0.55em;
  color: #a1a1aa;
  font-weight: 500;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  letter-spacing: 0.01em;
  pointer-events: none;
  display: block;  /* 网页版默认是 none、靠 JS 矩阵按阈值显示；EPUB 没有这套机制，所以这一行保持跟原来一样写死 block，不要改成 none，否则注音会全部消失 */
}
.annotation-phrase {
  text-decoration: underline;
  text-decoration-color: #3f3f46;
  text-decoration-thickness: 1px;
  text-underline-offset: 3px;
}
.annotation-phrase > .annotation-rt { color: #60a5fa; }
.annotation-entity {
  text-decoration: underline;
  text-decoration-color: #44403c;
  text-decoration-thickness: 1px;
  text-underline-offset: 3px;
}
.annotation-entity > .annotation-rt { color: #fbbf24; }
.annotation-word > .annotation-rt { color: #fde047; }
.annotation-view[data-translation-failed="true"] > .annotation-rt {
  color: #dc2626;
}
.annotated-para {
  line-height: 2.6 !important;
}
h1 + p, h2 + p, h3 + p, h4 + p, h5 + p, h6 + p {
  padding-top: 1.5em;
}
"""

def get_epub_chapter_paths(zip_in: zipfile.ZipFile) -> list[str]:
    """
    Parses META-INF/container.xml and content.opf to retrieve ordered chapter XHTML paths.
    """
    # 1. Locate OPF file path from container.xml
    try:
        container_xml = zip_in.read("META-INF/container.xml")
        container_root = ET.fromstring(container_xml)
        rootfile_elem = container_root.xpath(
            "//*[local-name()='rootfile'][@media-type='application/oebps-package+xml']"
        )
        if not rootfile_elem:
            rootfile_elem = container_root.xpath("//*[local-name()='rootfile']")
        if not rootfile_elem:
            raise ValueError("No rootfile element found in META-INF/container.xml")
        opf_path = rootfile_elem[0].get("full-path")
    except Exception as e:
        raise ValueError(f"Failed to parse META-INF/container.xml: {e}")

    opf_dir = os.path.dirname(opf_path)

    # 2. Parse OPF manifest & spine
    try:
        opf_xml = zip_in.read(opf_path)
        opf_root = ET.fromstring(opf_xml)

        # Build id -> href map from manifest
        manifest = {}
        for item in opf_root.xpath("//*[local-name()='manifest']/*[local-name()='item']"):
            item_id = item.get("id")
            href = item.get("href")
            media_type = item.get("media-type", "")
            if item_id and href:
                manifest[item_id] = (href, media_type)

        # Read spine order
        chapter_paths = []
        for itemref in opf_root.xpath("//*[local-name()='spine']/*[local-name()='itemref']"):
            idref = itemref.get("idref")
            if idref in manifest:
                href, media_type = manifest[idref]
                # Filter for HTML/XHTML content documents
                if "html" in media_type.lower() or href.lower().endswith((".xhtml", ".html", ".htm")):
                    full_path = os.path.normpath(os.path.join(opf_dir, href)).replace("\\", "/")
                    chapter_paths.append(full_path)

        return chapter_paths
    except Exception as e:
        raise ValueError(f"Failed to parse OPF package at {opf_path}: {e}")


def find_phrases_and_words(text: str, candidate_dict: dict[str, dict]) -> list[dict]:
    """
    Greedy longest-match tokenizer matching candidate phrases and words against text.
    Ported directly from TypeScript findPhrasesAndWords in annotator.tsx.
    """
    phrase_keys = [k for k in candidate_dict if " " in k or "-" in k]
    phrase_keys.sort(key=len, reverse=True)
    word_keys = {k for k in candidate_dict if " " not in k and "-" not in k}

    remaining = text
    chunks = []

    while remaining:
        matched_phrase = False
        lower_rem = remaining.lower()

        # 1. Multi-word phrases
        for phrase in phrase_keys:
            if lower_rem.startswith(phrase):
                next_char = remaining[len(phrase)] if len(remaining) > len(phrase) else ""
                if not next_char or not next_char.isalpha():
                    chunks.append({
                        "text": remaining[:len(phrase)],
                        "is_match": True,
                        "entry": candidate_dict[phrase]
                    })
                    remaining = remaining[len(phrase):]
                    matched_phrase = True
                    break
        if matched_phrase:
            continue

        # 2. Hyphenated compounds
        m_hyphen = re.match(r"^([a-zA-Z]+-(?:[a-zA-Z]+-)*[a-zA-Z]+)", remaining)
        if m_hyphen:
            compound = m_hyphen.group(1)
            lower_comp = compound.lower()
            if lower_comp in candidate_dict:
                chunks.append({"text": compound, "is_match": True, "entry": candidate_dict[lower_comp]})
            else:
                chunks.append({"text": compound, "is_match": False})
            remaining = remaining[len(compound):]
            continue

        # 3. Single word
        m_word = re.match(r"^([a-zA-Z]+)", remaining)
        if m_word:
            word = m_word.group(1)
            lower_word = word.lower()
            if lower_word in word_keys:
                chunks.append({"text": word, "is_match": True, "entry": candidate_dict[lower_word]})
            else:
                chunks.append({"text": word, "is_match": False})
            remaining = remaining[len(word):]
            continue

        # 4. Non-word characters (whitespace, punctuation)
        m_non_word = re.match(r"^([^a-zA-Z]+)", remaining)
        if m_non_word:
            chunks.append({"text": m_non_word.group(1), "is_match": False})
            remaining = remaining[len(m_non_word.group(1)):]
        else:
            chunks.append({"text": remaining[0], "is_match": False})
            remaining = remaining[1:]

    return chunks


def build_annotation_span_xml(chunk_text: str, entry: dict, trans: str, is_failed: bool) -> str:
    """Constructs well-formed XML for an annotated word or phrase span."""
    kind = entry.get("kind") or entry.get("type") or "word"
    raw_level = (
        entry.get("difficulty", {}).get("adjusted_level")
        or entry.get("difficulty", {}).get("base_level")
        or 500
    )
    level = round(raw_level / 50) * 50
    pos = entry.get("pos", "")
    
    # Format rt content
    if trans:
        rt_content = f"{trans} {pos}".strip() if (kind == "word" and pos) else trans
    elif is_failed:
        rt_content = "⚠️"
    else:
        rt_content = ""

    escaped_text = escape(chunk_text)
    escaped_rt = escape(rt_content)
    fail_attr = ' data-translation-failed="true"' if is_failed else ""

    return (
        f'<span class="annotation-view annotation-{kind}" data-level="{level}"{fail_attr}>'
        f"{escaped_text}"
        f'<span class="annotation-rt">{escaped_rt}</span>'
        f"</span>"
    )


def replace_text_node_with_markup(parent: ET._Element, is_tail: bool, child_idx: int, replacement_xml: str) -> int:
    """
    Replaces parent.text (is_tail=False) or parent[child_idx].tail (is_tail=True)
    with the parsed elements and text from an XML fragment <root>...</root>.
    Returns the number of injected child elements.
    """
    frag = ET.fromstring(replacement_xml)
    if not is_tail:
        parent.text = frag.text
        for i, child in enumerate(frag):
            parent.insert(i, child)
    else:
        target = parent[child_idx]
        target.tail = frag.text
        for i, child in enumerate(frag):
            parent.insert(child_idx + 1 + i, child)
    return len(frag)


def annotate_text_segment(text: str, candidate_dict: dict[str, dict], translations_cache: dict[str, str],
                          chapter_counts: dict[str, int], per_chapter_limit: int) -> tuple[str, bool]:
    """
    Annotates a text string into an XML fragment string <_root_>...</_root_>.
    Returns (xml_fragment_str, has_modifications).
    """
    chunks = find_phrases_and_words(text, candidate_dict)
    has_match = False
    xml_parts = ["<_root_>"]

    for chunk in chunks:
        if chunk["is_match"] and chunk.get("entry"):
            key = chunk["text"].lower()
            count = chapter_counts.get(key, 0)
            if count < per_chapter_limit:
                chapter_counts[key] = count + 1
                entry = chunk["entry"]
                trans = translations_cache.get(key, "")
                is_failed = not bool(trans)
                span_xml = build_annotation_span_xml(chunk["text"], entry, trans, is_failed)
                xml_parts.append(span_xml)
                has_match = True
                continue
        # Non-match or frequency limit reached: keep original text escaped
        xml_parts.append(escape(chunk["text"]))

    xml_parts.append("</_root_>")
    return "".join(xml_parts), has_match


def annotate_block_element(elem: ET._Element, candidate_dict: dict[str, dict], translations_cache: dict[str, str],
                           chapter_counts: dict[str, int], per_chapter_limit: int) -> bool:
    """
    Traverses and annotates text within an element (elem.text and each child.tail).
    Returns True if any annotations were injected in this element or its descendants.
    """
    has_any_match = False
    idx = 0
    # 1. Process elem.text
    if elem.text and elem.text.strip():
        frag_xml, modified = annotate_text_segment(
            elem.text, candidate_dict, translations_cache, chapter_counts, per_chapter_limit
        )
        if modified:
            has_any_match = True
            idx = replace_text_node_with_markup(elem, is_tail=False, child_idx=0, replacement_xml=frag_xml)

    # 2. Process children
    # We iterate by index because children may be added during replacement
    while idx < len(elem):
        child = elem[idx]
        
        # Recursively process children unless it's an existing annotation or non-content element
        tag_name = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        is_own_span = (tag_name == "span" and "annotation-view" in (child.get("class") or ""))
        if tag_name not in ("script", "style", "pre", "svg", "h1", "h2", "h3", "h4", "h5", "h6") and not is_own_span:
            child_matched = annotate_block_element(child, candidate_dict, translations_cache, chapter_counts, per_chapter_limit)
            if child_matched:
                has_any_match = True

        injected_count = 0
        # Process child.tail
        if child.tail and child.tail.strip():
            frag_xml, modified = annotate_text_segment(
                child.tail, candidate_dict, translations_cache, chapter_counts, per_chapter_limit
            )
            if modified:
                has_any_match = True
                injected_count = replace_text_node_with_markup(elem, is_tail=True, child_idx=idx, replacement_xml=frag_xml)

        idx += 1 + injected_count

    return has_any_match


# ── Two-Pass Engine ─────────────────────────────────────────────────────────

def run_pass1_scan_vocabulary(zip_in: zipfile.ZipFile, chapter_paths: list[str],
                              difficulty_threshold: int) -> dict[str, dict]:
    """
    Pass 1: Stream-reads each chapter, extracts visible text paragraphs,
    runs analyze_text, and collects all candidate words/phrases >= difficulty_threshold.
    Returns global candidate_meta dictionary, truncated to MAX_CANDIDATES by frequency.
    """
    candidate_meta: dict[str, dict] = {}
    candidate_freq: Counter = Counter()  # 统计每个 key 在全书中出现的段落次数

    for chapter_path in chapter_paths:
        try:
            raw_bytes = zip_in.read(chapter_path)
            tree = ET.fromstring(raw_bytes, parser=ET.XMLParser(recover=True))
            
            # Strip header tags so their text isn't extracted when getting div.itertext()
            for h in tree.xpath("//*[local-name()='h1' or local-name()='h2' or local-name()='h3' or local-name()='h4' or local-name()='h5' or local-name()='h6']"):
                parent = h.getparent()
                if parent is not None:
                    parent.remove(h)
                
            # Find all text-bearing paragraph elements (p, li, blockquote)
            # Note: 同 Pass 2，排除 div 避免将外层章节大容器整章文本合并不必要地送入 NLP。
            paragraphs = tree.xpath(
                "//*[local-name()='p' or local-name()='li' or local-name()='blockquote']"
            )
            for p in paragraphs:
                p_text = "".join(p.itertext()).strip()
                if not p_text or len(p_text) < 3:
                    continue

                # Run NLP analysis on paragraph
                annotations = backend.analyze_text(p_text)
                for key, meta in annotations.items():
                    raw_level = (
                        meta.get("difficulty", {}).get("adjusted_level")
                        or meta.get("difficulty", {}).get("base_level")
                        or 500
                    )
                    
                    # Also check if any sub_word is difficult enough
                    # Note: 如果散词达标，我们会把整个短语（包括其中未达标的邻近词）一并纳入词表并完整标注。
                    # 这是单层扁平 DOM 设计下的已知近似策略，并非对网页版逐词独立渲染的精确复刻。
                    max_sw_level = 0
                    for sw in meta.get("sub_words", []):
                        sw_level = sw.get("level", 500)
                        if sw_level > max_sw_level:
                            max_sw_level = sw_level

                    if raw_level >= difficulty_threshold or max_sw_level >= difficulty_threshold:
                        candidate_freq[key] += 1
                        if key not in candidate_meta:
                            candidate_meta[key] = meta

            # Immediate cleanup of DOM
            del tree
        except Exception as e:
            print(f"Warning: Pass 1 failed to scan chapter {chapter_path}: {e}")
            continue

    # ── 全书扫描完毕后，按出现频率排序截断至 MAX_CANDIDATES ──
    pre_truncate_count = len(candidate_meta)
    if pre_truncate_count > MAX_CANDIDATES:
        # 取出现频率最高的 MAX_CANDIDATES 个词
        top_keys = {k for k, _ in candidate_freq.most_common(MAX_CANDIDATES)}
        candidate_meta = {k: v for k, v in candidate_meta.items() if k in top_keys}
        print(f"[TRUNCATE] Candidate vocabulary truncated from {pre_truncate_count} to {len(candidate_meta)} (limit={MAX_CANDIDATES})")
    else:
        print(f"[PASS1] Candidate vocabulary size: {pre_truncate_count} (within limit={MAX_CANDIDATES})")

    gc.collect()
    return candidate_meta


def run_phase15_translate(candidate_meta: dict[str, dict], target_lang: str, book_hash: str):
    """
    Phase 1.5: Batch translates all deduplicated candidates using backend.batch_translate.
    All translations are persisted to SQLite translation_cache.db under book_hash.
    """
    if not candidate_meta:
        return
    # Use skip_source_check=True so anchor Check 2 (source text containment) is relaxed
    # for whole-book vocabulary while Check 1 (chunk containment) remains in full force.
    backend.batch_translate(
        candidate_meta,
        target_lang=target_lang,
        source_text="",
        doc_hash=book_hash,
        skip_source_check=True
    )


def run_pass2_transform_and_pack(zip_in: zipfile.ZipFile, output_epub_path: str,
                                 chapter_paths: list[str], candidate_meta: dict[str, dict],
                                 translations_cache: dict[str, str],
                                 per_chapter_limit: int = 2) -> dict:
    """
    Pass 2: Re-streams the input EPUB into output_epub_path.
    Copies all media, metadata, and stylesheets byte-for-byte.
    Injects inline CSS and <span class="annotation-view"> tags into each chapter XHTML.
    """
    chapter_path_set = set(chapter_paths)
    stats = {
        "total_chapters": len(chapter_paths),
        "total_unique_words": len(candidate_meta),
        "annotated_count": 0,
        "failed_words": []
    }

    # Open target zip for writing
    with zipfile.ZipFile(output_epub_path, "w", compression=zipfile.ZIP_DEFLATED) as zip_out:
        # 1. EPUB OCF Requirement: First file must be 'mimetype' stored uncompressed
        zip_out.writestr("mimetype", b"application/epub+zip", compress_type=zipfile.ZIP_STORED)

        # 2. Iterate through all items in source zip
        for item in zip_in.infolist():
            filename = item.filename
            if filename == "mimetype":
                continue

            raw_bytes = zip_in.read(filename)

            # If not a content chapter, copy verbatim
            if filename not in chapter_path_set:
                zip_out.writestr(item, raw_bytes)
                continue

            # Transform chapter XHTML
            try:
                tree = ET.fromstring(raw_bytes, parser=ET.XMLParser(recover=True, remove_blank_text=False))
                chapter_counts: dict[str, int] = {}

                # Injected inline <style> into <head>
                head_elem = tree.xpath("//*[local-name()='head']")
                if head_elem:
                    style_elem = ET.Element("style")
                    style_elem.attrib["type"] = "text/css"
                    style_elem.text = ET.CDATA(READLEVEL_INLINE_CSS)
                    head_elem[0].append(style_elem)

                # Process content block tags: p, li, blockquote
                # Note: div 被有意排除在 content_blocks 之外（已知取舍）：
                # 很多 EPUB 会用整章外层包裹 <div class="chapter">，若将 div 纳入 content_blocks，
                # 其内部任意段落命中难词均会导致外层大容器被贴上 .annotated-para，从而将整个章节（包括无生词段落）
                # 的行高全局拉宽至 2.6，破坏原书正常排版。因此仅对语义段落标签 (p, li, blockquote) 进行独立标注与行高控制。
                content_blocks = tree.xpath(
                    "//*[local-name()='p' or local-name()='li' or local-name()='blockquote']"
                )
                for block in content_blocks:
                    block_matched = annotate_block_element(
                        block, candidate_meta, translations_cache, chapter_counts, per_chapter_limit
                    )
                    if block_matched:
                        existing_class = block.get("class") or ""
                        block.set("class", f"{existing_class} annotated-para".strip())

                stats["annotated_count"] += sum(chapter_counts.values())

                # Serialize modified XHTML
                modified_bytes = ET.tostring(tree, encoding="utf-8", xml_declaration=True, method="xml")
                zip_out.writestr(filename, modified_bytes)

                del tree
            except Exception as e:
                print(f"Warning: Failed to annotate chapter {filename}, copying verbatim: {e}")
                zip_out.writestr(filename, raw_bytes)

            gc.collect()

    return stats


def process_epub_file(input_epub_path: str, output_epub_path: str,
                      target_lang: str = "zh-Hans", difficulty_level: int = 900,
                      per_chapter_limit: int = 2,
                      progress_callback=None) -> dict:
    """
    High-level entry point executing the full two-pass pipeline on an EPUB file.
    progress_callback(percent: int, step_desc: str) optional.
    """
    # 1. Compute stable book_hash from raw input file bytes
    with open(input_epub_path, "rb") as f:
        raw_epub_bytes = f.read()
    book_hash = hashlib.sha256(raw_epub_bytes).hexdigest()[:16]

    with zipfile.ZipFile(input_epub_path, "r") as zip_in:
        # 2. Extract spine chapters
        if progress_callback:
            progress_callback(5, "正在解析 EPUB 目录与章节结构...")
        chapter_paths = get_epub_chapter_paths(zip_in)
        if not chapter_paths:
            raise ValueError("未能从 EPUB 文件中解析出有效的章节内容。")

        # 3. Pass 1: Scan & Deduplicate Vocabulary
        if progress_callback:
            progress_callback(15, f"正在扫描全书章节并提取难词 (共 {len(chapter_paths)} 章)...")
        candidate_meta = run_pass1_scan_vocabulary(zip_in, chapter_paths, difficulty_level)

        # 4. Phase 1.5: Batch Translate
        print(f"[DEBUG] candidate_meta size: {len(candidate_meta)}")
        if progress_callback:
            progress_callback(40, f"正在批量翻译全书生词 (去重后共 {len(candidate_meta)} 词)...")
        run_phase15_translate(candidate_meta, target_lang, book_hash)

        # 4.5 Load all translations into memory dictionary
        translations_cache = {}
        with backend.get_db_connection() as conn:
            cursor = conn.execute("SELECT key, translation FROM translations WHERE target_lang=? AND doc_hash=?", (target_lang, book_hash))
            for row in cursor:
                translations_cache[row[0]] = row[1]
                
        # Also ensure global fallbacks exist
        for key in candidate_meta:
            if key not in translations_cache:
                trans = backend.get_cached_translation(key, target_lang, book_hash) or ""
                translations_cache[key] = trans

        # 5. Pass 2: Annotate & Repack
        if progress_callback:
            progress_callback(75, "正在流式注入注音标签并重新打包 EPUB...")
        stats = run_pass2_transform_and_pack(
            zip_in, output_epub_path, chapter_paths, candidate_meta,
            translations_cache, per_chapter_limit
        )

        if progress_callback:
            progress_callback(100, "EPUB 生成完毕！")

        return {
            "book_hash": book_hash,
            "total_chapters": len(chapter_paths),
            "total_unique_words": len(candidate_meta),
            "annotated_count": stats["annotated_count"]
        }
