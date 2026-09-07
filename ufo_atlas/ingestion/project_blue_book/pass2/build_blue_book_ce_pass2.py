#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import sqlite3
import zipfile
from collections import Counter
from pathlib import Path

PASS2_VERSION = "0.2.0-pass2-blue-book-ce"
CLASSIFIER_VERSION = "WATCHERS_BLUE_BOOK_CE1_CE5_PASS2_REVIEWED_2026-09-06_v1"
ALLOWED = {"CE1", "CE2", "CE3", "CE4", "CE5"}

# Public Atlas CE classification describes the reported encounter content. It is
# independent of the Project Blue Book disposition and does not imply the report
# is true or unexplained.  The default is deliberately conservative because some
# Blue Book source files contain multiple unrelated clippings/reports.

CE5 = {
    "IMPORT-PBB-NAID-6787898-CASE": (
        "HIGH",
        "Pilot deliberately flashed taxi lights; the reported objects immediately blacked out/disappeared in apparent response.",
    ),
}

# No reviewed Blue Book record in this source package met the Atlas CE4 rule:
# reported abduction or involuntary boarding/transport.  Generic discussions of
# kidnapping and secondary compilation text are not promoted to CE4.
CE4 = {}

CE3 = {
    "IMPORT-PBB-NAID-6311816-CASE": ("HIGH", "Two small men reportedly ran from a crashed flying disc."),
    "IMPORT-PBB-NAID-6978127-CASE": ("HIGH", "Three little creatures were reportedly associated with a flying saucer."),
    "IMPORT-PBB-NAID-8678617-CASE": ("HIGH", "A man was reportedly seen exiting and returning to a UFO hatch; occupants were referenced."),
    "IMPORT-PBB-NAID-8299818-CASE": ("HIGH", "Beings were reportedly seen entering the craft before it departed."),
    "IMPORT-PBB-NAID-8667478-CASE": ("HIGH", "Small beings reportedly exited and re-entered a hovering saucer."),
    "IMPORT-PBB-NAID-6977589-CASE": ("HIGH", "The Socorro witness reportedly observed two small figures beside the landed object."),
    "IMPORT-PBB-NAID-8699462-CASE": ("HIGH", "Two human-like figures reportedly emerged from the craft, approached the witnesses, then re-entered it."),
    "IMPORT-PBB-NAID-8721788-CASE": ("HIGH", "The reporting youths described a saucer and its occupants/creatures."),
    "IMPORT-PBB-NAID-8698829-CASE": ("MEDIUM", "Creatures were reportedly present during the encounter and appeared to study a nearby automobile."),
    "IMPORT-PBB-NAID-7403732-CASE": ("HIGH", "The witness reported a phenomenon together with two little men."),
    "IMPORT-PBB-NAID-9078459-CASE": ("HIGH", "The boys reportedly entered a UFO and interacted with little people inside."),
    "IMPORT-PBB-NAID-6788504-CASE": ("HIGH", "Three small human figures were reportedly present near the bright object."),
    "IMPORT-PBB-NAID-8683286-CASE": ("HIGH", "Three roughly human figures were reportedly present with the nearby object."),
    "IMPORT-PBB-NAID-6962652-CASE": ("HIGH", "Four or five men reportedly emerged from a window-like opening in the craft."),
}

