#!/usr/bin/env python3
"""Acquire searchable public text access copies for Australian government UFO files.

NAA/RAAF records remain the underlying source authority. UFO Transparency is used only as a
public technical access/transcription layer where RecordSearch automation is limited. Site
summaries are retained as secondary-source metadata, never promoted to official findings.
"""
from __future__ import annotations
import argparse,hashlib,json,re,sqlite3,time
from pathlib import Path
from urllib.parse import urljoin,urlparse
import requests
from bs4 import BeautifulSoup

LIST='https://ufotransparency.com/international/files/au'
UA='Watchers-UFO-Atlas/1.0 (public research; low-rate)'

def sha_bytes(b:bytes)->str:return hashlib.sha256(b).hexdigest()
def clean(s:str)->str:return re.sub(r'\s+',' ',s).strip()

def discover(session:requests.Session)->list[str]:
 r=session.get(LIST,timeout=120);r.raise_for_status();s=BeautifulSoup(r.text,'html.parser')
 urls=[]
 for a in s.find_all('a',href=True):
  href=a['href']
  if href.startswith('/files/intl-au-'):
   u=urljoin(LIST,href)
   if u not in urls:urls.append(u)
 return urls

def acquire(args):
 out=Path(args.output_dir);out.mkdir(parents=True,exist_ok=True);session=requests.Session();session.headers['User-Agent']=UA
 urls=discover(session)
 if len(urls)<10:raise SystemExit(f'Only {len(urls)} Australian access-copy file pages discovered')
 rows=[]
 for i,url in enumerate(urls,1):
  try:
   r=session.get(url,timeout=180);r.raise_for_status();raw=r.content;soup=BeautifulSoup(raw,'html.parser');title=clean((soup.find('h1').get_text(' ',strip=True) if soup.find('h1') else soup.title.get_text(' ',strip=True) if soup.title else url))
   text=soup.get_text('\n',strip=True)
   # Extracted-text pages are embedded in the file page. Preserve the whole visible page text;
   # later extraction can separate source transcript from the mirror's editorial wrapper.
   rec={'access_copy_url':url,'slug':urlparse(url).path.rsplit('/',1)[-1],'title':title,'http_status':r.status_code,'html_sha256':sha_bytes(raw),'text_sha256':hashlib.sha256(text.encode()).hexdigest(),'text_chars':len(text),'visible_text':text,'authority_note':'Underlying document attributed to RAAF/NAA on the access-copy page; mirror text is secondary technical access, not controlling official metadata.'}
   rows.append(rec)
   (out/f'{i:03d}_{rec["slug"]}.txt').write_text(text,encoding='utf-8')
  except Exception as e:
   rows.append({'access_copy_url':url,'slug':urlparse(url).path.rsplit('/',1)[-1],'title':None,'http_status':None,'error':f'{type(e).__name__}: {e}','visible_text':None})
  time.sleep(args.delay)
 (out/'ACCESS_COPY_RECORDS.json').write_text(json.dumps(rows,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
 db=out/'AUSTRALIA_PUBLIC_TEXT_ACCESS_v0.1.0.sqlite';con=sqlite3.connect(db)
 con.execute('CREATE TABLE access_copy(access_copy_id INTEGER PRIMARY KEY,url TEXT UNIQUE NOT NULL,slug TEXT,title TEXT,http_status INTEGER,html_sha256 TEXT,text_sha256 TEXT,text_chars INTEGER,visible_text TEXT,error TEXT,authority_note TEXT)')
 for i,r in enumerate(rows,1):con.execute('INSERT INTO access_copy VALUES(?,?,?,?,?,?,?,?,?,?,?)',(i,r.get('access_copy_url'),r.get('slug'),r.get('title'),r.get('http_status'),r.get('html_sha256'),r.get('text_sha256'),r.get('text_chars'),r.get('visible_text'),r.get('error'),r.get('authority_note')))
 con.commit();quick=con.execute('pragma quick_check').fetchone()[0];fk=con.execute('pragma foreign_key_check').fetchall();con.close()
 ok=[r for r in rows if r.get('visible_text')]
 summary={'status':'PASS' if quick=='ok' and not fk and ok else 'FAIL','listing_url':LIST,'discovered_file_pages':len(rows),'text_access_pages_recovered':len(ok),'failed_access_pages':len(rows)-len(ok),'total_visible_text_characters':sum(r.get('text_chars',0) or 0 for r in ok),'sqlite_quick_check':quick,'foreign_key_violations':len(fk),'source_policy':'NAA/RAAF remains source authority; mirror is a public text-access layer only.'}
 (out/'SUMMARY.json').write_text(json.dumps(summary,indent=2)+'\n');
 checks=[]
 for p in sorted(out.rglob('*')):
  if p.is_file() and p.name!='SHA256SUMS.txt':
   checks.append(f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(out).as_posix()}')
 (out/'SHA256SUMS.txt').write_text('\n'.join(checks)+'\n');print(json.dumps(summary,indent=2))
 if summary['status']!='PASS':raise SystemExit(1)

def main():
 p=argparse.ArgumentParser();p.add_argument('--output-dir',required=True);p.add_argument('--delay',type=float,default=1.5);acquire(p.parse_args())
if __name__=='__main__':main()
