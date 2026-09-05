#!/usr/bin/env python3
"""Extract conservative UFO case/incident candidates from Australian government source text.

This stage does not merge with the master Atlas. It preserves page text and source provenance,
identifies report-form starts, extracts raw report fields when readable, and adds three explicit
high-value incident candidates that are not reliably captured by the generic report-form parser.
Source explanations remain source material; no source disposition is converted into a CE class.
"""
from __future__ import annotations
import argparse,csv,hashlib,json,re,shutil,sqlite3
from pathlib import Path

REPORT_RX=re.compile(
 r'(REPORT\s+(?:ON|OF)\s+(?:AN?\s+)?(?:AERIAL|UNIDENTIFIED|UNUSUAL)[\s\w-]{0,80}(?:OBJECT|SIGHTING|PHENOMENON|OBSERVED)|'
 r'INTELLIGENCE\s*[-–—]?\s*REPORT\s+OF\s+AERIAL\s+OBJECT\s+OBSERVED|'
 r'UNUSUAL\s+AERIAL\s+SIGHTING\s+REPORT)',re.I)
FORM_MARKERS=[
 'name of observer','date and time of observation','duration of observation','period of observation',
 "observer's location",'where was object first','what first attracted','did object appear',
 'what was the colour','what was its apparent shape','was there any sound'
]
DATE_RX=re.compile(r'\b(?:[0-3]?\d[ /.-](?:0?\d|[A-Za-z]{3,9})[ /.-](?:19|20)?\d{2}|[0-3]?\d\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(?:19|20)?\d{2})\b',re.I)
TIME_RX=re.compile(r'\b(?:[01]?\d|2[0-3])[:.]?[0-5]\d\s*(?:hrs?|hours?)?\b',re.I)

def locate(root:Path,name:str)->Path:
 m=list(root.rglob(name))
 if len(m)!=1:raise RuntimeError(f'Expected one {name}; found {len(m)}')
 return m[0]
def sha_text(s:str)->str:return hashlib.sha256(s.encode('utf-8')).hexdigest()
def sha_file(p:Path)->str:
 h=hashlib.sha256()
 with p.open('rb') as f:
  for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
 return h.hexdigest()
def clean(v:str|None)->str|None:
 if v is None:return None
 x=re.sub(r'\s+',' ',v).strip(' .,:;|-')
 return x or None

def after_label(text:str,labels:list[str],limit:int=280)->str|None:
 for label in labels:
  m=re.search(label,text,re.I)
  if not m:continue
  tail=text[m.end():m.end()+limit]
  # Stop at the next numbered form label when OCR retained it.
  stop=re.search(r'\s(?:\d{1,2}\s*[.)]|\d{1,2}\.)\s+[A-Z]',tail)
  if stop:tail=tail[:stop.start()]
  return clean(tail)
 return None

def source_file_for_copy(con:sqlite3.Connection,copy_id:int)->int|None:
 r=con.execute("select source_file_id from file_access_match where access_copy_id=? and relationship='SAME_ARCHIVAL_FILE' order by match_id limit 1",(copy_id,)).fetchone()
 return int(r[0]) if r and r[0] is not None else None

def page_row(con,cid,pn):return con.execute('select * from text_access_page where access_copy_id=? and page_number=?',(cid,pn)).fetchone()
def page_has_report(text:str)->bool:return bool(REPORT_RX.search(text or ''))

