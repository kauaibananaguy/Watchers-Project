#!/usr/bin/env python3
from __future__ import annotations
import argparse, gzip, json, re, sqlite3, unicodedata, hashlib, os
from pathlib import Path
from typing import List, Dict, Tuple

from lxml import html as lhtml

MODEL_NAME = "Helsinki-NLP/opus-mt-fr-en"
CLASSIFIER_VERSION = "WATCHERS_CE1_CE5_PASS2_RULESET_2026-09-06_v1"
TRANSLATION_VERSION = "MARIAN_OPUS_MT_FR_EN_PASS2_2026-09-06_v1"

GEIPAN_CLASS_LABEL_EN = {
    "A": "Identified phenomenon",
    "B": "Probably identified phenomenon",
    "C": "Insufficient information for reliable identification",
    "D": "Unidentified / strange phenomenon after investigation",
}

def norm(s: str) -> str:
    s = s or ""
    return "".join(c for c in unicodedata.normalize("NFKD", s.lower()) if not unicodedata.combining(c))

def clean_lxml_text(el) -> str:
    if el is None:
        return ""
    return " ".join(el.text_content().split())

def text_with_breaks_lxml(el) -> str:
    if el is None:
        return ""
    pieces = []
    def walk(node):
        if node.text:
            pieces.append(node.text)
        for child in node:
            if isinstance(child.tag, str) and child.tag.lower() == "br":
                pieces.append("\n")
            else:
                walk(child)
            if child.tail:
                pieces.append(child.tail)
    walk(el)
    raw = "".join(pieces)
    lines = [" ".join(x.split()) for x in raw.splitlines()]
    return "\n".join(x for x in lines if x)

def extract_case(row: sqlite3.Row) -> Dict[str, str]:
    html = gzip.decompress(row["html_gzip"]).decode("utf-8", "replace")
    root = lhtml.fromstring(html)
    metadata: Dict[str, List[str]] = {}
    for one in root.xpath('//div[contains(concat(" ",normalize-space(@class)," ")," one_info ")]'):
        labs = one.xpath('./div[contains(concat(" ",normalize-space(@class)," ")," one_info-label ")]')
        vals = one.xpath('./div[contains(concat(" ",normalize-space(@class)," ")," one_info-data ")]')
        if labs and vals:
            k = clean_lxml_text(labs[0])
            v = clean_lxml_text(vals[0])
            metadata.setdefault(k, []).append(v)
    def first(k: str) -> str:
        vals = metadata.get(k) or []
        return vals[0] if vals else ""
    titles = root.xpath('//div[contains(concat(" ",normalize-space(@class)," ")," cas__title ")]//h2')
    sums = root.xpath('//div[contains(concat(" ",normalize-space(@class)," ")," cas__chapo ")]//div[contains(concat(" ",normalize-space(@class)," ")," field-value ")]')
    descs = root.xpath('//div[contains(concat(" ",normalize-space(@class)," ")," cas__body ")]//div[contains(concat(" ",normalize-space(@class)," ")," field-value ")]')
    title = clean_lxml_text(titles[0]) if titles else (row["page_title"] or row["source_case_id"])
    summary_fr = clean_lxml_text(sums[0]) if sums else ""
    description_fr = text_with_breaks_lxml(descs[0]) if descs else ""
    return {
        "case_url": row["case_url"],
        "source_case_id": row["source_case_id"],
        "title_fr": title,
        "observation_date": first("Date d'observation"),
        "region": first("Région"),
        "department": first("Département"),
        "geipan_class": first("Classification"),
        "geipan_class_label_en": GEIPAN_CLASS_LABEL_EN.get(first("Classification"), ""),
        "updated_date": first("Date de mise a jour"),
        "phenomenon_type_fr": first("Type de phénomène"),
        "strangeness": first("Etrangeté"),
        "consistency": first("Consistance"),
        "summary_fr": summary_fr,
        "description_fr": description_fr,
    }