CE2 = {
    "IMPORT-PBB-NAID-9669372-CASE": ("HIGH", "Reported burned weeds/ash at the landing site."),
    "IMPORT-PBB-NAID-9670536-CASE": ("MEDIUM", "Reported burned spots/heat effects on the lawn associated with recovered apparatus."),
    "IMPORT-PBB-NAID-6788397-CASE": ("HIGH", "Reported patrol-car radio/headlight effects and stopped wristwatches during the sighting."),
    "IMPORT-PBB-NAID-6781926-CASE": ("HIGH", "Reported automobile engine failure during a close object encounter."),
    "IMPORT-PBB-NAID-7232487-CASE": ("HIGH", "Reported car lights going out and the car engine stopping during the close encounter."),
    "IMPORT-PBB-NAID-6974996-CASE": ("HIGH", "Reported landing traces approximately four feet in diameter."),
    "IMPORT-PBB-NAID-6968887-CASE": ("HIGH", "Reported radio interference/disturbance during the event."),
    "IMPORT-PBB-NAID-7201675-CASE": ("HIGH", "Reported automobile failure near the UFO."),
    "IMPORT-PBB-NAID-9079566-CASE": ("HIGH", "Reported car-radio static during a close object observation."),
    "IMPORT-PBB-NAID-6958930-CASE": ("HIGH", "Reported a large ground indentation at the location of the prior light observation."),
    "IMPORT-PBB-NAID-8230439-CASE": ("HIGH", "Reported ground impact and crater/physical trace."),
    "IMPORT-PBB-NAID-8234735-CASE": ("HIGH", "Reported radio background static during the sighting."),
    "IMPORT-PBB-NAID-8302885-CASE": ("HIGH", "Reported radio interference and magnetic disturbance associated with the object."),
    "IMPORT-PBB-NAID-9316349-CASE": ("HIGH", "Reported ground depression where the object was said to have landed."),
    "IMPORT-PBB-NAID-9735589-CASE": ("HIGH", "Reported electrical-current interruption, motor stoppage, and vehicle damage."),
    "IMPORT-PBB-NAID-9739156-CASE": ("HIGH", "Reported police-car engine stall and radio failure while the object passed overhead."),
    "IMPORT-PBB-NAID-8694587-CASE": ("HIGH", "Reported burns to a witness associated with an approaching object."),
    "IMPORT-PBB-NAID-8722202-CASE": ("HIGH", "Reported ground impressions at the landing area."),
    "IMPORT-PBB-NAID-8683028-CASE": ("HIGH", "Reported car radio and motor stopping during the encounter."),
    "IMPORT-PBB-NAID-6978879-CASE": ("HIGH", "Reported physical trace on the ground."),
    "IMPORT-PBB-NAID-8293300-CASE": ("HIGH", "Reported dog barking/howling reaction associated with the aerial event."),
    "IMPORT-PBB-NAID-7170923-CASE": ("HIGH", "Reported radio interference during the beam/light event."),
    "IMPORT-PBB-NAID-6967462-CASE": ("HIGH", "Reported dogs barking and appearing frightened during the event."),
    "IMPORT-PBB-NAID-8724646-CASE": ("MEDIUM", "Reported physical material/object submitted for analysis in connection with the event."),
    "IMPORT-PBB-NAID-6387249-CASE": ("HIGH", "Reported soil depression attributed by the witnesses/source to the object settling on the ground."),
    "IMPORT-PBB-NAID-8770315-CASE": ("HIGH", "Levelland report: pickup-truck motor and lights reportedly went out as a glowing object descended near the road."),
    "IMPORT-PBB-NAID-6787030-CASE": ("HIGH", "Reported dogs continued barking while the silent object was present."),
    "IMPORT-PBB-NAID-7229541-CASE": ("HIGH", "Levelland-area report: patrol-car motor reportedly stopped during the event; similar effects were reported by others."),
    "IMPORT-PBB-NAID-8858926-CASE": ("HIGH", "Reported landing in a field leaving a small crater."),
    "IMPORT-PBB-NAID-7812281-CASE": ("MEDIUM", "Reported animals behaving abnormally/in a daze during the event."),
    "IMPORT-PBB-NAID-8672474-CASE": ("HIGH", "Reporter identified the family dog barking as the reaction that drew attention to the object."),
    "IMPORT-PBB-NAID-7446332-CASE": ("HIGH", "Excited barking by the family dog reportedly drew attention to the descending object."),
    "IMPORT-PBB-NAID-6977502-CASE": ("HIGH", "Reported radio abruptly stopping during the event."),
    "IMPORT-PBB-NAID-7102613-CASE": ("LOW", "Witness later reported a car-radio failure and associated it with the object; temporal linkage is weaker than other CE2 cases."),
}

