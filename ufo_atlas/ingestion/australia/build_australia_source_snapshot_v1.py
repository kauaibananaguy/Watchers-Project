#!/usr/bin/env python3
"""Build a source-neutral Australian government UFO source snapshot.

Combines the lossless 155-entry government-file discovery ledger with searchable
public text access copies. NAA/RAAF remains the authority; the text mirror is an
access layer only. No master-Atlas matching occurs here.
"""
from __future__ import annotations
import argparse,csv,hashlib,json,re,shutil,sqlite3
from pathlib import Path


def locate(root:Path,name:str)->Path:
 m=list(root.rglob(name))
 if len(m)!=1: raise RuntimeError(f'Expected one {name}; found {len(m)}')
 return m[0]

def sha(p:Path)->str:
 h=hashlib.sha256()
 with p.open('rb') as f:
  for b in iter(lambda:f.read(1024*1024),b''): h.update(b)
 return h.hexdigest()

def extract_pages(text:str)->list[dict]:
 marker=text.find('Extracted text')
 if marker<0:return []
 tail=text[marker:]
 rx=re.compile(r'(?m)^Page\s*\n(\d+)\s*\n(?:[·•]\s*)?(?:(born-digital extraction|OCR|unreadable)\s*\n)?')
 matches=list(rx.finditer(tail))
 best=[]
 for i,m in enumerate(matches):
  if int(m.group(1))!=1: continue
  run=[m];want=2
  for n in matches[i+1:]:
   num=int(n.group(1))
   if num==want: run.append(n);want+=1
   elif num<want: continue
   else: break
  if len(run)>len(best):best=run
 if not best:return []
 pages=[]
 for i,m in enumerate(best):
  stop=best[i+1].start() if i+1<len(best) else len(tail)
  body=tail[m.end():stop].strip()
  pages.append({'page_number':int(m.group(1)),'extraction_method':m.group(2) or 'UNKNOWN','text':body,'text_sha256':hashlib.sha256(body.encode('utf-8')).hexdigest()})
 return pages

def title_key(title:str|None)->str|None:
 if not title:return None
 # Keep archival series/item/part token, dropping descriptive suffix.
 m=re.search(r'\b([A-Z]{1,4}\d+\s+\d+(?:/\d+)*(?:\s+Part\s+\d+)?)\b',title,re.I)
 return re.sub(r'\s+',' ',m.group(1)).upper() if m else None