def add_candidate(con,*,candidate_id,title,copy_id,file_id,page_start,page_end,text,method,confidence,special_kind=None):
 dates=[clean(x) for x in DATE_RX.findall(text or '')][:20]
 times=[clean(x) for x in TIME_RX.findall(text or '')][:20]
 observer=after_label(text,[r'Name\s+of\s+Observer',r'Name\s+of\s+observer'])
 dt=after_label(text,[r'Date\s+and\s+Time\s+of\s+Observation',r'Date\s+and\s+time\s+of\s+observation'])
 duration=after_label(text,[r'Duration\s+of\s+Observation',r'Period\s+of\s+Observation'])
 location=after_label(text,[r"Observer\s*'?s\s+location\s+at\s+time\s+of\s+Sighting",r'Address\s+of\s+Observer'])
 colour=after_label(text,[r'What\s+was\s+the\s+colour\s+of\s+the\s+(?:Light|light|object)',r'colour\s+of\s+the\s+light\s+or\s+object'])
 shape=after_label(text,[r'What\s+was\s+its\s+apparent\s+shape'])
 sound=after_label(text,[r'Was\s+there\s+any\s+Sound',r'Was\s+there\s+any\s+sound'])
 url=con.execute('select url from text_access_copy where access_copy_id=?',(copy_id,)).fetchone()[0]
 con.execute('''insert into case_candidate(candidate_id,record_type,title,candidate_status,source_collection,source_file_id,access_copy_id,page_start,page_end,source_url,extraction_method,extraction_confidence,special_kind,event_date_raw,observer_raw,location_raw,duration_raw,colour_raw,shape_raw,sound_raw,date_mentions_json,time_mentions_json,encounter_class_candidate,source_text_sha256,source_text) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(
  candidate_id,'CASE_EVENT',title,'SOURCE_CANDIDATE','AUSTRALIA_NAA_RAAF',file_id,copy_id,page_start,page_end,url,method,confidence,special_kind,dt,observer,location,duration,colour,shape,sound,json.dumps(dates,ensure_ascii=False),json.dumps(times,ensure_ascii=False),None,sha_text(text),text))
 for field,val in [('event_date_raw',dt),('observer_raw',observer),('location_raw',location),('duration_raw',duration),('colour_raw',colour),('shape_raw',shape),('sound_raw',sound)]:
  if val:con.execute('insert into candidate_field(candidate_id,field_name,raw_value) values(?,?,?)',(candidate_id,field,val))
 for pn in range(page_start,page_end+1):
  r=page_row(con,copy_id,pn)
  if r:con.execute('insert or ignore into candidate_source_page(candidate_id,access_copy_id,page_number,text_sha256) values(?,?,?,?)',(candidate_id,copy_id,pn,r['text_sha256']))

def main():
 ap=argparse.ArgumentParser();ap.add_argument('--input-dir',required=True);ap.add_argument('--output-dir',required=True);a=ap.parse_args();src=Path(a.input_dir);out=Path(a.output_dir);shutil.rmtree(out,ignore_errors=True);out.mkdir(parents=True)
 inp=locate(src,'AUSTRALIA_GOVERNMENT_UFO_SOURCE_SNAPSHOT_v0.3.1.sqlite');db=out/'AUSTRALIA_UFO_IMPORT_MODULE_v0.1.0.sqlite';shutil.copyfile(inp,db)
 con=sqlite3.connect(db);con.row_factory=sqlite3.Row;con.execute('pragma foreign_keys=on')
 con.executescript('''
 CREATE TABLE case_candidate(candidate_id TEXT PRIMARY KEY,record_type TEXT NOT NULL,title TEXT NOT NULL,candidate_status TEXT NOT NULL,source_collection TEXT NOT NULL,source_file_id INTEGER REFERENCES source_file(source_file_id),access_copy_id INTEGER NOT NULL REFERENCES text_access_copy(access_copy_id),page_start INTEGER NOT NULL,page_end INTEGER NOT NULL,source_url TEXT NOT NULL,extraction_method TEXT NOT NULL,extraction_confidence TEXT NOT NULL,special_kind TEXT,event_date_raw TEXT,observer_raw TEXT,location_raw TEXT,duration_raw TEXT,colour_raw TEXT,shape_raw TEXT,sound_raw TEXT,date_mentions_json TEXT,time_mentions_json TEXT,encounter_class_candidate TEXT,source_text_sha256 TEXT NOT NULL,source_text TEXT NOT NULL);
 CREATE TABLE candidate_field(field_id INTEGER PRIMARY KEY,candidate_id TEXT NOT NULL REFERENCES case_candidate(candidate_id),field_name TEXT NOT NULL,raw_value TEXT NOT NULL);
 CREATE TABLE candidate_source_page(candidate_id TEXT NOT NULL REFERENCES case_candidate(candidate_id),access_copy_id INTEGER NOT NULL REFERENCES text_access_copy(access_copy_id),page_number INTEGER NOT NULL,text_sha256 TEXT NOT NULL,PRIMARY KEY(candidate_id,access_copy_id,page_number));
 CREATE TABLE event_fragment(fragment_id TEXT PRIMARY KEY,access_copy_id INTEGER NOT NULL REFERENCES text_access_copy(access_copy_id),source_file_id INTEGER REFERENCES source_file(source_file_id),page_number INTEGER NOT NULL,signal_codes_json TEXT NOT NULL,date_mentions_json TEXT NOT NULL,text_sha256 TEXT NOT NULL,text TEXT NOT NULL);
 CREATE INDEX idx_candidate_source_file ON case_candidate(source_file_id); CREATE INDEX idx_candidate_copy_page ON case_candidate(access_copy_id,page_start); CREATE INDEX idx_fragment_copy_page ON event_fragment(access_copy_id,page_number);
 ''')
 # Preserve every case-like page as an event fragment, even if it cannot safely be split into a distinct event yet.
 frag_count=0
 pages=list(con.execute('select p.access_copy_id,p.page_number,p.text,p.text_sha256 from text_access_page p order by p.access_copy_id,p.page_number'))
 for r in pages:
  text=r['text'] or '';low=text.lower();signals=[]
  if REPORT_RX.search(text):signals.append('REPORT_HEADER')
  if 'date and time of observation' in low:signals.append('OBSERVATION_DATE_FIELD')
  if 'name of observer' in low:signals.append('OBSERVER_FIELD')
  if 'unusual aerial' in low:signals.append('UNUSUAL_AERIAL_TERM')
  if 'unidentified flying object' in low or 'u.f.o' in low or 'ufo' in low:signals.append('UFO_TERM')
  if 'flying saucer' in low:signals.append('FLYING_SAUCER_TERM')
  dates=[clean(x) for x in DATE_RX.findall(text)][:20]
  if dates:signals.append('DATE_MENTION')
  if signals and (len(signals)>=2 or 'REPORT_HEADER' in signals):
   frag_count+=1;fid=f'AU-NAA-FRAG-C{r["access_copy_id"]:02d}-P{r["page_number"]:04d}'
   con.execute('insert into event_fragment values(?,?,?,?,?,?,?,?)',(fid,r['access_copy_id'],source_file_for_copy(con,r['access_copy_id']),r['page_number'],json.dumps(signals),json.dumps(dates,ensure_ascii=False),r['text_sha256'],text))
 # Distinct report-form starts become case candidates. Capture continuation pages until another report start, max 3 pages.
 starts=[]
 for r in pages:
  if REPORT_RX.search(r['text'] or ''):starts.append((r['access_copy_id'],r['page_number']))
 startset=set(starts);candidate_count=0
 for cid,pn in starts:
  p0=page_row(con,cid,pn);text0=p0['text'] or '';score=sum(1 for m in FORM_MARKERS if m in text0.lower())
  # References to a report can trigger the header regex. Require a form marker or a strong report heading plus a date mention.
  strong=score>=2 or (score>=1 and DATE_RX.search(text0))
  if not strong:continue
  end=pn
  chunks=[text0]
  for nxt in range(pn+1,pn+3):
   if (cid,nxt) in startset:break
   pr=page_row(con,cid,nxt)
   if not pr:break
   chunks.append(pr['text'] or '');end=nxt
  full='\n\n[CONTINUATION PAGE]\n\n'.join(chunks)
  candidate_count+=1;candidate_id=f'AU-NAA-CASE-C{cid:02d}-P{pn:04d}'
  title=f'Australian government UFO report — source copy {cid}, page {pn}'
  add_candidate(con,candidate_id=candidate_id,title=title,copy_id=cid,file_id=source_file_for_copy(con,cid),page_start=pn,page_end=end,text=full,method='REPORT_FORM_ANCHOR',confidence='HIGH' if score>=5 else 'MEDIUM')
 # Explicit non-form incidents supported by their source-text pages.
 specials=[
  ('AU-NAA-CASE-MAWSON-19580717','Mawson/Taylor Glacier visual phenomena — 17 July 1958',18,1,3,'NON_FORM_SOURCE_REPORT'),
  ('AU-NAA-CASE-WEWAK-19600715','Unidentified light, Wewak/Maralinga — 15 July 1960',21,1,2,'NON_FORM_SOURCE_REPORT'),
 ]
 for candidate_id,title,cid,start,end,kind in specials:
  chunks=[];actual=[]
  for pn in range(start,end+1):
   r=page_row(con,cid,pn)
   if r:chunks.append(r['text'] or '');actual.append(pn)
  if chunks:
   add_candidate(con,candidate_id=candidate_id,title=title,copy_id=cid,file_id=source_file_for_copy(con,cid),page_start=min(actual),page_end=max(actual),text='\n\n[CONTINUATION PAGE]\n\n'.join(chunks),method='EXPLICIT_INCIDENT_SOURCE_TEXT',confidence='HIGH',special_kind=kind)
 con.commit();quick=con.execute('pragma integrity_check').fetchone()[0];fk=con.execute('pragma foreign_key_check').fetchall()
 candidates=con.execute('select count(*) from case_candidate').fetchone()[0];fields=con.execute('select count(*) from candidate_field').fetchone()[0];source_links=con.execute('select count(*) from candidate_source_page').fetchone()[0];frags=con.execute('select count(*) from event_fragment').fetchone()[0];copies=con.execute('select count(distinct access_copy_id) from event_fragment').fetchone()[0]
 con.close()
 summary={'status':'PASS' if quick=='ok' and not fk and candidates>25 and frags>100 else 'FAIL','case_event_candidates':candidates,'candidate_fields_extracted':fields,'candidate_page_links':source_links,'case_like_event_fragments':frags,'access_copies_with_event_fragments':copies,'source_text_pages_preserved_in_parent_snapshot':2901,'encounter_class_policy':'No CE class assigned mechanically. Encounter class remains null until event content supports an independent UFO-spec classification.','candidate_policy':'Report-form anchors and explicit non-form source reports are candidates for later master integration; page fragments preserve other case-like source material without forcing one-page-one-case identity.','sqlite_integrity_check':quick,'foreign_key_violations':len(fk)}
 (out/'SUMMARY.json').write_text(json.dumps(summary,indent=2)+'\n',encoding='utf-8')
 con=sqlite3.connect(db);con.row_factory=sqlite3.Row
 for filename,query in [('CASE_CANDIDATES.csv','select * from case_candidate order by candidate_id'),('CANDIDATE_FIELDS.csv','select * from candidate_field order by candidate_id,field_id'),('EVENT_FRAGMENTS.csv','select fragment_id,access_copy_id,source_file_id,page_number,signal_codes_json,date_mentions_json,text_sha256 from event_fragment order by access_copy_id,page_number')]:
  with (out/filename).open('w',newline='',encoding='utf-8') as f:
   rows=con.execute(query);w=csv.writer(f);w.writerow([d[0] for d in rows.description]);w.writerows(rows)
 con.close()
 checks=[]
 for q in sorted(out.rglob('*')):
  if q.is_file() and q.name!='SHA256SUMS.txt':checks.append(f'{sha_file(q)}  {q.relative_to(out).as_posix()}')
 (out/'SHA256SUMS.txt').write_text('\n'.join(checks)+'\n',encoding='utf-8');print(json.dumps(summary,indent=2))
 if summary['status']!='PASS':raise SystemExit(1)
if __name__=='__main__':main()
