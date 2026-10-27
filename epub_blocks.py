"""EPUB 正文块定位——Pass 1 与 Pass 2 必须共用这一个函数。
纯 lxml，不依赖 spaCy，可以单独测试。

规则（与 test_epub_blocks.py 一一对应）：
1. 不删除、不改动标题、脚本、样式等任何节点；它们只是"不是正文"。
2. 正文块 = 候选标签里"后代中没有任何块级元素"的叶子，所以块与块之间互不包含，同一段文字只会出现在一个块里。
3. 容器里既有块级子元素、又有裸文字或行内元素时，把两个块之间连续的"文字 + 行内元素"整段包进
   一个行内 <span class="rl-leaf-block">（绝不插入 <p>，避免段落外边距改变排版）。
4. 空白不包裹；容器里没有需要包裹的内容时 DOM 完全不动，所以标准 <p> 书的输出不变。
5. 这里不做字数过滤；"有效段落至少 10 个字符"只属于 Pass 1 的计数。
这次不支持：表格单元格 td/th、figcaption 里的文字（它们是块级，但不是候选）。
"""
from lxml import etree

BLOCK_TAGS = frozenset({
    "p", "div", "li", "blockquote", "dd", "dt", "dl", "section", "article", "header", "footer",
    "aside", "nav", "figure", "figcaption", "ul", "ol", "table", "tbody", "thead", "tfoot",
    "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "hr",
    "script", "style", "svg", "math",
})
CONTENT_TAGS = frozenset({"p", "div", "li", "blockquote", "dd", "dt", "body"})
WRAP_CLASS = "rl-leaf-block"


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _is_block(node):
    return isinstance(node.tag, str) and _local(node.tag) in BLOCK_TAGS


def _el_text(node):
    return "".join(node.itertext()) if isinstance(node.tag, str) else ""


def _run_has_text(run):
    return any((k == "t" and v.strip()) or (k == "i" and _el_text(v).strip()) for k, v in run)


def _is_wrapper(node):
    return isinstance(node.tag, str) and _local(node.tag) == "span" and node.get("class") == WRAP_CLASS


def _needs_wrap(run):
    """这一段是否需要包进 span：有实质文字，并且不是"已经包好的单个 span 加空白"。"""
    if not _run_has_text(run):
        return False
    elems = [v for k, v in run if k == "i"]
    texts = [v for k, v in run if k == "t"]
    already = len(elems) == 1 and _is_wrapper(elems[0]) and not any(t.strip() for t in texts)
    return not already


def _wrap_mixed_content(container):
    kids = list(container)
    if not any(_is_block(c) for c in kids):
        return
    items = []
    if container.text:
        items.append(("t", container.text))
    for c in kids:
        items.append(("b" if _is_block(c) else "i", c))
        if c.tail:
            items.append(("t", c.tail))
    segs, run = [], []
    for kind, v in items:
        if kind == "b":
            if run:
                segs.append(("run", run)); run = []
            segs.append(("block", v))
        else:
            run.append((kind, v))
    if run:
        segs.append(("run", run))
    if not any(s == "run" and _needs_wrap(v) for s, v in segs):
        return  # 没有需要包裹的内容：DOM 保持原样
    ns = etree.QName(container).namespace
    span_tag = "{%s}span" % ns if ns else "span"
    container.text = None
    for c in kids:
        c.tail = None
        container.remove(c)

    def add_text(parent, last, text):
        if last is None:
            parent.text = (parent.text or "") + text
        else:
            last.tail = (last.tail or "") + text

    last = None
    for s, v in segs:
        if s == "block":
            container.append(v); last = v
        elif _needs_wrap(v):
            span = etree.SubElement(container, span_tag)
            span.set("class", WRAP_CLASS)
            slast = None
            for k, x in v:
                if k == "t":
                    add_text(span, slast, x)
                else:
                    span.append(x); slast = x
            last = span
        else:
            for k, x in v:
                if k == "t":
                    add_text(container, last, x)
                else:
                    container.append(x); last = x


def find_text_blocks(root):
    """返回文档顺序的正文块列表。可能为混合内容容器就地加入 <span class="rl-leaf-block">。"""
    if isinstance(root, etree._ElementTree):
        root = root.getroot()
    body = root if _local(root.tag) == "body" else next(iter(root.iter("{*}body")), None)
    scope = body if body is not None else root
    for el in [e for e in scope.iter() if isinstance(e.tag, str)]:
        if _local(el.tag) in BLOCK_TAGS or _local(el.tag) == "body":
            _wrap_mixed_content(el)
    elems = [e for e in scope.iter() if isinstance(e.tag, str)]
    has_block_desc = {}
    for e in reversed(elems):  # 文档顺序反过来：先处理后代，再处理祖先
        has_block_desc[e] = any(
            isinstance(c.tag, str) and (_local(c.tag) in BLOCK_TAGS or has_block_desc.get(c, False))
            for c in e)
    out = []
    for e in elems:
        t = _local(e.tag)
        candidate = t in CONTENT_TAGS or (t == "span" and e.get("class") == WRAP_CLASS)
        if candidate and not has_block_desc[e] and _el_text(e).strip():
            out.append(e)
    return out