def sentence_windows(text: str, max_chars: int = 1100) -> List[str]:
    text = text.strip()
    if not text:
        return []
    paras = [p.strip() for p in text.split("\n") if p.strip()]
    chunks: List[str] = []
    for p in paras:
        if len(p) <= max_chars:
            chunks.append(p)
            continue
        sents = re.split(r"(?<=[.!?;:])\s+", p)
        cur = ""
        for s in sents:
            s = s.strip()
            if not s:
                continue
            if not cur:
                cur = s
            elif len(cur) + 1 + len(s) <= max_chars:
                cur += " " + s
            else:
                chunks.append(cur)
                cur = s
            while len(cur) > max_chars:
                cut = cur.rfind(" ", 0, max_chars)
                if cut < max_chars // 2:
                    cut = max_chars
                chunks.append(cur[:cut].strip())
                cur = cur[cut:].strip()
        if cur:
            chunks.append(cur)
    return chunks

class MarianTranslator:
    def __init__(self, model_name: str = MODEL_NAME):
        from transformers import MarianMTModel, MarianTokenizer
        self.tokenizer = MarianTokenizer.from_pretrained(model_name)
        self.model = MarianMTModel.from_pretrained(model_name)
        self.model.eval()
        self.model_name = model_name

    def translate_many(self, texts: List[str], batch_size: int = 12) -> List[str]:
        import torch
        out: List[str] = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i+batch_size]
            enc = self.tokenizer(batch, return_tensors="pt", padding=True, truncation=True, max_length=512)
            with torch.inference_mode():
                gen = self.model.generate(**enc, max_new_tokens=512, num_beams=1)
            out.extend(self.tokenizer.batch_decode(gen, skip_special_tokens=True))
        return out

    def translate_text(self, text: str) -> str:
        chunks = sentence_windows(text)
        if not chunks:
            return ""
        translated = self.translate_many(chunks)
        return "\n".join(x.strip() for x in translated if x.strip())

CE4_OVERRIDES = {
    "1979-11-00685": ("HIGH", "reported abduction / taken experience"),
    "1980-01-50498": ("HIGH", "reported abduction / taken experience (source later identifies hoax)"),
    "1983-08-00992": ("HIGH", "reported involuntary transport by the phenomenon with displacement and memory disruption"),
    "1990-07-08305": ("HIGH", "later-recovered memory of boarding the craft and being surrounded by six beings"),
}

CE3_OVERRIDES = {
    "1954-09-00010": ("HIGH", "small figure/personage reported with flying object"),
    "1954-10-00013": ("HIGH", "being reported with landed craft"),
    "1964-03-01780": ("HIGH", "five figures reported moving between landed craft and its surroundings"),
    "1965-07-00050": ("HIGH", "two figures reported entering landed craft"),
    "1965-07-02381": ("HIGH", "small beings/children-like figures reported exiting landed object"),
    "1976-01-00279": ("HIGH", "man-like figure reported exiting landed craft and approaching witness"),
    "1976-02-00287": ("HIGH", "two figures reported inside hovering craft"),
    "1980-03-00752": ("HIGH", "entity encounter and communication reported"),
    "1980-05-00768": ("HIGH", "humanoid form/presence reported during craft observation"),
    "1987-12-01120": ("HIGH", "six strange persons reported in encounter and departing on unusual vehicles"),
    "1997-01-01443": ("HIGH", "ovoid being reported (source later identifies fabrication)"),
    "1998-04-08584": ("HIGH", "two human-looking occupants reported in cockpit of close unidentified craft"),
    "1998-06-01503": ("HIGH", "three beings/occupants reported in or entering close craft"),
    "2015-08-09351": ("MEDIUM", "witness reported apparent figures/occupants in photographed phenomenon"),
}

