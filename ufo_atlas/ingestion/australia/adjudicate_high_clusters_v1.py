#!/usr/bin/env python3
"""Adjudicate high-priority unresolved Australian UFO source clusters.

Conservative source-review stage: promotes only explicitly reviewed single-event clusters,
links continuation/transmittal/official-response documents without turning them into duplicate
cases, marks multi-event source blocks for later split, preserves all original pages/candidates,
and assigns neither Close Encounter classes nor final narratives.
"""
from __future__ import annotations
import argparse,csv,hashlib,json,re,shutil,sqlite3
from pathlib import Path

PROMOTE = {
 "AU-NAA-CLUSTER-C04-P0008-0008": ("NARRATIVE_GOVERNMENT_CASE","HIGH","Single-event RAAF interview report describing Constance MacDonald sighting."),
 "AU-NAA-CLUSTER-C04-P0114-0114": ("REPORT_FORM_CASE","HIGH","Single report-form event missed by OCR-tolerant first-pass start detection."),
 "AU-NAA-CLUSTER-C04-P0130-0130": ("NARRATIVE_GOVERNMENT_CASE","HIGH","Single-event RAAF report summarizing Alan Light observation."),
 "AU-NAA-CLUSTER-C05-P0176-0176": ("REPORT_FORM_CASE","HIGH","Observer report form with case fields."),
 "AU-NAA-CLUSTER-C06-P0037-0037": ("MEDIA_REPORT_CASE","MEDIUM","Contemporary press clipping describing a distinct Northern Tasmania sighting; preserved as media-source case candidate."),
 "AU-NAA-CLUSTER-C08-P0037-0038": ("REPORT_FORM_CASE","HIGH","Two-page observer report form from Denmark, Western Australia."),
 "AU-NAA-CLUSTER-C08-P0091-0092": ("NARRATIVE_GOVERNMENT_CASE","HIGH","RAAF control-tower report of 5 May 1962 Richmond CTA observation."),
 "AU-NAA-CLUSTER-C08-P0149-0149": ("REPORT_FORM_CASE","HIGH","Observer report form missed because of OCR damage."),
 "AU-NAA-CLUSTER-C09-P0104-0106": ("REPORT_FORM_CASE","HIGH","RAAF transmittal plus complete single-event Launceston report form, 29 June 1965."),
 "AU-NAA-CLUSTER-C09-P0173-0175": ("REPORT_FORM_CASE","HIGH","Complete Glen Morice observer report form, 5 May 1965."),
 "AU-NAA-CLUSTER-C10-P0158-0158": ("NARRATIVE_GOVERNMENT_CASE","HIGH","Single-event Wewak report dated 1 January 1966 with time, position and observation details."),
 "AU-NAA-CLUSTER-C10-P0198-0200": ("REPORT_FORM_CASE","HIGH","RAAF transmittal plus C. Tyeson single-event report form, 27 November 1965."),
 "AU-NAA-CLUSTER-C13-P0022-0023": ("REPORT_FORM_CASE","HIGH","RAAF Darwin transmittal plus A.G. Truman observer form."),
 "AU-NAA-CLUSTER-C13-P0037-0037": ("REPORT_FORM_CASE","HIGH","Single observer report form missed by OCR start parser."),
 "AU-NAA-CLUSTER-C19-P0034-0034": ("NARRATIVE_GOVERNMENT_CASE","HIGH","RAAF summary of L. Vollprecht 23 September 1955 observation; source explanation retained as source disposition."),
 "AU-NAA-CLUSTER-C19-P0097-0098": ("REPORT_FORM_CASE","HIGH","Police transmittal plus Charles Aubrey Bolton observer report form."),
 "AU-NAA-CLUSTER-C19-P0109-0109": ("REPORT_FORM_CASE","HIGH","Appendix B observer report by D.W. Horton, 29 October 1951."),
}
DECISIONS = {
 "AU-NAA-CLUSTER-C03-P0203-0206": ("MULTI_EVENT_SOURCE_BLOCK_NEEDS_SPLIT",None,"Boianai correspondence contains multiple sightings and witnesses; not one case."),
 "AU-NAA-CLUSTER-C04-P0011-0011": ("CASE_RELATED_TRANSMITTAL",None,"Damaged RAAF transmittal references R.V. Geist report; attachment identity not safely resolved here."),
 "AU-NAA-CLUSTER-C04-P0127-0127": ("CONTINUATION_OF_CASE","AU-NAA-CASE-C04-P0121","Continuation page explicitly labelled report on aerial object observed (cont.)."),
 "AU-NAA-CLUSTER-C05-P0224-0224": ("MULTI_EVENT_TRANSMITTAL_NEEDS_SPLIT",None,"Transmittal explicitly encloses separate Collins and Feodoroff reports."),
 "AU-NAA-CLUSTER-C05-P0233-0233": ("CASE_RELATED_TRANSMITTAL",None,"Single-case Mathewson transmittal; attachment match remains uncertain."),
 "AU-NAA-CLUSTER-C06-P0118-0122": ("DUPLICATE_MULTI_EVENT_SOURCE_BLOCK",None,"Substantially duplicated Boianai correspondence present in another source copy; duplicate relationship retained in decision basis."),
 "AU-NAA-CLUSTER-C06-P0137-0137": ("MULTI_EVENT_TRANSMITTAL_NEEDS_SPLIT",None,"RAAF report states that three sighting report forms were attached for Mr and Mrs Moore."),
 "AU-NAA-CLUSTER-C07-P0015-0015": ("CASE_RELATED_OFFICIAL_RESPONSE",None,"Official response to prior 25 November 1972 report; event source attachment is separate."),
 "AU-NAA-CLUSTER-C07-P0030-0030": ("CASE_RELATED_OFFICIAL_RESPONSE",None,"Official response to prior 19 October 1972 report."),
 "AU-NAA-CLUSTER-C07-P0074-0074": ("CASE_RELATED_OFFICIAL_RESPONSE",None,"Official response to prior 6 January 1973 report."),
 "AU-NAA-CLUSTER-C07-P0132-0135": ("MULTI_EVENT_OFFICIAL_CORRESPONDENCE",None,"Correspondence explicitly discusses multiple unusual aerial sighting reports."),
 "AU-NAA-CLUSTER-C07-P0141-0144": ("CASE_RELATED_OFFICIAL_RESPONSE",None,"Official correspondence on P. Clelland report and related material; not promoted as a standalone event."),
 "AU-NAA-CLUSTER-C07-P0162-0163": ("CASE_RELATED_OFFICIAL_RESPONSE",None,"Official response to G.P. Frewin 2 January 1973 report."),
 "AU-NAA-CLUSTER-C07-P0186-0186": ("CASE_RELATED_TRANSMITTAL",None,"Single Grosvenor case transmittal; enclosed report must be matched before promotion."),
 "AU-NAA-CLUSTER-C07-P0192-0192": ("CASE_RELATED_TRANSMITTAL",None,"Single Ian Maurice case transmittal; enclosed report begins separately."),
 "AU-NAA-CLUSTER-C07-P0227-0227": ("CASE_RELATED_OFFICIAL_RESPONSE",None,"Official response to Digran/Tew report."),
 "AU-NAA-CLUSTER-C07-P0239-0239": ("CASE_RELATED_OFFICIAL_RESPONSE",None,"Official response to D.L. Owen Bateau Bay report."),
 "AU-NAA-CLUSTER-C08-P0085-0085": ("CASE_RELATED_TRANSMITTAL",None,"Tennant Creek UFO report forwarding page; attached event report not in this cluster."),
 "AU-NAA-CLUSTER-C08-P0088-0088": ("CASE_RELATED_TRANSMITTAL","AU-NAA-CASE-ADJ-C08-P0091","Richmond Control Zone forwarding page for the promoted 5 May 1962 report."),
 "AU-NAA-CLUSTER-C08-P0107-0108": ("MULTI_EVENT_OFFICIAL_CORRESPONDENCE",None,"Pages combine Drury 1953 correspondence and a separate Moore report forwarding page."),
 "AU-NAA-CLUSTER-C08-P0134-0135": ("CASE_RELATED_TRANSMITTAL",None,"RAAF forwarding correspondence; attachment requires separate case matching."),
 "AU-NAA-CLUSTER-C08-P0164-0167": ("MULTI_EVENT_OFFICIAL_CORRESPONDENCE",None,"Pages contain multiple letters/reports including Cairns-area sightings; not a single event."),
 "AU-NAA-CLUSTER-C09-P0113-0113": ("MULTI_EVENT_TRANSMITTAL_NEEDS_SPLIT",None,"Port Moresby transmittal explicitly references Keroroeea Bay and a separate Mwatebu Village event."),
 "AU-NAA-CLUSTER-C10-P0019-0019": ("MULTI_DOCUMENT_TRANSMITTAL",None,"Page forwards an observer report and a separate newspaper information request."),
 "AU-NAA-CLUSTER-C12-P0103-0103": ("CASE_RELATED_TRANSMITTAL",None,"Civil Aviation forwarding page; attached report is not contained in this cluster."),
 "AU-NAA-CLUSTER-C17-P0001-0005": ("DISCOVERY_INDEX_NOT_CASE",None,"Published archival file index/discovery aid, not an event record."),
 "AU-NAA-CLUSTER-C19-P0042-0042": ("MULTI_EVENT_TRANSMITTAL_NEEDS_SPLIT",None,"Police letter returns forms completed by two observers, John Arnold Morris and Gary Martin."),
 "AU-NAA-CLUSTER-C19-P0077-0077": ("CASE_RELATED_OFFICIAL_RESPONSE",None,"RAAF/police correspondence requests completion of an additional pro-forma; event boundary unresolved."),
 "AU-NAA-CLUSTER-C19-P0093-0093": ("MULTI_EVENT_TRANSMITTAL_NEEDS_SPLIT",None,"Police forwarding page refers to forms completed for multiple persons."),
}
DATE_RX=re.compile(r'\b(?:[0-3]?\d[ /.-](?:0?\d|[A-Za-z]{3,9})[ /.-](?:19|20)?\d{2}|[0-3]?\d\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(?:19|20)?\d{2})\b',re.I)

