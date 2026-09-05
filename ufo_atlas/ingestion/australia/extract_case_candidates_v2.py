#!/usr/bin/env python3
"""Expand Australian government UFO source candidates without touching the canonical Atlas.

Uses verified government-UFO source files and public text access copies to identify report-form starts,
preserve all case-like fragments, attach official NAA provenance, create an adjudication queue for
unresolved case boundaries, and emit source-ordered narrative seeds. Narrative seeds are never final
Atlas narratives and CE classifications are deliberately left unassigned.
"""
from __future__ import annotations
import argparse,csv,hashlib,json,re,shutil,sqlite3,collections
from pathlib import Path

REPORT_RX=re.compile(r'(REPORT\s+(?:ON|OF)\s+(?:AN?\s+)?(?:AERIAL|UNIDENTIFIED|UNUSUAL)[\s\w-]{0,80}(?:OBJECT|SIGHTING|PHENOMENON|OBSERVED)|INTELLIGENCE\s*[-–—]?\s*REPORT\s+OF\s+AERIAL\s+OBJECT\s+OBSERVED|UNUSUAL\s+AERIAL\s+SIGHTING\s+REPORT)',re.I)
START_PATTERNS=[re.compile(x,re.I) for x in [r'name\s+of\s+observers?',r'address\s+of\s+observer',r'occupation\s+of\s+observer',r'date\s+and\s+time\s+of\s+observation',r'duration\s+of\s+observation|period\s+of\s+observation',r"observer\s*'?s\s+location"]]
DATE_RX=re.compile(r'\b(?:[0-3]?\d[ /.-](?:0?\d|[A-Za-z]{3,9})[ /.-](?:19|20)?\d{2}|[0-3]?\d\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(?:19|20)?\d{2})\b',re.I)
TIME_RX=re.compile(r'\b(?:[01]?\d|2[0-3])[:.]?[0-5]\d\s*(?:hrs?|hours?)?\b',re.I)

def sha_text(s:str)->str:return hashlib.sha256(s.encode('utf-8')).hexdigest()
def sha_file(p:Path)->str:
 h=hashlib.sha256()
 with p.open('rb') as f:
  for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
 return h.hexdigest()
def locate(root:Path,name:str)->Path:
 hits=list(root.rglob(name))
 if len(hits)!=1:raise RuntimeError(f'Expected one {name}; found {len(hits)}')
 return hits[0]
def clean(v:str|None)->str|None:
 if v is None:return None
 x=re.sub(r'\s+',' ',v).strip(' .,:;|-');return x or None
def start_score(text:str)->int:return sum(bool(rx.search(text or '')) for rx in START_PATTERNS)
def is_object_report(text:str)->bool:
 low=(text or '').lower();return bool(REPORT_RX.search(text or '')) or any(k in low for k in ('aerial object','unusual aerial','unidentified flying object','u.f.o',' ufo'))
def after_label(text:str,patterns:list[str],limit:int=240)->str|None:
 for pat in patterns:
  m=re.search(pat,text,re.I)
  if not m:continue
  tail=text[m.end():m.end()+limit]
  stop=re.search(r'\s(?:\d{1,2}\s*[.)]|\d{1,2}\.)\s+[A-Z]',tail)
  if stop:tail=tail[:stop.start()]
  stop2=re.search(r'\b(?:Address|Occupation|Date\s+and\s+Time|Duration|Period\s+of\s+Observation|Manner\s+of\s+Observation|Weather\s+Conditions|What\s+was\s+its\s+apparent\s+shape|Was\s+there\s+any\s+sound)\b',tail,re.I)
  if stop2 and stop2.start()>3:tail=tail[:stop2.start()]
  return clean(tail)
 return None
def quality(v:str|None)->str:
 if not v:return 'MISSING'
 s=v.strip();letters=sum(ch.isalpha() for ch in s);punct=sum((not ch.isalnum()) and (not ch.isspace()) for ch in s)
 if len(s)>180 or letters<2:return 'LOW'
 if punct/max(1,len(s))>0.24:return 'LOW'
 if letters>=5 and punct/max(1,len(s))<0.14:return 'HIGH'
 return 'MEDIUM'