# Explicitly reviewed false-positive traps.  These are asserted as CE1 so future
# broad keyword rules cannot silently re-introduce the old misclassifications.
REVIEWED_CE1 = {
    "IMPORT-PBB-NAID-7466173-CASE": "Narrative explicitly says the observed phenomenon was not a humanoid-carrying flying saucer.",
    "IMPORT-PBB-NAID-9670379-CASE": "The 'creature' in the report is a bird/fowl, not a reported craft occupant or entity.",
    "IMPORT-PBB-NAID-6792140-CASE": "'Occupants' refers to occupants of the observer vehicle/aircraft, not the UFO.",
    "IMPORT-PBB-NAID-9072340-CASE": "'Occupants' refers to household witnesses, not occupants of a UFO.",
    "IMPORT-PBB-NAID-8644262-CASE": "'Little men' appears only in a general discussion of UFO belief, not the incident account.",
    "IMPORT-PBB-NAID-8846121-CASE": "Source explicitly states that no occupant was seen.",
    "IMPORT-PBB-NAID-6964927-CASE": "Investigation explicitly reports no physical disturbance or landing marks.",
    "IMPORT-PBB-NAID-9617761-CASE": "Source explicitly reports no exceptional radio static/effect.",
    "IMPORT-PBB-NAID-8714624-CASE": "Signals are explicitly described as not ordinary radio/electrical interference.",
    "IMPORT-PBB-NAID-8702688-CASE": "Source explicitly reports no unusual radio or TV interference.",
    "IMPORT-PBB-NAID-8289517-CASE": "Source explicitly reports no radio interference.",
    "IMPORT-PBB-NAID-7364284-CASE": "Engine-failure wording belongs to a secondary unrelated clipping, not the primary Saratoga record.",
    "IMPORT-PBB-NAID-9316523-CASE": "Humanoid wording belongs to a secondary Milan clipping in a multi-report source file, not the primary Pacific record.",
    "IMPORT-PBB-NAID-9369426-CASE": "Non-human-men wording belongs to a secondary Washington State clipping, not the primary Trotwood record.",
    "IMPORT-PBB-NAID-9316131-CASE": "Occupant wording belongs to a secondary Angeles National Forest report, not the primary Kettering record.",
    "IMPORT-PBB-NAID-8726250-CASE": "Creature wording is embedded in a multi-report compilation and is not securely attributable to the primary Mississippi record.",
    "IMPORT-PBB-NAID-6786728-CASE": "Onboard-saucer narrative is a secondary Brazil publication item in a source record whose primary incident is Kentucky; not promoted.",
}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def classify(record_id: str):
    if record_id in CE5:
        return "CE5", *CE5[record_id], "reviewed_override"
    if record_id in CE4:
        return "CE4", *CE4[record_id], "reviewed_override"
    if record_id in CE3:
        return "CE3", *CE3[record_id], "reviewed_override"
    if record_id in CE2:
        return "CE2", *CE2[record_id], "reviewed_override"
    if record_id in REVIEWED_CE1:
        return "CE1", "HIGH", REVIEWED_CE1[record_id], "reviewed_negative"
    return (
        "CE1",
        "MEDIUM",
        "No source-supported CE2-CE5 feature was confirmed for the primary incident after conservative Pass 2 review; distant, radar-visual, unclassified, and ordinary visual reports collapse to CE1 in the public CE1-CE5-only Atlas taxonomy.",
        "conservative_default",
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source-db", required=True)
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()

    source = Path(args.source_db)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    dest = out / "UFO_ATLAS_PROJECT_BLUE_BOOK_PASS2_v0.2.0.sqlite"
    shutil.copy2(source, dest)

    con = sqlite3.connect(dest)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")

    source_rows = list(con.execute(
        "SELECT record_id, primary_ce, date_start, location_normalized, full_chronological_narrative FROM case_incidents ORDER BY record_id"
    ))
    if len(source_rows) != 10807:
        raise SystemExit(f"Expected 10,807 Blue Book cases, got {len(source_rows)}")

    source_census = Counter(r["primary_ce"] for r in source_rows)

    con.executescript("""
    DROP TABLE IF EXISTS case_ce_pass2_provenance;
    CREATE TABLE case_ce_pass2_provenance (
        record_id TEXT PRIMARY KEY,
        source_primary_ce TEXT NOT NULL,
        pass2_primary_ce TEXT NOT NULL CHECK(pass2_primary_ce IN ('CE1','CE2','CE3','CE4','CE5')),
        confidence TEXT NOT NULL CHECK(confidence IN ('LOW','MEDIUM','HIGH')),
        classification_basis TEXT NOT NULL,
        classification_method TEXT NOT NULL,
        classifier_version TEXT NOT NULL,
        source_primary_ce_preserved INTEGER NOT NULL DEFAULT 1 CHECK(source_primary_ce_preserved=1),
        FOREIGN KEY(record_id) REFERENCES case_incidents(record_id)
    );
    CREATE INDEX IF NOT EXISTS idx_case_ce_pass2_class ON case_ce_pass2_provenance(pass2_primary_ce);
    DROP TABLE IF EXISTS pass2_blue_book_release_info;
    CREATE TABLE pass2_blue_book_release_info (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """)

    out_rows = []
    for r in source_rows:
        new_ce, confidence, basis, method = classify(r["record_id"])
        if new_ce not in ALLOWED:
            raise AssertionError((r["record_id"], new_ce))
        con.execute(
            "INSERT INTO case_ce_pass2_provenance(record_id,source_primary_ce,pass2_primary_ce,confidence,classification_basis,classification_method,classifier_version) VALUES (?,?,?,?,?,?,?)",
            (r["record_id"], r["primary_ce"], new_ce, confidence, basis, method, CLASSIFIER_VERSION),
        )
        con.execute("UPDATE case_incidents SET primary_ce=? WHERE record_id=?", (new_ce, r["record_id"]))
        out_rows.append((r["record_id"], r["primary_ce"], new_ce, confidence, method, basis, r["date_start"], r["location_normalized"]))

    release_info = {
        "pass2_version": PASS2_VERSION,
        "classifier_version": CLASSIFIER_VERSION,
        "case_count": "10807",
        "public_encounter_classes": "CE1|CE2|CE3|CE4|CE5",
        "highest_applicable_class_wins": "true",
        "source_classification_policy": "Original Blue Book primary_ce retained in case_ce_pass2_provenance; public case_incidents.primary_ce normalized to CE1-CE5 only.",
        "semantic_policy": "Classification describes reported encounter content and does not assert truth or unexplained status.",
        "multi_report_policy": "Conservative: secondary unrelated clippings in a source file do not promote the primary case.",
    }
    con.executemany("INSERT INTO pass2_blue_book_release_info(key,value) VALUES (?,?)", release_info.items())
    con.commit()

    integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
    fk = con.execute("PRAGMA foreign_key_check").fetchall()
    pass2_count = con.execute("SELECT count(*) FROM case_incidents").fetchone()[0]
    prov_count = con.execute("SELECT count(*) FROM case_ce_pass2_provenance").fetchone()[0]
    invalid = con.execute("SELECT count(*) FROM case_incidents WHERE primary_ce NOT IN ('CE1','CE2','CE3','CE4','CE5')").fetchone()[0]
    census = dict(con.execute("SELECT primary_ce,count(*) FROM case_incidents GROUP BY primary_ce ORDER BY primary_ce").fetchall())
    preserved_source_census = dict(con.execute("SELECT source_primary_ce,count(*) FROM case_ce_pass2_provenance GROUP BY source_primary_ce ORDER BY source_primary_ce").fetchall())

    # Hard assertions for reviewed positive and false-positive cases.
    for rid in CE5:
        assert con.execute("SELECT primary_ce FROM case_incidents WHERE record_id=?", (rid,)).fetchone()[0] == "CE5"
    for rid in CE3:
        assert con.execute("SELECT primary_ce FROM case_incidents WHERE record_id=?", (rid,)).fetchone()[0] == "CE3"
    for rid in CE2:
        assert con.execute("SELECT primary_ce FROM case_incidents WHERE record_id=?", (rid,)).fetchone()[0] == "CE2"
    for rid in REVIEWED_CE1:
        assert con.execute("SELECT primary_ce FROM case_incidents WHERE record_id=?", (rid,)).fetchone()[0] == "CE1"

    status = "PASS" if (
        integrity == "ok" and not fk and pass2_count == 10807 and prov_count == 10807 and invalid == 0
        and sum(census.values()) == 10807 and source_census == Counter(preserved_source_census)
    ) else "FAIL"

    report = {
        "status": status,
        "sqlite_integrity": integrity,
        "foreign_key_violations": len(fk),
        "source_case_count": len(source_rows),
        "pass2_case_count": pass2_count,
        "provenance_row_count": prov_count,
        "invalid_public_ce_values": invalid,
        "source_primary_ce_census": dict(sorted(source_census.items())),
        "pass2_primary_ce_census": census,
        "reviewed_positive_counts": {"CE2": len(CE2), "CE3": len(CE3), "CE4": len(CE4), "CE5": len(CE5)},
        "reviewed_false_positive_ce1_count": len(REVIEWED_CE1),
        "source_database_sha256": sha256_file(source),
    }
    if status != "PASS":
        raise SystemExit(json.dumps(report, indent=2))

    con.close()
    report["pass2_database_sha256"] = sha256_file(dest)

    (out / "VALIDATION_REPORT.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    index_path = out / "PROJECT_BLUE_BOOK_PASS2_CE_INDEX.csv"
    with index_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["record_id", "source_primary_ce", "pass2_primary_ce", "confidence", "classification_method", "classification_basis", "date_start", "location_normalized"])
        w.writerows(out_rows)

    review_path = out / "REVIEWED_CLASSIFICATION_OVERRIDES.json"
    review_path.write_text(json.dumps({
        "classifier_version": CLASSIFIER_VERSION,
        "CE5": CE5,
        "CE4": CE4,
        "CE3": CE3,
        "CE2": CE2,
        "reviewed_CE1_false_positive_traps": REVIEWED_CE1,
    }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    readme = """WATCHERS PROJECT UFO ATLAS — PROJECT BLUE BOOK PASS 2\n\nThis successor database normalizes the Project Blue Book case encounter classification to the public Atlas CE1-CE5 system.\n\nRules:\n- CE1: observational report without a confirmed CE2-CE5 feature in the primary incident.\n- CE2: reported physical/physiological/environmental/electromagnetic effect or physical trace.\n- CE3: reported beings/entities/occupants associated with the phenomenon or craft.\n- CE4: reported abduction or involuntary boarding/transport.\n- CE5: deliberate human-initiated signaling/contact followed by an apparent response.\n- Highest applicable class wins.\n\nThe classification describes the reported content; it does not assert that the report is true or unexplained. Project Blue Book's original classification is preserved in case_ce_pass2_provenance. Because some source files contain multiple unrelated clippings, promotion above CE1 is deliberately conservative and is based on reviewed primary-incident evidence.\n"""
    (out / "README_FIRST.txt").write_text(readme, encoding="utf-8")

    files = [dest, out / "VALIDATION_REPORT.json", index_path, review_path, out / "README_FIRST.txt"]
    manifest = {"pass2_version": PASS2_VERSION, "files": []}
    for p in files:
        manifest["files"].append({"name": p.name, "bytes": p.stat().st_size, "sha256": sha256_file(p)})
    manifest_path = out / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    files.append(manifest_path)

    package = out / "UFO_ATLAS_PROJECT_BLUE_BOOK_PASS2_v0.2.0.zip"
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in files:
            z.write(p, p.name)
    package_hash = sha256_file(package)
    (out / "PACKAGE_SHA256.txt").write_text(f"{package_hash}  {package.name}\n", encoding="utf-8")

    print(json.dumps(report, indent=2))
    print(f"PACKAGE={package}")
    print(f"PACKAGE_SHA256={package_hash}")


if __name__ == "__main__":
    main()
