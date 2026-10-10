"""
retriever.py - pluggable retrieval over the MLR-approved content corpus.

Default backend is scikit-learn TF-IDF + cosine similarity. At ~60 documents
lexical retrieval is genuinely adequate, it is instant, and it requires no model
download - which matters because this demo has to run on a small VPS.

The same `Retriever` interface takes an embedding backend; an optional
Gemini embedding retriever is included and selected with RETRIEVER=gemini.
"""

import os
import re

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity


def _split_sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text or "") if s.strip()]


def best_excerpt(query, body, max_sentences=2):
    """Pick the sentences of `body` that overlap the query most."""
    terms = set(re.findall(r"[a-z0-9]+", (query or "").lower()))
    terms = {t for t in terms if len(t) > 2}
    sents = _split_sentences(body)
    if not sents:
        return ""
    # The first sentence is a title echo and the last is a fixed "refer to the
    # source publication" closer. Both match every query and say nothing, so
    # they are excluded whenever there is real body text to choose from.
    body_sents = sents[1:-1] if len(sents) > 3 else sents
    scored = []
    for i, s in enumerate(body_sents):
        words = set(re.findall(r"[a-z0-9]+", s.lower()))
        scored.append((len(terms & words), -i, s))
    scored.sort(reverse=True)
    if not scored or scored[0][0] == 0:
        return " ".join(body_sents[:max_sentences])
    return " ".join(s for _, _, s in sorted(scored[:max_sentences], key=lambda x: -x[1]))


class Retriever(object):
    """Interface: search(query, market, k) -> list of dicts with a score."""

    name = "base"

    def __init__(self, docs):
        # docs: list of dicts with asset_id, title, topic, therapy_area, body_text, ...
        self.docs = docs

    def search(self, query, market=None, k=5):
        raise NotImplementedError


class TfidfRetriever(Retriever):
    name = "tfidf-cosine (scikit-learn)"

    def __init__(self, docs):
        Retriever.__init__(self, docs)
        corpus = [
            "%s. %s. %s. %s. %s" % (d["title"], d["topic"], d["therapy_area"],
                                    d.get("product", ""), d["body_text"])
            for d in docs
        ]
        self.vec = TfidfVectorizer(stop_words="english", ngram_range=(1, 2),
                                   sublinear_tf=True, min_df=1)
        self.matrix = self.vec.fit_transform(corpus) if corpus else None

    def search(self, query, market=None, k=5):
        if self.matrix is None or not self.docs:
            return []
        q = self.vec.transform([query or ""])
        sims = cosine_similarity(q, self.matrix)[0]
        order = sims.argsort()[::-1]
        out = []
        for i in order[: max(k * 4, 20)]:
            if sims[i] <= 0.0:
                continue
            d = dict(self.docs[i])
            d["score"] = round(float(sims[i]), 3)
            out.append(d)
        return out


class GeminiEmbeddingRetriever(Retriever):
    """
    Optional embedding backend. Enabled with RETRIEVER=gemini and a GEMINI_API_KEY.
    Calls the Gemini embeddings HTTP endpoint - no local model, no downloads.
    Falls back to TF-IDF if the API is unavailable, so the demo never breaks.
    """

    name = "gemini-embedding (remote API)"
    MODEL = "models/text-embedding-004"

    def __init__(self, docs):
        Retriever.__init__(self, docs)
        self.fallback = TfidfRetriever(docs)
        self.vectors = None
        self.ok = False
        key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not key:
            return
        try:
            texts = ["%s. %s. %s" % (d["title"], d["topic"], d["body_text"][:800])
                     for d in docs]
            self.vectors = self._embed(texts, key, "RETRIEVAL_DOCUMENT")
            self.ok = self.vectors is not None
        except Exception:
            self.ok = False

    def _embed(self, texts, key, task):
        import json
        import requests
        url = ("https://generativelanguage.googleapis.com/v1beta/%s:batchEmbedContents"
               "?key=%s" % (self.MODEL, key))
        vecs = []
        for i in range(0, len(texts), 50):
            chunk = texts[i:i + 50]
            body = {"requests": [{"model": self.MODEL,
                                  "content": {"parts": [{"text": t}]},
                                  "taskType": task} for t in chunk]}
            r = requests.post(url, data=json.dumps(body),
                              headers={"Content-Type": "application/json"}, timeout=30)
            if r.status_code != 200:
                return None
            vecs.extend([e["values"] for e in r.json().get("embeddings", [])])
        return vecs

    def search(self, query, market=None, k=5):
        if not self.ok:
            return self.fallback.search(query, market, k)
        try:
            key = os.environ.get("GEMINI_API_KEY", "").strip()
            qv = self._embed([query or ""], key, "RETRIEVAL_QUERY")
            if not qv:
                return self.fallback.search(query, market, k)
            import numpy as np
            M = np.array(self.vectors, dtype="float32")
            q = np.array(qv[0], dtype="float32")
            M = M / (np.linalg.norm(M, axis=1, keepdims=True) + 1e-9)
            q = q / (np.linalg.norm(q) + 1e-9)
            sims = M.dot(q)
            order = sims.argsort()[::-1]
            out = []
            for i in order[: max(k * 4, 20)]:
                d = dict(self.docs[int(i)])
                d["score"] = round(float(sims[int(i)]), 3)
                out.append(d)
            return out
        except Exception:
            return self.fallback.search(query, market, k)


def build_retriever(docs):
    """Factory - env flag selects the backend, TF-IDF is the default."""
    choice = os.environ.get("RETRIEVER", "tfidf").strip().lower()
    if choice == "gemini":
        r = GeminiEmbeddingRetriever(docs)
        if r.ok:
            return r
        return r  # already falls back internally
    return TfidfRetriever(docs)