CE2_OVERRIDES = {
    "1954-09-09112": "reported ground traces / physical interaction",
    "1954-10-00025": "reported animal reaction during close event",
    "1954-10-09227": "reported ground traces associated with event sequence",
    "1976-04-00297": "reported burned-ground traces (source later identifies hoax)",
    "1976-07-00315": "reported circular ground traces",
    "1976-09-00342": "reported intense heat, television disturbance, animal reaction, and local power effect",
    "1978-10-00560": "reported animal reaction during object observation",
    "1978-10-00562": "reported heat/ground-burning effects",
    "1979-03-00607": "reported power interruption and animal/fish effects during luminous event",
    "1979-07-02484": "reported headaches synchronous with luminous event",
    "1980-02-00737": "reported ground traces associated with multi-phase luminous event",
    "1980-03-00748": "reported vehicle engine stall during close luminous event",
    "1980-04-00766": "reported burned/powdered ground traces after falling object",
    "1981-01-00849": "reported landing traces after object departure",
    "1981-12-00902": "reported ground trace discovered after luminous event (source later attributes trace separately)",
    "1983-01-00962": "reported livestock reaction during luminous explosion",
    "1983-04-00969": "reported crater/physical trace",
    "1983-05-00977": "reported radio interference during close luminous event",
    "1983-11-01002": "reported animal reaction and damaged tree associated with luminous event",
    "1988-04-02015": "reported persistent circular vegetation trace",
    "1988-12-01155": "reported electrical shock/pressure effect on witness and animal reaction",
    "1989-03-01170": "reported vehicle shutdown and animal reaction during luminous event",
    "1990-07-01209": "reported crater/physical trace (source later identifies wartime ordnance)",
    "1992-11-01274": "reported animal reaction during intense luminous event",
    "1993-08-01318": "reported muscular pain and severe headaches after event",
    "1993-09-01330": "reported circular vegetation trace",
    "1993-10-01663": "reported repeated ground traces",
    "1994-01-01342": "reported animal fear and muscular contractions during event",
    "1994-07-01363": "reported small craters / burned vegetation",
    "1995-05-01393": "reported witness, animal, and vegetation effects associated with object",
    "1999-12-50497": "reported vehicle/electrical malfunction during intense light event",
    "2000-01-01542": "reported vehicle/electrical malfunction during intense light event",
    "2001-06-01567": "reported heat-damaged vegetation / altered ground",
    "2001-09-01573": "reported burned vegetation circles",
    "2007-11-01803": "reported severe headache / acute distress during event",
    "2008-05-02083": "reported severe headaches and animal reaction during close object observation",
    "2008-07-02314": "reported eye irritation, heat sensation, insomnia, and unusual odor after close event",
    "2011-06-02779": "reported circular vegetation trace after luminous observations",
    "2016-03-09454": "reported burned-grass trace after light/noise event",
}

CE5_PATTERNS = [
    ("explicit human-initiated contact", r"\b(?:temoin|personne|groupe) .{0,100}(?:tente|essaie|cherche|decide) de communiqu(?:er|ation) .{0,140}(?:repond|reponse|reagit|reaction)\b"),
    ("deliberate signaling with reported response", r"\b(?:temoin|personne|groupe) .{0,80}(?:fait|envoie|emet) des? signaux? .{0,140}(?:repond|reagit|retourne|change de direction)\b"),
]
CE4_PATTERNS = [
    ("reported abduction in case summary", r"\b(?:enlevement|abduction)\b"),
    ("direct report of boarding craft", r"\b(?:temoin|personne|homme|femme|enfant|elle|il) .{0,120}(?:monte|montee|embarque|emmene|enleve|transporte) .{0,60}(?:a bord|dans l['’]engin|dans l['’]objet|dans l['’]appareil)\b"),
]
CE3_PATTERNS = [
    ("humanoid explicitly reported", r"\bforme humanoide\b|\bhumanoides?\b"),
    ("figure exits/enters craft", r"\b(?:personnage|homme|individu|occupant|humanoide) .{0,90}(?:sort|descend|entre|rentre|remonte) .{0,70}(?:engin|objet|appareil|trappe)\b"),
    ("occupants explicitly inside craft", r"\b(?:deux|trois|quatre|cinq|six|plusieurs) (?:personnages|hommes|occupants|etres) .{0,80}(?:dans|a l['’]interieur de|cockpit) .{0,60}(?:engin|objet|appareil|pan)?\b"),
]
CE2_PATTERNS = [
    ("explicit physical traces", r"\b(?:gendarmes?|enqueteurs?|temoins?) .{0,120}(?:constatent|decouvrent|trouvent) .{0,50}(?:traces? au sol|herbes? (?:brulees?|couchees?|ecrasees?)|cratere)\b"),
    ("witness physiological effect", r"\b(?:temoin|personne|enfant|fils|femme|homme) .{0,100}(?:maux de tete|douleurs musculaires|contractions musculaires|paralyse|decharge electrique|yeux irrites|nausees|vertiges)\b"),
    ("animal reaction", r"\b(?:chien|chiens|betail|chevaux|vaches|animaux) .{0,70}(?:affole|affoles|apeure|apeures|aboie|aboient|s['’]enfuit|s['’]enfuient|panique)\b"),
]

