#!/usr/bin/env python3
from __future__ import annotations
import argparse, csv, hashlib, json, os, shutil, sqlite3, zipfile
from collections import Counter
from pathlib import Path

PASS2_VERSION = "0.2.0-pass2-geipan-en-ce"

def sha256_file(path: Path) -> str:
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024), b''):
            h.update(chunk)
    return h.hexdigest()

def load_jsonl(paths):
    rows=[]
    for p in paths:
        with open(p,'r',encoding='utf-8') as f:
            for line in f:
                if line.strip(): rows.append(json.loads(line))
    rows.sort(key=lambda r:(r.get('source_case_id',''),r.get('case_url','')))
    return rows

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--source-db',required=True)
    ap.add_argument('--jsonl-dir',required=True)
    ap.add_argument('--out-dir',required=True)
    args=ap.parse_args()
    source=Path(args.source_db)
    out=Path(args.out_dir); out.mkdir(parents=True,exist_ok=True)
    parts=sorted(Path(args.jsonl_dir).glob('*.jsonl'))
    if not parts: raise SystemExit('No shard JSONL files found')
    rows=load_jsonl(parts)
    if len(rows)!=3381: raise SystemExit(f'Expected 3381 cases, got {len(rows)}')
    ids=[r['source_case_id'] for r in rows]
    urls=[r['case_url'] for r in rows]
    if len(set(ids))!=3381 or len(set(urls))!=3381: raise SystemExit('Duplicate case IDs/URLs in Pass 2 output')

    db=out/'UFO_ATLAS_GEIPAN_PASS2_v0.2.0.sqlite'
    shutil.copy2(source,db)
    con=sqlite3.connect(db)
    con.execute('PRAGMA foreign_keys=ON')
    con.executescript('''
    DROP TABLE IF EXISTS case_pass2;
    CREATE TABLE case_pass2 (
      case_url TEXT PRIMARY KEY,
      source_case_id TEXT NOT NULL UNIQUE,
      title_en TEXT NOT NULL,
      observation_date TEXT,
      region_en TEXT,
      department_en TEXT,
      geipan_class TEXT,
      geipan_class_label_en TEXT,
      updated_date TEXT,
      phenomenon_type_en TEXT,
      strangeness TEXT,
      consistency TEXT,
      summary_en TEXT NOT NULL,
      description_en TEXT NOT NULL,
      description_fr TEXT NOT NULL,
      bilingual_description TEXT NOT NULL,
      encounter_class TEXT NOT NULL CHECK(encounter_class IN ('CE1','CE2','CE3','CE4','CE5')),
      encounter_class_confidence TEXT NOT NULL,
      encounter_class_basis TEXT NOT NULL,
      encounter_classifier_version TEXT NOT NULL,
      translation_model TEXT NOT NULL,
      translation_version TEXT NOT NULL,
      FOREIGN KEY(case_url) REFERENCES case_page(case_url)
    );
    DROP TABLE IF EXISTS case_pass2_provenance;
    CREATE TABLE case_pass2_provenance (
      case_url TEXT PRIMARY KEY,
      title_fr TEXT,
      summary_fr TEXT,
      phenomenon_type_fr TEXT,
      source_language TEXT NOT NULL DEFAULT 'fr',
      normalized_language TEXT NOT NULL DEFAULT 'en',
      source_preserved INTEGER NOT NULL DEFAULT 1,
      FOREIGN KEY(case_url) REFERENCES case_page(case_url)
    );
    DROP TABLE IF EXISTS pass2_release_info;
    CREATE TABLE pass2_release_info (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    ''')
    insert='''INSERT INTO case_pass2 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)'''
    prov='''INSERT INTO case_pass2_provenance(case_url,title_fr,summary_fr,phenomenon_type_fr) VALUES (?,?,?,?)'''
    for r in rows:
        desc_en=(r.get('description_en') or '').strip()
        summary_en=(r.get('summary_en') or '').strip()
        bilingual=(r.get('bilingual_description') or '').strip()
        if not desc_en or not summary_en: raise SystemExit(f'Missing English translation for {r["source_case_id"]}')
        if not bilingual.startswith(desc_en): raise SystemExit(f'English is not first in bilingual narrative for {r["source_case_id"]}')
        if r.get('description_fr','').strip() not in bilingual: raise SystemExit(f'French original absent from bilingual narrative for {r["source_case_id"]}')
        con.execute(insert,(
            r['case_url'],r['source_case_id'],r.get('title_en') or r.get('title_fr') or r['source_case_id'],
            r.get('observation_date',''),r.get('region',''),r.get('department',''),r.get('geipan_class',''),
            r.get('geipan_class_label_en',''),r.get('updated_date',''),r.get('phenomenon_type_en',''),
            r.get('strangeness',''),r.get('consistency',''),summary_en,desc_en,r.get('description_fr',''),bilingual,
            r['encounter_class'],r['encounter_class_confidence'],r['encounter_class_basis'],r['encounter_classifier_version'],
            r['translation_model'],r['translation_version']
        ))
        con.execute(prov,(r['case_url'],r.get('title_fr',''),r.get('summary_fr',''),r.get('phenomenon_type_fr','')))
    info={
      'pass2_version':PASS2_VERSION,
      'case_count':'3381',
      'public_encounter_classes':'CE1|CE2|CE3|CE4|CE5',
      'narrative_order':'English first; French original second',
      'metadata_language':'English where translation/localization is appropriate; proper geographic names retained',
      'source_policy':'Original French and original source database preserved',
    }
    con.executemany('INSERT INTO pass2_release_info(key,value) VALUES (?,?)',info.items())
    con.commit()

    integrity=con.execute('PRAGMA integrity_check').fetchone()[0]
    fk=con.execute('PRAGMA foreign_key_check').fetchall()
    census=dict(con.execute('SELECT encounter_class,count(*) FROM case_pass2 GROUP BY encounter_class ORDER BY encounter_class').fetchall())
    untranslated=con.execute("SELECT count(*) FROM case_pass2 WHERE trim(description_en)='' OR trim(summary_en)='' OR trim(phenomenon_type_en)='' ").fetchone()[0]
    french_missing=con.execute("SELECT count(*) FROM case_pass2 WHERE trim(description_fr)='' OR instr(bilingual_description,description_fr)=0").fetchone()[0]
    english_first=con.execute("SELECT count(*) FROM case_pass2 WHERE substr(bilingual_description,1,length(description_en))<>description_en").fetchone()[0]
    con.close()
    checks={
      'status':'PASS' if integrity=='ok' and not fk and untranslated==0 and french_missing==0 and english_first==0 and sum(census.values())==3381 else 'FAIL',
      'sqlite_integrity':integrity,'foreign_key_violations':len(fk),'case_count':3381,'encounter_class_census':census,
      'missing_english_fields':untranslated,'missing_french_originals':french_missing,'english_not_first':english_first,
      'source_database_sha256':sha256_file(source),'pass2_database_sha256':sha256_file(db)
    }
    (out/'VALIDATION_REPORT.json').write_text(json.dumps(checks,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    if checks['status']!='PASS': raise SystemExit(json.dumps(checks,indent=2))

    csvp=out/'GEIPAN_PASS2_CASE_INDEX.csv'
    with csvp.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.writer(f); w.writerow(['source_case_id','encounter_class','confidence','geipan_class','title_en','case_url'])
        for r in rows: w.writerow([r['source_case_id'],r['encounter_class'],r['encounter_class_confidence'],r.get('geipan_class',''),r.get('title_en') or r.get('title_fr',''),r['case_url']])
    readme='''WATCHERS PROJECT UFO ATLAS — GEIPAN PASS 2\n\nThis package applies the requested Pass 2 language and encounter-class upgrades to all 3,381 acquired GEIPAN case pages.\n\n- Public encounter classification is CE1 through CE5.\n- Main case description is English first, followed by the preserved French original.\n- Normalized metadata is English where translation/localization is appropriate; proper names remain proper names.\n- Original French source text and the original GEIPAN source tables remain preserved for provenance.\n- GEIPAN A/B/C/D classification remains source metadata and is not treated as the Atlas encounter class.\n- CE classification describes the reported encounter content; it does not assert that the report is true or unexplained.\n'''
    (out/'README_FIRST.txt').write_text(readme,encoding='utf-8')
    files=[db,out/'VALIDATION_REPORT.json',csvp,out/'README_FIRST.txt']
    manifest={'pass2_version':PASS2_VERSION,'files':[]}
    for p in files:
        manifest['files'].append({'name':p.name,'bytes':p.stat().st_size,'sha256':sha256_file(p)})
    (out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
    files.append(out/'manifest.json')
    zip_path=out/'UFO_ATLAS_GEIPAN_PASS2_v0.2.0.zip'
    with zipfile.ZipFile(zip_path,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as z:
        for p in files: z.write(p,p.name)
    (out/'PACKAGE_SHA256.txt').write_text(f'{sha256_file(zip_path)}  {zip_path.name}\n',encoding='utf-8')
    print(json.dumps(checks,indent=2))
    print(zip_path)

if __name__=='__main__': main()