def field_quality(field_name:str,v:str|None)->str:
 q=quality(v)
 if q=='MISSING':return q
 s=(v or '').strip();low=s.lower()
 if field_name=='observer_raw':
  if re.search(r'\b(report|object|observed|address of observer|occupation of observer|date and time|the rectory|school of radio)\b',low):return 'LOW'
  if len(re.findall(r'[A-Za-z]{2,}',s))<1:return 'LOW'
 if field_name=='location_raw':
  if re.search(r'\b(weather conditions|occupation of observer|date and time|duration of observation|manner of observation)\b',low):return 'LOW'
  if len(re.findall(r'[A-Za-z]{2,}',s))<1:return 'LOW'
 if field_name=='event_date_raw':
  if not DATE_RX.search(s) and len(re.findall(r'\d',s))<4:return 'LOW'
 if field_name=='duration_raw' and not re.search(r'\b(sec(?:ond)?s?|mins?|minutes?|hrs?|hours?)\b|\d',s,re.I):return 'LOW'
 if field_name=='colour_raw' and re.search(r'what was|apparent shape|structure observable|propulsion',low):return 'LOW'
 if field_name=='shape_raw' and re.search(r'structure observable|propulsion|was there any sound|height|speed',low):return 'LOW'
 if field_name=='sound_raw':
  if re.search(r'height|speed|angular velocity|direction of flight',low):return 'LOW'
  if low in {'no','none','nil','yes'}:return 'HIGH'
 return q
def src_file_for_copy(con,cid):
 r=con.execute("select source_file_id from file_access_match where access_copy_id=? and relationship='SAME_ARCHIVAL_FILE' order by match_id limit 1",(cid,)).fetchone();return int(r[0]) if r and r[0] is not None else None

def fields(text:str)->dict:
 return {'event_date_raw':after_label(text,[r'Date\s+and\s+Time\s+of\s+Observation']),'observer_raw':after_label(text,[r'Name\s+of\s+Observers?']),'location_raw':after_label(text,[r"Observer\s*'?s\s+location(?:\s+at\s+time\s+of\s+Sighting)?",r'Address\s+of\s+Observer']),'duration_raw':after_label(text,[r'Duration\s+of\s+Observation',r'Period\s+of\s+Observation']),'colour_raw':after_label(text,[r'What\s+was\s+the\s+colou?r\s+of\s+the\s+(?:Light|light|object)',r'colou?r\s+of\s+the\s+light\s+or\s+object']),'shape_raw':after_label(text,[r'What\s+was\s+its\s+apparent\s+shape']),'sound_raw':after_label(text,[r'Was\s+there\s+any\s+Sound',r'Was\s+there\s+any\s+sound'])}

