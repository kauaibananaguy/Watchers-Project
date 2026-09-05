#!/usr/bin/env python3
"""Build a lossless Australian-government UFO file inventory from Basterfield's 2016 index.

NAA/RecordSearch remains the source authority. This index is only a discovery/access ledger.
Sections A (Archive Act), B (FOI/destroyed/missing), and C (cross-referenced files not yet located)
are all retained so unavailable material is not silently dropped.
"""
from __future__ import annotations
import argparse,csv,hashlib,json,re,sqlite3
from pathlib import Path
import requests
from pypdf import PdfReader

INDEX_URL='https://www.project1947.com/kbcat/kbuap2016.pdf'
UA='Watchers-UFO-Atlas/1.0 (archival research; low-rate)'

def clean(s:str)->str:return re.sub(r'\s+',' ',s.replace('\x02',' ')).strip()
def sha(p:Path)->str:
 h=hashlib.sha256();
 with p.open('rb') as f:
  for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
 return h.hexdigest()

def numbered_records(text:str)->list[tuple[int,str]]:
 a=text.find('SECTION A:'); c=text.find('SECTION C FILES')
 if a<0 or c<0: raise RuntimeError('Required section headings not found')
 body=text[a:c]
 candidates=list(re.finditer(r'(?m)^\s*(\d{1,3})(?:\s*\.\s*|\s+)(?=\S)',body))
 chosen=[]; cursor=0
 # The source document explicitly numbers the Archive/FOI ledger continuously 1..155.
 for expected in range(1,156):
  match=next((m for m in candidates if int(m.group(1))==expected and m.start()>=cursor),None)
  if match is None: raise RuntimeError(f'Missing numbered source entry {expected}')
  chosen.append(match); cursor=match.end()
 rows=[]
 for i,m in enumerate(chosen):
  stop=chosen[i+1].start() if i+1<len(chosen) else len(body)
  rows.append((i+1,clean(body[m.start():stop])))
 return rows

def parse(n:int,raw:str)->dict:
 series=None
 # Avoid treating ordinary years as series numbers. Prefer common archival series shapes.
 sm=re.search(r'\b([A-Z]{1,4}\d+(?:/\d+)?)\b',raw)
 if sm:series=sm.group(1)
 barcode=None
 bm=re.search(r'\b(?:Canberra|Melbourne|Sydney|Adelaide|Darwin|Brisbane|Perth|AWM)\s+(\d{5,9})\b',raw,re.I)
 if bm: barcode=bm.group(1)
 pages=None
 pm=re.search(r'\b(\d{1,4})\s*pp\b',raw,re.I)
 if pm: pages=int(pm.group(1))
 status='DISCOVERY_REFERENCE'
 low=raw.lower()
 if n<=123:
  status='ARCHIVE_ACT_FILE'
 elif 'destroyed' in low:
  status='DESTROYED'
 elif 'could not' in low or 'not located' in low or 'could not find' in low:
  status='NOT_LOCATED'
 elif 'released with deletions' in low:
  status='FOI_RELEASED_WITH_DELETIONS'
 elif 'foi pdf' in low or 'released by the dod' in low:
  status='FOI_RELEASE'
 else:
  status='FOI_OR_RESEARCH_REFERENCE'
 access='OPEN_WITH_EXCEPTION' if re.search(r'open\s+with\s+exception|\bOWE\b',raw,re.I) else ('OPEN' if re.search(r'\bOpen\b',raw) else None)
 return {'entry_number':n,'section':'A' if n<=123 else 'B','series_hint':series,'barcode':barcode,'page_count_hint':pages,'access_hint':access,'disposition':status,'naa_digital_hint':bool(re.search(r'NAA\s+digital\s+file',raw,re.I)),'raw_entry_text':raw}

def section_c(text:str)->str:
 pos=text.find('SECTION C FILES')
 if pos<0: raise RuntimeError('Section C not found')
 return text[pos:]

