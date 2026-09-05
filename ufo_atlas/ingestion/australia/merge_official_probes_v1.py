#!/usr/bin/env python3
"""Merge verified NAA/RecordSearch probe evidence into the Australian UFO source snapshot.

Preserves the v0.3.1 source snapshot, attaches official NAA probe evidence by exact discovery-entry
identity, and does not promote topic/case relationships to source-file identity.
"""
from __future__ import annotations
import argparse,csv,hashlib,json,shutil,sqlite3
from pathlib import Path

def sha_file(path:Path)->str:
    h=hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''): h.update(b)
    return h.hexdigest()

def locate(root:Path,name:str)->Path:
    hits=list(root.rglob(name))
    if len(hits)!=1: raise RuntimeError(f'Expected one {name}; found {len(hits)}')
    return hits[0]

def main():
    p=argparse.ArgumentParser();p.add_argument('--source-dir',required=True);p.add_argument('--probe-dir',required=True);p.add_argument('--output-dir',required=True);a=p.parse_args()
    source=locate(Path(a.source_dir),'AUSTRALIA_GOVERNMENT_UFO_SOURCE_SNAPSHOT_v0.3.1.sqlite')
    probe=locate(Path(a.probe_dir),'AUSTRALIA_NAA_OFFICIAL_PROBE_SNAPSHOT_v0.2.0.sqlite')
    out=Path(a.output_dir);shutil.rmtree(out,ignore_errors=True);out.mkdir(parents=True)
    db=out/'AUSTRALIA_GOVERNMENT_UFO_SOURCE_SNAPSHOT_v0.4.0.sqlite';shutil.copyfile(source,db)
    con=sqlite3.connect(db);con.row_factory=sqlite3.Row;con.execute('pragma foreign_keys=on')
    pcon=sqlite3.connect(probe);pcon.row_factory=sqlite3.Row
    con.executescript('''
    CREATE TABLE official_probe_result(
      discovery_entry_number INTEGER PRIMARY KEY,
      series_hint TEXT,barcode TEXT,page_count_hint INTEGER,access_hint TEXT,naa_digital_hint INTEGER,
      discovery_entry_text TEXT NOT NULL,official_probe_status TEXT NOT NULL,official_item_text TEXT,
      resolved_page_count INTEGER,official_recordsearch_item_url TEXT,official_recordsearch_view_url TEXT,
      raw_result_json TEXT NOT NULL
    );
    CREATE TABLE access_probe(
      probe_id INTEGER PRIMARY KEY,
      discovery_entry_number INTEGER NOT NULL REFERENCES official_probe_result(discovery_entry_number),
      kind TEXT NOT NULL,requested_url TEXT,status INTEGER,final_url TEXT,content_type TEXT,
      byte_count INTEGER,sha256 TEXT,error TEXT,text_sample TEXT
    );
    CREATE TABLE source_file_official_probe(
      source_file_id INTEGER PRIMARY KEY REFERENCES source_file(source_file_id),
      discovery_entry_number INTEGER NOT NULL REFERENCES official_probe_result(discovery_entry_number),
      identity_match_status TEXT NOT NULL,item_page_http_200 INTEGER NOT NULL,
      first_page_http_200 INTEGER NOT NULL,first_page_binary_reachable INTEGER NOT NULL,
      resolved_page_count INTEGER,detail_json TEXT NOT NULL
    );
    CREATE INDEX idx_probe_barcode ON official_probe_result(barcode);
    CREATE INDEX idx_access_probe_entry ON access_probe(discovery_entry_number);
    ''')
    probe_rows=list(pcon.execute('select * from official_probe_result order by discovery_entry_number'))
    for r in probe_rows: con.execute('insert into official_probe_result values(?,?,?,?,?,?,?,?,?,?,?,?,?)',tuple(r))
    for r in pcon.execute('select discovery_entry_number,kind,requested_url,status,final_url,content_type,byte_count,sha256,error,text_sample from access_probe order by probe_id'):
        con.execute('insert into access_probe(discovery_entry_number,kind,requested_url,status,final_url,content_type,byte_count,sha256,error,text_sample) values(?,?,?,?,?,?,?,?,?,?)',tuple(r))
    pcon.close();mismatches=[];linked=0
    for r in probe_rows:
        n=int(r['discovery_entry_number']);s=con.execute('select source_file_id,series_hint,barcode,page_count_hint,section from source_file where entry_number=?',(n,)).fetchone()
        if not s: raise SystemExit(f'Official probe entry {n} missing from complete source ledger')
        series_ok=(s['series_hint'] or '')==(r['series_hint'] or '');barcode_ok=(s['barcode'] or '')==(r['barcode'] or '')
        if not (series_ok and barcode_ok): mismatches.append({'entry':n,'source_series':s['series_hint'],'probe_series':r['series_hint'],'source_barcode':s['barcode'],'probe_barcode':r['barcode']})
        item200=con.execute("select count(*) from access_probe where discovery_entry_number=? and kind like 'ITEM_%' and status=200",(n,)).fetchone()[0]>0
        first200=con.execute("select count(*) from access_probe where discovery_entry_number=? and kind='PAGE_1' and status=200",(n,)).fetchone()[0]>0
        binary=con.execute("select count(*) from access_probe where discovery_entry_number=? and kind='PAGE_1' and status=200 and lower(coalesce(content_type,'')) not like 'text/html%'",(n,)).fetchone()[0]>0
        detail={'series_match':series_ok,'barcode_match':barcode_ok,'ledger_section':s['section'],'official_probe_status':r['official_probe_status'],'source_page_count_hint':s['page_count_hint'],'probe_page_count_hint':r['page_count_hint']}
        con.execute('insert into source_file_official_probe values(?,?,?,?,?,?,?,?)',(s['source_file_id'],n,'EXACT_DISCOVERY_ENTRY_IDENTITY' if series_ok and barcode_ok else 'IDENTITY_MISMATCH',int(item200),int(first200),int(binary),r['resolved_page_count'],json.dumps(detail,sort_keys=True)));linked+=1
    con.commit();quick=con.execute('pragma integrity_check').fetchone()[0];fk=con.execute('pragma foreign_key_check').fetchall()
    source_files=con.execute('select count(*) from source_file').fetchone()[0];copies=con.execute('select count(*) from text_access_copy').fetchone()[0];pages=con.execute('select count(*) from text_access_page').fetchone()[0]
    exact_matches=con.execute("select count(*) from file_access_match where relationship='SAME_ARCHIVAL_FILE'").fetchone()[0];binary_reachable=con.execute('select count(*) from source_file_official_probe where first_page_binary_reachable=1').fetchone()[0];item200=con.execute('select count(*) from source_file_official_probe where item_page_http_200=1').fetchone()[0]
    status='PASS' if quick=='ok' and not fk and source_files==155 and copies==21 and linked==len(probe_rows) and not mismatches else 'FAIL'
    summary={'status':status,'source_files':source_files,'text_access_copies':copies,'text_access_pages':pages,'exact_text_copy_file_identity_matches':exact_matches,'official_probe_results_merged':len(probe_rows),'official_probe_source_file_links':linked,'official_item_pages_http_200':item200,'official_first_page_binary_reachable':binary_reachable,'official_identity_mismatches':len(mismatches),'sqlite_integrity_check':quick,'foreign_key_violations':len(fk),'policy':'Official NAA probe evidence is attached by exact discovery-entry identity. Public text access copies remain independently provenance-tracked; no topic-level relationship is promoted to source-file identity.'}
    (out/'SUMMARY.json').write_text(json.dumps(summary,indent=2)+'\n');(out/'IDENTITY_MISMATCHES.json').write_text(json.dumps(mismatches,indent=2)+'\n')
    with (out/'OFFICIAL_SOURCE_LINKS.csv').open('w',newline='',encoding='utf-8') as f:
        rs=con.execute('''select s.source_file_id,s.entry_number,s.section,s.series_hint,s.barcode,p.identity_match_status,p.item_page_http_200,p.first_page_http_200,p.first_page_binary_reachable,p.resolved_page_count,o.official_recordsearch_item_url,o.official_recordsearch_view_url from source_file s join source_file_official_probe p using(source_file_id) join official_probe_result o on o.discovery_entry_number=p.discovery_entry_number order by s.entry_number''');w=csv.writer(f);w.writerow([d[0] for d in rs.description]);w.writerows(rs)
    con.close();checks=[]
    for q in sorted(out.rglob('*')):
        if q.is_file() and q.name!='SHA256SUMS.txt': checks.append(f'{sha_file(q)}  {q.relative_to(out).as_posix()}')
    (out/'SHA256SUMS.txt').write_text('\n'.join(checks)+'\n');print(json.dumps(summary,indent=2))
    if status!='PASS': raise SystemExit(1)
if __name__=='__main__': main()
