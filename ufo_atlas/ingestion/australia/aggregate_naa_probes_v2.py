#!/usr/bin/env python3
"""Aggregate completed NAA/RecordSearch item probes from the superseded v1 matrix run.

The original v1 aggregate assumed barcode uniqueness and could not safely represent multiple
discovery references to one archival item. This version preserves every completed result by
source entry and treats barcode as a non-unique official locator.
"""
from __future__ import annotations
import argparse,csv,hashlib,json,sqlite3
from pathlib import Path

def sha(p:Path)->str:
 h=hashlib.sha256()
 with p.open('rb') as f:
  for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
 return h.hexdigest()

def main():
 p=argparse.ArgumentParser();p.add_argument('--input-dir',required=True);p.add_argument('--output-dir',required=True);a=p.parse_args();src=Path(a.input_dir);out=Path(a.output_dir);out.mkdir(parents=True,exist_ok=True)
 rows=[]
 for f in sorted(src.rglob('RESULT.json')):
  r=json.loads(f.read_text(encoding='utf-8'));r['_artifact_path']=f.parent.name;rows.append(r)
 if len(rows)<100:raise SystemExit(f'Only {len(rows)} completed official NAA probe results found')
 # Preserve exactly one result per discovery entry number.
 by_entry={}
 for r in rows:
  n=int(r['discovery_entry_number'])
  if n in by_entry:raise SystemExit(f'Duplicate official probe result for discovery entry {n}')
  by_entry[n]=r
 db=out/'AUSTRALIA_NAA_OFFICIAL_PROBE_SNAPSHOT_v0.2.0.sqlite';con=sqlite3.connect(db);con.execute('pragma foreign_keys=on')
 con.executescript('''
 CREATE TABLE official_probe_result(
  discovery_entry_number INTEGER PRIMARY KEY,
  series_hint TEXT,barcode TEXT,page_count_hint INTEGER,access_hint TEXT,naa_digital_hint INTEGER,
  discovery_entry_text TEXT NOT NULL,official_probe_status TEXT NOT NULL,official_item_text TEXT,
  resolved_page_count INTEGER,official_recordsearch_item_url TEXT,official_recordsearch_view_url TEXT,
  raw_result_json TEXT NOT NULL
 );
 CREATE TABLE access_probe(
  probe_id INTEGER PRIMARY KEY,discovery_entry_number INTEGER NOT NULL REFERENCES official_probe_result(discovery_entry_number),
  kind TEXT NOT NULL,requested_url TEXT,status INTEGER,final_url TEXT,content_type TEXT,byte_count INTEGER,sha256 TEXT,error TEXT,text_sample TEXT
 );
 CREATE INDEX idx_official_barcode ON official_probe_result(barcode);
 CREATE INDEX idx_probe_entry ON access_probe(discovery_entry_number);
 ''')
 for n,r in sorted(by_entry.items()):
  con.execute('insert into official_probe_result values(?,?,?,?,?,?,?,?,?,?,?,?,?)',(n,r.get('series_hint'),r.get('barcode'),r.get('page_count_hint'),r.get('access_hint'),int(bool(r.get('naa_digital_hint'))),r.get('discovery_entry_text') or '',r.get('official_probe_status') or 'UNKNOWN',r.get('official_item_text'),r.get('resolved_page_count'),r.get('official_recordsearch_item_url'),r.get('official_recordsearch_view_url'),json.dumps(r,ensure_ascii=False,sort_keys=True)))
  for q in r.get('probes',[]):
   con.execute('insert into access_probe(discovery_entry_number,kind,requested_url,status,final_url,content_type,byte_count,sha256,error,text_sample) values(?,?,?,?,?,?,?,?,?,?)',(n,q.get('kind'),q.get('requested_url'),q.get('status'),q.get('final_url'),q.get('content_type'),q.get('bytes'),q.get('sha256'),q.get('error'),q.get('text_sample')))
 con.commit();quick=con.execute('pragma integrity_check').fetchone()[0];fk=con.execute('pragma foreign_key_check').fetchall()
 total=con.execute('select count(*) from official_probe_result').fetchone()[0]
 with_text=con.execute("select count(*) from official_probe_result where official_item_text is not null and length(official_item_text)>0").fetchone()[0]
 first_binary=con.execute("select count(distinct discovery_entry_number) from access_probe where kind='PAGE_1' and status=200 and lower(coalesce(content_type,'')) not like 'text/html%'").fetchone()[0]
 first_200=con.execute("select count(distinct discovery_entry_number) from access_probe where kind='PAGE_1' and status=200").fetchone()[0]
 item_200=con.execute("select count(distinct discovery_entry_number) from access_probe where kind like 'ITEM_%' and status=200").fetchone()[0]
 con.close()
 summary={'status':'PASS' if quick=='ok' and not fk and total==len(by_entry) else 'FAIL','completed_probe_results':total,'discovery_entry_min':min(by_entry),'discovery_entry_max':max(by_entry),'official_item_pages_http_200':item_200,'official_item_text_recovered':with_text,'first_page_http_200':first_200,'first_page_binary_reachable':first_binary,'sqlite_integrity_check':quick,'foreign_key_violations':len(fk),'source_run_id':33973328976,'policy':'Salvaged completed official NAA probes only; no source downloads were restarted. Duplicate barcodes are preserved as shared official locators rather than collapsed.'}
 (out/'SUMMARY.json').write_text(json.dumps(summary,indent=2)+'\n',encoding='utf-8')
 con=sqlite3.connect(db);con.row_factory=sqlite3.Row
 with (out/'OFFICIAL_PROBE_INDEX.csv').open('w',newline='',encoding='utf-8') as f:
  rs=con.execute('select discovery_entry_number,series_hint,barcode,official_probe_status,resolved_page_count,official_recordsearch_item_url,official_recordsearch_view_url from official_probe_result order by discovery_entry_number');w=csv.writer(f);w.writerow([d[0] for d in rs.description]);w.writerows(rs)
 con.close()
 checks=[]
 for q in sorted(out.rglob('*')):
  if q.is_file() and q.name!='SHA256SUMS.txt':checks.append(f'{sha(q)}  {q.relative_to(out).as_posix()}')
 (out/'SHA256SUMS.txt').write_text('\n'.join(checks)+'\n',encoding='utf-8');print(json.dumps(summary,indent=2))
 if summary['status']!='PASS':raise SystemExit(1)
if __name__=='__main__':main()
