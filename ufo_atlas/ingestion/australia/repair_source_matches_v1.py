#!/usr/bin/env python3
"""Repair linkage between Australian source-file ledger rows and public text-access copies.

Uses exact archival series/item/part identities only. Related media and ambiguous access copies
are preserved without forced identity merges. NAA/RAAF remains authoritative.
"""
from __future__ import annotations
import argparse,csv,hashlib,json,shutil,sqlite3
from pathlib import Path

EXACT = {
  1: 90,   # A12639 5/1/AIR
  2: 153,  # A4703 1978/1205
  3: 3,    # A703 554/1/30 part 1
  4: 20,   # A703 580/1/1 part 1
  5: 29,   # A703 580/1/1 part 10
  6: 21,   # A703 580/1/1 part 2
  7: 39,   # A703 580/1/1 part 20
  8: 22,   # A703 580/1/1 part 3
  9: 23,   # A703 580/1/1 part 4
  10: 24,  # A703 580/1/1 part 5
  11: 65,  # A9755 item 2
  12: 92,  # B5758 5/6AIR part 1; access title compresses 5/6 as 56
  13: 99,  # E1327/2 5 5/4/AIR part 1
  16: 109, # M1148 Flying Saucers 1954-1955
  19: 119, # PP474/1 5/5AIR
  20: 121, # PP959/1 5/3/AIR
}
RELATED = {
  14: [(153,'VALENTICH_CASE_MEDIA_RELATED_TO_A4703_1978_1205')],
  15: [(153,'VALENTICH_CASE_MEDIA_RELATED_TO_A4703_1978_1205')],
  21: [(61,'MARALINGA_PROJECT_FILE_POSSIBLE_PARENT'),(151,'WOOMERA_FOI_COMPILATION_POSSIBLE_PARENT')],
}
NOTES = {
  17: 'Researcher access/index document spans many Australian UFO files; no one-to-one source-file identity.',
  18: 'P1556 PHENOMENA-MAWSON 1958 access copy is preserved but does not resolve to one of the 155 numbered discovery-ledger entries.',
}

def locate(root:Path,name:str)->Path:
    m=list(root.rglob(name))
    if len(m)!=1: raise RuntimeError(f'Expected one {name}; found {len(m)}')
    return m[0]

def sha(path:Path)->str:
    h=hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''): h.update(b)
    return h.hexdigest()

