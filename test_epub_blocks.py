"""find_text_blocks 的规格测试。运行：python test_epub_blocks.py   （也兼容 pytest）
通过这些测试 = 满足 Plan 1 的核心要求。"""
from lxml import etree
from epub_blocks import find_text_blocks, WRAP_CLASS

NS = 'xmlns="http://www.w3.org/1999/xhtml"'


def parse(body_xml, ns=True):
    return etree.fromstring(('<html %s><head><title>t</title></head><body>%s</body></html>' % (NS if ns else "", body_xml)).encode())


def texts(blocks):
    return ["".join(b.itertext()).strip() for b in blocks]


def ser(tree):
    return etree.tostring(tree)


def test_standard_book_dom_untouched():
    t = parse('\n<h1>Chapter One</h1>\n<div class="c">\n  <p>First paragraph text.</p>\n  <p>Second paragraph text.</p>\n</div>\n')
    before = ser(t)
    blocks = find_text_blocks(t)
    assert texts(blocks) == ["First paragraph text.", "Second paragraph text."]
    assert ser(t) == before, "标准书的 DOM 不能有任何变化"


def test_div_only_book_is_selected():
    t = parse('<h1>T</h1><div>First paragraph text.</div><div>Second paragraph text.</div>')
    before = ser(t)
    assert texts(find_text_blocks(t)) == ["First paragraph text.", "Second paragraph text."]
    assert ser(t) == before


def test_nested_list_in_div_selected_once():
    t = parse('<div><ul><li>Item one text here</li></ul></div>')
    assert texts(find_text_blocks(t)) == ["Item one text here"]


def test_comments_and_pi_do_not_crash():
    t = parse('<!-- c --><?pi x?><p>Hello world text.</p><!-- d -->')
    assert texts(find_text_blocks(t)) == ["Hello world text."]


def test_mixed_inline_run_wrapped_in_one_span_no_p():
    t = parse('<div>Hello <em>big</em> world<p>Para two text.</p></div>')
    blocks = find_text_blocks(t)
    assert texts(blocks) == ["Hello big world", "Para two text."]
    assert blocks[0].tag.endswith("span") and blocks[0].get("class") == WRAP_CLASS
    assert b"<p" in ser(t) and ser(t).count(b"<p") == 1, "不能新插入 <p>"
    assert b"<span" in ser(t) and b"<em>big</em>" in ser(t)


def test_inline_only_run_is_wrapped():
    t = parse('<div><em>Intro</em><p>Body text.</p></div>')
    assert texts(find_text_blocks(t)) == ["Intro", "Body text."]


def test_headings_untouched_and_not_selected():
    t = parse('<div>Preface text<h2>Title</h2>More text after</div>')
    blocks = find_text_blocks(t)
    assert texts(blocks) == ["Preface text", "More text after"]
    assert b"<h2>Title</h2>" in ser(t)


def test_blocks_never_nested():
    t = parse('<div>Lead text<blockquote><p>Quoted para text.</p></blockquote>tail words<ul><li>One item text</li></ul></div><div><div>Inner div text</div></div>')
    blocks = find_text_blocks(t)
    for a in blocks:
        for b in blocks:
            assert a is b or a not in list(b.iterancestors()), "任何两个块都不能是祖先和后代"
    assert texts(blocks) == ["Lead text", "Quoted para text.", "tail words", "One item text", "Inner div text"]


def test_wrapper_keeps_document_namespace():
    t = parse('<div>Lead text<p>Para text here.</p></div>')
    blocks = find_text_blocks(t)
    assert blocks[0].tag == "{http://www.w3.org/1999/xhtml}span"


def test_table_cells_not_selected_by_design():
    t = parse('<table><tr><td>Cell text here</td></tr></table>')
    assert find_text_blocks(t) == []


def test_script_and_style_text_never_selected():
    t = parse('<div>Visible text here<script>var x = 1;</script><style>p{}</style></div>')
    assert texts(find_text_blocks(t)) == ["Visible text here"]


def test_bare_body_text():
    t = parse('Line one<br/>Line two')
    assert texts(find_text_blocks(t)) == ["Line one" + "Line two"]


def test_short_text_not_filtered_here():
    t = parse('<p>Hi.</p><p>Spartans.</p>')
    assert texts(find_text_blocks(t)) == ["Hi.", "Spartans."]


def test_idempotent():
    t = parse('<div>Hello <em>big</em> world<p>Para two text.</p></div>')
    first = texts(find_text_blocks(t)); s1 = ser(t)
    second = texts(find_text_blocks(t))
    assert first == second and ser(t) == s1, "重复调用不能再次包裹"


def test_no_namespace_document():
    t = parse('<div>Lead text<p>Para text here.</p></div>', ns=False)
    assert texts(find_text_blocks(t)) == ["Lead text", "Para text here."]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    bad = 0
    for f in fns:
        try:
            f(); print("[PASS]", f.__name__)
        except Exception as e:
            bad += 1; print("[FAIL]", f.__name__, "->", type(e).__name__, e)
    print("\n%d 个测试，%d 个失败" % (len(fns), bad))
    raise SystemExit(1 if bad else 0)
