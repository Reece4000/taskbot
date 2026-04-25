from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple


SYMBOL_PATTERNS = [
    re.compile(r"\b(class|struct|namespace|enum)\s+([A-Za-z_][A-Za-z0-9_]*)"),
    re.compile(r"\b([A-Za-z_][A-Za-z0-9_:<>]*)::([A-Za-z_][A-Za-z0-9_]*)\s*\("),
    re.compile(r"\bid\s*:\s*([A-Za-z_][A-Za-z0-9_]*)"),
    re.compile(r'"([A-Za-z_][A-Za-z0-9_]{2,})"\s*:'),
]

STOP_WORDS = {
    "the",
    "and",
    "for",
    "with",
    "that",
    "this",
    "from",
    "have",
    "need",
    "into",
    "only",
    "when",
    "while",
    "should",
    "would",
    "there",
    "they",
    "them",
    "then",
    "also",
    "make",
    "like",
    "same",
    "still",
    "does",
    "dont",
    "doesnt",
    "work",
    "works",
    "view",
    "panel",
    "section",
    "default",
    "current",
    "project",
    "song",
}


def _tokenise(text: str) -> List[str]:
    tokens = re.findall(r"[A-Za-z0-9_]{3,}", text.lower())
    return [token for token in tokens if token not in STOP_WORDS]


def _extract_symbols(text: str, max_symbols: int) -> List[str]:
    symbols: List[str] = []
    seen = set()
    for pattern in SYMBOL_PATTERNS:
        for match in pattern.finditer(text):
            for group in match.groups()[::-1]:
                if group and group not in seen:
                    seen.add(group)
                    symbols.append(group)
                    if len(symbols) >= max_symbols:
                        return symbols
    return symbols


def _should_exclude(rel_path: str, excluded_prefixes: List[str]) -> bool:
    normalised = rel_path.replace("\\", "/")
    for prefix in excluded_prefixes:
        candidate = prefix.strip().strip("/")
        if not candidate:
            continue
        if normalised == candidate or normalised.startswith(candidate + "/"):
            return True
    return False


def _iter_candidate_files(repo_root: Path, config: Dict[str, Any]) -> Iterable[Path]:
    include_extensions = set(config["context"]["include_extensions"])
    excluded_prefixes = list(config["context"].get("exclude_paths", []))
    max_file_size = int(config["context"]["max_file_size_bytes"])
    for scan_root in config["context"]["scan_roots"]:
        root = repo_root / scan_root
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            rel_path = str(path.relative_to(repo_root))
            if _should_exclude(rel_path, excluded_prefixes):
                continue
            if path.suffix.lower() not in include_extensions:
                continue
            try:
                if path.stat().st_size > max_file_size:
                    continue
            except OSError:
                continue
            yield path


def build_repo_index(repo_root: Path, config: Dict[str, Any], *, rebuild: bool = False) -> Dict[str, Any]:
    index_path = Path(config["state_dir"]) / "repo_index.json"
    if index_path.exists() and not rebuild:
        with index_path.open("r", encoding="utf-8") as handle:
            cached = json.load(handle)
    else:
        cached = {"files": {}}

    files = cached.setdefault("files", {})
    max_symbols = int(config["context"]["max_symbols_per_file"])
    seen_paths = set()

    for path in _iter_candidate_files(repo_root, config):
        rel_path = str(path.relative_to(repo_root))
        seen_paths.add(rel_path)
        try:
            stat = path.stat()
            mtime = stat.st_mtime
        except OSError:
            continue

        cached_entry = files.get(rel_path)
        if cached_entry and cached_entry.get("mtime") == mtime:
            continue

        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        files[rel_path] = {
            "mtime": mtime,
            "symbols": _extract_symbols(text, max_symbols),
            "tokens": _tokenise(rel_path + " " + text[:1200]),
        }

    for rel_path in list(files.keys()):
        if rel_path not in seen_paths:
            del files[rel_path]

    index_path.parent.mkdir(parents=True, exist_ok=True)
    with index_path.open("w", encoding="utf-8") as handle:
        json.dump(cached, handle, indent=2, sort_keys=True)
    return cached


def rank_files_for_task(index: Dict[str, Any], task_text: str, section: str, limit: int) -> List[Tuple[str, List[str], float]]:
    task_tokens = set(_tokenise(task_text + " " + section))
    ranked: List[Tuple[str, List[str], float]] = []

    for rel_path, entry in index.get("files", {}).items():
        path_tokens = set(entry.get("tokens", []))
        overlap = task_tokens.intersection(path_tokens)
        if not overlap:
            continue
        score = float(len(overlap))
        if any(token in rel_path.lower() for token in task_tokens):
            score += 1.5
        ranked.append((rel_path, list(entry.get("symbols", [])), score))

    ranked.sort(key=lambda item: (-item[2], item[0]))
    return ranked[:limit]
