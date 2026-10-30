"""Global test configuration and fixtures.

Ensures:
  1. Project root is on sys.path for test discovery.
  2. Proxy environment variables that break httpx in test sandboxes are cleared.
  3. spaCy model falls back to a lightweight mock pipeline when 'en_core_web_sm'
     is not downloaded locally, enabling fast offline unit testing without crashing
     on missing model weights.
"""
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

# 1. Project root in sys.path
ROOT_DIR = str(Path(__file__).parent.resolve())
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

# 2. Clear proxy env vars that trip up httpx inside supabase-py
for _var in (
    "http_proxy", "https_proxy", "all_proxy",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "no_proxy", "NO_PROXY"
):
    os.environ.pop(_var, None)

# 3. Fallback mock for spaCy if en_core_web_sm is not installed locally
try:
    import spacy
    try:
        spacy.load("en_core_web_sm")
    except Exception:
        _mock_doc = MagicMock()
        _mock_doc.noun_chunks = []
        _mock_doc.ents = []
        _mock_doc.__iter__.return_value = []
        _mock_nlp = MagicMock(return_value=_mock_doc)
        _mock_nlp.max_length = 1000000
        spacy.load = MagicMock(return_value=_mock_nlp)
except ImportError:
    pass