def locate(root:Path,name:str)->Path:
 hits=list(root.rglob(name))
 if len(hits)!=1:raise RuntimeError(f'Expected one {name}; found {len(hits)}')
 return hits[0]
def sha_file(p:Path)->str:
 h=hashlib.sha256()
 with p.open('rb') as f:
  for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
 return h.hexdigest()
def sha_text(s:str)->str:return hashlib.sha256(s.encode()).hexdigest()
def clean(s:str|None)->str|None:
 if not s:return None
 x=re.sub(r'\s+',' ',s).strip(' .,:;|-');return x or None
def after(text:str,patterns:list[str],limit=260):
 for pat in patterns:
  m=re.search(pat,text,re.I)
  if not m:continue
  t=text[m.end():m.end()+limit]
  stop=re.search(r'\s(?:\d{1,2}\s*[.)]|\d{1,2}\.)\s+[A-Z]',t)
  if stop:t=t[:stop.start()]
  return clean(t)
 return None
def extract_fields(text:str):
 return {'event_date_raw':after(text,[r'Date\s+and\s+Time\s+(?:of|or)\s+Observation',r'Date:\s*']),'observer_raw':after(text,[r'Name\s+of\s+Observers?',r'Name\s+of\s+observer']),'location_raw':after(text,[r"Observers?\s*'?s?\s+location(?:\s+at\s+time\s+of\s+sighting)?",r'Address\s+(?:or|of)\s+Observer']),'duration_raw':after(text,[r'Duration\s+of\s+Observation',r'Period\s+of\s+Observation']),'colour_raw':after(text,[r'What\s+was\s+the\s+colou?r\s+of\s+the\s+(?:light|object)']),'shape_raw':after(text,[r'What\s+was\s+its\s+apparent\s+shape']),'sound_raw':after(text,[r'Was\s+there\s+any\s+sound'])}