def main():
 ap=argparse.ArgumentParser();ap.add_argument('--output-dir',required=True);a=ap.parse_args();out=Path(a.output_dir);out.mkdir(parents=True,exist_ok=True)
 r=requests.get(INDEX_URL,headers={'User-Agent':UA},timeout=120);r.raise_for_status();pdf=out/'AUSTRALIAN_GOVERNMENT_UAP_FILES_LISTING_2016.pdf';pdf.write_bytes(r.content)
 text='\n'.join(p.extract_text() or '' for p in PdfReader(str(pdf)).pages);(out/'DISCOVERY_INDEX_TEXT.txt').write_text(text,encoding='utf-8')
 numbered=[parse(n,raw) for n,raw in numbered_records(text)]
 if [r['entry_number'] for r in numbered]!=list(range(1,156)):raise RuntimeError('Numbered source ledger is not continuous 1..155')
 ctext=section_c(text);(out/'SECTION_C_REFERENCES.txt').write_text(ctext,encoding='utf-8')
 (out/'NUMBERED_LEDGER.json').write_text(json.dumps(numbered,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
 with (out/'NUMBERED_LEDGER.csv').open('w',newline='',encoding='utf-8') as f:
  w=csv.DictWriter(f,fieldnames=list(numbered[0]));w.writeheader();w.writerows(numbered)
 db=out/'AUSTRALIA_GOVERNMENT_UFO_FILE_INVENTORY_v0.2.0.sqlite';con=sqlite3.connect(db)
 con.execute('CREATE TABLE numbered_file(entry_number INTEGER PRIMARY KEY,section TEXT NOT NULL,series_hint TEXT,barcode TEXT,page_count_hint INTEGER,access_hint TEXT,disposition TEXT NOT NULL,naa_digital_hint INTEGER NOT NULL,raw_entry_text TEXT NOT NULL)')
 con.executemany('INSERT INTO numbered_file VALUES(?,?,?,?,?,?,?,?,?)',[(r['entry_number'],r['section'],r['series_hint'],r['barcode'],r['page_count_hint'],r['access_hint'],r['disposition'],int(r['naa_digital_hint']),r['raw_entry_text']) for r in numbered])
 con.execute('CREATE TABLE section_c_reference(reference_id INTEGER PRIMARY KEY,raw_section_text TEXT NOT NULL)');con.execute('INSERT INTO section_c_reference(raw_section_text) VALUES(?)',(ctext,));con.commit()
 quick=con.execute('pragma quick_check').fetchone()[0];fk=con.execute('pragma foreign_key_check').fetchall();counts=dict(con.execute('SELECT disposition,count(*) FROM numbered_file GROUP BY disposition'));con.close()
 summary={'status':'PASS' if quick=='ok' and not fk else 'FAIL','discovery_index_url':INDEX_URL,'discovery_index_sha256':sha(pdf),'numbered_entries':155,'section_a_entries':123,'section_b_entries':32,'section_c_preserved':True,'entries_with_barcode':sum(bool(r['barcode']) for r in numbered),'entries_marked_naa_digital':sum(r['naa_digital_hint'] for r in numbered),'disposition_counts':counts,'sqlite_quick_check':quick,'foreign_key_violations':len(fk),'authority_policy':'NAA/RecordSearch controls official metadata; the 2016 listing is a discovery/access aid only.'}
 (out/'SUMMARY.json').write_text(json.dumps(summary,indent=2)+'\n');
 checks=[]
 for p in sorted(out.rglob('*')):
  if p.is_file() and p.name!='SHA256SUMS.txt':checks.append(f'{sha(p)}  {p.relative_to(out).as_posix()}')
 (out/'SHA256SUMS.txt').write_text('\n'.join(checks)+'\n');print(json.dumps(summary,indent=2))
 if summary['status']!='PASS':raise SystemExit(1)
if __name__=='__main__':main()