def main():
 ap=argparse.ArgumentParser();ap.add_argument('--input-dir',required=True);ap.add_argument('--output-dir',required=True);a=ap.parse_args();inp=locate(Path(a.input_dir),'AUSTRALIA_GOVERNMENT_UFO_SOURCE_SNAPSHOT_v0.4.0.sqlite');out=Path(a.output_dir);shutil.rmtree(out,ignore_errors=True);out.mkdir(parents=True)
 db=out/'AUSTRALIA_UFO_IMPORT_MODULE_v0.2.0.sqlite';shutil.copyfile(inp,db);con=sqlite3.connect(db);con.row_factory=sqlite3.Row;con.execute('pragma foreign_keys=on')
 con.executescript('''
 CREATE TABLE case_candidate(candidate_id TEXT PRIMARY KEY,record_type TEXT NOT NULL,title TEXT NOT NULL,candidate_status TEXT NOT NULL,source_collection TEXT NOT NULL,source_file_id INTEGER REFERENCES source_file(source_file_id),access_copy_id INTEGER NOT NULL REFERENCES text_access_copy(access_copy_id),page_start INTEGER NOT NULL,page_end INTEGER NOT NULL,source_url TEXT NOT NULL,extraction_method TEXT NOT NULL,extraction_confidence TEXT NOT NULL,special_kind TEXT,event_date_raw TEXT,observer_raw TEXT,location_raw TEXT,duration_raw TEXT,colour_raw TEXT,shape_raw TEXT,sound_raw TEXT,date_mentions_json TEXT,time_mentions_json TEXT,encounter_class_candidate TEXT,source_text_sha256 TEXT NOT NULL,source_text TEXT NOT NULL);
 CREATE TABLE candidate_field(field_id INTEGER PRIMARY KEY,candidate_id TEXT NOT NULL REFERENCES case_candidate(candidate_id),field_name TEXT NOT NULL,raw_value TEXT NOT NULL,quality_state TEXT NOT NULL);
 CREATE TABLE candidate_source_page(candidate_id TEXT NOT NULL REFERENCES case_candidate(candidate_id),access_copy_id INTEGER NOT NULL REFERENCES text_access_copy(access_copy_id),page_number INTEGER NOT NULL,text_sha256 TEXT NOT NULL,PRIMARY KEY(candidate_id,access_copy_id,page_number));
 CREATE TABLE event_fragment(fragment_id TEXT PRIMARY KEY,access_copy_id INTEGER NOT NULL REFERENCES text_access_copy(access_copy_id),source_file_id INTEGER REFERENCES source_file(source_file_id),page_number INTEGER NOT NULL,signal_codes_json TEXT NOT NULL,date_mentions_json TEXT NOT NULL,text_sha256 TEXT NOT NULL,text TEXT NOT NULL);
 CREATE TABLE event_cluster(cluster_id TEXT PRIMARY KEY,access_copy_id INTEGER NOT NULL REFERENCES text_access_copy(access_copy_id),source_file_id INTEGER REFERENCES source_file(source_file_id),page_start INTEGER NOT NULL,page_end INTEGER NOT NULL,fragment_count INTEGER NOT NULL,cluster_status TEXT NOT NULL,priority TEXT NOT NULL,signal_codes_json TEXT NOT NULL,date_mentions_json TEXT NOT NULL,overlapping_candidate_count INTEGER NOT NULL);
 CREATE TABLE case_chronology_fact(fact_id INTEGER PRIMARY KEY,candidate_id TEXT NOT NULL REFERENCES case_candidate(candidate_id),sequence_no INTEGER NOT NULL,fact_role TEXT NOT NULL,raw_value TEXT NOT NULL,quality_state TEXT NOT NULL,source_basis TEXT NOT NULL);
 CREATE TABLE case_narrative_seed(candidate_id TEXT PRIMARY KEY REFERENCES case_candidate(candidate_id),seed_status TEXT NOT NULL,narrative_text TEXT NOT NULL,quality_note TEXT NOT NULL);
 CREATE TABLE entity_candidate(entity_id TEXT PRIMARY KEY,record_type TEXT NOT NULL,display_name TEXT NOT NULL,quality_state TEXT NOT NULL,source_candidate_id TEXT REFERENCES case_candidate(candidate_id),source_field_name TEXT,source_raw_value TEXT NOT NULL);
 CREATE TABLE candidate_entity_link(candidate_id TEXT NOT NULL REFERENCES case_candidate(candidate_id),entity_id TEXT NOT NULL REFERENCES entity_candidate(entity_id),relationship_role TEXT NOT NULL,PRIMARY KEY(candidate_id,entity_id,relationship_role));
 CREATE TABLE candidate_official_provenance(candidate_id TEXT PRIMARY KEY REFERENCES case_candidate(candidate_id),source_file_id INTEGER REFERENCES source_file(source_file_id),discovery_entry_number INTEGER,official_item_page_http_200 INTEGER,official_first_page_binary_reachable INTEGER,official_recordsearch_item_url TEXT,official_recordsearch_view_url TEXT);
 ''')
 pages_by_copy={}
 for c in con.execute('select access_copy_id from text_access_copy order by access_copy_id'):
  cid=c[0];pages_by_copy[cid]={r['page_number']:r for r in con.execute('select page_number,text,text_sha256 from text_access_page where access_copy_id=? order by page_number',(cid,))}
 frag_rows=[]
 for cid,pages in pages_by_copy.items():
  sf=src_file_for_copy(con,cid)
  for pn,r in pages.items():
   text=r['text'] or '';low=text.lower();signals=[]
   if REPORT_RX.search(text):signals.append('REPORT_HEADER')
   if re.search(r'date\s+and\s+time\s+of\s+observation',text,re.I):signals.append('OBSERVATION_DATE_FIELD')
   if re.search(r'name\s+of\s+observers?',text,re.I):signals.append('OBSERVER_FIELD')
   if 'unusual aerial' in low:signals.append('UNUSUAL_AERIAL_TERM')
   if 'unidentified flying object' in low or 'u.f.o' in low or ' ufo' in low:signals.append('UFO_TERM')
   if 'flying saucer' in low:signals.append('FLYING_SAUCER_TERM')
   dates=[clean(x) for x in DATE_RX.findall(text)][:20]
   if dates:signals.append('DATE_MENTION')
   if signals and (len(signals)>=2 or 'REPORT_HEADER' in signals or start_score(text)>=2):
    fid=f'AU-NAA-FRAG-C{cid:02d}-P{pn:04d}';con.execute('insert into event_fragment values(?,?,?,?,?,?,?,?)',(fid,cid,sf,pn,json.dumps(signals),json.dumps(dates,ensure_ascii=False),r['text_sha256'],text));frag_rows.append((cid,pn,sf,signals,dates))
 starts_by_copy={}
 for cid,pages in pages_by_copy.items():
  sf=src_file_for_copy(con,cid);starts=[]
  for pn,r in pages.items():
   sc=start_score(r['text'] or '')
   if sc>=2 and (is_object_report(r['text'] or '') or sf is not None):starts.append((pn,sc))
  starts_by_copy[cid]=sorted(starts)
 candidate_count=0
 for cid,starts in starts_by_copy.items():
  pages=pages_by_copy[cid];sf=src_file_for_copy(con,cid);url=con.execute('select url from text_access_copy where access_copy_id=?',(cid,)).fetchone()[0]
  for i,(pn,sc) in enumerate(starts):
   nextpn=starts[i+1][0] if i+1<len(starts) else None;end=pn
   for q in range(pn+1,pn+4):
    if q not in pages or (nextpn is not None and q>=nextpn):break
    end=q
   full='\n\n[CONTINUATION PAGE]\n\n'.join(pages[q]['text'] or '' for q in range(pn,end+1) if q in pages);vals=fields(full);dates=[clean(x) for x in DATE_RX.findall(full)][:20];times=[clean(x) for x in TIME_RX.findall(full)][:20];candidate_id=f'AU-NAA-CASE-C{cid:02d}-P{pn:04d}';candidate_count+=1
   con.execute('insert into case_candidate values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(candidate_id,'CASE_EVENT',f'Australian government UFO report — source copy {cid}, page {pn}','SOURCE_CANDIDATE','AUSTRALIA_NAA_RAAF',sf,cid,pn,end,url,'REPORT_FORM_START_FIELDS_V2','HIGH' if sc>=4 else 'MEDIUM',None,vals['event_date_raw'],vals['observer_raw'],vals['location_raw'],vals['duration_raw'],vals['colour_raw'],vals['shape_raw'],vals['sound_raw'],json.dumps(dates,ensure_ascii=False),json.dumps(times,ensure_ascii=False),None,sha_text(full),full))
   for fname,val in vals.items():
    if val:con.execute('insert into candidate_field(candidate_id,field_name,raw_value,quality_state) values(?,?,?,?)',(candidate_id,fname,val,field_quality(fname,val)))
   for q in range(pn,end+1):
    if q in pages:con.execute('insert into candidate_source_page values(?,?,?,?)',(candidate_id,cid,q,pages[q]['text_sha256']))
   if sf is not None:
    op=con.execute('''select p.discovery_entry_number,p.item_page_http_200,p.first_page_binary_reachable,o.official_recordsearch_item_url,o.official_recordsearch_view_url from source_file_official_probe p join official_probe_result o on o.discovery_entry_number=p.discovery_entry_number where p.source_file_id=?''',(sf,)).fetchone()
    if op:con.execute('insert into candidate_official_provenance values(?,?,?,?,?,?,?)',(candidate_id,sf,*tuple(op)))
   fact_spec=[('OBSERVATION_TIME','event_date_raw'),('OBSERVER','observer_raw'),('EVENT_LOCATION','location_raw'),('DURATION','duration_raw'),('COLOUR','colour_raw'),('SHAPE','shape_raw'),('SOUND','sound_raw')];facts=[]
   for role,fname in fact_spec:
    val=vals[fname];q=field_quality(fname,val)
    if val and q!='LOW':facts.append((role,fname,val,q))
   for seq,(role,fname,val,q) in enumerate(facts,1):con.execute('insert into case_chronology_fact(candidate_id,sequence_no,fact_role,raw_value,quality_state,source_basis) values(?,?,?,?,?,?)',(candidate_id,seq,role,val,q,fname))
   parts=[]
   for fname,prefix in [('event_date_raw','The source report dates the observation as '),('observer_raw','The observer is recorded as '),('location_raw','The observation location is recorded as '),('duration_raw','The reported duration was ')]:
    if vals[fname] and field_quality(fname,vals[fname])=='HIGH':parts.append(prefix+vals[fname]+'.')
   desc=[]
   for fname,label in [('colour_raw','colour'),('shape_raw','shape')]:
    if vals[fname] and field_quality(fname,vals[fname])=='HIGH':desc.append(label+': '+vals[fname])
   if desc:parts.append('The object description records '+'; '.join(desc)+'.')
   if vals['sound_raw'] and field_quality('sound_raw',vals['sound_raw'])=='HIGH':parts.append('The sound field records '+vals['sound_raw']+'.')
   seed=' '.join(parts) if parts else 'No reliable narrative seed was generated from machine-readable form fields; use the preserved source pages for manual chronological reconstruction.'
   con.execute('insert into case_narrative_seed values(?,?,?,?)',(candidate_id,'DRAFT_SOURCE_ORDERED_SEED' if parts else 'SOURCE_REVIEW_REQUIRED',seed,'Not a final Atlas narrative; preserves only readable form-field order and does not infer missing event phases.'))
   for rtype,fname,role in [('PERSON','observer_raw','OBSERVER'),('LOCATION','location_raw','EVENT_LOCATION')]:
    val=vals[fname];q=field_quality(fname,val)
    if val and q=='HIGH':
     eid=f'{candidate_id}-{rtype[:3]}';con.execute('insert into entity_candidate values(?,?,?,?,?,?,?)',(eid,rtype,val,q,candidate_id,fname,val));con.execute('insert into candidate_entity_link values(?,?,?)',(candidate_id,eid,role))
 specials=[('AU-NAA-CASE-MAWSON-19580717','Mawson/Taylor Glacier visual phenomena — 17 July 1958',18,1,3),('AU-NAA-CASE-WEWAK-19600715','Unidentified light, Wewak/Maralinga — 15 July 1960',21,1,2)]
 for candidate_id,title,cid,start,end in specials:
  pages=pages_by_copy.get(cid,{});actual=[q for q in range(start,end+1) if q in pages]
  if not actual:continue
  full='\n\n[CONTINUATION PAGE]\n\n'.join(pages[q]['text'] or '' for q in actual);sf=src_file_for_copy(con,cid);url=con.execute('select url from text_access_copy where access_copy_id=?',(cid,)).fetchone()[0]
  con.execute('insert into case_candidate values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(candidate_id,'CASE_EVENT',title,'SOURCE_CANDIDATE','AUSTRALIA_NAA_RAAF',sf,cid,min(actual),max(actual),url,'EXPLICIT_NON_FORM_SOURCE_REPORT','HIGH','NON_FORM_SOURCE_REPORT',None,None,None,None,None,None,None,json.dumps([clean(x) for x in DATE_RX.findall(full)][:20],ensure_ascii=False),json.dumps([clean(x) for x in TIME_RX.findall(full)][:20],ensure_ascii=False),None,sha_text(full),full))
  for q in actual:con.execute('insert into candidate_source_page values(?,?,?,?)',(candidate_id,cid,q,pages[q]['text_sha256']))
  con.execute('insert into case_narrative_seed values(?,?,?,?)',(candidate_id,'SOURCE_REVIEW_REQUIRED','This non-form incident is preserved as a high-confidence case candidate, but its complete chronological narrative must be reconstructed directly from the linked source pages.','No form-field narrative generated.'))
 bycopy=collections.defaultdict(list)
 for cid,pn,sf,signals,dates in frag_rows:bycopy[cid].append((pn,sf,signals,dates))
 cluster_count=unresolved=0
 for cid,rows in bycopy.items():
  rows=sorted(rows);groups=[];cur=[];prev=None
  for row in rows:
   if prev is None or row[0]-prev<=2:cur.append(row)
   else:groups.append(cur);cur=[row]
   prev=row[0]
  if cur:groups.append(cur)
  for g in groups:
   cluster_count+=1;start=g[0][0];end=g[-1][0];sf=g[0][1];signals=sorted({s for x in g for s in x[2]});dates=[]
   for x in g:
    for d in x[3]:
     if d and d not in dates:dates.append(d)
   overlaps=con.execute('select count(distinct candidate_id) from candidate_source_page where access_copy_id=? and page_number between ? and ?',(cid,start,end)).fetchone()[0];status='RESOLVED_TO_CASE_CANDIDATE' if overlaps else 'UNRESOLVED_CASE_BOUNDARY'
   if status.startswith('UNRESOLVED'):unresolved+=1
   priority='HIGH' if ('REPORT_HEADER' in signals or ('OBSERVER_FIELD' in signals and dates)) else ('MEDIUM' if dates and any(s in signals for s in ('UFO_TERM','UNUSUAL_AERIAL_TERM','FLYING_SAUCER_TERM')) else 'LOW')
   con.execute('insert into event_cluster values(?,?,?,?,?,?,?,?,?,?,?)',(f'AU-NAA-CLUSTER-C{cid:02d}-P{start:04d}-{end:04d}',cid,sf,start,end,len(g),status,priority,json.dumps(signals),json.dumps(dates[:30],ensure_ascii=False),overlaps))
 con.commit();quick=con.execute('pragma integrity_check').fetchone()[0];fk=con.execute('pragma foreign_key_check').fetchall()
 summary={'status':'PASS' if quick=='ok' and not fk and candidate_count>=100 else 'FAIL','report_form_case_candidates':candidate_count,'explicit_non_form_candidates':2,'total_case_candidates':con.execute('select count(*) from case_candidate').fetchone()[0],'candidate_fields':con.execute('select count(*) from candidate_field').fetchone()[0],'high_quality_entity_candidates':con.execute('select count(*) from entity_candidate').fetchone()[0],'chronology_facts':con.execute('select count(*) from case_chronology_fact').fetchone()[0],'narrative_seeds':con.execute('select count(*) from case_narrative_seed').fetchone()[0],'official_provenance_links':con.execute('select count(*) from candidate_official_provenance').fetchone()[0],'event_fragments':con.execute('select count(*) from event_fragment').fetchone()[0],'event_clusters':cluster_count,'unresolved_event_clusters':unresolved,'encounter_class_policy':'No CE class assigned mechanically. CE classification remains null until the event narrative supports an independent UFO-spec determination.','narrative_policy':'Narrative seeds are explicitly non-final. Final Atlas cases still require complete chronological reconstruction from source pages; missing event phases are never invented.','sqlite_integrity_check':quick,'foreign_key_violations':len(fk)}
 (out/'SUMMARY.json').write_text(json.dumps(summary,indent=2)+'\n')
 for filename,query in [('CASE_CANDIDATES.csv','select candidate_id,title,source_file_id,access_copy_id,page_start,page_end,extraction_confidence,event_date_raw,observer_raw,location_raw,duration_raw,colour_raw,shape_raw,sound_raw from case_candidate order by candidate_id'),('EVENT_CLUSTERS.csv','select * from event_cluster order by access_copy_id,page_start'),('ENTITY_CANDIDATES.csv','select * from entity_candidate order by record_type,display_name'),('CHRONOLOGY_FACTS.csv','select * from case_chronology_fact order by candidate_id,sequence_no'),('OFFICIAL_PROVENANCE.csv','select * from candidate_official_provenance order by candidate_id')]:
  with (out/filename).open('w',newline='',encoding='utf-8') as f:
   rs=con.execute(query);w=csv.writer(f);w.writerow([d[0] for d in rs.description]);w.writerows(rs)
 con.close();checks=[]
 for q in sorted(out.rglob('*')):
  if q.is_file() and q.name!='SHA256SUMS.txt':checks.append(f'{sha_file(q)}  {q.relative_to(out).as_posix()}')
 (out/'SHA256SUMS.txt').write_text('\n'.join(checks)+'\n');print(json.dumps(summary,indent=2))
 if summary['status']!='PASS':raise SystemExit(1)
if __name__=='__main__':main()
