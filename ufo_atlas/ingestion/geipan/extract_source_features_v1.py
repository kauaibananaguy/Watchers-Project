#!/usr/bin/env python3
"""Extract directly stated GEIPAN case features from preserved French sources.

The extractor is deliberately source-bounded. It records matched wording,
location in the source, deterministic rule, and confidence. It does not turn
GEIPAN classifications into Atlas conclusions and it does not infer a source-
owned hierarchy.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import unicodedata
from pathlib import Path
from typing import Any, Iterable


def fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(char for char in text if not unicodedata.combining(char))
    return text.lower()


def sentence_window(text: str, start: int, end: int, limit: int = 800) -> str:
    left = max(text.rfind(".", 0, start), text.rfind("!", 0, start), text.rfind("?", 0, start), text.rfind("\n", 0, start))
    right_candidates = [position for position in (text.find(".", end), text.find("!", end), text.find("?", end), text.find("\n", end)) if position >= 0]
    right = min(right_candidates) + 1 if right_candidates else min(len(text), end + 350)
    excerpt = re.sub(r"\s+", " ", text[max(0, left + 1):right]).strip()
    if len(excerpt) > limit:
        relative = max(0, start - max(0, left + 1))
        begin = max(0, relative - limit // 2)
        excerpt = excerpt[begin:begin + limit]
    return excerpt


FEATURES: dict[str, dict[str, list[str]]] = {
    "OBJECT_FORM": {
        "DISK_SAUCER": [r"\bdisque\b", r"\bsoucoupe\b", r"disc(?:-shaped)?"],
        "SPHERE_BALL": [r"\bsphere\b", r"\bboule\b", r"\bballon\b", r"\borbe\b"],
        "TRIANGLE": [r"\btriangle\b", r"triangulaire"],
        "CIGAR_CYLINDER": [r"\bcigare\b", r"cylindr(?:e|ique)"],
        "OVAL_ELLIPTICAL": [r"\bovale?\b", r"ellipti(?:que|cal)"],
        "BOOMERANG_V_SHAPE": [r"boomerang", r"forme\s+de\s+v\b", r"en\s+v\b"],
        "RECTANGLE_SQUARE": [r"rectangulaire", r"\brectangle\b", r"\bcarre\b", r"quadrangulaire"],
        "LIGHT_POINT": [r"point\s+lumineux", r"lumiere\s+(?:vive|intense|etrange|mobile)", r"objet\s+lumineux"],
        "FORMATION": [r"formation\s+(?:de|en)", r"plusieurs\s+(?:objets|lumieres|points)"],
    },
    "COLOR": {
        "WHITE": [r"\bblanc(?:he|s)?\b"],
        "RED": [r"\brouge(?:s)?\b"],
        "ORANGE": [r"\borange(?:s)?\b"],
        "YELLOW": [r"\bjaune(?:s)?\b"],
        "GREEN": [r"\bvert(?:e|s|es)?\b"],
        "BLUE": [r"\bbleu(?:e|s|es)?\b"],
        "BLACK": [r"\bnoir(?:e|s|es)?\b"],
        "GRAY_SILVER": [r"\bgris(?:e|es)?\b", r"argente(?:e|s|es)?"],
        "GOLD": [r"dore(?:e|s|es)?"],
        "MULTICOLORED": [r"multicolore", r"change(?:ait|ant)?\s+de\s+couleur"],
    },
    "MOTION": {
        "STATIONARY": [r"stationnaire", r"immobile", r"sans\s+bouger"],
        "ACCELERATION": [r"accel(?:ere|era|eration|erant)", r"brusque\s+acceleration"],
        "HIGH_SPEED": [r"tres\s+rapid", r"grande\s+vitesse", r"vitesse\s+elevee"],
        "ZIGZAG_ERRATIC": [r"zig[ -]?zag", r"trajectoire\s+erratique", r"mouvement\s+erratique"],
        "ASCENT": [r"mont(?:e|ait|ee|er)\s+(?:rapidement|verticalement|dans\s+le\s+ciel)", r"ascension"],
        "DESCENT": [r"descend(?:it|ait|u|re)?", r"descente"],
        "ROTATION": [r"rotation", r"tourn(?:e|ait|ant)\s+sur\s+(?:lui|elle)-meme"],
        "HOVER": [r"vol\s+stationnaire", r"en\s+sustentation"],
        "SUDDEN_DISAPPEARANCE": [r"dispar(?:ait|ut|ition)\s+(?:brusquement|instantanement|soudainement)", r"s'eteignit\s+brusquement"],
    },
    "OBSERVATION_CHANNEL": {
        "VISUAL": [r"observe(?:e|s|es|r|rent|ait)?", r"aper(?:cu|cut|cevoir)", r"a\s+vu\b", r"temoin\s+oculaire"],
        "RADAR": [r"\bradar\b", r"echo\s+radar", r"trace\s+radar"],
        "PHOTOGRAPHIC": [r"photograph", r"appareil\s+photo", r"cliche"],
        "VIDEO": [r"\bvideo\b", r"filme(?:e|r|s)?", r"camera"],
        "AUDIO": [r"enregistrement\s+sonore", r"bruit\s+(?:etrange|inhabituel)", r"bourdonnement", r"sifflement"],
    },
    "PHENOMENON_EFFECT": {
        "PHYSICAL_TRACE": [r"trace\s+(?:au\s+sol|physique)", r"empreinte", r"vegetation\s+(?:brulee|ecrasee|endommagee)", r"sol\s+(?:brule|marque)"] ,
        "ELECTROMAGNETIC": [r"panne\s+(?:electrique|de\s+moteur|de\s+radio)", r"interference", r"perturbation\s+(?:electrique|magnetique|radio)", r"compas\s+affole"],
        "PHYSIOLOGICAL": [r"brulure", r"nausee", r"maux?\s+de\s+tete", r"paralys", r"picotement", r"chaleur\s+sur\s+(?:le|la)\s+corps"],
        "ANIMAL_REACTION": [r"animaux?\s+(?:affole|effraye|agite)", r"chien(?:s)?\s+(?:aboie|hurle|affole)", r"betail\s+(?:agite|effraye)"],
        "HEAT": [r"chaleur\s+intense", r"sensation\s+de\s+chaleur", r"rayonnement\s+thermique"],
        "ODOR": [r"odeur\s+(?:etrange|forte|de\s+soufre|d'ozone)", r"senteur\s+inhabituelle"],
        "SOUND": [r"bruit\s+(?:sourd|fort|etrange|inhabituel)", r"bourdonnement", r"sifflement", r"grondement"],
        "LANDING": [r"atterriss", r"pose\s+au\s+sol", r"au\s+sol\s+pres\s+de"],
    },
    "TOOL_DEVICE": {
        "RADAR_DEVICE": [r"\bradar\b"],
        "CAMERA": [r"camera", r"appareil\s+photo", r"camescope"],
        "BINOCULARS": [r"jumelles"],
        "TELESCOPE": [r"telescope", r"lunette\s+astronomique"],
        "RADIO": [r"poste\s+radio", r"radio\s+(?:de\s+bord|militaire|amateur)"],
        "MOBILE_PHONE": [r"telephone\s+portable", r"smartphone"],
    },
    "WITNESS_ROLE": {
        "PILOT": [r"\bpilote\b", r"commandant\s+de\s+bord"],
        "AIR_TRAFFIC_CONTROL": [r"controleur\s+aerien", r"controle\s+du\s+trafic\s+aerien"],
        "MILITARY": [r"\bmilitaire\b", r"armee\s+de\s+l'air", r"gendarme"],
        "POLICE": [r"\bpolicier\b", r"police\s+nationale"],
        "ASTRONOMER": [r"astronome", r"observatoire"],
        "METEOROLOGIST": [r"meteorologue", r"station\s+meteo"],
        "CHILD": [r"\benfant\b", r"age\s+de\s+\d{1,2}\s+ans"],
    },
    "ALLEGED_ENTITY": {
        "HUMANOID": [r"humanoid", r"etre\s+(?:humanoide|de\s+forme\s+humaine)", r"silhouette\s+humaine"],
        "CREATURE": [r"creature", r"etre\s+etrange", r"personnage\s+etrange"],
        "OCCUPANT": [r"occupant(?:s)?", r"a\s+l'interieur\s+de\s+l'objet"],
    },
    "CHRONOLOGY_SIGNAL": {
        "PRE_EVENT_CONTEXT": [r"avant\s+(?:l'observation|de\s+voir|l'apparition)", r"se\s+trouvait\s+(?:chez|sur|dans)"],
        "FIRST_DETECTION": [r"a\s+d'abord\s+(?:vu|remarque|apercu)", r"premiere?\s+(?:apparition|observation)", r"soudain.*(?:voit|apercoit|remarque)"],
        "WITNESS_ACTION": [r"a\s+(?:appele|prevenu|alerte|photographie|filme|suivi)", r"les\s+temoins\s+ont"],
        "OBJECT_DEPARTURE": [r"s'est\s+eloigne", r"a\s+disparu", r"disparut", r"quitta\s+les\s+lieux", r"partit\s+(?:rapidement|vers)"],
        "AFTERMATH": [r"apres\s+(?:l'observation|la\s+disparition|le\s+depart)", r"le\s+lendemain", r"par\s+la\s+suite"],
    },
}

NUMBER_PATTERNS = {
    "DURATION_SECONDS": (r"\b(\d+(?:[.,]\d+)?)\s*(seconde|secondes|sec\.?|s)\b", 1.0),
    "DURATION_MINUTES": (r"\b(\d+(?:[.,]\d+)?)\s*(minute|minutes|min\.?)\b", 60.0),
    "DURATION_HOURS": (r"\b(\d+(?:[.,]\d+)?)\s*(heure|heures|h)\b", 3600.0),
    "DISTANCE_METERS": (r"\b(\d+(?:[.,]\d+)?)\s*(metre|metres|m)\b", 1.0),
    "DISTANCE_KILOMETERS": (r"\b(\d+(?:[.,]\d+)?)\s*(kilometre|kilometres|km)\b", 1000.0),
    "ALTITUDE_METERS": (r"\b(?:altitude|hauteur)\s*(?:de|d'environ|environ|:)?\s*(\d+(?:[.,]\d+)?)\s*(metre|metres|m)\b", 1.0),
    "SPEED_KMH": (r"\b(\d+(?:[.,]\d+)?)\s*(km/h|kmh|kilometres?\s+par\s+heure)\b", 1.0),
    "OBJECT_COUNT": (r"\b(\d{1,3})\s+(objets?|lumieres?|points?\s+lumineux)\b", 1.0),
}


def assertions_for_text(case_key: str, source_uri: str, source_locator: str, text: str) -> Iterable[dict[str, Any]]:
    folded = fold(text)
    seen: set[tuple[str, str, int, int]] = set()
    for family, codes in FEATURES.items():
        for code, patterns in codes.items():
            for pattern in patterns:
                for match in re.finditer(pattern, folded, flags=re.I):
                    key = (family, code, match.start(), match.end())
                    if key in seen:
                        continue
                    seen.add(key)
                    yield {
                        "case_key": case_key,
                        "source_uri": source_uri,
                        "source_locator": source_locator,
                        "feature_family": family,
                        "feature_code": code,
                        "original_excerpt": sentence_window(text, match.start(), match.end()),
                        "normalized_value": code,
                        "numeric_value": None,
                        "unit": None,
                        "derivation_method": f"REGEX_DIRECT_STATEMENT:{pattern}",
                        "confidence_code": "MEDIUM",
                        "start_char": match.start(),
                        "end_char": match.end(),
                    }
    for code, (pattern, multiplier) in NUMBER_PATTERNS.items():
        for match in re.finditer(pattern, folded, flags=re.I):
            try:
                value = float(match.group(1).replace(",", ".")) * multiplier
            except ValueError:
                continue
            unit = "seconds" if code.startswith("DURATION") else "meters" if code.startswith(("DISTANCE", "ALTITUDE")) else "km/h" if code == "SPEED_KMH" else "count"
            yield {
                "case_key": case_key,
                "source_uri": source_uri,
                "source_locator": source_locator,
                "feature_family": "QUANTITATIVE_ASSERTION",
                "feature_code": code,
                "original_excerpt": sentence_window(text, match.start(), match.end()),
                "normalized_value": str(value),
                "numeric_value": value,
                "unit": unit,
                "derivation_method": f"REGEX_NORMALIZED_QUANTITY:{pattern}",
                "confidence_code": "MEDIUM",
                "start_char": match.start(),
                "end_char": match.end(),
            }


def create_database(output: Path) -> sqlite3.Connection:
    if output.exists():
        output.unlink()
    con = sqlite3.connect(output)
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript("""
    CREATE TABLE source_document(
      source_uri TEXT PRIMARY KEY,
      source_kind TEXT NOT NULL,
      case_key TEXT NOT NULL,
      source_locator TEXT NOT NULL,
      source_sha256 TEXT,
      language_code TEXT NOT NULL DEFAULT 'fr',
      text_length INTEGER NOT NULL,
      source_text TEXT NOT NULL
    );
    CREATE TABLE feature_assertion(
      assertion_id TEXT PRIMARY KEY,
      case_key TEXT NOT NULL,
      source_uri TEXT NOT NULL REFERENCES source_document(source_uri),
      source_locator TEXT NOT NULL,
      feature_family TEXT NOT NULL,
      feature_code TEXT NOT NULL,
      original_excerpt TEXT NOT NULL,
      normalized_value TEXT,
      numeric_value REAL,
      unit TEXT,
      derivation_method TEXT NOT NULL,
      confidence_code TEXT NOT NULL,
      start_char INTEGER NOT NULL,
      end_char INTEGER NOT NULL
    );
    CREATE INDEX idx_feature_case ON feature_assertion(case_key);
    CREATE INDEX idx_feature_family_code ON feature_assertion(feature_family,feature_code);
    """)
    return con


def add_document(con: sqlite3.Connection, kind: str, case_key: str, uri: str, locator: str, text: str, digest: str | None = None) -> int:
    text = text or ""
    con.execute(
        "INSERT OR REPLACE INTO source_document VALUES(?,?,?,?,?,?,?,?)",
        (uri, kind, case_key, locator, digest, "fr", len(text), text),
    )
    count = 0
    for row in assertions_for_text(case_key, uri, locator, text):
        identity = "|".join(
            [case_key, uri, row["feature_family"], row["feature_code"], str(row["start_char"]), str(row["end_char"])]
        )
        assertion_id = "GEIPAN-FA-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24].upper()
        con.execute(
            "INSERT OR IGNORE INTO feature_assertion VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                assertion_id, case_key, uri, locator, row["feature_family"], row["feature_code"],
                row["original_excerpt"], row["normalized_value"], row["numeric_value"], row["unit"],
                row["derivation_method"], row["confidence_code"], row["start_char"], row["end_char"],
            ),
        )
        count += 1
    return count


def locate(root: Path, pattern: str) -> Path:
    matches = list(root.rglob(pattern))
    if len(matches) != 1:
        raise RuntimeError(f"Expected one {pattern}, found {len(matches)}")
    return matches[0]


def build(args: argparse.Namespace) -> None:
    output = Path(args.output)
    con = create_database(output)
    document_count = 0
    assertion_attempts = 0

    if args.case_pages:
        db = locate(Path(args.case_pages), "GEIPAN_CASE_PAGE_SOURCE_SNAPSHOT_v0.2.0.sqlite")
        src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        src.row_factory = sqlite3.Row
        for row in src.execute("SELECT case_url,COALESCE(source_case_id,case_url),visible_text,visible_text_sha256 FROM case_page WHERE retrieval_status='DOWNLOADED'"):
            case_url, case_key, text, digest = row
            assertion_attempts += add_document(con, "GEIPAN_CASE_PAGE", case_key, case_url, case_url, text or "", digest)
            document_count += 1
        src.close()

    if args.assets:
        db = locate(Path(args.assets), "GEIPAN_LINKED_ASSET_METADATA_AND_TEXT_v0.1.0.sqlite")
        src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        src.row_factory = sqlite3.Row
        assets = {row["asset_url"]: dict(row) for row in src.execute("SELECT * FROM asset")}
        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in src.execute("SELECT asset_url,unit_number,unit_type,text,text_sha256 FROM asset_text_unit WHERE text IS NOT NULL ORDER BY asset_url,unit_number"):
            grouped.setdefault(row["asset_url"], []).append(row)
        for url, units in grouped.items():
            metadata = assets[url]
            case_keys = [value for value in (metadata.get("case_urls") or "").split(",") if value]
            if not case_keys:
                case_keys = [url]
            combined = "\n\n".join(row["text"] or "" for row in units)
            digest = hashlib.sha256(combined.encode("utf-8")).hexdigest()
            for case_key in case_keys:
                uri = f"{url}#case={hashlib.sha256(case_key.encode()).hexdigest()[:12]}"
                assertion_attempts += add_document(con, "GEIPAN_LINKED_ASSET_TEXT", case_key, uri, url, combined, digest)
                document_count += 1
        src.close()

    con.commit()
    quick = con.execute("PRAGMA quick_check").fetchone()[0]
    fk = len(con.execute("PRAGMA foreign_key_check").fetchall())
    assertions = con.execute("SELECT COUNT(*) FROM feature_assertion").fetchone()[0]
    families = dict(con.execute("SELECT feature_family,COUNT(*) FROM feature_assertion GROUP BY feature_family"))
    sources = con.execute("SELECT COUNT(*) FROM source_document").fetchone()[0]
    con.close()
    summary = {
        "overall_status": "PASS" if quick == "ok" and fk == 0 else "FAIL",
        "source_documents": sources,
        "feature_assertions": assertions,
        "feature_family_counts": families,
        "sqlite_quick_check": quick,
        "foreign_key_violations": fk,
        "source_boundary": "All assertions retain directly matched source wording, locator, rule, and confidence; none are Atlas conclusions.",
    }
    Path(args.summary).write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    if summary["overall_status"] != "PASS":
        raise SystemExit(json.dumps(summary, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case-pages")
    parser.add_argument("--assets")
    parser.add_argument("--output", required=True)
    parser.add_argument("--summary", required=True)
    build(parser.parse_args())


if __name__ == "__main__":
    main()
