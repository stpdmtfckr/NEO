# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import difflib
import os
import re
from pathlib import Path
from typing import Any


def _norm(value: str) -> str:
    return re.sub(r"[^a-zа-яё0-9]+", " ", str(value or "").casefold()).strip()


def configured_terms(config: Any) -> list[dict[str, Any]]:
    if not bool(config.get("personal_dictionary.enabled", True)):
        return []
    rows=[]
    for raw in (config.get("personal_dictionary.entries", []) or []):
        if not isinstance(raw, dict): continue
        name=str(raw.get("name","") or "").strip(); path=str(raw.get("path","") or "").strip()
        aliases=[str(x).strip() for x in (raw.get("aliases",[]) or []) if str(x).strip()]
        if name or path:
            rows.append({"name":name or Path(path).stem,"path":path,"kind":str(raw.get("kind","term") or "term"),"aliases":aliases})
    return rows


def installed_application_terms(limit: int = 250) -> list[str]:
    """Conservative local vocabulary from Start Menu shortcut names only."""
    if os.name != "nt": return []
    roots=[]
    appdata=os.environ.get("APPDATA",""); programdata=os.environ.get("PROGRAMDATA","")
    if appdata: roots.append(Path(appdata)/"Microsoft/Windows/Start Menu/Programs")
    if programdata: roots.append(Path(programdata)/"Microsoft/Windows/Start Menu/Programs")
    out=[]; seen=set()
    for root in roots:
        if not root.exists(): continue
        try:
            for p in root.rglob("*.lnk"):
                name=p.stem.strip(); key=_norm(name)
                if len(key)>=3 and key not in seen:
                    seen.add(key); out.append(name)
                    if len(out)>=limit: return out
        except OSError: continue
    return out


def hotwords(config: Any) -> list[str]:
    values=[]
    for row in configured_terms(config):
        values.append(row["name"]); values.extend(row["aliases"])
    return [x for x in values if str(x).strip()]


def repair_text(text: str, config: Any) -> str:
    """Repair only high-confidence user-dictionary phrases.

    Automatic Start-Menu names use an even stricter threshold and are considered
    only when a command already contains an open/run/show verb.  Ambiguous matches
    are deliberately left untouched for the normal resolver/LLM.
    """
    original=str(text or "")
    if not original.strip(): return original
    threshold=max(0.80,min(0.99,float(config.get("personal_dictionary.safe_match_threshold",0.88) or 0.88)))
    candidates=[]
    for row in configured_terms(config):
        canonical=str(row["name"] or "").strip()
        if not canonical: continue
        for spoken in [canonical,*row["aliases"]]:
            if _norm(spoken): candidates.append((canonical,_norm(spoken),threshold))
    low=_norm(original)
    if re.search(r"\b(?:открой|запусти|покажи|выведи|open|run)\b", low):
        for name in installed_application_terms(180):
            candidates.append((name,_norm(name),max(0.93,threshold)))
    # Replace n-grams only when the best candidate clearly wins.
    words=list(re.finditer(r"[a-zа-яё0-9]+", original, flags=re.I))
    if not words: return original
    proposals=[]
    for start in range(len(words)):
        for width in range(1,min(5,len(words)-start)+1):
            fragment=" ".join(m.group(0) for m in words[start:start+width])
            frag=_norm(fragment)
            if len(frag)<3: continue
            ranked=[]
            for canonical,spoken,need in candidates:
                sw=len(spoken.split())
                if abs(sw-width)>1: continue
                score=difflib.SequenceMatcher(None,frag,spoken).ratio()
                if score>=need: ranked.append((score,canonical,spoken,need))
            ranked.sort(reverse=True)
            if not ranked: continue
            best=ranked[0]
            if len(ranked)>1 and best[0]-ranked[1][0] < 0.045 and best[1].casefold()!=ranked[1][1].casefold():
                continue
            if _norm(best[1])==frag: continue
            proposals.append((best[0],start,width,best[1]))
    if not proposals: return original
    proposals.sort(reverse=True)
    occupied=set(); chosen=[]
    for score,start,width,repl in proposals:
        span=set(range(start,start+width))
        if span & occupied: continue
        occupied |= span; chosen.append((start,width,repl))
    if not chosen: return original
    replacements={start:(width,repl) for start,width,repl in chosen}
    out=[]; cursor=0; i=0
    while i<len(words):
        if i in replacements:
            width,repl=replacements[i]; first=words[i]; last=words[i+width-1]
            out.append(original[cursor:first.start()]); out.append(repl); cursor=last.end(); i+=width
        else: i+=1
    out.append(original[cursor:])
    return "".join(out)