def quality(v):
 if not v:return 'MISSING'
 letters=sum(c.isalpha() for c in v);punct=sum((not c.isalnum()) and not c.isspace() for c in v)
 if len(v)>180 or letters<2 or punct/max(1,len(v))>0.25:return 'LOW'
 return 'HIGH' if letters>=5 and punct/max(1,len(v))<0.14 else 'MEDIUM'
def cluster_text(con,cid,start,end):
 rows=con.execute('select page_number,text,text_sha256 from text_access_page where access_copy_id=? and page_number between ? and ? order by page_number',(cid,start,end)).fetchall()
 return rows,'\n\n[CONTINUATION PAGE]\n\n'.join((r['text'] or '') for r in rows)
def add_promoted_case(con,cluster_id,kind,confidence):
 cl=con.execute('select * from event_cluster where cluster_id=?',(cluster_id,)).fetchone();cid,st,en=cl['access_copy_id'],cl['page_start'],cl['page_end'];pages,text=cluster_text(con,cid,st,en);sf=cl['source_file_id'];url=con.execute('select url from text_access_copy where access_copy_id=?',(cid,)).fetchone()[0];candidate_id=f'AU-NAA-CASE-ADJ-C{cid:02d}-P{st:04d}'
 if con.execute('select 1 from case_candidate where candidate_id=?',(candidate_id,)).fetchone():return candidate_id
 vals=extract_fields(text);dates=[clean(x) for x in DATE_RX.findall(text)][:20];title=f'Australian government UFO case — source copy {cid}, pages {st}-{en}'
 con.execute('insert into case_candidate values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(candidate_id,'CASE_EVENT',title,'SOURCE_CANDIDATE','AUSTRALIA_NAA_RAAF',sf,cid,st,en,url,'HIGH_CLUSTER_SOURCE_ADJUDICATION',confidence,kind,vals['event_date_raw'],vals['observer_raw'],vals['location_raw'],vals['duration_raw'],vals['colour_raw'],vals['shape_raw'],vals['sound_raw'],json.dumps(dates,ensure_ascii=False),'[]',None,sha_text(text),text))
 for fn,val in vals.items():
  if val:con.execute('insert into candidate_field(candidate_id,field_name,raw_value,quality_state) values(?,?,?,?)',(candidate_id,fn,val,quality(val)))
 for p in pages:con.execute('insert into candidate_source_page values(?,?,?,?)',(candidate_id,cid,p['page_number'],p['text_sha256']))
 if sf is not None:
  op=con.execute("select p.discovery_entry_number,p.item_page_http_200,p.first_page_binary_reachable,o.official_recordsearch_item_url,o.official_recordsearch_view_url from source_file_official_probe p join official_probe_result o on o.discovery_entry_number=p.discovery_entry_number where p.source_file_id=?",(sf,)).fetchone()
  if op:con.execute('insert into candidate_official_provenance values(?,?,?,?,?,?,?)',(candidate_id,sf,*tuple(op)))
 seq=0
 for role,fn in [('OBSERVATION_TIME','event_date_raw'),('OBSERVER','observer_raw'),('EVENT_LOCATION','location_raw'),('DURATION','duration_raw'),('COLOUR','colour_raw'),('SHAPE','shape_raw'),('SOUND','sound_raw')]:
  val=vals[fn];q=quality(val)
  if val and q!='LOW':seq+=1;con.execute('insert into case_chronology_fact(candidate_id,sequence_no,fact_role,raw_value,quality_state,source_basis) values(?,?,?,?,?,?)',(candidate_id,seq,role,val,q,fn))
 con.execute('insert into case_narrative_seed values(?,?,?,?)',(candidate_id,'SOURCE_REVIEW_REQUIRED','Promoted from a manually adjudicated source cluster. Reconstruct the complete chronological narrative directly from the linked source pages before canonical import.','No event phase is inferred and no CE class is assigned.'))
 return candidate_id

def main():
 ap=argparse.ArgumentParser();ap.add_argument('--input-dir',required=True);ap.add_argument('--output-dir',required=True);a=ap.parse_args();src=locate(Path(a.input_dir),'AUSTRALIA_UFO_IMPORT_MODULE_v0.2.0.sqlite');out=Path(a.output_dir);shutil.rmtree(out,ignore_errors=True);out.mkdir(parents=True);db=out/'AUSTRALIA_UFO_IMPORT_MODULE_v0.2.1.sqlite';shutil.copyfile(src,db)
 con=sqlite3.connect(db);con.row_factory=sqlite3.Row;con.execute('pragma foreign_keys=on');con.executescript("CREATE TABLE cluster_adjudication(cluster_id TEXT PRIMARY KEY REFERENCES event_cluster(cluster_id),previous_status TEXT NOT NULL,decision TEXT NOT NULL,linked_case_id TEXT,decision_basis TEXT NOT NULL,review_level TEXT NOT NULL); CREATE TABLE cluster_case_link(cluster_id TEXT NOT NULL REFERENCES event_cluster(cluster_id),candidate_id TEXT NOT NULL REFERENCES case_candidate(candidate_id),relationship TEXT NOT NULL,PRIMARY KEY(cluster_id,candidate_id,relationship));")
 high=[r['cluster_id'] for r in con.execute("select cluster_id from event_cluster where cluster_status='UNRESOLVED_CASE_BOUNDARY' and priority='HIGH' order by cluster_id")];expected=set(PROMOTE)|set(DECISIONS);missing=set(high)-expected;extra=expected-set(high)
 if missing or extra:raise SystemExit(f'Adjudication map mismatch missing={sorted(missing)} extra={sorted(extra)}')
 promoted=[]
 for clid in high:
  prev='UNRESOLVED_CASE_BOUNDARY'
  if clid in PROMOTE:
   kind,conf,basis=PROMOTE[clid];cid=add_promoted_case(con,clid,kind,conf);decision='PROMOTED_SINGLE_EVENT_CASE';linked=cid;promoted.append(cid);con.execute("update event_cluster set cluster_status='ADJUDICATED_PROMOTED_CASE' where cluster_id=?",(clid,));con.execute('insert into cluster_case_link values(?,?,?)',(clid,cid,'PROMOTED_FROM_SOURCE_CLUSTER'))
  else:
   decision,linked,basis=DECISIONS[clid];con.execute('update event_cluster set cluster_status=? where cluster_id=?',('ADJUDICATED_'+decision,clid))
   if linked and linked.startswith('AU-NAA-CASE-') and con.execute('select 1 from case_candidate where candidate_id=?',(linked,)).fetchone():
    rel='CONTINUATION_OF' if decision=='CONTINUATION_OF_CASE' else 'SOURCE_DOCUMENT_FOR';con.execute('insert into cluster_case_link values(?,?,?)',(clid,linked,rel))
  con.execute('insert into cluster_adjudication values(?,?,?,?,?,?)',(clid,prev,decision,linked,basis,'MANUAL_SOURCE_REVIEW'))
 con.commit();quick=con.execute('pragma integrity_check').fetchone()[0];fk=con.execute('pragma foreign_key_check').fetchall();remaining_high=con.execute("select count(*) from event_cluster where cluster_status='UNRESOLVED_CASE_BOUNDARY' and priority='HIGH'").fetchone()[0];unresolved_any=con.execute("select count(*) from event_cluster where cluster_status='UNRESOLVED_CASE_BOUNDARY'").fetchone()[0];decision_counts=dict(con.execute('select decision,count(*) from cluster_adjudication group by decision order by decision'))
 summary={'status':'PASS' if quick=='ok' and not fk and len(high)==46 and remaining_high==0 and len(promoted)==17 else 'FAIL','high_priority_clusters_adjudicated':len(high),'promoted_new_case_candidates':len(promoted),'total_case_candidates':con.execute('select count(*) from case_candidate').fetchone()[0],'remaining_unresolved_high_priority_clusters':remaining_high,'remaining_unresolved_medium_low_clusters':unresolved_any,'decision_counts':decision_counts,'cluster_case_links':con.execute('select count(*) from cluster_case_link').fetchone()[0],'official_provenance_links':con.execute('select count(*) from candidate_official_provenance').fetchone()[0],'policy':'Only explicitly reviewed single-event clusters were promoted. Transmittals, official responses, indexes, duplicate blocks and multi-event material remain separately typed. Canonical Atlas unchanged; no mechanical CE classes or final narratives.','sqlite_integrity_check':quick,'foreign_key_violations':len(fk)};(out/'SUMMARY.json').write_text(json.dumps(summary,indent=2)+'\n')
 for fn,q in [('CLUSTER_ADJUDICATION.csv','select * from cluster_adjudication order by cluster_id'),('PROMOTED_CASES.csv',"select candidate_id,title,access_copy_id,page_start,page_end,extraction_confidence,special_kind from case_candidate where extraction_method='HIGH_CLUSTER_SOURCE_ADJUDICATION' order by candidate_id"),('REMAINING_UNRESOLVED_CLUSTERS.csv',"select cluster_id,access_copy_id,source_file_id,page_start,page_end,priority,signal_codes_json,date_mentions_json from event_cluster where cluster_status='UNRESOLVED_CASE_BOUNDARY' order by priority,access_copy_id,page_start")]:
  with (out/fn).open('w',newline='',encoding='utf-8') as f:rs=con.execute(q);w=csv.writer(f);w.writerow([d[0] for d in rs.description]);w.writerows(rs)
 con.close();checks=[]
 for p in sorted(out.rglob('*')):
  if p.is_file() and p.name!='SHA256SUMS.txt':checks.append(f'{sha_file(p)}  {p.relative_to(out).as_posix()}')
 (out/'SHA256SUMS.txt').write_text('\n'.join(checks)+'\n');print(json.dumps(summary,indent=2))
 if summary['status']!='PASS':raise SystemExit(1)
if __name__=='__main__':main()