def main():
 ap=argparse.ArgumentParser();ap.add_argument('--ledger-dir',required=True);ap.add_argument('--text-dir',required=True);ap.add_argument('--output-dir',required=True);a=ap.parse_args()
 out=Path(a.output_dir);shutil.rmtree(out,ignore_errors=True);out.mkdir(parents=True)
 ldb=sqlite3.connect(f'file:{locate(Path(a.ledger_dir),"AUSTRALIA_GOVERNMENT_UFO_FILE_INVENTORY_v0.2.0.sqlite").resolve()}?mode=ro',uri=True);ldb.row_factory=sqlite3.Row
 tdb=sqlite3.connect(f'file:{locate(Path(a.text_dir),"AUSTRALIA_PUBLIC_TEXT_ACCESS_v0.1.0.sqlite").resolve()}?mode=ro',uri=True);tdb.row_factory=sqlite3.Row
 db=out/'AUSTRALIA_GOVERNMENT_UFO_SOURCE_SNAPSHOT_v0.3.0.sqlite';con=sqlite3.connect(db);con.execute('pragma foreign_keys=on')
 con.executescript('''
 CREATE TABLE source_file(source_file_id INTEGER PRIMARY KEY,entry_number INTEGER,section TEXT,series_hint TEXT,barcode TEXT,page_count_hint INTEGER,access_hint TEXT,disposition TEXT,naa_digital_hint INTEGER NOT NULL,raw_discovery_text TEXT NOT NULL,authority TEXT NOT NULL DEFAULT 'National Archives of Australia / Royal Australian Air Force');
 CREATE TABLE discovery_cross_reference(cross_reference_id INTEGER PRIMARY KEY,raw_text TEXT NOT NULL);
 CREATE TABLE text_access_copy(access_copy_id INTEGER PRIMARY KEY,url TEXT UNIQUE NOT NULL,slug TEXT,title TEXT,archival_key TEXT,http_status INTEGER,html_sha256 TEXT,text_sha256 TEXT,text_chars INTEGER,visible_text TEXT,error TEXT,authority_note TEXT);
 CREATE TABLE text_access_page(access_page_id INTEGER PRIMARY KEY,access_copy_id INTEGER NOT NULL REFERENCES text_access_copy(access_copy_id),page_number INTEGER NOT NULL,extraction_method TEXT NOT NULL,text TEXT NOT NULL,text_sha256 TEXT NOT NULL,UNIQUE(access_copy_id,page_number));
 CREATE TABLE file_access_match(match_id INTEGER PRIMARY KEY,source_file_id INTEGER REFERENCES source_file(source_file_id),access_copy_id INTEGER NOT NULL REFERENCES text_access_copy(access_copy_id),match_method TEXT NOT NULL,match_status TEXT NOT NULL,detail TEXT);
 CREATE INDEX idx_file_series ON source_file(series_hint); CREATE INDEX idx_page_copy ON text_access_page(access_copy_id,page_number);
 ''')
 ledger=list(ldb.execute('select * from numbered_file order by entry_number'))
 for r in ledger: con.execute('insert into source_file(source_file_id,entry_number,section,series_hint,barcode,page_count_hint,access_hint,disposition,naa_digital_hint,raw_discovery_text) values(?,?,?,?,?,?,?,?,?,?)',(r['entry_number'],r['entry_number'],r['section'],r['series_hint'],r['barcode'],r['page_count_hint'],r['access_hint'],r['disposition'],r['naa_digital_hint'],r['raw_entry_text']))
 for r in ldb.execute('select raw_section_text from section_c_reference'):con.execute('insert into discovery_cross_reference(raw_text) values(?)',(r[0],))
 copies=list(tdb.execute('select * from access_copy order by access_copy_id'))
 total_pages=0
 for r in copies:
  key=title_key(r['title']);con.execute('insert into text_access_copy values(?,?,?,?,?,?,?,?,?,?,?,?)',(r['access_copy_id'],r['url'],r['slug'],r['title'],key,r['http_status'],r['html_sha256'],r['text_sha256'],r['text_chars'],r['visible_text'],r['error'],r['authority_note']))
  pages=extract_pages(r['visible_text'] or '')
  for p in pages:
   con.execute('insert into text_access_page(access_copy_id,page_number,extraction_method,text,text_sha256) values(?,?,?,?,?)',(r['access_copy_id'],p['page_number'],p['extraction_method'],p['text'],p['text_sha256']));total_pages+=1
  # Conservative match: normalized archival key must appear verbatim in discovery text after whitespace folding.
  candidates=[]
  if key:
   k=re.sub(r'\s+',' ',key.upper())
   for f in ledger:
    raw=re.sub(r'\s+',' ',f['raw_entry_text'].upper())
    if k in raw or (f['series_hint'] and f['series_hint'].upper() in k and any(tok in raw for tok in k.split()[1:])): candidates.append(f['entry_number'])
  if len(candidates)==1: con.execute('insert into file_access_match(source_file_id,access_copy_id,match_method,match_status,detail) values(?,?,?,?,?)',(candidates[0],r['access_copy_id'],'ARCHIVAL_KEY_IN_DISCOVERY_ENTRY','MATCHED',key))
  else: con.execute('insert into file_access_match(source_file_id,access_copy_id,match_method,match_status,detail) values(?,?,?,?,?)',(None,r['access_copy_id'],'ARCHIVAL_KEY_IN_DISCOVERY_ENTRY','AMBIGUOUS_OR_UNMATCHED',json.dumps({'archival_key':key,'candidates':candidates})))
 con.commit();quick=con.execute('pragma integrity_check').fetchone()[0];fk=con.execute('pragma foreign_key_check').fetchall()
 matched=con.execute("select count(*) from file_access_match where match_status='MATCHED'").fetchone()[0];unmatched=con.execute("select count(*) from file_access_match where match_status!='MATCHED'").fetchone()[0]
 file_count=con.execute('select count(*) from source_file').fetchone()[0];copy_count=con.execute('select count(*) from text_access_copy').fetchone()[0];page_count=con.execute('select count(*) from text_access_page').fetchone()[0];con.close();ldb.close();tdb.close()
 summary={'status':'PASS' if quick=='ok' and not fk and file_count==155 and copy_count==21 else 'FAIL','government_file_ledger_rows':file_count,'text_access_copies':copy_count,'source_text_pages_extracted':page_count,'access_copy_matches':matched,'access_copy_ambiguous_or_unmatched':unmatched,'sqlite_integrity_check':quick,'foreign_key_violations':len(fk),'authority_policy':'NAA/RAAF is source authority; Project 1947 is discovery aid; UFO Transparency is searchable technical access only.','next_stage':'Extract individual UFO case/incident candidates under the standalone UFO specification with exact file/page provenance.'}
 (out/'SUMMARY.json').write_text(json.dumps(summary,indent=2)+'\n')
 with (out/'SOURCE_FILE_INDEX.csv').open('w',newline='',encoding='utf-8') as f:
  w=csv.writer(f);w.writerow(['entry_number','section','series_hint','barcode','disposition']);
  for r in ledger:w.writerow([r['entry_number'],r['section'],r['series_hint'],r['barcode'],r['disposition']])
 checks=[]
 for p in sorted(out.rglob('*')):
  if p.is_file() and p.name!='SHA256SUMS.txt':checks.append(f'{sha(p)}  {p.relative_to(out).as_posix()}')
 (out/'SHA256SUMS.txt').write_text('\n'.join(checks)+'\n');print(json.dumps(summary,indent=2))
 if summary['status']!='PASS':raise SystemExit(1)
if __name__=='__main__':main()