NEG = re.compile(r"(?:aucun(?:e)?|sans|absence de|pas de|n['’]a pas|ne .{0,35} pas)\s+.{0,45}$")
def positive_hits(text: str, patterns, window: int = 100):
    t = norm(text)
    hits = []
    for label, pat in patterns:
        for m in re.finditer(pat, t, flags=re.I):
            before = t[max(0, m.start()-window):m.start()]
            if NEG.search(before):
                continue
            hits.append((label, m.group(0)))
            break
    return hits

def classify_case(rec: Dict[str, str]) -> Tuple[str, str, str]:
    cid = rec.get("source_case_id", "")
    if cid in CE4_OVERRIDES:
        conf, basis = CE4_OVERRIDES[cid]
        return "CE4", conf, basis
    if cid in CE3_OVERRIDES:
        conf, basis = CE3_OVERRIDES[cid]
        return "CE3", conf, basis
    if cid in CE2_OVERRIDES:
        return "CE2", "HIGH", CE2_OVERRIDES[cid]

    summary = rec.get("summary_fr", "")
    desc = rec.get("description_fr", "")
    early = desc[:max(1800, int(len(desc) * 0.45))]
    primary = (summary + "\n" + early).strip()

    h5 = positive_hits(primary, CE5_PATTERNS)
    if h5:
        return "CE5", "MEDIUM", "; ".join(x[0] for x in h5[:3])
    h4s = positive_hits(summary, CE4_PATTERNS)
    h4e = positive_hits(early, CE4_PATTERNS[1:])
    if h4s or h4e:
        return "CE4", "HIGH" if h4s else "MEDIUM", "; ".join(x[0] for x in (h4s + h4e)[:3])
    h3s = positive_hits(summary, CE3_PATTERNS)
    h3e = positive_hits(early, CE3_PATTERNS)
    if h3s or h3e:
        return "CE3", "HIGH" if h3s else "MEDIUM", "; ".join(x[0] for x in (h3s + h3e)[:3])
    h2s = positive_hits(summary, CE2_PATTERNS)
    h2e = positive_hits(early, CE2_PATTERNS)
    if h2s or h2e:
        return "CE2", "HIGH" if h2s else "MEDIUM", "; ".join(x[0] for x in (h2s + h2e)[:3])
    return "CE1", "MEDIUM", "observational encounter without a source-supported CE2-CE5 feature in the available case account"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--shards", type=int, default=1)
    ap.add_argument("--translate", choices=["yes","no"], default="yes")
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    rows = list(con.execute("SELECT case_url, source_case_id, page_title, html_gzip FROM case_page ORDER BY source_case_id, case_url"))
    rows = [r for i, r in enumerate(rows) if i % args.shards == args.shard]
    records = [extract_case(r) for r in rows]

    translator = MarianTranslator() if args.translate == "yes" else None
    phen_map: Dict[str, str] = {}
    if translator:
        uniq = sorted({r["phenomenon_type_fr"] for r in records if r["phenomenon_type_fr"]})
        vals = translator.translate_many(uniq, batch_size=16) if uniq else []
        phen_map = dict(zip(uniq, vals))

    outp = Path(args.out)
    outp.parent.mkdir(parents=True, exist_ok=True)
    with outp.open("w", encoding="utf-8") as f:
        for n, rec in enumerate(records, 1):
            ce, conf, basis = classify_case(rec)
            rec["encounter_class"] = ce
            rec["encounter_class_confidence"] = conf
            rec["encounter_class_basis"] = basis
            rec["encounter_classifier_version"] = CLASSIFIER_VERSION
            if translator:
                rec["title_en"] = rec["title_fr"]
                rec["summary_en"] = translator.translate_text(rec["summary_fr"])
                rec["description_en"] = translator.translate_text(rec["description_fr"])
                rec["phenomenon_type_en"] = phen_map.get(rec["phenomenon_type_fr"], rec["phenomenon_type_fr"])
                rec["translation_model"] = MODEL_NAME
                rec["translation_version"] = TRANSLATION_VERSION
            else:
                rec["title_en"] = ""
                rec["summary_en"] = ""
                rec["description_en"] = ""
                rec["phenomenon_type_en"] = ""
                rec["translation_model"] = ""
                rec["translation_version"] = ""
            rec["bilingual_description"] = ((rec["description_en"] + "\n\n--- French original ---\n\n" + rec["description_fr"]).strip()
                                             if rec["description_en"] else rec["description_fr"])
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if n % 25 == 0:
                print(f"shard {args.shard}: {n}/{len(records)}")

if __name__ == "__main__":
    main()