def main():
    p=argparse.ArgumentParser();p.add_argument('--input-dir',required=True);p.add_argument('--output-dir',required=True);a=p.parse_args()
    src=Path(a.input_dir);out=Path(a.output_dir);shutil.rmtree(out,ignore_errors=True);out.mkdir(parents=True)
    original=locate(src,'AUSTRALIA_GOVERNMENT_UFO_SOURCE_SNAPSHOT_v0.3.0.sqlite')
    db=out/'AUSTRALIA_GOVERNMENT_UFO_SOURCE_SNAPSHOT_v0.3.1.sqlite';shutil.copyfile(original,db)
    con=sqlite3.connect(db);con.execute('pragma foreign_keys=on');con.row_factory=sqlite3.Row
    con.execute('DROP TABLE IF EXISTS file_access_match')
    con.execute('''CREATE TABLE file_access_match(
      match_id INTEGER PRIMARY KEY,
      source_file_id INTEGER REFERENCES source_file(source_file_id),
      access_copy_id INTEGER NOT NULL REFERENCES text_access_copy(access_copy_id),
      relationship TEXT NOT NULL,
      confidence TEXT NOT NULL,
      match_method TEXT NOT NULL,
      detail TEXT
    )''')
    con.execute('CREATE INDEX idx_file_access_copy ON file_access_match(access_copy_id)')
    con.execute('CREATE INDEX idx_file_access_source ON file_access_match(source_file_id)')
    for copy_id,file_id in EXACT.items():
        title=con.execute('select title from text_access_copy where access_copy_id=?',(copy_id,)).fetchone()['title']
        raw=con.execute('select raw_discovery_text from source_file where source_file_id=?',(file_id,)).fetchone()['raw_discovery_text']
        con.execute('insert into file_access_match(source_file_id,access_copy_id,relationship,confidence,match_method,detail) values(?,?,?,?,?,?)',(file_id,copy_id,'SAME_ARCHIVAL_FILE','HIGH','EXACT_SERIES_ITEM_PART_IDENTITY',json.dumps({'title':title,'ledger_entry':raw},ensure_ascii=False)))
    for copy_id,rels in RELATED.items():
        for file_id,reason in rels:
            con.execute('insert into file_access_match(source_file_id,access_copy_id,relationship,confidence,match_method,detail) values(?,?,?,?,?,?)',(file_id,copy_id,'RELATED_SOURCE_MATERIAL','MEDIUM','CASE_OR_TOPIC_RELATION_NOT_FILE_IDENTITY',reason))
    for copy_id,note in NOTES.items():
        con.execute('insert into file_access_match(source_file_id,access_copy_id,relationship,confidence,match_method,detail) values(?,?,?,?,?,?)',(None,copy_id,'UNRESOLVED_FILE_IDENTITY','NONE','PRESERVED_WITHOUT_FORCED_MATCH',note))
    represented={r[0] for r in con.execute('select distinct access_copy_id from file_access_match')}
    all_copies={r[0] for r in con.execute('select access_copy_id from text_access_copy')}
    for copy_id in sorted(all_copies-represented):
        con.execute('insert into file_access_match(source_file_id,access_copy_id,relationship,confidence,match_method,detail) values(?,?,?,?,?,?)',(None,copy_id,'UNRESOLVED_FILE_IDENTITY','NONE','PRESERVED_WITHOUT_FORCED_MATCH','No exact one-to-one archival identity established.'))
    con.commit()
    quick=con.execute('pragma integrity_check').fetchone()[0];fk=con.execute('pragma foreign_key_check').fetchall()
    counts={r[0]:r[1] for r in con.execute('select relationship,count(*) from file_access_match group by relationship')}
    exact_copies=con.execute("select count(distinct access_copy_id) from file_access_match where relationship='SAME_ARCHIVAL_FILE'").fetchone()[0]
    all_count=con.execute('select count(*) from text_access_copy').fetchone()[0]
    con.close()
    summary={'status':'PASS' if quick=='ok' and not fk and all_count==21 and exact_copies==16 else 'FAIL','source_files':155,'text_access_copies':all_count,'exact_file_identity_matches':exact_copies,'match_relationship_counts':counts,'sqlite_integrity_check':quick,'foreign_key_violations':len(fk),'matching_policy':'Exact archival identity only; case/topic relationships are not treated as source-file identity; unresolved access copies remain separate.'}
    (out/'SUMMARY.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    con=sqlite3.connect(db);con.row_factory=sqlite3.Row
    with (out/'FILE_ACCESS_MATCHES.csv').open('w',newline='',encoding='utf-8') as f:
        rows=con.execute('select m.*,c.title,s.entry_number,s.series_hint,s.barcode from file_access_match m join text_access_copy c on c.access_copy_id=m.access_copy_id left join source_file s on s.source_file_id=m.source_file_id order by m.access_copy_id,m.match_id')
        w=csv.writer(f);w.writerow([d[0] for d in rows.description]);w.writerows(rows)
    con.close()
    checks=[]
    for q in sorted(out.rglob('*')):
        if q.is_file() and q.name!='SHA256SUMS.txt':checks.append(f'{sha(q)}  {q.relative_to(out).as_posix()}')
    (out/'SHA256SUMS.txt').write_text('\n'.join(checks)+'\n',encoding='utf-8');print(json.dumps(summary,indent=2))
    if summary['status']!='PASS':raise SystemExit(1)
if __name__=='__main__':main()
